import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from hrtf_manager import FILENAME_RE
from nova7x import ChatMixState, Nova7X
from pipewire_eq import PipeWireEQ
from pipewire_mic_eq import PipeWireMicEQ
from pipewire_mixer import PipeWireMixer
from pipewire_spatial import PipeWireSpatial
from state_io import atomic_write_json


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
    def test_rewrites_all_sofa_filenames(self):
        source = 'filename = "/old/a.sofa"\nfilename="/old/b.sofa"'
        changed, count = FILENAME_RE.subn('filename = "/new/hrtf.sofa"', source)
        self.assertEqual(count, 2)
        self.assertNotIn("/old/", changed)


class ShutdownTests(unittest.TestCase):
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
