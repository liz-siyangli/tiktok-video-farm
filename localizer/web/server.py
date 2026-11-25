"""Local web app: python -m localizer.web  ->  http://localhost:8000

Each project lives in work/<project-id>/ with project.json as its single source of truth.
Long steps (Whisper, Claude, TTS, render) run as background jobs the page polls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import translate as tr
from ..cli import ROOT, load_cfg
from ..render import render, video_info
from ..srt import Cue, read_srt, write_srt
from ..tts import mix_track, slots_for, synth_line

WORK = Path(os.environ.get("LOCALIZER_WORK", ROOT / "work"))
WORK.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Giselle Select Localizer")
_lock = threading.RLock()
JOBS: dict[str, dict] = {}

EDGE_VOICES = [
    {"id": "en-US-AvaNeural", "label": "Ava · 温暖自然（推荐）"},
    {"id": "en-US-EmmaNeural", "label": "Emma · 轻快活泼"},
    {"id": "en-US-JennyNeural", "label": "Jenny · 亲切"},
    {"id": "en-US-AriaNeural", "label": "Aria · 自信"},
    {"id": "en-US-MichelleNeural", "label": "Michelle · 成熟"},
    {"id": "en-US-AnaNeural", "label": "Ana · 年轻"},
]


# ---------------------------------------------------------------- project store

def pdir(pid: str) -> Path:
    d = (WORK / pid).resolve()
    if d.parent != WORK.resolve() or not (d / "project.json").exists():
        raise HTTPException(404, "project not found")
    return d


def load(pid: str) -> dict:
    return json.loads((pdir(pid) / "project.json").read_text(encoding="utf-8"))


def save(p: dict) -> dict:
    with _lock:
        p["updated"] = time.time()
        path = WORK / p["id"] / "project.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return decorate(p)


def decorate(p: dict) -> dict:
    """Copy for the page, with each line's TTS state: none / ok / stale (text or voice changed)."""
    out = copy.deepcopy(p)
    for l in out["en"]:
        t = l.get("tts")
        l["tts_state"] = "none" if not t else ("ok" if t.get("hash") == line_hash(p, l) else "stale")
    return out


def default_settings() -> dict:
    cfg = load_cfg(None)
    return {
        "context": "",
        "mask": {"enabled": True, "y_ratio": 0.68, "h_ratio": 0.18, **cfg.get("mask", {})},
        "subtitle": {"y_ratio": 0.78, "font_size_ratio": 0.042, "font": "Inter", "enabled": True, **cfg.get("subtitle", {})},
        "tts": {"backend": "edge", "voice": "en-US-AvaNeural", "base_rate": 5, "max_rate": 30, "max_atempo": 1.2,
                **cfg.get("tts", {})},
        "audio": {"original_volume": 0.0, "voice_volume": 1.0, "bgm_volume": 0.12, **cfg.get("audio", {})},
    }


def run_cfg(p: dict) -> dict:
    """Merge config.toml (brand, rules, model) with this project's settings."""
    cfg = copy.deepcopy(load_cfg(None))
    for k, v in p["settings"].items():
        cfg[k] = v
    return cfg


def line_hash(p: dict, line: dict) -> str:
    t = p["settings"]["tts"]
    return hashlib.md5(f"{line['text']}|{t['backend']}|{t['voice']}|{t['base_rate']}".encode()).hexdigest()[:10]


# ---------------------------------------------------------------- jobs

def start_job(pid: str, kind: str, fn) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "project": pid, "kind": kind, "status": "running", "progress": "", "error": None,
           "started": time.time()}
    JOBS[jid] = job

    def runner():
        try:
            fn(job)
            job["status"] = "done"
        except Exception as e:  # surface any failure to the page
            traceback.print_exc()
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=runner, daemon=True).start()
    return job


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


# ---------------------------------------------------------------- status / projects

@app.get("/api/status")
def status():
    cfg = load_cfg(None)
    return {
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        "has_elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "has_say": shutil.which("say") is not None,
        "brand": cfg.get("brand", "Giselle Select"),
        "voices": EDGE_VOICES,
        "rules": cfg.get("localization_rules", []),
        "words_per_second": cfg.get("words_per_second", 2.6),
    }


@app.get("/api/projects")
def list_projects():
    out = []
    for f in sorted(WORK.glob("*/project.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": p["id"], "name": p["name"], "stage": stage(p), "updated": p.get("updated", 0)})
    return out


def stage(p: dict) -> str:
    if p.get("output"):
        return "已出成片"
    if any(l.get("tts") for l in p.get("en", [])):
        return "已配音"
    if p.get("en"):
        return "已翻译"
    if p.get("zh"):
        return "已有中文字幕"
    return "新建"


@app.post("/api/projects")
async def create_project(file: UploadFile = File(...)):
    pid = time.strftime("%m%d-") + uuid.uuid4().hex[:6]
    d = WORK / pid
    d.mkdir(parents=True)
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    src = d / f"source{ext}"
    with src.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        w, h, dur = video_info(src)
    except Exception:
        shutil.rmtree(d)
        raise HTTPException(400, "无法读取视频，请确认是 mp4/mov 文件")
    p = {
        "id": pid, "name": Path(file.filename or pid).stem, "video": src.name,
        "width": w, "height": h, "duration": dur, "created": time.time(),
        "settings": default_settings(), "zh": [], "en": [], "titles": [], "dropped_ids": [],
        "output": None, "bgm": None,
    }
    return save(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return decorate(load(pid))


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: dict = Body(...)):
    """Save edits from the page: name, settings, zh cues, en lines, titles."""
    with _lock:
        p = load(pid)
        for key in ("name", "zh", "titles"):
            if key in body:
                p[key] = body[key]
        if "settings" in body:
            for k, v in body["settings"].items():
                if isinstance(v, dict):
                    p["settings"].setdefault(k, {}).update(v)
                else:
                    p["settings"][k] = v
        if "en" in body:
            old = {l["id"]: l for l in p["en"]}
            new = []
            for l in body["en"]:
                l.pop("tts_state", None)
                # tts state is owned by the server (a TTS job may be writing it right now)
                l["tts"] = old.get(l["id"], {}).get("tts")
                new.append(l)
            p["en"] = sorted(new, key=lambda l: l["start"])
        return save(p)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    shutil.rmtree(pdir(pid))
    return {"ok": True}


@app.get("/media/{pid}/{name:path}")
def media(pid: str, name: str):
    d = pdir(pid)
    f = (d / name).resolve()
    if d not in f.parents or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f)


# ---------------------------------------------------------------- step 1: Chinese subtitles

@app.post("/api/projects/{pid}/zh-srt")
async def upload_srt(pid: str, file: UploadFile = File(...)):
    d = pdir(pid)
    path = d / "zh.upload.srt"
    path.write_bytes(await file.read())
    cues = read_srt(path)
    if not cues:
        raise HTTPException(400, "SRT 里没有读到字幕")
    p = load(pid)
    p["zh"] = [{"id": c.index, "start": round(c.start, 2), "end": round(c.end, 2), "text": c.text} for c in cues]
    return save(p)


@app.post("/api/projects/{pid}/extract")
def extract(pid: str, body: dict = Body(default={})):
    d = pdir(pid)
    model = body.get("model", "large-v3")

    def work(job):
        from ..transcribe import transcribe

        job["progress"] = f"Whisper {model} 识别中（首次会下载模型，几分钟）..."
        p = load(pid)
        cues = transcribe(d / p["video"], d / "zh.srt", model)
        p = load(pid)
        p["zh"] = [{"id": c.index, "start": round(c.start, 2), "end": round(c.end, 2), "text": c.text} for c in cues]
        save(p)

    return start_job(pid, "extract", work)


# ---------------------------------------------------------------- step 2: English script

def zh_cues(p: dict) -> list[Cue]:
    return [Cue(c["id"], c["start"], c["end"], c["text"]) for c in p["zh"]]


def apply_result(p: dict, result: dict) -> dict:
    cues = tr.result_to_cues(result, zh_cues(p))
    p["en"] = [
        {"id": i + 1, "start": round(c.start, 2), "end": round(c.end, 2), "text": c.text,
         "source_ids": c.meta["source_ids"], "zh": c.meta["zh"], "change": c.meta["change"],
         "note_zh": c.meta["note_zh"], "tts": None}
        for i, c in enumerate(cues)
    ]
    p["titles"] = result.get("title_suggestions", [])
    p["dropped_ids"] = result.get("dropped_ids", [])
    p["output"] = None
    return save(p)


@app.post("/api/projects/{pid}/translate")
def translate(pid: str):
    p = load(pid)
    if not p["zh"]:
        raise HTTPException(400, "还没有中文字幕")

    def work(job):
        job["progress"] = "Claude 正在翻译、删改、本地化..."
        p = load(pid)
        result = tr.translate_api(zh_cues(p), run_cfg(p), p["settings"].get("context", ""))
        apply_result(load(pid), result)

    return start_job(pid, "translate", work)


@app.get("/api/projects/{pid}/prompt", response_class=PlainTextResponse)
def manual_prompt(pid: str):
    p = load(pid)
    out = pdir(pid) / "prompt.md"
    tr.write_manual_prompt(zh_cues(p), run_cfg(p), out, p["settings"].get("context", ""))
    return out.read_text(encoding="utf-8")


@app.post("/api/projects/{pid}/reply")
def manual_reply(pid: str, body: dict = Body(...)):
    try:
        result = tr.parse_reply(body.get("text", ""))
    except Exception as e:
        raise HTTPException(400, f"JSON 解析失败：{e}")
    return apply_result(load(pid), result)


@app.post("/api/projects/{pid}/lines/{lid}/rewrite")
def rewrite(pid: str, lid: int, body: dict = Body(default={})):
    p = load(pid)
    line = next((l for l in p["en"] if l["id"] == lid), None)
    if not line:
        raise HTTPException(404)
    wps = load_cfg(None).get("words_per_second", 2.6)
    max_words = max(2, int((line["end"] - line["start"]) * wps))
    try:
        text = tr.rewrite_line(line.get("zh", ""), line["text"], body.get("instruction", ""), max_words, run_cfg(p))
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"text": text}


# ---------------------------------------------------------------- step 3: voice-over

@app.post("/api/projects/{pid}/tts")
def tts(pid: str, body: dict = Body(default={})):
    only = body.get("line_id")
    force = bool(body.get("force"))
    d = pdir(pid)

    def work(job):
        p = load(pid)
        t = p["settings"]["tts"]
        lines = p["en"]
        slots = slots_for([l["start"] for l in lines], p["duration"])
        todo = [
            (l, s) for l, s in zip(lines, slots)
            if (only is None or l["id"] == only)
            and (force or only is not None or not l.get("tts") or l["tts"].get("hash") != line_hash(p, l))
        ]
        for k, (l, slot) in enumerate(todo, 1):
            job["progress"] = f"配音 {k}/{len(todo)}：{l['text'][:40]}"
            h = line_hash(p, l)
            wav = d / "tts" / f"line{l['id']:03d}-{h}.wav"
            info = synth_line(l["text"], slot, wav, t["backend"], t["voice"],
                              t.get("base_rate", 0), t.get("max_rate", 30), t.get("max_atempo", 1.2))
            with _lock:
                fresh = load(pid)
                for fl in fresh["en"]:
                    if fl["id"] == l["id"]:
                        fl["tts"] = {**info, "file": f"tts/{wav.name}", "hash": h}
                fresh["output"] = None
                save(fresh)
        job["progress"] = f"完成 {len(todo)} 句"

    return start_job(pid, "tts", work)


@app.post("/api/projects/{pid}/bgm")
async def upload_bgm(pid: str, file: UploadFile = File(...)):
    d = pdir(pid)
    ext = Path(file.filename or "bgm.mp3").suffix or ".mp3"
    path = d / f"bgm{ext}"
    path.write_bytes(await file.read())
    p = load(pid)
    p["bgm"] = path.name
    return save(p)


@app.delete("/api/projects/{pid}/bgm")
def delete_bgm(pid: str):
    p = load(pid)
    p["bgm"] = None
    return save(p)


# ---------------------------------------------------------------- step 4: render

@app.post("/api/projects/{pid}/render")
def do_render(pid: str):
    d = pdir(pid)
    p = load(pid)
    missing = [l["id"] for l in p["en"] if not l.get("tts") or l["tts"].get("hash") != line_hash(p, l)]
    if missing:
        raise HTTPException(400, f"这些句子还没配音或改过文字：{missing}，请先生成配音")

    def work(job):
        p = load(pid)
        job["progress"] = "合成配音音轨..."
        clips = [(l["start"], d / l["tts"]["file"]) for l in p["en"]]
        mix_track(clips, p["duration"], d / "vo.wav")
        timed = [Cue(i + 1, l["start"], l["start"] + l["tts"]["audio"], l["text"]) for i, l in enumerate(p["en"])]
        write_srt(timed, d / "en.final.srt")
        cfg = run_cfg(p)
        if not p["settings"]["subtitle"].get("enabled", True):
            timed = []
        job["progress"] = "渲染视频（遮字幕 + 英文字幕 + 混音）..."
        out = d / f"{p['name']}.en.mp4"
        render(d / p["video"], timed, d / "vo.wav", out, d, cfg, d / p["bgm"] if p.get("bgm") else None)
        p = load(pid)
        p["output"] = out.name
        p["output_at"] = time.time()
        save(p)

    return start_job(pid, "render", work)


@app.get("/api/projects/{pid}/srt/{which}")
def download_srt(pid: str, which: str):
    p = load(pid)
    d = pdir(pid)
    if which == "zh":
        cues = zh_cues(p)
    elif which == "en":
        cues = [Cue(i + 1, l["start"], l["end"], l["text"]) for i, l in enumerate(p["en"])]
    else:
        raise HTTPException(404)
    path = d / f"{which}.download.srt"
    write_srt(cues, path)
    return FileResponse(path, filename=f"{p['name']}.{which}.srt")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
