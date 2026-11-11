#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Linux session with a Cricut Maker 3: handshake, then read-only status polls.
Protocol notes are in PROTOCOL.md.
No Design Space, no Wine. Does NOT move the machine: it only sends the queries Design Space sends
when it connects, plus the two status polls (60, 73).

Usage: cricut_native.py [--polls N]
"""
import glob, os, select, sys, termios, time, tty
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_FILE = os.environ.get("CRICUT_HANDSHAKE_KEY") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "cricut", "handshake.key")


def handshake_key():
    """The 32-byte key that unwraps the machine's key-exchange reply. It is not distributed here:
    extract it from your own Design Space install with tools/extract-handshake-key.sh."""
    try:
        return bytes.fromhex(open(KEY_FILE).read().strip())
    except OSError:
        sys.exit(f"No handshake key at {KEY_FILE}. See native/README.md, 'Getting the handshake key'.")


def _ecb(key, data, decrypt):
    c = Cipher(algorithms.AES(key), modes.ECB())  # the device protocol is AES-256-ECB
    op = c.decryptor() if decrypt else c.encryptor()
    return op.update(data) + op.finalize()


class Cricut:
    def __init__(self, path=None):
        path = path or os.path.realpath(glob.glob("/dev/serial/by-id/usb-Cricut_Inc_*-if00")[0])
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        a = termios.tcgetattr(self.fd)
        a[2] = (a[2] & ~termios.HUPCL) | termios.CLOCAL | termios.CREAD
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.key = None

    def _read_frame(self, timeout):
        buf = b""; end = time.time() + timeout
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                buf += os.read(self.fd, 4096)
                if len(buf) >= 2:
                    hdr = int.from_bytes(buf[:2], "big")
                    if len(buf) >= 2 + (hdr & 0x7FFF):
                        return buf[2:2 + (hdr & 0x7FFF)], bool(hdr & 0x8000)
        raise TimeoutError("no reply from machine")

    def cmd(self, payload, timeout=3.0):
        """Send one command, return the reply payload (decrypted when a session key is set)."""
        if self.key is None:
            os.write(self.fd, len(payload).to_bytes(2, "big") + payload)
            rep, enc = self._read_frame(timeout)
            return rep
        n = len(payload)
        pt = n.to_bytes(2, "big") + payload
        pt += bytes(-(len(pt) + 1) % 16)             # zero pad, leaving room for the checksum byte
        pt += bytes([sum(pt) & 0xFF])
        ct = _ecb(self.key, pt, False)
        os.write(self.fd, (len(ct) | 0x8000).to_bytes(2, "big") + ct)
        rep, enc = self._read_frame(timeout)
        if not enc:
            raise RuntimeError("machine answered in plaintext: " + rep.hex())
        p = _ecb(self.key, rep, True)
        ln = int.from_bytes(p[:2], "little")
        if ln > len(p) - 2:
            raise RuntimeError("reply did not decrypt (wrong session key?)")
        return p[2:2 + ln]

    def read_param(self, pid):
        r = self.cmd(bytes([0xA0, 0x00, pid]))
        return r[3:7]

    def serial(self):
        return "".join(self.read_param(p)[1:4][::-1].decode("ascii", "replace") for p in (0x51, 0x52, 0x53, 0x54))

    def handshake(self):
        nonce = self.cmd(b"\xcf" + os.urandom(3))
        info = {"nonce": nonce.hex()}
        # same plaintext queries, in the same order, that Design Space sends before the key exchange
        info["12"] = self.cmd(bytes.fromhex("12000000")).hex()
        info["77"] = self.cmd(b"\x77").hex()
        info["03"] = self.cmd(b"\x03").hex()
        info["status"] = self.cmd(bytes.fromhex("60000000")).hex()
        info["serial"] = self.serial()
        info["b9"] = self.cmd(b"\xb9").hex()
        for p in (0xA3, 0x9B, 0x11, 0x89):
            self.read_param(p)
        self.cmd(bytes.fromhex("a10114ffffffff"))
        # Design Space also sends 3d 00 24 24 24 00 here. 0x24 0x24 0x24 is "$$$", the command-mode escape
        # of the machine's Bluetooth module (BM78 / RN4678). Twice the machine stopped answering at exactly
        # this command and needed a power-cycle, so the native handshake leaves it out: it is not needed
        # for the key exchange over USB.
        self.cmd(b"\x0a"); self.cmd(b"\xa4")
        self.cmd(bytes.fromhex("60000000"))
        self.read_param(0x65); self.read_param(0x66)
        reply = self.cmd(b"\xc7" + bytes([2 + os.urandom(1)[0] % 12]) + os.urandom(47))
        if len(reply) != 48:
            raise RuntimeError("unexpected key-exchange reply: " + reply.hex())
        p = _ecb(handshake_key(), reply, True)
        off = p[0]
        if not 1 <= off <= 15:
            raise RuntimeError("key-exchange reply did not decrypt sensibly (different firmware key?)")
        self.key = p[off:off + 32]
        return info

    def status(self):
        return self.cmd(bytes.fromhex("60000000")), self.cmd(b"\x73")

    def close(self):
        os.close(self.fd)


if __name__ == "__main__":
    polls = int(sys.argv[sys.argv.index("--polls") + 1]) if "--polls" in sys.argv else 5
    c = Cricut()
    info = c.handshake()
    print("handshake ok; serial", info["serial"], "status", info["status"])
    for i in range(polls):
        s60, s73 = c.status()
        print(f"poll {i}: 60 -> {s60.hex()}   73 -> {s73.hex()}")
        time.sleep(0.2)
    c.close()
