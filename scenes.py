#!/usr/bin/env python3
"""Recall the device's own stored scenes (C1 / C2), and see what changed.

These are the DX5 II's real presets -- stored on the device, recalled from the
front panel or remote. The registers were listed as "unmapped" until the vendor
bundle named them.

A recall can change volume, input, output and PEQ in one go, and there is no way
to know what a slot holds without recalling it. So this snapshots every settings
field before and after and prints the difference, which is also the information
needed to put things back.

    ./scenes.py recall c1
    ./scenes.py recall c2 --dry-run

Saving is deliberately absent. SaveC1/SaveC2 overwrite slots the operator may
have set from the front panel, with no undo and no way to read them first.
"""
import argparse
import sys

import devstate
from toppingctl import Device, frame
from vendor_commands import COMMANDS

RECALL = {"c1": 0x7111, "c2": 0x7112}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["recall"])
    ap.add_argument("slot", choices=["c1", "c2"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cmd = RECALL[a.slot]
    print(f"  {COMMANDS.get(cmd, '?')} (0x{cmd:04x})")

    before = {} if a.dry_run else devstate.by_name()
    dev = Device(a.dry_run, None)
    dev.send(frame(cmd >> 8, cmd & 0xFF, 1), f"recall {a.slot}")
    dev.commit()
    dev.close()
    if a.dry_run:
        return 0

    after = devstate.by_name()
    changed = [(k, before.get(k), after.get(k))
               for k in sorted(set(before) | set(after))
               if before.get(k) != after.get(k)]
    if not changed:
        print(f"  nothing changed -- {a.slot} either matches the current state "
              f"or is empty")
        return 0
    print(f"  {len(changed)} field(s) changed:")
    for k, b, c in changed:
        print(f"    {k:<24} {devstate.label(k, b):<16} -> {devstate.label(k, c)}")
    print("\n  PEQ is not included: band registers echo rather than report,")
    print("  so a scene's filter contents cannot be read back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
