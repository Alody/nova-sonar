# Nova Sonar

Nova Sonar is a Linux/PipeWire desktop controller for the SteelSeries Arctis
Nova 7X (`1038:22ad`). It provides physical ChatMix-wheel support, persistent
Game/Chat application routing, playback and microphone equalizers, RNNoise
microphone suppression, live spectrum analyzers, and optional HRTF spatial
audio.

The interface uses a dark Sonar-inspired mixer layout, application icons,
automatic route restoration, and a system tray. Closing the window keeps it
running; use the tray menu to quit completely.

## Platform requirements

- Linux with PipeWire, PipeWire Pulse, and WirePlumber
- Python 3.11 or newer
- `pactl`, `pw-cli`, `pw-dump`, and `parec`
- SteelSeries Arctis Nova 7X product ID `1038:22ad`
- Read/write permission for the headset's SteelSeries HID interfaces

Advanced audio graphs additionally require LSP Plugins LV2, RNNoise LADSPA
from [noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice),
and PipeWire's filter-chain/SOFA plugins.

## Install from a clone

```bash
git clone https://github.com/YOUR-ACCOUNT/nova-sonar.git
cd nova-sonar
chmod +x install-user.sh
./install-user.sh
```

The installer creates an isolated `.venv`, installs launcher commands under
`~/.local/bin`, registers the desktop icon, and enables desktop-session
autostart. It does not use `sudo` or modify the immutable system image.

Start it immediately with `~/.local/bin/nova-sonar`.

For development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nova-sonar
```

## PipeWire graph templates

The repository contains portable templates for the advanced output and
microphone graphs:

- `91-nova-sonar-advanced-eq.conf`
- `90-nova-sonar-mic-eq.conf`

Before installing them, replace these tokens with values from the target
machine:

| Token | Meaning |
|---|---|
| `@HEADSET_SINK@` | Physical Nova 7X playback sink from `pactl list sinks` |
| `@MIC_SOURCE@` | Physical Nova 7X microphone source from `pactl list sources` |
| `@RNNOISE_PLUGIN@` | Absolute path to `librnnoise_ladspa.so` |

Install rendered copies under
`~/.config/pipewire/pipewire.conf.d/`, then restart `pipewire`,
`pipewire-pulse`, and `wireplumber`. Never install templates containing
unresolved `@...@` tokens.

The Game/Chat buses are created automatically. Spatial audio uses a dedicated
`nova-sonar-game.service` and a filter-chain graph under
`~/.config/pipewire/filter-chain.conf.d/92-nova-sonar-game.conf`. Use
`nova-sonar-hrtf install` to download supported HRTFs.

## State and privacy

Runtime state is stored under `~/.config/nova-sonar` using atomic replacement.
Application routes use stable application identities rather than temporary
PipeWire IDs. Audio is processed locally; Nova Sonar does not upload recordings
or settings.

## Tests

```bash
.venv/bin/python -m compileall -q .
.venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions runs these checks on Python 3.11 and 3.13 for every push and
pull request.

## Publishing on GitHub

Before making the repository public:

1. Choose and add a project license.
2. Replace `YOUR-ACCOUNT` in the clone URL above.
3. Initialize Git and review exactly what will be committed:

   ```bash
   git init
   git add .
   git status --short
   git commit -m "Initial public release"
   git branch -M main
   git remote add origin git@github.com:YOUR-ACCOUNT/nova-sonar.git
   git push -u origin main
   ```

The `.gitignore` excludes the large virtual environment, local backups, raw
audio captures, test recordings, caches, runtime state, and environment files.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.
