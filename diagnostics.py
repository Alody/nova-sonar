"""Nova Sonar preflight diagnostics without GUI dependencies."""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import re
from pathlib import Path


COMMANDS = ("pactl", "pw-cli", "pw-dump", "parec")
NODE_NAMES = ("nova_sonar_eq", "nova_sonar_mic", "nova_sonar_game")
SOFA_FILENAME_RE = re.compile(r'filename\s*=\s*"([^"]+\.sofa)"')
SOFA_MAGICS = (b"CDF\x01", b"CDF\x02", b"\x89HDF\r\n\x1a\n")


def _valid_sofa(path: Path) -> bool:
    try:
        if path.stat().st_size < 100_000:
            return False
        with path.open("rb") as source:
            header = source.read(8)
    except OSError:
        return False
    return any(header.startswith(magic) for magic in SOFA_MAGICS)


def _run(args: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = result.stdout.strip() or result.stderr.strip()
    return result.returncode == 0, output


def collect() -> list[dict[str, str | bool]]:
    results: list[dict[str, str | bool]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append({"name": name, "ok": ok, "detail": detail})

    for command in COMMANDS:
        path = shutil.which(command)
        add(f"command:{command}", path is not None, path or "not found")

    for module in ("numpy", "PySide6"):
        found = importlib.util.find_spec(module) is not None
        add(f"python:{module}", found, "available" if found else "not installed")

    ok, output = _run(["pactl", "info"])
    server = next(
        (line for line in output.splitlines() if line.startswith("Server Name:")),
        output.splitlines()[0] if output else "no response",
    )
    add("pipewire-pulse", ok and "PipeWire" in output, server)

    ok, output = _run(["pw-dump"])
    discovered: set[str] = set()
    if ok:
        try:
            objects = json.loads(output)
            if not isinstance(objects, list):
                raise TypeError
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                info = obj.get("info", {})
                if not isinstance(info, dict):
                    continue
                props = info.get("props", {})
                if not isinstance(props, dict):
                    continue
                name = props.get("node.name")
                if name in NODE_NAMES:
                    discovered.add(name)
        except (TypeError, json.JSONDecodeError):
            ok = False
    add("pipewire-graph", ok, ", ".join(sorted(discovered)) or "no Nova nodes")
    for node in NODE_NAMES:
        add(f"node:{node}", node in discovered, "present" if node in discovered else "missing")

    matching_hid = []
    for filename in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        try:
            text = Path(filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "1038" in text.lower() and "22ad" in text.lower():
            matching_hid.append(filename)
    add(
        "headset-hid",
        bool(matching_hid),
        ", ".join(matching_hid) if matching_hid else "Nova 7X HID interface not found",
    )

    unresolved = []
    pipewire_root = Path.home() / ".config" / "pipewire"
    for path in pipewire_root.glob("**/*.conf"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(token in text for token in ("@HEADSET_SINK@", "@MIC_SOURCE@", "@RNNOISE_PLUGIN@")):
            unresolved.append(str(path))
    add(
        "rendered-config",
        not unresolved,
        ", ".join(unresolved) if unresolved else "no unresolved template tokens",
    )

    spatial_config = (
        Path.home() / ".config" / "pipewire" / "filter-chain.conf.d"
        / "92-nova-sonar-game.conf"
    )
    add(
        "spatial-config",
        spatial_config.is_file(),
        str(spatial_config) if spatial_config.is_file() else f"missing: {spatial_config}",
    )
    sofa_paths: list[Path] = []
    if spatial_config.is_file():
        try:
            spatial_text = spatial_config.read_text(encoding="utf-8")
            sofa_paths = [Path(value) for value in SOFA_FILENAME_RE.findall(spatial_text)]
        except OSError:
            pass
    unique_sofas = list(dict.fromkeys(sofa_paths))
    spatial_hrtf_ok = len(sofa_paths) == 8 and all(_valid_sofa(path) for path in unique_sofas)
    add(
        "spatial-hrtf",
        spatial_hrtf_ok,
        (
            ", ".join(str(path) for path in unique_sofas)
            if spatial_hrtf_ok
            else f"expected 8 valid SOFA references; found {len(sofa_paths)}"
        ),
    )
    spatial_service = (
        Path.home() / ".config" / "systemd" / "user" / "nova-sonar-game.service"
    )
    add(
        "spatial-service",
        spatial_service.is_file(),
        str(spatial_service) if spatial_service.is_file() else f"missing: {spatial_service}",
    )
    service_ok, service_output = _run(
        ["systemctl", "--user", "is-active", "nova-sonar-game.service"]
    )
    add(
        "spatial-service-active",
        service_ok and service_output.strip() == "active",
        service_output or "inactive",
    )

    ok, output = _run(
        ["systemctl", "--user", "is-active", "pipewire", "pipewire-pulse", "wireplumber"]
    )
    active_count = sum(line.strip() == "active" for line in output.splitlines())
    add(
        "user-audio-services",
        ok and active_count == 3,
        output or "unable to query user services",
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Nova Sonar runtime readiness")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    results = collect()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            marker = "PASS" if result["ok"] else "WARN"
            print(f"[{marker}] {result['name']}: {result['detail']}")


if __name__ == "__main__":
    main()
