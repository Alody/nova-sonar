import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hrtf_manager
from hrtf_manager import (
    DEFAULT_PRESET,
    EXPECTED_SOFA_NODES,
    FILENAME_RE,
    PRESETS,
    is_sofa_file,
)
from nova7x import ChatMixState, Nova7X
from pipewire_eq import PipeWireEQ
from pipewire_mic_eq import PipeWireMicEQ
from pipewire_mixer import PipeWireMixer
from pipewire_spatial import PipeWireSpatial
from state_io import atomic_write_json
from audio_events import parse_pactl_event
from diagnostics import _run as diagnostic_run


class DiagnosticTests(unittest.TestCase):
    def test_parses_relevant_pactl_events(self):
        self.assertEqual(
            parse_pactl_event("Event 'new' on sink-input #12"),
            ("new", "sink-input"),
        )
        self.assertEqual(
            parse_pactl_event("Event 'remove' on sink #3"),
            ("remove", "sink"),
        )
        self.assertIsNone(parse_pactl_event("Event 'change' on source #4"))

    def test_diagnostic_command_failure_is_reported(self):
        ok, detail = diagnostic_run(["nova-sonar-command-that-does-not-exist"])
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_installer_registers_spatial_user_service(self):
        source = Path("install-user.sh").read_text(encoding="utf-8")
        self.assertIn('"${SYSTEMD_USER_DIR}/nova-sonar-game.service"', source)
        self.assertIn('"${FILTER_CHAIN_DIR}/92-nova-sonar-game.conf"', source)
        self.assertIn('sed "s|@HRTF_DIR@|${HRTF_DIR}|g"', source)
        self.assertIn("systemctl --user daemon-reload", source)

    def test_spatial_graph_contract(self):
        source = Path("92-nova-sonar-game.conf").read_text(encoding="utf-8")
        self.assertEqual(source.count("type = sofa"), EXPECTED_SOFA_NODES)
        self.assertEqual(len(FILENAME_RE.findall(source)), EXPECTED_SOFA_NODES)
        self.assertEqual(source.count("ari_dtf_d_nh1230.sofa"), EXPECTED_SOFA_NODES)
        self.assertIn('node.name = "nova_sonar_game"', source)
        self.assertIn('target.object = "nova_sonar_eq"', source)
        self.assertIn("audio.channels = 8", source)
        self.assertIn("audio.position = [ FL FR FC LFE RL RR SL SR ]", source)
        for control in (
            'final_l:In 1',
            'final_l:In 2',
            'final_r:In 1',
            'final_r:In 2',
        ):
            self.assertIn(control, source)


class ChatMixTests(unittest.TestCase):
    def test_normalizes_and_clamps(self):
        self.assertEqual(Nova7X._normalize_chatmix(100, 0), 0)
        self.assertEqual(Nova7X._normalize_chatmix(50, 50), 50)
        self.assertEqual(Nova7X._normalize_chatmix(0, 100), 100)
        self.assertEqual(Nova7X._normalize_chatmix(200, 0), 0)

    def test_side(self):
        self.assertEqual(ChatMixState(100, 0, 0).side, "GAME")
        self.assertEqual(ChatMixState(50, 50, 50).side, "CENTER")
        self.assertEqual(ChatMixState(0, 100, 100).side, "CHAT")

    def test_vanished_hid_path_is_a_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "headset-hidraw"
            path.touch()
            headset = Nova7X(path)
            read_fd, write_fd = os.pipe()
            headset._fd = read_fd
            path.unlink()
            try:
                with self.assertRaises(OSError):
                    headset.read_chatmix(timeout=0)
            finally:
                headset.close()
                os.close(write_fd)

    def test_empty_hid_read_is_a_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "headset-hidraw"
            path.touch()
            headset = Nova7X(path)
            read_fd, write_fd = os.pipe()
            headset._fd = read_fd
            os.close(write_fd)
            try:
                with self.assertRaises(OSError):
                    headset.read_chatmix(timeout=0.05)
            finally:
                headset.close()

    def test_wireless_status_report_detects_radio_disconnect(self):
        self.assertFalse(Nova7X._parse_wireless_status(bytes([0, 0xB0, 80, 0])))
        self.assertTrue(Nova7X._parse_wireless_status(bytes([0, 0xB0, 80, 3])))
        self.assertIsNone(Nova7X._parse_wireless_status(bytes([0, 0xB0])))


class SpatialTests(unittest.TestCase):
    def test_binary_gains(self):
        self.assertEqual(PipeWireSpatial._mix_gains(True), (1.0, 0.0))
        self.assertEqual(PipeWireSpatial._mix_gains(False), (0.0, 1.0))


class StateTests(unittest.TestCase):
    def test_identical_eq_payload_is_not_sent_twice(self):
        class FakeEQ(PipeWireEQ):
            calls = []

            @classmethod
            def _run(cls, args):
                cls.calls.append(args)
                return ""

        FakeEQ.calls.clear()
        eq = FakeEQ()
        eq._node_id = 42
        settings = eq.default_settings()
        eq.apply_settings(settings)
        eq.apply_settings(settings)
        self.assertEqual(len(FakeEQ.calls), 1)

    def test_identical_microphone_payload_is_not_sent_twice(self):
        class FakeMicEQ(PipeWireMicEQ):
            calls = []

            @classmethod
            def _run(cls, args):
                cls.calls.append(args)
                return ""

        FakeMicEQ.calls.clear()
        eq = FakeMicEQ()
        eq._node_id = 43
        state = eq.defaults()
        eq.apply(state)
        eq.apply(state)
        self.assertEqual(len(FakeMicEQ.calls), 1)

    def test_pw_dump_requires_a_list(self):
        class FakeEQ(PipeWireEQ):
            @staticmethod
            def _run(args):
                return "{}"

        with self.assertRaisesRegex(RuntimeError, "unexpected JSON structure"):
            FakeEQ().find_node()

    def test_malformed_persisted_state_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            eq = PipeWireEQ()
            eq.STATE_FILE = root / "eq.json"
            eq.STATE_FILE.write_text("[]")
            self.assertEqual(eq.load_settings(), eq.default_settings())

            mic = PipeWireMicEQ()
            mic.STATE_FILE = root / "mic.json"
            mic.STATE_FILE.write_text("[]")
            self.assertEqual(mic.load(), mic.defaults())

            mixer = PipeWireMixer()
            mixer.STATE_FILE = root / "chatmix.json"
            mixer.STATE_FILE.write_text("[]")
            self.assertIsNone(mixer.load_chatmix())
            mixer.ROUTING_FILE = root / "routing.json"
            mixer.ROUTING_FILE.write_text("[]")
            self.assertEqual(mixer.load_routing_rules(), {})

            spatial = PipeWireSpatial()
            spatial.STATE_FILE = root / "spatial.json"
            spatial.STATE_FILE.write_text("[]")
            self.assertEqual(spatial.load(), spatial.DEFAULT_ENABLED)

    def test_invalid_band_fields_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            eq = PipeWireEQ()
            eq.STATE_FILE = Path(directory) / "eq.json"
            malformed = eq.default_settings()
            malformed["mid_bands"][0] = {"gain": "not-a-number"}
            eq.STATE_FILE.write_text(json.dumps(malformed))
            settings = eq.load_settings()
            self.assertEqual(settings["mid_bands"][0]["gain"], 0.0)

    def test_non_finite_and_extreme_state_values_are_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            eq = PipeWireEQ()
            eq.STATE_FILE = Path(directory) / "eq.json"
            state = eq.default_settings()
            state["output_gain"] = float("nan")
            state["dynamic_amount"] = 999
            state["mid_bands"][0]["frequency"] = float("inf")
            eq.STATE_FILE.write_text(json.dumps(state))
            loaded = eq.load_settings()
            self.assertEqual(loaded["output_gain"], 0.0)
            self.assertEqual(loaded["dynamic_amount"], 12.0)
            self.assertEqual(loaded["mid_bands"][0]["frequency"], 31.5)

            mic = PipeWireMicEQ()
            mic.STATE_FILE = Path(directory) / "mic.json"
            state = mic.defaults()
            state["noise_voice_threshold"] = float("nan")
            state["noise_grace_period"] = 9000
            mic.STATE_FILE.write_text(json.dumps(state))
            loaded = mic.load()
            self.assertEqual(loaded["noise_voice_threshold"], 85.0)
            self.assertEqual(loaded["noise_grace_period"], 1000.0)


    def test_eq_find_node_uses_cached_id(self):
        class FakeEQ(PipeWireEQ):
            @staticmethod
            def _run(args):
                raise AssertionError("pw-dump should not run for a cached node")

        eq = FakeEQ()
        eq._node_id = 42
        self.assertEqual(eq.find_node(), 42)

    def test_microphone_eq_find_node_uses_cached_id(self):
        class FakeMicEQ(PipeWireMicEQ):
            @classmethod
            def _run(cls, args):
                raise AssertionError("pw-dump should not run for a cached node")

        eq = FakeMicEQ()
        eq._node_id = 43
        self.assertEqual(eq.find_node(), 43)

    def test_all_microphone_presets_have_six_safe_bands(self):
        for name, gains in PipeWireMicEQ.PRESETS.items():
            with self.subTest(name=name):
                self.assertEqual(len(gains), 6)
                self.assertTrue(all(-12 <= gain <= 12 for gain in gains))

    def test_microphone_curve_is_hidden_by_default(self):
        self.assertFalse(PipeWireMicEQ.defaults()["show_eq_curve"])

    def test_microphone_eq_payload_contains_voice_controls(self):
        class FakeMicEQ(PipeWireMicEQ):
            calls = []

            @classmethod
            def _run(cls, args):
                cls.calls.append(args)
                return ""

        eq = FakeMicEQ()
        eq._node_id = 42
        state = eq.defaults()
        state["bands"][2].update(
            type="Notch", frequency=612.0, gain=-3.0, q=8.5
        )
        state["high_pass_slope"] = 4
        eq.apply(state)
        payload = FakeMicEQ.calls[-1][-1]
        self.assertIn('\"mic_eq:ft_2\" 6', payload)
        self.assertIn('\"mic_eq:f_2\" 612.00', payload)
        self.assertIn('\"mic_eq:q_2\" 8.500', payload)
        self.assertIn('\"mic_eq:ft_6\" 2', payload)
        self.assertIn('\"mic_eq:s_6\" 3', payload)
        self.assertIn('\"noise_mix:Gain 1\" 1', payload)
        self.assertIn('\"noise_mix:Gain 2\" 0', payload)
        self.assertIn('\"rnnoise:VAD Threshold (%)\" 85.0', payload)

    def test_all_eq_presets_have_ten_safe_bands(self):
        for name, gains in PipeWireEQ.PRESETS.items():
            with self.subTest(name=name):
                self.assertEqual(len(gains), 10)
                self.assertTrue(all(-12 <= gain <= 12 for gain in gains))

    def test_atomic_json_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            atomic_write_json(path, {"game": 80, "chat": 20})
            self.assertEqual(json.loads(path.read_text()), {"game": 80, "chat": 20})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_eq_invalid_state_falls_back_to_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            eq = PipeWireEQ()
            eq.STATE_FILE = Path(directory) / "eq.json"
            eq.STATE_FILE.write_text('{"gains": [1]}')
            self.assertEqual(eq.load(), PipeWireEQ.PRESETS["Flat"])

    def test_advanced_eq_migrates_graphic_gains(self):
        with tempfile.TemporaryDirectory() as directory:
            eq = PipeWireEQ()
            eq.STATE_FILE = Path(directory) / "eq.json"
            eq.STATE_FILE.write_text('{"gains": [1,2,3,4,5,6,7,8,9,10]}')
            settings = eq.load_settings()
            self.assertEqual(settings["mid_bands"][5]["gain"], 6)
            self.assertEqual(settings["side_bands"][5]["gain"], 6)

    def test_advanced_eq_payload_contains_parametric_controls(self):
        class FakeEQ(PipeWireEQ):
            calls = []

            @classmethod
            def _run(cls, args):
                cls.calls.append(args)
                return ""

        eq = FakeEQ()
        eq._node_id = 42
        settings = eq.default_settings()
        settings["mode"] = "Linear FIR"
        settings["mid_bands"][0]["frequency"] = 42.0
        eq.apply_settings(settings)
        payload = FakeEQ.calls[-1][-1]
        self.assertIn('"parametric:mode" 1', payload)
        self.assertIn('"parametric:fm_0" 42.000', payload)
        self.assertIn('"dynamics:enabled" 0', payload)

    def test_chatmix_state_is_clamped(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = PipeWireMixer()
            mixer.STATE_FILE = Path(directory) / "state.json"
            mixer.STATE_FILE.write_text('{"game": 200, "chat": -5}')
            self.assertEqual(mixer.load_chatmix(), (100, 0))


class RoutingTests(unittest.TestCase):
    def test_malformed_stream_entries_are_ignored(self):
        class FakeMixer(PipeWireMixer):
            @classmethod
            def _json(cls, args):
                if args == ["list", "sinks"]:
                    return [None, {"index": "bad"}, {"index": 7, "name": cls.GAME_SINK}]
                return [
                    None,
                    {"index": "bad", "properties": []},
                    {
                        "index": 12,
                        "sink": 7,
                        "properties": {"application.name": "Music"},
                    },
                ]

        with tempfile.TemporaryDirectory() as directory:
            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            self.assertEqual(
                [stream.name for stream in mixer.list_streams()],
                ["Music"],
            )

    def test_unrouted_drop_moves_to_master_and_removes_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                moves = []

                @classmethod
                def _run(cls, args, *, capture=True):
                    cls.moves.append(args)
                    return ""

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            mixer.save_routing_rule("player", "Music", "Game")
            mixer.route_stream(12, "Unrouted", "player", "Music")
            self.assertNotIn("binary:player", mixer.load_routing_rules())
            self.assertIn(
                ["move-sink-input", "12", mixer.MASTER_SINK],
                FakeMixer.moves,
            )

    def test_failed_drop_restores_previous_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                @classmethod
                def _run(cls, args, *, capture=True):
                    raise RuntimeError("move failed")

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            mixer.save_routing_rule("player", "Music", "Game")
            with self.assertRaises(RuntimeError):
                mixer.route_stream(12, "Chat", "player", "Music")
            self.assertEqual(
                mixer.load_routing_rules()["binary:player"],
                "Game",
            )

    def test_sink_topology_is_cached_until_invalidated(self):
        class FakeMixer(PipeWireMixer):
            sink_queries = 0

            @classmethod
            def _json(cls, args):
                if args == ["list", "sinks"]:
                    cls.sink_queries += 1
                    return [{"index": 7, "name": cls.GAME_SINK}]
                return []

        mixer = FakeMixer()
        mixer.list_streams()
        mixer.list_streams()
        self.assertEqual(FakeMixer.sink_queries, 1)
        mixer.invalidate_topology()
        mixer.list_streams()
        self.assertEqual(FakeMixer.sink_queries, 2)

    def test_failed_automatic_route_uses_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                move_attempts = 0

                @classmethod
                def _json(cls, args):
                    if args == ["list", "sinks"]:
                        return [{"index": 7, "name": cls.GAME_SINK}]
                    return [{
                        "index": 12,
                        "sink": 7,
                        "properties": {
                            "application.name": "Voice",
                            "application.process.binary": "voice-app",
                        },
                    }]

                @classmethod
                def _run(cls, args, *, capture=True):
                    cls.move_attempts += 1
                    raise RuntimeError("temporary failure")

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            atomic_write_json(mixer.ROUTING_FILE, {"binary:voice-app": "Chat"})
            mixer.list_streams()
            mixer.list_streams()
            self.assertEqual(FakeMixer.move_attempts, 1)
            self.assertIsNotNone(mixer.next_route_retry_delay())

    def test_ensure_buses_uses_one_sink_snapshot(self):
        class FakeMixer(PipeWireMixer):
            sink_queries = 0
            created = []

            def get_sinks(self):
                self.sink_queries += 1
                return {self.MASTER_SINK: 1}

            def _create_bus(self, name, description):
                self.created.append(name)

            @classmethod
            def _run(cls, args, *, capture=True):
                return ""

        mixer = FakeMixer()
        mixer.ensure_buses()
        self.assertEqual(mixer.sink_queries, 1)
        self.assertEqual(
            mixer.created,
            [mixer.GAME_FALLBACK_SINK, mixer.CHAT_SINK],
        )

    def test_spatial_start_keeps_hot_fallback_and_uses_spatial_default(self):
        class FakeMixer(PipeWireMixer):
            created = []
            calls = []

            def get_sinks(self):
                return {self.MASTER_SINK: 1, self.GAME_SINK: 2}

            def _create_bus(self, name, description):
                self.created.append(name)

            @classmethod
            def _run(cls, args, *, capture=True):
                cls.calls.append(args)
                return ""

        mixer = FakeMixer()
        mixer.ensure_buses()
        self.assertEqual(
            mixer.created,
            [mixer.GAME_FALLBACK_SINK, mixer.CHAT_SINK],
        )
        self.assertIn(
            ["set-default-sink", mixer.GAME_SINK],
            mixer.calls,
        )

    def test_legacy_game_remap_is_removed_without_touching_spatial_node(self):
        class FakeMixer(PipeWireMixer):
            legacy_present = True
            calls = []

            @classmethod
            def _json(cls, args):
                if args != ["list", "sinks"]:
                    return []
                sinks = [{"index": 1, "name": cls.MASTER_SINK}]
                if cls.legacy_present:
                    sinks.append({
                        "index": 2,
                        "name": cls.GAME_SINK,
                        "owner_module": 42,
                        "properties": {"device.description": "Nova Sonar Game"},
                    })
                return sinks

            @classmethod
            def _run(cls, args, *, capture=True):
                cls.calls.append(args)
                if args == ["unload-module", "42"]:
                    cls.legacy_present = False
                return ""

            def _create_bus(self, name, description):
                pass

        mixer = FakeMixer()
        mixer.ensure_buses()
        self.assertIn(["unload-module", "42"], FakeMixer.calls)
        self.assertEqual(mixer._game_sink, mixer.GAME_FALLBACK_SINK)

    def test_game_volume_moves_with_spatial_target(self):
        class FakeMixer(PipeWireMixer):
            spatial_available = False
            calls = []

            @classmethod
            def _json(cls, args):
                if args == ["list", "sinks"]:
                    sinks = [{"index": 7, "name": cls.GAME_FALLBACK_SINK}]
                    if cls.spatial_available:
                        sinks.append({"index": 8, "name": cls.GAME_SINK})
                    return sinks
                return []

            @classmethod
            def _run(cls, args, *, capture=True):
                cls.calls.append(args)
                return ""

        mixer = FakeMixer()
        mixer._last_game = 65
        mixer.list_streams()
        mixer.invalidate_topology()
        FakeMixer.spatial_available = True
        mixer.list_streams()
        self.assertIn(
            ["set-sink-volume", mixer.GAME_SINK, "65%"],
            FakeMixer.calls,
        )
        self.assertEqual(mixer._last_game, 65)

    def test_spatial_sink_replaces_fallback_as_game_target(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                moves = []

                @classmethod
                def _json(cls, args):
                    if args == ["list", "sinks"]:
                        return [
                            {"index": 7, "name": cls.GAME_FALLBACK_SINK},
                            {"index": 8, "name": cls.GAME_SINK},
                        ]
                    return [{
                        "index": 12,
                        "sink": 7,
                        "properties": {
                            "application.name": "Game",
                            "application.process.binary": "game",
                        },
                    }]

                @classmethod
                def _run(cls, args, *, capture=True):
                    cls.moves.append(args)
                    return ""

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            atomic_write_json(mixer.ROUTING_FILE, {"binary:game": "Game"})
            stream = mixer.list_streams()[0]
            self.assertEqual(stream.sink_name, mixer.GAME_SINK)
            self.assertIn(
                ["move-sink-input", "12", mixer.GAME_SINK],
                mixer.moves,
            )

    def test_routing_rules_persist_by_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            mixer = PipeWireMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            mixer.save_routing_rule("discord", "Discord", "Chat")
            self.assertEqual(
                mixer.load_routing_rules()["binary:discord"], "Chat"
            )

    def test_returning_application_is_automatically_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                moves = []

                @classmethod
                def _json(cls, args):
                    if args == ["list", "sinks"]:
                        return [
                            {"index": 7, "name": cls.GAME_SINK},
                            {"index": 8, "name": cls.CHAT_SINK},
                        ]
                    return [{
                        "index": 12,
                        "sink": 7,
                        "properties": {
                            "application.name": "Discord",
                            "application.process.binary": "discord",
                        },
                    }]

                @classmethod
                def _run(cls, args, *, capture=True):
                    cls.moves.append(args)
                    return ""

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            atomic_write_json(mixer.ROUTING_FILE, {"binary:discord": "Chat"})
            stream = mixer.list_streams()[0]
            self.assertEqual(stream.sink_name, mixer.CHAT_SINK)
            self.assertIn(
                ["move-sink-input", "12", mixer.CHAT_SINK], mixer.moves
            )

    def test_current_route_is_learned_without_manual_change(self):
        with tempfile.TemporaryDirectory() as directory:
            class FakeMixer(PipeWireMixer):
                @classmethod
                def _json(cls, args):
                    if args == ["list", "sinks"]:
                        return [{"index": 8, "name": cls.CHAT_SINK}]
                    return [{
                        "index": 12,
                        "sink": 8,
                        "properties": {
                            "application.name": "Voice",
                            "application.process.binary": "voice-app",
                        },
                    }]

            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            mixer.list_streams()
            self.assertEqual(
                mixer.load_routing_rules()["binary:voice-app"], "Chat"
            )

    def test_stream_mapping_and_internal_filtering(self):
        class FakeMixer(PipeWireMixer):
            @classmethod
            def _json(cls, args):
                if args == ["list", "sinks"]:
                    return [{"index": 7, "name": cls.GAME_SINK}]
                return [
                    {
                        "index": 12,
                        "sink": 7,
                        "properties": {
                            "application.name": "Music",
                            "application.process.binary": "player",
                            "pipewire.access.portal.app_id": "org.example.Music",
                        },
                    },
                    {
                        "index": 13,
                        "sink": 7,
                        "properties": {"node.name": "output.nova_sonar_chat"},
                    },
                ]

        with tempfile.TemporaryDirectory() as directory:
            mixer = FakeMixer()
            mixer.ROUTING_FILE = Path(directory) / "routing.json"
            self.assertEqual(
                mixer.list_streams()[0].sink_name,
                PipeWireMixer.GAME_SINK,
            )
            self.assertEqual(
                mixer.list_streams()[0].icon_name, "org.example.Music"
            )
            self.assertEqual(len(mixer.list_streams()), 1)


class HrtfConfigTests(unittest.TestCase):
    def test_nh1230_is_default_preset(self):
        self.assertEqual(DEFAULT_PRESET, "ari-nh1230")
        self.assertEqual(next(iter(PRESETS)), DEFAULT_PRESET)

    def test_rewrites_all_sofa_filenames(self):
        source = 'filename = "/old/a.sofa"\nfilename="/old/b.sofa"'
        changed, count = FILENAME_RE.subn('filename = "/new/hrtf.sofa"', source)
        self.assertEqual(count, 2)
        self.assertNotIn("/old/", changed)

    def test_sofa_validation_checks_container_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.sofa"
            invalid = root / "invalid.sofa"
            valid.write_bytes(b"CDF\x02" + b"\0" * 100_000)
            invalid.write_bytes(b"<html>" + b"x" * 100_000)
            self.assertTrue(is_sofa_file(valid))
            self.assertFalse(is_sofa_file(invalid))

    def test_install_reports_download_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "failed.sofa"
            presets = {
                "broken": {
                    "label": "Broken",
                    "path": destination,
                    "url": "https://example.invalid/broken.sofa",
                }
            }
            with (
                mock.patch.object(hrtf_manager, "HRTF_DIR", Path(directory)),
                mock.patch.object(hrtf_manager, "PRESETS", presets),
                mock.patch.object(
                    hrtf_manager,
                    "download",
                    side_effect=RuntimeError("offline"),
                ),
            ):
                with self.assertRaises(SystemExit) as raised:
                    hrtf_manager.install()
            self.assertIn("broken", str(raised.exception))


class ShutdownTests(unittest.TestCase):
    def test_spectrum_capture_tracks_visible_tab(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("self.tabs.currentChanged.connect(self.update_spectrum_workers)", source)
        self.assertIn("self.spectrum_worker.set_active(visible and current_tab == 1)", source)
        self.assertIn("self.mic_spectrum_worker.set_active(visible and current_tab == 2)", source)

    def test_spectrum_uses_overlapping_analysis_and_latest_frame_rendering(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("hop_size = 512", source)
        self.assertIn("self.spectrum_render_timer.setInterval(16)", source)
        self.assertIn("worker.copy_latest(widget.values", source)
        self.assertNotIn("spectrum_ready.emit", source)

    def test_event_watcher_resyncs_after_reconnect(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn(
            'self.topology_changed.emit("reconnect", "server")',
            source,
        )

    def test_main_window_initializes_shutdown_guard(self):
        source = Path("app.py").read_text(encoding="utf-8")
        module = ast.parse(source)
        window = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow"
        )
        initializer = next(
            node
            for node in window.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        assignments = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Assign)
        ]
        self.assertTrue(
            any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == "closing"
                for assignment in assignments
                for target in assignment.targets
            )
        )


if __name__ == "__main__":
    unittest.main()
