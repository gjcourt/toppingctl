#!/usr/bin/env python3
"""Read the DAC's actual state. Not a cache -- the device is queried."""
import argparse
import math

import devstate
from toppingctl import DEVICES, DX1_F_MUTE
from vendor_commands import (
    ENUMS,
    FIELD_ENUM,
    SETTINGS_FIELDS,
    decode_balance,
    decode_sample_rate,
    decode_version,
)


def volume_step_db(rec):
    """dB per raw volume unit, read from the device rather than assumed.

    volumeStep (settings field 32) selects the unit: 0 = half_db, 1 = one_db.
    Falls back to 0.5 only if the field is absent, which would mean a firmware
    that predates it.
    """
    try:
        return 1.0 if rec[32] == 1 else 0.5
    except (IndexError, KeyError, TypeError):
        return 0.5


def dx1_db(raw):
    return max(0, min(990, int(raw))) / 10 - 99


def dx1_show(st):
    """Decode the DX1 II's state (devstate.dx1_state) -- block, registers and
    the stored PEQ configs. Every value here was pushed by the device."""
    blk = st["block"]
    regs = st["regs"]
    print("device      DX1 II (dx1 next protocol)")
    print("block       0x810a output state")
    for idx, label in [(3, "hpVolume"), (4, "loVolume"), (5, "loHpVolume"), (6, "optVolume")]:
        if idx in blk:
            print(f"  f{idx}  {label:<12} {blk[idx]:>4}   = {dx1_db(blk[idx]):+.1f} dB")
    if DX1_F_MUTE in blk:
        m = blk[DX1_F_MUTE]
        print(f"  f7  mute        {m:>4}   = analog {'muted' if m & 1 else 'unmuted'}, "
              f"opt {'muted' if m & 2 else 'unmuted'}")
    s = blk.get(2)
    if s is not None:
        mask = s & 7
        outs = "/".join(n for bit, n in [(1, "lo"), (2, "hp"), (4, "opt")] if mask & bit) or "none"
        print(f"  f2  state       {s:>4}   = outputs {outs}, "
              f"volume {'linked' if s >> 4 & 1 else 'independent'}, "
              f"eq route {('analog', 'opt', 'both')[s >> 6 & 3]}")
    print("\nregisters (read-safe set only -- see devstate.dx1_query; the")
    print("write-only registers' state arrives as unsolicited pushes)")
    labels = {
        "state": ("state", {1: "working", 2: "standby"}),
        "autoStandby": ("autoStandby", {0: "on", 1: "off"}),
        "autoScreenOff": ("autoScreenOff", None),
        "analogBalance": ("analogBalance", None),
        "optBalance": ("optBalance", None),
        "remoteArrow": ("remoteArrow", None),
        "remoteMute": ("remoteMute", None),
        "knobSingle": ("knobSingle", None),
        "knobDouble": ("knobDouble", None),
        "knobEventCaps": ("knobEventCaps", None),
        "eqEnableState": ("eqEnableState", None),
        "eqCurrentConfig": ("eqCurrentConfig", None),
    }
    for key, (label, table) in labels.items():
        v = regs.get(key)
        if v is None:
            continue
        extra = f"   = {table.get(v, '?')}" if table else ""
        if key == "eqEnableState":
            extra = (f"   = {'on' if v & 2 else 'off'}"
                     f" (runtime {'active' if v & 1 else 'idle'}, valid={bool(v & 4)})")
        if key == "eqCurrentConfig":
            extra = f"   = EQ{v + 1 if v is not None and v <= 2 else '?'} active"
        print(f"  {label:<18} {v:<6}{extra}")

    print("\nPEQ configs (slot order)")
    for i, words in enumerate(st["configs"], 1):
        nb = []
        for w in words[0:4]:
            nb += [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]
        name = bytes(nb[:15]).split(b"\x00")[0].decode("ascii", "replace").strip() or "Config"
        pre_l = 20 * math.log10(words[5] / 2**25) if words[5] else float("-inf")
        pre_r = 20 * math.log10(words[7] / 2**25) if words[7] else float("-inf")
        active = []
        for ch, off in (("L", 8), ("R", 41)):
            for j in range(11):
                p, fr = words[off + 3 * j], words[off + 3 * j + 1]
                g = (p >> 16) & 0xFF
                if g > 127:
                    g -= 256
                if p & 0xFF == 1:
                    active.append(f"{ch}{j + 1}:{fr}Hz {g / 10:+.1f}dB")
        cur = "  <= ACTIVE" if regs.get("eqCurrentConfig") == i - 1 else ""
        print(f"  slot {i} \"{name}\"  preamp {pre_l:+.1f}/{pre_r:+.1f} dB, "
              f"{len(active)} band(s) on{cur}")
        if active:
            print("    " + "  ".join(active))


# devstate owns the read: it retries when another client holds the device and
# tolerates the hid wrapper raising on benign zero-length reports.
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--device", default="dx5ii")
dev_key = ap.parse_args().device

if DEVICES[dev_key].get("protocol") == "dx1":
    dx1_show(devstate.dx1_state(dev_key))
    raise SystemExit(0)

rec = devstate.read_settings(dev_key, secs=3.0)

name = b"".join(rec.get(i, 0).to_bytes(4, "big")[::-1] for i in range(1, 9))
# Extracted rather than inlined: nested same-type quotes inside an f-string
# are PEP 701, which needs Python >= 3.12. The audio nodes this runs on ship
# Python 3.11, and CI only tested 3.14, so the incompatibility stayed
# invisible until the tool was actually run on one.
dev_name = name.split(b"\x00")[0].decode("ascii", "replace").strip()
print(f"device      {dev_name}")
print(f"records     {len(rec)}  (firmware >= 2.40 adds 48..51: "
      f"{'yes' if max(rec, default=0) >= 48 else 'no'})\n")
for i in sorted(rec):
    if i in range(1, 9):
        continue
    f = SETTINGS_FIELDS.get(i, f"idx{i}")
    v = rec[i]
    extra = ""
    if f == "volume":
        # The raw unit is NOT fixed: volumeStep (field 32) selects it.
        # Measured on a DX5 II, 2026-08-27, against the front panel:
        #   half_db: raw 60 -> -30.0 dB, raw 55 -> -27.5 dB   (0.5 dB/unit)
        #   one_db:  raw 25 -> -25.0 dB                       (1.0 dB/unit)
        # Hardcoding /2 reported -12.5 dB while the panel read -25.0.
        extra = f"   = {-v * volume_step_db(rec):+.1f} dB"
    elif f.endswith("Mask"):
        extra = f"   = 0b{v:b}  ({bin(v).count('1')} options)"
    elif f in ("powered", "muted", "highGain", "bluetoothAptx", "remoteEnabled"):
        extra = f"   = {bool(v)}"
    elif f == "balance":
        extra = f"   = {decode_balance(v)}"
    elif f == "sampleRate":
        extra = f"   = {decode_sample_rate(v)}"
    elif f == "dcDetectSensitivity":
        extra = f"   = {'high' if v else 'low'}"
    elif i in (45, 46, 47):
        extra = f"   = version {decode_version(v)}"
    elif f in FIELD_ENUM:
        extra = f"   = {ENUMS[FIELD_ENUM[f]].get(v, '?')}"
    print(f"  [{i:2d}] {f:<32} {v:<6}{extra}")
