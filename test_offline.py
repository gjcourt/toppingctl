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
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import toppingctl  # noqa: E402


def load_dx1_show():
    """readsettings.py reads the device at import time (argparse and devstate
    calls at module level), so it can't be imported here. Exec only its
    imports and function/class defs to get at dx1_show() without hardware."""
    with open(os.path.join(HERE, "readsettings.py")) as fh:
        tree = ast.parse(fh.read())
    keep = [n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))]
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


if __name__ == "__main__":
    unittest.main()
