#!/usr/bin/env python3
"""toppingd -- the per-host agent behind the toppingctl web UI. Dry-run only.

Plan: docs/plans/2026-10-26-web-ui-architecture-plan.md, build steps 2 and 4
(the agent half of step 4, in dry-run). This build sends no writes at all:
its only mode is dry-run, the volume route returns the frames it would send
and sends nothing, and its handle wrapper refuses any frame that is not a
readNack in CONFIRMED_READS for the model.

    GET /v1/room             identity: host, room, DAC, read path, mode, and
                             whether the device lock file can be opened
    GET /v1/state[?fresh=1]  state from the model's read path; fresh=1 reads
                             the device, otherwise the cached read and its age
    POST /v1/volume          {target_db, expected_db, unlock_id?}: reads the
                             device, checks expected_db against that read in
                             raw units (409 changed), applies the -10 dB rule,
                             and returns the frames, with an undo token
    POST /v1/unlocks         {} -> {unlock_id, expires_at}: 60 s above -10 dB
    DELETE /v1/unlocks/{id}  discards an unlock

Undo is a volume write: POST /v1/volume with the undo token's target_db and
expected_db (the plan's "Undo of a volume change is a volume write").

Run it with a YAML config (toppingd.yaml.example):

    toppingd.py --config /etc/toppingd/toppingd.yaml

Every request is signed with the host's key (HMAC-SHA256), and so is every
response to a request that passed the check:

    X-Toppingd-Timestamp  unix seconds, as a decimal integer
    X-Toppingd-Nonce      16-128 characters of [A-Za-z0-9_-], fresh per request
    X-Toppingd-Signature  hex HMAC-SHA256 of request_message(...)

The response carries the request's nonce, its own timestamp, and a signature
over response_message(...), which covers that nonce: a reply can't be
replayed against another request. The agent refuses a bad signature, a
timestamp more than 30 s off, a nonce already seen in that window, and
anything at all until the clock reports NTP sync. At first sync it records a
floor and refuses timestamps before it, so a restart, which empties the nonce
cache, doesn't reopen the replay window.
"""

import argparse
import contextlib
import datetime
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import yaml

import devstate
import toppingctl
from toppingctl import (
    CONFIRMED_READS,
    CONFIRMED_WRITES,
    DEVICES,
    DX1_F_HP_VOL,
    DX1_F_LO_VOL,
    DX1_F_LOHP_VOL,
    DX1_F_MUTE,
    DX1_F_OPT_VOL,
    DX1_MUTE,
    REG_CTRL,
    SUB_COMMIT,
    SUB_VOLUME,
    VOL_WARN_DB,
    DeviceBusy,
    Refused,
    check_volume_range,
    dx1_raw_to_db,
    find_devices,
    open_checked,
    plan_volume,
    volume_steps,
)
from vendor_commands import COMMANDS, ENUMS, FIELD_ENUM, SETTINGS_FIELDS

VERSION = "0.2.0"
PROG = "toppingd"

DEFAULT_CONFIG = "/etc/toppingd/toppingd.yaml"
CONFIG_KEYS = {"listen", "room", "device", "mode", "key_file", "state_dir"}
# Only dry-run exists: live waits for the front-panel checks of step 4
# (TOPPINGCTL-7/8/9). There is deliberately no config key that makes an
# unverified model writable.
MODES = ("dry-run",)

# "Allow above -10 dB for 60 s" (George, 2026-11-01 and 2026-11-03). Volume
# writes don't consume an unlock; only the cap, DELETE or a 409 ends it.
UNLOCK_SECS = 60
UNLOCK_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
VOLUME_KEYS = {"target_db", "expected_db", "unlock_id"}
# The frames a volume write is: 71 02 and the commit.
VOLUME_WRITES = frozenset([(REG_CTRL, SUB_VOLUME), (REG_CTRL, SUB_COMMIT)])
# GetSettings record indexes (vendor_commands.SETTINGS_FIELDS).
REC_VOLUME, REC_VOLUME_STEP = 9, 32

# The plan's waits: the agent gives up on the device lock after 2 s and
# answers 423, well inside toppingweb's 10 s response timeout.
AGENT_LOCK_WAIT = 2.0
# GetSettings listens for a fixed window today; step 2b stops early once the
# record count is measured.
READ_SECS = 2.0
OPEN_ATTEMPTS = 4
OPEN_RETRY_SECS = 0.4

SKEW_SECS = 30
SIGNATURE = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}")
TIMESTAMP = re.compile(r"[0-9]{1,12}")
MIN_KEY_BYTES = 32
MAX_BODY = 64 * 1024

# listen.py's NEVER set, resolved by name through vendor_commands. The
# readNack allow-list already excludes these; the deny-list is checked first
# anyway, so it holds whatever the allow-list later grows to.
NEVER = {"SaveC1", "SaveC2", "FactoryReset", "FirmwareUpdate"}
NEVER_REGS = frozenset((c >> 8, c & 0xFF) for c, n in COMMANDS.items() if n in NEVER)
READ_NACK = 0x10


# --- signing ------------------------------------------------------------------


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def request_message(method, path, timestamp, nonce, body):
    """What a request signature covers. `path` includes the query string."""
    return "\n".join(
        ["toppingd-v1-request", method, path, timestamp, nonce, _digest(body)]
    ).encode()


def response_message(nonce, status, timestamp, body):
    """What a response signature covers: the REQUEST's nonce, so a reply
    can't be replayed against another request. The different prefix keeps a
    response signature from ever passing as a request signature."""
    return "\n".join(
        ["toppingd-v1-response", nonce, str(status), timestamp, _digest(body)]
    ).encode()


def sign(key, message):
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_response(key, sent_nonce, status, headers, body):
    """The client's check (toppingweb's, and the tests'): True only for a
    response signed with `key` for the nonce this client sent."""
    nonce = headers.get("X-Toppingd-Nonce", "")
    ts = headers.get("X-Toppingd-Timestamp", "")
    sig = headers.get("X-Toppingd-Signature", "")
    if not (SIGNATURE.fullmatch(sig) and TIMESTAMP.fullmatch(ts) and NONCE.fullmatch(nonce)):
        return False
    if not hmac.compare_digest(nonce, sent_nonce):
        return False
    want = sign(key, response_message(sent_nonce, status, ts, body))
    return hmac.compare_digest(sig, want)


def timedatectl_synced():
    """True once systemd reports NTP sync. False if it can't be asked: a Pi
    has no real-time clock, and an unsynced clock may be days behind."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "yes"


class AuthError(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Auth:
    """The request check. `now` and `ntp_synced` are injectable so the tests
    can run a clock that starts in the past and then syncs."""

    def __init__(self, key, now=time.time, ntp_synced=timedatectl_synced):
        self.key = key
        self.now = now
        self.ntp_synced = ntp_synced
        self.floor = None
        self._checked_at = None
        self._seen = {}  # nonce -> its timestamp
        self.recheck_secs = 1.0  # how often to ask timedatectl before first sync
        self._watching = False
        self._mu = threading.Lock()

    def _synced_floor(self):
        """The floor, taken the first time the clock reports sync -- not at
        process start, when fake-hwclock may have left it far behind. It is
        the next whole second, so a request signed before the restart
        second can't be replayed. One signed up to SKEW_SECS ahead of this
        host's clock still could be: the floor bounds the replay window, it
        doesn't close it. Once watch() is running, only the watcher takes
        it. timedatectl runs outside _mu."""
        if self.floor is not None or self._watching:
            return self.floor
        with self._mu:
            t = time.monotonic()
            if self._checked_at is not None and t - self._checked_at < self.recheck_secs:
                return None
            self._checked_at = t
        return self._take_floor()

    def _take_floor(self):
        if self.ntp_synced():
            with self._mu:
                if self.floor is None:
                    self.floor = int(self.now()) + 1
        return self.floor

    def watch(self):
        """Take the floor the moment the clock syncs, from a background
        thread, rather than inside the first request's check: a request
        signed a second before that check would otherwise land under it."""
        self._watching = True

        def run():
            while self._take_floor() is None:
                time.sleep(self.recheck_secs)

        threading.Thread(target=run, name="ntp-floor", daemon=True).start()

    def check(self, method, path, headers, body):
        """Return the request's nonce, or raise AuthError."""
        ts = headers.get("X-Toppingd-Timestamp") or ""
        nonce = headers.get("X-Toppingd-Nonce") or ""
        sig = headers.get("X-Toppingd-Signature") or ""
        # Format first: compare_digest raises on a non-ASCII str.
        if not (TIMESTAMP.fullmatch(ts) and NONCE.fullmatch(nonce) and SIGNATURE.fullmatch(sig)):
            raise AuthError(401, "missing or malformed signature headers")
        floor = self._synced_floor()
        with self._mu:
            if floor is None:
                raise AuthError(503, "the clock is not NTP-synchronised yet; nothing is accepted")
            want = sign(self.key, request_message(method, path, ts, nonce, body))
            if not hmac.compare_digest(sig, want):
                raise AuthError(401, "bad signature")
            now, t = self.now(), int(ts)
            if abs(now - t) > SKEW_SECS:
                raise AuthError(401, f"timestamp is more than {SKEW_SECS} s off this host's clock")
            if t < floor:
                raise AuthError(401, "timestamp is from before this agent's clock synchronised")
            self._seen = {n: s for n, s in self._seen.items() if now - s <= SKEW_SECS}
            if nonce in self._seen:
                raise AuthError(401, "nonce already used")
            self._seen[nonce] = t
        return nonce


# --- the read guard -----------------------------------------------------------


class ReadOnlyHandle:
    """Wraps the opened hid.Device: every frame goes through write(), which
    refuses -- before anything reaches the wire -- any frame that isn't a
    readNack in CONFIRMED_READS for this model, and anything in NEVER_REGS.
    devstate's readers write to the handle directly, so this is the agent's
    send path."""

    def __init__(self, h, dev_key):
        self._h = h
        self.dev_key = dev_key
        self.sent = []

    def write(self, data):
        data = bytes(data)
        # The dx1 readers prefix report id 0 (devstate._prefixed).
        f = data[1:] if data[:1] == b"\x00" and data[1:3] == b"\x22\x33" else data
        if len(f) < 7 or f[:2] != b"\x22\x33":
            raise Refused(f"refusing an unrecognised frame: {data.hex(' ')}")
        opcode, reg_sub = f[2], (f[5], f[6])
        if reg_sub in NEVER_REGS:
            raise Refused(f"refusing {reg_sub[0]:02x} {reg_sub[1]:02x}: in the NEVER set")
        if opcode != READ_NACK:
            raise Refused(
                f"refusing opcode {opcode:02x} to {reg_sub[0]:02x} {reg_sub[1]:02x}: "
                "this agent is read-only"
            )
        if any(f[7:11]):
            # On the DX1 II an unsafe readNack writes its data field; the
            # readers here always send 0.
            raise Refused(f"refusing a readNack with a non-zero data field: {data.hex(' ')}")
        if reg_sub not in CONFIRMED_READS.get(self.dev_key, ()):
            raise Refused(
                f"refusing a readNack to {reg_sub[0]:02x} {reg_sub[1]:02x} on "
                f"{self.dev_key}: not in CONFIRMED_READS"
            )
        self.sent.append(data)
        return self._h.write(data)

    def read(self, *a, **kw):
        return self._h.read(*a, **kw)

    def close(self):
        self._h.close()


def volume_writable(key):
    """Whether a volume write may be planned for this model: confirmed in
    DEVICES, read through GetSettings (where volumeStep comes from), and
    with both of its frames in CONFIRMED_WRITES. Today only the DX5 II."""
    spec = DEVICES.get(key) or {}
    return (
        spec.get("status") == "confirmed"
        and read_path(key) == "get_settings"
        and VOLUME_WRITES <= CONFIRMED_WRITES.get(key, frozenset())
    )


def check_agent_writes(key, frames):
    """The agent's write gate, on every model whatever its status: the NEVER
    set first, then only write frames in CONFIRMED_WRITES. In dry-run these
    frames are returned, not sent; ReadOnlyHandle would refuse them anyway."""
    allowed = CONFIRMED_WRITES.get(key, frozenset())
    for f in frames:
        reg, sub = f.reg_sub
        if f.reg_sub in NEVER_REGS:
            raise Refused(f"refusing {reg:02x} {sub:02x}: in the NEVER set")
        if f.opcode != 0x20 or f.reg_sub not in allowed:
            raise Refused(
                f"refusing opcode {f.opcode:02x} to {reg:02x} {sub:02x} on {key}: "
                "not in CONFIRMED_WRITES"
            )


# --- identity and the per-model read paths ------------------------------------

READ_PATHS = {
    "dx5ii": "get_settings",
    "dx1ii": "dx1_read_safe",
}
NO_READ_REASON = "not read: no confirmed read path for this model"
# The fields the room card shows, in the plan's vocabulary. A model reports
# each one from its read path, from state.json ("written") or as unknown.
FIELDS = ("volume_db", "power", "mute", "gain", "input", "output", "preset", "preamp_db")


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.UTC).isoformat(timespec="seconds")


def _device(value, at, **extra):
    return {"value": value, "source": "device", "at": _iso(at), **extra}


def _unknown(reason):
    return {"value": None, "source": "unknown", "at": None, "reason": reason}


def read_path(dev_key):
    """The model's read path, or None. A path needs both an entry here and
    CONFIRMED_READS, so emptying the table also stops the read."""
    path = READ_PATHS.get(dev_key)
    return path if path and CONFIRMED_READS.get(dev_key) else None


def identify(configured="auto"):
    """What is plugged in, from USB enumeration only -- no frames are sent.
    -> dict with "status": found | no_dac | unrecognised | several |
    different (a DEVICES key was configured and another model is attached)."""
    entries = find_devices()
    keys = sorted({e["key"] for e in entries if e["key"]})
    unknown = sorted({e["product"] for e in entries if not e["key"]})
    out = {"unrecognised": unknown}
    if not keys:
        out["status"] = "unrecognised" if unknown else "no_dac"
        return out
    if len(keys) > 1:
        out.update(status="several", keys=keys)
        return out
    (key,) = keys
    if len({e["serial"] for e in entries if e["key"] == key}) > 1:
        out.update(status="several", keys=keys)
        return out
    spec = DEVICES[key]
    product = next(e["product"] for e in entries if e["key"] == key)
    out.update(
        key=key,
        name=spec["name"],
        product=product,
        model_status=spec["status"],
    )
    if configured != "auto" and configured != key:
        out["status"] = "different"
        out["expected"] = configured
    else:
        out["status"] = "found"
        out["read_path"] = read_path(key)
    return out


def read_written(state_dir):
    """The "last written by toppingctl" values that can't be read back: the
    PEQ preset and the preamp (plan decision 9 and Q7). Never follows a
    symlink at state.json."""
    path = os.path.join(state_dir, "state.json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd) as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return {}, None
            st, at = json.load(fh), os.fstat(fh.fileno()).st_mtime
    except (OSError, ValueError):
        return {}, None
    return (st if isinstance(st, dict) else {}), at


def _written(st, at, key):
    # load_state's fallback source, which vol and gain save as-is: not a preset.
    if key not in st or (key == "source" and st[key] == "defaults (no cache yet)"):
        return _unknown("never written by toppingctl on this host")
    return {"value": st[key], "source": "written", "at": _iso(at), "label": "last written"}


def decode_dx5(rec, at):
    """GetSettings records -> the room card's fields plus every record."""
    by_name = {SETTINGS_FIELDS[i]: v for i, v in rec.items() if i in SETTINGS_FIELDS}

    def enum(field):
        v = by_name[field]
        return ENUMS[FIELD_ENUM[field]].get(v, f"?{v}")

    def field(name, fn):
        return _device(fn(), at) if name in by_name else _unknown(f"{name} not in the read")

    fields = {
        "power": field("powered", lambda: "on" if by_name["powered"] else "off"),
        "mute": field("muted", lambda: bool(by_name["muted"])),
        "gain": field("highGain", lambda: "high" if by_name["highGain"] else "low"),
        "input": field("inputType", lambda: enum("inputType")),
        "output": field("outputType", lambda: enum("outputType")),
    }
    # The raw volume unit follows volumeStep (measured 2026-08-27, cmd_vol).
    # Without it there is no scale, so no guess.
    if "volume" in by_name and "volumeStep" in by_name:
        step = 1.0 if by_name["volumeStep"] == 1 else 0.5
        fields["volume_db"] = _device(-by_name["volume"] * step, at, raw=by_name["volume"])
    else:
        fields["volume_db"] = _unknown("volume or volumeStep not in the read")
    settings = {}
    for i in sorted(rec):
        name = SETTINGS_FIELDS.get(i, f"idx{i}")
        settings[name] = rec[i]
    return fields, settings


def decode_dx1(st, at):
    blk, regs = st["block"], st["regs"]
    volumes = {
        name: _device(dx1_raw_to_db(blk[i]), at, raw=blk[i])
        for name, i in (
            ("hp", DX1_F_HP_VOL),
            ("lo", DX1_F_LO_VOL),
            ("lo_hp", DX1_F_LOHP_VOL),
            ("opt", DX1_F_OPT_VOL),
        )
        if i in blk
    }
    fields = {
        # One volume per output here, so there is no single figure to show.
        "volume_db": _unknown("this model has a volume per output; see volumes_db"),
        "gain": _unknown("write-only on this model: a readNack to it would write it"),
        "input": _unknown("write-only on this model: a readNack to it would write it"),
    }
    if DX1_F_MUTE in blk:
        names = {v: k for k, v in DX1_MUTE.items()}
        fields["mute"] = _device(names.get(blk[DX1_F_MUTE] & 3), at, raw=blk[DX1_F_MUTE])
    else:
        fields["mute"] = _unknown("mute not in the read")
    if 2 in blk:
        mask = blk[2] & 7
        outs = [n for bit, n in ((1, "lo"), (2, "hp"), (4, "opt")) if mask & bit]
        fields["output"] = _device(outs, at, raw=blk[2])
    else:
        fields["output"] = _unknown("output state not in the read")
    power = {1: "on", 2: "off"}.get(regs.get("state"))
    fields["power"] = (
        _device(power, at, raw=regs["state"]) if power else _unknown("state not in the read")
    )
    settings = {f"block_f{i}": v for i, v in sorted(blk.items())}
    settings.update({k: v for k, v in regs.items() if v is not None})
    return fields, settings, volumes


class Unavailable(Exception):
    """The device couldn't be used for this read. `status` is the room state
    the plan names: busy (423) or read_failed."""

    def __init__(self, status, message, http=200, holder=None, internal=False):
        super().__init__(message)
        self.internal = internal  # busy with another request to this agent
        self.status = status
        self.message = message
        self.http = http
        self.holder = holder


class BadRequest(Exception):
    pass


def _number(body, name):
    v = body.get(name)
    # json.loads accepts NaN and Infinity; a level can't be either.
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise BadRequest(f"{name} must be a number of dB")
    return float(v)


def _level(raw, step):
    # + 0.0 so raw 0 is 0.0 dB, not -0.0.
    return {"db": -raw * step + 0.0, "raw": raw, "volume_step_db": step}


class Agent:
    def __init__(
        self,
        room,
        configured_device="auto",
        state_dir="/var/lib/toppingctl",
        mode="dry-run",
        now=time.time,
    ):
        if mode not in MODES:
            raise ValueError(f"mode {mode!r}: this build only runs {', '.join(MODES)}")
        self.room = room
        self.configured_device = configured_device
        self.state_dir = state_dir
        self.mode = mode
        self.now = now  # the unlocks' clock; injectable for the tests
        self._hid = threading.Lock()  # one device operation at a time
        self._cache = None  # (state dict, time.time() of the read)
        # unlock_id -> expiry, in self.now() seconds. Memory only: a restart
        # drops every unlock, which is safe (plan Q6).
        self._unlocks = {}
        self._unlocks_mu = threading.Lock()

    def room_info(self):
        dac = identify(self.configured_device)
        key = dac.get("key") if dac["status"] == "found" else None
        return {
            "host": socket.gethostname().split(".")[0],
            "room": self.room,
            "mode": self.mode,
            "version": VERSION,
            "dac": dac,
            "read_path": read_path(key) if key else None,
            "device_lock": device_lock_status(),
            "capabilities": {
                # In dry-run "volume" means planned and returned, not sent.
                "writes": ["volume"] if key and volume_writable(key) else [],
                "reads": sorted(f"{r:02x} {s:02x}" for r, s in CONFIRMED_READS.get(key, ())),
            },
        }

    def state(self, fresh):
        if not fresh and self._cache:
            st, at = self._cache
            return 200, {**st, "cached": True, "age_s": round(time.time() - at, 1)}
        status, body, _ = self._read()
        return status, body

    # --- unlocks --------------------------------------------------------------

    def create_unlock(self):
        """POST /v1/unlocks. Sends nothing and needs no DAC: it only lets a
        volume write above -10 dB through for the next UNLOCK_SECS."""
        with self._unlocks_mu:
            now = self.now()
            self._unlocks = {u: t for u, t in self._unlocks.items() if t > now}
            uid = secrets.token_urlsafe(24)
            self._unlocks[uid] = now + UNLOCK_SECS
        return 200, {
            "unlock_id": uid,
            "expires_at": _iso(now + UNLOCK_SECS),
            "expires_in_s": UNLOCK_SECS,
        }

    def discard_unlock(self, uid):
        """DELETE /v1/unlocks/{id}: Cancel, leaving the room, signing out or
        unticking the box. Discarding an unknown or expired one is not an
        error: the UI may call it after the cap."""
        with self._unlocks_mu:
            found = self._unlocks.pop(uid, None) is not None
        return 200, {"unlock_id": uid, "discarded": found}

    def _unlock_problem(self, uid):
        """None if `uid` is a live unlock, else why it isn't. Checked when the
        write runs, not when the UI queued it."""
        if uid is None:
            return "no unlock_id was sent"
        with self._unlocks_mu:
            expiry = self._unlocks.get(uid)
            if expiry is None:
                return "the unlock is unknown, cancelled, or was discarded by a 409"
            if self.now() >= expiry:
                del self._unlocks[uid]
                return f"the unlock expired ({UNLOCK_SECS} s)"
        return None

    # --- volume ---------------------------------------------------------------

    def volume(self, body):
        """POST /v1/volume {target_db, expected_db, unlock_id?}: every volume
        change, undo included. Reads the device, then decides in the same
        operation (_volume_decision). Dry-run: the frames come back, unsent."""
        if not isinstance(body, dict):
            raise BadRequest("the body must be a JSON object")
        extra = set(body) - VOLUME_KEYS
        if extra:
            raise BadRequest(
                f"unknown keys {', '.join(sorted(extra))}: /v1/volume sets volume only; "
                "other changes go through /v1/plans"
            )
        target, expected = _number(body, "target_db"), _number(body, "expected_db")
        uid = body.get("unlock_id")
        if uid is not None and not (isinstance(uid, str) and UNLOCK_ID.fullmatch(uid)):
            raise BadRequest("unlock_id must be an id from POST /v1/unlocks")
        try:
            check_volume_range(target)
        except Refused as e:
            raise BadRequest(str(e)) from None
        dac = identify(self.configured_device)
        key = dac.get("key") if dac["status"] == "found" else None
        if key is None:
            return 503, {"error": "unavailable", "status": dac["status"], "dac": dac}
        if not volume_writable(key):
            return 403, {
                "error": "model_unverified",
                "reason": (
                    f"{DEVICES[key]['name']} is {DEVICES[key]['status']}: this agent writes "
                    "volume only on a model confirmed on hardware, and no setting changes that"
                ),
            }
        spec = DEVICES[key]
        status, st, decided = self._read(
            dac, then=lambda rec: self._volume_decision(spec, key, rec, target, expected, uid)
        )
        if decided is None:  # the read itself failed or was busy
            return (status if status != 200 else 503), {
                "error": st["status"],
                "reason": st.get("error") or st["fields"]["volume_db"].get("reason"),
                "state": st,
            }
        status, obj = decided
        if status == 409:
            obj["state"] = st
        return status, obj

    def _volume_decision(self, spec, key, rec, target, expected, uid):
        """The plan's rules, in its order: (a) compare-and-set on expected_db
        in raw units, (b) the -10 dB rule before and after quantising."""
        if REC_VOLUME not in rec or REC_VOLUME_STEP not in rec:
            raise RuntimeError("volume or volumeStep not in the read; nothing to compare with")
        step = 1.0 if rec[REC_VOLUME_STEP] == 1 else 0.5
        before_raw = rec[REC_VOLUME]
        before = _level(before_raw, step)
        expected_raw, expected_on_grid = volume_steps(expected, step)
        if expected_on_grid != expected or expected_raw != before_raw:
            # A 409 discards the unlock it carried, as Cancel does.
            discarded = uid is not None and self.discard_unlock(uid)[1]["discarded"]
            return 409, {
                "error": "changed",
                "reason": (
                    f"the device reads {before['db']:+.1f} dB, not the {expected:+.1f} dB "
                    "this change started from"
                ),
                "before": before,
                "unlock_discarded": discarded,
            }
        steps, actual = volume_steps(target, step)
        # "Quieter is always allowed": not louder than the fresh read, both
        # as asked and as quantised. More raw steps is more attenuation.
        quieter = target <= before["db"] and steps >= before_raw
        problem = None if quieter else self._unlock_problem(uid)
        try:
            # plan_volume runs check_loud on the requested level and on the
            # quantised one: the same -10 dB check the CLI's vol uses.
            frames = plan_volume(spec, target, step, allow_loud=quieter or problem is None)
        except Refused:
            return 403, {
                "error": "unlock_required",
                "reason": (
                    f"{actual:+.1f} dB is above {VOL_WARN_DB:+.0f} dB and louder than the "
                    f"device's {before['db']:+.1f} dB, so it needs a live unlock: {problem}"
                ),
                "before": before,
            }
        try:
            check_agent_writes(key, frames)
        except Refused as e:
            return 403, {"error": "refused", "reason": str(e), "before": before}
        after = _level(steps, step)
        return 200, {
            "mode": self.mode,
            "sent": False,
            "before": before,
            "after_predicted": after,
            "frames": [{"hex": f.data.hex(" "), "label": f.label} for f in frames],
            "loud_result": after["db"] > VOL_WARN_DB,
            # POST this back to /v1/volume to undo. For a burst of writes the
            # UI keeps the first write's target_db and the last's expected_db.
            "undo": {"target_db": before["db"], "expected_db": after["db"]},
        }

    def _base(self, dac, status, **extra):
        written, wat = read_written(self.state_dir) if dac.get("key") else ({}, None)
        fields = {name: _unknown(extra.pop("reason", NO_READ_REASON)) for name in FIELDS}
        fields["preset"] = _written(written, wat, "source")
        fields["preamp_db"] = _written(written, wat, "preamp_db")
        return {"room": self.room, "status": status, "dac": dac, "fields": fields, **extra}

    def _read(self, dac=None, then=None):
        """A fresh read -> (http status, state body, then's result or None).
        `then(rec)` runs on a GetSettings read under the same lock and open,
        so a write decided from the read can't be interleaved."""
        dac = dac or identify(self.configured_device)
        if dac["status"] != "found":
            reasons = {
                "no_dac": "no DAC found on this host",
                "unrecognised": "unrecognised DAC; read-only, and not read",
                "several": "several DACs attached; this agent drives one",
                "different": f"a different DAC than configured ({self.configured_device})",
            }
            # The cached read no longer describes the room; nor below.
            self._cache = None
            return 200, self._base(dac, dac["status"], reason=reasons[dac["status"]]), None
        key = dac["key"]
        path = read_path(key)
        if path is None:
            self._cache = None
            return 200, self._base(dac, "not_read", reason=NO_READ_REASON), None
        try:
            at, fields, settings, volumes, extra = self._device_read(key, path, then)
        except Unavailable as e:
            # Busy with this agent's own read says nothing new about the
            # room, so the last read stays servable, with its age. Another
            # process holding the DAC (the CLI, perhaps writing) or a failed
            # read does: the cache goes.
            if not e.internal:
                self._cache = None
            body = self._base(dac, e.status, reason=e.message, error=e.message)
            if e.holder:
                body["holder"] = e.holder
            return e.http, body, None
        body = self._base(dac, "read", read_path=path, at=_iso(at))
        body["fields"].update(fields)
        body["settings"] = settings
        if volumes is not None:
            body["volumes_db"] = volumes
        self._cache = (body, at)
        return 200, {**body, "cached": False, "age_s": 0.0}, extra

    def _device_read(self, key, path, then=None):
        deadline = time.monotonic() + AGENT_LOCK_WAIT
        if not self._hid.acquire(timeout=AGENT_LOCK_WAIT):
            holder = f"{PROG} (pid {os.getpid()})"
            raise Unavailable(
                "busy", toppingctl.busy_message(holder), http=423, holder=holder, internal=True
            )
        try:
            with self._device_lock(max(0.0, deadline - time.monotonic())):
                h = self._open(key)
                try:
                    at = time.time()
                    if path == "get_settings":
                        rec = devstate.read_settings(key, secs=READ_SECS, h=h)
                        decoded = decode_dx5(rec, at)
                        return (at, *decoded, None, then(rec) if then else None)
                    if then:
                        raise RuntimeError(f"no write is planned from the {path} read")
                    st = devstate.dx1_state(key, skip_peq_dump=True, h=h)
                    if not st["block"]:
                        raise RuntimeError("the 0x810a block read returned nothing")
                    return (at, *decode_dx1(st, at), None)
                except Refused as e:
                    raise Unavailable("read_failed", f"refused: {e}") from None
                except RuntimeError as e:
                    raise Unavailable("read_failed", str(e)) from None
                finally:
                    h.close()
        finally:
            self._hid.release()

    @contextlib.contextmanager
    def _device_lock(self, wait):
        """The shared device lock, once per operation. A read may run without
        it when the lock file can't be opened, as the CLI's reads do."""
        try:
            toppingctl.acquire_device_lock(wait, prog=PROG)
            held = True
        except DeviceBusy as e:
            raise Unavailable(
                "busy", toppingctl.busy_message(e.holder), http=423, holder=e.holder
            ) from None
        except OSError as e:
            log(f"warning: {_lock_problem(e)}")
            held = False
        try:
            yield
        finally:
            if held:
                toppingctl.release_device_lock()

    def _open(self, key):
        """Open through open_checked (the model check), retrying as
        read_settings does: the vendor app can hold the only handle."""
        last = None
        for attempt in range(OPEN_ATTEMPTS):
            try:
                return ReadOnlyHandle(open_checked(key), key)
            except SystemExit as e:  # Device._check_model exits on a mismatch
                raise Unavailable("read_failed", str(e.code)) from None
            except Exception as e:
                last = e
                if attempt < OPEN_ATTEMPTS - 1:
                    time.sleep(OPEN_RETRY_SECS)
        raise Unavailable(
            "busy", f"could not open the DAC ({last}); another app may hold it", http=423
        )


def _lock_problem(e):
    return f"cannot use the device lock {toppingctl.lock_path()}: {e.strerror or e}"


def device_lock_status():
    """Whether the shared device lock file can be opened right now. Reads run
    without it when it can't (as the CLI's do), so /v1/room says so rather
    than leaving it to a log line. Opens and closes; never takes the lock."""
    try:
        fd, _ = toppingctl._open_lock(toppingctl.lock_path())
    except OSError as e:
        return {"path": toppingctl.lock_path(), "ok": False, "error": _lock_problem(e)}
    os.close(fd)
    return {"path": toppingctl.lock_path(), "ok": True}


# --- HTTP ---------------------------------------------------------------------


def log(msg):
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"{PROG}/{VERSION}"
    sys_version = ""
    # An idle or slow client can't hold a thread for ever.
    timeout = 10
    # Set by make_server.
    agent = None
    auth = None

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, status, obj, nonce=None):
        body = json.dumps(obj, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if nonce is not None:
            ts = str(int(self.auth.now()))
            sig = sign(self.auth.key, response_message(nonce, status, ts, body))
            self.send_header("X-Toppingd-Nonce", nonce)
            self.send_header("X-Toppingd-Timestamp", ts)
            self.send_header("X-Toppingd-Signature", sig)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._send(400, {"error": "bad_request", "reason": "bad Content-Length"})
        body = self.rfile.read(length) if length else b""
        try:
            nonce = self.auth.check(self.command, self.path, self.headers, body)
        except AuthError as e:
            # Unsigned: signing a reply for a request that failed the check
            # would sign for a nonce of the sender's choosing.
            error = "clock_unsynced" if e.status == 503 else "unauthorized"
            return self._send(e.status, {"error": error, "reason": e.reason})
        url = urlsplit(self.path)
        prefix = "/v1/unlocks/"
        if url.path.startswith(prefix) and UNLOCK_ID.fullmatch(url.path[len(prefix) :]):
            uid = url.path[len(prefix) :]
            methods = {"DELETE": lambda q, b: self.agent.discard_unlock(uid)}
        else:
            methods = {
                "/v1/room": {"GET": self._room},
                "/v1/state": {"GET": self._state},
                "/v1/volume": {"POST": lambda q, b: self.agent.volume(_json(b))},
                "/v1/unlocks": {"POST": self._unlock},
            }.get(url.path)
        if methods is None:
            return self._send(404, {"error": "not_found"}, nonce)
        if self.command not in methods:
            return self._send(
                405,
                {"error": "method_not_allowed", "reason": f"{url.path} takes {', '.join(methods)}"},
                nonce,
            )
        try:
            status, obj = methods[self.command](parse_qs(url.query), body)
        except BadRequest as e:
            status, obj = 400, {"error": "bad_request", "reason": str(e)}
        except Exception as e:  # report, don't drop the connection unsigned
            log(f"error on {url.path}: {e!r}")
            status, obj = 500, {"error": "internal", "reason": str(e)}
        self._send(status, obj, nonce)

    def _room(self, query, body):
        return 200, self.agent.room_info()

    def _state(self, query, body):
        return self.agent.state(fresh=query.get("fresh") == ["1"])

    def _unlock(self, query, body):
        if _json(body) != {}:
            raise BadRequest("POST /v1/unlocks takes {}")
        return self.agent.create_unlock()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


def _json(body):
    """A request body as JSON; empty is {}."""
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        raise BadRequest("the body is not valid JSON") from None


def make_server(address, agent, auth):
    handler = type("BoundHandler", (Handler,), {"agent": agent, "auth": auth})
    return http.server.ThreadingHTTPServer(address, handler)


# --- config -------------------------------------------------------------------


class ConfigError(Exception):
    pass


def load_key(path):
    """The host's HMAC key: the file's contents, stripped (e.g. the output of
    `openssl rand -hex 32`). Refused if anyone but its owner can read it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise ConfigError(f"cannot read the key file {path}: {e.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"the key file {path} is not a regular file")
        if st.st_mode & 0o077:
            raise ConfigError(f"the key file {path} is readable by others (chmod 600 {path})")
        key = fh.read().strip()
    if len(key) < MIN_KEY_BYTES:
        raise ConfigError(f"the key in {path} is shorter than {MIN_KEY_BYTES} bytes")
    return key


def load_config(path):
    try:
        with open(path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must be a mapping")
    unknown = set(cfg) - CONFIG_KEYS
    if unknown:
        raise ConfigError(f"unknown keys in {path}: {', '.join(sorted(unknown))}")
    mode = cfg.get("mode", "dry-run")
    if mode not in MODES:
        raise ConfigError(
            f"mode {mode!r} isn't available: this build only runs dry-run, which sends "
            "no writes (live waits for the front-panel checks of plan step 4)"
        )
    device = cfg.get("device", "auto")
    if not isinstance(device, str) or (device != "auto" and device not in DEVICES):
        raise ConfigError(f"unknown device {device!r}; auto or one of {', '.join(DEVICES)}")
    host, _, port = str(cfg.get("listen", "0.0.0.0:8740")).rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"listen must be host:port, not {cfg.get('listen')!r}")
    if "key_file" not in cfg:
        raise ConfigError(f"{path} needs key_file")
    return {
        "listen": (host, int(port)),
        "room": cfg.get("room") or socket.gethostname().split(".")[0],
        "device": device,
        "mode": mode,
        "key": load_key(cfg["key_file"]),
        "state_dir": cfg.get("state_dir", "/var/lib/toppingctl"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    a = ap.parse_args()
    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        sys.exit(f"{PROG}: {e}")
    agent = Agent(cfg["room"], cfg["device"], cfg["state_dir"], cfg["mode"])
    auth = Auth(cfg["key"])
    auth.watch()
    try:
        server = make_server(cfg["listen"], agent, auth)
    except OSError as e:
        sys.exit(f"{PROG}: cannot listen on {cfg['listen'][0]}:{cfg['listen'][1]}: {e.strerror}")
    host, port = cfg["listen"]
    log(f"{VERSION} serving room {cfg['room']} on {host}:{port}, mode {cfg['mode']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
