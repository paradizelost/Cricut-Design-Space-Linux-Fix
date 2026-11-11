#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Read the handshake key out of your own Design Space install.
#
# The machine answers the key exchange with 48 bytes that Design Space's device helper
# (CricutDevice.exe) decrypts with a fixed AES-256 key. The helper builds that key at run time and
# wipes it afterwards, so it is not in the file or in a memory dump. This script catches it in the
# act: it lets the helper connect to a fake machine that replays your own machine's recorded
# replies, stops the helper with Wine's debugger at the decrypt call, and reads the key argument.
#
# The real cutter is not used and can stay unplugged. Design Space opens by itself and connects
# within a minute or so; do not click anything in it.
#
# Usage: extract-handshake-key.sh CAPTURE.jsonl
#   CAPTURE.jsonl: one connection recorded with serial-tap.py (see native/README.md)
# Uses WINEPREFIX (default ~/Games/cricut) and the wine/winedbg on PATH.
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CAP="$(readlink -f "${1:?usage: extract-handshake-key.sh CAPTURE.jsonl}")"
export WINEPREFIX="${WINEPREFIX:-$HOME/Games/cricut}" WINEDEBUG=-all WINEDLLOVERRIDES="winemenubuilder.exe=d"
DS="$WINEPREFIX/drive_c/users/$USER/AppData/Local/Programs/Cricut Design Space"
HELPER="$DS/resources/plugins/device-common/CricutDevice.exe"
OUT="${CRICUT_HANDSHAKE_KEY:-${XDG_CONFIG_HOME:-$HOME/.config}/cricut/handshake.key}"
WORK="$(mktemp -d)"
[ -f "$HELPER" ] || { echo "Design Space's device helper not found under $WINEPREFIX"; exit 1; }
command -v winedbg >/dev/null || { echo "winedbg not found (it ships with Wine)"; exit 1; }

# The decrypt call: push ecx; push 0x30; push eax; push [ebp-0x2c]; call. The key pointer is the last
# push, so it is on top of the stack when the call instruction is reached.
ADDR=$(python3 - "$HELPER" <<'PY'
import re, struct, sys
d = open(sys.argv[1], "rb").read()
hits = [m.end() - 1 for m in re.finditer(rb"\x6a\x30\x50\xff\x75.\xe8", d)]
if len(hits) != 1:
    sys.exit(f"expected one match for the decrypt call, found {len(hits)}: this Design Space build differs")
pe = struct.unpack_from("<I", d, 0x3C)[0]
nsec, opt = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
base = struct.unpack_from("<I", d, pe + 24 + 28)[0]
for i in range(nsec):
    vs, va, rs, raw = struct.unpack_from("<IIII", d, pe + 24 + opt + 40 * i + 8)
    if raw <= hits[0] < raw + rs:
        print("0x%08x" % (base + va + hits[0] - raw))
PY
) || exit 1
echo "decrypt call at $ADDR"

COM3="$WINEPREFIX/dosdevices/com3"
OLD_LINK="$(readlink "$COM3" 2>/dev/null || true)"
cleanup() {
    wineserver -k 2>/dev/null
    [ -n "${FAKE_PID:-}" ] && kill "$FAKE_PID" 2>/dev/null
    if [ -n "$OLD_LINK" ]; then ln -sfn "$OLD_LINK" "$COM3"; else rm -f "$COM3"; fi
    [ -n "${OLD_PORT:-}" ] && wine reg add 'HKLM\Software\Wine\Ports' /v COM3 /d "$OLD_PORT" /f >/dev/null 2>&1
    wineserver -k 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

wineserver -k 2>/dev/null; wineserver -w 2>/dev/null
python3 "$HERE/fake-device.py" "$CAP" "$WORK" >/dev/null 2>&1 &
FAKE_PID=$!
for _ in $(seq 20); do [ -L "$WORK/ttyCricut" ] && break; sleep 0.25; done
[ -L "$WORK/ttyCricut" ] || { echo "fake machine did not start"; exit 1; }
ln -sfn "$WORK/ttyCricut" "$COM3"
# If COM3 is also pinned in the registry, point that at the fake machine too and restore it afterwards
OLD_PORT="$(wine reg query 'HKLM\Software\Wine\Ports' /v COM3 2>/dev/null | tr -d '\r' | awk '$1=="COM3"{print $3}')"
[ -n "$OLD_PORT" ] && wine reg add 'HKLM\Software\Wine\Ports' /v COM3 /d "$WORK/ttyCricut" /f >/dev/null 2>&1
wineserver -w 2>/dev/null

echo "starting Design Space against the fake machine; wait, and do not click anything"
( cd "$DS" && setsid wine "Cricut Design Space.exe" </dev/null >>"$WORK/ds.log" 2>&1 & )

# Attach only after the helper has sent its first command: with a debugger attached from the start
# the helper never opens the port.
for _ in $(seq 300); do [ -s "$WORK/fake.jsonl" ] && break; sleep 1; done
[ -s "$WORK/fake.jsonl" ] || { echo "the helper never contacted the fake machine (is Design Space signed in, and is the cutter registered on COM3?)"; exit 1; }
PID=$(winedbg --command "info proc" 2>/dev/null | awk 'tolower($0) ~ /cricutdevice\.exe/ {print $1; exit}')
[ -n "$PID" ] || { echo "device helper process not found"; exit 1; }
{
    echo "attach 0x$PID"
    echo "break *$ADDR"
    echo "cont"
    echo "x/8x *(int*)(\$esp)"
    echo "detach"
    echo "quit"
} > "$WORK/cmds"
( sleep 6; touch "$WORK/go" ) &      # the fake machine starts answering once the breakpoint is set
timeout 240 winedbg < "$WORK/cmds" > "$WORK/trace.log" 2>&1

KEY=$(python3 - "$WORK/trace.log" <<'PY'
import re, sys
t = open(sys.argv[1], errors="replace").read()
rows = re.findall(r"0x[0-9a-f]+:\s+((?:[0-9a-f]{8}\s*){4})", t)
if "Stopped on breakpoint" in t and len(rows) >= 2:
    words = (rows[-2] + " " + rows[-1]).split()
    print(b"".join(int(w, 16).to_bytes(4, "little") for w in words).hex())
PY
)
[ ${#KEY} -eq 64 ] || { echo "the breakpoint was not reached; nothing was read"; tail -5 "$WORK/trace.log"; exit 1; }

# Check it against your recording: the key must unwrap the recorded reply into a session key that
# decrypts the first encrypted frame Design Space sent, with a valid length, padding and checksum.
python3 - "$CAP" "$KEY" <<'PY' || { echo "the value read does not decrypt your recording; not saved"; exit 1; }
import json, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
dec = lambda k, b: (lambda d: d.update(b) + d.finalize())(Cipher(algorithms.AES(k), modes.ECB()).decryptor())  # device protocol
hk = bytes.fromhex(sys.argv[2]); reply = first = None; seen_c7 = False
for line in open(sys.argv[1]):
    r = json.loads(line); h = r.get("hex", "")
    if r["d"] == "H>D" and h.startswith("0031c7"): seen_c7 = True
    elif r["d"] == "D>H" and seen_c7 and reply is None and h.startswith("0030"): reply = bytes.fromhex(h[4:])
    elif r["d"] == "H>D" and reply is not None and h.startswith("80"):
        n = int(h[:4], 16) & 0x7FFF; first = bytes.fromhex(h[4:4 + 2 * n]); break
if reply is None or first is None: sys.exit("recording has no key exchange followed by an encrypted frame")
p = dec(hk, reply); off = p[0]
q = dec(p[off:off + 32], first) if 1 <= off <= 15 else b"\xff" * 16
n = int.from_bytes(q[:2], "big")
ok = n <= len(q) - 3 and (sum(q[:-1]) & 0xFF) == q[-1] and not any(q[2 + n:-1])
sys.exit(0 if ok else 1)
PY

mkdir -p "$(dirname "$OUT")"
( umask 077; echo "$KEY" > "$OUT" )
echo "handshake key verified against your recording and saved to $OUT"
