from __future__ import annotations

import os
import sys
from pathlib import Path


DEVICE = Path(
    "/dev/input/by-id/"
    "usb-SteelSeries_Arctis_Nova_7X-if03-hidraw"
)

REPORT_SIZE = 64
EQ_COMMAND = 0x33
EQ_BASELINE = 0x14  # 20 decimal = flat

PRESETS = {
    "flat": [
        0, 0, 0, 0, 0,
        0, 0, 0, 0, 0,
    ],

    # Integer equivalents of the Nova 7 bass-style curve.
    "bass": [
        3, 5, 4, 1, -2,
        -2, -1, -1, -1, -1,
    ],

    "focus": [
        -5, -4, -1, -4, -3,
        4, 6, -4, 0, 0,
    ],

    "smiley": [
        3, 3, 1, -2, -4,
        -4, -3, 1, 3, 4,
    ],

    "cut": [
    -10, -10, -10, -10, -10,
    -10, -10, -10, -10, -10,
    ],

}


def build_eq_report(gains: list[int]) -> bytes:
    if len(gains) != 10:
        raise ValueError("Nova 7X requires exactly 10 EQ bands")

    report = bytearray(REPORT_SIZE)

    # Byte 0 is the unnumbered HID report ID.
    report[0] = 0x00

    # SteelSeries Nova EQ command.
    report[1] = EQ_COMMAND

    for i, gain in enumerate(gains):
        if not -10 <= gain <= 10:
            raise ValueError(
                f"Band {i + 1}: gain must be between -10 and +10"
            )

        report[i + 2] = EQ_BASELINE + gain

    # Terminator after the ten bands.
    report[12] = 0x00

    return bytes(report)


def set_eq(gains: list[int]) -> None:
    if not DEVICE.exists():
        raise SystemExit(
            f"Nova 7X interface 3 not found:\n{DEVICE}"
        )

    report = build_eq_report(gains)

    print("Sending:")
    print(" ".join(f"{b:02X}" for b in report[:13]))

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

    if written != REPORT_SIZE:
        raise RuntimeError(
            f"Only wrote {written}/{REPORT_SIZE} bytes"
        )


def main() -> None:
    if len(sys.argv) != 2:
        choices = " | ".join(PRESETS)
        raise SystemExit(
            f"Usage: python eq_probe.py [{choices}]"
        )

    preset = sys.argv[1].lower()

    if preset not in PRESETS:
        raise SystemExit(
            f"Unknown preset: {preset}"
        )

    print(f"Applying EQ preset: {preset}")

    set_eq(
        PRESETS[preset]
    )

    print("Done.")


if __name__ == "__main__":
    main()
