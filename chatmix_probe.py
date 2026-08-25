from pathlib import Path


DEVICE = Path(
    "/dev/input/by-id/"
    "usb-SteelSeries_Arctis_Nova_7X-if05-hidraw"
)


def chatmix_position(game: int, chat: int) -> float:
    """
    Convert the Nova 7X Game/Chat values into a conventional
    0-100 ChatMix slider.

      0   = 100% Game
      50  = Center
      100 = 100% Chat
    """

    return (chat - game + 100) / 2


def main() -> None:
    print(f"Opening {DEVICE}")

    if not DEVICE.exists():
        raise SystemExit("Nova 7X HID interface not found.")

    with DEVICE.open("rb", buffering=0) as headset:
        print("Nova 7X connected.")
        print("Move the ChatMix wheel. Ctrl+C to stop.\n")

        while True:
            report = headset.read(64)

            if len(report) < 3:
                continue

            # ChatMix report
            if report[0] != 0x45:
                continue

            game = report[1]
            chat = report[2]

            # Ignore malformed/unexpected values
            if game > 100 or chat > 100:
                continue

            position = chatmix_position(game, chat)

            if position < 50:
                side = "GAME"
            elif position > 50:
                side = "CHAT"
            else:
                side = "CENTER"

            print(
                f"\r"
                f"Game: {game:3d}%  "
                f"Chat: {chat:3d}%  "
                f"Mix: {position:5.1f}%  "
                f"[{side}]",
                end="",
                flush=True,
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
