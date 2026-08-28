# AGENTS.md — toppingctl

Local control for Topping DACs over USB HID, without the vendor app.

## What this is

A single-file Python CLI (`toppingctl.py`) plus a hardware smoke test
(`smoke.py`). The protocol it speaks is documented in
[`gjcourt/lab`](https://github.com/gjcourt/lab/blob/main/01-audio-midi/_reference/topping-dx5ii-hid-protocol.md)
— **that spec is the source of truth; this repo is an implementation of it.**
If the two disagree, the spec was tested against hardware and this was not.

## ⚠️ The rules that exist because hardware is involved

- **`--dry-run` first, always.** It prints frames without sending them. Every
  command supports it.
- **The device can be read.** `readsettings.py` queries live state. (This line used to say the opposite; that predated the read work.) `show` reports
  what *this tool last wrote*, cached in `~/.toppingctl/state.json`. Anything
  changed from the front panel, remote, or vendor app is invisible and makes the
  cache stale.
- **Never implement scene save (`71 35`).** It would overwrite the C1/C2 presets
  stored on the device. Deliberately absent.
- **Volume above −10 dB requires `--force`.** A wrong level into headphones is
  the one irreversible mistake available here.
- **Only write registers confirmed in the spec.** Factory Reset lives in the same
  register space; blind sweeps are not safe. Read-only probing is.

## Adding device support

**Do not add a model to `DEVICES` you have not driven.** The vendor uses one web
app across its range, which makes compatibility *plausible*, not *established*.
An entry in that table is a claim, and the cost of it being wrong is someone
writing unknown registers to their DAC.

Sequence: `devices` for the PID → entry with `"status": "unverified"` →
`--dry-run` → a `vol` change watched on the front panel → `smoke.py` → only then
`"confirmed"`.

## Conventions

- **Apache-2.0**, © George Courtsunis.
- **`ruff check`** is the lint gate; CI runs it plus dry-run exercises of every
  frame builder, so CI needs no hardware.
- Branch and PR for every change. Never commit to `main`.
- Presets in `presets/`, third-party measurement data in `measurements/` —
  attribute the source in the file or its README.
