#!/usr/bin/env python3
"""Send ONE control-register write, for mapping unknown enums by front panel.

Deliberately not a toppingctl subcommand. The values these registers take are
not yet known, and shipping a guessed enum as a CLI surface is how band 11 got
into BAND_COUNT. This stays a probe until a real mapping is confirmed, then the
mapping -- not this script -- becomes the feature.

The device cannot be read, so this CANNOT restore what was set before. Note the
current setting off the front panel first; you are the only way back.

Usage:
    ./probe.py 0x04 2            # input select = 2
    ./probe.py 0x05 3 --dry-run  # show the frame, send nothing
"""
import argparse
import sys

from toppingctl import REG_CTRL, Device, frame

# Scene save. Writing it overwrites the C1/C2 presets stored on the device,
# which is not recoverable from here. Never probe it.
FORBIDDEN = {0x35: "scene save -- would overwrite the device's stored C1/C2"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sub", help="sub-register, e.g. 0x04")
    ap.add_argument("value", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--device", default=None)
    a = ap.parse_args()

    sub = int(a.sub, 16) if a.sub.lower().startswith("0x") else int(a.sub)
    if sub in FORBIDDEN:
        sys.exit(f"refusing to write 0x{sub:02x}: {FORBIDDEN[sub]}")

    print(f"  71 {sub:02x} <- {a.value}")
    dev = Device(a.dry_run, a.device)
    dev.send(frame(REG_CTRL, sub, a.value), f"probe 71 {sub:02x} = {a.value}")
    dev.commit()
    dev.close()
    if not a.dry_run:
        print("  sent. what does the front panel say?")


if __name__ == "__main__":
    main()
