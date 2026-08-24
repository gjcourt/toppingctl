#!/usr/bin/env python3
"""toppingctl — local control for Topping DACs, over USB HID.

No vendor app, no cloud account, no dependency on toppingaudio.com (which is
unreachable from some US ISPs, which is why this exists).

Protocol spec: gjcourt/lab 01-audio-midi/_reference/topping-dx5ii-hid-protocol.md

Reverse-engineered and hardware-confirmed on a DX5 II. Other Topping models very
likely share the register map -- the vendor drives them from one web app -- but
only the DX5 II has been proven. See DEVICES and README "Adding a device".

    toppingctl apply <autoeq.txt|preset.json>   write a PEQ preset
    toppingctl dump [file]                      write current known state to JSON
    toppingctl show                             print current known state
    toppingctl flat                             disable every band
    toppingctl preamp <dB>                      set PEQ preamp, e.g. -6.7
    toppingctl vol <dB>                         set volume, e.g. -30
    toppingctl gain on|off                      headphone gain
    toppingctl power on|off                     wake / sleep

Add --dry-run to any command to print frames without sending them.

IMPORTANT — THE DEVICE CANNOT BE READ.
Reads return an echo, not state (see spec §3). So "current state" here means
"what this tool last wrote", cached in ~/.toppingctl/state.json. Anything changed
via the front panel, the remote, or the vendor app is invisible to us and will
make the cache stale. `apply` always writes every band, so a preset is still
applied correctly regardless of cache accuracy.
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("missing dependency: pip3 install hid   (and: brew install hidapi)")

# --- device table -----------------------------------------------------------
#
# VID 0x152A is Thesycon's, whose XMOS USB-audio stack many DAC vendors ship.
# The VID therefore does NOT imply Topping; the PID is what identifies a model.
#
#   confirmed  -- driven against real hardware, front panel observed
#   unverified -- register map assumed identical, PID unknown until someone
#                 with the device runs `toppingctl devices`
#
DEVICES = {
    "dx5ii": {
        "name": "Topping DX5 II",
        "vid": 0x152A,
        "pid": 0x8750,
        "bands": 11,
        "status": "confirmed",
    },
    # D90 III Discrete: same vendor, same web app, so the protocol is very
    # likely identical -- but the PID has never been read off one, and band
    # count is unconfirmed. Do not guess it; see README "Adding a device".
}

THESYCON_VID = 0x152A

# --- protocol constants (see spec) ------------------------------------------

REG_CTRL = 0x71           # device control
SUB_POWER = 0x01
SUB_VOLUME = 0x02
SUB_GAIN = 0x17
SUB_COMMIT = 0x34

PEQ_FIRST, PEQ_LAST = 0x91, 0x9B      # 11 band registers
REG_PREAMP = 0x9C                     # preamp: subs 01/03 = value L/R, 02/04 = enable
PREAMP_SCALE = 1 << 25                # linear gain in Q25 fixed point
BAND_COUNT = PEQ_LAST - PEQ_FIRST + 1

# Per-band sub-indices. 01-05 left channel, 06-0a right.
SUB_TYPE, SUB_FREQ, SUB_GAIN_, SUB_Q, SUB_ON = 1, 2, 3, 4, 5
CHANNEL_OFFSET = 5

# AutoEQ emits LSC/HSC ("corrected" shelves) as well as LS/HS. They are the same
# standard biquad shelf at the Q values AutoEQ uses, so they map to the same
# device filter types.
FILTER_TYPES = {"PK": 1, "LS": 4, "HS": 5, "LSC": 4, "HSC": 5}
FILTER_NAMES = {1: "PK", 4: "LS", 5: "HS"}

# A band the device considers unused. Taken from vendor-app captures — 632 Hz is
# the factory default centre, not a meaningful setting.
DEFAULT_BAND = {"type": "PK", "freq": 632, "gain": 0.0, "q": 0.707, "on": False}

# Power is the one register requiring a real checksum, and it does NOT answer
# the checksum oracle. These two frames are replayed verbatim from capture;
# since the register is binary, that is sufficient.
POWER_FRAMES = {
    False: bytes([0x22, 0x33, 0x20, 0x01, 0x00, 0x71, 0x01,
                  0x00, 0x00, 0x00, 0x00, 0xDC, 0x65, 0x66, 0x77, 0x00]),
    True: bytes([0x22, 0x33, 0x20, 0x01, 0x00, 0x71, 0x01,
                 0x00, 0x00, 0x00, 0x01, 0x1C, 0xA4, 0x66, 0x77, 0x00]),
}

STATE_DIR = os.path.expanduser("~/.toppingctl")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Volume is attenuation in half-dB steps. Clamp hard: a wrong value here is
# instantly loud into headphones, and that is the one irreversible mistake this
# tool could make.
VOL_MIN_DB, VOL_MAX_DB = -99.0, 0.0
VOL_WARN_DB = -10.0


def db_to_q25(db):
    """Preamp is LINEAR gain in Q25 fixed point, not a dB integer.

    dB = 20*log10(value / 2^25); value = round(10^(dB/20) * 2^25)

    Decoded 2026-08-08 and confirmed by round trip: derived from Topping's own
    0x016A77C4 (= -3.0 dB), then used to PREDICT the value for -6.0 dB, write it,
    and read it back in the vendor app.

    NOTE ON THE DISPLAY: the app TRUNCATES toward zero rather than rounding, so a
    mathematically exact -6.0000000 dB shows as "-5.9". That is a display
    convention, not an encoding error - do not "correct" it with an epsilon.
    """
    return int(round((10 ** (db / 20.0)) * PREAMP_SCALE))


def q25_to_db(v):
    import math
    return 20 * math.log10(v / PREAMP_SCALE) if v > 0 else float("-inf")


# --- frame construction -----------------------------------------------------

def frame(reg, sub, value, opcode=0x20, b4=0x01):
    """22 33 <op> 01 <b4> <reg> <sub> <int32 BE> 00 00 66 77 00

    Bytes 11-12 are a checksum the device fills in on its own frames. Writes are
    accepted with 00 00 for every register except power (handled separately).
    """
    v = int(value) & 0xFFFFFFFF
    return bytes([0x22, 0x33, opcode, 0x01, b4, reg, sub,
                  (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF,
                  0x00, 0x00, 0x66, 0x77, 0x00])


def band_frames(index, band):
    """All 10 frames for one band: five parameters x two channels."""
    reg = PEQ_FIRST + index
    ftype = FILTER_TYPES[band["type"]]
    freq = int(round(band["freq"]))
    gain = int(round(band["gain"] * 10))        # tenths of a dB, signed
    q = int(round(band["q"] * 10000))           # x10^4
    on = 1 if band["on"] else 0
    out = []
    for ch in (0, CHANNEL_OFFSET):
        out += [
            frame(reg, SUB_TYPE + ch, ftype),
            frame(reg, SUB_FREQ + ch, freq),
            frame(reg, SUB_GAIN_ + ch, gain),
            frame(reg, SUB_Q + ch, q),
            frame(reg, SUB_ON + ch, on),
        ]
    return out


def preamp_frames(db):
    """Both channels, matching the vendor app's 9c 01..04 sequence."""
    v = db_to_q25(db)
    return [
        frame(REG_PREAMP, 0x01, v),
        frame(REG_PREAMP, 0x02, 1),
        frame(REG_PREAMP, 0x03, v),
        frame(REG_PREAMP, 0x04, 1),
    ]


# --- device -----------------------------------------------------------------

def find_devices():
    """Enumerate every Thesycon-VID HID device, flagging which we know."""
    found = []
    for d in hid.enumerate(THESYCON_VID, 0):
        key = next((k for k, v in DEVICES.items() if v["pid"] == d["product_id"]), None)
        found.append({
            "key": key,
            "pid": d["product_id"],
            "product": d.get("product_string") or "?",
            "known": key is not None,
        })
    return found


class Device:
    """A Topping DAC over USB HID.

    Was `DX5` and hardcoded to one PID. The protocol layer is model-independent;
    only the PID and band count differ, so both now come from DEVICES.
    """

    def __init__(self, dry_run=False, key=None):
        self.dry_run = dry_run
        self.h = None
        self.key = key or "dx5ii"
        if self.key not in DEVICES:
            sys.exit(f"unknown device {self.key!r}; known: {', '.join(DEVICES)}")
        self.spec = DEVICES[self.key]
        if not dry_run:
            try:
                self.h = hid.Device(self.spec["vid"], self.spec["pid"])
            except Exception as e:
                hint = ""
                others = [f for f in find_devices() if not f["known"]]
                if others:
                    hint = ("\n\nUnrecognised Thesycon-VID devices are attached:\n" +
                            "\n".join(f"  pid={f['pid']:#06x}  {f['product']}" for f in others) +
                            "\nIf one is yours, see README \"Adding a device\".")
                sys.exit(f"cannot open {self.spec['name']} "
                         f"({self.spec['vid']:#06x}/{self.spec['pid']:#06x}): {e}\n"
                         f"is it plugged in? if this is a permissions error, grant "
                         f"your terminal Input Monitoring in System Settings.{hint}")

    def send(self, f, label=""):
        if self.dry_run:
            print(f"  {f.hex(' ')}  {label}")
            return
        self.h.write(f)
        time.sleep(0.004)      # the vendor app paces writes; don't flood the DSP

    def commit(self):
        self.send(frame(REG_CTRL, SUB_COMMIT, 1), "commit")

    def close(self):
        if self.h:
            self.h.close()


# --- state cache ------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"bands": [dict(DEFAULT_BAND) for _ in range(BAND_COUNT)],
                "volume_db": None, "gain": None, "source": "defaults (no cache yet)"}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=2)


# --- preset parsing ---------------------------------------------------------

AUTOEQ_FILTER = re.compile(
    r"^Filter\s+\d+:\s+(ON|OFF)\s+(\w+)\s+Fc\s+([\d.]+)\s*Hz\s+"
    r"Gain\s+(-?[\d.]+)\s*dB\s+Q\s+([\d.]+)", re.I)
AUTOEQ_PREAMP = re.compile(r"^Preamp:\s*(-?[\d.]+)\s*dB", re.I)


def parse_autoeq(text):
    """Parse AutoEQ / oratory1990 'ParametricEQ.txt' format.

    Returns (bands, preamp_db, skipped). Only PK / LS / HS are supported —
    those are the three filter types confirmed on this device. Anything else is
    reported rather than silently dropped, because a silently missing filter
    produces a wrong curve that sounds plausible.
    """
    bands, preamp, skipped = [], None, []
    for line in text.splitlines():
        line = line.strip()
        m = AUTOEQ_PREAMP.match(line)
        if m:
            preamp = float(m.group(1))
            continue
        m = AUTOEQ_FILTER.match(line)
        if not m:
            continue
        on, ftype, fc, gain, q = m.groups()
        ftype = ftype.upper()
        if ftype not in FILTER_TYPES:
            skipped.append(f"{ftype} @ {fc} Hz")
            continue
        bands.append({"type": ftype, "freq": float(fc), "gain": float(gain),
                      "q": float(q), "on": on.upper() == "ON"})
    return bands, preamp, skipped


def load_preset(path):
    text = open(path).read()
    if path.lower().endswith(".json"):
        data = json.loads(text)
        return data.get("bands", data), data.get("preamp_db"), []
    return parse_autoeq(text)


def validate(bands):
    """Reject nonsense before it reaches the DSP."""
    errs = []
    if len(bands) > BAND_COUNT:
        errs.append(f"{len(bands)} filters but this device has {BAND_COUNT} bands")
    for i, b in enumerate(bands, 1):
        if b["type"] not in FILTER_TYPES:
            errs.append(f"filter {i}: unsupported type {b['type']}")
        if not 10 <= b["freq"] <= 22000:
            errs.append(f"filter {i}: frequency {b['freq']} Hz out of range")
        if not -40 <= b["gain"] <= 40:
            errs.append(f"filter {i}: gain {b['gain']} dB out of range")
        if not 0.01 <= b["q"] <= 100:
            errs.append(f"filter {i}: Q {b['q']} out of range")
    return errs


# --- commands ---------------------------------------------------------------

def cmd_apply(args):
    bands, preamp, skipped = load_preset(args.file)
    if not bands:
        sys.exit(f"no usable filters found in {args.file}")
    errs = validate(bands)
    if errs:
        sys.exit("preset rejected:\n  " + "\n  ".join(errs))

    padded = bands + [dict(DEFAULT_BAND) for _ in range(BAND_COUNT - len(bands))]

    print(f"applying {len(bands)} filter(s) from {os.path.basename(args.file)}")
    for i, b in enumerate(bands, 1):
        state = "" if b["on"] else "  (off)"
        print(f"  {i:2d}. {b['type']}  {b['freq']:>7.0f} Hz  "
              f"{b['gain']:+5.1f} dB  Q {b['q']:.3f}{state}")
    if len(padded) > len(bands):
        print(f"  {len(padded) - len(bands)} unused band(s) disabled")

    if skipped:
        print(f"\n  SKIPPED unsupported filter types: {', '.join(skipped)}")
        print("  only PK / LS / HS are confirmed on this device — the applied")
        print("  curve will differ from the preset.")

    if preamp is not None:
        print(f"\n  preamp {preamp:+.1f} dB (raw 0x{db_to_q25(preamp):08X})")
    else:
        boost = max([b["gain"] for b in bands if b["on"]] or [0.0])
        if boost > 0:
            print(f"\n  WARNING: this preset boosts up to {boost:+.1f} dB and declares no preamp.")
            print("  Nothing is written to 0x9c, so the device keeps whatever preamp was set")
            print("  last -- which this tool cannot read back. Positive gain with no preamp")
            print(f"  can clip. Set one first:  ./toppingctl.py preamp {-abs(boost):.1f}")

    dev = Device(args.dry_run, getattr(args, "device", None))
    if preamp is not None:
        for f in preamp_frames(preamp):
            dev.send(f, f"preamp {preamp:+.1f}")
    for i, b in enumerate(padded):
        for f in band_frames(i, b):
            dev.send(f, f"band{i+1} {b['type']}")
    dev.commit()
    dev.close()

    if not args.dry_run:
        st = load_state()
        st["bands"] = padded
        if preamp is not None:
            st["preamp_db"] = preamp
        st["source"] = os.path.abspath(args.file)
        save_state(st)
        print(f"\napplied. {BAND_COUNT} bands written, committed.")
    else:
        print("\ndry run — nothing sent.")


def cmd_flat(args):
    dev = Device(args.dry_run, getattr(args, "device", None))
    for i in range(BAND_COUNT):
        for f in band_frames(i, DEFAULT_BAND):
            dev.send(f, f"band{i+1} default")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["bands"] = [dict(DEFAULT_BAND) for _ in range(BAND_COUNT)]
        st["source"] = "flat"
        save_state(st)
        print(f"all {BAND_COUNT} bands disabled.")


def cmd_preamp(args):
    db = args.db
    if not -40.0 <= db <= 10.0:
        sys.exit(f"preamp {db} dB out of range (-40..+10)")
    v = db_to_q25(db)
    if db > 0:
        print(f"  warning: positive preamp ({db:+.1f} dB) can clip. AutoEQ presets"
              f" are always negative.")
    dev = Device(args.dry_run, getattr(args, "device", None))
    for f in preamp_frames(db):
        dev.send(f, f"preamp {db:+.1f} dB")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["preamp_db"] = db
        save_state(st)
        print(f"preamp {db:+.1f} dB  (raw {v} = 0x{v:08X})")


def cmd_vol(args):
    db = args.db
    if not VOL_MIN_DB <= db <= VOL_MAX_DB:
        sys.exit(f"volume {db} dB out of range ({VOL_MIN_DB}..{VOL_MAX_DB})")
    steps = int(round(-db * 2))          # attenuation, half-dB steps
    actual = -steps / 2
    if db > VOL_WARN_DB and not args.force:
        sys.exit(f"{actual:+.1f} dB is loud — re-run with --force if you mean it")
    dev = Device(args.dry_run, getattr(args, "device", None))
    dev.send(frame(REG_CTRL, SUB_VOLUME, steps), f"volume {actual:+.1f} dB")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["volume_db"] = actual
        save_state(st)
        print(f"volume {actual:+.1f} dB  (raw {steps})")


def cmd_gain(args):
    on = args.state == "on"
    dev = Device(args.dry_run, getattr(args, "device", None))
    dev.send(frame(REG_CTRL, SUB_GAIN, 1 if on else 0), f"gain {args.state}")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["gain"] = on
        save_state(st)
        print(f"gain {args.state}")
        print("note: the vendor app only exposes gain on headphone outputs.")


def cmd_power(args):
    on = args.state == "on"
    dev = Device(args.dry_run, getattr(args, "device", None))
    dev.send(POWER_FRAMES[on], f"power {args.state}")
    dev.close()
    if not args.dry_run:
        print(f"power {args.state}")


def cmd_show(args):
    st = load_state()
    print(f"last written by toppingctl (source: {st.get('source')})")
    print("the device cannot be queried — this is a cache, not a read.\n")
    if st.get("preamp_db") is not None:
        print(f"  preamp  {st['preamp_db']:+.1f} dB")
    if st.get("volume_db") is not None:
        print(f"  volume  {st['volume_db']:+.1f} dB")
    if st.get("gain") is not None:
        print(f"  gain    {'on' if st['gain'] else 'off'}")
    print()
    active = 0
    for i, b in enumerate(st["bands"], 1):
        if not b["on"]:
            continue
        active += 1
        print(f"  band {i:2d}  {b['type']}  {b['freq']:>7.0f} Hz  "
              f"{b['gain']:+5.1f} dB  Q {b['q']:.3f}")
    print(f"  ({active} of {BAND_COUNT} bands active)")


def cmd_dump(args):
    st = load_state()
    out = json.dumps({"bands": st["bands"], "volume_db": st.get("volume_db"),
                      "gain": st.get("gain")}, indent=2)
    if args.file:
        open(args.file, "w").write(out + "\n")
        print(f"wrote {args.file}")
    else:
        print(out)


def cmd_devices(args):
    """List attached devices so an unknown model's PID can be discovered."""
    found = find_devices()
    if not found:
        print(f"no HID devices with VID {THESYCON_VID:#06x} attached.")
        print("that VID is Thesycon's; a Topping DAC should appear here when plugged in.")
        return
    for f in found:
        mark = f"known: {f['key']}" if f["known"] else "UNKNOWN -- see README"
        print(f"  pid={f['pid']:#06x}  {f['product']:<40} {mark}")
    if any(not f["known"] for f in found):
        print("\nAn unknown device is NOT assumed compatible. Adding it to DEVICES is a")
        print("claim that its register map matches, which only testing can establish.")


# --- cli --------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="toppingctl",
        description="Local control for Topping DACs over USB HID.")
    p.add_argument("--device", default="dx5ii", choices=sorted(DEVICES),
                   help="which model to talk to (default: dx5ii)")
    p.add_argument("--dry-run", action="store_true",
                   help="print frames instead of sending them")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("devices", help="list attached Thesycon-VID HID devices")
    a.set_defaults(func=cmd_devices)

    a = sub.add_parser("apply", help="write a PEQ preset (AutoEQ .txt or .json)")
    a.add_argument("file")
    a.set_defaults(func=cmd_apply)

    a = sub.add_parser("flat", help="disable all bands")
    a.set_defaults(func=cmd_flat)

    a = sub.add_parser("preamp", help="set PEQ preamp in dB, e.g. -6.7")
    a.add_argument("db", type=float)
    a.set_defaults(func=cmd_preamp)

    a = sub.add_parser("vol", help="set volume in dB, e.g. -30")
    a.add_argument("db", type=float)
    a.add_argument("--force", action="store_true", help="allow levels above -10 dB")
    a.set_defaults(func=cmd_vol)

    a = sub.add_parser("gain", help="headphone gain")
    a.add_argument("state", choices=["on", "off"])
    a.set_defaults(func=cmd_gain)

    a = sub.add_parser("power", help="wake or sleep the device")
    a.add_argument("state", choices=["on", "off"])
    a.set_defaults(func=cmd_power)

    a = sub.add_parser("show", help="print last-written state")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("dump", help="write last-written state as JSON")
    a.add_argument("file", nargs="?")
    a.set_defaults(func=cmd_dump)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
