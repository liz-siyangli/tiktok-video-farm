/* Giselle Select localizer — single-page front end (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (t) => { t = Math.max(0, t || 0); const m = Math.floor(t / 60); return `${m}:${(t % 60).toFixed(1).padStart(4, "0")}`; };

const S = { status: null, projects: [], P: null, tab: "frame", rewriteOpen: null, audio: {}, lastT: 0 };
const video = $("#video");

// ------------------------------------------------------------------ api helpers
async function api(method, url, body, isForm = false) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(url, opt);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || data || r.statusText);
  return data;
}

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), error ? 8000 : 3000);
}

async function runJob(url, body, doneMsg) {
  let job;
  try { job = await api("POST", url, body ?? {}); }
  catch (e) { toast(e.message, true); return false; }
  const bar = $("#jobbar");
  bar.hidden = false;
  $("#jobText").textContent = "开始...";
  while (job.status === "running") {
    await new Promise((r) => setTimeout(r, 900));
    job = await api("GET", `/api/jobs/${job.id}`);
    $("#jobText").textContent = job.progress || "处理中...";
  }
  bar.hidden = true;
  await reloadProject();
  if (job.status === "error") { toast("出错了：" + job.error, true); return false; }
  toast(doneMsg || "完成");
  return true;
}

let saveTimer = null, pending = {};
function save(fields, delay = 500) {
  Object.assign(pending, fields);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, delay);
}
async function flushSave() {
  clearTimeout(saveTimer);
  if (!S.P || !Object.keys(pending).length) return;
  const body = pending; pending = {};
  try {
    const p = await api("PATCH", `/api/projects/${S.P.id}`, body);
    // keep local objects (focus/typing) but refresh server-derived TTS state
    const byId = Object.fromEntries(p.en.map((l) => [l.id, l]));
    S.P.en.forEach((l) => { if (byId[l.id]) { l.tts = byId[l.id].tts; l.tts_state = byId[l.id].tts_state; } });
    if (S.tab === "tts" || S.tab === "out") renderTab();
    if ("name" in body) loadProjects();
  } catch (e) { toast("保存失败：" + e.message, true); }
}
const saveEn = () => save({ en: S.P.en.map(({ tts, tts_state, ...l }) => l) });
const saveSettings = () => save({ settings: S.P.settings });

// ------------------------------------------------------------------ projects
async function loadProjects() {
  S.projects = await api("GET", "/api/projects");
  $("#projects").innerHTML = S.projects.map((p) => `
    <li data-id="${p.id}" class="${S.P && S.P.id === p.id ? "active" : ""}">
      <span class="pname">${esc(p.name)}</span><span class="pstage">${esc(p.stage)}</span>
    </li>`).join("") || `<li class="muted" style="cursor:default">还没有项目</li>`;
}

async function openProject(id) {
  await flushSave();
  S.P = await api("GET", `/api/projects/${id}`);
  location.hash = id;
  $("#empty").hidden = true;
  $("#workspace").hidden = false;
  $("#projName").value = S.P.name;
  $("#showFinal").checked = false;
  $("#dubPreview").checked = false;
  setVideoSource();
  const P = S.P;
  S.tab = !P.zh.length ? "frame" : !P.en.length ? "en" : P.output ? "out" : "tts";
  renderAll();
  loadProjects();
}

async function reloadProject() {
  if (!S.P) return;
  const keepSrc = video.getAttribute("src");
  S.P = await api("GET", `/api/projects/${S.P.id}`);
  if ($("#showFinal").checked && S.P.output) setVideoSource(); else if (!keepSrc) setVideoSource();
  renderAll();
  loadProjects();
}

async function upload(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  $("#jobbar").hidden = false;
  $("#jobText").textContent = `上传 ${file.name}...`;
  try {
    const p = await api("POST", "/api/projects", fd, true);
    await loadProjects();
    await openProject(p.id);
    toast("上传成功。先在「画面设置」里框好中文字幕的位置");
  } catch (e) { toast(e.message, true); }
  $("#jobbar").hidden = true;
}

// ------------------------------------------------------------------ preview
function setVideoSource() {
  const P = S.P;
  const final = $("#showFinal").checked && P.output;
  video.src = final ? `/media/${P.id}/${encodeURIComponent(P.output)}?t=${P.output_at || 0}` : `/media/${P.id}/${encodeURIComponent(P.video)}`;
  video.controls = true;
  video.muted = $("#dubPreview").checked && !final;
  const phone = $("#phone");
  phone.style.aspectRatio = `${P.width} / ${P.height}`;
  phone.style.width = `min(100%, calc(72vh * ${P.width / P.height}))`;
  updateOverlay();
}

function currentLine(t) {
  return S.P.en.find((l) => t >= l.start && t < (l.tts ? l.start + l.tts.audio : l.end));
}

function updateOverlay() {
  if (!S.P) return;
  const { mask, subtitle } = S.P.settings;
  const final = $("#showFinal").checked && S.P.output;
  const band = $("#maskBand"), sub = $("#subLine"), phone = $("#phone");
  band.hidden = final || !mask.enabled;
  band.style.top = mask.y_ratio * 100 + "%";
  band.style.height = mask.h_ratio * 100 + "%";
  band.classList.toggle("outline", S.tab === "frame");

  const t = video.currentTime || 0;
  const line = currentLine(t);
  let text = line ? line.text : "";
  if (!text && S.tab === "frame") text = "This is how your English subtitles will look";
  sub.hidden = final || !subtitle.enabled || !text;
  sub.style.top = subtitle.y_ratio * 100 + "%";
  sub.style.fontSize = subtitle.font_size_ratio * phone.clientHeight + "px";
  $("span", sub).textContent = text;

  const zh = S.P.zh.find((c) => t >= c.start && t < c.end);
  $("#nowLine").textContent = zh ? `原文：${zh.text}` : "";
  document.querySelectorAll(".line").forEach((el) => el.classList.toggle("playing", line && +el.dataset.id === line.id));
}

// dub preview: play each line's TTS clip when the playhead crosses its start
function stopClips() { Object.values(S.audio).forEach((a) => a.pause()); }
function tick() {
  if (S.P && !video.paused) {
    const t = video.currentTime;
    if ($("#dubPreview").checked && !$("#showFinal").checked) {
      for (const l of S.P.en) {
        if (!l.tts) continue;
        if (S.lastT < l.start && t >= l.start && t - l.start < 0.5) {
          const src = `/media/${S.P.id}/${l.tts.file}`;
          const a = (S.audio[src] ||= new Audio(src));
          a.currentTime = t - l.start;
          a.play();
        }
      }
    }
    S.lastT = t;
    updateOverlay();
  }
  requestAnimationFrame(tick);
}
video.addEventListener("seeked", () => { S.lastT = video.currentTime - 0.01; stopClips(); updateOverlay(); });
video.addEventListener("pause", stopClips);
video.addEventListener("play", () => (S.lastT = video.currentTime - 0.01));
requestAnimationFrame(tick);
window.addEventListener("resize", updateOverlay);

function playRange(start, end) {
  video.currentTime = start;
  video.play();
  const stop = () => { if (video.currentTime >= end) { video.pause(); video.removeEventListener("timeupdate", stop); } };
  video.addEventListener("timeupdate", stop);
}

// ------------------------------------------------------------------ tabs
function renderAll() {
  const P = S.P;
  const done = {
    frame: true, zh: P.zh.length > 0, en: P.en.length > 0,
    tts: P.en.length > 0 && P.en.every((l) => l.tts_state === "ok"), out: !!P.output,
  };
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === S.tab);
    b.innerHTML = b.textContent.replace(/\s*$/, "") + (done[b.dataset.tab] && b.dataset.tab !== "frame" ? '<span class="dot"></span>' : "");
  });
  renderTab();
  updateOverlay();
}

function renderTab() {
  const fn = { frame: tabFrame, zh: tabZh, en: tabEn, tts: tabTts, out: tabOut }[S.tab];
  $("#tabBody").innerHTML = fn();
  updateOverlay();
}

const slider = (key, label, min, max, step, value, suffix = "") => `
  <label class="field">${label} <span class="val" data-val="${key}">${value}${suffix}</span>
    <input type="range" data-set="${key}" min="${min}" max="${max}" step="${step}" value="${value}">
  </label>`;

function tabFrame() {
  const { mask, subtitle } = S.P.settings;
  return `
  <div class="card">
    <h3>遮挡原视频里的中文字幕</h3>
    <p class="hint">左边预览里的红色虚线框会被模糊掉。拖动视频到有字幕的地方，调到刚好盖住中文字幕。</p>
    <label class="toggle" style="margin-bottom:10px"><input type="checkbox" data-set="mask.enabled" ${mask.enabled ? "checked" : ""}> 原视频有烧录的中文字幕，需要遮挡</label>
    <div class="grid2">
      ${slider("mask.y_ratio", "位置（距顶部）", 0, 0.95, 0.005, mask.y_ratio)}
      ${slider("mask.h_ratio", "高度", 0.03, 0.4, 0.005, mask.h_ratio)}
    </div>
  </div>
  <div class="card">
    <h3>英文字幕样式</h3>
    <p class="hint">默认压在被模糊的区域中间。</p>
    <label class="toggle" style="margin-bottom:10px"><input type="checkbox" data-set="subtitle.enabled" ${subtitle.enabled !== false ? "checked" : ""}> 烧录英文字幕</label>
    <div class="grid2">
      ${slider("subtitle.y_ratio", "位置（中心）", 0.1, 0.95, 0.005, subtitle.y_ratio)}
      ${slider("subtitle.font_size_ratio", "字号", 0.025, 0.07, 0.001, subtitle.font_size_ratio)}
    </div>
  </div>
  <div class="row"><span class="spacer"></span><button class="btn primary" data-goto="zh">下一步：中文字幕 →</button></div>`;
}

function tabZh() {
  const P = S.P;
  const head = `
  <div class="card">
    <h3>中文字幕来源</h3>
    <p class="hint">剪映导出的 SRT 最准；没有的话用 Whisper 自动识别（在你电脑本地运行，免费，第一次要下载模型）。</p>
    <div class="row">
      <button class="btn primary" data-act="whisper">🎙 Whisper 自动识别</button>
      <label class="btn">📄 上传 SRT<input type="file" accept=".srt" data-act="srt" hidden></label>
      ${P.zh.length ? `<a class="btn" href="/api/projects/${P.id}/srt/zh">下载 SRT</a>` : ""}
    </div>
  </div>`;
  if (!P.zh.length) return head;
  return head + `
  <div class="card">
    <h3>${P.zh.length} 句中文 <span class="muted" style="font-weight:400;font-size:13px">· 识别错的字直接改，点时间跳到那里</span></h3>
    ${P.zh.map((c, i) => `
      <div class="cue">
        <span class="time" data-seek="${c.start}" data-end="${c.end}">${fmt(c.start)} – ${fmt(c.end)}</span>
        <input type="text" data-zh="${i}" value="${esc(c.text)}">
        <button class="icon-btn" data-zhdel="${i}" title="删除">✕</button>
      </div>`).join("")}
  </div>
  <div class="row"><span class="spacer"></span><button class="btn primary" data-goto="en">下一步：英文脚本 →</button></div>`;
}

const CHANGE_LABEL = { translate: "直译", condense: "删减", localize: "本土化", rewrite: "重写", manual: "手动" };
const REWRITE_CHIPS = ["更短一点", "更口语、更像朋友聊天", "开头更抓人", "突出面料和保暖", "加一句号召下单"];

function slotOf(i) {
  const en = S.P.en;
  const next = i + 1 < en.length ? en[i + 1].start : S.P.duration;
  return Math.max(0.3, next - en[i].start - 0.12);
}
function wordInfo(i) {
  const l = S.P.en[i];
  const words = l.text.trim() ? l.text.trim().split(/\s+/).length : 0;
  const max = Math.max(2, Math.floor(slotOf(i) * (S.status.words_per_second || 2.6) * 1.3));
  const cls = words <= max * 0.85 ? "ok" : words <= max ? "tight" : "over";
  return { words, max, cls };
}

function tabEn() {
  const P = S.P;
  const st = S.status;
  if (!P.zh.length) return `<div class="card"><h3>先完成第 2 步</h3><p class="hint">需要先有中文字幕。</p><button class="btn primary" data-goto="zh">去中文字幕</button></div>`;
  const gen = `
  <div class="card">
    <h3>生成英文脚本</h3>
    <p class="hint">Claude 会按时长删减到约 70%、把国内的地名/温度/尺码/双十一等换成美国观众熟悉的说法，并标注每句改了什么。</p>
    <label class="field">视频背景（可选，帮助 Claude 理解）
      <textarea rows="2" data-set="context" placeholder="例如：乌鲁木齐冬天拍的，白鸭绒短款羽绒服，面向美国 20-35 岁女生">${esc(P.settings.context || "")}</textarea>
    </label>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" data-act="translate" ${st.has_api_key ? "" : "disabled"}>✨ Claude 生成${P.en.length ? "（会覆盖下面的修改）" : ""}</button>
      <button class="btn" data-act="manualToggle">手动模式：用 claude.ai</button>
      ${st.has_api_key ? "" : `<span class="muted" style="font-size:12px">没检测到 ANTHROPIC_API_KEY，可用手动模式</span>`}
    </div>
    <div id="manualBox" hidden style="margin-top:12px">
      <p class="hint">① 复制提示词 → ② 粘贴到 claude.ai 发送 → ③ 把它回复的 JSON 整个粘贴到下面 → ④ 导入</p>
      <div class="row"><button class="btn" data-act="copyPrompt">① 复制提示词</button></div>
      <textarea rows="5" id="replyText" placeholder='③ 粘贴 Claude 回复的 JSON：{"lines": [...], ...}' style="margin-top:8px"></textarea>
      <div class="row" style="margin-top:8px"><button class="btn dark" data-act="importReply">④ 导入</button></div>
    </div>
    <details style="margin-top:12px"><summary class="muted" style="cursor:pointer">当前的本土化规则（在 config.toml 里改）</summary>
      <ul>${(st.rules || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>
  </div>`;
  if (!P.en.length) return gen;

  const zhTotal = P.zh.reduce((n, c) => n + c.text.length, 0);
  const enWords = P.en.reduce((n, l) => n + (l.text.trim() ? l.text.trim().split(/\s+/).length : 0), 0);
  const lines = P.en.map((l, i) => {
    const w = wordInfo(i);
    return `
    <div class="line" data-id="${l.id}">
      <div class="line-head">
        <span class="num">${i + 1}</span>
        <input type="number" step="0.1" min="0" data-en="${i}" data-field="start" value="${l.start}" title="开始(秒)">
        <span class="muted">→</span>
        <input type="number" step="0.1" min="0" data-en="${i}" data-field="end" value="${l.end}" title="结束(秒)">
        ${l.change ? `<span class="badge ${l.change}">${CHANGE_LABEL[l.change] || l.change}</span>` : ""}
        <button class="btn small" data-seek="${l.start}" data-end="${l.end}">▶ 原片</button>
        ${l.tts ? `<button class="btn small" data-play="${l.tts.file}">🔊 配音</button>` : ""}
        <span class="spacer"></span>
        <button class="icon-btn danger" data-endel="${i}" title="删除这句">✕</button>
      </div>
      ${l.zh ? `<div class="zh-src">${esc(l.zh)}</div>` : ""}
      <textarea rows="2" data-en="${i}" data-field="text">${esc(l.text)}</textarea>
      <div class="line-foot">
        <span class="words ${w.cls}" data-words="${i}">${w.words} / ${w.max} 词</span>
        ${l.note_zh ? `<span class="note">💡 ${esc(l.note_zh)}</span>` : ""}
        <span class="spacer"></span>
        <button class="btn small" data-act="rewriteToggle" data-i="${i}" ${st.has_api_key ? "" : "disabled title='需要 API key'"}>✨ AI 改写</button>
        <button class="btn small" data-act="insertAfter" data-i="${i}">＋ 插入一句</button>
      </div>
      ${S.rewriteOpen === l.id ? `
      <div class="chips">${REWRITE_CHIPS.map((c) => `<button class="chip" data-rw="${i}" data-ins="${esc(c)}">${esc(c)}</button>`).join("")}</div>
      <div class="rewrite-box">
        <input type="text" id="rwInput" placeholder="或者自己写要求，例如：不要提温度，强调显瘦">
        <button class="btn dark" data-rw="${i}">改写</button>
      </div>` : ""}
    </div>`;
  }).join("");

  return gen + `
  <div class="card">
    <h3>英文脚本 <span class="muted" style="font-weight:400;font-size:13px">· 中文 ${P.zh.length} 句 / ${zhTotal} 字 → 英文 ${P.en.length} 句 / ${enWords} 词</span></h3>
    <p class="hint">词数是按配音语速估算的上限，红色说明太长、配音会被明显加速，建议删短。改完自动保存。</p>
    ${lines}
    <div class="row"><a class="btn" href="/api/projects/${P.id}/srt/en">下载英文 SRT（可导入剪映）</a><span class="spacer"></span>
      <button class="btn primary" data-goto="tts">下一步：配音 →</button></div>
  </div>
  ${P.titles.length ? `<div class="card"><h3>标题 / 文案建议</h3><ul class="titles">${P.titles.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}`;
}

function tabTts() {
  const P = S.P, st = S.status, t = P.settings.tts, a = P.settings.audio;
  if (!P.en.length) return `<div class="card"><h3>先完成第 3 步</h3><button class="btn primary" data-goto="en">去英文脚本</button></div>`;
  const backends = [["edge", "Edge 神经语音（免费）"], ...(st.has_say ? [["say", "macOS 自带（离线）"]] : []),
    ...(st.has_elevenlabs ? [["elevenlabs", "ElevenLabs（付费）"]] : []), ["dummy", "测试提示音"]];
  const voiceField = t.backend === "edge"
    ? `<select data-set="tts.voice">${st.voices.map((v) => `<option value="${v.id}" ${v.id === t.voice ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select>`
    : `<input type="text" data-set="tts.voice" value="${esc(t.voice)}" placeholder="${t.backend === "say" ? "Samantha" : "voice id"}">`;
  const n = { ok: 0, stale: 0, none: 0, over: 0 };
  P.en.forEach((l) => { n[l.tts_state || "none"]++; if (l.tts && l.tts.overflow > 0.05 && l.tts_state === "ok") n.over++; });

  const rows = P.en.map((l, i) => {
    const s = l.tts_state || "none";
    const over = l.tts && l.tts.overflow > 0.05;
    let meta = "未生成";
    if (l.tts) {
      meta = `${l.tts.audio}s / 可用 ${l.tts.slot}s`;
      if (l.tts.rate_pct > (t.base_rate || 0)) meta += ` · 语速 +${l.tts.rate_pct}%`;
      if (l.tts.atempo > 1) meta += ` · 加速 ×${l.tts.atempo}`;
      if (over) meta += ` · 超出 ${l.tts.overflow}s，建议删短`;
    }
    const badge = s === "stale" ? `<span class="status stale">文字/声音改过，需重新生成</span>`
      : s === "ok" ? (over ? `<span class="status over">太长</span>` : `<span class="status ok">已生成</span>`) : `<span class="status none">未生成</span>`;
    return `
    <div class="tts-row">
      <b>${i + 1}</b>
      <div><div>${esc(l.text)}</div><div class="tts-meta">${badge} ${esc(meta)}</div></div>
      <div class="row">
        ${l.tts ? `<button class="btn small" data-play="${l.tts.file}">▶ 试听</button>` : ""}
        <button class="btn small" data-act="ttsOne" data-id="${l.id}">↻ 生成</button>
      </div>
    </div>`;
  }).join("");

  return `
  <div class="card">
    <h3>声音</h3>
    <div class="grid2">
      <label class="field">配音引擎<select data-set="tts.backend">${backends.map(([v, lab]) => `<option value="${v}" ${v === t.backend ? "selected" : ""}>${lab}</option>`).join("")}</select></label>
      <label class="field">声音${voiceField}</label>
      ${slider("tts.base_rate", "基础语速 %", -10, 25, 1, t.base_rate, "%")}
      ${slider("tts.max_rate", "装不下时最多提速 %", 10, 50, 1, t.max_rate, "%")}
    </div>
  </div>
  <div class="card">
    <h3>逐句配音 <span class="muted" style="font-weight:400;font-size:13px">· 已生成 ${n.ok} · 需重新生成 ${n.stale} · 未生成 ${n.none}${n.over ? ` · <span style="color:var(--bad)">太长 ${n.over}</span>` : ""}</span></h3>
    <p class="hint">「生成配音」只处理新的和改过的句子。勾选左下「用英文配音预览」，播放视频就能听到整体效果。</p>
    <div class="row" style="margin-bottom:8px">
      <button class="btn primary" data-act="ttsAll">🎧 生成配音</button>
      <button class="btn" data-act="ttsForce">全部重新生成</button>
    </div>
    ${rows}
  </div>
  <div class="card">
    <h3>声音混合</h3>
    <div class="grid2">
      ${slider("audio.original_volume", "原视频声音（中文人声）", 0, 0.5, 0.01, a.original_volume)}
      ${slider("audio.bgm_volume", "背景音乐音量", 0, 0.5, 0.01, a.bgm_volume)}
    </div>
    <div class="row" style="margin-top:10px">
      ${P.bgm ? `<span>🎵 ${esc(P.bgm)}</span><button class="btn small" data-act="delBgm">移除</button>`
        : `<label class="btn">🎵 上传背景音乐<input type="file" accept="audio/*" data-act="bgm" hidden></label><span class="muted" style="font-size:12px">原声静音时建议加一点，否则会很空</span>`}
    </div>
  </div>
  <div class="row"><span class="spacer"></span><button class="btn primary" data-goto="out">下一步：成片 →</button></div>`;
}

function tabOut() {
  const P = S.P;
  const ready = P.en.length && P.en.every((l) => l.tts_state === "ok");
  const checks = [
    ["中文字幕", P.zh.length > 0], ["英文脚本", P.en.length > 0], ["全部配音已生成", ready],
  ];
  return `
  <div class="card">
    <h3>合成成片</h3>
    <ul style="padding-left:18px;margin:6px 0 12px">${checks.map(([k, ok]) => `<li>${ok ? "✅" : "⬜"} ${k}</li>`).join("")}</ul>
    <button class="btn primary" data-act="render" ${ready ? "" : "disabled"}>🎬 合成视频</button>
    ${ready ? "" : `<button class="btn" data-goto="tts">去配音</button>`}
  </div>
  ${P.output ? `
  <div class="card">
    <h3>成片</h3>
    <video class="out-video" controls src="/media/${P.id}/${encodeURIComponent(P.output)}?t=${P.output_at || 0}"></video>
    <div class="row" style="margin-top:10px">
      <a class="btn dark" href="/media/${P.id}/${encodeURIComponent(P.output)}" download>⬇ 下载 MP4</a>
      <a class="btn" href="/api/projects/${P.id}/srt/en">英文 SRT</a>
    </div>
  </div>` : ""}
  ${P.titles.length ? `<div class="card"><h3>标题 / 文案建议</h3><ul class="titles">${P.titles.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
    <p class="hint">发布时记得在 TikTok 打开 “AI-generated content” 标注（AI 配音）。</p></div>` : ""}`;
}

// ------------------------------------------------------------------ events
function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  keys.slice(0, -1).forEach((k) => (o = o[k] ||= {}));
  o[keys.at(-1)] = value;
}

document.addEventListener("input", (e) => {
  const el = e.target;
  if (!S.P) return;
  if (el.dataset.set) {
    let v = el.type === "checkbox" ? el.checked : el.type === "range" || el.type === "number" ? +el.value : el.value;
    if (el.dataset.set === "context") { S.P.settings.context = v; return saveSettings(); }
    setPath(S.P.settings, el.dataset.set, v);
    const label = document.querySelector(`[data-val="${el.dataset.set}"]`);
    if (label) label.textContent = v + (el.dataset.set.includes("rate") ? "%" : "");
    updateOverlay();
    saveSettings();
    if (el.tagName === "SELECT" || el.type === "checkbox") { flushSave().then(() => el.dataset.set === "tts.backend" && renderTab()); }
  } else if (el.dataset.zh !== undefined) {
    S.P.zh[+el.dataset.zh].text = el.value;
    save({ zh: S.P.zh });
  } else if (el.dataset.en !== undefined) {
    const i = +el.dataset.en, l = S.P.en[i], f = el.dataset.field;
    l[f] = f === "text" ? el.value : +el.value;
    if (f === "text") {
      const w = wordInfo(i), span = document.querySelector(`[data-words="${i}"]`);
      span.textContent = `${w.words} / ${w.max} 词`;
      span.className = `words ${w.cls}`;
    }
    saveEn();
    updateOverlay();
  }
});

document.addEventListener("change", async (e) => {
  const el = e.target;
  if (el.id === "videoInput") return upload(el.files[0]);
  if (el.id === "dubPreview") { video.muted = el.checked; stopClips(); return; }
  if (el.id === "showFinal") {
    if (el.checked && !S.P.output) { el.checked = false; return toast("还没有成片，先在第 5 步合成"); }
    return setVideoSource();
  }
  if (el.dataset.act === "srt" && el.files[0]) {
    const fd = new FormData(); fd.append("file", el.files[0]);
    try { S.P = await api("POST", `/api/projects/${S.P.id}/zh-srt`, fd, true); renderAll(); loadProjects(); toast(`导入了 ${S.P.zh.length} 句`); }
    catch (err) { toast(err.message, true); }
  }
  if (el.dataset.act === "bgm" && el.files[0]) {
    const fd = new FormData(); fd.append("file", el.files[0]);
    S.P = await api("POST", `/api/projects/${S.P.id}/bgm`, fd, true); renderTab();
  }
  if (el.dataset.en !== undefined && el.dataset.field !== "text") {
    S.P.en.sort((a, b) => a.start - b.start); saveEn(); renderTab();
  }
});

document.addEventListener("click", async (e) => {
  const el = e.target.closest("button, [data-seek], li[data-id], [data-goto]");
  if (!el) return;
  const P = S.P;
  if (el.matches("li[data-id]")) return openProject(el.dataset.id);
  if (el.dataset.goto) { await flushSave(); S.tab = el.dataset.goto; S.rewriteOpen = null; renderAll(); return window.scrollTo({ top: 0 }); }
  if (el.dataset.tab) { await flushSave(); S.tab = el.dataset.tab; S.rewriteOpen = null; return renderAll(); }
  if (el.dataset.seek !== undefined) return playRange(+el.dataset.seek, +(el.dataset.end || 1e9));
  if (el.dataset.play) { stopClips(); const a = new Audio(`/media/${P.id}/${el.dataset.play}`); S.audio._one?.pause(); S.audio._one = a; return a.play(); }
  if (el.dataset.zhdel !== undefined) { P.zh.splice(+el.dataset.zhdel, 1); save({ zh: P.zh }); return renderTab(); }
  if (el.dataset.endel !== undefined) {
    if (!confirm("删除这句英文？")) return;
    P.en.splice(+el.dataset.endel, 1); saveEn(); return renderTab();
  }
  if (el.dataset.rw !== undefined) {
    const i = +el.dataset.rw, l = P.en[i];
    const instruction = el.dataset.ins || $("#rwInput")?.value || "";
    el.disabled = true; el.textContent = "改写中...";
    try {
      await flushSave();
      const r = await api("POST", `/api/projects/${P.id}/lines/${l.id}/rewrite`, { instruction });
      l.prev = l.text; l.text = r.text; l.change = l.change || "manual";
      saveEn(); S.rewriteOpen = null; renderTab();
      toast(`已改写。原句：${l.prev}`);
    } catch (err) { toast(err.message, true); el.disabled = false; el.textContent = "改写"; }
    return;
  }
  switch (el.dataset.act) {
    case "whisper": return runJob(`/api/projects/${P.id}/extract`, {}, "识别完成，检查一下错字");
    case "translate": await flushSave(); return runJob(`/api/projects/${P.id}/translate`, {}, "英文脚本已生成，逐句检查一下");
    case "manualToggle": $("#manualBox").hidden = !$("#manualBox").hidden; return;
    case "copyPrompt": {
      await flushSave();
      const text = await api("GET", `/api/projects/${P.id}/prompt`);
      try { await navigator.clipboard.writeText(text); toast("提示词已复制，去 claude.ai 粘贴发送"); }
      catch { $("#replyText").value = text; toast("浏览器不让自动复制：提示词已放进下面的框，全选复制后再清空"); }
      return;
    }
    case "importReply":
      try { S.P = await api("POST", `/api/projects/${P.id}/reply`, { text: $("#replyText").value }); renderAll(); loadProjects(); toast(`导入了 ${S.P.en.length} 句英文`); }
      catch (err) { toast(err.message, true); }
      return;
    case "rewriteToggle": { const l = P.en[+el.dataset.i]; S.rewriteOpen = S.rewriteOpen === l.id ? null : l.id; return renderTab(); }
    case "insertAfter": {
      const i = +el.dataset.i, cur = P.en[i], next = P.en[i + 1];
      const start = +(cur.end + 0.1).toFixed(2), end = +Math.min(next ? next.start - 0.1 : P.duration, start + 2.5).toFixed(2);
      const id = Math.max(...P.en.map((l) => l.id)) + 1;
      P.en.splice(i + 1, 0, { id, start, end: Math.max(end, start + 0.5), text: "", change: "manual", note_zh: "", source_ids: [], zh: "" });
      saveEn(); renderTab(); return;
    }
    case "ttsAll": await flushSave(); return runJob(`/api/projects/${P.id}/tts`, {}, "配音生成完成");
    case "ttsForce": await flushSave(); return runJob(`/api/projects/${P.id}/tts`, { force: true }, "全部重新生成完成");
    case "ttsOne": await flushSave(); return runJob(`/api/projects/${P.id}/tts`, { line_id: +el.dataset.id }, "这句已重新生成");
    case "delBgm": S.P = await api("DELETE", `/api/projects/${P.id}/bgm`); return renderTab();
    case "render": await flushSave(); if (await runJob(`/api/projects/${P.id}/render`, {}, "成片已生成 🎉")) { $("#showFinal").checked = true; setVideoSource(); } return;
  }
});

$("#projName").addEventListener("input", (e) => save({ name: e.target.value }));
$("#delProject").addEventListener("click", async () => {
  if (!confirm(`删除项目「${S.P.name}」和它的所有文件？`)) return;
  await api("DELETE", `/api/projects/${S.P.id}`);
  S.P = null; location.hash = "";
  $("#workspace").hidden = true; $("#empty").hidden = false;
  loadProjects();
});

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
window.addEventListener("beforeunload", flushSave);

// ------------------------------------------------------------------ boot
(async function boot() {
  S.status = await api("GET", "/api/status");
  $("#brand").textContent = S.status.brand;
  $("#pills").innerHTML = `
    <span class="pill ${S.status.has_api_key ? "ok" : "warn"}">${S.status.has_api_key ? "Claude API 已连接" : "Claude API 未配置 · 可用手动模式"}</span>`;
  await loadProjects();
  const id = location.hash.slice(1);
  if (id && S.projects.some((p) => p.id === id)) openProject(id);
})();
