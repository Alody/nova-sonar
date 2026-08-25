"""Dependency-free parsing helpers for PulseAudio compatibility events."""

from __future__ import annotations

import re


EVENT_RE = re.compile(r"Event '([^']+)' on ([\w-]+)")
RELEVANT_FACILITIES = {"sink", "sink-input", "server"}


def parse_pactl_event(line: str) -> tuple[str, str] | None:
    match = EVENT_RE.search(line)
    if match is None or match.group(2) not in RELEVANT_FACILITIES:
        return None
    return match.group(1), match.group(2)
