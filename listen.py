#!/usr/bin/env python3
"""Listen to what the DAC sends back, and optionally ask it something first.

Until now this tool has been write-only: `show` reports what we last wrote,
because nobody knew where the device's replies arrived. The vendor bundle
answers that -- byte 2 of every frame is a protocolType, and we had only ever
sent writeNack (0x20). Sending readNack (0x10) is what makes the device answer.

Note the DX5 II tags its *replies* writeNack (0x20) too; readAck (0x11) is in the
vendor's constant table but this device never emits it. Measured, not assumed.

Usage:
    ./listen.py                        # passive, 10s
    ./listen.py --read 0x710c          # ask GetSettings, then listen
    ./listen.py --read GetSettings     # same, by vendor name
    ./listen.py --secs 30 --raw        # longer, show undecodable frames too

Sends nothing except readNack, which is what the vendor app itself issues.
"""
import argparse
import sys
import time

from toppingctl import frame, open_checked
from vendor_commands import COMMANDS, PROTOCOL

READ_NACK = 0x10

# A read of these should be inert, but the cost of being wrong is losing stored
# presets or a firmware state machine, so they are simply not addressable here.
NEVER = {"SaveC1", "SaveC2", "FactoryReset", "FirmwareUpdate"}


def resolve(token):
    if token.lower().startswith("0x"):
        return int(token, 16)
    hits = [c for c, n in COMMANDS.items() if n.lower() == token.lower()]
    if not hits:
        sys.exit(f"unknown command {token!r}; try a hex cmd like 0x710c")
    return hits[0]


def decode(buf):
    """Frame is 22 33 <proto> 01 <b4> <reg> <sub> <int32 BE> <ck ck> 66 77 00."""
    if len(buf) < 15 or buf[0] != 0x22 or buf[1] != 0x33:
        return None
    proto, b4, reg, sub = buf[2], buf[4], buf[5], buf[6]
    val = int.from_bytes(buf[7:11], "big")
    cmd = (reg << 8) | sub
    return {
        "proto": PROTOCOL.get(proto, f"0x{proto:02x}"),
        "b4": b4,
        "ascii": "".join(chr(c) if 32 <= c < 127 else "." for c in buf[7:11]),
        "cmd": cmd,
        "name": COMMANDS.get(cmd, f"0x{reg:02x}/{sub:02x}"),
        "value": val,
        "signed": val - (1 << 32) if val >> 31 else val,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--read", metavar="CMD", help="send a readNack first, hex or vendor name")
    ap.add_argument("--secs", type=float, default=10.0)
    ap.add_argument("--raw", action="store_true", help="also print frames that do not decode")
    ap.add_argument("--device", default="dx5ii")
    a = ap.parse_args()

    h = open_checked(a.device)

    if a.read:
        cmd = resolve(a.read)
        name = COMMANDS.get(cmd, "?")
        if name in NEVER:
            h.close()
            sys.exit(f"refusing to address {name}: not worth the risk from a probe")
        reg, sub = cmd >> 8, cmd & 0xFF
        print(f"-> readNack  0x{cmd:04x}  {name}")
        h.write(frame(reg, sub, 0, opcode=READ_NACK))

    print(f"listening {a.secs:g}s ...\n")
    t0, seen = time.time(), 0
    while time.time() - t0 < a.secs:
        try:
            buf = h.read(64, timeout=200)
        except Exception as e:
            print(f"  read error: {e}")
            break
        if not buf:
            continue
        seen += 1
        d = decode(bytes(buf))
        dt = time.time() - t0
        if d:
            print(f"  {dt:6.2f}s  {d['proto']:<9} idx={d['b4']:<3} {d['name']:<22} "
                  f"= {d['signed']:<11} 0x{d['value']:08x}  |{d['ascii']}|")
        elif a.raw:
            print(f"  {dt:6.2f}s  RAW {bytes(buf).hex(' ')}")
    h.close()
    print(f"\n{seen} report(s) in {a.secs:g}s")
    if not seen:
        print("nothing arrived. the device may be sending only on change, or the")
        print("read may need a different protocolType. try --secs 30, or twiddle")
        print("the volume knob while this runs to force traffic.")


if __name__ == "__main__":
    main()
