"""Command-line entry: python -m localizer <command> VIDEO ...

Workdir for each video: work/<video-stem>/
  zh.srt            Chinese cues (from Whisper or your own SRT)
  prompt.md         (manual mode) paste into claude.ai
  translation.json  Claude's raw answer
  en.srt            English script on the original timeline  <- edit this to fix wording
  review.md         side-by-side zh/en review table
  vo.wav            English voice-over track
  en.final.srt      subtitles re-timed to the actual voice
  tts_report.json   per-line timing / speed-up report
  <stem>.en.mp4     final video
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tomllib
from pathlib import Path

from .srt import read_srt, write_srt

ROOT = Path(__file__).resolve().parent.parent


def load_cfg(path: str | None) -> dict:
    p = Path(path) if path else ROOT / "config.toml"
    return tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def workdir_for(video: Path, base: Path) -> Path:
    wd = base / video.stem
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def cmd_extract(a, cfg, wd: Path):
    out = wd / "zh.srt"
    if a.srt:
        write_srt(read_srt(a.srt), out)
        print(f"使用已有字幕 → {out}")
    else:
        from .transcribe import transcribe

        print(f"Whisper 识别中（模型 {a.whisper_model}，首次运行会下载模型）...")
        cues = transcribe(a.video, out, a.whisper_model)
        print(f"识别出 {len(cues)} 句 → {out}")
    print("建议打开 zh.srt 快速扫一眼，有识别错的字直接改。")


def cmd_translate(a, cfg, wd: Path):
    from . import translate as tr

    cues = read_srt(wd / "zh.srt")
    if a.mode == "manual":
        p = wd / "prompt.md"
        tr.write_manual_prompt(cues, cfg, p, a.context or "")
        print(f"已生成 {p}\n1) 整个文件内容粘贴到 claude.ai\n2) 把 Claude 回复的 JSON 存成 {wd / 'reply.json'}\n"
              f"3) 运行: python -m localizer apply-manual \"{a.video}\"")
        return
    print("调用 Claude 翻译/删改/本地化中...")
    result = tr.translate_api(cues, cfg, a.context or "", a.model)
    en = tr.save_outputs(result, cues, wd, cfg.get("words_per_second", 2.6))
    print(f"完成：{len(cues)} 句中文 → {len(en)} 句英文。审阅 {wd / 'review.md'}，需要改就改 {wd / 'en.srt'}")


def cmd_apply_manual(a, cfg, wd: Path):
    from . import translate as tr

    reply = Path(a.reply) if a.reply else wd / "reply.json"
    result = tr.parse_reply(reply.read_text(encoding="utf-8"))
    en = tr.save_outputs(result, read_srt(wd / "zh.srt"), wd, cfg.get("words_per_second", 2.6))
    print(f"已导入：{len(en)} 句英文。审阅 {wd / 'review.md'}")


def cmd_tts(a, cfg, wd: Path):
    from .render import video_info
    from .tts import synthesize_all

    t = cfg.get("tts", {})
    _, _, dur = video_info(a.video)
    timed = synthesize_all(
        read_srt(wd / "en.srt"), wd, dur,
        backend=a.backend or t.get("backend", "edge"), voice=a.voice or t.get("voice", "en-US-AvaNeural"),
        base_rate=t.get("base_rate", 0), max_rate=t.get("max_rate", 30), max_atempo=t.get("max_atempo", 1.2),
    )
    import json

    rep = json.loads((wd / "tts_report.json").read_text())
    over = [r for r in rep if r["overflow"] > 0.05]
    print(f"配音完成：{len(timed)} 句 → {wd / 'vo.wav'}")
    if over:
        print(f"⚠️ 有 {len(over)} 句加速后仍超出时长（会压到下一句），建议在 en.srt 里缩短：")
        for r in over:
            print(f"   #{r['id']} 超 {r['overflow']}s: {r['text']}")


def cmd_render(a, cfg, wd: Path):
    from .render import render

    out = Path(a.out) if a.out else wd / f"{a.video.stem}.en.mp4"
    if a.no_mask:
        cfg.setdefault("mask", {})["enabled"] = False
    render(a.video, read_srt(wd / "en.final.srt"), wd / "vo.wav", out, wd, cfg, Path(a.bgm) if a.bgm else None)
    print(f"✅ 成片：{out}")


def cmd_preview_mask(a, cfg, wd: Path):
    from .render import preview_mask

    out = wd / "mask_preview.png"
    preview_mask(a.video, out, cfg, a.at)
    print(f"红框 = 会被模糊的区域 → {out}（调整 config.toml 的 [mask]）")


def cmd_run(a, cfg, wd: Path):
    if not (wd / "zh.srt").exists() or a.srt:
        cmd_extract(a, cfg, wd)
    cmd_translate(a, cfg, wd)
    if a.mode == "manual":
        return
    cmd_tts(a, cfg, wd)
    cmd_render(a, cfg, wd)


def main(argv=None):
    if not shutil.which("ffmpeg"):
        sys.exit("需要先安装 ffmpeg（Mac: brew install ffmpeg）")
    p = argparse.ArgumentParser(prog="localizer", description="中文试穿视频 → 英文 TikTok 视频")
    p.add_argument("--config")
    p.add_argument("--workdir", default=str(ROOT / "work"))
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("video", type=Path)
        sp.set_defaults(fn=fn)
        return sp

    def tr_args(sp):
        sp.add_argument("--mode", choices=["api", "manual"], default="api")
        sp.add_argument("--context", help="补充背景，例如 '乌鲁木齐冬天拍的，羽绒服，169元'")
        sp.add_argument("--model")

    def ex_args(sp):
        sp.add_argument("--srt", help="已有的中文 SRT（剪映可导出），提供则跳过语音识别")
        sp.add_argument("--whisper-model", default="large-v3")

    def tts_args(sp):
        sp.add_argument("--backend", choices=["edge", "say", "elevenlabs", "dummy"])
        sp.add_argument("--voice")

    def render_args(sp):
        sp.add_argument("--bgm", help="背景音乐文件")
        sp.add_argument("--out")
        sp.add_argument("--no-mask", action="store_true", help="原视频没有烧录中文字幕时用")

    ex_args(add("extract", cmd_extract, "1. 提取中文字幕"))
    tr_args(add("translate", cmd_translate, "2. 翻译+删改+本地化"))
    am = add("apply-manual", cmd_apply_manual, "2b. 导入 claude.ai 的回复")
    am.add_argument("--reply")
    tts_args(add("tts", cmd_tts, "3. 生成英文配音"))
    render_args(add("render", cmd_render, "4. 合成成片"))
    pm = add("preview-mask", cmd_preview_mask, "预览中文字幕遮挡区域")
    pm.add_argument("--at", type=float, default=2.0)
    r = add("run", cmd_run, "一键跑完 1-4")
    ex_args(r); tr_args(r); tts_args(r); render_args(r)

    a = p.parse_args(argv)
    cfg = load_cfg(a.config)
    a.fn(a, cfg, workdir_for(a.video, Path(a.workdir)))


if __name__ == "__main__":
    main()
