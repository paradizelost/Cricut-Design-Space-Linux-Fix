#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fake Cricut on a pty: replays the plaintext replies recorded in a tap capture so CricutDevice.exe
can be studied without touching the real machine.
Stays silent until <workdir>/go exists (gives time to attach a debugger), then answers each plaintext
request with the recorded reply (cf -> recorded nonce, c7 -> recorded 48-byte reply, or the overrides
given with --hello HEX / --rep HEX). Encrypted frames are logged and not answered.
Usage: fake-device.py recorded.jsonl WORKDIR [--hello HEX32] [--rep HEX96]
"""
import json, os, select, sys, time, tty

ROOT = os.path.abspath(sys.argv[2])
os.makedirs(ROOT, exist_ok=True)
LINK, GO = ROOT + "/ttyCricut", ROOT + "/go"
rep_override = bytes.fromhex(sys.argv[sys.argv.index("--rep") + 1]) if "--rep" in sys.argv else None

bufs = {"H>D": b"", "D>H": b""}; frames = []
for line in open(sys.argv[1]):
    r = json.loads(line); d = r["d"]
    if d not in bufs: continue
    bufs[d] += bytes.fromhex(r["hex"]); b = bufs[d]
    while len(b) >= 2:
        h = int.from_bytes(b[:2], "big"); n = h & 0x7FFF
        if len(b) < 2 + n: break
        frames.append((d, bool(h & 0x8000), b[2:2 + n])); b = b[2 + n:]
    bufs[d] = b
table = {}
for i, (d, e, p) in enumerate(frames):
    if e: break
    if d == "H>D" and i + 1 < len(frames) and frames[i + 1][0] == "D>H":
        key = p[:1] if p[0] in (0xCF, 0xC7) else p
        table.setdefault(key, frames[i + 1][2])
if rep_override: table[b"\xc7"] = rep_override
if "--hello" in sys.argv: table[b"\xcf"] = bytes.fromhex(sys.argv[sys.argv.index("--hello") + 1])

master, slave = os.openpty(); tty.setraw(slave)
if os.path.islink(LINK): os.unlink(LINK)
os.symlink(os.ttyname(slave), LINK)
if os.path.exists(GO): os.unlink(GO)
log = open(ROOT + "/fake.jsonl", "w", buffering=1)
print("fake device on", os.ttyname(slave), "log", log.name, flush=True)
t0 = time.monotonic(); buf = b""
while True:
    if not select.select([master], [], [], 0.2)[0]:
        continue
    buf += os.read(master, 65536)
    while len(buf) >= 2:
        h = int.from_bytes(buf[:2], "big"); n = h & 0x7FFF
        if len(buf) < 2 + n: break
        p, enc = buf[2:2 + n], bool(h & 0x8000); buf = buf[2 + n:]
        t = round(time.monotonic() - t0, 4)
        log.write(json.dumps({"t": t, "d": "H>D", "hex": (h.to_bytes(2, "big") + p).hex()}) + "\n")
        if enc or not os.path.exists(GO):
            continue
        key = p[:1] if p and p[0] in (0xCF, 0xC7) else p
        rep = table.get(key)
        if rep is None:
            log.write(json.dumps({"t": t, "d": "note", "msg": "no recorded reply for " + p.hex()}) + "\n"); continue
        if p[0] == 0x3D: time.sleep(0.5)
        out = len(rep).to_bytes(2, "big") + rep
        os.write(master, out)
        log.write(json.dumps({"t": t, "d": "D>H", "hex": out.hex()}) + "\n")
