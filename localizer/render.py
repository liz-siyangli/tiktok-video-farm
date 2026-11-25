"""Step 4: final video = original footage
  - Chinese burned-in subtitles hidden (blurred band, optional)
  - English subtitles burned in (TikTok style)
  - English voice-over, with the original audio muted or ducked underneath, plus optional BGM
"""
from __future__ import annotations

import json
import subprocess
from functools import lru_cache
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


def probe(path: Path) -> dict:
    """Display-oriented video facts. iPhone portrait .mov files are stored landscape with a
    rotation tag; ffmpeg auto-rotates when decoding, so width/height here are what you see."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,pix_fmt,color_transfer:stream_tags=rotate"
         ":stream_side_data=rotation:format=duration",
         "-of", "json", str(path)], check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    rot = 0
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(st.get("tags", {}).get("rotate", 0) or 0)
    w, h = int(st["width"]), int(st["height"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {
        "width": w, "height": h, "duration": float(data["format"]["duration"]),
        "codec": st.get("codec_name", ""), "pix_fmt": st.get("pix_fmt", ""), "rotation": rot,
        "hdr": st.get("color_transfer", "") in ("arib-std-b67", "smpte2084"),
    }


def video_info(path: Path) -> tuple[int, int, float]:
    i = probe(path)
    return i["width"], i["height"], i["duration"]


def browser_friendly(info: dict) -> bool:
    """Can Chrome/Safari play the original directly with correct colors?"""
    return info["codec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p") and not info["hdr"]


@lru_cache(maxsize=1)
def _has_zscale() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " zscale " in out


def sdr_filter(info: dict) -> str:
    """iPhone 'HDR video' (HLG) looks washed out if encoded to normal H.264 as-is: tone-map to SDR."""
    if info["hdr"] and _has_zscale():
        return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    return "format=yuv420p"


def output_size(w: int, h: int, short_side: int = 1080) -> tuple[int, int]:
    """TikTok tops out at 1080p; rendering 4K is slow and wasted."""
    scale = min(1.0, short_side / min(w, h))
    return int(w * scale) // 2 * 2, int(h * scale) // 2 * 2


def make_preview(src: Path, out: Path, info: dict) -> None:
    """Small H.264 proxy for the browser preview (HEVC / HDR / ProRes sources)."""
    w, h = output_size(info["width"], info["height"], 720)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vf", f"{sdr_filter(info)},scale={w}:{h}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


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
    info = probe(video)
    w, h = output_size(info["width"], info["height"])
    dur = info["duration"]
    ass = workdir / "en.ass"
    write_ass(cues, ass, w, h, cfg)

    mask = cfg.get("mask", {})
    vf = [f"[0:v]{sdr_filter(info)},scale={w}:{h},setsar=1[src]"]
    vchain = "[src]"
    if mask.get("enabled", True):
        y0 = int(h * mask.get("y_ratio", 0.70)) // 2 * 2
        mh = min(h - y0, int(h * mask.get("h_ratio", 0.16))) // 2 * 2
        vf.append(f"[src]split[base][band]")
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
        "-map", "[v]", "-map", "[a]", "-map_metadata", "-1", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
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
