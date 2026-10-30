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
import functools
import io
import json
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
            mock.patch.dict(
                toppingctl.CONFIRMED_WRITES, {"fake7": toppingctl.CONFIRMED_WRITES["dx5ii"]}
            ),
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


class Dx1ReadSafeListTest(unittest.TestCase):
    """Review #16 / #28: a readNack to a dx1 register outside the vendor's
    query list is treated as a WRITE of the data field (devstate.dx1_query's
    docstring). The list used to live only in that docstring and inline in
    dx1_state; it is a constant now, and every reader goes through it."""

    def test_confirmed_reads_is_the_read_list_minus_0x1106(self):
        self.assertEqual(
            set(toppingctl.DX1_READ_REGS) - {(0x11, 0x06)},
            toppingctl.CONFIRMED_READS["dx1ii"],
        )

    def test_0x1106_is_read_but_not_confirmed(self):
        self.assertEqual(toppingctl.DX1_REG_PEQ_DUMP, (0x11, 0x06))
        self.assertIn((0x11, 0x06), toppingctl.DX1_READ_REGS)
        self.assertNotIn((0x11, 0x06), toppingctl.DX1_VENDOR_READ_SAFE)
        self.assertNotIn((0x11, 0x06), toppingctl.CONFIRMED_READS["dx1ii"])

    def test_dx1_state_queries_only_vendor_listed_registers(self):
        self.assertLessEqual(set(devstate.DX1_STATE_QUERIES), set(toppingctl.DX1_VENDOR_READ_SAFE))

    def test_dx1_state_sends_readnacks_only_to_the_read_list(self):
        writes = []
        handle = _make_fake_hid(writes)()
        with (
            mock.patch.object(devstate, "open_checked", lambda key: handle),
            mock.patch.object(devstate, "_frames", lambda h, secs: []),
        ):
            devstate.dx1_state("dx1ii")
        sent = {(w[6], w[7]) for w in writes}  # report-id prefixed: reg/sub at 6/7
        self.assertTrue(all(w[3] == 0x10 for w in writes), "dx1_state sent a non-readNack")
        self.assertLessEqual(sent, set(toppingctl.DX1_READ_REGS))
        # The block comes from dx1_read_block, the rest from dx1_query, 0x1106
        # from dx1_read_configs.
        self.assertEqual(
            sent,
            set(devstate.DX1_STATE_QUERIES) | {(0x81, 0x0A), toppingctl.DX1_REG_PEQ_DUMP},
        )

    def test_dx1_query_refuses_an_unlisted_register_before_writing(self):
        # Guard: 0x7500 is high gain -- the register a "read" sweep once reset.
        writes = []
        with self.assertRaises(toppingctl.Refused):
            devstate.dx1_query(_make_fake_hid(writes)(), *toppingctl.DX1_REG_HIGH_GAIN)
        self.assertEqual(writes, [])


def _vendor_reg(name):
    """(reg, sub) for a vendor command name, via vendor_commands."""
    from vendor_commands import COMMANDS

    (code,) = [c for c, n in COMMANDS.items() if n == name]
    return (code >> 8, code & 0xFF)


class ConfirmedWritesTableTest(unittest.TestCase):
    """CONFIRMED_WRITES is the spec's confirmed tables, and only those."""

    # Decision 1 and Q15 (TOPPINGCTL-2), plus the NEVER set in listen.py.
    ABSENT = (
        "InputType",
        "OutputType",
        "Mute",
        "CallC1",
        "CallC2",
        "SaveC1",
        "SaveC2",
        "FactoryReset",
        "FirmwareUpdate",
    )

    def test_every_model_has_an_entry_in_both_tables(self):
        self.assertEqual(set(toppingctl.CONFIRMED_WRITES), set(toppingctl.DEVICES))
        self.assertEqual(set(toppingctl.CONFIRMED_READS), set(toppingctl.DEVICES))

    def test_absent_registers_are_absent_for_every_model(self):
        import listen

        self.assertLessEqual(listen.NEVER, set(self.ABSENT))
        for key, allowed in toppingctl.CONFIRMED_WRITES.items():
            for name in self.ABSENT:
                with self.subTest(model=key, register=name):
                    self.assertNotIn(_vendor_reg(name), allowed)

    def test_unconfirmed_models_get_nothing(self):
        for key, spec in toppingctl.DEVICES.items():
            if spec["status"] != "confirmed":
                self.assertEqual(toppingctl.CONFIRMED_WRITES[key], frozenset(), key)

    def test_dx5ii_table_matches_the_plan(self):
        expected = {(0x71, 0x02), (0x71, 0x17), (0x71, 0x01), (0x71, 0x34)}
        expected |= {(r, s) for r in range(0x91, 0x9C) for s in range(0x01, 0x0B)}
        expected |= {(0x9C, s) for s in range(0x01, 0x05)}
        self.assertEqual(toppingctl.CONFIRMED_WRITES["dx5ii"], expected)

    def test_dx5ii_reads_are_get_settings(self):
        self.assertEqual(toppingctl.CONFIRMED_READS["dx5ii"], {devstate.GET_SETTINGS})
        self.assertEqual(devstate.GET_SETTINGS, (0x71, 0x0C))
        self.assertEqual(toppingctl.CONFIRMED_READS["d90iii"], frozenset())


class CheckConfirmedWritesTest(unittest.TestCase):
    """Guard: toppingctl's write commands refuse, on a confirmed model, any
    frame outside CONFIRMED_WRITES -- even one built by hand."""

    def test_refuses_each_absent_register_on_the_dx5ii(self):
        for name in ConfirmedWritesTableTest.ABSENT:
            with self.subTest(register=name), self.assertRaises(toppingctl.Refused):
                reg, sub = _vendor_reg(name)
                toppingctl.check_confirmed_writes(
                    "dx5ii", [toppingctl.Frame(toppingctl.frame(reg, sub, 1), name)]
                )

    def test_refuses_a_non_write_opcode_to_a_confirmed_register(self):
        f = toppingctl.Frame(toppingctl.frame(0x71, 0x02, 60, opcode=0x10), "readNack")
        with self.assertRaises(toppingctl.Refused):
            toppingctl.check_confirmed_writes("dx5ii", [f])

    def test_does_not_apply_to_an_unverified_model(self):
        # Those are gated by check_writable / --unverified instead.
        f = toppingctl.Frame(toppingctl.frame(*toppingctl.DX1_REG_INPUT, 1), "input")
        toppingctl.check_confirmed_writes("dx1ii", [f])

    def test_cmd_refuses_and_sends_nothing_when_the_table_lacks_a_frame(self):
        sent = []

        class Recorder(FakeDevice):
            def send(self, frame, label=None):
                sent.append(frame)

        table = toppingctl.CONFIRMED_WRITES["dx5ii"] - {(0x71, 0x17)}
        args = argparse.Namespace(device="dx5ii", dry_run=True, unverified=False, state="on")
        with (
            mock.patch.dict(toppingctl.CONFIRMED_WRITES, {"dx5ii": table}),
            mock.patch.object(toppingctl, "Device", Recorder),
            self.assertRaises(SystemExit) as cm,
        ):
            run_quietly(toppingctl.cmd_gain, args)
        self.assertIn("not in CONFIRMED_WRITES", str(cm.exception.code))
        self.assertEqual(sent, [])


class GuardTest(unittest.TestCase):
    """One test per guard in the plan_* layer. Each raises Refused -- not
    SystemExit, so a long-running caller can catch it as an Exception."""

    DX5 = toppingctl.DEVICES["dx5ii"]
    DX1 = toppingctl.DEVICES["dx1ii"]

    def test_refused_is_an_ordinary_exception(self):
        self.assertTrue(issubclass(toppingctl.Refused, Exception))
        self.assertFalse(issubclass(toppingctl.Refused, SystemExit))

    def test_unverified_model(self):
        with self.assertRaisesRegex(toppingctl.Refused, "not confirmed"):
            toppingctl.check_writable(toppingctl.DEVICES["d90iii"])
        toppingctl.check_writable(toppingctl.DEVICES["d90iii"], allow_unverified=True)
        toppingctl.check_writable(self.DX5)

    def test_device_send_refuses_an_unverified_model_live(self):
        dev = toppingctl.Device.__new__(toppingctl.Device)
        dev.spec, dev.dry_run, dev.allow_unverified, dev.h = self.DX1, False, False, None
        with self.assertRaises(SystemExit) as cm:
            dev.send(toppingctl.frame(0x71, 0x02, 60))
        self.assertIn("not confirmed", str(cm.exception.code))

    def test_volume_range(self):
        for db in (0.5, -99.5):
            with self.subTest(db=db), self.assertRaisesRegex(toppingctl.Refused, "out of range"):
                toppingctl.plan_volume(self.DX5, db, 0.5, allow_loud=True)

    def test_loud_volume_needs_allow_loud(self):
        for spec in (self.DX5, self.DX1):
            with self.subTest(model=spec["name"]):
                with self.assertRaisesRegex(toppingctl.Refused, "is loud"):
                    toppingctl.plan_volume(spec, -9.5, 0.5, allow_loud=False)
                self.assertTrue(toppingctl.plan_volume(spec, -9.5, 0.5, allow_loud=True))
                self.assertTrue(toppingctl.plan_volume(spec, -10.0, 0.5, allow_loud=False))

    def test_loud_check_covers_the_quantised_level(self):
        # Rounding never lifts a level <= -10 dB above it today; the check
        # makes that explicit instead of relying on round().
        with self.assertRaises(toppingctl.Refused):
            toppingctl.check_loud(-10.0, -9.5, allow_loud=False, shown=-9.5)
        for step in (0.5, 1.0):
            for tenth in range(100, 1000):
                db = -tenth / 10
                _, actual = toppingctl.volume_steps(db, step)
                self.assertLessEqual(actual, -10.0, (db, step))
                self.assertLessEqual(
                    toppingctl.dx1_raw_to_db(toppingctl.dx1_db_to_raw(db)), -10.0, db
                )

    def test_preamp_range(self):
        with self.assertRaisesRegex(toppingctl.Refused, "out of range"):
            toppingctl.plan_preamp(self.DX5, 10.5)

    def test_preset_validation(self):
        bad = [dict(toppingctl.DEFAULT_BAND, freq=5)]
        with self.assertRaisesRegex(toppingctl.Refused, "preset rejected"):
            toppingctl.plan_preset(self.DX5, bad)

    def test_unestablished_band_count(self):
        unset = dict(self.DX5, bands=None)
        for plan in (toppingctl.plan_flat, lambda s: toppingctl.plan_preset(s, [])):
            with self.assertRaisesRegex(toppingctl.Refused, "not established"):
                plan(unset)

    def test_dx1_only_commands_refuse_the_dx5ii(self):
        for plan, arg in (
            (toppingctl.plan_mute, "off"),
            (toppingctl.plan_input, "opt"),
            (toppingctl.plan_filter, "f3"),
            (toppingctl.plan_eq, "off"),
        ):
            with self.subTest(plan=plan.__name__), self.assertRaises(toppingctl.Refused):
                plan(self.DX5, arg)

    def test_filter_name(self):
        for name in ("f0", "f9", "x", "3"):
            with self.subTest(name=name), self.assertRaises(toppingctl.Refused):
                toppingctl.plan_filter(self.DX1, name)

    def test_eq_slot(self):
        with self.assertRaisesRegex(toppingctl.Refused, "slot number 1-3"):
            toppingctl.plan_eq(self.DX1, "4")

    def test_eq_on_uses_the_read_slot_and_clamps_nonsense(self):
        (f,) = toppingctl.plan_eq(self.DX1, "on", lambda: 2)
        self.assertEqual((f.data[10], f.label), (2, "eq on (slot 3)"))
        (f,) = toppingctl.plan_eq(self.DX1, "on", lambda: None)
        self.assertEqual((f.data[10], f.label), (0, "eq on (slot 1)"))


def _settings_record(index, value):
    """One GetSettings reply frame, as devstate.read_settings parses it."""
    return bytes([0x22, 0x33, 0x10, 0x01, index, 0x71, 0x0C]) + value.to_bytes(4, "big") + bytes(5)


class VolumeStepFromDeviceTest(unittest.TestCase):
    """--dry-run assumes 0.5 dB/step and never reads volumeStep, so the dry-run
    golden can't cover the live read. A fake device answers GetSettings with
    volumeStep (field 32) and records what the CLI writes back."""

    def run_vol(self, volume_step):
        writes = []
        reply = [_settings_record(32, volume_step)]

        class Handle(_make_fake_hid(writes)):
            def read(self, *a, **kw):
                return reply.pop() if reply else b""

        args = argparse.Namespace(
            device="dx5ii", dry_run=False, unverified=False, db=-30.0, force=False, vol_step=None
        )
        fast_read = functools.partial(devstate.read_settings, secs=0.2)
        with (
            mock.patch.object(toppingctl.hid, "Device", Handle),
            mock.patch.object(toppingctl.hid, "enumerate", _fake_enumerate("DX5II")),
            mock.patch.object(devstate, "read_settings", fast_read),
            mock.patch.object(toppingctl, "load_state", dict),
            mock.patch.object(toppingctl, "save_state", lambda st: writes.append(st)),
        ):
            out = run_quietly(toppingctl.cmd_vol, args)
        return writes, out

    def test_one_db(self):
        writes, out = self.run_vol(1)
        get_settings, volume, commit, saved = writes
        self.assertEqual((get_settings[2], get_settings[5], get_settings[6]), (0x10, 0x71, 0x0C))
        self.assertEqual(volume, toppingctl.frame(0x71, 0x02, 30))
        self.assertEqual(commit, toppingctl.frame(0x71, 0x34, 1))
        self.assertEqual(saved["volume_db"], -30.0)
        self.assertIn("volume -30.0 dB  (raw 30)", out)

    def test_half_db(self):
        writes, out = self.run_vol(0)
        self.assertEqual(writes[1], toppingctl.frame(0x71, 0x02, 60))
        self.assertIn("volume -30.0 dB  (raw 60)", out)


GOLDEN = os.path.join(HERE, "fixtures", "dry_run_golden.json")
FRAME_LINE = re.compile(r"^  ((?:[0-9a-f]{2} ){15}[0-9a-f]{2})  ", re.M)


def _golden():
    with open(GOLDEN) as fh:
        return json.load(fh)


def _run_cli(cmdline):
    sys.path.insert(0, os.path.join(HERE, "fixtures"))
    try:
        import make_dry_run_golden
    finally:
        sys.path.pop(0)
    return make_dry_run_golden.run(cmdline)


class DryRunGoldenTest(unittest.TestCase):
    """The plan_*/Refused refactor must not change a byte of --dry-run
    output, refusals included. fixtures/dry_run_golden.json was captured from
    main before it (see fixtures/make_dry_run_golden.py)."""

    def test_every_command_matches_the_golden_output(self):
        for cmdline, expected in _golden().items():
            with self.subTest(cmd=cmdline):
                self.assertEqual(_run_cli(cmdline), expected)

    def test_golden_covers_every_dry_run_command_in_ci(self):
        with open(os.path.join(HERE, ".github", "workflows", "ci.yml")) as fh:
            ci = re.findall(r"^\s+\./(\S+\.py\b.*)$", fh.read(), re.M)
        self.assertTrue(ci)
        golden = _golden()
        for cmdline in ci:
            self.assertIn(cmdline.strip(), golden)
        for preset in os.listdir(os.path.join(HERE, "presets")):
            if preset.endswith(".txt"):
                self.assertIn(f"toppingctl.py --dry-run apply presets/{preset}", golden)


class Dx5iiDryRunFramesAreConfirmedTest(unittest.TestCase):
    """Every DX5 II dry-run command emits only frames in CONFIRMED_WRITES.
    Run live, not read from the golden file, so it holds for the code as it
    is now; smoke.py is included because it targets the DX5 II too."""

    def test_only_confirmed_frames(self):
        allowed = toppingctl.CONFIRMED_WRITES["dx5ii"]
        for cmdline, golden in _golden().items():
            dx5 = cmdline.startswith("smoke.py") or (
                cmdline.startswith("toppingctl.py") and "--device" not in cmdline
            )
            dx5 = dx5 or cmdline.startswith("toppingctl.py --device dx5ii")
            if not dx5 or golden["returncode"] != 0:
                continue
            with self.subTest(cmd=cmdline):
                frames = FRAME_LINE.findall(_run_cli(cmdline)["stdout"])
                self.assertTrue(frames, "no frames printed")
                for hexed in frames:
                    b = bytes.fromhex(hexed)
                    self.assertEqual(b[2], 0x20, hexed)
                    self.assertIn((b[5], b[6]), allowed, hexed)


if __name__ == "__main__":
    unittest.main()
