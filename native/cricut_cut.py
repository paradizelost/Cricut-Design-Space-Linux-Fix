#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native cut on a Cricut Maker 3 (no Design Space): the job flow, driven by the machine's status
and buttons, plus a circle generator for testing. cricut_svg.py uses the Job class to cut SVG files.

The speed, pressure and tool values are the ones Design Space 10.11.128 sent for Smart Paper Sticker
Cardstock loaded without a mat, fine-point blade in clamp B. Only use it with that material loaded
that way: other materials need their own values (see PROTOCOL.md).

Usage: cricut_cut.py --circle CX CY DIAMETER [--segments 96] [--print-path] [--port /dev/ttyACM0]
"""
import sys, time
from cricut_native import Cricut

H = bytes.fromhex
F12_0, F4_0 = "41400000", "40800000"   # 12.0 and 4.0 as big-endian floats

# speed / pressure style tables exactly as recorded (u16 BE values after a 5-byte header)
PROFILE_MEASURE = "310f0f0f0f0f019001900190019003e8019001900190019003e803e803e807d007d009c403e803e80f140f1461a8"
PROFILE_CUT = "310f0f0f0f0f032003200190019003e804b004b00190019003e82710271007d007d009c44e204e200f140f1461a8"
ZERO3E = "3e00020000000000000000000000000000000000"

# path: 9-byte records op | float32 LE x | float32 LE y (inches); op 02 = travel, 00 = cut
MAX_RECORDS = 35   # largest path command the machine has been seen to accept (316 bytes)


def pack_records(records, max_records=MAX_RECORDS):
    """records: list of (op, x, y). Returns a list of 0x43 command payloads of at most max_records each."""
    import struct
    raw = [bytes([op]) + struct.pack("<ff", x, y) for op, x, y in records]
    return [b"\x43" + b"".join(raw[i:i + max_records]) for i in range(0, len(raw), max_records)]


def circle_records(cx, cy, diameter, segments, overcut=0.03):
    import math
    r = diameter / 2
    pts = [(cx + r * math.cos(2 * math.pi * i / segments), cy + r * math.sin(2 * math.pi * i / segments))
           for i in range(segments)] 
    pts.append(pts[0])
    # overcut: carry on a little past the start so the trailing blade closes the outline
    # (Design Space's recorded triangle does the same, by about 0.016 in)
    a = overcut / r
    end = (cx + r * math.cos(a), cy + r * math.sin(a))
    return [(2, *pts[0])] + [(0, *pts[0])] * 5 + [(0, *q) for q in pts[1:]] + [(0, *end), (2, *end)]


def circle_path(cx, cy, diameter, segments=28):
    """Circle as a polyline, laid out like the recorded path: travel to the start, five pen-down
    records on the start point, the outline, then a pen-up record. 28 segments keeps the command at
    35 records (316 bytes), the size the machine has already accepted."""
    import math, struct
    r = diameter / 2
    pts = [(cx + r * math.cos(2 * math.pi * i / segments), cy + r * math.sin(2 * math.pi * i / segments))
           for i in range(segments + 1)]
    pts[-1] = pts[0]
    rec = lambda op, p: bytes([op]) + struct.pack("<ff", *p)
    out = b"\x43" + rec(2, pts[0]) + rec(0, pts[0]) * 5
    out += b"".join(rec(0, q) for q in pts[1:]) + rec(2, pts[0])
    return out


class Job:
    def __init__(self, dev, log=print):
        self.d, self.log = dev, log

    def cmd(self, hexstr, timeout=5.0, note=""):
        p = H(hexstr) if isinstance(hexstr, str) else hexstr
        r = self.d.cmd(p, timeout=timeout)
        self.log(f"  > {p.hex()[:44]}{'..' if len(p) > 22 else ''} -> {r.hex()} {note}")
        return r

    def wait(self, what, cond, timeout, polls=("60", "73")):
        """Poll the machine (same poll types Design Space uses in that phase) until cond(s60, s73, ee)."""
        self.log(f"  waiting: {what}")
        q = {"60": H("60000000"), "73": H("73"), "ee": H("ee00000000")}
        st = {"60": b"\0" * 9, "73": b"\0" * 6, "ee": b"\xee\x01"}
        end = time.time() + timeout
        last = None
        while time.time() < end:
            for k in polls:
                st[k] = self.d.cmd(q[k], timeout=30)   # the machine can go quiet for a while when a feed retries
            snap = tuple(st[k] for k in polls)
            if snap != last:
                self.log("    status " + " ".join(f"{k}={st[k].hex()}" for k in polls))
                last = snap
            if cond(st["60"], st["73"], st["ee"]):
                return st
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for: {what}")

    idle = staticmethod(lambda s60, s73, ee: s60[1] == 0x68)

    def run(self, path, max_y=1.8569):
        """max_y: lowest point of the artwork (blade-tip y, inches from the top of the material).
        Size values follow two Design Space recordings (max_y 1.857 and 10.838):
          job length L = max_y + 0.3611; load length = max(4.075, L - 0.4875);
          measured length = max(6.0, L + 1.4375); unload move y = L - 2.9."""
        import math, struct
        c, w = self.cmd, self.wait
        f = lambda v: struct.pack(">f", v).hex()
        L = max_y + 0.3611
        load_len = max(4.075, L - 0.4875)
        measure = max(6.0, L + 1.4375)
        self.log(f"job length {L:.3f} in; needs at least {measure:.2f} in of material")
        if self.d.cmd(H("60000000"))[2] & 0x01:
            self.log(">>> Material is already in the machine. Press Load/Unload to eject it; you will reload when Load flashes")
            w("material ejected", lambda s60, s73, ee: not (s60[2] & 0x01), 600, polls=("73", "60"))
            c("710f")
            time.sleep(1.0)
        self.log("== prepare")
        for _ in range(2):   # Design Space reads these after connecting and again before a job
            c("12000000"); c("a000d0"); c("a000d4")
        c("784600f0")
        c("1101" + F12_0 + f(load_len) + "0000000000000000")
        c("ed02")
        c("784620f0")
        self.log(">>> Load the material under the guides and press the flashing Load button")
        w("material loaded", lambda s60, s73, ee: s60[2] & 0x01, 300, polls=("73", "60"))
        time.sleep(2.2)

        self.log("== measure material")
        c("ee00000000")
        c("1101" + F4_0 + f(measure) + "0000000000000000")
        c("3089000004" + "%02x" % math.ceil(measure) + "00000000020000")
        c(PROFILE_MEASURE)
        c(ZERO3E); c(ZERO3E); c("3f00")
        c("ee" + f(measure), timeout=60)
        w("measurement finished", lambda s60, s73, ee: ee[1] == 0x00, 120, polls=("60", "ee"))
        time.sleep(0.6)

        self.log("== park carriage")
        c("21000000")
        c("43" + "0200008040" + "0000803e" + "0200008040" + "0000803e")
        c("22000000")
        w("carriage parked", self.idle, 60, polls=("60",))
        w("ready flag", lambda s60, s73, ee: s73[3] & 0x04, 20)
        c("710f"); c("710f")
        c("78000000"); c("784208f0")
        self.log(">>> Press the flashing Go button")
        w("Go pressed", lambda s60, s73, ee: s73[3] & 0x20, 300)
        c("710f")
        c("784204f0")
        c("68ff", timeout=10)
        c("784101f0")

        self.log("== tool detection")
        c("f008", timeout=60)
        c("ea")
        c("22000000")
        w("idle after tool detection", self.idle, 60, polls=("60",))

        self.log("== cut")
        c("1101" + F12_0 + f(L) + "0000000000000000")
        c("300800000c" + "%02x" % math.ceil(L) + "000000000013e4")
        c(PROFILE_CUT)
        c(ZERO3E); c(ZERO3E); c("3f00")
        c("21000000"); c("22000000")
        c("760000003e00000080", timeout=20)
        c("21000000")
        chunks = path if isinstance(path, list) else [path]
        # Path commands are sent back to back: the machine queues them and keeps cutting. Waiting for idle
        # between them stalls it (status 60 byte 1 = 0x28, queued but not moving) until the rest arrives.
        t_path = time.time()
        for n, chunk in enumerate(chunks, 1):
            # generous timeout: if the machine holds its reply while its queue is full, wait for it
            t0 = time.time()
            try:
                r = c(chunk, timeout=180, note=f"(path chunk {n}/{len(chunks)}, {(len(chunk) - 1) // 9} records, t+{time.time() - t_path:.1f}s)")
                ok = len(r) == 5 and r[0] == 0x00 and r[1] == 0x43
            except RuntimeError as e:
                self.log(f"  !! path command {n} refused: {e}"); ok = False
            if time.time() - t0 > 2:
                self.log(f"  (machine took {time.time() - t0:.1f}s to accept command {n})")
            if not ok:
                # stop sending and close the path with a pen-up at the last accepted point so the queue runs out
                self.log(f"  !! stopping after command {n - 1} of {len(chunks)}; closing the path so the machine can finish")
                last = chunks[n - 2][-8:] if n > 1 else None
                if last:
                    try: c(b"\x43\x02" + last, timeout=60, note="(pen-up terminator)")
                    except Exception as e: self.log(f"  !! terminator failed: {e}")
                break
        time.sleep(0.3)
        w("cut finished", self.idle, 1800)
        time.sleep(1.0)
        c("22000000")
        w("idle", self.idle, 60, polls=("60",))
        c("21000000")
        c(b"\x43" + (b"\x02" + struct.pack("<ff", 4.0, L - 2.9)) * 2)
        c("22000000")
        w("material moved to unload position", self.idle, 60, polls=("60",))
        c("784620f0"); c("710f")

        self.log(">>> Cut done. Press the Load/Unload button to eject the material")
        w("material unloaded", lambda s60, s73, ee: not (s60[2] & 0x01), 600, polls=("73", "60"))
        c("1101" + F12_0 + f(L) + "0000000000000000")
        c("ed02"); c("784200f0"); c("710f")
        c("12000000"); c("a000d0"); c("a000d4")
        c("784700f0")
        self.log("== finished")


if __name__ == "__main__":
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
    if "--circle" in sys.argv:      # --circle CX CY DIAMETER (inches)
        i = sys.argv.index("--circle")
        seg = int(sys.argv[sys.argv.index("--segments") + 1]) if "--segments" in sys.argv else 28
        recs = circle_records(*(float(v) for v in sys.argv[i + 1:i + 4]), seg)
        assert all(0.10 <= y <= 1.88 and 0.10 <= x <= 11.5 for _, x, y in recs), "circle outside the area the recorded settings cover"
        path = pack_records(recs)
    elif "--path-hex" in sys.argv:
        path = H(sys.argv[sys.argv.index("--path-hex") + 1])
    else:
        sys.exit(__doc__)
    _chunks = path if isinstance(path, list) else [path]
    print("path: " + ", ".join(f"{(len(ch) - 1) // 9} records" for ch in _chunks), flush=True)
    if "--print-path" in sys.argv:
        import struct
        for ch in _chunks:
            print("  -- chunk")
            for k in range((len(ch) - 1) // 9):
                op = ch[1 + 9 * k]; x, y = struct.unpack_from("<ff", ch, 2 + 9 * k)
                print(f"  op {op:02x} x={x:7.4f} y={y:7.4f}")
        sys.exit(0)
    dev = Cricut(port)
    info = dev.handshake()
    print("connected; status", info["status"], flush=True)
    try:
        Job(dev, log=lambda m: print(m, flush=True)).run(path)
    finally:
        dev.close()
