"""Step 1: get Chinese subtitles with timestamps.

Two sources:
  * an existing SRT you exported from 剪映 / CapCut / Premiere (most accurate), or
  * Whisper speech recognition on the video's audio (faster-whisper, runs locally, free).
"""
from __future__ import annotations

from pathlib import Path

from .srt import Cue, write_srt


def transcribe(video: Path, out_srt: Path, model_size: str = "large-v3", device: str = "auto") -> list[Cue]:
    from faster_whisper import WhisperModel  # imported lazily: heavy

    compute = "int8" if device in ("cpu", "auto") else "float16"
    model = WhisperModel(model_size, device=device, compute_type=compute)
    segments, info = model.transcribe(
        str(video),
        language="zh",
        vad_filter=True,
        # nudges Whisper toward simplified Chinese + punctuation
        initial_prompt="以下是普通话的句子，使用简体中文，有标点。",
    )
    cues = [Cue(i + 1, s.start, s.end, s.text.strip()) for i, s in enumerate(segments) if s.text.strip()]
    write_srt(cues, out_srt)
    return cues
