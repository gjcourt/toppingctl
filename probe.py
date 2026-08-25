#!/usr/bin/env python3
"""Send ONE control-register write, for mapping unknown enums by front panel.

Deliberately not a toppingctl subcommand. The values these registers take are
not yet known, and shipping a guessed enum as a CLI surface is how band 11 got
into BAND_COUNT. This stays a probe until a real mapping is confirmed, then the
mapping -- not this script -- becomes the feature.

This writes. Run ./readsettings.py first and record the current value -- that is
your way back, and it is an actual read now rather than a guess.

Usage:
    ./probe.py 0x04 2            # input select = 2
    ./probe.py 0x05 3 --dry-run  # show the frame, send nothing
"""
import argparse
import sys

from listen import NEVER
from toppingctl import REG_CTRL, Device, frame
from vendor_commands import COMMANDS

# Derived from listen.py's list rather than restated, so the read-only tool and
# the writing one cannot drift apart. They had: this file once blocked only
# scene-save, leaving FactoryReset and FirmwareUpdate reachable from the tool
# that actually writes -- the guard was inverted relative to the risk.
FORBIDDEN = {c & 0xFF: n for c, n in COMMANDS.items() if n in NEVER}


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

    name = COMMANDS.get((REG_CTRL << 8) | sub, "unknown")
    print(f"  71 {sub:02x} <- {a.value}   ({name})")
    if not a.dry_run:
        if input(f"  write {name} = {a.value}? [y/N] ").strip().lower() != "y":
            sys.exit("  aborted")
    dev = Device(a.dry_run, a.device)
    dev.send(frame(REG_CTRL, sub, a.value), f"probe 71 {sub:02x} = {a.value}")
    dev.commit()
    dev.close()
    if not a.dry_run:
        print("  sent. what does the front panel say?")


if __name__ == "__main__":
    main()
