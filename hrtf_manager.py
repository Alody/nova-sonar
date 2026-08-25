#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from state_io import atomic_write_text


CONFIG = (
    Path.home()
    / ".config"
    / "pipewire"
    / "filter-chain.conf.d"
    / "92-nova-sonar-game.conf"
)

SERVICE = "nova-sonar-game.service"

HRTF_DIR = (
    Path.home()
    / ".local"
    / "share"
    / "nova-sonar"
    / "hrtf"
)

PRESETS = {
    "ari-nh1230": {
        "label": "ARI DTF-d NH1230",
        "path": HRTF_DIR / "ari_dtf_d_nh1230.sofa",
        "url": (
            "https://sofacoustics.org/data/database/"
            "ari/dtf%20d_nh1230.sofa"
        ),
    },
    "kemar": {
        "label": "MIT KEMAR Normal Pinna",
        "path": Path(
            "/usr/share/libmysofa/"
            "MIT_KEMAR_normal_pinna.sofa"
        ),
        "url": None,
    },
    "ari-nh5": {
        "label": "ARI DTF-b NH5",
        "path": HRTF_DIR / "ari_dtf_b_nh5.sofa",
        "url": (
            "https://sofacoustics.org/data/database/"
            "ari/dtf%20b_nh5.sofa"
        ),
    },
    "ari-nh21": {
        "label": "ARI DTF-b NH21",
        "path": HRTF_DIR / "ari_dtf_b_nh21.sofa",
        "url": (
            "https://sofacoustics.org/data/database/"
            "ari/dtf%20b_nh21.sofa"
        ),
    },
    "viking-a": {
        "label": "Viking Subject A",
        "path": HRTF_DIR / "viking_subj_A.sofa",
        "url": (
            "https://sofacoustics.org/data/database/"
            "viking/subj_A.sofa"
        ),
    },
    "viking-m": {
        "label": "Viking Subject M",
        "path": HRTF_DIR / "viking_subj_M.sofa",
        "url": (
            "https://sofacoustics.org/data/database/"
            "viking/subj_M.sofa"
        ),
    },
}

DEFAULT_PRESET = "ari-nh1230"

COMMAND_TIMEOUT = 15.0

FILENAME_RE = re.compile(
    r'filename\s*=\s*"[^"]+\.sofa"'
)
EXPECTED_SOFA_NODES = 8
SOFA_MAGICS = (b"CDF\x01", b"CDF\x02", b"\x89HDF\r\n\x1a\n")


def is_sofa_file(path: Path) -> bool:
    try:
        if path.stat().st_size < 100_000:
            return False
        with path.open("rb") as source:
            header = source.read(8)
    except OSError:
        return False
    return any(header.startswith(magic) for magic in SOFA_MAGICS)


def run(
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"command timed out: {' '.join(args)}") from error

    if check and result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "command failed"
        )

    return result


def download(
    url: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nova-Sonar-HRTF-Manager/2.0",
        },
    )

    temp = destination.with_suffix(
        destination.suffix + ".part"
    )

    print(f"Downloading {destination.name}...")

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            with temp.open("wb") as output:
                shutil.copyfileobj(
                    response,
                    output,
                )

        if not is_sofa_file(temp):
            raise RuntimeError(
                "download is not a valid SOFA/netCDF file"
            )

        temp.replace(destination)
        print(f"  saved: {destination}")

    except Exception:
        temp.unlink(missing_ok=True)
        raise


def install(names: list[str] | None = None) -> None:
    HRTF_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    selected = names or [DEFAULT_PRESET]
    failures: list[str] = []
    for name in selected:
        preset = PRESETS[name]
        url = preset["url"]
        path = Path(preset["path"])

        if url is None:
            status = "ready" if is_sofa_file(path) else "MISSING"
            print(f"{name:10} {status}: {path}")
            continue

        if is_sofa_file(path):
            print(f"{name:10} already downloaded: {path}")
            continue

        try:
            download(str(url), path)
        except Exception as error:
            failures.append(name)
            print(
                f"{name:10} download failed: {error}",
                file=sys.stderr,
            )

    print()
    if failures:
        raise SystemExit(
            "HRTF setup incomplete; failed presets: "
            + ", ".join(failures)
        )
    print("HRTF pack setup complete.")


def list_presets() -> None:
    print("Available Nova Sonar HRTFs:")
    print()

    for name, preset in PRESETS.items():
        path = Path(preset["path"])
        status = "ready" if is_sofa_file(path) else "missing"

        default = " (default)" if name == DEFAULT_PRESET else ""
        print(
            f"  {name:10} "
            f"{status:7} "
            f"{preset['label']}{default}"
        )


def restart_game_service() -> None:
    print(f"Restarting {SERVICE}...")

    run(
        [
            "systemctl",
            "--user",
            "restart",
            SERVICE,
        ]
    )

    result = run(
        [
            "systemctl",
            "--user",
            "is-active",
            SERVICE,
        ],
        check=False,
    )

    if result.stdout.strip() != "active":
        raise RuntimeError(
            f"{SERVICE} did not come back active"
        )


def backup_config() -> Path:
    backup = CONFIG.with_suffix(
        CONFIG.suffix + ".hrtf-backup"
    )

    if not backup.exists():
        shutil.copy2(CONFIG, backup)
        print(f"Created backup: {backup}")

    return backup


def use_preset(name: str) -> None:
    if name not in PRESETS:
        raise SystemExit(
            f"Unknown HRTF preset: {name}"
        )

    if not CONFIG.exists():
        raise SystemExit(
            "Dedicated Game filter config was not found:\n"
            f"{CONFIG}\n\n"
            "Install the rendered spatial filter-chain graph first; see README.md."
        )

    preset = PRESETS[name]
    hrtf_path = Path(preset["path"])

    if not is_sofa_file(hrtf_path):
        raise SystemExit(
            f"HRTF is missing or invalid:\n{hrtf_path}\n\n"
            "Run:\n"
            "  nova-sonar-hrtf install"
        )

    sink_check = run(["pactl", "list", "short", "sinks"], check=False)
    if sink_check.returncode != 0 or not any(
        line.split("\t", 2)[1] == "nova_sonar_eq"
        for line in sink_check.stdout.splitlines()
        if "\t" in line
    ):
        raise SystemExit(
            "Nova Sonar playback EQ is not active. Run install-user.sh "
            "successfully before enabling spatial audio."
        )

    backup_config()

    old_text = CONFIG.read_text(
        encoding="utf-8"
    )

    matches = FILENAME_RE.findall(old_text)

    if len(matches) != EXPECTED_SOFA_NODES:
        raise SystemExit(
            "The Game filter config is incomplete: "
            f"expected {EXPECTED_SOFA_NODES} SOFA nodes, "
            f"found {len(matches)}."
        )

    new_text = FILENAME_RE.sub(
        f'filename = "{hrtf_path}"',
        old_text,
    )

    atomic_write_text(CONFIG, new_text)

    try:
        restart_game_service()

    except Exception as error:
        atomic_write_text(CONFIG, old_text)

        try:
            restart_game_service()
        except Exception:
            pass

        raise SystemExit(
            "Could not load the selected HRTF; "
            "the previous config was restored.\n"
            f"Reason: {error}"
        )

    print()
    print(f"Active HRTF: {preset['label']}")
    print(f"Updated {len(matches)} SOFA nodes.")
    print(
        "Only nova-sonar-game.service was restarted."
    )
    print(
        "If Nova Sonar is open, its node watcher will "
        "reapply the saved Spatial ON/OFF state."
    )


def restore() -> None:
    backup = CONFIG.with_suffix(
        CONFIG.suffix + ".hrtf-backup"
    )

    if not backup.exists():
        raise SystemExit(
            f"No backup found:\n{backup}"
        )

    old_text = CONFIG.read_text(
        encoding="utf-8"
    )

    shutil.copy2(
        backup,
        CONFIG,
    )

    try:
        restart_game_service()

    except Exception as error:
        atomic_write_text(CONFIG, old_text)
        restart_game_service()

        raise SystemExit(
            f"Restore failed: {error}"
        )

    print("Original HRTF config restored.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Install and switch HRTFs for Nova Sonar "
            "without restarting the main PipeWire daemon."
        )
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    install_parser = sub.add_parser(
        "install",
        help="Download NH1230 by default, one preset, or the full audition pack.",
    )
    install_parser.add_argument("preset", nargs="?", choices=sorted(PRESETS))
    install_parser.add_argument("--all", action="store_true", dest="all_presets")

    sub.add_parser(
        "list",
        help="List installed HRTFs.",
    )

    use = sub.add_parser(
        "use",
        help=(
            "Select an HRTF and restart only "
            "nova-sonar-game.service."
        ),
    )

    use.add_argument(
        "preset",
        choices=sorted(PRESETS),
        default=DEFAULT_PRESET,
        nargs="?",
    )

    sub.add_parser(
        "restore",
        help="Restore the original HRTF graph backup.",
    )

    args = parser.parse_args()

    if args.command == "install":
        if args.all_presets and args.preset:
            parser.error("install accepts either a preset or --all, not both")
        names = list(PRESETS) if args.all_presets else ([args.preset] if args.preset else None)
        install(names)
    elif args.command == "list":
        list_presets()
    elif args.command == "use":
        use_preset(args.preset)
    elif args.command == "restore":
        restore()


if __name__ == "__main__":
    main()
