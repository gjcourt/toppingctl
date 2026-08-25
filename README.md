# toppingctl

Local control for **Topping DACs** over USB HID. No vendor app, no cloud
account, no dependency on `toppingaudio.com` — which is unreachable from some US
ISPs, which is why this exists.

Protocol was reverse-engineered clean-room by observing the vendor web app drive
the device over WebHID. Full spec:
[`gjcourt/lab` → `01-audio-midi/_reference/topping-dx5ii-hid-protocol.md`](https://github.com/gjcourt/lab/blob/main/01-audio-midi/_reference/topping-dx5ii-hid-protocol.md)

## Which devices

| Model | PID | Status |
|---|---|---|
| **Topping DX5 II** | `0x8750` | ✅ **confirmed** — driven on real hardware |

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

1. `devices` to get the PID.
2. Add the entry with `"status": "unverified"`.
3. `--dry-run` everything first and read the frames.
4. `vol` at a **safe level** and watch the front panel. If the display moves, the
   register map holds.
5. Only then `smoke.py`, and only then mark it confirmed.

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

## Install

```bash
brew install hidapi
pip3 install hid
```

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

`--dry-run` prints the frames without sending them. Use it first.

## Presets

Accepts **AutoEQ / oratory1990 `ParametricEQ.txt`** directly:

```
Preamp: -6.7 dB
Filter 1: ON LS Fc 105 Hz Gain 5.5 dB Q 0.70
Filter 2: ON PK Fc 1200 Hz Gain -3.2 dB Q 1.41
```

Only **PK**, **LS** and **HS** are supported — the three filter types confirmed
on this device. Any other type is **reported, not silently dropped**, because a
missing filter yields a wrong curve that still sounds plausible.

The device has **10 usable bands**; presets with more are rejected rather than
truncated.

Eleven band registers exist (`0x91`–`0x9b`) but **`0x9b` does nothing**, which
was an open question until it was tested on 2026-08-24. A `PK 1 kHz -15 dB
Q 0.7` cut written to band 10 (`0x9a`) was plainly audible; the identical
filter written to band 11 (`0x9b`) was inaudible; re-applying band 10 brought
the cut back, so the silence was the register and not the test rig. `0x9b`
accepts writes and commits without error — it is simply not wired to a filter,
and the vendor UI's "BANDS n / 10" reports the hardware correctly.

All eleven registers are still written, so a stale band 11 left behind by the
vendor app is cleared rather than left underneath your preset.

## Two things to know

**The device cannot be read.** Read requests return an echo, not state. So
`show` reports what *this tool* last wrote, cached in `~/.toppingctl/state.json`.
Changes made from the front panel, the remote, or the vendor app are invisible
here and will make the cache stale. This does not affect correctness of
`apply`, which always writes all 11 bands.

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
