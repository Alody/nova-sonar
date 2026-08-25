from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import json
import os
import subprocess
import time
import threading

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
    GAME_FALLBACK_SINK = "nova_sonar_game_fallback"
    CHAT_SINK = "nova_sonar_chat"

    CONFIG_DIR = Path.home() / ".config" / "nova-sonar"
    STATE_FILE = CONFIG_DIR / "state.json"
    ROUTING_FILE = CONFIG_DIR / "routing.json"

    def __init__(self):
        self._last_game: int | None = None
        self._last_chat: int | None = None
        self._routing_rules: dict[str, str] | None = None
        self._sink_names: dict[int, str] | None = None
        self._route_failures: dict[tuple[int, str], tuple[float, float]] = {}
        self._state_lock = threading.RLock()
        self._topology_generation = 0
        self._game_sink = self.GAME_FALLBACK_SINK
        self._legacy_game_modules: set[int] = set()

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

        try:
            data = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError("pactl returned malformed JSON") from error
        if not isinstance(data, list):
            raise RuntimeError("pactl returned an unexpected JSON structure")
        return data

    # ---------------------------------------------------------
    # Sink management
    # ---------------------------------------------------------

    def get_sinks(self) -> dict[str, int]:
        sinks = self._json(
            ["list", "sinks"]
        )

        result: dict[str, int] = {}

        for sink in sinks:
            if not isinstance(sink, dict):
                continue
            name = sink.get("name")

            if not name:
                continue
            try:
                result[str(name)] = int(sink["index"])
            except (KeyError, TypeError, ValueError):
                continue

            # Versions before 0.3 used the spatial node name for their Pulse
            # fallback. Only unload a positively identified legacy remap;
            # native filter-chain nodes do not have a Pulse owner module.
            props = sink.get("properties", {})
            if not isinstance(props, dict):
                props = {}
            if (
                name == self.GAME_SINK
                and props.get("device.description") == "Nova Sonar Game"
                and sink.get("owner_module") is not None
            ):
                try:
                    self._legacy_game_modules.add(int(sink["owner_module"]))
                except (TypeError, ValueError):
                    pass

        return result

    def ensure_buses(self) -> None:
        """
        Make sure Nova Sonar Game and Chat exist.

        Safe to call repeatedly.
        """

        sinks = self.get_sinks()

        if self._legacy_game_modules:
            for module_id in tuple(self._legacy_game_modules):
                self._run(["unload-module", str(module_id)], capture=False)
            self._legacy_game_modules.clear()
            sinks = self.get_sinks()

        if self.MASTER_SINK not in sinks:
            raise RuntimeError(
                "Arctis Nova 7X hardware output is not available."
            )

        if self.GAME_FALLBACK_SINK not in sinks:
            self._create_bus(
                self.GAME_FALLBACK_SINK,
                "Nova Sonar Game (Fallback)",
            )
            sinks[self.GAME_FALLBACK_SINK] = -1
        self._game_sink = (
            self.GAME_SINK
            if self.GAME_SINK in sinks
            else self.GAME_FALLBACK_SINK
        )

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
                self._game_sink,
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
            with self._state_lock:
                game_sink = self._game_sink
            self._set_sink_volume(
                game_sink,
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
            TypeError,
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
        with self._state_lock:
            if self._routing_rules is not None:
                return self._routing_rules

            if not self.ROUTING_FILE.exists():
                self._routing_rules = {}
                return self._routing_rules
            try:
                data = json.loads(self.ROUTING_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise TypeError
                self._routing_rules = {
                    str(key): target
                    for key, target in data.items()
                    if target in {"Game", "Chat"}
                }
            except (OSError, TypeError, json.JSONDecodeError):
                self._routing_rules = {}
            return self._routing_rules

    def save_routing_rule(self, binary: str, name: str, target: str) -> None:
        if target not in {"Game", "Chat"}:
            raise ValueError(f"Unknown routing target: {target}")
        key = self.routing_key(binary, name)
        if key in {"binary:", "name:"}:
            return
        with self._state_lock:
            rules = self.load_routing_rules()
            rules[key] = target
            atomic_write_json(self.ROUTING_FILE, rules)

    def remove_routing_rule(self, binary: str, name: str) -> None:
        key = self.routing_key(binary, name)
        with self._state_lock:
            rules = self.load_routing_rules()
            if rules.pop(key, None) is not None:
                atomic_write_json(self.ROUTING_FILE, rules)

    def invalidate_topology(self) -> None:
        with self._state_lock:
            self._sink_names = None
            self._topology_generation += 1

    def next_route_retry_delay(self) -> float | None:
        with self._state_lock:
            if not self._route_failures:
                return None
            retry_at = min(value[0] for value in self._route_failures.values())
        return max(0.0, retry_at - time.monotonic())

    @staticmethod
    def _first_property(props: dict, *keys: str) -> str:
        return next((str(props[key]) for key in keys if props.get(key)), "")

    @classmethod
    def is_game_sink(cls, sink_name: str) -> bool:
        return sink_name in {cls.GAME_SINK, cls.GAME_FALLBACK_SINK}

    def list_streams(
        self,
    ) -> list[AudioStream]:

        with self._state_lock:
            sink_names = self._sink_names
            topology_generation = self._topology_generation
        if sink_names is None:
            sinks = self._json(["list", "sinks"])
            sink_names = {}
            for sink in sinks:
                if not isinstance(sink, dict):
                    continue
                try:
                    sink_names[int(sink["index"])] = str(sink.get("name", ""))
                except (KeyError, TypeError, ValueError):
                    continue
            with self._state_lock:
                if topology_generation == self._topology_generation:
                    self._sink_names = sink_names

        available_sink_names = set(sink_names.values())
        with self._state_lock:
            previous_game_sink = self._game_sink
            next_game_sink = (
                self.GAME_SINK
                if self.GAME_SINK in available_sink_names
                else self.GAME_FALLBACK_SINK
            )
            previous_game_volume = self._last_game
            self._game_sink = next_game_sink
            if next_game_sink != previous_game_sink:
                self._last_game = None
            game_sink = self._game_sink

        if next_game_sink != previous_game_sink and previous_game_volume is not None:
            try:
                self._set_sink_volume(next_game_sink, previous_game_volume)
            except RuntimeError:
                pass
            else:
                with self._state_lock:
                    self._last_game = previous_game_volume

        inputs = self._json(
            ["list", "sink-inputs"]
        )

        streams: list[AudioStream] = []
        routing_rules = self.load_routing_rules()
        rules_changed = False

        for item in inputs:
            if not isinstance(item, dict):
                continue
            props = item.get("properties", {})
            if not isinstance(props, dict):
                props = {}

            node_name = str(props.get("node.name", ""))

            # Don't display our own internal remap streams.
            if node_name.startswith(
                "output.nova_sonar_"
            ):
                continue

            try:
                index = int(item["index"])
                sink_index = int(item.get("sink", -1))
            except (KeyError, TypeError, ValueError):
                continue

            sink_name = sink_names.get(
                sink_index,
                "",
            )

            name = self._first_property(
                props,
                "application.name",
                "media.name",
                "application.process.binary",
            ) or f"Audio stream {index}"

            binary = props.get(
                "application.process.binary",
                "",
            )

            icon_name = self._first_property(
                props,
                "pipewire.access.portal.app_id",
                "application.id",
                "application.desktop",
                "application.icon_name",
                "application.process.binary",
            )

            route_key = self.routing_key(binary, name)
            saved_target = routing_rules.get(route_key)
            if saved_target is None and route_key not in {"binary:", "name:"}:
                current_target = {
                    self.GAME_SINK: "Game",
                    self.GAME_FALLBACK_SINK: "Game",
                    self.CHAT_SINK: "Chat",
                }.get(sink_name)
                if current_target:
                    with self._state_lock:
                        routing_rules[route_key] = current_target
                    saved_target = current_target
                    rules_changed = True
            desired_sink = {
                "Game": game_sink,
                "Chat": self.CHAT_SINK,
            }.get(saved_target)
            if desired_sink and sink_name != desired_sink:
                failure_key = (index, desired_sink)
                with self._state_lock:
                    retry_at, delay = self._route_failures.get(
                        failure_key, (0.0, 1.0)
                    )
                if time.monotonic() < retry_at:
                    desired_sink = None
            if desired_sink and sink_name != desired_sink:
                try:
                    self._run(
                        ["move-sink-input", str(index), desired_sink],
                        capture=False,
                    )
                    sink_name = desired_sink
                    with self._state_lock:
                        self._route_failures.pop((index, desired_sink), None)
                except RuntimeError:
                    delay = min(60.0, delay * 2.0)
                    with self._state_lock:
                        self._route_failures[(index, desired_sink)] = (
                            time.monotonic() + delay,
                            delay,
                        )

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
            with self._state_lock:
                atomic_write_json(self.ROUTING_FILE, routing_rules)

        live_indexes = {stream.index for stream in streams}
        with self._state_lock:
            self._route_failures = {
                key: value
                for key, value in self._route_failures.items()
                if key[0] in live_indexes
            }

        return streams

    def route_stream(
        self,
        stream_index: int,
        target: str,
        binary: str = "",
        name: str = "",
    ) -> None:

        if target == "Game":
            with self._state_lock:
                sink = self._game_sink

        elif target == "Chat":
            sink = self.CHAT_SINK

        elif target == "Unrouted":
            sink = self.MASTER_SINK

        else:
            raise ValueError(
                f"Unknown routing target: {target}"
            )

        key = self.routing_key(binary, name)
        with self._state_lock:
            previous_target = self.load_routing_rules().get(key)

        if target == "Unrouted":
            self.remove_routing_rule(binary, name)
        else:
            self.save_routing_rule(binary, name, target)

        try:
            self._run(
                [
                    "move-sink-input",
                    str(stream_index),
                    sink,
                ],
                capture=False,
            )
        except RuntimeError:
            if previous_target in {"Game", "Chat"}:
                self.save_routing_rule(binary, name, previous_target)
            else:
                self.remove_routing_rule(binary, name)
            raise
