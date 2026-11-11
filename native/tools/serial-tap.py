#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Logging man-in-the-middle for the Cricut serial port (no root needed).

Creates a pty and publishes it as <dir>/ttyCricut (point Wine's COM3 there: see native/README.md),
forwards bytes both ways to the real tty and appends every chunk to a JSONL log:
  {"t": seconds_since_start, "d": "H>D" | "D>H", "hex": "..."}
H>D = host (Design Space) to device, D>H = device to host.
"""
import glob, json, os, select, sys, termios, time, tty

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.abspath("cricut-capture")
LINK = os.path.join(ROOT, "ttyCricut")


def main():
    os.makedirs(ROOT, exist_ok=True)
    real = sys.argv[2] if len(sys.argv) > 2 else None
    if not real:
        m = glob.glob("/dev/serial/by-id/usb-Cricut_Inc_*-if00")
        if not m:
            sys.exit("no Cricut tty found")
        real = os.path.realpath(m[0])
    log_path = os.path.join(ROOT, time.strftime("tap-%Y%m%d-%H%M%S.jsonl"))

    def open_dev():
        fd = os.open(real, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[2] = (attrs[2] & ~termios.HUPCL) | termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd

    dev = open_dev()

    master, slave = os.openpty()  # keep slave open so master never sees EIO between opens
    tty.setraw(slave)
    if os.path.islink(LINK):
        os.unlink(LINK)
    os.symlink(os.ttyname(slave), LINK)

    print(f"tap: {LINK} -> {os.ttyname(slave)} <=> {real}; log {log_path}", flush=True)
    t0 = time.monotonic()
    with open(log_path, "a", buffering=1) as log:
        log.write(json.dumps({"t": 0, "d": "meta", "real": real, "wall": time.time()}) + "\n")
        while True:
            r, _, _ = select.select([master, dev], [], [])
            for fd in r:
                now = round(time.monotonic() - t0, 4)
                try:
                    data = os.read(fd, 65536)
                except OSError as e:
                    data = b""
                    log.write(json.dumps({"t": now, "d": "err", "where": "read " + ("pty" if fd == master else "dev"), "e": str(e)}) + "\n")
                    if fd == dev:  # real tty was hung up: reopen it
                        os.close(dev); time.sleep(0.2); dev = open_dev()
                    else:
                        time.sleep(0.05)
                    break
                if not data:
                    continue
                d = "H>D" if fd == master else "D>H"
                log.write(json.dumps({"t": now, "d": d, "hex": data.hex()}) + "\n")
                while data:
                    try:
                        n = os.write(dev if fd == master else master, data)
                        data = data[n:]
                    except OSError as e:
                        log.write(json.dumps({"t": now, "d": "err", "where": "write " + d, "e": str(e)}) + "\n")
                        if fd != master:
                            break
                        os.close(dev); time.sleep(0.2); dev = open_dev()
                break  # re-select: fds may have changed


if __name__ == "__main__":
    main()
