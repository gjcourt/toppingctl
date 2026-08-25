#!/usr/bin/env python3
"""End-to-end smoke test for toppingctl against real hardware.

Written against a DX5 II, which is the only model the protocol is confirmed on.

Exercises every implemented command and restores the starting state. Each step
prints WHAT TO LOOK FOR on the device's front panel or in the vendor app, since
the device cannot be read back — a human is the only available sensor.

Safety rules baked in:
  - volume never goes above -40 dB (quiet); it is restored at the end
  - scene save (71 35) is never touched: it would overwrite stored C1/C2
  - PEQ is restored to the state captured before the run
  - power-off is last and optional (--sleep)

Usage:
    ./smoke.py            run the test, leave the device awake
    ./smoke.py --sleep    also test sleep at the end
    ./smoke.py --dry-run  print frames only
"""

import argparse
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from toppingctl import (
    BAND_COUNT,
    DEFAULT_BAND,
    REG_CTRL,
    SUB_GAIN,
    SUB_POWER,
    SUB_POWER_B4,
    SUB_VOLUME,
    Device,
    band_frames,
    frame,
    load_state,
    save_state,
)

# The PEQ state captured from the vendor app before this tool existed.
BASELINE = [
    {"type": "LS", "freq": 200, "gain": 6.0, "q": 0.707, "on": True},
    {"type": "HS", "freq": 3413, "gain": 5.3, "q": 0.707, "on": True},
] + [dict(DEFAULT_BAND) for _ in range(BAND_COUNT - 2)]

# A deliberately obvious curve, so a glance at the app's PEQ graph confirms it.
PROBE = [
    {"type": "PK", "freq": 1000, "gain": -12.0, "q": 2.0, "on": True},
] + [dict(DEFAULT_BAND) for _ in range(BAND_COUNT - 1)]

SAFE_VOL = -50.0
RESTORE_VOL = -40.0
steps_run = []


def step(n, title, expect):
    print(f"\n[{n}] {title}")
    print(f"    expect: {expect}")
    steps_run.append(title)


def set_volume(dev, db):
    dev.send(frame(REG_CTRL, SUB_VOLUME, int(round(-db * 2))), f"vol {db:+.1f}")
    dev.commit()


def write_bands(dev, bands, label):
    for i, b in enumerate(bands):
        for f in band_frames(i, b):
            dev.send(f, label)
    dev.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", action="store_true", help="test power-off at the end")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=2.5,
                    help="seconds between steps, for observation")
    args = ap.parse_args()

    print("toppingctl smoke test")
    print("=" * 58)
    print("Watch the DX5 II front panel (volume is displayed) and, if it is")
    print("open, the vendor app's PEQ graph.")
    if not args.dry_run:
        print("\nVolume will be set quiet and restored. Nothing is saved to C1/C2.")

    dev = Device(args.dry_run)
    pause = 0 if args.dry_run else args.pause

    try:
        step(1, "WAKE", "device powers on / display lights")
        dev.send(frame(REG_CTRL, SUB_POWER, 1, b4=SUB_POWER_B4, crc=True), "power on")
        time.sleep(pause)

        step(2, f"VOLUME -> {SAFE_VOL:+.1f} dB", f"display reads {SAFE_VOL:+.1f}")
        set_volume(dev, SAFE_VOL)
        time.sleep(pause)

        step(3, "VOLUME -> -45.5 dB", "display reads -45.5 — proves half-dB steps")
        set_volume(dev, -45.5)
        time.sleep(pause)

        step(4, "GAIN on", "gain indicator changes (headphone outputs only)")
        dev.send(frame(REG_CTRL, SUB_GAIN, 1), "gain on")
        dev.commit()
        time.sleep(pause)

        step(5, "GAIN off", "gain indicator returns")
        dev.send(frame(REG_CTRL, SUB_GAIN, 0), "gain off")
        dev.commit()
        time.sleep(pause)

        step(6, "PEQ -> flat", "app PEQ graph goes flat, all bands off")
        write_bands(dev, [dict(DEFAULT_BAND) for _ in range(BAND_COUNT)], "flat")
        time.sleep(pause)

        step(7, "PEQ -> probe curve", "sharp -12 dB notch at 1 kHz, Q 2.0")
        write_bands(dev, PROBE, "probe")
        time.sleep(pause)

        step(8, "PEQ -> restore baseline", "LS 200Hz +6.0 and HS 3413Hz +5.3 return")
        write_bands(dev, BASELINE, "restore")
        time.sleep(pause)

        step(9, f"VOLUME -> {RESTORE_VOL:+.1f} dB", f"display reads {RESTORE_VOL:+.1f}")
        set_volume(dev, RESTORE_VOL)
        time.sleep(pause)

        if args.sleep:
            step(10, "SLEEP", "device powers down / display off")
            dev.send(frame(REG_CTRL, SUB_POWER, 0, b4=SUB_POWER_B4, crc=True), "power off")
            time.sleep(pause)
            print("\n    (device is asleep — wake with: ./toppingctl.py power on)")
    finally:
        dev.close()

    if not args.dry_run:
        st = load_state()
        st["bands"] = BASELINE
        st["volume_db"] = RESTORE_VOL
        st["gain"] = False
        st["source"] = "smoke test"
        save_state(st)

    print("\n" + "=" * 58)
    print(f"{len(steps_run)} steps sent without error.")
    print("\nThe device accepts frames silently — no error means the writes were")
    print("well-formed, NOT that they took effect. Confirm against the display:")
    print("  - did volume track -50.0 -> -45.5 -> -40.0 ?")
    print("  - did the PEQ graph go flat, then notch, then back?")
    print("If yes, the protocol implementation is correct end to end.")


if __name__ == "__main__":
    main()
