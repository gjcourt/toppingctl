#!/usr/bin/env python3
"""Regression tests for the PR #16 review follow-ups.

Not wired into CI: the repo has no test framework and adding a workflow
step needs a change to .github/workflows/ci.yml, which is George's to make.
Run directly: ./test_dx1ii_review_followups.py -- everything here is offline,
same as --dry-run.
"""
import argparse
import ast
import unittest

import toppingctl


def load_dx1_show():
    """readsettings.py reads the device at import time (argparse + devstate
    calls at module level), so it can't be imported here. Exec just the
    imports and function/class defs -- skips the module-level read -- to get
    at dx1_show() without touching hardware."""
    with open("readsettings.py") as fh:
        tree = ast.parse(fh.read())
    keep = [n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef))]
    ns = {"__name__": "readsettings_under_test"}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "readsettings.py", "exec"), ns)
    return ns["dx1_show"]


class EqRouteDecodeTest(unittest.TestCase):
    """s >> 6 & 3 is a 2-bit field: 0..3. ('analog', 'opt', 'both') only
    covers 0..2, so a device reporting 0b11 raised IndexError."""

    def test_0b11_no_longer_raises(self):
        dx1_show = load_dx1_show()
        st = {"block": {2: 0b11000011}, "regs": {}, "configs": []}
        dx1_show(st)  # would raise IndexError before the fix


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


class CmdFlatBandCountTest(unittest.TestCase):
    """cmd_flat's cache write used to size against the module-level
    BAND_COUNT constant instead of the per-device band_count(spec) the rest
    of the file uses."""

    def setUp(self):
        self.saved = None
        self._device, toppingctl.Device = toppingctl.Device, FakeDevice
        self._load, toppingctl.load_state = toppingctl.load_state, lambda: {}
        self._save, toppingctl.save_state = toppingctl.save_state, self._capture
        self._assert, toppingctl.assert_writable = toppingctl.assert_writable, lambda args: None

    def tearDown(self):
        toppingctl.Device = self._device
        toppingctl.load_state = self._load
        toppingctl.save_state = self._save
        toppingctl.assert_writable = self._assert

    def _capture(self, st):
        self.saved = st

    def _run_flat(self, device):
        args = argparse.Namespace(device=device, dry_run=False, unverified=True)
        toppingctl.cmd_flat(args)

    def test_dx5ii_cached_band_count_matches_spec(self):
        self._run_flat("dx5ii")
        spec = toppingctl.DEVICES["dx5ii"]
        self.assertEqual(len(self.saved["bands"]), toppingctl.band_count(spec))

    def test_dx1ii_cached_band_count_matches_spec(self):
        self._run_flat("dx1ii")
        spec = toppingctl.DEVICES["dx1ii"]
        self.assertEqual(len(self.saved["bands"]), toppingctl.band_count(spec))


if __name__ == "__main__":
    unittest.main()
