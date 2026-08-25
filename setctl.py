#!/usr/bin/env python3
"""Set any settings field by name, and verify the device actually took it.

Every mapping here is checked at runtime rather than trusted: the field is read
before the write, written, then read back. If the value did not change to what
was asked, that is reported as a failure rather than assumed to have worked.

That check is the whole point. The register->field mapping for 14 of these
fields is INFERRED from name similarity (brightness -> ScreenBrightness, and so
on) and inference is what produced every wrong entry in this project's history.

    ./setctl.py brightness high
    ./setctl.py pcmFilter f3
    ./setctl.py --list
"""
import argparse
import sys

import devstate
from toppingctl import Device, frame
from vendor_commands import COMMANDS, ENUMS, FIELD_ENUM

# settings-field name -> (vendor command name, confidence).
#
# All of these are INFERRED from name similarity; the vendor bundle does not
# state them. Confidence records how far each has actually been checked, because
# "the device echoed my value back" and "the device did the thing" are different
# claims and this project has a history of conflating them:
#
#   hardware  observed physically doing what the name says
#   register  written and read back correctly, effect NOT observed
#   unchecked never confirmed on any device
#
# Only `hardware` means the register does what its name claims. `register` means
# it accepts and stores a value, which band 11 also did while driving nothing.
ALIASES = {
    "brightness": ("ScreenBrightness", "hardware"),
    "classicVuLevel": ("VuMeterLevel", "register"),
    "vuBarMode": ("VuMode", "register"),
    "bluetoothMode": ("AudioBluetooth", "register"),
    "powered": ("PowerOn", "unchecked"),
    "muted": ("Mute", "unchecked"),
    "highGain": ("HeadphoneGain", "unchecked"),
    "remoteEnabled": ("Remote", "unchecked"),
    "autoScreenTimeout": ("DimScreenTimeout", "unchecked"),
    "inputOptionMask": ("InputOption", "unchecked"),
    "outputOptionMask": ("OutputOption", "unchecked"),
    "crossfeedConvolutionOptionMask": ("CrossfeedConvolutionOption", "unchecked"),
    "crossfeedSimpleOptionMask": ("CrossfeedSimpleOption", "unchecked"),
}
# Never settable from here.
NEVER = {"powered", "sampleRate", "inputOptionMask", "outputOptionMask",
         "crossfeedConvolutionOptionMask", "crossfeedSimpleOptionMask"}

_BY_NAME = {n.lower(): c for c, n in COMMANDS.items()}


def cmd_for(field):
    name, conf = ALIASES.get(field, (field, "vendor"))
    return _BY_NAME.get(name.lower()), name, conf


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("field", nargs="?")
    ap.add_argument("value", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.list or not a.field:
        for f in sorted(set(FIELD_ENUM) | set(ALIASES)):
            c, name, conf = cmd_for(f)
            if not c or f in NEVER:
                continue
            e = FIELD_ENUM.get(f)
            vals = "  ".join(sorted(ENUMS[e].values())) if e else "<int>"
            print(f"  {f:<32} {conf:<9} {vals}")
        return

    field = a.field
    if field in NEVER:
        sys.exit(f"{field} is not settable from here")
    cmd, vendor_name, conf = cmd_for(field)
    if not cmd:
        sys.exit(f"no command maps to {field!r}; try --list")

    enum = ENUMS.get(FIELD_ENUM.get(field, ""), None)
    if a.value is None:
        sys.exit("need a value")
    if enum:
        rev = {n: v for v, n in enum.items()}
        if a.value not in rev:
            sys.exit(f"{field} takes: {', '.join(sorted(rev))}")
        raw = rev[a.value]
    else:
        raw = int(a.value)

    state = devstate.by_name()
    if field not in state:
        sys.exit(
            f"{field} is not reported by this device -- most likely a firmware "
            f"that predates it (indices 48-51 need >= 2.40). Refusing to write a "
            f"register whose effect cannot be read back."
        )
    before = state[field]
    if before == raw:
        sys.exit(
            f"{field} is already {a.value} -- writing it would prove nothing, "
            f"since the read-back would match whether or not the write landed."
        )
    print(f"  {field} = {devstate.label(field, before)}  ->  {a.value}")
    note = "" if conf == "vendor" else f"  [mapping inferred, confidence: {conf}]"
    print(f"  via {vendor_name} (0x{cmd:04x}){note}")

    dev = Device(a.dry_run, None)
    dev.send(frame(cmd >> 8, cmd & 0xFF, raw), f"{vendor_name} = {raw}")
    dev.commit()
    dev.close()
    if a.dry_run:
        return

    after = devstate.by_name().get(field)
    if after == (raw & 0xFFFFFFFF):
        print(f"  VERIFIED: device reports {devstate.label(field, after)}")
    else:
        print(f"  FAILED: device reports {devstate.label(field, after)} "
              f"(raw {after}), expected {raw}")
        sys.exit(1)


if __name__ == "__main__":
    main()
