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

The DX1 II stores three scenes (C1-C3) behind a different register, 0x7800.
Its recall needs --unverified, like every DX1 II write, and --force: the
volume it lands on can't be known before recalling, so that is the -10 dB
guard (AGENTS.md). The before/after snapshots come from what the device
pushes -- its full state after connectState, then the settings the recall
changed -- so no readNack is sent (a readNack can write on this model).

    ./scenes.py --device dx1ii recall c3 --dry-run
    ./scenes.py --device dx1ii --unverified --force recall c3

Saving is deliberately absent. SaveC1/SaveC2 overwrite slots the operator may
have set from the front panel, with no undo and no way to read them first.
"""

import argparse
import sys

import devstate
from toppingctl import (
    DX1_F_HP_VOL,
    DX1_F_LO_VOL,
    DX1_F_LOHP_VOL,
    DX1_F_MUTE,
    DX1_F_OPT_VOL,
    DX1_REG_CONNECT,
    DX1_REG_FILTER,
    DX1_REG_HIGH_GAIN,
    DX1_REG_INPUT,
    DX1_REG_SCENE,
    VOL_WARN_DB,
    Device,
    assert_writable,
    dx1_raw_to_db,
    frame,
)
from vendor_commands import COMMANDS

RECALL = {"c1": 0x7111, "c2": 0x7112}

# --- DX1 II ------------------------------------------------------------------
DX1_SLOTS = {"c1": 1, "c2": 2, "c3": 3}
DX1_SCENE_CMD = (DX1_REG_SCENE[0] << 8) | DX1_REG_SCENE[1]
# How long to collect each push. The connectState push is ~300 frames; the
# recall's own push arrived within a few hundred ms on hardware.
DX1_CONNECT_PUSH_SECS = 2.5
DX1_RECALL_PUSH_SECS = 2.0

# Names for the single-frame registers a push can carry, as 16-bit numbers.
DX1_SINGLE_NAMES = {
    (reg << 8) | sub: name for (reg, sub), name in devstate.DX1_STATE_QUERIES.items()
}
DX1_SINGLE_NAMES.update(
    {
        (DX1_REG_FILTER[0] << 8) | DX1_REG_FILTER[1]: "filter",
        (DX1_REG_HIGH_GAIN[0] << 8) | DX1_REG_HIGH_GAIN[1]: "highGain",
        (DX1_REG_INPUT[0] << 8) | DX1_REG_INPUT[1]: "input",
    }
)
DX1_VOLUME_FRAMES = {
    DX1_F_HP_VOL: "hpVolume",
    DX1_F_LO_VOL: "loVolume",
    DX1_F_LOHP_VOL: "loHpVolume",
    DX1_F_OPT_VOL: "optVolume",
}
DX1_F_STATE = 2
DX1_BLOCK_NAMES = {
    1: "protocol",
    DX1_F_STATE: "stateWord",
    **DX1_VOLUME_FRAMES,
    DX1_F_MUTE: "mute",
}


def dx1_live_volume_frames(state_word):
    """The volume frames of the outputs the state word enables (bits 0-2:
    lo, hp, opt -- readsettings.dx1_show's decode). loHpVolume counts when lo
    or hp is on. With no state word, every output counts: the guard errs loud.
    A scene that only feeds the headphones can leave the disabled optical
    output at 0 dB; seen on hardware, 2026-10-31."""
    if state_word is None:
        return dict(DX1_VOLUME_FRAMES)
    live = {}
    for bit, f in ((1, DX1_F_LO_VOL), (2, DX1_F_HP_VOL), (4, DX1_F_OPT_VOL)):
        if state_word & bit:
            live[f] = DX1_VOLUME_FRAMES[f]
    if state_word & 3:
        live[DX1_F_LOHP_VOL] = DX1_VOLUME_FRAMES[DX1_F_LOHP_VOL]
    return live


def dx1_block_name(index):
    return DX1_BLOCK_NAMES.get(index, f"restore[{index}]")


def dx1_value(index, raw):
    """A block value for display: volumes in dB, the rest raw."""
    if raw is None:
        return "?"
    if index in DX1_VOLUME_FRAMES:
        return f"{dx1_raw_to_db(raw):+.1f} dB"
    return str(raw)


def recall_dx1(a):
    slot = DX1_SLOTS[a.slot]
    print(f"  dx1 scene recall C{slot} (0x{DX1_SCENE_CMD:04x} = {slot})")
    if not a.dry_run and not a.force:
        sys.exit(
            "refusing: a recall can change volume, and what the slot holds can't\n"
            "be read before recalling it. Pass --force to recall anyway (-10 dB\n"
            "guard; keep headphones off the first time)."
        )
    assert_writable(a)

    dev = Device(a.dry_run, "dx1ii", allow_unverified=a.unverified)
    try:
        # The baseline comes from the full state push that answers
        # connectState: no readNack, which can write on this model.
        dev.send(frame(*DX1_REG_CONNECT, 1), "dx1 connectState (state push = baseline)")
        before, before_block = (
            ({}, {}) if a.dry_run else devstate.dx1_pushed(dev.h, DX1_CONNECT_PUSH_SECS)
        )
        dev.send(frame(*DX1_REG_SCENE, slot), f"dx1 recall {a.slot}")
        if a.dry_run:
            return 0
        pushed, pushed_block = devstate.dx1_pushed(dev.h, DX1_RECALL_PUSH_SECS)
    finally:
        dev.close()

    echo = pushed.get(DX1_SCENE_CMD)
    if echo != slot:
        print(
            f"  WARNING: the device did not confirm the recall (0x{DX1_SCENE_CMD:04x} "
            f"pushed {echo!r}, expected {slot}). Check the front panel."
        )
        return 1
    print(f"  device confirmed C{slot}")

    after_block = {**before_block, **pushed_block}
    loud = [
        (name, dx1_raw_to_db(after_block[i]))
        for i, name in dx1_live_volume_frames(after_block.get(DX1_F_STATE)).items()
        if after_block.get(i) is not None and dx1_raw_to_db(after_block[i]) > VOL_WARN_DB
    ]
    if loud:
        levels = ", ".join(f"{name} {db:+.1f} dB" for name, db in loud)
        print(f"\n  WARNING: louder than {VOL_WARN_DB:.0f} dB after the recall: {levels}")
        print("  Lower with: ./toppingctl.py --device dx1ii --unverified vol -30\n")
    elif not after_block:
        print("  volume unknown: no 0x810a push arrived -- check the front panel")

    changed = [
        (DX1_SINGLE_NAMES.get(cmd, f"0x{cmd:04x}"), str(before.get(cmd, "?")), str(v))
        for cmd, v in sorted(pushed.items())
        if cmd != DX1_SCENE_CMD and before.get(cmd) != v
    ]
    changed += [
        (dx1_block_name(i), dx1_value(i, before_block.get(i)), dx1_value(i, v))
        for i, v in sorted(pushed_block.items())
        if before_block.get(i) != v
    ]
    if not changed:
        print(f"  nothing changed -- {a.slot} matches the current state")
        return 0
    print(f"  {len(changed)} field(s) changed:")
    for name, b, c in changed:
        print(f"    {name:<24} {b:<16} -> {c}")
    print("\n  PEQ is not included: the recall pushes the active slot, not the curve.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("action", choices=["recall"])
    ap.add_argument("slot", choices=["c1", "c2", "c3"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--device", default="dx5ii", help="dx5ii (default) or dx1ii")
    ap.add_argument("--unverified", action="store_true", help="allow writes to the DX1 II")
    ap.add_argument(
        "--force", action="store_true", help="DX1 II: recall although the volume can't be known"
    )
    a = ap.parse_args()

    if a.device == "dx1ii":
        return recall_dx1(a)
    if a.device != "dx5ii":
        ap.error(f"scene recall is mapped for dx5ii and dx1ii only, not {a.device!r}")
    if a.slot not in RECALL:
        ap.error("the DX5 II stores two scenes: c1, c2")

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
    changed = [
        (k, before.get(k), after.get(k))
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k)
    ]
    if not changed:
        print(f"  nothing changed -- {a.slot} either matches the current state or is empty")
        return 0
    print(f"  {len(changed)} field(s) changed:")
    for k, b, c in changed:
        print(f"    {k:<24} {devstate.label(k, b):<16} -> {devstate.label(k, c)}")
    print("\n  PEQ is not included: band registers echo rather than report,")
    print("  so a scene's filter contents cannot be read back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
