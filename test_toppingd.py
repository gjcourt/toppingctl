#!/usr/bin/env python3
"""toppingd API tests against a fake HID device -- no hardware.

Each test starts the real HTTP server on 127.0.0.1 and calls it with signed
requests, so the signature check, the routes and the read paths are
exercised as toppingweb will call them. test_offline loads these too (see
its load_tests), so CI runs them on both matrix interpreters.

    python3 -m unittest test_toppingd
"""

import fcntl
import functools
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import devstate  # noqa: E402
import toppingctl  # noqa: E402
import toppingd  # noqa: E402

KEY = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

PRODUCTS = {"dx5ii": "DX5 II", "dx1ii": "DX1 II", "d90iii": "D90 III Discrete"}


def _enumerate(*products):
    """hid.enumerate for these product strings: two HID interfaces each, as a
    real DAC presents, sharing a serial."""

    def enumerate_(vid=0, pid=0):
        out = []
        for n, product in enumerate(products):
            for page in (1, 12):
                out.append(
                    {
                        "product_id": 0x8750,
                        "product_string": product,
                        "path": f"/fake/{n}/{page}".encode(),
                        "usage_page": page,
                        "serial_number": f"SN{n}",
                    }
                )
        return out

    return enumerate_


def _reply(reg_sub, total, cur, value):
    return (
        bytes([0x22, 0x33, 0x20, total, cur, reg_sub >> 8, reg_sub & 0xFF])
        + int(value).to_bytes(4, "big")
        + bytes([0, 0, 0x66, 0x77])
    )


class FakeDac:
    """The device behind hid.Device: records every frame written and answers
    the reads each model answers. `records` is the DX5 II's GetSettings set;
    `block` and `regs` are the DX1 II's 0x810a block and query answers."""

    def __init__(self, records=None, block=None, regs=None, fail_opens=0):
        self.writes, self.opens, self.closes = [], 0, 0
        self.records = records if records is not None else {0: 1, 9: 30, 32: 1}
        self.block = block or {}
        self.regs = regs or {}
        self.fail_opens = fail_opens
        self.pending = []

    def handle_class(self):
        dac = self

        class Handle:
            def __init__(self, *a, **kw):
                if dac.fail_opens:
                    dac.fail_opens -= 1
                    raise OSError("open failed")
                dac.opens += 1

            def write(self, data):
                data = bytes(data)
                dac.writes.append(data)
                f = data[1:] if data[0] == 0 else data
                reg_sub = (f[5] << 8) | f[6]
                if reg_sub == 0x710C:
                    for i, v in dac.records.items():
                        dac.pending.append(
                            bytes([0x22, 0x33, 0x10, 0x01, i, 0x71, 0x0C])
                            + v.to_bytes(4, "big")
                            + bytes(5)
                        )
                elif reg_sub == 0x810A:
                    for i, v in dac.block.items():
                        dac.pending.append(_reply(0x810A, 12, i, v))
                elif reg_sub in dac.regs:
                    dac.pending.append(_reply(reg_sub, 1, 1, dac.regs[reg_sub]))
                return len(data)

            def read(self, *a, **kw):
                return dac.pending.pop(0) if dac.pending else b""

            def close(self):
                dac.closes += 1

        return Handle


class AgentTestCase(unittest.TestCase):
    """Starts the agent on an ephemeral port with a fake DAC and a temp lock
    directory. Set `products` and `dac` before calling start()."""

    products = ("DX5 II",)
    configured = "auto"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lockdir = os.path.join(self.tmp, "lock")
        os.mkdir(self.lockdir, 0o700)
        self.state_dir = os.path.join(self.tmp, "state")
        os.mkdir(self.state_dir, 0o700)
        self.dac = FakeDac()
        self.clock = [time.time()]
        self.synced = [True]

    def start(self):
        fast = {
            "dx1_read_block": functools.partial(devstate.dx1_read_block, secs=0.1),
            "dx1_query": functools.partial(devstate.dx1_query, secs=0.05),
        }
        patches = [
            mock.patch.object(toppingctl.hid, "Device", self.dac.handle_class()),
            mock.patch.object(toppingctl.hid, "enumerate", _enumerate(*self.products)),
            mock.patch.object(toppingctl, "LOCK_SYSTEM_DIR", self.lockdir),
            mock.patch.object(toppingd, "AGENT_LOCK_WAIT", 0.3),
            mock.patch.object(toppingd, "OPEN_RETRY_SECS", 0.01),
            mock.patch.object(toppingd, "READ_SECS", 0.15),
        ] + [mock.patch.object(devstate, name, fn) for name, fn in fast.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.auth = toppingd.Auth(KEY, now=lambda: self.clock[0], ntp_synced=lambda: self.synced[0])
        self.auth.recheck_secs = 0
        if self.synced[0]:
            # Synced an hour ago, so the floor (NtpFloorTest) stays out of the way.
            self.auth.floor = int(self.clock[0]) - 3600
        self.agent = toppingd.Agent(
            "office", self.configured, self.state_dir, now=lambda: self.clock[0]
        )
        self.server = toppingd.make_server(("127.0.0.1", 0), self.agent, self.auth)
        self.server.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, path, method="GET", body=b"", key=KEY, ts=None, nonce=None, headers=None):
        """A signed request -> (status, json body, response headers, nonce, raw)."""
        nonce = nonce or uuid.uuid4().hex
        ts = str(int(self.clock[0] if ts is None else ts))
        sig = toppingd.sign(key, toppingd.request_message(method, path, ts, nonce, body))
        hdrs = {
            "X-Toppingd-Timestamp": ts,
            "X-Toppingd-Nonce": nonce,
            "X-Toppingd-Signature": sig,
        }
        if headers is not None:
            hdrs = headers
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            raw = r.read()
            return r.status, json.loads(raw), r.headers, nonce, raw
        finally:
            conn.close()

    def get(self, path):
        status, obj, headers, nonce, raw = self.call(path)
        self.assertTrue(toppingd.verify_response(KEY, nonce, status, headers, raw))
        return status, obj

    def send(self, path, obj=None, method="POST"):
        """A signed JSON request -> (status, body), its response checked."""
        body = b"" if obj is None else json.dumps(obj).encode()
        status, out, headers, nonce, raw = self.call(path, method=method, body=body)
        self.assertTrue(toppingd.verify_response(KEY, nonce, status, headers, raw))
        return status, out

    def readnacks(self):
        """(opcode, reg, sub) of every frame the fake DAC received."""
        out = []
        for data in self.dac.writes:
            f = data[1:] if data[0] == 0 else data
            out.append((f[2], f[5], f[6]))
        return out


class AuthTest(AgentTestCase):
    """The plan's "Agent authentication": signatures, skew, replay and
    signed responses that cover the request's nonce."""

    def setUp(self):
        super().setUp()
        self.start()

    def test_signed_request_gets_a_signed_response(self):
        status, obj, headers, nonce, raw = self.call("/v1/room")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Toppingd-Nonce"], nonce)
        self.assertTrue(toppingd.verify_response(KEY, nonce, status, headers, raw))

    def test_response_replayed_against_another_nonce_fails(self):
        status, _, headers, nonce, raw = self.call("/v1/room")
        self.assertFalse(toppingd.verify_response(KEY, uuid.uuid4().hex, status, headers, raw))

    def test_tampered_response_fails(self):
        status, _, headers, nonce, raw = self.call("/v1/room")
        self.assertFalse(toppingd.verify_response(KEY, nonce, status, headers, raw + b" "))
        self.assertFalse(toppingd.verify_response(KEY, nonce, 500, headers, raw))
        self.assertFalse(toppingd.verify_response(b"x" * 64, nonce, status, headers, raw))

    def test_bad_signature_is_refused_unsigned(self):
        status, obj, headers, *_ = self.call("/v1/room", key=b"y" * 64)
        self.assertEqual((status, obj["reason"]), (401, "bad signature"))
        self.assertIsNone(headers.get("X-Toppingd-Signature"))

    def test_signature_covers_path_query_and_method(self):
        nonce, ts = uuid.uuid4().hex, str(int(self.clock[0]))
        sig = toppingd.sign(KEY, toppingd.request_message("GET", "/v1/state", ts, nonce, b""))
        hdrs = {"X-Toppingd-Timestamp": ts, "X-Toppingd-Nonce": nonce, "X-Toppingd-Signature": sig}
        for method, path in (
            ("GET", "/v1/state?fresh=1"),
            ("GET", "/v1/room"),
            ("POST", "/v1/state"),
        ):
            with self.subTest(method=method, path=path):
                status, obj, *_ = self.call(path, method=method, headers=hdrs)
                self.assertEqual((status, obj["reason"]), (401, "bad signature"))

    def test_signature_covers_the_body(self):
        nonce, ts = uuid.uuid4().hex, str(int(self.clock[0]))
        sig = toppingd.sign(KEY, toppingd.request_message("POST", "/v1/room", ts, nonce, b"a"))
        hdrs = {"X-Toppingd-Timestamp": ts, "X-Toppingd-Nonce": nonce, "X-Toppingd-Signature": sig}
        status, obj, *_ = self.call("/v1/room", method="POST", body=b"b", headers=hdrs)
        self.assertEqual((status, obj["reason"]), (401, "bad signature"))

    def test_non_ascii_signature_is_a_401_not_a_crash(self):
        hdrs = {
            "X-Toppingd-Timestamp": str(int(self.clock[0])),
            "X-Toppingd-Nonce": uuid.uuid4().hex,
            "X-Toppingd-Signature": "\xff" * 64,
        }
        status, obj, *_ = self.call("/v1/room", headers=hdrs)
        self.assertEqual(status, 401)
        self.assertFalse(toppingd.verify_response(KEY, hdrs["X-Toppingd-Nonce"], 200, hdrs, b"{}"))

    def test_missing_headers(self):
        status, obj, *_ = self.call("/v1/room", headers={})
        self.assertEqual(status, 401)
        self.assertIn("missing", obj["reason"])

    def test_skewed_timestamps(self):
        for skew in (-31, 31):
            with self.subTest(skew=skew):
                status, obj, *_ = self.call("/v1/room", ts=self.clock[0] + skew)
                self.assertEqual(status, 401)
                self.assertIn("off this host's clock", obj["reason"])
        status, *_ = self.call("/v1/room", ts=self.clock[0] - 29)
        self.assertEqual(status, 200)

    def test_replayed_nonce(self):
        nonce = uuid.uuid4().hex
        self.assertEqual(self.call("/v1/room", nonce=nonce)[0], 200)
        status, obj, *_ = self.call("/v1/room", nonce=nonce)
        self.assertEqual((status, obj["reason"]), (401, "nonce already used"))

    def test_nonce_pruned_after_the_window_still_refused_by_skew(self):
        nonce, ts = uuid.uuid4().hex, self.clock[0]
        self.assertEqual(self.call("/v1/room", nonce=nonce, ts=ts)[0], 200)
        self.clock[0] += 31
        status, obj, *_ = self.call("/v1/room", nonce=nonce, ts=ts)
        self.assertEqual(status, 401)
        self.assertIn("off this host's clock", obj["reason"])


class NtpFloorTest(AgentTestCase):
    """The Pis have no RTC: nothing is accepted before NTP sync, and the floor
    is taken at first sync, not at start."""

    def test_clock_in_the_past_then_syncs(self):
        stale = time.time() - 86400  # fake-hwclock's idea of now
        self.clock[0], self.synced[0] = stale, False
        self.start()
        # A request signed against the stale clock: refused before sync, and
        # captured on the wire for a replay.
        captured = uuid.uuid4().hex
        status, obj, headers, *_ = self.call("/v1/room", nonce=captured)
        self.assertEqual((status, obj["error"]), (503, "clock_unsynced"))
        self.assertIn("NTP", obj["reason"])
        self.assertIsNone(headers.get("X-Toppingd-Signature"))
        self.clock[0], self.synced[0] = time.time(), True
        self.assertEqual(self.auth._synced_floor(), int(self.clock[0]) + 1)
        self.clock[0] += 2
        self.assertEqual(self.call("/v1/room")[0], 200)
        # The replay is refused by skew, and a request signed just before
        # the floor by the floor, even within the 30 s window.
        status, obj, *_ = self.call("/v1/room", ts=stale, nonce=captured)
        self.assertEqual(status, 401)
        self.assertIn("off this host's clock", obj["reason"])
        status, obj, *_ = self.call("/v1/room", ts=self.auth.floor - 5)
        self.assertEqual((status, obj["reason"]), (401, toppingd_floor_reason()))

    def test_floor_is_taken_at_sync_not_at_the_first_request(self):
        # A client clock a second behind must not be refused just because
        # its request was the one that noticed the sync.
        self.synced[0] = False
        self.start()
        self.synced[0] = True
        self.auth.recheck_secs = 0.01
        self.auth.watch()
        for _ in range(200):
            if self.auth.floor is not None:
                break
            time.sleep(0.01)
        self.assertEqual(self.auth.floor, int(self.clock[0]) + 1)
        self.clock[0] += 5
        self.assertEqual(self.call("/v1/room", ts=self.clock[0] - 1)[0], 200)

    def test_requests_before_sync_dont_take_the_floor_once_watched(self):
        self.synced[0] = False
        self.start()
        self.auth.recheck_secs = 60  # the watcher checks once, then sleeps
        self.auth.watch()
        time.sleep(0.05)
        self.synced[0] = True
        # The clock has synced but the watcher hasn't looked yet: a request
        # must not take the floor itself.
        self.assertEqual(self.call("/v1/room")[0], 503)
        self.assertIsNone(self.auth.floor)

    def test_restart_in_the_same_second_does_not_reopen_the_window(self):
        self.start()
        self.clock[0] = float(int(self.clock[0])) + 0.2
        ts = self.clock[0] + 2
        self.clock[0] += 2
        nonce = uuid.uuid4().hex
        self.assertEqual(self.call("/v1/room", nonce=nonce, ts=ts)[0], 200)
        self.clock[0] += 0.5  # restart within the same second
        self.server.RequestHandlerClass.auth = toppingd.Auth(
            KEY, now=lambda: self.clock[0], ntp_synced=lambda: True
        )
        status, obj, *_ = self.call("/v1/room", nonce=nonce, ts=ts)
        self.assertEqual((status, obj["reason"]), (401, toppingd_floor_reason()))

    def test_restart_does_not_reopen_the_replay_window(self):
        self.start()
        nonce, ts = uuid.uuid4().hex, self.clock[0]
        self.assertEqual(self.call("/v1/room", nonce=nonce, ts=ts)[0], 200)
        # A restart a few seconds later: a fresh Auth, nonce cache empty.
        self.clock[0] += 5
        self.server.RequestHandlerClass.auth = toppingd.Auth(
            KEY, now=lambda: self.clock[0], ntp_synced=lambda: True
        )
        status, obj, *_ = self.call("/v1/room", nonce=nonce, ts=ts)
        self.assertEqual((status, obj["reason"]), (401, toppingd_floor_reason()))


def toppingd_floor_reason():
    return "timestamp is from before this agent's clock synchronised"


class RoutesTest(AgentTestCase):
    """The routes this build has, and that none of them sends a write."""

    def setUp(self):
        super().setUp()
        self.start()

    def test_unknown_routes_and_methods(self):
        cases = [(m, "/v1/room") for m in ("POST", "PUT", "PATCH", "DELETE")]
        cases += [(m, "/v1/state") for m in ("POST", "DELETE")]
        cases += [(m, "/v1/volume") for m in ("GET", "PUT", "DELETE")]
        cases += [
            ("GET", "/v1/unlocks"),
            ("DELETE", "/v1/unlocks"),
            ("GET", "/v1/unlocks/" + "a" * 32),
        ]
        cases += [(m, "/v1/plans") for m in ("GET", "POST")]
        cases += [("DELETE", "/v1/unlocks/short"), ("POST", "/v1/plans/x/send")]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, obj, headers, nonce, raw = self.call(path, method=method, body=b"{}")
                self.assertIn(status, (404, 405))
                self.assertTrue(toppingd.verify_response(KEY, nonce, status, headers, raw))
        self.assertEqual(self.dac.writes, [])

    def test_every_route_sends_only_confirmed_readnacks(self):
        for path in ("/v1/room", "/v1/state", "/v1/state?fresh=1"):
            self.assertEqual(self.get(path)[0], 200)
        self.assertEqual(set(self.readnacks()), {(0x10, 0x71, 0x0C)})

    def test_no_route_sends_a_write_in_dry_run(self):
        uid = self.send("/v1/unlocks", {})[1]["unlock_id"]
        calls = [
            ("GET", "/v1/room", None),
            ("GET", "/v1/state?fresh=1", None),
            ("POST", "/v1/volume", {"target_db": -5, "expected_db": -30, "unlock_id": uid}),
            ("POST", "/v1/volume", {"target_db": -40, "expected_db": -30}),
            ("POST", "/v1/volume", {"target_db": -40, "expected_db": -31, "unlock_id": uid}),
            ("POST", "/v1/volume/lower", {}),
            ("DELETE", f"/v1/unlocks/{uid}", None),
        ]
        for method, path, body in calls:
            with self.subTest(method=method, path=path):
                self.assertIn(self.send(path, body, method=method)[0], (200, 409))
        self.assertEqual(set(self.readnacks()), {(0x10, 0x71, 0x0C)})


class Dx5StateTest(AgentTestCase):
    """DX5 II: GetSettings, decoded with the device's volumeStep."""

    def test_room(self):
        self.start()
        status, room = self.get("/v1/room")
        self.assertEqual(status, 200)
        self.assertEqual((room["room"], room["mode"]), ("office", "dry-run"))
        self.assertEqual(room["dac"]["key"], "dx5ii")
        self.assertEqual(room["dac"]["model_status"], "confirmed")
        self.assertEqual(room["read_path"], "get_settings")
        self.assertEqual(room["capabilities"], {"writes": ["volume"], "reads": ["71 0c"]})
        self.assertEqual(self.dac.writes, [], "identity must not send frames")

    def test_state_one_db(self):
        self.dac.records = {0: 1, 9: 25, 10: 0, 11: 0, 12: 1, 13: 1, 32: 1}
        self.start()
        status, st = self.get("/v1/state?fresh=1")
        self.assertEqual((status, st["status"], st["cached"]), (200, "read", False))
        f = st["fields"]
        self.assertEqual((f["volume_db"]["value"], f["volume_db"]["raw"]), (-25.0, 25))
        self.assertEqual(f["volume_db"]["source"], "device")
        self.assertEqual(f["power"]["value"], "on")
        self.assertEqual(f["mute"]["value"], False)
        self.assertEqual(f["gain"]["value"], "high")
        self.assertEqual(f["input"]["value"], "usb")
        self.assertEqual(st["settings"]["volumeStep"], 1)
        self.assertEqual(self.readnacks(), [(0x10, 0x71, 0x0C)])
        self.assertEqual((self.dac.opens, self.dac.closes), (1, 1))

    def test_state_half_db(self):
        self.dac.records = {9: 60, 32: 0}
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["fields"]["volume_db"]["value"], -30.0)

    def test_no_volume_step_means_no_volume(self):
        self.dac.records = {9: 60}
        self.start()
        f = self.get("/v1/state")[1]["fields"]["volume_db"]
        self.assertEqual((f["value"], f["source"]), (None, "unknown"))

    def test_cached_read_has_its_age_and_fresh_reads_again(self):
        self.start()
        self.get("/v1/state?fresh=1")
        time.sleep(0.1)
        status, st = self.get("/v1/state")
        self.assertTrue(st["cached"])
        self.assertGreater(st["age_s"], 0)
        self.assertEqual(self.dac.opens, 1)
        self.get("/v1/state?fresh=1")
        self.assertEqual(self.dac.opens, 2)

    def test_a_failed_read_drops_the_cache(self):
        self.start()
        self.assertEqual(self.get("/v1/state?fresh=1")[1]["status"], "read")
        self.dac.records = {}
        self.assertEqual(self.get("/v1/state?fresh=1")[1]["status"], "read_failed")
        st = self.get("/v1/state")[1]
        self.assertEqual(st["status"], "read_failed")
        self.assertFalse(st.get("cached"))

    def test_busy_keeps_the_cached_read(self):
        self.start()
        self.get("/v1/state?fresh=1")
        self.agent._hid.acquire()
        try:
            self.assertEqual(self.get("/v1/state?fresh=1")[0], 423)
        finally:
            self.agent._hid.release()
        st = self.get("/v1/state")[1]
        self.assertEqual((st["status"], st["cached"]), ("read", True))

    def test_device_lock_busy_drops_the_cached_read(self):
        self.start()
        self.get("/v1/state?fresh=1")
        fd = os.open(os.path.join(self.lockdir, "device.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.get("/v1/state?fresh=1")[0], 423)
        self.assertEqual(self.get("/v1/state")[0], 423)  # read again, not cached

    def test_cli_default_source_is_not_a_preset(self):
        with open(os.path.join(self.state_dir, "state.json"), "w") as fh:
            json.dump({"source": "defaults (no cache yet)", "volume_db": -30.0}, fh)
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["fields"]["preset"]["source"], "unknown")

    def test_written_preset_and_preamp_come_from_state_json(self):
        with open(os.path.join(self.state_dir, "state.json"), "w") as fh:
            json.dump({"source": "/opt/presets/bass1.json", "preamp_db": -6.0}, fh)
        self.start()
        f = self.get("/v1/state")[1]["fields"]
        self.assertEqual(f["preset"]["source"], "written")
        self.assertEqual(f["preset"]["value"], "/opt/presets/bass1.json")
        self.assertEqual((f["preamp_db"]["value"], f["preamp_db"]["label"]), (-6.0, "last written"))

    def test_nothing_written_is_unknown(self):
        self.start()
        f = self.get("/v1/state")[1]["fields"]
        self.assertEqual(f["preset"]["source"], "unknown")
        self.assertEqual(f["preamp_db"]["source"], "unknown")

    def test_state_json_symlink_is_not_followed(self):
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as fh:
            json.dump({"preamp_db": 3.0}, fh)
        os.symlink(target, os.path.join(self.state_dir, "state.json"))
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["fields"]["preamp_db"]["source"], "unknown")

    def test_empty_read_is_read_failed(self):
        self.dac.records = {}
        self.start()
        status, st = self.get("/v1/state")
        self.assertEqual((status, st["status"]), (200, "read_failed"))
        self.assertIn("no settings records", st["error"])
        self.assertEqual(st["fields"]["volume_db"]["source"], "unknown")

    def test_open_retries_then_busy(self):
        self.dac.fail_opens = 99
        self.start()
        status, st = self.get("/v1/state")
        self.assertEqual((status, st["status"]), (423, "busy"))
        self.assertIn("another app may hold it", st["error"])
        self.assertEqual(self.dac.writes, [])

    def test_open_retry_succeeds(self):
        self.dac.fail_opens = 2
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["status"], "read")


class LockTest(AgentTestCase):
    def test_busy_within_the_wait_naming_the_holder(self):
        self.start()
        path = os.path.join(self.lockdir, "device.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another open file: conflicts as a CLI would
        # The record a CLI holder writes; without /proc it is the only source.
        os.pwrite(fd, f"toppingctl {os.getpid()}\n".encode(), 0)
        t0 = time.monotonic()
        status, st = self.get("/v1/state?fresh=1")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((status, st["status"]), (423, "busy"))
        self.assertIn(f"pid {os.getpid()}", st["holder"])
        self.assertIn("in use by", st["error"])
        self.assertEqual(self.dac.opens, 0)

    def test_busy_holder_from_the_record_without_proc(self):
        # macOS has no /proc/locks: the holder comes from the lock file's record.
        self.start()
        p = mock.patch.object(toppingctl, "_proc_locks_holder", lambda fd: None)
        p.start()
        self.addCleanup(p.stop)
        path = os.path.join(self.lockdir, "device.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.pwrite(fd, f"toppingctl {os.getpid()}\n".encode(), 0)
        status, st = self.get("/v1/state?fresh=1")
        self.assertEqual((status, st["status"]), (423, "busy"))
        self.assertEqual(st["holder"], f"toppingctl (pid {os.getpid()})")

    def test_room_reports_the_device_lock(self):
        self.start()
        room = self.get("/v1/room")[1]
        self.assertEqual(
            room["device_lock"], {"path": os.path.join(self.lockdir, "device.lock"), "ok": True}
        )

    def test_room_reports_an_unusable_device_lock(self):
        self.start()
        os.chmod(self.lockdir, 0o777)  # group/other-writable: refused
        self.addCleanup(os.chmod, self.lockdir, 0o700)
        room = self.get("/v1/room")[1]
        self.assertFalse(room["device_lock"]["ok"])
        self.assertIn("cannot use the device lock", room["device_lock"]["error"])
        # A read still runs without it, as the CLI's reads do.
        self.assertEqual(self.get("/v1/state?fresh=1")[1]["status"], "read")

    def test_another_request_in_flight_is_busy(self):
        self.start()
        self.agent._hid.acquire()
        self.addCleanup(self.agent._hid.release)
        status, st = self.get("/v1/state?fresh=1")
        self.assertEqual((status, st["status"]), (423, "busy"))
        self.assertEqual(st["holder"], f"toppingd (pid {os.getpid()})")
        self.assertEqual(self.dac.opens, 0)

    def test_lock_released_after_each_read(self):
        self.start()
        self.get("/v1/state?fresh=1")
        self.assertEqual(toppingctl._lock["depth"], 0)
        fd = os.open(os.path.join(self.lockdir, "device.lock"), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held


class Dx1StateTest(AgentTestCase):
    """DX1 II: the vendor read-safe list only. Never 11 06, never a register
    outside CONFIRMED_READS, never a write."""

    products = ("DX1 II",)

    def setUp(self):
        super().setUp()
        self.dac.block = {2: 0b011, 3: 540, 4: 600, 5: 600, 6: 990, 7: 1}
        self.dac.regs = {0x7100: 1, 0x1204: 2, 0x1206: 1}

    def test_state(self):
        self.start()
        status, st = self.get("/v1/state?fresh=1")
        self.assertEqual((status, st["status"]), (200, "read"))
        self.assertEqual(st["read_path"], "dx1_read_safe")
        f = st["fields"]
        self.assertEqual(st["volumes_db"]["hp"]["value"], -45.0)
        self.assertEqual(f["volume_db"]["source"], "unknown")
        self.assertEqual(f["mute"]["value"], "analog")
        self.assertEqual(f["output"]["value"], ["lo", "hp"])
        self.assertEqual(f["power"]["value"], "on")
        self.assertEqual(f["gain"]["source"], "unknown")
        self.assertEqual(f["input"]["source"], "unknown")
        self.assertEqual((self.dac.opens, self.dac.closes), (1, 1))

    def test_only_confirmed_reads(self):
        self.start()
        self.get("/v1/state?fresh=1")
        sent = self.readnacks()
        self.assertTrue(sent)
        for opcode, reg, sub in sent:
            self.assertEqual(opcode, 0x10)
            self.assertIn((reg, sub), toppingctl.CONFIRMED_READS["dx1ii"])
        self.assertNotIn((0x10, 0x11, 0x06), sent)
        for data in self.dac.writes:
            self.assertEqual(data[0], 0, "dx1 frames carry report id 0")

    def test_room(self):
        self.start()
        room = self.get("/v1/room")[1]
        self.assertEqual(room["dac"]["model_status"], "unverified")
        self.assertEqual(room["capabilities"]["writes"], [])
        self.assertNotIn("11 06", room["capabilities"]["reads"])


class NoReadPathTest(AgentTestCase):
    """D90 III and anything else: identity only. No frame, no open."""

    def check_no_io(self):
        self.assertEqual((self.dac.writes, self.dac.opens), ([], 0))

    def test_d90iii(self):
        self.products = ("D90 III Discrete",)
        self.start()
        status, st = self.get("/v1/state?fresh=1")
        self.assertEqual((status, st["status"]), (200, "not_read"))
        self.assertEqual(st["dac"]["key"], "d90iii")
        self.assertEqual(st["fields"]["volume_db"]["reason"], toppingd.NO_READ_REASON)
        self.assertIsNone(self.get("/v1/room")[1]["read_path"])
        self.check_no_io()

    def test_no_dac(self):
        self.products = ()
        self.start()
        st = self.get("/v1/state")[1]
        self.assertEqual(st["status"], "no_dac")
        self.assertEqual(st["fields"]["preset"]["source"], "unknown")
        self.check_no_io()

    def test_unrecognised(self):
        self.products = ("D50s",)
        self.start()
        st = self.get("/v1/state")[1]
        self.assertEqual(st["status"], "unrecognised")
        self.assertEqual(st["dac"]["unrecognised"], ["D50s"])
        self.check_no_io()

    def test_several_dacs(self):
        self.products = ("DX5 II", "DX1 II")
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["status"], "several")
        self.check_no_io()

    def test_two_dacs_of_one_model(self):
        self.products = ("DX5 II", "DX5 II")
        self.start()
        self.assertEqual(self.get("/v1/state")[1]["status"], "several")
        self.assertIsNone(self.get("/v1/room")[1]["read_path"])
        self.check_no_io()

    def test_different_dac_than_configured(self):
        self.configured = "dx1ii"
        self.start()
        st = self.get("/v1/state")[1]
        self.assertEqual((st["status"], st["dac"]["expected"]), ("different", "dx1ii"))
        self.check_no_io()

    def test_emptied_confirmed_reads_stop_the_read(self):
        with mock.patch.dict(toppingctl.CONFIRMED_READS, {"dx5ii": frozenset()}):
            self.start()
            self.assertEqual(self.get("/v1/state")[1]["status"], "not_read")
        self.check_no_io()


def _vol_frame(raw):
    return toppingctl.frame(0x71, 0x02, raw).hex(" ")


class VolumeTestCase(AgentTestCase):
    """A DX5 II on one_db (raw = -dB) unless a test sets volumeStep 0."""

    def setUp(self):
        super().setUp()
        self.level(-30)

    def level(self, db, step=1):
        """Where the fake DAC's knob is: the next read returns it."""
        self.dac.records = {0: 1, 9: int(round(-db / (1.0 if step == 1 else 0.5))), 32: step}

    def volume(self, target, expected, unlock_id=None):
        body = {"target_db": target, "expected_db": expected}
        if unlock_id is not None:
            body["unlock_id"] = unlock_id
        return self.send("/v1/volume", body)

    def unlock(self):
        status, out = self.send("/v1/unlocks", {})
        self.assertEqual(status, 200)
        return out["unlock_id"]

    def assert_no_write_sent(self):
        """Dry-run: the DAC saw readNacks to GetSettings and nothing else."""
        self.assertTrue(self.readnacks(), "expected the operation to read the device")
        for opcode, reg, sub in self.readnacks():
            self.assertEqual((opcode, reg, sub), (0x10, 0x71, 0x0C))


class VolumeTest(VolumeTestCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_dry_run_returns_the_frames_and_sends_none(self):
        status, out = self.volume(-25, -30)
        self.assertEqual(status, 200)
        self.assertEqual((out["mode"], out["sent"]), ("dry-run", False))
        self.assertEqual(out["before"], {"db": -30.0, "raw": 30, "volume_step_db": 1.0})
        self.assertEqual(out["after_predicted"], {"db": -25.0, "raw": 25, "volume_step_db": 1.0})
        self.assertEqual(
            [f["hex"] for f in out["frames"]],
            [_vol_frame(25), toppingctl.frame(0x71, 0x34, 1).hex(" ")],
        )
        self.assertFalse(out["loud_result"])
        self.assertEqual(out["undo"], {"target_db": -30.0, "expected_db": -25.0})
        self.assertEqual(self.readnacks(), [(0x10, 0x71, 0x0C)])
        self.assertEqual((self.dac.opens, self.dac.closes), (1, 1))

    def test_half_db_step_reads_its_own_scale(self):
        self.level(-30, step=0)
        status, out = self.volume(-20.5, -30)
        self.assertEqual(status, 200)
        self.assertEqual(out["before"]["raw"], 60)
        self.assertEqual(out["after_predicted"], {"db": -20.5, "raw": 41, "volume_step_db": 0.5})

    def test_a_jump_of_any_size_is_one_write(self):
        status, out = self.volume(-99, -30)
        self.assertEqual(status, 200)
        self.assertEqual(out["frames"][0]["hex"], _vol_frame(99))
        self.level(-99)
        status, out = self.volume(-11, -99)
        self.assertEqual((status, out["after_predicted"]["raw"]), (200, 11))

    def test_the_read_refreshes_the_state_cache(self):
        self.level(-40)
        self.volume(-41, -40)
        st = self.get("/v1/state")[1]
        self.assertEqual((st["cached"], st["fields"]["volume_db"]["value"]), (True, -40.0))

    def test_changed_elsewhere_is_409_with_the_fresh_state(self):
        # The knob moved to -60 since the UI read -30: the "+" must not jump.
        self.level(-60)
        status, out = self.volume(-29, -30)
        self.assertEqual((status, out["error"]), (409, "changed"))
        self.assertIn("reads -60.0 dB, not the -30.0 dB", out["reason"])
        self.assertEqual(out["state"]["fields"]["volume_db"]["value"], -60.0)
        self.assertNotIn("frames", out)
        self.assertFalse(out["unlock_discarded"])
        self.assert_no_write_sent()

    def test_expected_is_compared_in_raw_units(self):
        # -30.4 is within float noise of nothing, but it is not a level a
        # one_db device reports, so it can't match raw 30.
        for expected in (-30.4, -30.5, -29.9999):
            with self.subTest(expected=expected):
                self.assertEqual(self.volume(-31, expected)[0], 409)
        self.assertEqual(self.volume(-31, -30.0)[0], 200)
        self.level(-30, step=0)
        self.assertEqual(self.volume(-31, -30.5)[0], 409)
        self.level(-30.5, step=0)
        self.assertEqual(self.volume(-31, -30.5)[0], 200)

    def test_busy_agent_is_423_and_sends_nothing(self):
        self.agent._hid.acquire()
        self.addCleanup(self.agent._hid.release)
        status, out = self.volume(-25, -30)
        self.assertEqual((status, out["error"]), (423, "busy"))
        self.assertEqual(self.dac.opens, 0)

    def test_no_volume_step_in_the_read_is_read_failed(self):
        self.dac.records = {9: 30}
        status, out = self.volume(-25, -30)
        self.assertEqual((status, out["error"]), (503, "read_failed"))
        self.assertIn("volumeStep", out["reason"])
        self.assertEqual(out["state"]["status"], "read")
        self.assert_no_write_sent()
        # The read itself worked, so it stays servable.
        self.assertTrue(self.get("/v1/state")[1]["cached"])

    def test_bad_requests_open_nothing(self):
        cases = [
            {"expected_db": -30},
            {"target_db": -25},
            {"target_db": True, "expected_db": -30},
            {"target_db": "-25", "expected_db": -30},
            {"target_db": 3, "expected_db": -30},
            {"target_db": 10**400, "expected_db": -30},
            {"target_db": -20, "expected_db": -1.7e308},
            {"target_db": -20, "expected_db": 5},
            {"target_db": -25, "expected_db": -30, "unlock_id": 7},
            {"target_db": -25, "expected_db": -30, "unlock_id": "../x"},
            [],
        ]
        for body in cases:
            with self.subTest(body=body):
                status, out = self.send("/v1/volume", body)
                self.assertEqual((status, out["error"]), (400, "bad_request"))
        for raw in (b"not json", b'{"target_db": NaN, "expected_db": -30}', b"[" * 60000):
            with self.subTest(raw=raw):
                self.assertEqual(self.call("/v1/volume", method="POST", body=raw)[0], 400)
        self.assertEqual(self.dac.opens, 0)

    def test_anything_but_volume_points_at_plans(self):
        status, out = self.send("/v1/volume", {"target_db": -25, "expected_db": -30, "gain": 1})
        self.assertEqual(status, 400)
        self.assertIn("/v1/plans", out["reason"])
        self.assertEqual(self.dac.opens, 0)


class VolumeModelTest(VolumeTestCase):
    """Only a confirmed model with its volume frames in CONFIRMED_WRITES."""

    def check_refused(self, error, http=403):
        status, out = self.volume(-31, -30)
        self.assertEqual((status, out["error"]), (http, error))
        self.assertNotIn("frames", out)
        self.assertEqual(self.dac.writes, [])
        return out

    def test_dx1ii_is_unverified(self):
        self.products = ("DX1 II",)
        self.start()
        self.assertIn("unverified", self.check_refused("model_unverified")["reason"])
        self.assertEqual(self.dac.opens, 0)

    def test_d90iii_is_unverified(self):
        self.products = ("D90 III Discrete",)
        self.start()
        self.check_refused("model_unverified")
        self.assertEqual(self.dac.opens, 0)

    def test_no_dac(self):
        self.products = ()
        self.start()
        self.assertEqual(self.check_refused("unavailable", http=503)["status"], "no_dac")

    def test_several_dacs(self):
        self.products = ("DX5 II", "DX1 II")
        self.start()
        self.assertEqual(self.check_refused("unavailable", http=503)["status"], "several")
        self.assertEqual(self.dac.opens, 0)

    def test_emptied_confirmed_writes_refuse(self):
        with mock.patch.dict(toppingctl.CONFIRMED_WRITES, {"dx5ii": frozenset()}):
            self.start()
            self.check_refused("model_unverified")
            self.assertEqual(self.get("/v1/room")[1]["capabilities"]["writes"], [])

    def test_agent_write_gate(self):
        frames = [toppingctl.Frame(toppingctl.frame(0x71, 0x35, 1), "SaveC1")]
        with self.assertRaisesRegex(toppingctl.Refused, "NEVER"):
            toppingd.check_agent_writes("dx5ii", frames)
        for reg_sub in ((0x71, 0x11), (0x71, 0x04), (0x71, 0x05), (0x71, 0x03)):
            f = [toppingctl.Frame(toppingctl.frame(*reg_sub, 1), "")]
            with self.subTest(reg_sub=reg_sub), self.assertRaises(toppingctl.Refused):
                toppingd.check_agent_writes("dx5ii", f)
        readnack = [toppingctl.Frame(toppingctl.frame(0x71, 0x02, 1, opcode=0x10), "")]
        with self.assertRaises(toppingctl.Refused):
            toppingd.check_agent_writes("dx5ii", readnack)
        toppingd.check_agent_writes("dx5ii", toppingctl.plan_volume(DX5, -30, 1.0, False))


DX5 = toppingctl.DEVICES["dx5ii"]


class LoudTest(VolumeTestCase):
    """The -10 dB rule: quieter is always allowed; louder above -10 dB needs
    a live unlock, checked before and after quantising."""

    def setUp(self):
        super().setUp()
        self.start()

    def test_louder_above_minus_10_needs_an_unlock(self):
        status, out = self.volume(-5, -30)
        self.assertEqual((status, out["error"]), (403, "unlock_required"))
        self.assertIn("no unlock_id was sent", out["reason"])
        uid = self.unlock()
        status, out = self.volume(-5, -30, uid)
        self.assertEqual(status, 200)
        self.assertTrue(out["loud_result"])
        self.assert_no_write_sent()

    def test_quieter_above_minus_10_needs_none(self):
        self.level(-3)
        status, out = self.volume(-6, -3)
        self.assertEqual(status, 200)
        self.assertTrue(out["loud_result"])
        self.assertEqual(self.volume(-3, -3)[0], 200)  # not louder: the same level

    def test_lower_to_minus_30_needs_none(self):
        self.level(0)
        self.assertEqual(self.volume(-30, 0)[0], 200)

    def test_at_minus_10_needs_none(self):
        self.assertEqual(self.volume(-10, -30)[0], 200)

    def test_requested_level_is_checked_before_quantising(self):
        # -9.6 quantises to -10.0 on one_db, but -9.6 was asked for.
        status, out = self.volume(-9.6, -30)
        self.assertEqual((status, out["error"]), (403, "unlock_required"))
        self.assertTrue(out["reason"].startswith("-9.6 dB is above"), out["reason"])

    def test_quantised_level_is_checked_too(self):
        # A quantiser that rounds one step louder: the after-quantising check
        # is what stops -10.0 landing at -9.0.
        def louder(db, step_db):
            steps = int(round(-db / step_db)) - 1
            return steps, -steps * step_db

        with mock.patch.object(toppingctl, "volume_steps", louder):
            status, out = self.volume(-10, -30)
            self.assertEqual((status, out["error"]), (403, "unlock_required"))
            # Quieter by the agent's own check, but the frame would go out
            # one step louder: refused, because the frame is checked too.
            self.level(-10)
            status, out = self.volume(-10, -10)
            self.assertEqual((status, out["error"]), (403, "refused"))
            self.assertIn("raw 9, not the 10", out["reason"])

    def test_rounding_never_carries_an_accepted_level_above_minus_10(self):
        for step in (1, 0):
            rec = {9: 30 if step else 60, 32: step}
            for hundredths in range(-1200, -999, 5):
                target = hundredths / 100
                with self.subTest(step=step, target=target):
                    status, out = self.agent._volume_decision(DX5, "dx5ii", rec, target, -30, None)
                    self.assertEqual(status, 200)
                    self.assertLessEqual(out["after_predicted"]["db"], -10.0)
            for target in (-9.99, -9.75, -9.5, -9.25, -9.0):
                with self.subTest(step=step, target=target):
                    status, _ = self.agent._volume_decision(DX5, "dx5ii", rec, target, -30, None)
                    self.assertEqual(status, 403)


class UnlockTest(VolumeTestCase):
    """POST/DELETE /v1/unlocks: 60 s, not consumed by writes, ended by the
    cap, DELETE (Cancel, leaving, signing out, unticking) or a 409."""

    def setUp(self):
        super().setUp()
        self.start()

    def test_create_sends_nothing(self):
        status, out = self.send("/v1/unlocks", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["expires_in_s"], 60)
        self.assertEqual(out["expires_at"], toppingd._iso(self.clock[0] + 60))
        self.assertRegex(out["unlock_id"], toppingd.UNLOCK_ID)
        self.assertEqual(self.send("/v1/unlocks")[0], 200)  # an empty body is {}
        self.assertEqual(self.send("/v1/unlocks", {"secs": 600})[0], 400)
        self.assertEqual((self.dac.writes, self.dac.opens), ([], 0))

    def test_expires_at_60_s(self):
        uid = self.unlock()
        self.clock[0] += 59.9
        self.assertEqual(self.volume(-5, -30, uid)[0], 200)
        self.clock[0] += 0.1
        status, out = self.volume(-5, -30, uid)
        self.assertEqual((status, out["error"]), (403, "unlock_required"))
        self.assertIn("expired", out["reason"])

    def test_checked_when_the_write_runs(self):
        # The UI queued the write inside the cap; it runs after.
        uid = self.unlock()
        self.clock[0] += 61
        self.assertEqual(self.volume(-5, -30, uid)[0], 403)

    def test_cancelled_unlock_is_refused(self):
        uid = self.unlock()
        status, out = self.send(f"/v1/unlocks/{uid}", method="DELETE")
        self.assertEqual((status, out["discarded"]), (200, True))
        status, out = self.volume(-5, -30, uid)
        self.assertEqual((status, out["error"]), (403, "unlock_required"))
        self.assertIn("cancelled", out["reason"])
        # Discarding again, as leaving after Cancel would, is not an error.
        status, out = self.send(f"/v1/unlocks/{uid}", method="DELETE")
        self.assertEqual((status, out["discarded"]), (200, False))

    def test_unknown_unlock_is_refused(self):
        self.assertEqual(self.volume(-5, -30, "x" * 32)[0], 403)

    def test_a_409_discards_the_unlock(self):
        uid = self.unlock()
        self.level(-20)  # changed elsewhere
        status, out = self.volume(-5, -30, uid)
        self.assertEqual((status, out["unlock_discarded"]), (409, True))
        status, out = self.volume(-5, -20, uid)
        self.assertEqual((status, out["error"]), (403, "unlock_required"))

    def test_paused_drag(self):
        uid = self.unlock()
        # A drag up past -10 dB: each write lands, and the next starts from it.
        for t, target, expected in ((1, -9, -30), (2, -7, -9)):
            self.clock[0] += t
            self.assertEqual(self.volume(target, expected, uid)[0], 200)
            self.level(target)
        # The volume settles; a pause of 40 s doesn't relock the unlock.
        self.clock[0] += 40
        self.assertEqual(self.volume(-5, -7, uid)[0], 200)
        self.level(-5)
        # Two writes at once: the second waits for the first, and both run
        # under the same unlock.
        self.clock[0] += 10
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.volume(-4, -5, uid)[0]))
            for _ in range(2)
        ]
        # The second waits for the first under the agent's own lock.
        with mock.patch.object(toppingd, "AGENT_LOCK_WAIT", 2.0):
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(results, [200, 200])
        # The first write after the cap is refused.
        self.clock[0] += 7  # 60 s since the unlock
        self.assertEqual(self.volume(-4, -5, uid)[0], 403)
        self.assert_no_write_sent()

    def test_unlocks_are_independent(self):
        a, b = self.unlock(), self.unlock()
        self.send(f"/v1/unlocks/{a}", method="DELETE")
        self.assertEqual(self.volume(-5, -30, b)[0], 200)


class UndoTest(VolumeTestCase):
    """Undo is a volume write to the level read before the burst, with
    expected_db the level the burst set."""

    def setUp(self):
        super().setUp()
        self.start()

    def undo(self, token, unlock_id=None):
        return self.volume(token["target_db"], token["expected_db"], unlock_id)

    def test_undo_token_round_trip(self):
        status, out = self.volume(-25, -30)
        self.level(-25)  # the write landed
        status, out = self.undo(out["undo"])
        self.assertEqual(status, 200)
        self.assertEqual(out["frames"][0]["hex"], _vol_frame(30))

    def test_undo_returns_to_before_the_whole_burst(self):
        first = self.volume(-28, -30)[1]
        self.level(-28)
        last = self.volume(-25, -28)[1]
        self.level(-25)
        burst = {
            "target_db": first["undo"]["target_db"],
            "expected_db": last["undo"]["expected_db"],
        }
        self.assertEqual(burst, {"target_db": -30.0, "expected_db": -25.0})
        status, out = self.undo(burst)
        self.assertEqual((status, out["after_predicted"]["db"]), (200, -30.0))
        # The first write's own token no longer matches the device.
        self.assertEqual(self.undo(first["undo"])[0], 409)

    def test_undo_after_a_change_elsewhere_is_409(self):
        out = self.volume(-25, -30)[1]
        self.level(-40)  # the knob moved after the write landed
        status, out = self.undo(out["undo"])
        self.assertEqual((status, out["error"]), (409, "changed"))

    def test_undo_that_raises_above_minus_10_needs_an_unlock(self):
        self.level(-5)
        out = self.volume(-20, -5)[1]  # lowering: no unlock needed
        self.level(-20)
        status, refused = self.undo(out["undo"])
        self.assertEqual((status, refused["error"]), (403, "unlock_required"))
        self.assertEqual(self.undo(out["undo"], self.unlock())[0], 200)

    def test_undo_that_lowers_above_minus_10_needs_none(self):
        # An interpretation in the plan, not George's decision (flagged in
        # the PR): quieter is always allowed, even when it lands above -10.
        self.level(-6)
        out = self.volume(-3, -6, self.unlock())[1]
        self.level(-3)
        status, out = self.undo(out["undo"])
        self.assertEqual(status, 200)
        self.assertEqual(out["after_predicted"]["db"], -6.0)
        self.assertTrue(out["loud_result"])
        self.assert_no_write_sent()


class LowerTest(VolumeTestCase):
    """POST /v1/volume/lower: "Always lower if louder" (George, 2026-11-08,
    #40 Q8). No expected_db, so no 409; never an unlock; never raises."""

    def setUp(self):
        super().setUp()
        self.start()

    def lower(self, body=None):
        return self.send("/v1/volume/lower", {} if body is None else body)

    def test_louder_room_is_lowered_without_expected_or_unlock(self):
        # The knob went to -5 elsewhere; the UI never saw it, and needn't.
        self.level(-5)
        status, out = self.lower()
        self.assertEqual(status, 200)
        self.assertEqual((out["lowered"], out["sent"]), (True, False))
        self.assertEqual(out["before"]["db"], -5.0)
        self.assertEqual(out["after_predicted"]["db"], -30.0)
        self.assertEqual(out["frames"][0]["hex"], _vol_frame(30))
        self.assertFalse(out["loud_result"])
        self.assertEqual(out["undo"], {"target_db": -5.0, "expected_db": -30.0})
        self.assert_no_write_sent()

    def test_half_db_step(self):
        self.level(-20.5, step=0)
        status, out = self.lower()
        self.assertEqual((status, out["frames"][0]["hex"]), (200, _vol_frame(60)))

    def test_at_or_below_minus_30_plans_nothing(self):
        for db in (-30, -30.5, -60):
            with self.subTest(db=db):
                self.level(db, step=0)
                status, out = self.lower()
                self.assertEqual((status, out["lowered"], out["frames"]), (200, False, []))
                self.assertNotIn("undo", out)
        self.assert_no_write_sent()

    def test_takes_only_an_empty_body(self):
        for body in ({"expected_db": -30}, {"target_db": -40}, {"unlock_id": "x" * 32}, []):
            with self.subTest(body=body):
                self.assertEqual(self.lower(body)[0], 400)
        self.assertEqual(self.send("/v1/volume/lower", method="GET")[0], 405)
        self.assertEqual(self.dac.opens, 0)

    def test_does_not_touch_unlocks(self):
        uid = self.unlock()
        self.level(-5)
        self.assertEqual(self.lower()[0], 200)
        self.assertEqual(self.volume(-4, -5, uid)[0], 200)

    def test_ordinary_writes_keep_compare_and_set(self):
        self.level(-5)
        self.assertEqual(self.volume(-30, -20)[0], 409)

    def test_undo_of_a_lower_is_an_ordinary_write(self):
        # Undoing back to -5 raises above -10: it needs the unlock, and it
        # keeps compare-and-set like any other volume write.
        self.level(-5)
        token = self.lower()[1]["undo"]
        self.level(-30)
        status, out = self.volume(token["target_db"], token["expected_db"])
        self.assertEqual((status, out["error"]), (403, "unlock_required"))
        self.assertEqual(
            self.volume(token["target_db"], token["expected_db"], self.unlock())[0], 200
        )
        self.level(-40)
        self.assertEqual(self.volume(token["target_db"], token["expected_db"])[0], 409)

    def test_unverified_model_is_refused(self):
        with mock.patch.dict(toppingctl.CONFIRMED_WRITES, {"dx5ii": frozenset()}):
            status, out = self.lower()
        self.assertEqual((status, out["error"]), (403, "model_unverified"))
        self.assertEqual(self.dac.opens, 0)


class ReadOnlyHandleTest(unittest.TestCase):
    """The agent's send path, with frames built by hand: anything but a
    confirmed readNack is refused before it reaches the handle."""

    def setUp(self):
        self.dac = FakeDac()
        self.h = toppingd.ReadOnlyHandle(self.dac.handle_class()(), "dx5ii")

    def refused(self, data):
        with self.assertRaises(toppingctl.Refused):
            self.h.write(data)
        self.assertEqual(self.dac.writes, [])

    def test_never_set_even_as_readnack(self):
        for name in sorted(toppingd.NEVER):
            (cmd,) = [c for c, n in toppingd.COMMANDS.items() if n == name]
            for opcode in (0x10, 0x20):
                with self.subTest(name=name, opcode=opcode):
                    self.refused(toppingctl.frame(cmd >> 8, cmd & 0xFF, 1, opcode=opcode))
        self.assertEqual(
            toppingd.NEVER_REGS, {(0x71, 0x35), (0x71, 0x36), (0x71, 0x0B), (0x71, 0x2E)}
        )

    def test_writes_refused_even_confirmed_ones(self):
        for reg, sub in sorted(toppingctl.CONFIRMED_WRITES["dx5ii"]):
            self.refused(toppingctl.frame(reg, sub, 0, opcode=0x20))
        for reg, sub in ((0x71, 0x11), (0x71, 0x12), (0x71, 0x04), (0x71, 0x05), (0x71, 0x03)):
            self.refused(toppingctl.frame(reg, sub, 0, opcode=0x20))
        # A write to a register that IS a confirmed read is still a write.
        self.refused(toppingctl.frame(0x71, 0x0C, 0, opcode=0x20))
        dx1 = toppingd.ReadOnlyHandle(self.dac.handle_class()(), "dx1ii")
        with self.assertRaises(toppingctl.Refused):  # a dx1 volume write to the 0x810a block
            dx1.write(bytes([0]) + toppingctl.dx1_block_frame(toppingctl.DX1_F_HP_VOL, 540)[:15])
        self.assertEqual(self.dac.writes, [])

    def test_unlisted_readnack(self):
        self.refused(toppingctl.frame(0x71, 0x0F, 0, opcode=0x10))
        dx1 = toppingd.ReadOnlyHandle(self.dac.handle_class()(), "dx1ii")
        for reg_sub in ((0x11, 0x06), (0x79, 0x00), (0x7D, 0x00), (0x75, 0x00), (0x71, 0x0C)):
            with self.subTest(reg_sub=reg_sub), self.assertRaises(toppingctl.Refused):
                dx1.write(devstate._prefixed(toppingctl.frame(*reg_sub, 0, opcode=0x10)))
        d90 = toppingd.ReadOnlyHandle(self.dac.handle_class()(), "d90iii")
        with self.assertRaises(toppingctl.Refused):
            d90.write(toppingctl.frame(0x71, 0x0C, 0, opcode=0x10))
        self.assertEqual(self.dac.writes, [])

    def test_readnack_with_data(self):
        self.refused(toppingctl.frame(0x71, 0x0C, 1, opcode=0x10))

    def test_garbage(self):
        self.refused(b"\x00" * 16)
        self.refused(b"")

    def test_confirmed_readnack_passes(self):
        self.h.write(toppingctl.frame(0x71, 0x0C, 0, opcode=0x10))
        self.assertEqual(len(self.dac.writes), 1)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = os.path.join(self.tmp, "key")
        with open(self.key, "wb") as fh:
            fh.write(KEY + b"\n")
        os.chmod(self.key, 0o600)

    def load(self, text):
        path = os.path.join(self.tmp, "toppingd.yaml")
        with open(path, "w") as fh:
            fh.write(text.replace("KEY", self.key))
        return toppingd.load_config(path)

    def refused(self, text, match):
        with self.assertRaises(toppingd.ConfigError) as cm:
            self.load(text)
        self.assertIn(match, str(cm.exception))

    def test_example_loads(self):
        with open(os.path.join(HERE, "toppingd.yaml.example")) as fh:
            example = fh.read().replace("/etc/toppingd/key", "KEY")
        cfg = self.load(example)
        self.assertEqual(cfg["mode"], "dry-run")
        self.assertEqual(cfg["listen"], ("0.0.0.0", 8740))
        self.assertEqual(cfg["key"], KEY)

    def test_live_is_refused(self):
        self.refused("key_file: KEY\nmode: live\n", "only runs dry-run")
        with self.assertRaises(ValueError):
            toppingd.Agent("office", mode="live")

    def test_unknown_key_is_refused(self):
        # No switch can make an unverified model writable; a typo or an
        # invented one is an error, not ignored.
        self.refused("key_file: KEY\nunverified: true\n", "unknown keys")

    def test_unknown_device(self):
        self.refused("key_file: KEY\ndevice: e50ii\n", "unknown device")

    def test_key_readable_by_others(self):
        os.chmod(self.key, 0o640)
        self.refused("key_file: KEY\n", "chmod 600")

    def test_short_key(self):
        with open(self.key, "wb") as fh:
            fh.write(b"short")
        self.refused("key_file: KEY\n", "shorter than")

    def test_key_file_required(self):
        self.refused("mode: dry-run\n", "needs key_file")

    def test_room_defaults_to_hostname(self):
        cfg = self.load("key_file: KEY\n")
        self.assertEqual(cfg["room"], toppingd.socket.gethostname().split(".")[0])


if __name__ == "__main__":
    unittest.main()
