#!/usr/bin/env python3
"""Read the DX5 II's actual state. Not a cache -- the device is queried."""
import devstate
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

# devstate owns the read: it retries when another client holds the device and
# tolerates the hid wrapper raising on benign zero-length reports.
rec = devstate.read_settings(secs=3.0)

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
