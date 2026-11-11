# Cricut Maker 3 USB protocol: what is known

Worked out 2026-11-11 on one Maker 3 (USB `20d3:0028`) with the device helper from Design Space
10.11.128, from serial captures and from the helper itself. Confidence is marked where it matters.
Command names come from the C++ class names (RTTI) in `CricutDevice.exe`; `Zorro` and `Athena` are
the prefixes it uses for machine families.

## Transport

USB CDC-ACM, `/dev/ttyACM*`. The baud rate is ignored. The port must be in raw mode. The machine is
silent until written to.

## Framing

```
u16 big-endian header | payload
header bit 15 = payload is encrypted, bits 0-14 = payload length
```

A request payload is an opcode byte plus arguments. A reply either echoes the opcode followed by data,
or is a 5-byte status `00 <opcode> <flags> <flags> ff`.

Malformed input gets `00 08 01 ff 05 09 ff 9X 00 00` after a 50 ms timeout (`93`: no usable header,
`92`: payload shorter than the header says).

## Handshake

1. `cf` + 3 random bytes (ResetAES). The machine returns 16 bytes. They do not affect the key.
2. Design Space then reads, in plaintext: `12 00 00 00` (CheckFirmware), `77` (GetCommandCount), `03`,
   `60 00 00 00` (status), parameters `a0 00 51`..`54` (serial number), `b9`, `a0 00 a3`/`9b`/`11`/`89`,
   `a1 01 14 ff ff ff ff`, `3d 00 24 24 24 00`, `0a`, `a4`, `60 00 00 00`, `a0 00 65`/`66`.
3. `c7` (NewAES) + one byte in 2..13 + 47 random bytes. Nothing the host sends affects the key.
4. The machine returns 48 bytes `R`. Everything after this is encrypted.
5. `P = AES-256-ECB-decrypt(HANDSHAKE_KEY, R)`. `P[0]` is an offset (1 and 2 were seen). The session
   key is `P[P[0] : P[0]+32]`; the rest is padding.

`HANDSHAKE_KEY` is fixed for a machine type and firmware range. The helper builds it at run time from
an embedded table keyed by machine type and firmware version, and wipes it after use. It is not
published here; `tools/extract-handshake-key.sh` reads it from your own install.

The machine picks a new session key for every `c7`, so a recorded session cannot be replayed. No
Cricut server takes part in the handshake for this machine. The helper also has
`riOPENDEVICEEncryption…WebRequest` states that fetch keys over the web; they were not used here.

`3d 00 24 24 24 00` is BluetoothUART: `24 24 24` is `$$$`, the command-mode escape of the machine's
Bluetooth module. Twice the machine never answered it and then ignored everything until power-cycled.
The handshake works without it.

Serial number: parameters `51`..`54` each return three ASCII characters, byte-reversed; joined they
are the 12-character number on the label. Each `a0` reply carries its 4-byte value twice.

## Encrypted frames

AES-256-ECB with the session key, both directions.

- Host to machine: `u16 BE length | payload | zero padding | 1 byte: sum of all preceding bytes`.
- Machine to host: `u16 LE length | payload | random padding`.

## What needs encryption

Tested with no handshake: `60`, `73`, `78` and `71` are accepted in plaintext. `21` and `22` are refused
with `01 ff 05 09 ff 48 04 00`. Reading state and setting the LEDs needs no key; drawing does.

## Commands

| Opcode | Name | Notes |
| --- | --- | --- |
| `0a` | ClearError | reply `0a 01 00` |
| `11` | (sizes) | `11 01 <f32 BE> <f32 BE> 00…`: two lengths in inches, see "Size values" |
| `12` | CheckFirmware | |
| `21`, `22` | BeginDraw, EndDraw | bracket path commands |
| `30` | SetMachineState | carries the two `11` lengths rounded up to whole inches |
| `31` | SetSpeedState | 5 bytes then a table of u16 BE values; differs between measuring and cutting |
| `3d` | BluetoothUART | see above |
| `3e` | SetFullToolState | sent twice with zeros |
| `3f` | CPRToggle | `3f 00` |
| `43` | MultiDrawVector | path data, see below |
| `60` | GetMachineStatus | `60 <state> <flags> …`; state `68` idle, `0a`/`2a` moving, `28` queued but waiting; flags bit 0 = material loaded |
| `68` | AbortCutting | Design Space sends `68 ff` before tool detection |
| `71` | ClearButtonState | `71 0f` |
| `73` | GetInterfaceState | byte 3: `04` ready/unload event, `20` Go pressed, `40` Load pressed; byte 4: `03` tool up, `02` tool down |
| `76` | OrientBlade | `76 00 00 00 3e 00 00 00 80` before the cut; arguments not decoded |
| `77` | GetCommandCount | |
| `78` | SetLEDStates | `78 xx xx f0` |
| `84` | LoadMat | seen once, at the end of a session |
| `a0`, `a1` | Get/SetPersistentMemory | `a0 00 <id>` |
| `a4` | GetPersistentValueCount | |
| `b9` | ReadPositionFloat | |
| `c7` | NewAES | key exchange |
| `cf` | ResetAES | start of the handshake |
| `ea` | ToolDetect | reply `ea 02 00 00 00 00` with the fine-point blade |
| `ed` | (load mode) | `ed 02` before loading without a mat |
| `ee` | MeasureMaterial | `ee <f32 BE length>` starts it; `ee 00 00 00 00` polls: `ee <busy> <f32 BE position>` |
| `f0` | HomeTheta | `f0 08`, about 8 s, before the cut |

## Path data (`43`)

`43` then 9-byte records: `op | f32 LE x | f32 LE y`, inches. x runs left to right, y from the leading
edge of the material. `op 02` moves with the tool up, `op 00` with it down.

The coordinates are the carriage, not the blade tip. Design Space's own paths run the carriage 0.4 mm
(0.01575 in) ahead of the tip along each segment, swing it around each sharp corner on an arc of that
radius in 15 degree steps while the tip stays on the vertex, and run past the start of a closed shape
by the same 0.4 mm. A shape starts with a travel record and five pen-down records on the same point.

Long paths go out as several `43` commands back to back; 35 records per command (316 bytes) is what
Design Space used and what was tested. The machine queues them: 52 commands (1,810 records) were all
acknowledged within 0.3 s and cut completely. Do not wait for idle between commands: a command that
does not end with a pen-up record leaves the next one queued and not moving (status `28`, tool down)
until the rest of the path arrives.

## Job flow (matless Smart Material)

Each wait polls `60` and `73` (and `ee 00 00 00 00` while measuring).

1. `78…`, `11 01 12.0 <load length>`, `ed 02`, flash Load. Wait for material loaded.
2. `11 01 4.0 <measured length>`, `30 89 …`, `31` (measure table), `3e` x2, `3f 00`, `ee <measured length>`.
   Poll until the `ee` reply's busy byte is 0.
3. `21`, `43` travel to (4.0, 0.25), `22`. Wait for idle, then for `73` byte 3 bit `04`.
4. `71 0f` x2, LEDs flash Go. Wait for `73` byte 3 bit `20`.
5. `71 0f`, LEDs, `68 ff`, `f0 08`, `ea`, `22`. Wait for idle.
6. `11 01 12.0 <job length>`, `30 08 …`, `31` (cut table), `3e` x2, `3f 00`, `21`, `22`, `76 …`, `21`,
   the `43` path commands. Wait for idle. `22`.
7. `21`, `43` travel to (4.0, job length - 2.9), `22`. LEDs. Wait for material unloaded.
8. `11 01 12.0 <job length>`, `ed 02`, LEDs, `71 0f`.

## Size values

With H = the lowest blade-tip y of the artwork, in inches. These reproduce two Design Space jobs
exactly (H = 1.857 and H = 10.838):

| Value | Formula |
| --- | --- |
| job length L | H + 0.3611 |
| load length | max(4.075, L - 0.4875) |
| measured length M | max(6.0, L + 1.4375) |
| unload move | y = L - 2.9 |

On the tall job Design Space measured twice, first to the material length set in the app and then
to M. Measuring once, to M, works.

## Not known

- What the values in `31`, `30` and `76` mean. They are replayed as recorded for one material.
- Mats, other tools, Print Then Cut, other machines and firmware versions.
- Limits of the path queue, and the countdown seen in the last byte of the `73` reply during a long cut.
- How a session should be closed. Stopping after the last command has caused no problem.
