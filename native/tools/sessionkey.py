#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print the session key(s) of a tap capture by decrypting each key-exchange reply with the handshake key.
Usage: sessionkey.py capture.jsonl"""
import json, os, sys
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cricut_native import handshake_key
HK = handshake_key()
want = False
for line in open(sys.argv[1]):
    r = json.loads(line)
    h = r.get("hex", "")
    if r["d"] == "H>D" and h.startswith("0031c7"): want = True
    elif r["d"] == "D>H" and want and h.startswith("0030"):
        d = Cipher(algorithms.AES(HK), modes.ECB()).decryptor()   # device protocol
        p = d.update(bytes.fromhex(h[4:])) + d.finalize()
        print(p[p[0]:p[0] + 32].hex()); want = False
