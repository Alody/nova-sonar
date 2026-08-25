from __future__ import annotations

import json
import subprocess
from pathlib import Path

from state_io import atomic_write_json


class PipeWireSpatial:
    COMMAND_TIMEOUT = 5.0
    NODE_NAME = "nova_sonar_game"

    STATE_FILE = (
        Path.home()
        / ".config"
        / "nova-sonar"
        / "spatial.json"
    )

    DEFAULT_ENABLED = True

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
                timeout=PipeWireSpatial.COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("PipeWire command timed out") from error

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or "PipeWire command failed"
            )

        return result.stdout

    def _discover_node_id(
        self,
    ) -> int | None:
        try:
            objects = json.loads(
                self._run(["pw-dump"])
            )
        except (
            RuntimeError,
            json.JSONDecodeError,
        ):
            return None
        if not isinstance(objects, list):
            return None

        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if (
                obj.get("type")
                != "PipeWire:Interface:Node"
            ):
                continue

            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            props = info.get("props", {})
            if not isinstance(props, dict):
                continue

            if (
                props.get("node.name")
                == self.NODE_NAME
            ):
                try:
                    return int(obj["id"])
                except (KeyError, TypeError, ValueError):
                    continue

        return None

    def find_node(self) -> int:
        node_id = self._discover_node_id()

        if node_id is None:
            raise RuntimeError(
                "Nova Sonar Game node not found"
            )

        self._node_id = node_id
        return node_id

    def invalidate_node(self) -> None:
        self._node_id = None

    @staticmethod
    def _mix_gains(
        enabled: bool,
    ) -> tuple[float, float]:
        # Binary only. No HRTF/dry crossfade.
        if enabled:
            return 1.0, 0.0

        return 0.0, 1.0

    def _apply_enabled(
        self,
        enabled: bool,
    ) -> None:
        if self._node_id is None:
            self.find_node()

        spatial, stereo = self._mix_gains(
            bool(enabled)
        )

        payload = (
            "{ params = [ "
            f'"final_l:Gain 1" {spatial:.1f} '
            f'"final_l:Gain 2" {stereo:.1f} '
            f'"final_r:Gain 1" {spatial:.1f} '
            f'"final_r:Gain 2" {stereo:.1f} '
            "] }"
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
            self.invalidate_node()
            self.find_node()

            self._run(
                [
                    "pw-cli",
                    "set-param",
                    str(self._node_id),
                    "Props",
                    payload,
                ]
            )

    def set_enabled(
        self,
        enabled: bool,
        *,
        save: bool = True,
    ) -> None:
        enabled = bool(enabled)

        self._apply_enabled(
            enabled
        )

        if save:
            self.save(enabled)

    def sync_if_recreated(
        self,
        enabled: bool,
    ) -> str:
        """Detect a recreated Game node and restore ON/OFF state."""
        current_id = self._discover_node_id()

        if current_id is None:
            self._node_id = None
            return "missing"

        if current_id == self._node_id:
            return "unchanged"

        self._node_id = current_id

        # Reapply exactly:
        # ON  -> 1.0 / 0.0
        # OFF -> 0.0 / 1.0
        self._apply_enabled(
            bool(enabled)
        )

        return "reapplied"

    def save(
        self,
        enabled: bool,
    ) -> None:
        # This rewrites old state files and drops any
        # obsolete "intensity" field.
        atomic_write_json(
            self.STATE_FILE,
            {"enabled": bool(enabled)},
        )

    def load(self) -> bool:
        if not self.STATE_FILE.exists():
            return self.DEFAULT_ENABLED

        try:
            data = json.loads(
                self.STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(data, dict):
                return self.DEFAULT_ENABLED

            return bool(
                data.get(
                    "enabled",
                    self.DEFAULT_ENABLED,
                )
            )

        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return self.DEFAULT_ENABLED
