#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Condensed timeline of a tap capture: every non-poll command with its reply, and status polls
(60, 73, ee) only when the reply changes. Usage: timeline.py capture.jsonl KEYHEX"""
import json, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

key = bytes.fromhex(sys.argv[2])
dec = lambda b: (lambda d: d.update(b) + d.finalize())(Cipher(algorithms.AES(key), modes.ECB()).decryptor())
bufs = {"H>D": b"", "D>H": b""}; fr = []
for line in open(sys.argv[1]):
    r = json.loads(line); d = r["d"]
    if d not in bufs: continue
    bufs[d] += bytes.fromhex(r["hex"]); b = bufs[d]
    while len(b) >= 2:
        h = int.from_bytes(b[:2], "big"); n = h & 0x7FFF
        if len(b) < 2 + n: break
        p = b[2:2 + n]; b = b[2 + n:]
        if h & 0x8000:
            q = dec(p)
            ln = int.from_bytes(q[:2], "big" if d == "H>D" else "little")
            p = q[2:2 + ln]
        elif not any(f[2] for f in fr) and False:
            pass
        fr.append((r["t"], d, bool(h & 0x8000), p))
    bufs[d] = b
start = next(i for i, f in enumerate(fr) if f[2])
last = {}
i = start
while i < len(fr):
    t, d, e, p = fr[i]
    if d == "H>D":
        rep = fr[i + 1][3] if i + 1 < len(fr) and fr[i + 1][1] == "D>H" else None
        rt = fr[i + 1][0] if rep is not None else t
        op = p[0]
        if op in (0x60, 0x73) or (op == 0xEE and p == bytes.fromhex("ee00000000")):
            if last.get(p) != rep:
                print(f"{t:8.2f}  poll {p.hex():12s} -> {rep.hex() if rep else None}")
                last[p] = rep
        else:
            dt = f" (+{rt - t:.2f}s)" if rt - t > 0.05 else ""
            print(f"{t:8.2f}  CMD  {p.hex()[:120]}{'...' if len(p) > 60 else ''}  [{len(p)}] -> {rep.hex() if rep else None}{dt}")
        i += 2 if rep is not None else 1
    else:
        print(f"{t:8.2f}  UNSOLICITED {p.hex()}"); i += 1
