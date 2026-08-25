from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from state_io import atomic_write_json


class PipeWireEQ:
    COMMAND_TIMEOUT = 5.0
    PROCESSOR = "parametric"
    NODE_NAME = "nova_sonar_eq"

    BANDS = [
        "31 Hz",
        "63 Hz",
        "125 Hz",
        "250 Hz",
        "500 Hz",
        "1 kHz",
        "2 kHz",
        "4 kHz",
        "8 kHz",
        "16 kHz",
    ]

    # Same basic curves SteelSeries defines for the Nova 7 family.
    PRESETS = {
        "Flat": [
            0, 0, 0, 0, 0,
            0, 0, 0, 0, 0,
        ],

        "Bass": [
            3.5, 5.5, 4, 1, -1.5,
            -1.5, -1, -1, -1, -1,
        ],

        "Focus": [
            -5, -3.5, -1, -3.5, -2.5,
            4, 6, -3.5, 0, 0,
        ],

        "Smiley": [
            3, 3.5, 1.5, -1.5, -4,
            -4, -2.5, 1.5, 3, 4,
        ],

        "Competitive FPS": [
            -3, -2, -1.5, -1, 0,
            1.5, 3, 4, 2, 0,
        ],

        "Immersive Gaming": [
            3, 2.5, 1.5, 0, -1,
            -1, 0, 1.5, 2, 1.5,
        ],

        "Clear Dialogue": [
            -4, -3, -2, -1, 0,
            2, 3.5, 2, 0, -2,
        ],

        "Night Mode": [
            -5, -4, -2.5, -1, 0.5,
            2, 2.5, 0.5, -2, -4,
        ],

        "Warm": [
            2.5, 2, 1.5, 0.5, 0,
            -0.5, -0.5, -1, -1.5, -2,
        ],

        "Bright": [
            -2, -1, 0, 0, 0,
            0.5, 1.5, 2.5, 3, 2,
        ],

        "Reduce Harshness": [
            0, 0, 0, 0.5, 1,
            0, -1.5, -3, -2, 0,
        ],

        "Vocal Presence": [
            -2, -1.5, -1, 0, 1,
            2.5, 3, 1, -1, -2,
        ],
    }

    STATE_FILE = (
        Path.home()
        / ".config"
        / "nova-sonar"
        / "eq.json"
    )

    FREQUENCIES = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
    TYPE_IDS = {
        "Off": 0,
        "Bell": 1,
        "High-pass": 2,
        "High shelf": 3,
        "Low-pass": 4,
        "Low shelf": 5,
        "Notch": 6,
        "Band-pass": 9,
    }

    def __init__(self):
        self._node_id: int | None = None

    @staticmethod
    def _run(args: list[str]) -> str:
        try:
            result = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=PipeWireEQ.COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("PipeWire command timed out") from error

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or "PipeWire command failed"
            )

        return result.stdout

    def find_node(self) -> int:
        data = json.loads(
            self._run(["pw-dump"])
        )

        for obj in data:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue

            props = (
                obj.get("info", {})
                .get("props", {})
            )

            if props.get("node.name") == self.NODE_NAME:
                self._node_id = int(obj["id"])
                return self._node_id

        raise RuntimeError(
            "Nova Sonar EQ PipeWire node not found"
        )

    def apply(self, gains: list[float]) -> None:
        if len(gains) != 10:
            raise ValueError(
                "EQ requires exactly 10 bands"
            )

        gains = [
            max(-12.0, min(12.0, float(g)))
            for g in gains
        ]

        # Automatically create headroom.
        #
        # Example:
        # highest band = +6 dB
        # preamp = -6 dB
        highest_boost = max(
            0.0,
            max(gains),
        )

        preamp_db = -highest_boost

        preamp_linear = (
            10.0 ** (preamp_db / 20.0)
        )

        if self._node_id is None:
            self.find_node()

        params = [
            f'"{self.PROCESSOR}:g_in" {preamp_linear:.6f}'
        ]

        for index, gain in enumerate(
            gains,
        ):
            linear_gain = 10.0 ** (gain / 20.0)
            params.extend(
                [
                    f'"{self.PROCESSOR}:gm_{index}" {linear_gain:.6f}',
                    f'"{self.PROCESSOR}:gs_{index}" {linear_gain:.6f}',
                ]
            )

        payload = (
            "{ params = [ "
            + " ".join(params)
            + " ] }"
        )

        try:
            self._run(
                [
                    "pw-cli",
                    "set-param",
                    str(self._node_id),
                    "Props",
                    payload,
                ]
            )

        except RuntimeError:
            # Node IDs change after PipeWire restarts.
            self._node_id = None
            self.find_node()
            self._run(
                [
                    "pw-cli", "set-param", str(self._node_id),
                    "Props", payload,
                ]
            )

    @classmethod
    def default_settings(cls) -> dict:
        bands = []
        for index, frequency in enumerate(cls.FREQUENCIES):
            if index == 0:
                filter_type, q = "Low shelf", 0.7
            elif index == 9:
                filter_type, q = "High shelf", 0.7
            else:
                filter_type, q = "Bell", 1.41
            bands.append(
                {
                    "enabled": True,
                    "type": filter_type,
                    "frequency": frequency,
                    "gain": 0.0,
                    "q": q,
                    "slope": 1,
                }
            )
        return {
            "enabled": True,
            "show_eq_curve": False,
            "mode": "IIR",
            "output_gain": 0.0,
            "mid_gain": 0.0,
            "side_gain": 0.0,
            "dynamic_enabled": False,
            "dynamic_amount": 3.0,
            "high_pass": {"enabled": False, "frequency": 25.0, "slope": 2},
            "low_pass": {"enabled": False, "frequency": 20000.0, "slope": 2},
            "mid_bands": [dict(band) for band in bands],
            "side_bands": [dict(band) for band in bands],
        }

    def load_settings(self) -> dict:
        defaults = self.default_settings()
        if not self.STATE_FILE.exists():
            return defaults
        try:
            data = json.loads(self.STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return defaults

        # Migrate the original graphic-EQ state without losing its curve.
        if "gains" in data and "mid_bands" not in data:
            try:
                gains = [float(value) for value in data["gains"]]
                if len(gains) == 10:
                    for side in ("mid_bands", "side_bands"):
                        for band, gain in zip(defaults[side], gains):
                            band["gain"] = max(-12.0, min(12.0, gain))
            except (TypeError, ValueError):
                pass
            return defaults

        for key in (
            "enabled", "show_eq_curve", "mode", "output_gain", "mid_gain", "side_gain",
            "dynamic_enabled", "dynamic_amount", "high_pass", "low_pass",
            "mid_bands", "side_bands",
        ):
            if key in data:
                defaults[key] = data[key]
        if len(defaults["mid_bands"]) != 10 or len(defaults["side_bands"]) != 10:
            return self.default_settings()
        return defaults

    def save_settings(self, settings: dict) -> None:
        atomic_write_json(self.STATE_FILE, settings)

    @staticmethod
    def _db_to_gain(db: float) -> float:
        return 10.0 ** (max(-36.0, min(36.0, float(db))) / 20.0)

    def apply_settings(self, settings: dict) -> None:
        mode_ids = {"IIR": 0, "Linear FIR": 1, "Linear FFT": 2, "Spectral": 3}
        params = [
            f'"parametric:enabled" {int(bool(settings["enabled"]))}',
            f'"parametric:mode" {mode_ids.get(settings["mode"], 0)}',
            f'"parametric:g_out" {self._db_to_gain(settings["output_gain"]):.6f}',
            f'"parametric:gain_m" {self._db_to_gain(settings["mid_gain"]):.6f}',
            f'"parametric:gain_s" {self._db_to_gain(settings["side_gain"]):.6f}',
            f'"parametric:clink" 0',
            f'"dynamics:enabled" {int(bool(settings["dynamic_enabled"]))}',
        ]
        dynamic_amount = max(0.0, min(12.0, float(settings["dynamic_amount"])))
        for prefix in ("m", "s"):
            for index in range(8):
                params.extend(
                    [
                        f'"dynamics:tl0_{index}{prefix}" {self._db_to_gain(-12.0):.6f}',
                        f'"dynamics:gl0_{index}{prefix}" {self._db_to_gain(-12.0 - dynamic_amount):.6f}',
                        f'"dynamics:atd_{index}{prefix}" 20.0',
                        f'"dynamics:rtd_{index}{prefix}" 120.0',
                    ]
                )

        for prefix, bands in (("m", settings["mid_bands"]), ("s", settings["side_bands"])):
            for index, band in enumerate(bands):
                type_id = self.TYPE_IDS.get(band["type"], 1) if band["enabled"] else 0
                params.extend(
                    [
                        f'"parametric:ft{prefix}_{index}" {type_id}',
                        f'"parametric:f{prefix}_{index}" {float(band["frequency"]):.3f}',
                        f'"parametric:g{prefix}_{index}" {self._db_to_gain(band["gain"]):.6f}',
                        f'"parametric:q{prefix}_{index}" {float(band["q"]):.4f}',
                        f'"parametric:s{prefix}_{index}" {max(0, min(3, int(band["slope"]) - 1))}',
                    ]
                )

            high_pass = settings["high_pass"]
            low_pass = settings["low_pass"]
            params.extend(
                [
                    f'"parametric:ft{prefix}_10" {2 if high_pass["enabled"] else 0}',
                    f'"parametric:f{prefix}_10" {float(high_pass["frequency"]):.3f}',
                    f'"parametric:s{prefix}_10" {max(0, min(3, int(high_pass["slope"]) - 1))}',
                    f'"parametric:ft{prefix}_11" {4 if low_pass["enabled"] else 0}',
                    f'"parametric:f{prefix}_11" {float(low_pass["frequency"]):.3f}',
                    f'"parametric:s{prefix}_11" {max(0, min(3, int(low_pass["slope"]) - 1))}',
                ]
            )

        payload = "{ params = [ " + " ".join(params) + " ] }"
        if self._node_id is None:
            self.find_node()
        try:
            self._run(["pw-cli", "set-param", str(self._node_id), "Props", payload])
        except RuntimeError:
            self._node_id = None
            self.find_node()
            self._run(["pw-cli", "set-param", str(self._node_id), "Props", payload])

    def save(self, gains: list[float]) -> None:
        atomic_write_json(self.STATE_FILE, {"gains": gains})

    def load(self) -> list[float]:
        if not self.STATE_FILE.exists():
            return list(
                self.PRESETS["Flat"]
            )

        try:
            data = json.loads(
                self.STATE_FILE.read_text()
            )

            gains = [
                float(value)
                for value in data["gains"]
            ]

            if len(gains) != 10:
                raise ValueError

            return gains

        except (
            OSError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ):
            return list(
                self.PRESETS["Flat"]
            )
