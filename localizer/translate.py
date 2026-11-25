"""Step 2: translate + condense + localize the Chinese cues into an English VO script.

Two modes:
  * api    -> calls the Claude API directly (needs ANTHROPIC_API_KEY).
  * manual -> writes a prompt file you paste into claude.ai (uses your subscription, no API cost),
              then you save Claude's JSON reply and run `apply-manual`.
"""
from __future__ import annotations

import json
from pathlib import Path

from .srt import Cue, write_srt

PROMPT_DIR = Path(__file__).parent / "prompts"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_ids": {"type": "array", "items": {"type": "integer"}},
                    "en": {"type": "string"},
                    "change": {"type": "string", "enum": ["translate", "condense", "localize", "rewrite"]},
                    "note_zh": {"type": "string"},
                },
                "required": ["source_ids", "en", "change", "note_zh"],
                "additionalProperties": False,
            },
        },
        "dropped_ids": {"type": "array", "items": {"type": "integer"}},
        "title_suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lines", "dropped_ids", "title_suggestions"],
    "additionalProperties": False,
}


def build_prompts(cues: list[Cue], cfg: dict, context: str = "") -> tuple[str, str]:
    wps = cfg.get("words_per_second", 2.6)
    rules = "\n".join(f"   - {r}" for r in cfg.get("localization_rules", [])) or "   - (none)"
    system = (PROMPT_DIR / "system.md").read_text(encoding="utf-8").format(
        brand=cfg.get("brand", "Giselle Select"),
        host=cfg.get("host", "Giselle"),
        wps=wps,
        keep_pct=int(cfg.get("keep_ratio", 0.7) * 100),
        rules=rules,
    )
    rows = [
        {
            "id": c.index,
            "start": round(c.start, 2),
            "end": round(c.end, 2),
            "max_words": max(2, int(c.duration * wps)),
            "zh": c.text,
        }
        for c in cues
    ]
    user = ""
    if context:
        user += f"Video context from the owner: {context}\n\n"
    user += (
        "Chinese cues (JSON):\n```json\n"
        + json.dumps(rows, ensure_ascii=False, indent=1)
        + "\n```\n\nReturn JSON matching the schema: lines (in order), dropped_ids, and 3 short TikTok "
        "caption/title suggestions in English."
    )
    return system, user


def translate_api(cues: list[Cue], cfg: dict, context: str = "", model: str | None = None) -> dict:
    import anthropic

    system, user = build_prompts(cues, cfg, context)
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=model or cfg.get("model", "claude-opus-5-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if a safety classifier declines, retry on a fallback model
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("response truncated (max_tokens); split the video into shorter parts")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def write_manual_prompt(cues: list[Cue], cfg: dict, out: Path, context: str = "") -> None:
    system, user = build_prompts(cues, cfg, context)
    schema = json.dumps(OUTPUT_SCHEMA, indent=1)
    out.write_text(
        f"{system}\n\n---\n\n{user}\n\nReply with ONLY a JSON object (no markdown fence) matching this JSON schema:\n{schema}\n",
        encoding="utf-8",
    )


def parse_reply(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def result_to_cues(result: dict, cues: list[Cue]) -> list[Cue]:
    """Map Claude's lines back onto the source timeline."""
    by_id = {c.index: c for c in cues}
    out: list[Cue] = []
    for line in result["lines"]:
        src = [by_id[i] for i in line["source_ids"] if i in by_id]
        if not src or not line["en"].strip():
            continue
        out.append(
            Cue(
                len(out) + 1,
                min(c.start for c in src),
                max(c.end for c in src),
                line["en"].strip(),
                meta={
                    "source_ids": line["source_ids"],
                    "zh": " / ".join(c.text for c in src),
                    "change": line.get("change", ""),
                    "note_zh": line.get("note_zh", ""),
                },
            )
        )
    out.sort(key=lambda c: c.start)
    return out


def save_outputs(result: dict, cues: list[Cue], workdir: Path, wps: float) -> list[Cue]:
    en = result_to_cues(result, cues)
    (workdir / "translation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_srt(en, workdir / "en.srt")

    zh_chars = sum(len(c.text) for c in cues)
    en_words = sum(len(c.text.split()) for c in en)
    md = [
        "# 字幕本地化审阅表",
        "",
        f"- 原中文 {len(cues)} 条 / {zh_chars} 字 → 英文 {len(en)} 条 / {en_words} 词",
        f"- 删除的原句 id: {result.get('dropped_ids', [])}",
        "- **改英文请直接编辑 `en.srt`**（时间轴可以微调），然后再跑 `tts` 步骤。",
        "",
        "| # | 时间 | 中文原文 | English | 处理 | 说明 | 语速 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in en:
        words = len(c.text.split())
        need = words / wps
        flag = "⚠️ 偏长" if need > c.duration * 1.15 else "OK"
        md.append(
            f"| {c.index} | {c.start:.1f}-{c.end:.1f}s | {c.meta['zh']} | {c.text} | {c.meta['change']} | "
            f"{c.meta['note_zh']} | {flag} ({need:.1f}s/{c.duration:.1f}s) |"
        )
    if result.get("title_suggestions"):
        md += ["", "## 标题/文案建议", *[f"- {t}" for t in result["title_suggestions"]]]
    (workdir / "review.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return en


def rewrite_line(zh: str, en: str, instruction: str, max_words: int, cfg: dict, model: str | None = None) -> str:
    """Rewrite a single English VO line per the owner's instruction. Returns the new line only."""
    import anthropic

    system, _ = build_prompts([], cfg)
    user = (
        f"Original Chinese: {zh}\nCurrent English line: {en}\n"
        f"Hard limit: {max_words} words.\nOwner's request: {instruction or 'make it more natural and punchy'}\n\n"
        "Reply with only the new English line, nothing else."
    )
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=model or cfg.get("model", "claude-opus-5-5"),
        max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined this rewrite")
    return "".join(b.text for b in resp.content if b.type == "text").strip().strip('"')
