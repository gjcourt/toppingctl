#!/usr/bin/env python3
"""Read the device's settings as a dict. Shared by readsettings.py and set."""
import time

from toppingctl import frame, open_checked
from vendor_commands import ENUMS, FIELD_ENUM, SETTINGS_FIELDS

GET_SETTINGS = (0x71, 0x0C)


def read_settings(dev_key="dx5ii", secs=2.0):
    """Query the device and return {index: raw_value}."""
    # Opening can fail transiently when something else holds the device -- the
    # vendor web app claims it over WebHID, and only one client gets it. The
    # device is still enumerated, so this is contention, not absence.
    for attempt in range(4):
        try:
            h = open_checked(dev_key)
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(0.4)
    try:
        h.write(frame(*GET_SETTINGS, 0, opcode=0x10))
        rec, t0 = {}, time.time()
        while time.time() - t0 < secs:
            try:
                b = h.read(64, timeout=200)
            except Exception:
                # The hid wrapper raises HIDException("Success") on a benign
                # zero-length read. It is noise, not a failure -- the device
                # streams empty reports when it has nothing to say.
                continue
            if b and len(b) >= 15 and b[0] == 0x22 and b[1] == 0x33 \
                    and b[5] == GET_SETTINGS[0] and b[6] == GET_SETTINGS[1]:
                rec[b[4]] = int.from_bytes(bytes(b[7:11]), "big")
        if not rec:
            raise RuntimeError(
                "device returned no settings records -- a timeout or a lost "
                "handle, not a missing field. Retry."
            )
        return rec
    finally:
        h.close()


def by_name(dev_key="dx5ii", secs=2.0):
    """Same, keyed by the vendor's field name."""
    rec = read_settings(dev_key, secs)
    return {SETTINGS_FIELDS[i]: v for i, v in rec.items() if i in SETTINGS_FIELDS}


def label(field, value):
    """Decode a raw value through its enum, if it has one."""
    e = FIELD_ENUM.get(field)
    return ENUMS[e].get(value, f"?{value}") if e else str(value)
