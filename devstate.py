#!/usr/bin/env python3
"""Read the device's settings as a dict. Shared by readsettings.py and set."""
import time

from toppingctl import frame, open_checked
from vendor_commands import ENUMS, FIELD_ENUM, SETTINGS_FIELDS

GET_SETTINGS = (0x71, 0x0C)

# --- DX1 II -------------------------------------------------------------
# The DX1 II has no 0x710c GetSettings: the read channels are (a) the 12-frame
# output-state block at 0x810a -- volume and mute live only there -- (b)
# readNack queries against individual dx1 registers, and (c) the 3x78-word PEQ
# config dump behind 0x1106. Everything here prefixes the report id, because
# this firmware drops unprefixed frames (see Device._wire, report_id_prefix).


def _prefixed(framebytes):
    return bytes([0x00]) + framebytes[:15]


def _frames(h, secs):
    """Collect 22 33 protocol frames for secs; skips the idle all-zero
    reports and the device's own 1 Hz 0x111a heartbeat tick."""
    import time as _t
    out, t0 = [], _t.time()
    while _t.time() - t0 < secs:
        try:
            b = h.read(64, timeout=100)
        except Exception:
            continue
        if not b or not any(bytes(b)):
            continue
        b = bytes(b)
        if b[0] == 0x22 and b[1] == 0x33:
            cmdr = (b[5] << 8) | b[6]
            if cmdr == 0x111A:
                continue
            out.append((cmdr, b[3], b[4], int.from_bytes(b[7:11], "big")))
    return out


def dx1_read_block(h, secs=1.5):
    """readNack the 0x810a output-state block -> {frame_index: value}."""
    h.write(_prefixed(frame(0x81, 0x0A, 0, opcode=0x10)))
    return {b[2]: b[3] for b in _frames(h, secs) if b[0] == 0x810A}


def dx1_query(h, reg, sub, secs=0.7):
    """One dx1 register readNack. Returns the device's current value or None.

    ⚠️ ONLY SAFE FOR THE VENDOR'S QUERY LIST. Measured on hardware:
    for registers outside that list the device treats an
    incoming readNack as a WRITE of the data field -- a readNack with
    data=0 zeroes the register. This was not obvious: the "reads" looked
    plausible while they silently reset the user's gain/filter/input to
    defaults, and the zero that came back was the echo of our own
    clobbering. The safe set is what the vendor bundle's
    requestGroup builder allows -- 0x7100, 0x7900, 0x7d00, 0x810b, 0x810c,
    0x810a, 0x810d, 0x810e, 0x810f, 0x8200, 0x8300, 0x8400, 0x1204,
    0x1206 -- plus the 3-slot PEQ dump 0x1106 (dx1_read_configs).
    Everything else (gain, filter, brightness, input, display
    mode, ...) is write-only for us; its state arrives as an unsolicited
    push after it changes.
    """
    h.write(_prefixed(frame(reg, sub, 0, opcode=0x10)))
    hits = [b for b in _frames(h, secs) if b[0] == ((reg << 8) | sub) and b[2] == 1]
    return hits[0][3] if hits else None


def dx1_read_configs(h, secs=4.5):
    """readNack 0x1106 -> the stored PEQ configs as 78-word lists, in slot
    order. Frames arrive strictly sequential (slot 0's curFrame 0..77, then
    slot 1's, ...); segmented on the curFrame reset, duplicates dropped."""
    h.write(_prefixed(frame(0x11, 0x06, 0, opcode=0x10)))
    hits = _frames(h, secs)
    configs, cur = [], {}
    for cmdr, ln, cf, val in hits:
        if cmdr != 0x1106 or ln < 70:
            continue
        if cf == 0 and cur:
            if len(cur) >= 78:
                configs.append([cur[i] for i in range(78)])
            cur = {}
        if cf not in cur:
            cur[cf] = val
    if len(cur) >= 78:
        configs.append([cur[i] for i in range(78)])
    return configs


def dx1_state(dev_key="dx1ii"):
    """Everything the DX1 II will safely say about itself, decoded by
    readsettings. Queries are restricted to the vendor's read-safe register
    list (see dx1_query -- anything else turns the readNack into a write of
    the data field and clobbers the user's settings). The write-only
    registers (gain, filter, brightness, input, display mode, ...) are
    deliberately absent: their state only arrives as an unsolicited push."""
    h = open_checked(dev_key)
    try:
        blk = dx1_read_block(h)
        regs = {}
        for reg, sub, name in [
            (0x71, 0x00, "state"), (0x79, 0x00, "autoStandby"),
            (0x7D, 0x00, "autoScreenOff"), (0x81, 0x0B, "analogBalance"),
            (0x81, 0x0C, "optBalance"), (0x81, 0x0E, "remoteArrow"),
            (0x81, 0x0F, "remoteMute"), (0x82, 0x00, "knobSingle"),
            (0x83, 0x00, "knobDouble"), (0x84, 0x00, "knobEventCaps"),
            (0x12, 0x04, "eqEnableState"), (0x12, 0x06, "eqCurrentConfig"),
        ]:
            regs[name] = dx1_query(h, reg, sub)
        configs = dx1_read_configs(h)
        return {"block": blk, "regs": regs, "configs": configs}
    finally:
        h.close()


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
