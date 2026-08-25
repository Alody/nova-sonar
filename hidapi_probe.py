import sys
import hid


VID = 0x1038
PID = 0x22AD
INTERFACE = 3
REPORT_SIZE = 64


def find_interface3():
    devices = hid.enumerate(VID, PID)

    print("Nova 7X HID interfaces:")

    for d in devices:
        print(
            f"interface={d.get('interface_number')} "
            f"usage_page={d.get('usage_page')} "
            f"path={d.get('path')!r}"
        )

    matches = [
        d for d in devices
        if d.get("interface_number") == INTERFACE
    ]

    if not matches:
        raise RuntimeError("Could not find Nova 7X HID interface 3")

    return matches[0]


def send(command: list[int]):
    info = find_interface3()

    report = bytearray(REPORT_SIZE)

    # HIDAPI dummy report ID for an unnumbered report.
    report[0] = 0x00

    for i, byte in enumerate(command, start=1):
        report[i] = byte

    dev = hid.device()

    try:
        dev.open_path(info["path"])

        print(
            "Sending:",
            " ".join(f"{b:02X}" for b in report[:8])
        )

        written = dev.write(bytes(report))

        print(f"hid_write returned: {written}")

    finally:
        dev.close()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python hidapi_probe.py [led-off|led-max]"
        )

    mode = sys.argv[1]

    if mode == "led-off":
        # Nova 7 mic mute LED brightness:
        # command AE, level 0
        send([0xAE, 0x00])

    elif mode == "led-max":
        # command AE, level 3
        send([0xAE, 0x03])

    else:
        raise SystemExit("Unknown mode")


if __name__ == "__main__":
    main()
