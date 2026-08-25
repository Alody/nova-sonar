"""Discover hardware and transactionally install Nova Sonar PipeWire graphs."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from state_io import atomic_write_text


PROJECT_DIR = Path(__file__).resolve().parent
PIPEWIRE_DIR = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"
PRODUCT_MARKERS = ("1038", "22ad")
RNNOISE_CANDIDATES = (
    Path("/usr/lib64/ladspa/librnnoise_ladspa.so"),
    Path("/usr/lib/ladspa/librnnoise_ladspa.so"),
    Path("/usr/local/lib64/ladspa/librnnoise_ladspa.so"),
    Path("/usr/local/lib/ladspa/librnnoise_ladspa.so"),
    Path.home() / ".ladspa" / "librnnoise_ladspa.so",
)
LSP_ROOTS = (Path("/usr/lib64/lv2"), Path("/usr/lib/lv2"), Path.home() / ".lv2")


def _run(args: list[str], *, timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"could not run {' '.join(args)}: {error}") from error
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(args)}")
    return result.stdout


def _pactl_objects(kind: str) -> list[dict]:
    output = _run(["pactl", "--format=json", "list", kind])
    try:
        objects = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"pactl returned malformed {kind} JSON") from error
    if not isinstance(objects, list):
        raise RuntimeError(f"pactl returned an unexpected {kind} structure")
    return [item for item in objects if isinstance(item, dict)]


def _hardware_name(kind: str) -> str:
    candidates: list[tuple[int, str]] = []
    for item in _pactl_objects(kind):
        name = str(item.get("name", ""))
        if not name or name.startswith("nova_sonar_"):
            continue
        props = item.get("properties", {})
        if not isinstance(props, dict):
            props = {}
        searchable = " ".join(f"{key}={value}" for key, value in props.items()).lower()
        score = sum(marker in searchable for marker in PRODUCT_MARKERS)
        if "steelseries" in searchable:
            score += 1
        if "nova 7" in searchable or "nova_7" in searchable:
            score += 1
        if score >= 2:
            candidates.append((score, name))
    if not candidates:
        singular = "sink" if kind == "sinks" else "source"
        raise RuntimeError(
            f"Nova 7X {singular} was not found. Connect the headset and dongle, "
            "select the expected profile, and retry."
        )
    candidates.sort(reverse=True)
    return candidates[0][1]


def _rnnoise_plugin() -> Path:
    for path in RNNOISE_CANDIDATES:
        if path.is_file():
            return path.resolve()
    raise RuntimeError(
        "librnnoise_ladspa.so was not found. Install the host build of "
        "noise-suppression-for-voice before running setup."
    )


def _check_lsp() -> None:
    for root in LSP_ROOTS:
        if root.is_dir() and any(root.glob("lsp-plugins.lv2/para_equalizer_x16_ms.ttl")):
            return
    raise RuntimeError(
        "LSP LV2 plugins were not found. On Fedora/Bazzite install the "
        "lsp-plugins-lv2 package on the host and reboot if package layering was used."
    )


def _check_hid_access() -> None:
    matches: list[Path] = []
    for uevent in Path("/sys/class/hidraw").glob("hidraw*/device/uevent"):
        try:
            text = uevent.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if all(marker in text for marker in PRODUCT_MARKERS):
            matches.append(Path("/dev") / uevent.parents[1].name)
    if not matches:
        raise RuntimeError(
            "Nova 7X HID interface 1038:22ad was not found. Connect and power on "
            "the headset before running setup."
        )
    inaccessible = [path for path in matches if not os.access(path, os.R_OK | os.W_OK)]
    if inaccessible:
        raise RuntimeError(
            "Nova 7X HID access is denied for: "
            + ", ".join(str(path) for path in inaccessible)
            + ". Install an appropriate udev rule, reconnect the dongle, and retry."
        )


def _render(template: str, replacements: dict[str, str]) -> str:
    text = (PROJECT_DIR / template).read_text(encoding="utf-8")
    for token, value in replacements.items():
        text = text.replace(token, value)
    unresolved = [token for token in ("@HEADSET_SINK@", "@MIC_SOURCE@", "@RNNOISE_PLUGIN@") if token in text]
    if unresolved:
        raise RuntimeError(f"unresolved template tokens in {template}: {', '.join(unresolved)}")
    return text


def _wait_for_eq() -> None:
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        try:
            names = {str(item.get("name", "")) for item in _pactl_objects("sinks")}
        except RuntimeError:
            names = set()
        if "nova_sonar_eq" in names:
            return
        time.sleep(0.4)
    raise RuntimeError("nova_sonar_eq did not appear after restarting PipeWire")


def install_graphs() -> None:
    _check_hid_access()
    _check_lsp()
    headset_sink = _hardware_name("sinks")
    microphone_source = _hardware_name("sources")
    rnnoise = _rnnoise_plugin()
    rendered = {
        PIPEWIRE_DIR / "90-nova-sonar-mic-eq.conf": _render(
            "90-nova-sonar-mic-eq.conf",
            {"@MIC_SOURCE@": microphone_source, "@RNNOISE_PLUGIN@": str(rnnoise)},
        ),
        PIPEWIRE_DIR / "91-nova-sonar-advanced-eq.conf": _render(
            "91-nova-sonar-advanced-eq.conf",
            {"@HEADSET_SINK@": headset_sink},
        ),
    }
    previous: dict[Path, str | None] = {}
    PIPEWIRE_DIR.mkdir(parents=True, exist_ok=True)
    for path in rendered:
        previous[path] = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        for path, text in rendered.items():
            atomic_write_text(path, text)
        _run(["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"])
        _wait_for_eq()
    except Exception:
        for path, text in previous.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, text)
        try:
            _run(["systemctl", "--user", "restart", "pipewire", "pipewire-pulse", "wireplumber"])
        except RuntimeError:
            pass
        raise
    print(f"Playback sink: {headset_sink}")
    print(f"Microphone source: {microphone_source}")
    print(f"RNNoise plugin: {rnnoise}")
    print("Playback and microphone graphs are active.")


def main() -> None:
    try:
        install_graphs()
    except RuntimeError as error:
        raise SystemExit(f"Audio graph setup failed: {error}") from error


if __name__ == "__main__":
    main()
