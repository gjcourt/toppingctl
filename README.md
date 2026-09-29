# toppingctl

Local control for **Topping DACs** over USB HID. No vendor app, no cloud
account, no dependency on `toppingaudio.com` — which is unreachable from some US
ISPs, which is why this exists.

The protocol was originally reverse-engineered clean-room, by watching the
vendor web app drive the device over WebHID. **That is no longer the whole
story.** The command names, settings layout and enum tables in
`vendor_commands.py` are read directly out of Topping's own web bundle, so this
project is now derived from the vendor application rather than from observation
alone — see the provenance note at the top of that file.

The change was worth making: observation had produced four confident and wrong
conclusions (an eleventh PEQ band that does nothing, two mislabelled crossfeed
registers, and an output enum wrong in both count and order). Reading the source
replaced guesses with the vendor's own values. Full spec:
[`gjcourt/lab` → `01-audio-midi/_reference/topping-dx5ii-hid-protocol.md`](https://github.com/gjcourt/lab/blob/main/01-audio-midi/_reference/topping-dx5ii-hid-protocol.md)

## Which devices

| Model | PID | Status |
|---|---|---|
| **Topping DX5 II** | `0x8750` | ✅ **confirmed** — driven on real hardware |
| **Topping DX1 II** | `0x8750` | community-contributed — unverified |

The DX1 II speaks a **different protocol family** ("dx1 next", reverse-engineered
from the vendor web app's bundle the same way as the DX5 II map, then driven on
one contributor's unit). It is *not* a copy of the DX5 II map on the same registers:

- **Framing**: writes need the report-id-0 prefix (like the D90 III); unprefixed
  frames are silently dropped.
- **Volume and mute live in a 12-frame state block at `0x810a`**, not at
  `0x7102`/`0x7103` — writing the DX5 II registers there is silently ignored.
  Frame 5 is the knob's "all outputs" volume, frames 3/4 the hp/lo memories,
  frame 7 the mute bitmask. Volume raw is `(dB + 99) × 10`, 0–990, 1 dB steps
  below −10 dB and 0.5 dB above.
- **Other settings are separate registers**: gain `0x7500`, PCM filter `0x7300`,
  input `0x7b00`, standby `0x7100` (1=working, 2=standby), brightness `0x7a00`,
  auto-standby `0x7900`, display mode `0x8109`.
- **PEQ is the same register map as the DX5 II** (`0x91`–`0x9b`, preamp `0x9c`,
  same sub-indices) and all 11 registers store values here — but the registers
  address **the active slot of 3 stored configs**, selected with `0x110e`
  (data = slot index 0..2, `0xffffffff` = EQ off). `apply`/`flat` therefore
  replace the curve stored in the active slot, not a global PEQ. Report-only
  registers `0x1204`/`0x1206` are read-only; writing them does nothing.
- There is **no `0x710c` GetSettings**, and reads are *restricted*: a readNack
  is only a read for the registers the vendor's own query builder allows
  (`0x7100`, `0x7900`, `0x7d00`, `0x810b`, `0x810c`, `0x810a`, `0x810d`,
  `0x810e`, `0x810f`, `0x8200`, `0x8300`, `0x8400`, `0x1204`, `0x1206`) plus
  the 3-slot PEQ dump `0x1106`. For anything else — gain, filter,
  brightness, input, display mode among them —
  **the device treats an incoming readNack as a write of the data field**:
  probing with data=0 resets the setting while "reading" it. Those registers
  are write-only from the host; their state arrives as an unsolicited push
  after a change. Found on hardware the expensive way: a register
  sweep meant to *read* gain pulled the user's front-panel high-gain back down
  to low. `./readsettings.py --device dx1ii` stays inside the safe set.
  Verification channel for the write-only registers: the device pushes the
  new value after accepting a write, and the front panel confirms it.

New commands (DX1 II): `mute off|analog|opt|both`, `input usb|opt`, `filter f1..f8`,
`eq on|off|1-3`, and `vol --target all|hp|lo`.

⚠️ **If writes seem to do nothing, close the vendor web app first.** While
home.toppingaudio.com is connected it re-asserts its own state over the same
HID interface and reverts your writes within about a second — the device is
fine, you are just fighting another controller for it. Observed repeatedly on
hardware: mute flipped back off, balance writes undone, volume stomped.

Two further hardware notes from the verification session: the device acks
writes with an echo frame, but the echo is a receipt, not proof of application —
read-back is the only truth. And if the screen is off the state word reads 11
instead of 1 and the panel needs a knob press (or a `power on`) before it
behaves again.

⚠️ **Only the DX5 II has been proven.** Other Topping models are *likely*
compatible — the vendor drives its whole range from one web app, which is
suggestive but not evidence. **No other model is listed until someone runs one.**

### Adding a device

USB VID `0x152A` belongs to **Thesycon**, whose XMOS USB-audio stack many DAC
vendors ship. **The VID does not imply Topping**, so the PID is what identifies a
model.

```bash
./toppingctl.py devices     # lists every attached Thesycon-VID HID device
```

Anything reported `UNKNOWN` is *not* assumed compatible. Adding an entry to
`DEVICES` is a **claim that the register map matches**, and only testing
establishes that. The order that matters:

1. `devices` to get the PID **and the USB product string**. The PID identifies
   nothing — many models ship `0x8750`, and three are directly confirmed here
   (DX5 II, D90 III Discrete, D30 Pro) with others reported — so the product
   string is what the entry matches on.
2. Add the entry with `"status": "unverified"` and `"bands": None`.
3. `--dry-run` everything first and read the frames. Note `--dry-run` is a
   **global** flag: it goes *before* the subcommand.
4. `vol` at a **safe level**, with `--unverified`, and watch the front panel. If
   the display moves, the register map holds.
5. Only then `smoke.py`, and only then mark it confirmed and set `bands`.

**`status` is enforced, not a label.** Anything other than `confirmed` refuses
writes unless `--unverified` is passed. Reads and `--dry-run` always work —
that is how you get through steps 3 and 4. Before this, adding an entry silently
granted full write access to hardware nobody had tested, which is the opposite
of what this procedure says it does.

**Some Topping DACs on this VID/PID have no HID interface at all.** A **D30 Pro**
enumerates as `152a:8750` — the same PID as the DX5 II and the D90 III Discrete
— and exposes only:

```
if00  class=01 subclass=01 proto=20  snd-usb-audio   UAC2 AudioControl
if01  class=01 subclass=02 proto=20  snd-usb-audio   UAC2 AudioStreaming
if02  class=fe subclass=01 proto=01  (no driver)     DFU, for firmware updates
```

No class `03` interface, no `/dev/hidraw*`. **`devices` returning nothing for it
is correct, not a fault** — `find_devices()` iterates `hid.enumerate()`, and a
device with no HID interfaces never appears there at all. That is the point
worth knowing: an empty result on something that is visibly a Topping DAC reads
as a bug until you know the hardware has nothing to enumerate.

Note this is *not* an argument about PID-versus-product-string matching. Such a
device is filtered out before either matcher sees it. The argument for matching
on the product string rests on models that DO expose HID and whose register maps
differ — the DX5 II and the D90 III Discrete.

Volume on the D30 Pro is the UAC2 mixer only, driven through ALSA
(`amixer -c <n> sset 'D30 Pro' -16dB`). That is how it runs in production here,
not an inference from the absent interfaces.

**Vendor tooling is split by model, and the split is not intuitive.** As of
2026-08-28 the browser app at `home.toppingaudio.com` drives **DX1 II and
DX5 II**; the desktop *Topping Tune* (V1.16) drives **D50 III, D90 III
Discrete, Centaurus, D900, DX9 Discrete, E50 II, DX1 II**. Neither covers
both a DX5 II and a D90 III. The web app is not a newer replacement for the
desktop one — assuming so sends you to a tool that cannot see your DAC and
reports it as a network error.

This matters for reverse engineering: the DX5 II map here came from the web
app's **JavaScript** bundle. Models only the desktop app supports have no such
bundle — it is a Qt/C++ binary — so their protocol needs USB capture instead.

**`bands` is per-device and is not guessed.** `None` means the count was never
established, and PEQ commands refuse rather than falling back to the DX5 II's
10. The DX5 II's 10 was found by writing a filter to each band and listening —
it also caught an eleventh register that accepts writes and drives nothing.
The DX1 II's 10 was found the same way on a loopback (balanced output into an
audio interface's line-in): a PK 1000 Hz −12 dB filter measured −7.7 dB from
band 10 and +0.0 dB from band 11. Every register still stores values — each
took a distinct probe value that showed up in the device's own config dump —
but the eleventh accepts writes and drives nothing here too.

**A DAC that silently accepts a wrong register write is the failure mode to
fear**, which is why step 4 uses a control with visible feedback.

## Status: hardware-confirmed

Smoke-tested against a real DX5 II (firmware 2.39) on 2026-08-07. **Volume and
gain changes were observed on the device's own front-panel display**, including
a −45.5 dB step — which confirms both the register map and the half-dB encoding.

**PEQ is confirmed too.** A band written only by this tool (PK 1 kHz, -12 dB,
Q 2.0) was then displayed correctly by Topping's own web app, together with a
volume this tool had set. An independent client wrote it; the vendor software
read it back. That is as strong as verification gets short of a measurement rig.

**The DX1 II entry stays unverified**, so those writes need `--unverified`. One unit was checked with each register class matched to a channel that
cannot lie. Volume: front-panel-confirmed by a human, plus read-back through
the `0x810a` block. Mute: block read-back and the panel's mute icon. PEQ (band
registers, preamp, slot select, EQ on/off): byte-verified through the device's
own config dump — every one of the 11 band registers (L and R) took a distinct
probe value that showed up in the dump, presets applied and flattened, and the
original curve restored byte-for-byte from a backup afterwards. The
write-only settings registers (gain, filter, input, brightness, display mode)
cannot be read from the host at all — a readNack writes them — so gain, filter,
input switching, EQ slot and standby/wake were each panel-verified by a human
while the tool drove them. What is *not* claimed: any register outside the map
above. Band 11's silence was measured afterwards with a loopback (the
balanced output into an audio interface's line-in): inert, as on the DX5 II.

## Install

```bash
pip3 install hid
```

The `hid` package is a thin ctypes wrapper and needs the hidapi **shared
library** next to it. macOS: `brew install hidapi`. Windows: there is no
package for this — drop the official `hidapi.dll` (x64) from the
[libusb/hidapi releases](https://github.com/libusb/hidapi/releases) into your
interpreter's directory (or anywhere on `PATH`).

macOS may require granting your terminal **Input Monitoring**
(System Settings → Privacy & Security).

## Use

```bash
./toppingctl.py apply e3.txt          # write a PEQ preset (AutoEQ .txt or .json)
./toppingctl.py flat                  # disable all bands
./toppingctl.py vol -30               # set volume in dB
./toppingctl.py gain on               # headphone gain
./toppingctl.py power off             # sleep
./toppingctl.py show                  # last-written state
./toppingctl.py dump preset.json      # export state as JSON
```

DX1 II (`--device dx1ii`). The entry is unverified, so a live write needs
`--unverified`. `--dry-run` and `readsettings.py` do not.

```bash
./toppingctl.py --device dx1ii --unverified vol -30              # knob volume ("all outputs")
./toppingctl.py --device dx1ii --unverified vol -30 --target hp  # or the hp/lo memories
./toppingctl.py --device dx1ii --unverified mute off             # neither output
./toppingctl.py --device dx1ii --unverified mute analog          # hp/lo only
./toppingctl.py --device dx1ii --unverified mute opt             # optical only
./toppingctl.py --device dx1ii --unverified mute both
./toppingctl.py --device dx1ii --unverified gain on
./toppingctl.py --device dx1ii --unverified input usb            # or: opt
./toppingctl.py --device dx1ii --unverified filter f3            # PCM filter f1..f8
./toppingctl.py --device dx1ii --unverified eq off               # EQ on | off | 1-3 (select slot)
./toppingctl.py --device dx1ii --unverified power off            # standby (wake: power on)
./toppingctl.py --device dx1ii --unverified apply e3.txt         # PEQ -> the ACTIVE slot of 3
./readsettings.py --device dx1ii                                 # full live state, decoded
```

Note `--dry-run` is a **global** flag: it goes *before* the subcommand.

`--dry-run` prints the frames without sending them. Use it first.

## Presets

Accepts **AutoEQ / oratory1990 `ParametricEQ.txt`** directly:

```
Preamp: -6.7 dB
Filter 1: ON LS Fc 105 Hz Gain 5.5 dB Q 0.70
Filter 2: ON PK Fc 1200 Hz Gain -3.2 dB Q 1.41
```

Only **PK**, **LS** and **HS** are supported — the three filter types confirmed
on these devices. Any other type is **reported, not silently dropped**, because
a missing filter yields a wrong curve that still sounds plausible.

**On the DX5 II** the device has **10 usable bands**; presets with more are
rejected rather than truncated. Value limits are the DX5 II's: 10 Hz–22 kHz,
±40 dB, Q 0.01–100.

Eleven band registers exist (`0x91`–`0x9b`) but **`0x9b` does nothing**.

Topping documents this device as a 10-band PEQ, so 11 was always a hopeful
reading — but a defensible one. The vendor's own app writes all eleven
registers on commit, and hardware does sometimes carry capability the vendor
never advertises. An undocumented eleventh band would have been a real find,
and it was worth testing for.

The mistake was making it the default *before* testing it, so every preset
silently depended on an unverified guess. Confirmed inert on hardware
2026-08-24. A `PK 1 kHz -15 dB
Q 0.7` cut written to band 10 (`0x9a`) was plainly audible; the identical
filter written to band 11 (`0x9b`) was inaudible; re-applying band 10 brought
the cut back, so the silence was the register and not the test rig. `0x9b`
accepts writes and commits without error — it is simply not wired to a filter,
and the vendor UI's "BANDS n / 10" reports the hardware correctly.

All eleven registers are still written, so a stale band 11 left behind by the
vendor app is cleared rather than left underneath your preset.

**On the DX1 II** the same preset format applies, with different numbers: **11
band registers, all storing values** (verified in the device's config dump),
but **10 treated as usable** — band 11 has not been listening-tested and gets
the DX5 II's `0x9b` treatment (written and cleared, not counted). The
firmware's own clamps are tighter — 20 Hz–20 kHz, **±12 dB**, Q 0.1–20 — so
presets beyond those are rejected instead of silently clamped into a different
curve. The preset lands in the **active PEQ slot** of three, replacing the
curve stored there — select the slot first with `eq 1-3`.

## Two things to know

**The device can be read** — `./readsettings.py` queries it and prints the real
state, decoded. Send a `readNack` (protocol byte `0x10`) for `GetSettings`
(`0x710c`) and the device replies with its whole configuration as a numbered
array of 32-bit records.

This was previously believed impossible. Reads *do* return state; the confusion
was that the device streams all-zero input reports while idle, so anyone
listening without asking a question concludes nothing arrives.

`show` is still only a cache of what this tool last wrote. Prefer
`readsettings.py`, which asks the device.

> `./listen.py --read 0x7133` returns your USB serial number in plain ASCII.
> Don't paste that output anywhere public.

**Preamp is applied when the preset declares one**, and only then. AutoEQ
`.txt` files carry a `Preamp:` line and JSON presets a `preamp_db` field;
`apply` writes it to `0x9c` before the bands. For those presets, do **not** also
lower the volume by hand — you would be attenuated twice.

**A preset with no preamp line writes none**, and the device keeps whatever
preamp was set last, which this tool cannot read back. `presets/bass1.json` is
exactly this case: it boosts `+6.0 dB` and declares no preamp. `apply` now warns
when a preset boosts without one — set it yourself first with
`./toppingctl.py preamp <dB>` (range `-40..+10`).

Register `0x9c` is confirmed: linear gain in Q25 fixed point,
`dB = 20·log10(value / 2^25)`, derived from the vendor app's own `-3.0 dB` value
and verified by round trip at `-6.0 dB`.

## Safety

- Volume is clamped to −99..0 dB, and anything above −10 dB needs `--force`.
  A wrong volume into headphones is the one irreversible mistake here.
- Only registers confirmed in the spec are written. Scene save (`71 35`) is
  deliberately **not** implemented — it would overwrite the C1/C2 presets
  stored on the device.
- Power uses two frames replayed verbatim from capture, since that register
  needs a real checksum and does not answer the checksum oracle.
