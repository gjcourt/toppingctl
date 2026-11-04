#!/usr/bin/env python3
"""Regenerate dry_run_golden.json: stdout, stderr and exit code of every
--dry-run command CI runs, plus the refusals and remaining commands CI skips.

test_offline.DryRunGoldenTest replays each command and requires
byte-identical results. The committed file was captured from main at be489ce,
before the plan_*/Refused refactor (TOPPINGCTL-3), so it pins the refactor to
the old behaviour.

Only regenerate on purpose -- a new preset, or a deliberate output change --
and say so in the PR, because regenerating erases exactly the diff the test
exists to show. Run from the repo root:

    python3 fixtures/make_dry_run_golden.py
"""

import glob
import json
import os
import shlex
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "fixtures", "dry_run_golden.json")

# The dry-run block of .github/workflows/ci.yml, in order.
CI = [
    "toppingctl.py --dry-run vol -30",
    "toppingctl.py --dry-run gain on",
    "toppingctl.py --dry-run power off",
    "toppingctl.py --dry-run flat",
    "toppingctl.py --dry-run apply presets/bass1.json",
    "PRESETS_TXT",
    "smoke.py --dry-run",
    "probe.py 0x04 0 --dry-run",
    "setctl.py --list",
    "toppingctl.py --device dx1ii --dry-run vol -30",
    "toppingctl.py --device dx1ii --dry-run vol -30 --target hp",
    "toppingctl.py --device dx1ii --dry-run mute off",
    "toppingctl.py --device dx1ii --dry-run mute analog",
    "toppingctl.py --device dx1ii --dry-run mute opt",
    "toppingctl.py --device dx1ii --dry-run mute both",
    "toppingctl.py --device dx1ii --dry-run gain on",
    "toppingctl.py --device dx1ii --dry-run power off",
    "toppingctl.py --device dx1ii --dry-run input opt",
    "toppingctl.py --device dx1ii --dry-run filter f3",
    "toppingctl.py --device dx1ii --dry-run eq off",
    "toppingctl.py --device dx1ii --dry-run eq on",
    "toppingctl.py --device dx1ii --dry-run eq 2",
    "toppingctl.py --device dx1ii --dry-run preamp -6",
    "toppingctl.py --device dx1ii --dry-run flat",
    "toppingctl.py --device dx1ii --dry-run apply presets/bass1.json",
    "scenes.py --device dx1ii recall c3 --dry-run",
    "toppingctl.py --device dx5ii --dry-run vol -30",
    "toppingctl.py --device dx5ii --dry-run vol -30 --target hp",
    "toppingctl.py --device dx5ii --dry-run gain on",
    "toppingctl.py --device dx5ii --dry-run power off",
    "toppingctl.py --device dx5ii --dry-run preamp -6",
    "toppingctl.py --device dx5ii --dry-run flat",
    "toppingctl.py --device dx5ii --dry-run apply presets/bass1.json",
]

# Not in CI: every refusal reachable under --dry-run, and the remaining
# commands, so the exit path and its message are pinned as well as the frames.
EXTRA = [
    "toppingctl.py --dry-run vol -5",
    "toppingctl.py --dry-run vol -5 --force",
    "toppingctl.py --dry-run vol 3",
    "toppingctl.py --dry-run --vol-step 1.0 vol -30",
    "toppingctl.py --dry-run preamp 3",
    "toppingctl.py --dry-run preamp -50",
    "toppingctl.py --dry-run gain off",
    "toppingctl.py --dry-run power on",
    "toppingctl.py --dry-run mute off",
    "toppingctl.py --dry-run input opt",
    "toppingctl.py --dry-run filter f3",
    "toppingctl.py --dry-run eq off",
    "toppingctl.py --dry-run apply presets/bass1-plus-1k-probe.json",
    "toppingctl.py --device dx1ii --dry-run vol -5",
    "toppingctl.py --device dx1ii --dry-run vol -5 --force",
    "toppingctl.py --device dx1ii --dry-run vol -30 --target lo",
    "toppingctl.py --device dx1ii --dry-run filter f9",
    "toppingctl.py --device dx1ii --dry-run filter x",
    "toppingctl.py --device dx1ii --dry-run eq 4",
    "toppingctl.py --device dx1ii --dry-run eq 1",
    "toppingctl.py --device dx1ii --dry-run eq 3",
    "toppingctl.py --device dx1ii --dry-run input usb",
    "toppingctl.py --device dx1ii --dry-run power on",
    "toppingctl.py --device dx1ii --dry-run gain off",
    "toppingctl.py --device d90iii --dry-run vol -30",
    "toppingctl.py --device d90iii --dry-run flat",
    "toppingctl.py --device d90iii --dry-run apply presets/bass1.json",
    "scenes.py recall c1 --dry-run",
    "scenes.py recall c3 --dry-run",
    "scenes.py --device d90iii recall c1 --dry-run",
]


def commands():
    out = []
    for c in CI + EXTRA:
        if c == "PRESETS_TXT":
            presets = sorted(glob.glob("presets/*.txt", root_dir=ROOT))
            out += [f"toppingctl.py --dry-run apply {p}" for p in presets]
        else:
            out.append(c)
    return out


def run(cmdline):
    """One command as CI runs it, from the repo root."""
    p = subprocess.run(
        [sys.executable, *shlex.split(cmdline)], capture_output=True, text=True, cwd=ROOT
    )
    return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


if __name__ == "__main__":
    golden = {c: run(c) for c in commands()}
    with open(GOLDEN, "w") as fh:
        json.dump(golden, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {len(golden)} commands to {GOLDEN}")
