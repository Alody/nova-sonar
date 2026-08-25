from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from state_io import atomic_write_json


class PipeWireMicEQ:
    NODE_NAME = "nova_sonar_mic"
    COMMAND_TIMEOUT = 5.0
    FREQUENCIES = [80.0, 200.0, 500.0, 1000.0, 3000.0, 8000.0]
    LABELS = ["80 Hz", "200 Hz", "500 Hz", "1 kHz", "3 kHz", "8 kHz"]
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
    PRESETS = {
        "Natural": [0, 0, 0, 0, 0, 0],
        "Clear Voice": [-2, -1.5, -1, 1, 3, 1.5],
        "Broadcast": [1.5, 1, -1.5, 0.5, 2.5, 1],
        "Warm Voice": [2, 2, 1, 0, -0.5, -1.5],
        "Reduce Boom": [-3, -2.5, -1, 0, 1, 0],
        "Reduce Sibilance": [0, 0, 0, 0, -1, -3.5],
    }
    STATE_FILE = Path.home() / ".config" / "nova-sonar" / "mic_eq.json"

    def __init__(self):
        self._node_id = None
        self._last_payload = None
        self._last_payload_node = None

    @classmethod
    def _run(cls, args: list[str]) -> str:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=cls.COMMAND_TIMEOUT
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Microphone EQ command timed out") from error
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "PipeWire command failed")
        return result.stdout

    def find_node(self) -> int:
        if self._node_id is not None:
            return self._node_id

        try:
            objects = json.loads(self._run(["pw-dump"]))
        except json.JSONDecodeError as error:
            raise RuntimeError("pw-dump returned malformed JSON") from error
        if not isinstance(objects, list):
            raise RuntimeError("pw-dump returned an unexpected JSON structure")
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            props = info.get("props", {})
            if not isinstance(props, dict):
                continue
            if props.get("node.name") == self.NODE_NAME:
                try:
                    self._node_id = int(obj["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                return self._node_id
        raise RuntimeError("Nova Sonar Microphone node not found")

    def invalidate_node(self) -> None:
        self._node_id = None
        self._last_payload = None
        self._last_payload_node = None

    @staticmethod
    def _safe_float(value, fallback: float, minimum: float, maximum: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(number):
            return fallback
        return max(minimum, min(maximum, number))

    @classmethod
    def defaults(cls) -> dict:
        bands = []
        for index, frequency in enumerate(cls.FREQUENCIES):
            bands.append({
                "enabled": True,
                "type": "Low shelf" if index == 0 else (
                    "High shelf" if index == 5 else "Bell"
                ),
                "frequency": frequency,
                "gain": 0.0,
                "q": 0.7 if index in (0, 5) else 1.2,
                "slope": 1,
            })
        return {
            "enabled": True,
            "preset": "Natural",
            "gains": list(cls.PRESETS["Natural"]),
            "bands": bands,
            "output_gain": 0.0,
            "show_eq_curve": False,
            "noise_suppression_enabled": True,
            "noise_voice_threshold": 85.0,
            "noise_grace_period": 200.0,
            "high_pass_enabled": True,
            "high_pass_frequency": 70.0,
            "high_pass_slope": 2,
        }

    def load(self) -> dict:
        state = self.defaults()
        if not self.STATE_FILE.exists():
            return state
        try:
            data = json.loads(self.STATE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError
            for key in (
                "enabled", "show_eq_curve", "noise_suppression_enabled",
                "high_pass_enabled",
            ):
                if isinstance(data.get(key), bool):
                    state[key] = data[key]
            if data.get("preset") in {*self.PRESETS, "Custom"}:
                state["preset"] = data["preset"]
            for key in (
                "output_gain", "noise_voice_threshold", "noise_grace_period",
                "high_pass_frequency",
            ):
                limits = {
                    "output_gain": (-24.0, 24.0),
                    "noise_voice_threshold": (0.0, 99.0),
                    "noise_grace_period": (0.0, 1000.0),
                    "high_pass_frequency": (10.0, 1000.0),
                }[key]
                state[key] = self._safe_float(data.get(key), state[key], *limits)
            try:
                state["high_pass_slope"] = max(
                    1, min(4, int(data["high_pass_slope"]))
                )
            except (KeyError, TypeError, ValueError):
                pass
            gains = data.get("gains")
            if isinstance(gains, list) and len(gains) == 6:
                try:
                    state["gains"] = [
                        self._safe_float(gain, 0.0, -12.0, 12.0)
                        for gain in gains
                    ]
                except (TypeError, ValueError):
                    pass
            values = data.get("bands")
            if isinstance(values, list) and len(values) == 6:
                normalized = []
                for fallback, value in zip(state["bands"], values):
                    band = dict(fallback)
                    if isinstance(value, dict):
                        if isinstance(value.get("enabled"), bool):
                            band["enabled"] = value["enabled"]
                        if value.get("type") in self.TYPE_IDS:
                            band["type"] = value["type"]
                        band["frequency"] = self._safe_float(
                            value.get("frequency"), band["frequency"], 10.0, 24000.0
                        )
                        band["gain"] = self._safe_float(
                            value.get("gain"), band["gain"], -12.0, 12.0
                        )
                        band["q"] = self._safe_float(
                            value.get("q"), band["q"], 0.1, 30.0
                        )
                        try:
                            band["slope"] = max(1, min(4, int(value["slope"])))
                        except (KeyError, TypeError, ValueError):
                            pass
                    normalized.append(band)
                state["bands"] = normalized
                state["gains"] = [float(band["gain"]) for band in normalized]
            else:
                for band, gain in zip(state["bands"], state["gains"]):
                    band["gain"] = gain
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return self.defaults()
        return state

    def save(self, state: dict) -> None:
        atomic_write_json(self.STATE_FILE, state)

    @staticmethod
    def _linear(db: float) -> float:
        return 10.0 ** (max(-24.0, min(24.0, float(db))) / 20.0)

    def apply(self, state: dict) -> None:
        bands = state.get("bands", self.defaults()["bands"])
        params = [
            f'"mic_eq:enabled" {int(bool(state["enabled"]))}',
            f'"mic_eq:g_out" {self._linear(state["output_gain"]):.6f}',
            f'"noise_mix:Gain 1" {1 if state.get("noise_suppression_enabled", True) else 0}',
            f'"noise_mix:Gain 2" {0 if state.get("noise_suppression_enabled", True) else 1}',
            f'"rnnoise:VAD Threshold (%)" {max(0.0, min(99.0, float(state.get("noise_voice_threshold", 85.0)))):.1f}',
            f'"rnnoise:VAD Grace Period (ms)" {max(0.0, min(1000.0, float(state.get("noise_grace_period", 200.0)))):.0f}',
            '"rnnoise:Retroactive VAD Grace (ms)" 0',
        ]
        for index, band in enumerate(bands):
            gain = max(-12.0, min(12.0, float(band["gain"])))
            filter_type = self.TYPE_IDS.get(str(band["type"]), 1)
            if not band.get("enabled", True):
                filter_type = 0
            params.extend([
                f'"mic_eq:ft_{index}" {filter_type}',
                f'"mic_eq:f_{index}" {max(10.0, min(24000.0, float(band["frequency"]))):.2f}',
                f'"mic_eq:g_{index}" {self._linear(gain):.6f}',
                f'"mic_eq:q_{index}" {max(0.1, min(30.0, float(band["q"]))):.3f}',
                f'"mic_eq:s_{index}" {max(0, min(3, int(band.get("slope", 1)) - 1))}',
            ])
        params.extend(
            [
                f'"mic_eq:ft_6" {2 if state["high_pass_enabled"] else 0}',
                f'"mic_eq:f_6" {float(state["high_pass_frequency"]):.2f}',
                f'"mic_eq:s_6" {max(0, min(3, int(state["high_pass_slope"]) - 1))}',
            ]
        )
        payload = "{ params = [ " + " ".join(params) + " ] }"
        if self._node_id is None:
            self.find_node()
        if payload == self._last_payload and self._node_id == self._last_payload_node:
            return
        try:
            self._run(["pw-cli", "set-param", str(self._node_id), "Props", payload])
        except RuntimeError:
            self.invalidate_node()
            self.find_node()
            self._run(["pw-cli", "set-param", str(self._node_id), "Props", payload])
        self._last_payload = payload
        self._last_payload_node = self._node_id
