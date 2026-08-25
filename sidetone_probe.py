import os
import sys
from pathlib import Path


DEVICE = Path(
    "/dev/input/by-id/"
    "usb-SteelSeries_Arctis_Nova_7X-if03-hidraw"
)

REPORT_SIZE = 64


def set_sidetone(level: int):
    if not 0 <= level <= 3:
        raise ValueError("Sidetone must be 0-3")

    report = bytearray(REPORT_SIZE)

    report[0] = 0x00
    report[1] = 0x39
    report[2] = level

    print(
        "Sending:",
        " ".join(f"{b:02X}" for b in report[:8])
    )

    fd = os.open(
        DEVICE,
        os.O_WRONLY,
    )

    try:
        written = os.write(
            fd,
            report,
        )
    finally:
        os.close(fd)

    print(f"Wrote {written} bytes")


if len(sys.argv) != 2:
    raise SystemExit(
        "Usage: python sidetone_probe.py [0-3]"
    )

set_sidetone(int(sys.argv[1]))
