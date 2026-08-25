from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json
import os
import subprocess

from state_io import atomic_write_json


@dataclass(frozen=True)
class AudioStream:
    index: int
    name: str
    binary: str
    sink_name: str
    icon_name: str = ""


class PipeWireMixer:
    COMMAND_TIMEOUT = 5.0
    MASTER_SINK = "nova_sonar_eq"

    GAME_SINK = "nova_sonar_game"
    CHAT_SINK = "nova_sonar_chat"

    CONFIG_DIR = Path.home() / ".config" / "nova-sonar"
    STATE_FILE = CONFIG_DIR / "state.json"
    ROUTING_FILE = CONFIG_DIR / "routing.json"

    def __init__(self):
        self._last_game: int | None = None
        self._last_chat: int | None = None

    # ---------------------------------------------------------
    # pactl helpers
    # ---------------------------------------------------------

    @staticmethod
    def _run(
        args: list[str],
        *,
        capture: bool = True,
    ) -> str:
        env = os.environ.copy()

        # Ensures pactl JSON uses a normal decimal separator.
        env["LC_NUMERIC"] = "C"

        try:
            result = subprocess.run(
                ["pactl", *args],
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=PipeWireMixer.COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("pactl command timed out") from error

        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or f"pactl command failed: {' '.join(args)}"
            )

        return result.stdout

    @classmethod
    def _json(cls, args: list[str]):
        output = cls._run(
            ["--format=json", *args]
        )

        if not output.strip():
            return []

        return json.loads(output)

    # ---------------------------------------------------------
    # Sink management
    # ---------------------------------------------------------

    def get_sinks(self) -> dict[str, int]:
        sinks = self._json(
            ["list", "sinks"]
        )

        result: dict[str, int] = {}

        for sink in sinks:
            name = sink.get("name")

            if not name:
                continue

            result[name] = int(sink["index"])

        return result

    def ensure_buses(self) -> None:
        """
        Make sure Nova Sonar Game and Chat exist.

        Safe to call repeatedly.
        """

        sinks = self.get_sinks()

        if self.MASTER_SINK not in sinks:
            raise RuntimeError(
                "Arctis Nova 7X hardware output is not available."
            )

        if self.GAME_SINK not in sinks:
            self._create_bus(
                self.GAME_SINK,
                "Nova Sonar Game",
            )

        sinks = self.get_sinks()

        if self.CHAT_SINK not in sinks:
            self._create_bus(
                self.CHAT_SINK,
                "Nova Sonar Chat",
            )

        # New applications should use Game unless routed
        # explicitly to Chat.
        self._run(
            [
                "set-default-sink",
                self.GAME_SINK,
            ],
            capture=False,
        )

        # Force the next ChatMix update to be applied.
        self._last_game = None
        self._last_chat = None

    def _create_bus(
        self,
        name: str,
        description: str,
    ) -> None:
        self._run(
            [
                "load-module",
                "module-remap-sink",
                f"sink_name={name}",
                f"master={self.MASTER_SINK}",
                "channels=2",
                "channel_map=front-left,front-right",
                "master_channel_map=front-left,front-right",
                "remix=no",
                (
                    "sink_properties="
                    f'device.description="{description}"'
                ),
            ]
        )

    # ---------------------------------------------------------
    # ChatMix
    # ---------------------------------------------------------

    def set_chatmix(
        self,
        game: int,
        chat: int,
    ) -> None:
        game = max(0, min(100, game))
        chat = max(0, min(100, chat))

        try:
            self._apply_chatmix(
                game,
                chat,
            )

        except RuntimeError:
            # PipeWire may have restarted and destroyed
            # our temporary sinks.
            self.ensure_buses()

            self._apply_chatmix(
                game,
                chat,
            )

    def _apply_chatmix(
        self,
        game: int,
        chat: int,
    ) -> None:
        if game != self._last_game:
            self._set_sink_volume(
                self.GAME_SINK,
                game,
            )

            self._last_game = game

        if chat != self._last_chat:
            self._set_sink_volume(
                self.CHAT_SINK,
                chat,
            )

            self._last_chat = chat

    def _set_sink_volume(
        self,
        sink: str,
        percent: int,
    ) -> None:
        self._run(
            [
                "set-sink-volume",
                sink,
                f"{percent}%",
            ],
            capture=False,
        )

    # ---------------------------------------------------------
    # State persistence
    # ---------------------------------------------------------

    def save_chatmix(
        self,
        game: int,
        chat: int,
    ) -> None:
        data = {
            "game": int(game),
            "chat": int(chat),
        }

        atomic_write_json(self.STATE_FILE, data)

    def load_chatmix(
        self,
    ) -> tuple[int, int] | None:
        if not self.STATE_FILE.exists():
            return None

        try:
            data = json.loads(
                self.STATE_FILE.read_text()
            )

            game = int(data["game"])
            chat = int(data["chat"])

        except (
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ):
            return None

        game = max(0, min(100, game))
        chat = max(0, min(100, chat))

        return game, chat

    # ---------------------------------------------------------
    # Application routing
    # ---------------------------------------------------------

    @staticmethod
    def routing_key(binary: str, name: str) -> str:
        binary = str(binary or "").strip().lower()
        name = str(name or "").strip().lower()
        return f"binary:{binary}" if binary else f"name:{name}"

    def load_routing_rules(self) -> dict[str, str]:
        if not self.ROUTING_FILE.exists():
            return {}
        try:
            data = json.loads(self.ROUTING_FILE.read_text(encoding="utf-8"))
            return {
                str(key): target
                for key, target in data.items()
                if target in {"Game", "Chat"}
            }
        except (OSError, TypeError, json.JSONDecodeError):
            return {}

    def save_routing_rule(self, binary: str, name: str, target: str) -> None:
        if target not in {"Game", "Chat"}:
            raise ValueError(f"Unknown routing target: {target}")
        key = self.routing_key(binary, name)
        if key in {"binary:", "name:"}:
            return
        rules = self.load_routing_rules()
        rules[key] = target
        atomic_write_json(self.ROUTING_FILE, rules)

    def list_streams(
        self,
    ) -> list[AudioStream]:

        sinks = self._json(
            ["list", "sinks"]
        )

        sink_names: dict[int, str] = {}

        for sink in sinks:
            sink_names[
                int(sink["index"])
            ] = sink.get(
                "name",
                "",
            )

        inputs = self._json(
            ["list", "sink-inputs"]
        )

        streams: list[AudioStream] = []
        routing_rules = self.load_routing_rules()
        rules_changed = False

        for item in inputs:
            props = item.get(
                "properties",
                {},
            )

            node_name = props.get(
                "node.name",
                "",
            )

            # Don't display our own internal remap streams.
            if node_name.startswith(
                "output.nova_sonar_"
            ):
                continue

            index = int(
                item["index"]
            )

            sink_index = int(
                item.get("sink", -1)
            )

            sink_name = sink_names.get(
                sink_index,
                "",
            )

            name = (
                props.get("application.name")
                or props.get("media.name")
                or props.get(
                    "application.process.binary"
                )
                or f"Audio stream {index}"
            )

            binary = props.get(
                "application.process.binary",
                "",
            )

            icon_name = (
                props.get("pipewire.access.portal.app_id")
                or props.get("application.id")
                or props.get("application.desktop")
                or props.get("application.icon_name")
                or props.get("application.process.binary")
                or ""
            )

            route_key = self.routing_key(binary, name)
            saved_target = routing_rules.get(route_key)
            if saved_target is None and route_key not in {"binary:", "name:"}:
                current_target = {
                    self.GAME_SINK: "Game",
                    self.CHAT_SINK: "Chat",
                }.get(sink_name)
                if current_target:
                    routing_rules[route_key] = current_target
                    saved_target = current_target
                    rules_changed = True
            desired_sink = {
                "Game": self.GAME_SINK,
                "Chat": self.CHAT_SINK,
            }.get(saved_target)
            if desired_sink and sink_name != desired_sink:
                try:
                    self._run(
                        ["move-sink-input", str(index), desired_sink],
                        capture=False,
                    )
                    sink_name = desired_sink
                except RuntimeError:
                    # Keep the stream visible and retry on the next refresh.
                    pass

            streams.append(
                AudioStream(
                    index=index,
                    name=name,
                    binary=binary,
                    sink_name=sink_name,
                    icon_name=icon_name,
                )
            )

        if rules_changed:
            atomic_write_json(self.ROUTING_FILE, routing_rules)

        return streams

    def route_stream(
        self,
        stream_index: int,
        target: str,
        binary: str = "",
        name: str = "",
    ) -> None:

        if target == "Game":
            sink = self.GAME_SINK

        elif target == "Chat":
            sink = self.CHAT_SINK

        else:
            raise ValueError(
                f"Unknown routing target: {target}"
            )

        self._run(
            [
                "move-sink-input",
                str(stream_index),
                sink,
            ],
            capture=False,
        )
        self.save_routing_rule(binary, name, target)
