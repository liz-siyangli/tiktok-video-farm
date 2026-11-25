# tiktok-video-farm

把以前拍的**中文试穿视频**，批量改成面向美国 TikTok Shop 的**英文视频**（Giselle Select）。

```
原视频(中文口播+中文字幕)
  │ 1. extract    Whisper 识别中文 → zh.srt（或直接用剪映导出的 SRT）
  │ 2. translate  Claude：翻译 + 删减到 ~70% + 本土化（乌鲁木齐 -10°C → Alaska, 14°F）
  │               → en.srt + review.md（中英对照审阅表）      ← 人工审阅/修改这一步
  │ 3. tts        AI 配音，每句自动提速塞进原时间段 → vo.wav
  │ 4. render     模糊遮掉中文字幕 + 烧录英文字幕 + 换成英文配音 → 成片 .en.mp4
```

## 安装（Mac）

```bash
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # 只有 api 模式需要
```

## 用法

```bash
# 一键跑完（Claude API + 免费 Edge 配音）
python -m localizer run videos/羽绒服试穿.mp4 --context "乌鲁木齐冬天拍的，白鸭绒羽绒服"

# 推荐的分步流程（每步都能停下来检查）
python -m localizer preview-mask videos/a.mp4          # 看红框是否盖住中文字幕，不准就调 config.toml [mask]
python -m localizer extract videos/a.mp4               # 或者 --srt 剪映导出的字幕.srt（更准，跳过识别）
python -m localizer translate videos/a.mp4 --context "..."
#   → 打开 work/a/review.md 看中英对照；要改英文就直接改 work/a/en.srt
python -m localizer tts videos/a.mp4                   # 会提示哪几句太长塞不下
python -m localizer render videos/a.mp4 --bgm music/lofi.mp3
#   → work/a/a.en.mp4
```

### 不想付 API 费用：manual 模式

```bash
python -m localizer translate videos/a.mp4 --mode manual
# 把 work/a/prompt.md 全部内容粘贴进 claude.ai，回复的 JSON 存为 work/a/reply.json
python -m localizer apply-manual videos/a.mp4
```

## 关键设计

| 问题 | 怎么处理的 |
|---|---|
| 中英文语速不同 | 每句按时长给 Claude 一个**英文词数上限**（默认 2.6 词/秒），允许合并相邻句、删掉口头禅 |
| 本土化 | `config.toml` 的 `localization_rules` 写品牌规则（城市/温度/身高体重/双十一/价格），会进提示词；每句标注 translate / condense / localize / rewrite 并附中文说明，方便快速审 |
| 合规 | 提示词禁止 AI 编造原视频没说的卖点（材质、功效、折扣），不提中国品牌名 |
| 配音塞不进时间 | 先让 TTS 引擎提速（最多 +30%，听感最自然），再不变调加速（最多 1.2×），仍超时会在终端列出让你改短 |
| 字幕和声音对齐 | 配完音后按真实音频时长重算字幕时间 → `en.final.srt` |
| 原视频的中文字幕 | 在字幕区域做模糊横带，英文字幕压在上面；没有烧录字幕用 `--no-mask` |
| 原中文声音 | 默认静音；`[audio] original_volume` 可保留一点环境声/BGM，或 `--bgm` 加新背景音乐 |

## 配音选择

| backend | 成本 | 效果 |
|---|---|---|
| `edge`（默认） | 免费 | 微软神经语音，挺自然。推荐 `en-US-AvaNeural` / `en-US-EmmaNeural` |
| `say` | 免费、离线 | macOS 自带，偏机械 |
| `elevenlabs` | 付费 | 最像真人，`--voice <voice_id>`，需 `ELEVENLABS_API_KEY` |
| `dummy` | — | 测试用的提示音 |

也可以只用到第 2 步：把 `en.srt` 导入剪映/CapCut，用它们自带的 TTS 和字幕样式。
