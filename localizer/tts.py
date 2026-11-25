"""Step 3: English TTS for each subtitle line, squeezed to fit its time slot.

Backends:
  edge       Microsoft Edge neural voices via `edge-tts` (free, good quality, needs internet)
  say        macOS built-in `say` (free, offline, robotic-ish)
  elevenlabs ElevenLabs API (paid, most natural; needs ELEVENLABS_API_KEY)
  dummy      silent tone placeholder, for testing the pipeline offline
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from .srt import Cue, write_srt


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def to_wav(src: Path, dst: Path) -> None:
    run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "44100", str(dst)])


# ---------- backends: each writes `out` (any audio format) for `text` at speed `rate_pct` ----------

def _edge(text: str, out: Path, voice: str, rate_pct: int) -> None:
    import edge_tts

    rate = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))


def _say(text: str, out: Path, voice: str, rate_pct: int) -> None:
    wpm = int(185 * (1 + rate_pct / 100))
    run(["say", "-v", voice or "Samantha", "-r", str(wpm), "-o", str(out.with_suffix(".aiff")), text])
    out.with_suffix(".aiff").rename(out)


def _elevenlabs(text: str, out: Path, voice: str, rate_pct: int) -> None:
    import urllib.request

    key = os.environ["ELEVENLABS_API_KEY"]
    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "speed": min(1.2, 1 + rate_pct / 100)},
    }).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        data=body, headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    out.write_bytes(urllib.request.urlopen(req).read())


def _dummy(text: str, out: Path, voice: str, rate_pct: int) -> None:
    secs = len(text.split()) / 2.6 / (1 + rate_pct / 100)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={secs:.2f}", "-af", "volume=0.2", str(out)])


BACKENDS = {"edge": _edge, "say": _say, "elevenlabs": _elevenlabs, "dummy": _dummy}


def atempo_chain(factor: float) -> str:
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def synthesize_all(
    cues: list[Cue], workdir: Path, video_duration: float, backend: str, voice: str,
    base_rate: int = 0, max_rate: int = 30, max_atempo: float = 1.25, gap: float = 0.12,
) -> list[Cue]:
    """Returns cues re-timed to the actual spoken audio. Writes tts/*.wav, vo.wav, en.final.srt, tts_report.json."""
    synth = BACKENDS[backend]
    tdir = workdir / "tts"
    tdir.mkdir(exist_ok=True)
    timed: list[Cue] = []
    report = []

    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else video_duration
        slot = max(0.3, nxt - c.start - gap)
        raw = tdir / f"{c.index:03d}.raw.mp3"
        wav = tdir / f"{c.index:03d}.wav"

        rate = base_rate
        synth(c.text, raw, voice, rate)
        dur = probe_duration(raw)

        # 1) ask the TTS engine itself to speak faster (sounds most natural)
        if dur > slot:
            need = math.ceil((dur / slot - 1) * 100) + base_rate + 3
            rate = min(max_rate, need)
            if rate > base_rate:
                synth(c.text, raw, voice, rate)
                dur = probe_duration(raw)

        # 2) still too long -> time-stretch (keeps pitch) up to max_atempo
        tempo = 1.0
        if dur > slot:
            tempo = min(max_atempo, dur / slot)
        if tempo > 1.001:
            run(["ffmpeg", "-y", "-i", str(raw), "-af", atempo_chain(tempo), "-ac", "1", "-ar", "44100", str(wav)])
        else:
            to_wav(raw, wav)
        raw.unlink(missing_ok=True)
        final = probe_duration(wav)

        overflow = final - slot
        report.append({
            "id": c.index, "start": round(c.start, 2), "slot": round(slot, 2), "audio": round(final, 2),
            "rate_pct": rate, "atempo": round(tempo, 3), "overflow": round(max(0, overflow), 2),
            "text": c.text,
        })
        timed.append(Cue(c.index, c.start, c.start + final, c.text, meta={**c.meta, "wav": str(wav)}))

    # mix every clip onto one track at its start time
    vo = workdir / "vo.wav"
    inputs, filters = [], []
    for k, c in enumerate(timed):
        inputs += ["-i", c.meta["wav"]]
        ms = int(c.start * 1000)
        filters.append(f"[{k}:a]adelay={ms}|{ms}[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(len(timed)))
    filters.append(f"{mix}amix=inputs={len(timed)}:normalize=0:dropout_transition=0,apad=whole_dur={video_duration:.3f}[out]")
    script = workdir / "vo_filter.txt"
    script.write_text(";\n".join(filters), encoding="utf-8")
    run(["ffmpeg", "-y", *inputs, "-filter_complex_script", str(script), "-map", "[out]",
         "-ac", "1", "-ar", "44100", "-t", f"{video_duration:.3f}", str(vo)])

    write_srt(timed, workdir / "en.final.srt")
    (workdir / "tts_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return timed


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None
