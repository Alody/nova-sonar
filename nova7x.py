from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import select
import glob


@dataclass(frozen=True)
class ChatMixState:
    game: int
    chat: int
    position: float

    @property
    def side(self) -> str:
        if self.position < 50:
            return "GAME"
        if self.position > 50:
            return "CHAT"
        return "CENTER"


class Nova7X:
    """
    SteelSeries Arctis Nova 7X HID backend.

    Currently implemented:
        - Device detection
        - Interface 5 access
        - Physical ChatMix wheel reading

    ChatMix HID report:
        45 <game 0-100> <chat 0-100> ...
    """

    VENDOR_ID = 0x1038
    PRODUCT_ID = 0x22AD

    DEVICE_GLOB = (
        "/dev/input/by-id/"
        "usb-SteelSeries_Arctis_Nova_7X-if05-hidraw"
    )

    REPORT_SIZE = 64
    CHATMIX_REPORT = 0x45
    STATUS_INTERFACE = 3
    STATUS_COMMAND = 0xB0

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else self.detect()
        self._fd: int | None = None

    @classmethod
    def detect(cls) -> Path:
        path = Path(cls.DEVICE_GLOB)

        if path.exists():
            return path

        raise FileNotFoundError(
            "SteelSeries Arctis Nova 7X interface 5 was not found."
        )

    @property
    def connected(self) -> bool:
        return self._fd is not None

    @classmethod
    def status_device(cls) -> Path | None:
        expected_id = f"0003:0000{cls.VENDOR_ID:04X}:0000{cls.PRODUCT_ID:04X}"
        for uevent_name in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
            try:
                values = Path(uevent_name).read_text(encoding="utf-8")
            except OSError:
                continue
            if f"HID_ID={expected_id}" not in values:
                continue
            if f"/input{cls.STATUS_INTERFACE}" not in values:
                continue
            hidraw_name = Path(uevent_name).parents[1].name
            device = Path("/dev") / hidraw_name
            if device.exists():
                return device
        return None

    @staticmethod
    def _parse_wireless_status(report: bytes) -> bool | None:
        if len(report) < 4:
            return None
        return report[3] != 0

    @classmethod
    def wireless_connected(cls, timeout: float = 0.2) -> bool | None:
        """Return the 2.4 GHz headset state, or None when unavailable."""
        device = cls.status_device()
        if device is None:
            return None
        descriptor = None
        try:
            descriptor = os.open(device, os.O_RDWR | os.O_NONBLOCK)
            request = bytes([0x00, cls.STATUS_COMMAND]) + bytes(cls.REPORT_SIZE - 2)
            os.write(descriptor, request)
            readable, _, _ = select.select([descriptor], [], [], timeout)
            if not readable:
                return None
            return cls._parse_wireless_status(os.read(descriptor, 128))
        except (OSError, PermissionError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def open(self) -> None:
        if self._fd is not None:
            return

        self._fd = os.open(
            self.path,
            os.O_RDONLY | os.O_NONBLOCK,
        )

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def read_chatmix(
        self,
        timeout: float = 0.25,
    ) -> ChatMixState | None:
        if self._fd is None:
            raise RuntimeError("Headset is not open.")

        # A hidraw descriptor can remain selectable briefly after USB
        # removal. Check the persistent by-id link as well so unplugging is
        # reported promptly instead of looking like an idle ChatMix wheel.
        if not self.path.exists():
            raise OSError("Headset HID device disappeared")

        readable, _, _ = select.select(
            [self._fd],
            [],
            [],
            timeout,
        )

        if not readable:
            if not self.path.exists():
                raise OSError("Headset HID device disappeared")
            return None

        try:
            report = os.read(
                self._fd,
                self.REPORT_SIZE,
            )
        except BlockingIOError:
            return None

        if not report:
            raise OSError("Headset HID device closed")

        if len(report) < 3:
            return None

        if report[0] != self.CHATMIX_REPORT:
            return None

        game = report[1]
        chat = report[2]

        if game > 100 or chat > 100:
            return None

        position = self._normalize_chatmix(game, chat)

        return ChatMixState(
            game=game,
            chat=chat,
            position=position,
        )

    @staticmethod
    def _normalize_chatmix(
        game: int,
        chat: int,
    ) -> float:
        """
        Convert SteelSeries Game/Chat attenuation into:

            0   = 100% Game
            50  = Center
            100 = 100% Chat
        """

        position = (chat - game + 100) / 2

        return max(0.0, min(100.0, position))

    def __enter__(self) -> "Nova7X":
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
