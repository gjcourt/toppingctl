#!/usr/bin/env python3
"""Live VU and FFT meters, read from the device.

The DX5 II pushes these unsolicited -- there is no subscribe register and the
vendor app never asks for them. Payload bytes are LITTLE-endian *within* each
big-endian int32, which is the trap: byte 4 of the frame is a record index and
the four payload bytes land at 4*index, LSB first.

    ./meters.py            # 10s of both
    ./meters.py --secs 30
"""
import argparse
import sys
import time

from toppingctl import DEVICES

VU, FFT = 0x7130, 0x7131


def _u8x4(v):
    """Frame int32 -> its four payload bytes, little-endian."""
    return [v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF]


def _i16(lo, hi):
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


def _i8(b):
    return b - 256 if b > 127 else b


def _bar(db, width=32, floor=-60):
    if db is None or db <= floor:
        n = 0
    else:
        n = round(min(1.0, (db - floor) / -floor) * width)
    return "#" * n + "." * (width - n)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--device", default="dx5ii")
    a = ap.parse_args()

    import hid
    spec = DEVICES[a.device]
    h = hid.Device(spec["vid"], spec["pid"])
    vu_buf, fft_buf = {}, {}
    counts = {VU: 0, FFT: 0, "other": 0}
    last = 0.0
    t0 = time.time()
    try:
        while time.time() - t0 < a.secs:
            try:
                b = h.read(64, timeout=200)
            except Exception:
                continue
            if not b or len(b) < 15 or b[0] != 0x22 or b[1] != 0x33:
                continue
            cmd = (b[5] << 8) | b[6]
            idx = b[4]
            val = int.from_bytes(bytes(b[7:11]), "big")
            if cmd == VU:
                counts[VU] += 1
                for i, byte in enumerate(_u8x4(val)):
                    vu_buf[4 * idx + i] = byte
                if all(i in vu_buf for i in range(8)) and time.time() - last > 0.15:
                    last = time.time()
                    left = _i16(vu_buf[0], vu_buf[1])
                    right = _i16(vu_buf[2], vu_buf[3])
                    print(f"  VU  L {left:4d} dB |{_bar(left)}|   "
                          f"R {right:4d} dB |{_bar(right)}|")
            elif cmd == FFT:
                counts[FFT] += 1
                for i, byte in enumerate(_u8x4(val)):
                    fft_buf[4 * idx + i] = byte
                if all(i in fft_buf for i in range(30)) and time.time() - last > 0.15:
                    last = time.time()
                    bands = [_i8(fft_buf[i]) for i in range(30)]
                    spark = "".join(" ▁▂▃▄▅▆▇█"[max(0, min(8, round((d + 60) / 60 * 8)))]
                                    for d in bands)
                    print(f"  FFT [{spark}]")
                    fft_buf.clear()
            else:
                counts["other"] += 1
    finally:
        h.close()
    print(f"\n  VU frames {counts[VU]}   FFT frames {counts[FFT]}   "
          f"other {counts['other']}")
    if not counts[VU] and not counts[FFT]:
        print("  nothing pushed. play audio, and note the meters may only stream")
        print("  when the display is on a VU or FFT page (see homePage).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
