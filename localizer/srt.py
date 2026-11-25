"""Minimal SRT read/write helpers (no external dependency)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Cue:
    index: int
    start: float  # seconds
    end: float
    text: str
    meta: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _parse_ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(f"bad timestamp: {s!r}")
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")[:3]) / 1000


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def read_srt(path: str | Path) -> list[Cue]:
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        ts_i = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_i is None:
            continue
        a, b = lines[ts_i].split("-->")
        text = "\n".join(lines[ts_i + 1 :]).strip()
        cues.append(Cue(len(cues) + 1, _parse_ts(a), _parse_ts(b), text))
    return cues


def write_srt(cues: list[Cue], path: str | Path) -> None:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_ts(c.start)} --> {fmt_ts(c.end)}\n{c.text.strip()}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")
