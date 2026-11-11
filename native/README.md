# Talking to the cutter without Design Space (experimental)

Python scripts that drive a Cricut Maker 3 directly over USB: no Wine and no Design Space once they
are set up. They do the machine's encrypted handshake, read its status, and cut an SVG.

This is separate from the rest of the repo. You don't need it to run Design Space under Wine. It is
here for people who want to script the machine or understand the protocol.

> **Made by AI**, like the rest of this repo: the protocol was worked out by an AI assistant, Claude
> (Anthropic), from serial captures and from Design Space's own device helper, on one user's PC. The
> user loaded the material, pressed the buttons and reported what came out.

## Status

Tested 2026-11-11 on one machine: Cricut Maker 3 (USB `20d3:0028`), firmware as shipped on that date,
with the helper from Design Space 10.11.128.

| What | Result |
| --- | --- |
| Handshake and status polls | Work. Run repeatedly |
| Cutting generated shapes and SVG files | Works: circles, a five-shape test file at two sizes, and a 1,810-point job (spiral, star, squares down to 0.2 in) |
| Material | **Only Smart Paper Sticker Cardstock, loaded without a mat, fine-point blade in clamp B.** The speed and pressure values are replayed from what Design Space sent for that material |
| Artwork size | Up to about 11 x 11 in. The material must be at least the artwork's bottom edge + 1.8 in long, 6 in minimum |
| Other machines, other firmware, mats, other tools, Print Then Cut, Bluetooth | Untested. The handshake key may differ per machine type and firmware |

It moves a machine with a blade in it, using values that were recorded, not understood. Use scrap
material and stay near the power switch.

## The handshake key is not included

The machine encrypts everything after connecting. The session key comes out of a key exchange that
needs one fixed 32-byte key, which Design Space's device helper builds at run time. That key is
Cricut's, so it is not in this repo. `tools/extract-handshake-key.sh` reads it out of **your own**
Design Space install and saves it on your PC.

## Setup

You need Design Space working under Wine first (the main README), Python 3 with the `cryptography`
package (`python3-cryptography` on Debian, Ubuntu and Fedora), and `winedbg`, which ships with Wine.

### 1. Record one connection

The extraction replays your machine's own replies, so record them once. `serial-tap.py` sits
between Wine's COM3 and the real port and logs every byte.

```bash
export WINEPREFIX="$HOME/Games/cricut"
python3 native/tools/serial-tap.py ~/cricut-capture &      # prints the pty it created
ln -sfn ~/cricut-capture/ttyCricut "$WINEPREFIX/dosdevices/com3"
```

Start Design Space, wait until it shows the machine as connected (or start a cut and let it
connect), then close Design Space and stop the tap (`kill %1`). Put COM3 back:

```bash
./scripts/cricut-registry.sh
```

The recording is `~/cricut-capture/tap-<date>.jsonl`. It contains your machine's serial number.

### 2. Getting the handshake key

```bash
native/tools/extract-handshake-key.sh ~/cricut-capture/tap-*.jsonl
```

Design Space opens by itself and connects to a fake machine that replays your recording. Don't
click anything; the real cutter isn't used. After a minute or two the script prints
`handshake key verified against your recording and saved to ~/.config/cricut/handshake.key`.

How it works: the helper wipes the key after use, so it isn't in the file or a memory dump. The
script finds the helper's decrypt call by a byte signature, lets the helper start its handshake with
the fake machine, attaches `winedbg` (attaching earlier makes the helper sit idle), sets a
breakpoint on that call and reads the key argument. It then checks that the key decrypts your
recording before saving it. If the signature isn't found, your Design Space build differs from
10.11.128 and the script stops without changing anything.

### 3. Try it

Close Design Space first: only one program can use the port.

```bash
python3 native/cricut_native.py
```

It should print `handshake ok` and a few status lines. Nothing moves.

## Cutting

```bash
# check placement first: writes a preview, sends nothing
python3 native/cricut_svg.py art.svg --x 0.3 --y 0.25 --dry-run --preview preview.svg

python3 native/cricut_svg.py art.svg --x 0.3 --y 0.25
```

`--x`/`--y` place the top-left corner of the artwork, in inches from the top-left of the material as
it feeds in. `--scale` resizes. The script then walks through the same steps Design Space does: press
**Load** when it flashes, **Go** when it flashes, and **Load/Unload** at the end.

SVG support: paths (lines, curves, arcs), rectangles, circles, ellipses, polygons, groups and
transforms. Text must be converted to paths first. The blade is compensated the way Design Space does
it (0.4 mm tip offset, a swivel at sharp corners) plus a short lead-in on closed shapes.

`python3 native/cricut_cut.py --circle 2 1 1.5` cuts a test circle.

## Files

| File | What it does |
| --- | --- |
| `cricut_native.py` | Serial framing, handshake, encrypted commands, status polls |
| `cricut_cut.py` | The job flow (load, measure, Go, tool detection, cut, unload) and a circle generator |
| `cricut_svg.py`, `svgflat.py` | SVG to blade-compensated path, then cut |
| `tools/serial-tap.py` | Logging proxy between Wine's COM3 and the cutter |
| `tools/extract-handshake-key.sh`, `tools/fake-device.py` | Read the handshake key from your Design Space install |
| `tools/sessionkey.py`, `tools/timeline.py` | Decrypt a recording and print its commands, for working out other materials |
| `PROTOCOL.md` | What is known about the protocol |

## Other materials

Record a Design Space cut of the material with `serial-tap.py`, then:

```bash
python3 native/tools/timeline.py capture.jsonl "$(python3 native/tools/sessionkey.py capture.jsonl | tail -1)"
```

That prints every command of the job. The `31` (speed and pressure) and `30` commands are the ones
that differ per material; `PROTOCOL.md` describes the rest.

## Known hazards

- Design Space's handshake includes `3d 00 24 24 24 00`, a query to the machine's Bluetooth module.
  Twice the machine never answered it and then ignored everything until it was power-cycled. These
  scripts leave that command out.
- A path must be sent as back-to-back `43` commands. Waiting for the machine to go idle between
  them stalls it with the blade down until the rest of the path arrives.
- If a script dies mid-job, the machine keeps what it was sent. Eject with **Load/Unload**, or
  switch it off.
