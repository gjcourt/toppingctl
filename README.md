<!-- readme-type: tool -->
# toppingctl

Local control for Topping DACs over USB HID, without the vendor app or a cloud account

toppingaudio.com's web app is the only tool Topping documents for driving its DACs,
and it is unreachable from some US ISPs. toppingctl speaks the same USB HID protocol
directly, so volume, PEQ, gain and power work without the vendor app, an account, or
a network connection at all. The register map was reverse-engineered clean-room by
watching the vendor app over WebHID, then re-derived from the vendor's own web
bundle for accuracy — see [`vendor_commands.py`](vendor_commands.py) for the
provenance note and [AGENTS.md](AGENTS.md) for the protocol spec this repo
implements.

**Status:** hardware-confirmed on a Topping DX5 II (firmware 2.39, verified
2026-08-07); the D90 III Discrete and DX1 II entries are unverified and need
`--unverified` for writes. Installed on every `dietpi-audio` node in the homelab
(`gjcourt/homelab`), inert until invoked. Under active development (last commit
2026-09-29).

## Quick start

Needs: Python 3.11+, the `hid` package, and the hidapi shared library.

```bash
git clone https://github.com/gjcourt/toppingctl && cd toppingctl
pip3 install hid
./toppingctl.py --dry-run vol -30
```

## Usage

Set volume, in dB (drop `--dry-run` once you've checked the frame it prints):

```bash
./toppingctl.py --dry-run vol -30
```

Write a PEQ preset (AutoEQ `ParametricEQ.txt` or a preset JSON file):

```bash
./toppingctl.py --dry-run apply presets/e3-oratory-harman-oe.txt
```

`--dry-run` prints the frames it would send instead of sending them; run it before
any write — see [AGENTS.md](AGENTS.md) for why. Full command and flag reference:
`./toppingctl.py --help`. Per-model quirks (DX1 II's separate volume/mute block,
D90 III's UAC2-only volume) and the device-verification workflow are in
[AGENTS.md](AGENTS.md).

## Configuration

| Name | Default | Meaning |
|---|---|---|
| `--device` | `dx5ii` | Target model: `dx5ii`, `d90iii`, or `dx1ii`. |
| `--dry-run` | off | Print frames instead of sending them. |
| `--unverified` | off | Allow writes to a device whose register map isn't confirmed. |
| `--force` | off | `vol` option: allow levels above −10 dB. |
| `--vol-step` | read from device | Override the dB-per-step, for models that don't support a settings read. |

## How it works

toppingctl builds and sends fixed-size HID report frames over the same interface
the vendor's WebHID app uses, addressing settings by register and, for PEQ, by
band index. Device compatibility is asserted per model, not assumed from a shared
USB PID — see `DEVICES` in [`toppingctl.py`](toppingctl.py) and the protocol spec
linked from [AGENTS.md](AGENTS.md) for the full register map. A handful of
standalone scripts cover the rest of the protocol: `readsettings.py` queries live
device state instead of the tool's local write cache, `listen.py` sends a raw read
for any register, `meters.py` prints the unsolicited VU/FFT pushes, `scenes.py`
recalls a stored C1/C2 scene, `setctl.py` sets one named field with a read-verify
round trip, and `probe.py` sends one raw register write for mapping an unknown
enum. Each takes `--help`.

## Development

```bash
ruff check .
ruff format --check .
python3 -m unittest -v test_offline
./toppingctl.py --dry-run vol -30
./smoke.py --dry-run
```

CI (`.github/workflows/ci.yml`) runs the same checks on Python 3.11 and 3.14, plus
dry-run exercises of the DX5 II (via `smoke.py`) and DX1 II frame builders — none
of it touches hardware. Conventions for contributors and agents: [AGENTS.md](AGENTS.md).

## License

[Apache-2.0](LICENSE)
