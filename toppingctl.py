#!/usr/bin/env python3
"""toppingctl — local control for Topping DACs, over USB HID.

No vendor app, no cloud account, no dependency on toppingaudio.com (which is
unreachable from some US ISPs, which is why this exists).

Protocol spec: gjcourt/lab 01-audio-midi/_reference/topping-dx5ii-hid-protocol.md

Reverse-engineered and hardware-confirmed on a DX5 II. Other Topping models very
likely share the register map -- the vendor drives them from one web app -- but
only the DX5 II has been proven. See DEVICES and README "Adding a device".

    toppingctl apply <autoeq.txt|preset.json>   write a PEQ preset
    toppingctl dump [file]                      write current known state to JSON
    toppingctl show                             print current known state
    toppingctl flat                             disable every band
    toppingctl preamp <dB>                      set PEQ preamp, e.g. -6.7
    toppingctl vol <dB>                         set volume, e.g. -30
    toppingctl gain on|off                      headphone gain
    toppingctl power on|off                     wake / sleep

Add --dry-run to any command to print frames without sending them.

NOTE ON READING THE DEVICE.
The device CAN be read: `readsettings.py` / `devstate.read_settings()` query it
and return live state. (An earlier version of this file said reads were only an
echo; that was superseded by the read work and is no longer true.) What is still
cached in ~/.toppingctl/state.json is the PEQ band state, which the tool tracks
locally because it is not read back. Anything changed
via the front panel, the remote, or the vendor app is invisible to us and will
make the cache stale. `apply` always writes every band, so a preset is still
applied correctly regardless of cache accuracy.
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("missing dependency: pip3 install hid   (and: brew install hidapi)")

# --- device table -----------------------------------------------------------
#
# VID 0x152A is Thesycon's, whose XMOS USB-audio stack many DAC vendors ship.
# The VID therefore does NOT imply Topping; the PID is what identifies a model.
#
#   confirmed  -- driven against real hardware, front panel observed
#   unverified -- register map assumed identical, PID unknown until someone
#                 with the device runs `toppingctl devices`
#
DEVICES = {
    # ⚠️ "status" is ENFORCED, not a label. A device that is not "confirmed"
    # refuses writes unless --unverified is passed. It used to be decorative,
    # which meant adding an entry -- documented as "a claim that the register
    # map matches" -- silently granted full write access to hardware nobody had
    # tested. Reads and --dry-run are always allowed; that is how you test.
    "dx5ii": {
        "name": "Topping DX5 II",
        "vid": 0x152A,
        "pid": 0x8750,
        "bands": 10,
        # PID 0x8750 is NOT unique: the DX1 II, E50 II and D90 III Discrete ship the same one, and
        # their register maps collide across 0x7101-0x7131 with DIFFERENT
        # meanings -- 0x7113 is HomePage here and BluetoothMode on an E50 II.
        # So the PID cannot identify the model and the USB product string is
        # what actually distinguishes them, which is what the vendor app uses.
        "product_match": ("DX5II", "DX52", "DX5"),
        "status": "confirmed",
    },
    "d90iii": {
        "name": "Topping D90 III Discrete",
        "vid": 0x152A,
        # Read off real hardware 2026-08-28: the D90 III reports the SAME
        # 0x8750 as the DX5 II, confirming the PID identifies nothing.
        "pid": 0x8750,
        "product_match": ("D90III", "D90IIIDISCRETE", "D90"),
        # Report id 0 + 15-byte payload. See Device._wire().
        "report_id_prefix": True,
        # The vendor manual (TP234A v1.4, p.10) documents ten: "TOPPING Tune
        # 拥有 PEQ 调节功能, 支持十段自定义频点调节". Still None, deliberately.
        # The DX5 II's 10 was established by writing a filter to each band and
        # listening, and that same exercise found an eleventh register that
        # accepts writes and drives nothing -- so vendor documentation is
        # evidence, not confirmation. Set this to 10 once a filter written to
        # band 10 is audible on THIS device.
        "bands": 10,
        # ✅ WRITES CONFIRMED ON HARDWARE 2026-08-28, once the framing was
        # fixed -- see Device._wire(). The register map DOES transfer; the
        # earlier failures were entirely a one-byte framing error:
        #
        #   >>> write band1 freq = 1000 Hz
        #   echo reg=0x91 sub=0x02 value=1000 crc=c227
        #
        # The device ECHOES every accepted write back with a computed CRC,
        # which is an acknowledgement channel this codebase has never used.
        # It is the direct answer to the README's "a DAC that silently
        # accepts a wrong register write is the failure mode to fear": with
        # echoes, that failure is detectable rather than silent. Wiring
        # send() to verify against the echo is the obvious follow-up.
        #
        # Still NOT marked confirmed, for one specific reason: VOLUME. The
        # vendor's own Topping Tune never touches 0x71 in 298 captured
        # frames, and 0x71/0x02 does not move this device's front panel even
        # with correct framing. PEQ (0x91-0x9b), preamp (0x9c) and EQ enable
        # (0x9e sub 01) are all exercised by the vendor and echo correctly.
        # Historical note -- the original failures, before framing was fixed:
        #   - The settings READ returns no records at all, so volumeStep
        #     cannot be discovered (hence --vol-step).
        #   - A volume WRITE is accepted with no error -- `vol -30` reported
        #     "volume -30.0 dB (raw 60)" -- and the front panel did not move.
        #     Line Out was PRE at the time, so volume was adjustable; the
        #     device simply swallowed it.
        # That is the exact failure this guard exists for: silent acceptance
        # of a wrong register. Do NOT mark this confirmed, and do not assume
        # any other register in the DX5 II map applies here.
        #
        # WHY THE DX5 II ROUTE DOES NOT REPEAT HERE. Topping ship two
        # different tools, with COMPLEMENTARY -- not overlapping -- device
        # support, verified 2026-08-28:
        #   home.toppingaudio.com (web) : DX1 II, DX5 II          <- JS bundle
        #   Topping Tune (desktop V1.16): D50 III, D90 III Discrete,
        #                                 Centaurus, D900, DX9 Discrete,
        #                                 E50 II, DX1 II          <- Qt/C++
        # The DX5 II map in vendor_commands.py came from the WEB app's
        # JavaScript bundle. The web app has no d90iii device code at all --
        # its /client-capabilities feature flags list only dx1ii and dx5ii --
        # so there is no bundle to read for this model. The desktop app that
        # does drive it is a Qt binary: its strings carry the device names but
        # no command tables, and HID goes straight to IOKit.
        #
        # So the D90 III protocol is not statically extractable the way the
        # DX5 II's was. It needs USB capture against the desktop app
        # (Windows + USBPcap is the tractable rig; there is no Linux build).
        #
        # Band count: Topping Tune reads "EQ Max NUM:10" off the device, and
        # writes to 0x91 now echo correctly, so 10 is set below.
        #
        # And one documented difference already argues against assuming it.
        # The DX5 II's volumeStep is a global half_db/one_db choice. On the
        # D90 III the manual (setup item 17) makes the step RANGE-DEPENDENT:
        # fixed 0.5 dB above -50 dB, selectable 0.5 or 1.0 dB from -50 to
        # -99 dB. Any code deriving a dB scale from a single volumeStep field
        # is therefore wrong here for part of the range. Resolve before
        # marking confirmed.
        "status": "unverified",
    },
}

THESYCON_VID = 0x152A

# --- protocol constants (see spec) ------------------------------------------

# The DAC presents multiple HID interfaces; the control protocol lives on
# usage page 1. Writes to the others fail at the OS layer.
PROTOCOL_USAGE_PAGE = 1

REG_CTRL = 0x71           # device control
SUB_POWER = 0x01
SUB_VOLUME = 0x02
SUB_GAIN = 0x17
# The vendor calls 0x34 Heartbeat, not Commit. It does bracket every
# transaction, which is why it read like a commit -- but the name is theirs and
# the mental model here may be wrong. Kept as SUB_COMMIT because that is what
# this code has always meant by it; renaming is a separate change.
SUB_COMMIT = 0x34  # vendor name: Heartbeat

PEQ_FIRST, PEQ_LAST = 0x91, 0x9B      # 11 band registers exist ...
REG_PREAMP = 0x9C                     # preamp: subs 01/03 = value L/R, 02/04 = enable
PREAMP_SCALE = 1 << 25                # linear gain in Q25 fixed point
REG_COUNT = PEQ_LAST - PEQ_FIRST + 1  # ... and all 11 are written
# ... but only 10 of them DO anything. Topping documents 10; the vendor app
# nonetheless writes all eleven, which made an undocumented eleventh band a
# reasonable hypothesis -- hardware does sometimes exceed its datasheet. The
# error was shipping that hypothesis as the default before testing it, so every
# preset depended on an unverified guess. Tested 2026-08-24 on a DX5 II: a
# PK 1 kHz -15 dB Q 0.7 cut written to band 10 (0x9a) was plainly audible, the
# identical filter written to band 11 (0x9b) was inaudible, and re-applying
# band 10 brought the cut back -- so the silence was the register, not the rig.
# 0x9b accepts writes and commits without error; it is simply not wired to a
# filter. The vendor UI's "BANDS n / 10" reports the hardware correctly.
#
# All 11 registers are still written, so a stale band 11 left behind by the
# vendor app gets cleared rather than lingering. Only 10 are offered.
BAND_COUNT = 10


def band_count(spec):
    """Bands for a model, or exit if that number was never established.

    Per-device "bands" used to be dead data -- declared in DEVICES and never
    read, with this module-level constant used everywhere instead. That is
    harmless while one model is supported and wrong the moment a second is
    added, so it now resolves per device and refuses rather than defaulting.
    """
    n = spec.get("bands", BAND_COUNT)
    if n is None:
        sys.exit(
            f"{spec['name']}: PEQ band count is not established for this model.\n"
            "It is not read from a datasheet -- the DX5 II's 10 was found by "
            "writing a filter to each band and listening, which also caught an "
            "eleventh register that accepts writes and drives nothing.\n"
            "Do the same here before using PEQ, then set \"bands\" in DEVICES."
        )
    return n

# Per-band sub-indices. 01-05 left channel, 06-0a right.
SUB_TYPE, SUB_FREQ, SUB_GAIN_, SUB_Q, SUB_ON = 1, 2, 3, 4, 5
CHANNEL_OFFSET = 5

# AutoEQ emits LSC/HSC ("corrected" shelves) as well as LS/HS. They are the same
# standard biquad shelf at the Q values AutoEQ uses, so they map to the same
# device filter types.
FILTER_TYPES = {"PK": 1, "LS": 4, "HS": 5, "LSC": 4, "HSC": 5}
FILTER_NAMES = {1: "PK", 4: "LS", 5: "HS"}

# A band the device considers unused. Taken from vendor-app captures — 632 Hz is
# the factory default centre, not a meaningful setting.
DEFAULT_BAND = {"type": "PK", "freq": 632, "gain": 0.0, "q": 0.707, "on": False}

# Power is the one register the device checksums. It used to be two frames
# replayed verbatim from capture because the algorithm was unknown; it is
# CRC-16/MODBUS, so they are computed now. b4 is 0 here, not the usual 1, and
# that byte is inside the CRC -- which is why the replayed frames worked and a
# naive rebuild would not have.
SUB_POWER_B4 = 0x00

STATE_DIR = os.path.expanduser("~/.toppingctl")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Volume is attenuation in half-dB steps. Clamp hard: a wrong value here is
# instantly loud into headphones, and that is the one irreversible mistake this
# tool could make.
VOL_MIN_DB, VOL_MAX_DB = -99.0, 0.0
VOL_WARN_DB = -10.0


def db_to_q25(db):
    """Preamp is LINEAR gain in Q25 fixed point, not a dB integer.

    dB = 20*log10(value / 2^25); value = round(10^(dB/20) * 2^25)

    Decoded 2026-08-08 and confirmed by round trip: derived from Topping's own
    0x016A77C4 (= -3.0 dB), then used to PREDICT the value for -6.0 dB, write it,
    and read it back in the vendor app.

    NOTE ON THE DISPLAY: the app TRUNCATES toward zero rather than rounding, so a
    mathematically exact -6.0000000 dB shows as "-5.9". That is a display
    convention, not an encoding error - do not "correct" it with an epsilon.
    """
    return int(round((10 ** (db / 20.0)) * PREAMP_SCALE))


def q25_to_db(v):
    import math
    return 20 * math.log10(v / PREAMP_SCALE) if v > 0 else float("-inf")


# --- frame construction -----------------------------------------------------

def crc16_modbus(data):
    """Reflected CRC-16, polynomial 0xA001, init 0xFFFF, no final XOR."""
    n = 0xFFFF
    for b in data:
        n ^= b
        for _ in range(8):
            n = ((n >> 1) ^ 0xA001) if n & 1 else (n >> 1)
            n &= 0xFFFF
    return n


def frame(reg, sub, value, opcode=0x20, b4=0x01, crc=False):
    """22 33 <op> 01 <b4> <reg> <sub> <int32 BE> <ck ck> 66 77 00

    Bytes 11-12 are a CRC-16/MODBUS over the nine bytes between the 22 33
    header and the checksum itself, stored HIGH BYTE FIRST -- the reverse of
    standard Modbus framing, which is why it resisted earlier attempts.

    The device accepts 00 00 for every register except power, and the vendor app
    likewise computes the CRC only for power, so crc defaults to False to match
    the traffic rather than to be tidy.
    """
    v = int(value) & 0xFFFFFFFF
    f = [0x22, 0x33, opcode, 0x01, b4, reg, sub,
         (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]
    c = crc16_modbus(f[2:11]) if crc else 0
    return bytes(f + [(c >> 8) & 0xFF, c & 0xFF, 0x66, 0x77, 0x00])


def band_frames(index, band):
    """All 10 frames for one band: five parameters x two channels."""
    reg = PEQ_FIRST + index
    ftype = FILTER_TYPES[band["type"]]
    freq = int(round(band["freq"]))
    gain = int(round(band["gain"] * 10))        # tenths of a dB, signed
    q = int(round(band["q"] * 10000))           # x10^4
    on = 1 if band["on"] else 0
    out = []
    for ch in (0, CHANNEL_OFFSET):
        out += [
            frame(reg, SUB_TYPE + ch, ftype),
            frame(reg, SUB_FREQ + ch, freq),
            frame(reg, SUB_GAIN_ + ch, gain),
            frame(reg, SUB_Q + ch, q),
            frame(reg, SUB_ON + ch, on),
        ]
    return out


def preamp_frames(db):
    """Both channels, matching the vendor app's 9c 01..04 sequence."""
    v = db_to_q25(db)
    return [
        frame(REG_PREAMP, 0x01, v),
        frame(REG_PREAMP, 0x02, 1),
        frame(REG_PREAMP, 0x03, v),
        frame(REG_PREAMP, 0x04, 1),
    ]


# --- device -----------------------------------------------------------------

def _product_matches(spec, product_string):
    """True if this USB product string is one this spec claims.

    Extracted so find_devices() and Device._check_model() apply the SAME test.
    They used to differ: the open path checked the product string and the
    listing path checked only the PID, which is how an unknown model came to be
    listed as a known one.
    """
    want = spec.get("product_match")
    if not want:
        return True
    name = Device._normalise(product_string)
    return any(name.startswith(w) or w in name for w in want)


def find_devices():
    """Enumerate every Thesycon-VID HID device, flagging which we know."""
    found = []
    for d in hid.enumerate(THESYCON_VID, 0):
        # Match on the PRODUCT STRING, not the PID. Matching on PID alone
        # reported a D90 III Discrete as "known: dx5ii" -- measured 2026-08-28
        # on real hardware -- because six Topping models share 0x8750. That
        # inverted the safety default the README's step 1 depends on: an
        # unrecognised DAC appeared as an already-confirmed one, so following
        # the documented procedure walked straight past the guard.
        key = next(
            (k for k, v in DEVICES.items()
             if v["pid"] == d["product_id"] and _product_matches(v, d.get("product_string"))),
            None,
        )
        found.append({
            "key": key,
            "pid": d["product_id"],
            "product": d.get("product_string") or "?",
            "known": key is not None,
        })
    return found


def open_checked(dev_key="dx5ii"):
    """Open the device with the model check applied.

    Everything that talks to the hardware must come through here. The check
    used to live only in Device.__init__, so devstate, meters and listen -- all
    of which open their own handle -- drove whatever was on the VID/PID pair.
    An E50 II would have been decoded through the DX5 II settings map and
    reported as confident wrong values.
    """
    import hid
    spec = DEVICES[dev_key]
    probe = Device.__new__(Device)
    probe.spec = spec
    path = probe._check_model()
    return hid.Device(path=path) if path else hid.Device(spec["vid"], spec["pid"])


class Device:
    """A Topping DAC over USB HID.

    Was `DX5` and hardcoded to one PID. The protocol layer is model-independent;
    only the PID and band count differ, so both now come from DEVICES.
    """

    def __init__(self, dry_run=False, key=None, allow_unverified=False):
        self.dry_run = dry_run
        self.allow_unverified = allow_unverified
        self.h = None
        self.key = key or "dx5ii"
        if self.key not in DEVICES:
            sys.exit(f"unknown device {self.key!r}; known: {', '.join(DEVICES)}")
        self.spec = DEVICES[self.key]
        if not dry_run:
            path = self._check_model()
            try:
                self.h = hid.Device(path=path) if path else \
                    hid.Device(self.spec["vid"], self.spec["pid"])
            except Exception as e:
                hint = ""
                others = [f for f in find_devices() if not f["known"]]
                if others:
                    hint = ("\n\nUnrecognised Thesycon-VID devices are attached:\n" +
                            "\n".join(f"  pid={f['pid']:#06x}  {f['product']}" for f in others) +
                            "\nIf one is yours, see README \"Adding a device\".")
                sys.exit(f"cannot open {self.spec['name']} "
                         f"({self.spec['vid']:#06x}/{self.spec['pid']:#06x}): {e}\n"
                         f"is it plugged in? if this is a permissions error, grant "
                         f"your terminal Input Monitoring in System Settings.{hint}")

    @staticmethod
    def _normalise(name):
        """Vendor's comparison: upper-case, Roman numeral to II, alnum only."""
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        return "".join(c for c in (name or "").upper().replace("\u2161", "II")
                       if c.isalnum())

    def _check_model(self):
        """Refuse to drive a device whose product string is not this model.

        Writing a DX5 II register map to an E50 II would not fail loudly -- the
        registers exist on both and mean different things -- so this is checked
        before anything is opened rather than discovered afterwards.
        """
        want = self.spec.get("product_match")
        if not want:
            return None
        try:
            entries = hid.enumerate(self.spec["vid"], self.spec["pid"])
        except Exception as e:
            sys.exit(f"cannot enumerate HID devices: {e}")
        matched = [d for d in entries
                   if any(self._normalise(d.get("product_string")).startswith(w)
                          or w in self._normalise(d.get("product_string"))
                          for w in want)]
        # Opening by VID/PID when several devices share it would hand back an
        # arbitrary one -- possibly the very E50 II this check exists to avoid.
        # So return a matched entry's path and open THAT, not the pair.
        #
        # But one DAC exposes SEVERAL HID interfaces (this one presents two,
        # usage pages 1 and 12, on the same serial), so a count of matches does
        # not distinguish "two interfaces" from "two devices". Group by serial:
        # more than one distinct serial is a real collision, more than one entry
        # sharing a serial is just one device with several endpoints.
        if matched:
            # One DAC exposes several HID interfaces and only ONE speaks this
            # protocol: usage_page 1 accepts writes, usage_page 12 rejects every
            # one with IOHIDDeviceSetReport failed. Opening by VID/PID used to
            # pick whichever hidapi happened to return first, which worked by
            # luck. Choose deliberately.
            matched.sort(key=lambda d: (d.get("usage_page") != PROTOCOL_USAGE_PAGE,
                                        d.get("interface_number") or 0))
            serials = {d.get("serial_number") for d in matched}
            if len(serials) > 1:
                sys.exit(
                    f"{len(serials)} distinct devices match {self.spec['name']}; "
                    f"unplug one -- this tool cannot tell which you meant."
                )
            return matched[0].get("path")
        seen = [d.get("product_string") for d in entries]
        sys.exit(
            f"refusing to drive {self.spec['name']}: attached device on "
            f"{self.spec['vid']:#06x}/{self.spec['pid']:#06x} reports "
            f"{seen!r}, which is not {list(want)}.\n"
            f"That PID is shared by the DX1 II and E50 II, whose registers "
            f"collide with this map and mean different things."
        )

    def require_writable(self):
        """Refuse an unverified model BEFORE any I/O happens.

        send() also enforces this, but several commands read from the device
        first -- `vol` reads volumeStep to derive the dB scale -- so relying on
        send() alone surfaced a confusing "device returned no settings records"
        instead of the actual reason. Measured on a D90 III, 2026-08-28.
        """
        if self.dry_run or self.allow_unverified:
            return
        if self.spec.get("status") != "confirmed":
            self._refuse_unverified()

    def _refuse_unverified(self):
        sys.exit(
            f"refusing to write to {self.spec['name']}: its register map is "
            f"{self.spec.get('status', 'unknown')}, not confirmed.\n"
            "Six Topping models share USB 0x152a/0x8750 and their registers "
            "collide -- 0x7113 is HomePage on one and BluetoothMode on "
            "another -- so an unconfirmed map can write a real value to the "
            "wrong setting and report success.\n"
            "Read and --dry-run work without this flag; that is how you "
            "verify. When a write moves the FRONT PANEL as expected, mark the "
            "entry confirmed. To proceed anyway: --unverified"
        )

    def _wire(self, f):
        """Put the frame on the wire the way THIS model expects it.

        hidapi's write() takes byte 0 as the REPORT ID. Captured from the
        vendor's own Topping Tune driving a D90 III (2026-08-28), every frame
        goes out as report id 0 followed by a 15-byte payload ending 66 77:

            id=0  22 33 20 01 01 91 02 00 00 00 14 00 00 66 77

        frame() builds 16 bytes with a trailing 00 instead, so writing it
        directly makes hidapi read 0x22 as the report id and ship the rest
        shifted by one byte. The DX5 II tolerates that; the D90 III silently
        drops it -- which is the entire "accepted with no effect" mystery.

        Per-device rather than fixed globally: the DX5 II path is
        hardware-confirmed as it stands, and changing its framing is a separate
        change needing its own verification.
        """
        if self.spec.get("report_id_prefix"):
            return bytes([0x00]) + f[:15]
        return f

    def send(self, f, label=""):
        # Second line of defence: a command that forgets require_writable()
        # still cannot reach hardware.
        if not self.dry_run and self.spec.get("status") != "confirmed" \
                and not self.allow_unverified:
            self._refuse_unverified()
        if self.dry_run:
            print(f"  {f.hex(' ')}  {label}")
            return
        self.h.write(self._wire(f))
        time.sleep(0.004)      # the vendor app paces writes; don't flood the DSP

    def commit(self):
        self.send(frame(REG_CTRL, SUB_COMMIT, 1), "commit")

    def close(self):
        if self.h:
            self.h.close()


# --- state cache ------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"bands": [dict(DEFAULT_BAND) for _ in range(BAND_COUNT)],
                "volume_db": None, "gain": None, "source": "defaults (no cache yet)"}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=2)


# --- preset parsing ---------------------------------------------------------

AUTOEQ_FILTER = re.compile(
    r"^Filter\s+\d+:\s+(ON|OFF)\s+(\w+)\s+Fc\s+([\d.]+)\s*Hz\s+"
    r"Gain\s+(-?[\d.]+)\s*dB\s+Q\s+([\d.]+)", re.I)
AUTOEQ_PREAMP = re.compile(r"^Preamp:\s*(-?[\d.]+)\s*dB", re.I)


def parse_autoeq(text):
    """Parse AutoEQ / oratory1990 'ParametricEQ.txt' format.

    Returns (bands, preamp_db, skipped). Only PK / LS / HS are supported —
    those are the three filter types confirmed on this device. Anything else is
    reported rather than silently dropped, because a silently missing filter
    produces a wrong curve that sounds plausible.
    """
    bands, preamp, skipped = [], None, []
    for line in text.splitlines():
        line = line.strip()
        m = AUTOEQ_PREAMP.match(line)
        if m:
            preamp = float(m.group(1))
            continue
        m = AUTOEQ_FILTER.match(line)
        if not m:
            continue
        on, ftype, fc, gain, q = m.groups()
        ftype = ftype.upper()
        if ftype not in FILTER_TYPES:
            skipped.append(f"{ftype} @ {fc} Hz")
            continue
        bands.append({"type": ftype, "freq": float(fc), "gain": float(gain),
                      "q": float(q), "on": on.upper() == "ON"})
    return bands, preamp, skipped


def load_preset(path):
    text = open(path).read()
    if path.lower().endswith(".json"):
        data = json.loads(text)
        return data.get("bands", data), data.get("preamp_db"), []
    return parse_autoeq(text)


def validate(bands, max_bands=BAND_COUNT):
    """Reject nonsense before it reaches the DSP.

    max_bands is per-device: a model's usable band count is a measured property,
    not a constant. Defaults to the DX5 II's 10 so existing callers are
    unchanged.
    """
    errs = []
    if len(bands) > max_bands:
        errs.append(f"{len(bands)} filters but this device has {max_bands} usable bands")
    for i, b in enumerate(bands, 1):
        if b["type"] not in FILTER_TYPES:
            errs.append(f"filter {i}: unsupported type {b['type']}")
        if not 10 <= b["freq"] <= 22000:
            errs.append(f"filter {i}: frequency {b['freq']} Hz out of range")
        if not -40 <= b["gain"] <= 40:
            errs.append(f"filter {i}: gain {b['gain']} dB out of range")
        if not 0.01 <= b["q"] <= 100:
            errs.append(f"filter {i}: Q {b['q']} out of range")
    return errs


# --- commands ---------------------------------------------------------------

def assert_writable(args):
    """Refuse an unverified model before any I/O at all.

    Must be spec-level, not Device-level: cmd_vol deliberately reads
    volumeStep *before* constructing a Device (so two handles are never open
    at once), so a check on the Device object runs too late and the user sees
    "device returned no settings records" instead of the real reason.
    Measured against a D90 III, 2026-08-28.
    """
    if getattr(args, "dry_run", False) or getattr(args, "unverified", False):
        return
    spec = DEVICES.get(getattr(args, "device", None) or "dx5ii")
    if spec and spec.get("status") != "confirmed":
        sys.exit(
            f"refusing to write to {spec['name']}: its register map is "
            f"{spec.get('status', 'unknown')}, not confirmed.\n"
            "Six Topping models share USB 0x152a/0x8750 and their registers "
            "collide -- 0x7113 is HomePage on one and BluetoothMode on "
            "another -- so an unconfirmed map can write a real value to the "
            "wrong setting and report success.\n"
            "Reads and --dry-run work without this flag; that is how you "
            "verify. When a write moves the FRONT PANEL as expected, mark the "
            "entry confirmed. To proceed anyway: --unverified"
        )


def cmd_apply(args):
    assert_writable(args)
    bands, preamp, skipped = load_preset(args.file)
    if not bands:
        sys.exit(f"no usable filters found in {args.file}")
    # Resolve the band count for THIS device before validating against it.
    # Exits if the model's band count was never established, rather than
    # silently validating against the DX5 II's 10.
    errs = validate(bands, band_count(DEVICES[getattr(args, "device", None) or "dx5ii"]))
    if errs:
        sys.exit("preset rejected:\n  " + "\n  ".join(errs))

    # Pad to REG_COUNT, not BAND_COUNT: band 11 is inert but still gets an
    # explicit disable so nothing stale survives underneath the preset.
    padded = bands + [dict(DEFAULT_BAND) for _ in range(REG_COUNT - len(bands))]

    print(f"applying {len(bands)} filter(s) from {os.path.basename(args.file)}")
    for i, b in enumerate(bands, 1):
        state = "" if b["on"] else "  (off)"
        print(f"  {i:2d}. {b['type']}  {b['freq']:>7.0f} Hz  "
              f"{b['gain']:+5.1f} dB  Q {b['q']:.3f}{state}")
    if len(padded) > len(bands):
        print(f"  {len(padded) - len(bands)} unused band(s) disabled")

    if skipped:
        print(f"\n  SKIPPED unsupported filter types: {', '.join(skipped)}")
        print("  only PK / LS / HS are confirmed on this device — the applied")
        print("  curve will differ from the preset.")

    if preamp is not None:
        print(f"\n  preamp {preamp:+.1f} dB (raw 0x{db_to_q25(preamp):08X})")
    else:
        boost = max([b["gain"] for b in bands if b["on"]] or [0.0])
        if boost > 0:
            print(f"\n  WARNING: this preset boosts up to {boost:+.1f} dB and declares no preamp.")
            print("  Nothing is written to 0x9c, so the device keeps whatever preamp was set")
            print("  last -- which this tool cannot read back. Positive gain with no preamp")
            print(f"  can clip. Set one first:  ./toppingctl.py preamp {-abs(boost):.1f}")

    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    if preamp is not None:
        for f in preamp_frames(preamp):
            dev.send(f, f"preamp {preamp:+.1f}")
    for i, b in enumerate(padded):
        for f in band_frames(i, b):
            dev.send(f, f"band{i+1} {b['type']}")
    dev.commit()
    dev.close()

    if not args.dry_run:
        st = load_state()
        st["bands"] = padded
        if preamp is not None:
            st["preamp_db"] = preamp
        st["source"] = os.path.abspath(args.file)
        save_state(st)
        print(f"\napplied. {BAND_COUNT} bands written, committed.")
    else:
        print("\ndry run — nothing sent.")


def cmd_flat(args):
    assert_writable(args)
    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    for i in range(REG_COUNT):
        for f in band_frames(i, DEFAULT_BAND):
            dev.send(f, f"band{i+1} default")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["bands"] = [dict(DEFAULT_BAND) for _ in range(BAND_COUNT)]
        st["source"] = "flat"
        save_state(st)
        print(f"all {BAND_COUNT} bands disabled.")


def cmd_preamp(args):
    assert_writable(args)
    db = args.db
    if not -40.0 <= db <= 10.0:
        sys.exit(f"preamp {db} dB out of range (-40..+10)")
    v = db_to_q25(db)
    if db > 0:
        print(f"  warning: positive preamp ({db:+.1f} dB) can clip. AutoEQ presets"
              f" are always negative.")
    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    for f in preamp_frames(db):
        dev.send(f, f"preamp {db:+.1f} dB")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["preamp_db"] = db
        save_state(st)
        print(f"preamp {db:+.1f} dB  (raw {v} = 0x{v:08X})")


def cmd_vol(args):
    assert_writable(args)
    db = args.db
    if not VOL_MIN_DB <= db <= VOL_MAX_DB:
        sys.exit(f"volume {db} dB out of range ({VOL_MIN_DB}..{VOL_MAX_DB})")
    # The raw unit is NOT fixed at half a dB: volumeStep (settings field 32)
    # selects it. Measured on a DX5 II against the front panel, 2026-08-27:
    #   half_db: raw 60 -> -30.0 dB          one_db: raw 25 -> -25.0 dB
    # Assuming 0.5 while the device is on one_db sends TWICE the attenuation
    # asked for -- `vol -30` lands at -60 dB. The error is always in the quiet
    # direction, so it is not dangerous, but it is wrong and it makes the
    # --force guard fire for levels that are not actually loud.
    # Read the scale FIRST, and let that read close its own handle before the
    # writer opens one. read_settings() calls open_checked() internally, so
    # holding a Device open across it would mean two handles on a device that
    # only grants one -- and it takes a device KEY, not a Device instance.
    key = getattr(args, "device", None) or "dx5ii"
    step_db = 0.5
    if getattr(args, "vol_step", None):
        # Explicit override. Needed because the settings READ is model-specific
        # and can fail on a device whose WRITE path may still be fine: a D90 III
        # returns no settings records to the DX5 II read protocol (measured
        # 2026-08-28), which blocked the very write test that would establish
        # whether its register map matches. Its manual documents the step
        # directly -- fixed 0.5 dB above -50 dB -- so the number does not have
        # to be discovered by reading the device.
        step_db = float(args.vol_step)
    elif not args.dry_run:
        try:
            import devstate  # local: devstate imports this module
            step_db = 1.0 if devstate.read_settings(key)[32] == 1 else 0.5
        except Exception as e:                      # noqa: BLE001 - deliberate
            # Refuse rather than guess: a wrong scale silently doubles or
            # halves every level on a headphone amp.
            sys.exit(
                f"cannot read volumeStep to determine the dB scale: {e}\n"
                "If this model's step is documented, pass it explicitly: "
                "--vol-step 0.5"
            )
    steps = int(round(-db / step_db))
    actual = -steps * step_db
    # Guard before anything is opened or sent.
    if db > VOL_WARN_DB and not args.force:
        sys.exit(f"{actual:+.1f} dB is loud — re-run with --force if you mean it")
    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    dev.send(frame(REG_CTRL, SUB_VOLUME, steps),
             f"volume {actual:+.1f} dB ({step_db} dB/step)")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["volume_db"] = actual
        save_state(st)
        print(f"volume {actual:+.1f} dB  (raw {steps})")


def cmd_gain(args):
    assert_writable(args)
    on = args.state == "on"
    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    dev.send(frame(REG_CTRL, SUB_GAIN, 1 if on else 0), f"gain {args.state}")
    dev.commit()
    dev.close()
    if not args.dry_run:
        st = load_state()
        st["gain"] = on
        save_state(st)
        print(f"gain {args.state}")
        print("note: the vendor app only exposes gain on headphone outputs.")


def cmd_power(args):
    assert_writable(args)
    on = args.state == "on"
    dev = Device(args.dry_run, getattr(args, "device", None),
                 allow_unverified=getattr(args, "unverified", False))
    dev.send(frame(REG_CTRL, SUB_POWER, int(on), b4=SUB_POWER_B4, crc=True),
             f"power {args.state}")
    dev.close()
    if not args.dry_run:
        print(f"power {args.state}")


def cmd_show(args):
    st = load_state()
    print(f"last written by toppingctl (source: {st.get('source')})")
    print("the device cannot be queried — this is a cache, not a read.\n")
    if st.get("preamp_db") is not None:
        print(f"  preamp  {st['preamp_db']:+.1f} dB")
    if st.get("volume_db") is not None:
        print(f"  volume  {st['volume_db']:+.1f} dB")
    if st.get("gain") is not None:
        print(f"  gain    {'on' if st['gain'] else 'off'}")
    print()
    active = 0
    for i, b in enumerate(st["bands"], 1):
        if not b["on"]:
            continue
        active += 1
        print(f"  band {i:2d}  {b['type']}  {b['freq']:>7.0f} Hz  "
              f"{b['gain']:+5.1f} dB  Q {b['q']:.3f}")
    print(f"  ({active} of {BAND_COUNT} bands active)")


def cmd_dump(args):
    st = load_state()
    out = json.dumps({"bands": st["bands"], "volume_db": st.get("volume_db"),
                      "gain": st.get("gain")}, indent=2)
    if args.file:
        open(args.file, "w").write(out + "\n")
        print(f"wrote {args.file}")
    else:
        print(out)


def cmd_devices(args):
    """List attached devices so an unknown model's PID can be discovered."""
    found = find_devices()
    if not found:
        print(f"no HID devices with VID {THESYCON_VID:#06x} attached.")
        print("that VID is Thesycon's; a Topping DAC should appear here when plugged in.")
        return
    for f in found:
        mark = f"known: {f['key']}" if f["known"] else "UNKNOWN -- see README"
        print(f"  pid={f['pid']:#06x}  {f['product']:<40} {mark}")
    if any(not f["known"] for f in found):
        print("\nAn unknown device is NOT assumed compatible. Adding it to DEVICES is a")
        print("claim that its register map matches, which only testing can establish.")


# --- cli --------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        prog="toppingctl",
        description="Local control for Topping DACs over USB HID.")
    p.add_argument("--device", default="dx5ii", choices=sorted(DEVICES),
                   help="which model to talk to (default: dx5ii)")
    p.add_argument("--vol-step", choices=("0.5", "1.0"),
                   help="dB per raw volume step, instead of reading it from "
                        "the device. For models whose settings read is not "
                        "supported but whose step is documented.")
    p.add_argument("--unverified", action="store_true",
                   help="allow writes to a device whose register map is "
                        "unverified. You are asserting you will watch the "
                        "hardware and confirm it did the right thing.")
    p.add_argument("--dry-run", action="store_true",
                   help="print frames instead of sending them")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("devices", help="list attached Thesycon-VID HID devices")
    a.set_defaults(func=cmd_devices)

    a = sub.add_parser("apply", help="write a PEQ preset (AutoEQ .txt or .json)")
    a.add_argument("file")
    a.set_defaults(func=cmd_apply)

    a = sub.add_parser("flat", help="disable all bands")
    a.set_defaults(func=cmd_flat)

    a = sub.add_parser("preamp", help="set PEQ preamp in dB, e.g. -6.7")
    a.add_argument("db", type=float)
    a.set_defaults(func=cmd_preamp)

    a = sub.add_parser("vol", help="set volume in dB, e.g. -30")
    a.add_argument("db", type=float)
    a.add_argument("--force", action="store_true", help="allow levels above -10 dB")
    a.set_defaults(func=cmd_vol)

    a = sub.add_parser("gain", help="headphone gain")
    a.add_argument("state", choices=["on", "off"])
    a.set_defaults(func=cmd_gain)

    a = sub.add_parser("power", help="wake or sleep the device")
    a.add_argument("state", choices=["on", "off"])
    a.set_defaults(func=cmd_power)

    a = sub.add_parser("show", help="print last-written state")
    a.set_defaults(func=cmd_show)

    a = sub.add_parser("dump", help="write last-written state as JSON")
    a.add_argument("file", nargs="?")
    a.set_defaults(func=cmd_dump)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
