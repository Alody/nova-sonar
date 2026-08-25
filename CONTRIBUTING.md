# Contributing

Use Python 3.11 or newer and keep hardware access out of unit tests.

```bash
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Before opening a pull request, run the test suite and `python -m compileall -q
.`. Hardware-facing changes should describe the tested headset product ID,
firmware, PipeWire version, and Linux distribution.

Do not commit recordings, raw captures, local state, virtual environments,
downloaded plugins, HRTFs, or device-specific absolute paths.

