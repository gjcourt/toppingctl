#!/usr/bin/env python3
"""Offline regression tests -- no hardware, same as --dry-run.

CI runs these on every matrix interpreter (.github/workflows/ci.yml,
"Offline regression tests"), alongside ruff and the dry-run commands. Run
them locally from anywhere with `hid` importable (toppingctl imports it):

    python3 -m unittest test_offline      # from the repo root
    ./test_offline.py
"""

import argparse
import ast
import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import devstate  # noqa: E402
import toppingctl  # noqa: E402


def load_dx1_show():
    """readsettings.py reads the device at import time (argparse and devstate
    calls at module level), so it can't be imported here. Exec only its
    imports and function/class defs to get at dx1_show() without hardware."""
    with open(os.path.join(HERE, "readsettings.py")) as fh:
        tree = ast.parse(fh.read())
    keep = [
        n
        for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))
    ]
    ns = {"__name__": "readsettings_under_test"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "readsettings.py", "exec"), ns)
    return ns["dx1_show"]


def run_quietly(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args)
    return out.getvalue()


class EqRouteDecodeTest(unittest.TestCase):
    """The eq route is a 2-bit field (s >> 6 & 3), so it can read 0..3."""

    def decode(self, route):
        st = {"block": {2: route << 6}, "regs": {}, "configs": []}
        out = run_quietly(load_dx1_show(), st)
        return next(line for line in out.splitlines() if "eq route" in line)

    def test_known_values(self):
        for route, label in ((0, "analog"), (1, "opt"), (2, "both")):
            self.assertTrue(self.decode(route).endswith(f"eq route {label}"))

    def test_0b11_decodes_instead_of_raising(self):
        self.assertTrue(self.decode(0b11).endswith("eq route unknown (0b11)"))


class FakeDevice:
    """Stands in for toppingctl.Device: records frames, opens no hardware."""

    def __init__(self, *a, **kw):
        self.sent = []

    def send(self, frame, label=None):
        self.sent.append((frame, label))

    def commit(self):
        pass

    def close(self):
        pass


class PerDeviceBandCountTest(unittest.TestCase):
    """flat and show size bands per device via band_count(spec), not the
    module-level BAND_COUNT. Every real model has 10 today, the same as
    BAND_COUNT, so these use a fake 7-band model to tell the two apart."""

    def setUp(self):
        self.saved = None
        self.cache = {}
        fake = dict(toppingctl.DEVICES["dx5ii"], name="Fake7", bands=7)
        unset = dict(toppingctl.DEVICES["dx5ii"], name="FakeUnset", bands=None)
        patches = [
            mock.patch.dict(toppingctl.DEVICES, {"fake7": fake, "fakeunset": unset}),
            mock.patch.object(toppingctl, "Device", FakeDevice),
            mock.patch.object(toppingctl, "load_state", lambda: dict(self.cache)),
            mock.patch.object(toppingctl, "save_state", self._capture),
            mock.patch.object(toppingctl, "assert_writable", lambda args: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _capture(self, st):
        self.saved = st

    def args(self, device, **kw):
        return argparse.Namespace(device=device, dry_run=False, unverified=True, **kw)

    def test_flat_caches_the_devices_band_count(self):
        out = run_quietly(toppingctl.cmd_flat, self.args("fake7"))
        self.assertEqual(len(self.saved["bands"]), 7)
        self.assertIn("all 7 bands disabled.", out)

    def test_show_uses_the_devices_band_count(self):
        band = dict(toppingctl.DEFAULT_BAND, on=True)
        self.cache = {"bands": [dict(band) for _ in range(11)], "source": "test"}
        out = run_quietly(toppingctl.cmd_show, self.args("fake7"))
        self.assertIn("(7 of 7 bands active)", out)
        self.assertNotIn("band  8", out)

    def test_show_refuses_before_printing_when_band_count_unset(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            toppingctl.cmd_show(self.args("fakeunset"))
        self.assertIn("band count is not established", str(cm.exception.code))
        self.assertEqual(out.getvalue(), "")


def _make_fake_hid(writes):
    """A `hid.Device` stand-in that records every write() instead of touching
    hardware. `writes` is a plain list the caller inspects afterwards."""

    class _FakeHidHandle:
        def __init__(self, *a, **kw):
            pass

        def write(self, data):
            writes.append(bytes(data))
            return len(data)

        def read(self, *a, **kw):
            return b""

        def close(self):
            pass

    return _FakeHidHandle


def _fake_enumerate(product_string):
    def enumerate_(*a, **kw):
        return [{"product_string": product_string, "path": b"/fake/dx1ii"}]

    return enumerate_


class Dx1iiWriteGateTest(unittest.TestCase):
    """Review #16: the DX1 II entry must start "unverified" -- it is the write
    gate, not a label. "confirmed" would let setctl.py and probe.py send DX5
    II registers to it with no guard, and neither knows the dx1 protocol
    exists."""

    def test_dx1ii_status_is_unverified(self):
        self.assertEqual(toppingctl.DEVICES["dx1ii"]["status"], "unverified")

    def test_write_refused_without_unverified_flag(self):
        args = argparse.Namespace(device="dx1ii", dry_run=False, unverified=False)
        with self.assertRaises(SystemExit) as cm:
            toppingctl.assert_writable(args)
        self.assertIn("unverified", str(cm.exception.code))

    def test_dry_run_and_unverified_flag_both_bypass_the_gate(self):
        toppingctl.assert_writable(
            argparse.Namespace(device="dx1ii", dry_run=True, unverified=False)
        )
        toppingctl.assert_writable(
            argparse.Namespace(device="dx1ii", dry_run=False, unverified=True)
        )


class EqDryRunNeverTouchesHardwareTest(unittest.TestCase):
    """Review #16: `--dry-run eq on` used to open the device and write before
    `Device(args.dry_run, ...)` existed. Every other command only prints
    frames under --dry-run; eq must too."""

    def setUp(self):
        patches = [
            mock.patch.object(
                toppingctl.hid, "Device", side_effect=AssertionError("opened hardware")
            ),
            mock.patch.object(
                toppingctl.hid, "enumerate", side_effect=AssertionError("enumerated hardware")
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def args(self, state):
        return argparse.Namespace(device="dx1ii", dry_run=True, state=state, unverified=False)

    def test_eq_on_dry_run_opens_nothing(self):
        out = run_quietly(toppingctl.cmd_eq, self.args("on"))
        self.assertIn("eq on (dry-run assumes slot 1; live run reads 0x1206)", out)

    def test_eq_off_dry_run_opens_nothing(self):
        out = run_quietly(toppingctl.cmd_eq, self.args("off"))
        self.assertIn("eq off", out)


class Dx1iiLiveWriteUsesWireTest(unittest.TestCase):
    """Review #16: a live dx1 write must go through Device._wire() so the
    report-id-0 prefix this firmware requires (report_id_prefix: True) is
    actually applied, rather than writing the bare frame directly."""

    def setUp(self):
        self.writes = []
        patches = [
            mock.patch.object(toppingctl.hid, "Device", _make_fake_hid(self.writes)),
            mock.patch.object(toppingctl.hid, "enumerate", _fake_enumerate("DX1II")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_eq_off_write_is_report_id_prefixed(self):
        args = argparse.Namespace(device="dx1ii", dry_run=False, state="off", unverified=True)
        run_quietly(toppingctl.cmd_eq, args)
        self.assertEqual(len(self.writes), 1)
        written = self.writes[0]
        self.assertEqual(len(written), 16)
        self.assertEqual(written[0], 0x00, "report id byte missing -- _wire() was bypassed")
        # EQ off writes 0xffffffff to 0x110e (DX1_REG_SWITCH_CFG), big-endian,
        # at f[7:11] -- shifted to written[8:12] by the report-id prefix.
        self.assertEqual(int.from_bytes(written[8:12], "big"), 0xFFFFFFFF)


class MuteBitmaskTest(unittest.TestCase):
    """Review #16: frame 7 of the 0x810a block is a bitmask (bit0 = analog
    muted, bit1 = opt muted). The fix replaced a 1/0 on/off write -- which
    would have cleared bit1 -- with four explicit, named target states that
    each write the FULL desired bitmask, not a toggle of one bit over a
    stale read of the other."""

    def setUp(self):
        self.writes = []
        patches = [
            mock.patch.object(toppingctl.hid, "Device", _make_fake_hid(self.writes)),
            mock.patch.object(toppingctl.hid, "enumerate", _fake_enumerate("DX1II")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def mute(self, state):
        self.writes.clear()
        args = argparse.Namespace(device="dx1ii", dry_run=False, state=state, unverified=True)
        run_quietly(toppingctl.cmd_mute, args)
        written = self.writes[0]
        self.assertEqual(written[5], toppingctl.DX1_F_MUTE, "wrong block frame")
        return int.from_bytes(written[8:12], "big")

    def test_off_clears_both_bits(self):
        self.assertEqual(self.mute("off"), 0b00)

    def test_analog_sets_only_bit0(self):
        self.assertEqual(self.mute("analog"), 0b01)

    def test_opt_sets_only_bit1(self):
        self.assertEqual(self.mute("opt"), 0b10)

    def test_both_sets_both_bits(self):
        self.assertEqual(self.mute("both"), 0b11)

    def test_no_longer_offers_an_ambiguous_on(self):
        # The original bug was reachable only through a bare "on" state that
        # could not say which output(s) it meant.
        self.assertNotIn("on", toppingctl.DX1_MUTE)


class Dx1106ReadSafeListConsistencyTest(unittest.TestCase):
    """Review #16: a readNack to a dx1 register outside the vendor's query
    list is treated as a WRITE of the data field (devstate.dx1_query's
    docstring). 0x1106, the 3-slot PEQ config dump, must be documented as
    read-safe consistently in both devstate.py and toppingctl.py, or the two
    files silently disagree about which registers are safe to probe."""

    REGISTER_RE = re.compile(r"0x[0-9A-Fa-f]{4}")

    def _toppingctl_dx1ii_safe_set_comment(self):
        with open(os.path.join(HERE, "toppingctl.py")) as fh:
            src = fh.read()
        start = src.index("READS ARE WRITE-ONLY-ADJACENT HERE")
        end = src.index("never probed with readNack.", start)
        return src[start:end]

    def test_0x1106_is_listed_as_read_safe_in_devstate(self):
        self.assertIn("0x1106", devstate.dx1_query.__doc__)

    def test_0x1106_is_listed_as_read_safe_in_toppingctl(self):
        self.assertIn("0x1106", self._toppingctl_dx1ii_safe_set_comment())

    def test_devstate_and_toppingctl_agree_on_the_safe_set(self):
        devstate_regs = set(self.REGISTER_RE.findall(devstate.dx1_query.__doc__))
        toppingctl_regs = set(self.REGISTER_RE.findall(self._toppingctl_dx1ii_safe_set_comment()))
        self.assertEqual(devstate_regs, toppingctl_regs)


if __name__ == "__main__":
    unittest.main()
