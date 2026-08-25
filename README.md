# Nova Sonar

Nova Sonar is a Linux/PipeWire open-source replacement for SteelSeries GG desktop controller specifically for the SteelSeries Arctis
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

Before installation, connect and power on the Nova 7X and select a profile that
exposes both its playback sink and microphone source. Install the LSP LV2
plugins and the RNNoise LADSPA plugin on the host; the installer verifies both
and prints an actionable error without installing autostart if either is
missing.

```bash
git clone https://github.com/Alody/nova-sonar.git
cd nova-sonar
chmod +x install-user.sh
./install-user.sh
```

The installer discovers the connected headset endpoints, renders and activates
the playback and microphone graphs transactionally, creates an isolated stable
environment under `~/.local/share/nova-sonar`, installs launcher commands under
`~/.local/bin`, and only then enables desktop-session autostart. It does not use
`sudo` or modify the immutable system image.

Install and activate optional spatial audio after the main installer succeeds:

```bash
nova-sonar-hrtf install
nova-sonar-hrtf use
systemctl --user enable nova-sonar-game.service
nova-sonar-diagnostics
```

`nova-sonar-hrtf install` downloads only the default ARI NH1230 file. Use
`nova-sonar-hrtf install --all` to download the complete audition pack.

Launch it from anywhere with `nova-sonar` or `~/.local/bin/nova-sonar`.
Run `nova-sonar-diagnostics` at any time to check commands, Python packages,
PipeWire services and nodes, headset discovery, and unresolved configuration
tokens. Add `--json` for machine-readable output.

### Bazzite

Run the installer from the normal Bazzite desktop session, not inside a
Toolbox/Distrobox container. Nova Sonar needs the host session's PipeWire
socket, HID devices, desktop autostart directory, and user systemd instance.
The installer writes only to the clone and the user's home directory, so it
does not modify Bazzite's immutable system image.

Fedora supplies the LSP dependency as `lsp-plugins-lv2`. If it is absent on the
host, `rpm-ostree install lsp-plugins-lv2` can layer it, followed by a reboot.
Bazzite recommends package layering only for system-level software when a
user-space option is unavailable. RNNoise must provide
`librnnoise_ladspa.so` on the host in a standard LADSPA directory; use the
upstream noise-suppression-for-voice installation instructions. Do not install
audio plugins only inside Toolbox/Distrobox because the host PipeWire process
cannot load them. The installer also verifies read/write access to the Nova 7X
`hidraw` interface. If it reports permission denial, install a host udev rule
for USB ID `1038:22ad`, reconnect the dongle, and rerun the installer.

For development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nova-sonar
```

## PipeWire graph templates

The repository contains portable templates for the advanced output,
microphone, and spatial Game graphs:

- `91-nova-sonar-advanced-eq.conf`
- `90-nova-sonar-mic-eq.conf`
- `92-nova-sonar-game.conf`

The installer discovers and replaces these tokens automatically. They remain
documented for manual or development installations:

| Token | Meaning |
|---|---|
| `@HEADSET_SINK@` | Physical Nova 7X playback sink from `pactl list sinks` |
| `@MIC_SOURCE@` | Physical Nova 7X microphone source from `pactl list sources` |
| `@RNNOISE_PLUGIN@` | Absolute path to `librnnoise_ladspa.so` |

For a manual installation, install rendered copies under
`~/.config/pipewire/pipewire.conf.d/`, then restart `pipewire`,
`pipewire-pulse`, and `wireplumber`. Never install templates containing
unresolved `@...@` tokens.

The Game/Chat buses are created automatically. Spatial audio uses a dedicated
`nova-sonar-game.service` and a filter-chain graph under
`~/.config/pipewire/filter-chain.conf.d/92-nova-sonar-game.conf`. Use
`nova-sonar-hrtf install` to download supported HRTFs.

The installer installs the spatial graph and registers the user service but
deliberately does not enable it. The graph defaults to ARI NH1230. Run
`nova-sonar-hrtf install`, select and validate it with `nova-sonar-hrtf use`,
then persist it with `systemctl --user enable nova-sonar-game.service`. The
HRTF command refuses to start spatial processing until `nova_sonar_eq` is
available. Playback EQ,
microphone EQ, ChatMix, routing, and the spectrum analyzers do not depend on
this optional service. `nova-sonar-diagnostics` reports the graph and service
independently so failures cannot be silent.

## Uninstall

```bash
./uninstall-user.sh
```

This removes launchers, autostart, services, installed graphs, and the managed
virtual environment, then restarts the user audio services. Saved application
settings and downloaded HRTFs are retained.

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

## Bazzite smoke test

After installation, save the initial report:

```bash
nova-sonar-diagnostics | tee nova-sonar-diagnostics.txt
```

Then verify:

1. Launch Nova Sonar and confirm the Game and Chat sinks appear.
2. Switch among all tabs; only the visible spectrum should capture audio.
3. Hide, minimize, and restore the window; spectrum capture should stop and
   resume without spawning repeated `parec` processes.
4. Move an application between Game and Chat, restart it, and confirm its route
   is restored.
5. Adjust playback and microphone EQ, wait one second, restart Nova Sonar, and
   confirm the settings persist.
6. Restart PipeWire and WirePlumber and confirm buses, routing, EQ, and spatial
   state recover.
7. Quit through the tray and confirm no `nova-sonar`, `parec`, or
   `pactl subscribe` process remains.

Useful live logs and state:

```bash
journalctl --user -b -u pipewire -u pipewire-pulse -u wireplumber --no-pager
journalctl --user -b -u nova-sonar-game.service --no-pager
pactl list short sinks
pactl list short sink-inputs
pw-cli ls Node
```

To capture application diagnostics, launch it from a terminal:

```bash
NOVA_SONAR_LOG_LEVEL=DEBUG nova-sonar 2>&1 | tee nova-sonar.log
```
