#!/usr/bin/env python3
"""Read the DX5 II's actual state. Not a cache -- the device is queried."""
import time

import hid

from toppingctl import DEVICES, frame
from vendor_commands import ENUMS, FIELD_ENUM, SETTINGS_FIELDS

spec = DEVICES["dx5ii"]
h = hid.Device(spec["vid"], spec["pid"])
h.write(frame(0x71, 0x0c, 0, opcode=0x10))          # readNack GetSettings

rec, t0 = {}, time.time()
while time.time() - t0 < 3.0:
    b = h.read(64, timeout=200)
    if not b or len(b) < 15 or b[0] != 0x22 or b[1] != 0x33:
        continue
    if b[5] == 0x71 and b[6] == 0x0c:
        rec[b[4]] = int.from_bytes(bytes(b[7:11]), "big")
h.close()

name = b"".join(rec.get(i, 0).to_bytes(4, "big")[::-1] for i in range(1, 9))
print(f"device      {name.split(b'\\x00')[0].decode('ascii', 'replace').strip()}")
print(f"records     {len(rec)}  (firmware >= 2.40 adds 48..51: "
      f"{'yes' if max(rec, default=0) >= 48 else 'no'})\n")
for i in sorted(rec):
    if i in range(1, 9):
        continue
    f = SETTINGS_FIELDS.get(i, f"idx{i}")
    v = rec[i]
    extra = ""
    if f == "volume":
        extra = f"   = {-v/2:+.1f} dB"
    elif f.endswith("Mask"):
        extra = f"   = 0b{v:b}  ({bin(v).count('1')} options)"
    elif f in ("powered", "muted", "highGain", "bluetoothAptx", "remoteEnabled"):
        extra = f"   = {bool(v)}"
    elif f in FIELD_ENUM:
        extra = f"   = {ENUMS[FIELD_ENUM[f]].get(v, '?')}"
    print(f"  [{i:2d}] {f:<32} {v:<6}{extra}")
