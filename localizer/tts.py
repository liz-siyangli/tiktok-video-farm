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


def synth_line(
    text: str, slot: float, wav: Path, backend: str, voice: str,
    base_rate: int = 0, max_rate: int = 30, max_atempo: float = 1.25,
) -> dict:
    """Synthesize one line into `wav`, speeding it up to fit `slot` seconds if needed."""
    synth = BACKENDS[backend]
    wav.parent.mkdir(parents=True, exist_ok=True)
    raw = wav.with_suffix(".raw.mp3")
    rate = base_rate
    synth(text, raw, voice, rate)
    dur = probe_duration(raw)

    # 1) ask the TTS engine itself to speak faster (sounds most natural)
    if dur > slot:
        rate = min(max_rate, math.ceil((dur / slot - 1) * 100) + base_rate + 3)
        if rate > base_rate:
            synth(text, raw, voice, rate)
            dur = probe_duration(raw)

    # 2) still too long -> time-stretch (keeps pitch) up to max_atempo
    tempo = min(max_atempo, dur / slot) if dur > slot else 1.0
    if tempo > 1.001:
        run(["ffmpeg", "-y", "-i", str(raw), "-af", atempo_chain(tempo), "-ac", "1", "-ar", "44100", str(wav)])
    else:
        to_wav(raw, wav)
    raw.unlink(missing_ok=True)
    final = probe_duration(wav)
    return {
        "slot": round(slot, 2), "audio": round(final, 2), "rate_pct": rate,
        "atempo": round(tempo, 3), "overflow": round(max(0.0, final - slot), 2),
    }


def slots_for(starts: list[float], video_duration: float, gap: float = 0.12) -> list[float]:
    """Available time for each line = until the next line starts (or the video ends)."""
    out = []
    for i, s in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else video_duration
        out.append(max(0.3, nxt - s - gap))
    return out


def mix_track(clips: list[tuple[float, Path]], video_duration: float, out: Path) -> None:
    """Place each (start_seconds, wav) clip on one mono track of length video_duration."""
    if not clips:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{video_duration:.3f}", str(out)])
        return
    inputs, filters = [], []
    for k, (start, wav) in enumerate(clips):
        inputs += ["-i", str(wav)]
        ms = int(start * 1000)
        filters.append(f"[{k}:a]adelay={ms}|{ms}[a{k}]")
    mix = "".join(f"[a{k}]" for k in range(len(clips)))
    filters.append(
        f"{mix}amix=inputs={len(clips)}:normalize=0:dropout_transition=0,apad=whole_dur={video_duration:.3f}[out]"
    )
    script = out.with_name(out.stem + "_filter.txt")
    script.write_text(";\n".join(filters), encoding="utf-8")
    run(["ffmpeg", "-y", *inputs, "-filter_complex_script", str(script), "-map", "[out]",
         "-ac", "1", "-ar", "44100", "-t", f"{video_duration:.3f}", str(out)])


def synthesize_all(
    cues: list[Cue], workdir: Path, video_duration: float, backend: str, voice: str,
    base_rate: int = 0, max_rate: int = 30, max_atempo: float = 1.25,
) -> list[Cue]:
    """CLI path. Returns cues re-timed to the actual audio. Writes tts/*.wav, vo.wav, en.final.srt, tts_report.json."""
    tdir = workdir / "tts"
    slots = slots_for([c.start for c in cues], video_duration)
    timed, report = [], []
    for c, slot in zip(cues, slots):
        wav = tdir / f"{c.index:03d}.wav"
        info = synth_line(c.text, slot, wav, backend, voice, base_rate, max_rate, max_atempo)
        report.append({"id": c.index, "start": round(c.start, 2), **info, "text": c.text})
        timed.append(Cue(c.index, c.start, c.start + info["audio"], c.text, meta={**c.meta, "wav": str(wav)}))
    mix_track([(c.start, Path(c.meta["wav"])) for c in timed], video_duration, workdir / "vo.wav")
    write_srt(timed, workdir / "en.final.srt")
    (workdir / "tts_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return timed


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None
