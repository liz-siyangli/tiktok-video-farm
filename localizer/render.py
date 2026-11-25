"""Step 4: final video = original footage
  - Chinese burned-in subtitles hidden (blurred band, optional)
  - English subtitles burned in (TikTok style)
  - English voice-over, with the original audio muted or ducked underneath, plus optional BGM
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .srt import Cue

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def video_info(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "default=nw=1", str(path)], check=True, capture_output=True, text=True,
    ).stdout
    kv = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
    return int(kv["width"]), int(kv["height"]), float(kv["duration"])


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout
    return bool(out.strip())


def _ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(cues: list[Cue], path: Path, w: int, h: int, cfg: dict) -> None:
    sub = cfg.get("subtitle", {})
    size = int(h * sub.get("font_size_ratio", 0.042))
    # default: subtitle sits where the Chinese one was (centre of the mask band)
    y_center = sub.get("y_ratio", 0.78)
    margin_v = int(h * (1 - y_center) - size / 2)
    hdr = ASS_HEADER.format(
        w=w, h=h, font=sub.get("font", "Inter"), size=size, outline=max(2, size // 12),
        margin_lr=int(w * 0.08), margin_v=margin_v,
    )
    lines = []
    for c in cues:
        text = c.text.replace("\n", " ").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_ass_ts(c.start)},{_ass_ts(c.end)},Default,,0,0,0,,{text}")
    path.write_text(hdr + "\n".join(lines) + "\n", encoding="utf-8")


def _escape_filter_path(p: Path) -> str:
    return str(p.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render(video: Path, cues: list[Cue], vo: Path, out: Path, workdir: Path, cfg: dict,
           bgm: Path | None = None) -> None:
    w, h, dur = video_info(video)
    ass = workdir / "en.ass"
    write_ass(cues, ass, w, h, cfg)

    mask = cfg.get("mask", {})
    vchain = "[0:v]"
    vf = []
    if mask.get("enabled", True):
        y0 = int(h * mask.get("y_ratio", 0.70)) // 2 * 2
        mh = int(h * mask.get("h_ratio", 0.16)) // 2 * 2
        vf.append(f"[0:v]split[base][band]")
        vf.append(f"[band]crop={w}:{mh}:0:{y0},boxblur=luma_radius=24:luma_power=3[blur]")
        vf.append(f"[base][blur]overlay=0:{y0}[masked]")
        vchain = "[masked]"
    vf.append(f"{vchain}subtitles=filename='{_escape_filter_path(ass)}'[v]")

    audio = cfg.get("audio", {})
    orig_vol = float(audio.get("original_volume", 0.0))
    inputs = ["-i", str(video), "-i", str(vo)]
    af = [f"[1:a]volume={audio.get('voice_volume', 1.0)}[vo]"]
    mix = ["[vo]"]
    if orig_vol > 0 and has_audio(video):
        af.append(f"[0:a]volume={orig_vol}[orig]")
        mix.append("[orig]")
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]
        af.append(f"[{inputs.count('-i') - 1}:a]volume={audio.get('bgm_volume', 0.12)},atrim=0:{dur:.3f}[bgm]")
        mix.append("[bgm]")
    af.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first,alimiter=limit=0.95[a]")

    cmd = [
        "ffmpeg", "-y", *inputs, "-filter_complex", ";".join(vf + af),
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.3f}", "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def preview_mask(video: Path, out_png: Path, cfg: dict, at: float = 2.0) -> None:
    """Saves a frame with the mask band outlined in red, to tune mask.y_ratio / h_ratio."""
    w, h, _ = video_info(video)
    mask = cfg.get("mask", {})
    y0 = int(h * mask.get("y_ratio", 0.70))
    mh = int(h * mask.get("h_ratio", 0.16))
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(at), "-i", str(video), "-frames:v", "1",
         "-vf", f"drawbox=x=0:y={y0}:w={w}:h={mh}:color=red@0.9:t=6", str(out_png)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
