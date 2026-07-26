"use strict";

/* PTA 助手前端：与本地 Flask 后端（pta/webapp.py）通信，API 契约保持不变。 */

const $ = (id) => document.getElementById(id);
const API_TOKEN = document.querySelector('meta[name="pta-api-token"]')?.content || "";
const state = {
  scope: "active",
  psid: "",
  examId: "",
  ended: false,
  compilers: [],
  defaultCompiler: "GCC",
  problems: [],
  current: null,
  filter: "",
  statusFilter: "all",
  visible: [],
  restoredSet: "",
  busy: 0,
  problemSetsCache: {},
  batchAbort: null,
  batchStats: { done: 0, ok: 0, bad: 0, total: 0 },
};

let overviewSeq = 0;
const PROBLEM_SET_CACHE_MS = 60000;

const TYPE_LABEL = { CODE_COMPLETION: "函数题", PROGRAMMING: "编程题", MULTIPLE_FILE: "多文件" };
const TYPE_ORDER = ["CODE_COMPLETION", "PROGRAMMING", "MULTIPLE_FILE"];
const RING_C = 2 * Math.PI * 16.5;

/* ============ 通用 ============ */
function setBusy(on) {
  state.busy += on ? 1 : -1;
  if (state.busy < 0) state.busy = 0;
  const p = $("progress");
  if (p) p.classList.toggle("hidden", state.busy === 0);
}

async function api(path, opts = {}) {
  setBusy(true);
  try {
    const headers = {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${API_TOKEN}`,
      ...(opts.headers || {}),
    };
    const res = await fetch(path, { ...opts, headers });
    let data = {};
    try { data = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) {
      const err = new Error(data.error || `HTTP ${res.status}`);
      err.auth = data.auth; err.status = res.status;
      throw err;
    }
    return data;
  } finally {
    setBusy(false);
  }
}

let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2800);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ============ 主题 ============ */
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t === "light" ? "light" : "dark");
}
function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  applyTheme(next);
  localStorage.setItem("pta_theme", next);
}

/* ============ 草稿（localStorage，按题集+题目）============ */
function draftKey() {
  return state.current && state.psid ? `pta_draft_${state.psid}_${state.current.pspid}` : null;
}
function saveDraft(text) {
  const k = draftKey();
  if (!k) return;
  try {
    if (text && text.trim()) localStorage.setItem(k, text);
    else localStorage.removeItem(k);
  } catch (e) { /* 存储不可用/已满时静默 */ }
}
function loadDraft() {
  const k = draftKey();
  if (!k) return "";
  try { return localStorage.getItem(k) || ""; } catch (e) { return ""; }
}

/* ============ 代码字号 ============ */
const CODE_FS_STEPS = [11, 12, 13, 14, 15, 16, 18, 20];
function applyCodeFs(px, persist = true) {
  const wrap = document.querySelector(".code-wrap");
  if (wrap) wrap.style.setProperty("--code-fs", px + "px");
  if (persist) localStorage.setItem("pta_code_fs", String(px));
}
function bumpCodeFs(dir) {
  const cur = parseInt(localStorage.getItem("pta_code_fs") || "13", 10) || 13;
  let i = CODE_FS_STEPS.indexOf(cur);
  if (i < 0) i = CODE_FS_STEPS.reduce((best, v, idx) => (Math.abs(v - cur) < Math.abs(CODE_FS_STEPS[best] - cur) ? idx : best), 0);
  const next = CODE_FS_STEPS[Math.max(0, Math.min(CODE_FS_STEPS.length - 1, i + dir))];
  applyCodeFs(next);
  toast("代码字号 " + next + "px");
}

/* ============ 编辑器全屏 ============ */
function toggleFullscreen(force) {
  const panel = $("editor-panel");
  const on = force !== undefined ? force : !panel.classList.contains("fullscreen");
  panel.classList.toggle("fullscreen", on);
}

/* ============ detail 区重置（切换题集时）============ */
function resetDetail() {
  state.current = null;
  $("statement").innerHTML = '<div class="pane-empty tall"><svg class="ico big"><use href="#i-book"/></svg><p>从左侧选择一道题开始</p></div>';
  $("result").innerHTML = "";
  setLastSub(null);
  $("editor-panel").classList.add("hidden");
  $("detail").classList.remove("has-editor");
  toggleFullscreen(false);
}

/* ============ 轻量 Markdown（自包含，离线可用）============ */
function renderInline(s) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0001${codes.length - 1}\u0001`; });
  s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g, '<img alt="$1" src="$2" />');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_([^_\n]+)_(?=[^\w]|$)/g, "$1<em>$2</em>");
  s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => `<code>${codes[+i]}</code>`);
  return s;
}

function renderMarkdown(src) {
  if (!src) return "";
  const fences = [];
  src = String(src).replace(/\r\n/g, "\n").replace(/```[ \t]*([\w-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    fences.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000F${fences.length - 1}\u0000`;
  });

  const lines = esc(src).split("\n");
  const out = [];
  let i = 0;
  const isFence = (l) => /^\u0000F\d+\u0000$/.test(l.trim());

  while (i < lines.length) {
    const t = lines[i].trim();
    if (!t) { i++; continue; }
    if (isFence(t)) { out.push(t); i++; continue; }

    let m = t.match(/^(#{1,6})\s+(.*)$/);
    if (m) { const lv = Math.min(m[1].length, 4); out.push(`<h${lv}>${renderInline(m[2])}</h${lv}>`); i++; continue; }

    if (/^([-*_])\1{2,}$/.test(t.replace(/\s+/g, ""))) { out.push("<hr/>"); i++; continue; }

    if (t.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const splitRow = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
      const headers = splitRow(t);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().includes("|")) { rows.push(splitRow(lines[i])); i++; }
      let html = "<table><thead><tr>" + headers.map((h) => `<th>${renderInline(h)}</th>`).join("") + "</tr></thead><tbody>";
      for (const r of rows) html += "<tr>" + headers.map((_, c) => `<td>${renderInline(r[c] || "")}</td>`).join("") + "</tr>";
      out.push(html + "</tbody></table>");
      continue;
    }

    if (/^&gt;\s?/.test(t)) {
      const buf = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i].trim())) { buf.push(lines[i].trim().replace(/^&gt;\s?/, "")); i++; }
      out.push(`<blockquote>${renderInline(buf.join("<br>"))}</blockquote>`);
      continue;
    }

    if (/^([-*+]\s+|\d+[.)]\s+)/.test(t)) {
      const ordered = /^\d+[.)]\s+/.test(t);
      const items = [];
      while (i < lines.length) {
        const mm = lines[i].trim().match(/^(?:[-*+]|\d+[.)])\s+(.*)$/);
        if (!mm) break;
        items.push(`<li>${renderInline(mm[1])}</li>`); i++;
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !isFence(lines[i].trim()) &&
           !/^(#{1,6}\s|&gt;\s?|[-*+]\s+|\d+[.)]\s+)/.test(lines[i].trim()) &&
           !/^([-*_])\1{2,}$/.test(lines[i].trim().replace(/\s+/g, ""))) {
      para.push(lines[i].trim()); i++;
    }
    if (para.length) out.push(`<p>${renderInline(para.join("<br>"))}</p>`);
  }

  return out.join("\n").replace(/\u0000F(\d+)\u0000/g, (_, n) => fences[+n]);
}

/* ============ 登录 / 状态 ============ */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    applyStatus(s);
    return s;
  } catch (e) {
    showView(false);
    return null;
  }
}

function applyStatus(s) {
  if (s.loggedIn) {
    const name = (s.user && s.user.nickname) || "已登录";
    $("user-name").textContent = name;
    $("user-avatar").textContent = (name[0] || "P").toUpperCase();
    $("user-area").classList.remove("hidden");
    showView(true);
    if (s.problemSetId && !state.psid) $("psid-input").value = s.problemSetId;
    if (s.defaultCompiler) state.defaultCompiler = s.defaultCompiler;
  } else {
    $("user-area").classList.add("hidden");
    showView(false);
  }
}

function showView(loggedIn) {
  $("login-view").classList.toggle("hidden", loggedIn);
  $("main-view").classList.toggle("hidden", !loggedIn);
}

function setLoginMsg(text, kind) {
  const el = $("login-msg");
  el.textContent = text;
  el.className = "msg " + (kind || "");
}

async function browserLogin() {
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  const btn = $("btn-browser-login");
  btn.disabled = true;
  setLoginMsg("正在打开浏览器，请在弹出窗口完成登录（拖动滑块并点登录）…", "info");
  try {
    const s = await api("/api/login/browser", { method: "POST", body: JSON.stringify({ email, password }) });
    if (s.loggedIn) {
      state.problemSetsCache = {};
      setLoginMsg("登录成功！", "ok");
      applyStatus(s);
      loadProblemSets();
    }
    else setLoginMsg("已保存 cookie，但校验未通过，请重试。", "err");
  } catch (e) {
    setLoginMsg("登录失败：" + e.message, "err");
  } finally { btn.disabled = false; }
}

async function manualLogin() {
  const cookie = $("cookie-input").value.trim();
  if (!cookie) return setLoginMsg("请粘贴 cookie。", "err");
  setLoginMsg("正在校验…", "info");
  try {
    const s = await api("/api/login/manual", { method: "POST", body: JSON.stringify({ cookie }) });
    if (s.loggedIn) {
      state.problemSetsCache = {};
      setLoginMsg("登录成功！", "ok");
      applyStatus(s);
      loadProblemSets();
    }
    else setLoginMsg("cookie 无效或已过期。", "err");
  } catch (e) {
    setLoginMsg("登录失败：" + e.message, "err");
  }
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  state.psid = ""; state.problems = []; state.current = null;
  state.problemSetsCache = {};
  $("current-set-name").textContent = "选择题集";
  $("exam-status").classList.add("hidden");
  await refreshStatus();
}

/* ============ 题集切换器 ============ */
function openMenu() { document.querySelector(".set-switch").classList.add("open"); $("set-menu").classList.remove("hidden"); $("set-switcher").setAttribute("aria-expanded", "true"); }
function closeMenu() { document.querySelector(".set-switch").classList.remove("open"); $("set-menu").classList.add("hidden"); $("set-switcher").setAttribute("aria-expanded", "false"); }
function toggleMenu() { $("set-menu").classList.contains("hidden") ? (openMenu(), loadProblemSets()) : closeMenu(); }

function renderProblemSets(sets) {
  const ul = $("set-list");
  if (!sets.length) { ul.innerHTML = '<li class="menu-empty">该范围下没有题集</li>'; return; }
  ul.innerHTML = "";
  for (const ps of sets) {
    const li = document.createElement("li");
    li.className = "set-item" + (ps.id === state.psid ? " active" : "");
    li.innerHTML = `<span class="si-name">${esc(ps.name)}</span><span class="si-id">${esc(ps.id)}</span>`;
    li.onclick = () => openSet(ps.id, ps.name);
    ul.appendChild(li);
  }
}

async function loadProblemSets(force = false) {
  const ul = $("set-list");
  const key = state.scope;
  const cached = state.problemSetsCache[key];
  if (!force && cached && Date.now() - cached.time < PROBLEM_SET_CACHE_MS) {
    renderProblemSets(cached.sets);
    return;
  }
  ul.innerHTML = '<li class="menu-empty">加载中…</li>';
  try {
    const qs = new URLSearchParams({ scope: state.scope });
    if (force) qs.set("refresh", "1");
    const data = await api("/api/problem-sets?" + qs.toString());
    const sets = data.problemSets || [];
    state.problemSetsCache[key] = { time: Date.now(), sets };
    renderProblemSets(sets);
  } catch (e) {
    ul.innerHTML = `<li class="menu-empty">加载失败：${esc(e.message)}</li>`;
  }
}

async function openSet(id, name) {
  const psid = (id || $("psid-input").value || "").trim();
  if (!psid) return toast("请选择或粘贴 problemSetId");
  state.psid = psid;
  $("psid-input").value = psid;
  if (name) $("current-set-name").textContent = name;
  closeMenu();
  closeSidebar();
  resetDetail();
  api("/api/config", {
    method: "POST",
    body: JSON.stringify({ problemSetId: psid }),
  }).catch(() => {});
  await loadOverview();
}

async function loadOverview() {
  const seq = ++overviewSeq;
  $("problem-count").textContent = "加载中…";
  if (window.EventSource) {
    try {
      await loadOverviewStream(seq);
      if (seq === overviewSeq) restoreLastProblem();
      return;
    } catch (e) {
      if (seq !== overviewSeq) return;
      // 流式加载不可用时回退到普通 JSON 接口。
    }
  }
  try {
    const ov = await api("/api/overview?psid=" + encodeURIComponent(state.psid));
    if (seq !== overviewSeq) return;
    applyOverview(ov);
    restoreLastProblem();
  } catch (e) {
    if (seq !== overviewSeq) return;
    $("problem-count").textContent = "加载失败";
    toast("题集加载失败：" + e.message);
  }
}

/* 每个题集每会话自动恢复一次上次打开的题目 */
function restoreLastProblem() {
  if (state.restoredSet === state.psid || state.current) return;
  state.restoredSet = state.psid;
  let pid = "";
  try { pid = localStorage.getItem("pta_last_" + state.psid) || ""; } catch (e) { return; }
  if (!pid) return;
  const p = state.problems.find((x) => x.pspid === pid);
  if (p) openProblem(p);
}

function applyOverview(ov) {
  if (ov.examId !== undefined) state.examId = ov.examId;
  if (ov.ended !== undefined) state.ended = ov.ended;
  if (ov.compilers !== undefined) state.compilers = ov.compilers || [];
  if (Array.isArray(ov.problems)) state.problems = ov.problems;
  if (ov.problemSetName) $("current-set-name").textContent = ov.problemSetName;
  const pill = $("exam-status");
  if (ov.status !== undefined || ov.ended !== undefined) {
    const ended = ov.ended !== undefined ? ov.ended : state.ended;
    pill.textContent = statusLabel(ov.status) + (ended ? " · 已结束" : "");
    pill.classList.toggle("ended", !!ended);
    pill.classList.remove("hidden");
  }
  renderProblemList();
}

function loadOverviewStream(seq) {
  return new Promise((resolve, reject) => {
    const psid = state.psid;
    const qs = new URLSearchParams({ psid, _token: API_TOKEN });
    const es = new EventSource("/api/overview-stream?" + qs.toString());
    let settled = false;
    let gotData = false;
    const timer = setTimeout(() => finish(false, new Error("流式加载超时")), 45000);

    function finish(ok, err) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      es.close();
      setBusy(false);
      ok ? resolve() : reject(err || new Error("流式加载失败"));
    }

    setBusy(true);
    es.onmessage = (ev) => {
      if (seq !== overviewSeq || psid !== state.psid) return finish(true);
      let msg = {};
      try { msg = JSON.parse(ev.data || "{}"); } catch (e) { return; }
      if (msg.phase === "error") return finish(false, new Error(msg.error || "题集加载失败"));
      if (msg.phase === "catalog") $("problem-count").textContent = "加载状态…";
      if (msg.phase === "exam" || msg.phase === "catalog" || msg.phase === "status" || msg.phase === "done") {
        gotData = true;
        applyOverview(msg);
      }
      if (msg.phase === "done") finish(true);
    };
    es.onerror = () => {
      finish(false, new Error(gotData ? "流式加载中断" : "流式加载失败"));
    };
  });
}

function statusLabel(s) {
  return ({ PENDING: "未开始", READY: "可答题", PROCESSING: "进行中", END: "已结束" })[s] || s || "";
}

/* ============ 题目列表（按题型分组）============ */
function statusDot(s) {
  if (s === "PROBLEM_ACCEPTED") return "dot ok";
  if (s && s !== "PROBLEM_NO_ANSWER") return "dot sub";
  return "dot";
}

function setProgress(pct) {
  const f = $("ring-fill");
  if (f) { f.style.strokeDasharray = RING_C.toFixed(2); f.style.strokeDashoffset = (RING_C * (1 - pct / 100)).toFixed(2); }
  const t = $("ring-text");
  if (t) t.textContent = pct + "%";
}

function makeItem(p) {
  const li = document.createElement("li");
  const done = p.status === "PROBLEM_ACCEPTED";
  li.className = "problem-item" + (done ? " done" : "") + (state.current && state.current.pspid === p.pspid ? " active" : "");
  li.dataset.pspid = p.pspid;
  li.tabIndex = 0;
  li.innerHTML =
    `<span class="${statusDot(p.status)}"></span>` +
    (p.label ? `<span class="p-label">${esc(p.label)}</span>` : "") +
    `<span class="p-title">${esc(p.title)}</span>`;
  li.onclick = () => openProblem(p);
  li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openProblem(p); } };
  return li;
}

function renderProblemList() {
  const ul = $("problem-list");
  const empty = $("list-empty");
  const scrollTop = ul.scrollTop;
  ul.innerHTML = "";
  state.visible = [];
  const total = state.problems.length;

  if (!total) {
    empty.classList.remove("hidden");
    $("problem-count").textContent = "未选择题集";
    setProgress(0);
    return;
  }
  empty.classList.add("hidden");

  const kw = state.filter.trim().toLowerCase();
  const sf = state.statusFilter;
  const list = state.problems.filter((p) => {
    if (sf === "done" && p.status !== "PROBLEM_ACCEPTED") return false;
    if (sf === "todo" && p.status === "PROBLEM_ACCEPTED") return false;
    return !kw || (p.title || "").toLowerCase().includes(kw) || (p.label || "").toLowerCase().includes(kw) || p.pspid.includes(kw);
  });
  const acc = state.problems.filter((p) => p.status === "PROBLEM_ACCEPTED").length;
  const pct = total ? Math.round((acc / total) * 100) : 0;
  $("problem-count").textContent = `${total} 题 · 已过 ${acc}` + (kw || sf !== "all" ? ` · 匹配 ${list.length}` : "");
  setProgress(pct);

  if (!list.length) { ul.innerHTML = '<li class="menu-empty">没有匹配的题目</li>'; return; }

  const groups = {};
  for (const p of list) (groups[p.ptype || p.type || "OTHER"] = groups[p.ptype || p.type || "OTHER"] || []).push(p);
  const types = Object.keys(groups).sort((a, b) => {
    const ia = TYPE_ORDER.indexOf(a), ib = TYPE_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  for (const ty of types) {
    if (types.length > 1) {
      const head = document.createElement("li");
      head.className = "group-head";
      head.innerHTML = `${esc(TYPE_LABEL[ty] || ty)} <span class="gc">${groups[ty].length}</span>`;
      ul.appendChild(head);
    }
    for (const p of groups[ty]) {
      state.visible.push(p);
      ul.appendChild(makeItem(p));
    }
  }
  ul.scrollTop = scrollTop;
}

/* ↑↓ 键在可见题目间移动并打开 */
function moveSelection(delta) {
  const list = state.visible;
  if (!list.length) return;
  let i = list.findIndex((p) => state.current && p.pspid === state.current.pspid);
  i = i < 0 ? (delta > 0 ? 0 : list.length - 1) : (i + delta + list.length) % list.length;
  openProblem(list[i]);
  const el = document.querySelector(`.problem-item[data-pspid="${list[i].pspid}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

/* ============ 单题 ============ */
function typeTag(t) {
  if (t === "PROGRAMMING") return '<span class="tag prog">编程题</span>';
  if (t === "CODE_COMPLETION") return '<span class="tag code">函数题</span>';
  return `<span class="tag">${esc(t || "?")}</span>`;
}

async function openProblem(p) {
  document.querySelectorAll(".problem-item").forEach((el) => el.classList.toggle("active", el.dataset.pspid === p.pspid));
  state.current = p;
  closeSidebar();
  try { localStorage.setItem("pta_last_" + state.psid, p.pspid); } catch (e) { /* 静默 */ }
  setLastSub(null);
  $("statement").innerHTML = '<div class="pane-empty tall">加载中…</div>';
  $("editor-panel").classList.add("hidden");
  $("detail").classList.remove("has-editor");
  try {
    const d = await api(`/api/problem?psid=${encodeURIComponent(state.psid)}&pspid=${p.pspid}`);
    if (!state.current || state.current.pspid !== p.pspid) return; // 已切到别的题
    state.current = { ...p, ...d, index: d.indexInPool };
    renderStatement(d);
    renderEditor(d);
    loadLastSubmission(d);
  } catch (e) {
    $("statement").innerHTML = `<div class="pane-empty tall">加载失败：${esc(e.message)}</div>`;
  }
}

/* 上次提交状态条（复用 /api/result，无需新接口） */
function setLastSub(r, sid) {
  const el = $("last-sub");
  if (!r) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  const st = (r.status || "").toUpperCase();
  const pending = !!r.pending;
  const ok = st === "ACCEPTED";
  const cls = pending ? "ls-wait" : ok ? "ls-ok" : "ls-bad";
  const text = pending ? "判题中…" : (r.statusText || r.status || "未知");
  el.innerHTML = `上次提交：<span class="${cls}">${esc(text)}</span>` +
    (r.score != null ? ` · ${esc(String(r.score))} 分` : "") +
    (sid ? ' · <button class="ls-link" id="ls-view">查看详情</button>' : "");
  el.classList.remove("hidden");
  if (sid) $("ls-view").onclick = () => renderResult(r);
}

function loadLastSubmission(d) {
  if (!d.lastSubmissionId || !state.examId) return;
  const sid = d.lastSubmissionId;
  const pspid = state.current.pspid;
  const qs = new URLSearchParams({ examId: state.examId, submissionId: sid, psid: state.psid, pspid });
  api("/api/result?" + qs.toString())
    .then((r) => {
      if (!state.current || state.current.pspid !== pspid) return;
      setLastSub(r, sid);
      if (r.pending) pollResult(state.examId, sid, (r2) => {
        if (state.current && state.current.pspid === pspid) setLastSub(r2, sid);
      }, 45, { psid: state.psid, pspid });
    })
    .catch(() => { /* 上次提交不可用时静默 */ });
}

function renderStatement(d) {
  const limits = [];
  if (d.score != null) limits.push(`满分 ${d.score}`);
  if (d.timeLimit != null) limits.push(`${d.timeLimit} ms`);
  if (d.memoryLimit != null) limits.push(`${d.memoryLimit} KB`);
  if (d.codeSizeLimit != null) limits.push(`${d.codeSizeLimit} B`);
  if (d.difficulty) limits.push(String(d.difficulty));

  let html = `<h3 class="p-title">${esc(d.title)}</h3>`;
  html += `<div class="meta">${typeTag(d.type)}` + limits.map((l) => `<span>${esc(l)}</span>`).join('<span class="sep">·</span>') + `</div>`;
  const main = (d.content || d.description || "").trim();
  html += `<div class="prose">${main ? renderMarkdown(main) : "(接口未返回题面文本)"}</div>`;
  if (d.content && d.description && d.description.trim() && d.description.trim() !== d.content.trim()) {
    html += `<div class="section-title">补充描述</div><div class="prose">${renderMarkdown(d.description.trim())}</div>`;
  }
  if (d.examples && d.examples.length) {
    html += `<div class="section-title">样例</div>`;
    d.examples.forEach((ex, i) => {
      html += `<div class="example-io">` +
        `<div><div class="io-label">输入 ${i + 1}</div><pre>${esc(ex.input)}</pre></div>` +
        `<div><div class="io-label">输出 ${i + 1}</div><pre>${esc(ex.output)}</pre></div></div>`;
    });
  }
  $("statement").innerHTML = html;
  $("statement").scrollTop = 0;
}

function renderEditor(d) {
  $("editor-panel").classList.remove("hidden");
  $("detail").classList.add("has-editor");
  $("editor-title").textContent = d.title;
  $("result").innerHTML = "";
  const draft = loadDraft();
  setCode(draft);

  $("answer-hint").textContent = draft
    ? "已恢复未保存草稿"
    : d.localAnswerExists ? "本地已有答案，可点「载入」" : "本地暂无答案文件";

  const wrap = $("compiler-select").closest(".compiler-wrap");
  if (d.type === "PROGRAMMING") {
    wrap.classList.remove("hidden");
    const sel = $("compiler-select");
    const list = state.compilers.length ? state.compilers : ["GCC", "GXX", "PYTHON3", "JAVAC"];
    sel.innerHTML = "";
    for (const c of list) {
      const o = document.createElement("option");
      o.value = c; o.textContent = c;
      sel.appendChild(o);
    }
    if (d.compiler && list.includes(d.compiler)) sel.value = d.compiler;
    else if (state.defaultCompiler && list.includes(state.defaultCompiler)) sel.value = state.defaultCompiler;
  } else {
    wrap.classList.add("hidden");
  }
}

/* ============ 代码区 ============ */
function setCode(text) {
  $("code").value = text;
  updateGutter();
  saveDraft(text);
}
function updateGutter() {
  const code = $("code");
  if (!code) return;
  const lines = code.value.split("\n").length || 1;
  const g = $("gutter");
  if (g) {
    let s = "";
    for (let i = 1; i <= lines; i++) s += i + "\n";
    g.textContent = s;
    g.scrollTop = code.scrollTop;
  }
  const stat = $("code-stat");
  if (stat) stat.textContent = `${lines} 行 · ${code.value.length} 字符`;
}

async function loadLocalAnswer() {
  if (!state.current) return;
  try {
    const d = await api("/api/answer?pspid=" + state.current.pspid);
    if (d.exists) { setCode(d.code); toast("已载入本地答案"); }
    else toast("本地没有该题答案文件");
  } catch (e) { toast("载入失败：" + e.message); }
}

function answerExt() {
  if (state.current && state.current.type === "PROGRAMMING") {
    const c = $("compiler-select").value;
    if (c === "GXX" || c === "CLANGXX") return ".cpp";
    if (c === "PYTHON3" || c === "PYPY3" || c === "PYTHON2") return ".py";
    if (c === "JAVAC") return ".java";
    return ".c";
  }
  return ".c";
}

async function saveAnswer() {
  if (!state.current) return;
  const code = $("code").value;
  if (!code.trim()) return toast("代码为空");
  try {
    const r = await api("/api/save-answer", { method: "POST", body: JSON.stringify({ pspid: state.current.pspid, code, ext: answerExt() }) });
    $("answer-hint").textContent = "本地已有答案，可点「载入」";
    toast("已保存到 " + r.path);
  } catch (e) { toast("保存失败：" + e.message); }
}

/* ============ 提交 / 判题 ============ */
async function submitCurrent() {
  if (!state.current) return;
  const code = $("code").value;
  if (!code.trim()) return toast("代码为空");
  if (state.ended) return toast("该考试已结束，无法提交");
  const btn = $("btn-submit");
  btn.disabled = true;
  setResultWaiting("提交中…");
  try {
    const compiler = state.current.type === "PROGRAMMING" ? $("compiler-select").value : null;
    const r = await api("/api/submit", {
      method: "POST",
      body: JSON.stringify({ psid: state.psid, pspid: state.current.pspid, type: state.current.type, index: state.current.index ?? 0, compiler, code }),
    });
    setResultWaiting("已提交，判题中…");
    await pollResult(r.examId, r.submissionId, renderResult, 45, {
      psid: state.psid,
      pspid: state.current.pspid,
      previousProblemStatus: r.previousProblemStatus ?? state.current.status,
    });
    refreshStatusDots();
  } catch (e) {
    renderResultError(e.message);
  } finally { btn.disabled = false; }
}

async function refreshStatusDots() {
  try {
    const ov = await api("/api/overview?psid=" + encodeURIComponent(state.psid));
    state.problems = ov.problems || [];
    renderProblemList();
  } catch (e) { /* 静默 */ }
}

function setResultWaiting(text) {
  $("result").innerHTML = `<div class="result-inner"><div class="verdict wait"><span class="spin"></span>${esc(text)}</div></div>`;
}
function renderResultError(msg) {
  $("result").innerHTML = `<div class="result-inner"><div class="verdict bad"><span class="vico">✗</span>提交失败</div><pre>${esc(msg)}</pre></div>`;
}

async function pollResult(examId, sid, cb, tries = 45, extra = {}) {
  const qs = new URLSearchParams({ examId, submissionId: sid });
  if (extra.psid) qs.set("psid", extra.psid);
  if (extra.pspid) qs.set("pspid", extra.pspid);
  if (extra.previousProblemStatus) qs.set("previousProblemStatus", extra.previousProblemStatus);
  for (let i = 0; i < tries; i++) {
    const r = await api("/api/result?" + qs.toString());
    const st = (r.status || "").toUpperCase();
    const pending = r.pending ?? (r.queued || !st || ["WAITING", "JUDGING", "PENDING", "RUNNING", "COMPILING", "QUEUED", "QUEUEING", "SUBMITTED"].includes(st));
    if (!pending) return cb(r);
    await new Promise((res) => setTimeout(res, 1500));
  }
  cb({ status: "TIMEOUT", statusText: "判题轮询超时" });
}

function renderCases(publicCases) {
  let arr = publicCases;
  if (arr && !Array.isArray(arr) && typeof arr === "object") arr = Object.values(arr);
  if (!Array.isArray(arr) || !arr.length) return "";
  try {
    const pills = arr.map((c, i) => {
      const raw = (c && (c.status || c.result || c.judgeResult || c.verdict)) || "";
      const up = String(raw).toUpperCase();
      const ok = up.includes("ACCEPT");
      const cls = ok ? "ok" : (up ? "bad" : "");
      return `<span class="case-pill ${cls}" title="${esc(raw || "")}">${i + 1}</span>`;
    }).join("");
    return `<div class="cases">${pills}</div>`;
  } catch (e) { return ""; }
}

/* 编译错误/警告按行高亮 */
function renderCause(cause) {
  return esc(cause).split("\n").map((line) => {
    const l = line.toLowerCase();
    if (l.includes("error") || line.includes("错误")) return `<span class="cl-err">${line}</span>`;
    if (l.includes("warning") || line.includes("警告")) return `<span class="cl-warn">${line}</span>`;
    return line;
  }).join("\n");
}

function renderResult(r) {
  const ok = (r.status || "").toUpperCase() === "ACCEPTED";
  let inner = `<div class="verdict ${ok ? "ok" : "bad"}"><span class="vico">${ok ? "✓" : "✗"}</span>${esc(r.statusText || r.status || "未知")}</div>`;
  const chips = [];
  if (r.score != null) chips.push(`得分 ${r.score}`);
  if (r.time != null) chips.push(`用时 ${r.time} ms`);
  if (r.memory != null) chips.push(`内存 ${r.memory} KB`);
  if (r.compiler) chips.push(r.compiler);
  if (chips.length) inner += `<div class="chips">${chips.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div>`;
  inner += renderCases(r.publicCases);
  if (r.cause) inner += `<pre>${renderCause(r.cause)}</pre>`;
  if (r.hints && typeof r.hints === "object" && Object.keys(r.hints).length) inner += `<pre>${esc(JSON.stringify(r.hints, null, 2))}</pre>`;
  inner += `<div class="result-actions"><button class="btn btn-ghost btn-sm" id="btn-resubmit"><svg class="ico"><use href="#i-send"/></svg>重新提交</button></div>`;
  $("result").innerHTML = `<div class="result-inner">${inner}</div>`;
  $("btn-resubmit").onclick = () => { if (!state.ended) submitCurrent(); else toast("该考试已结束，无法提交"); };
}

/* ============ 批量提交 ============ */
function openBatch() {
  $("batch-modal").classList.remove("hidden");
  $("batch-log").innerHTML = "";
  batchResetUI();
}
function closeBatch() {
  if (state.batchAbort) state.batchAbort.abort();
  $("batch-modal").classList.add("hidden");
}

function batchResetUI() {
  state.batchStats = { done: 0, ok: 0, bad: 0, total: 0 };
  $("batch-progress").classList.add("hidden");
  $("bp-bar").style.width = "0%";
  $("bp-text").textContent = "0/0";
  $("btn-batch-cancel").classList.add("hidden");
}

function batchProgress() {
  const s = state.batchStats;
  $("batch-progress").classList.remove("hidden");
  $("bp-bar").style.width = (s.total ? (s.done / s.total) * 100 : 0).toFixed(1) + "%";
  $("bp-text").textContent = `${s.done}/${s.total}`;
}

function batchSummary(log) {
  const s = state.batchStats;
  log.innerHTML += `<b>批量提交完成：通过 <span class="ok">${s.ok}</span> · 失败 <span class="bad">${s.bad}</span> · 共 ${s.done}</b><br>`;
  log.scrollTop = log.scrollHeight;
}

async function runBatch() {
  if (!state.psid) return toast("请先打开一个题集");
  const log = $("batch-log");
  log.innerHTML = "读取 answers/ 目录并提交…<br>";
  batchResetUI();
  $("btn-batch-run").disabled = true;
  $("btn-batch-cancel").classList.remove("hidden");
  const abort = new AbortController();
  state.batchAbort = abort;
  const includeAccepted = $("batch-include-accepted").checked;
  try {
    if (window.ReadableStream) {
      await runBatchStream(log, includeAccepted, abort.signal);
      refreshStatusDots();
      return;
    }
    const data = await api("/api/batch-submit", { method: "POST", body: JSON.stringify({ psid: state.psid, includeAccepted }) });
    const results = data.results || [];
    state.batchStats.total = results.length;
    batchProgress();
    if (!results.length) log.innerHTML += "没有可提交的题（answers/ 为空，或都已通过）。<br>";
    for (const item of results) {
      if (abort.signal.aborted) throw new DOMException("已取消", "AbortError");
      if (item.error) {
        state.batchStats.done++; state.batchStats.bad++; batchProgress();
        log.innerHTML += `<span class="bad">✗ ${esc(item.pspid)} 提交失败：${esc(item.error)}</span><br>`;
        continue;
      }
      log.innerHTML += `<span class="wait">… ${esc(item.pspid)} 判题中…</span><br>`;
      await pollResult(item.examId, item.submissionId, (r) => {
        const ok = (r.status || "").toUpperCase() === "ACCEPTED";
        state.batchStats.done++;
        if (ok) state.batchStats.ok++; else state.batchStats.bad++;
        batchProgress();
        log.innerHTML += `<span class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"} ${esc(item.pspid)} ${esc(r.statusText || r.status)}${r.score != null ? " (" + r.score + ")" : ""}</span><br>`;
        log.scrollTop = log.scrollHeight;
      }, 45, { psid: state.psid, pspid: item.pspid, previousProblemStatus: item.previousProblemStatus });
    }
    batchSummary(log);
    refreshStatusDots();
  } catch (e) {
    if (e && e.name === "AbortError") log.innerHTML += `<span class="wait">■ 已取消（已提交的题仍会判题）。</span><br>`;
    else log.innerHTML += `<span class="bad">错误：${esc(e.message)}</span><br>`;
  } finally {
    state.batchAbort = null;
    $("btn-batch-run").disabled = false;
    $("btn-batch-cancel").classList.add("hidden");
    log.scrollTop = log.scrollHeight;
  }
}

async function runBatchStream(log, includeAccepted, signal) {
  setBusy(true);
  try {
    const res = await fetch("/api/batch-submit-stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${API_TOKEN}`,
      },
      body: JSON.stringify({ psid: state.psid, includeAccepted }),
      signal,
    });
    if (!res.ok || !res.body) {
      let data = {};
      try { data = await res.json(); } catch (e) { /* 非 JSON */ }
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) handleBatchEvent(JSON.parse(line), log);
    }
    buf += decoder.decode();
    if (buf.trim()) handleBatchEvent(JSON.parse(buf), log);
  } finally {
    setBusy(false);
  }
}

function handleBatchEvent(ev, log) {
  const s = state.batchStats;
  if (ev.phase === "prepared") {
    s.total = ev.total || 0;
    batchProgress();
    if (ev.unknown && ev.unknown.length) log.innerHTML += `忽略未知答案：${esc(ev.unknown.join(", "))}<br>`;
    if (ev.skipped && ev.skipped.length) log.innerHTML += `跳过已通过：${esc(ev.skipped.join(", "))}<br>`;
    if (!ev.total) log.innerHTML += "没有可提交的题（answers/ 为空，或都已通过）。<br>";
  } else if (ev.phase === "submitting") {
    log.innerHTML += `<span class="wait">… [${ev.index}/${ev.total}] ${esc(ev.pspid)} 提交中…</span><br>`;
  } else if (ev.phase === "submitted") {
    log.innerHTML += `<span class="wait">… ${esc(ev.pspid)} 判题中…</span><br>`;
  } else if (ev.phase === "result") {
    const ok = (ev.status || "").toUpperCase() === "ACCEPTED";
    s.done++;
    if (ok) s.ok++; else s.bad++;
    batchProgress();
    log.innerHTML += `<span class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"} ${esc(ev.pspid)} ${esc(ev.statusText || ev.status)}${ev.score != null ? " (" + ev.score + ")" : ""}</span><br>`;
  } else if (ev.phase === "error") {
    if (ev.pspid) { s.done++; s.bad++; batchProgress(); }
    const prefix = ev.pspid ? `${ev.pspid} ` : "";
    log.innerHTML += `<span class="bad">✗ ${esc(prefix)}${esc(ev.error || "未知错误")}</span><br>`;
    if (ev.fatal) throw new Error(ev.error || "批量提交失败");
  } else if (ev.phase === "done") {
    batchSummary(log);
  }
  log.scrollTop = log.scrollHeight;
}

/* ============ 侧栏抽屉（移动端）============ */
function openSidebar() { $("main-view").classList.add("sidebar-open"); $("sidebar-backdrop").classList.remove("hidden"); }
function closeSidebar() { $("main-view").classList.remove("sidebar-open"); $("sidebar-backdrop").classList.add("hidden"); }

/* ============ 拖拽分隔条（横向/纵向自适应，比例持久化）============ */
function initSplitter() {
  const splitter = $("resizer");
  const detail = $("detail");
  const pane = document.querySelector(".statement-pane");
  if (!splitter || !detail || !pane) return;
  let dragging = false;
  let vertical = false;

  try { // 恢复上次拖出的比例
    const h = parseFloat(localStorage.getItem("pta_split_h"));
    if (h) pane.style.flexBasis = h + "%";
    const v = parseFloat(localStorage.getItem("pta_split_v"));
    if (v) pane.style.maxHeight = v + "%";
  } catch (e) { /* 静默 */ }

  splitter.addEventListener("mousedown", (e) => {
    dragging = true; e.preventDefault();
    vertical = getComputedStyle(detail).flexDirection === "column";
    splitter.classList.add("dragging");
    document.body.style.cursor = vertical ? "row-resize" : "col-resize";
    document.body.style.userSelect = "none";
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = detail.getBoundingClientRect();
    if (vertical) {
      let pct = ((e.clientY - rect.top) / rect.height) * 100;
      pct = Math.max(18, Math.min(78, pct));
      pane.style.maxHeight = pct + "%";
    } else {
      let pct = ((e.clientX - rect.left) / rect.width) * 100;
      pct = Math.max(24, Math.min(72, pct));
      pane.style.flexBasis = pct + "%";
    }
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    try {
      const val = parseFloat(vertical ? pane.style.maxHeight : pane.style.flexBasis);
      if (!isNaN(val)) localStorage.setItem(vertical ? "pta_split_v" : "pta_split_h", String(val));
    } catch (e) { /* 静默 */ }
  });
}

/* ============ 事件绑定 ============ */
function bind() {
  document.querySelectorAll(".login-card .seg-btn").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll(".login-card .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      $("tab-browser").classList.toggle("hidden", b.dataset.tab !== "browser");
      $("tab-cookie").classList.toggle("hidden", b.dataset.tab !== "cookie");
    };
  });
  $("btn-browser-login").onclick = browserLogin;
  $("btn-cookie-login").onclick = manualLogin;

  document.querySelectorAll("#scope-seg .seg-btn").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#scope-seg .seg-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.scope = b.dataset.scope;
      localStorage.setItem("pta_scope", state.scope);
      loadProblemSets();
    };
  });

  document.querySelectorAll("#filter-chips .fchip").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#filter-chips .fchip").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.statusFilter = b.dataset.f;
      localStorage.setItem("pta_status_filter", state.statusFilter);
      renderProblemList();
    };
  });

  $("set-switcher").onclick = (e) => { e.stopPropagation(); toggleMenu(); };
  $("set-menu").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { closeMenu(); $("more-menu").classList.add("hidden"); });

  $("btn-theme").onclick = toggleTheme;
  $("btn-logout").onclick = logout;
  $("btn-load-sets").onclick = () => loadProblemSets(true);
  $("btn-open-set").onclick = () => openSet();
  $("psid-input").addEventListener("keydown", (e) => { if (e.key === "Enter") openSet(); });
  $("problem-search").addEventListener("input", (e) => { state.filter = e.target.value; renderProblemList(); });
  $("btn-load-answer").onclick = loadLocalAnswer;
  $("btn-save-answer").onclick = saveAnswer;
  $("btn-submit").onclick = submitCurrent;
  $("btn-batch").onclick = openBatch;
  $("btn-batch-close").onclick = closeBatch;
  $("btn-batch-run").onclick = runBatch;
  $("btn-batch-cancel").onclick = () => { if (state.batchAbort) state.batchAbort.abort(); };
  $("btn-fs-dec").onclick = () => bumpCodeFs(-1);
  $("btn-fs-inc").onclick = () => bumpCodeFs(1);
  $("btn-fullscreen").onclick = () => toggleFullscreen();
  $("btn-more").onclick = (e) => { e.stopPropagation(); $("more-menu").classList.toggle("hidden"); };
  $("more-menu").addEventListener("click", (e) => e.stopPropagation());
  $("mm-load").onclick = () => { $("more-menu").classList.add("hidden"); loadLocalAnswer(); };
  $("mm-save").onclick = () => { $("more-menu").classList.add("hidden"); saveAnswer(); };
  $("btn-sidebar").onclick = openSidebar;
  $("sidebar-backdrop").onclick = closeSidebar;
  $("batch-modal").addEventListener("click", (e) => { if (e.target.id === "batch-modal") closeBatch(); });

  const code = $("code");
  code.addEventListener("input", () => { updateGutter(); saveDraft(code.value); });
  code.addEventListener("scroll", () => { const g = $("gutter"); if (g) g.scrollTop = code.scrollTop; });
  code.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const s = code.selectionStart, en = code.selectionEnd;
      code.value = code.value.slice(0, s) + "    " + code.value.slice(en);
      code.selectionStart = code.selectionEnd = s + 4;
      updateGutter();
      saveDraft(code.value);
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveAnswer();
    } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      submitCurrent();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("editor-panel").classList.contains("fullscreen")) { toggleFullscreen(false); return; }
      closeBatch(); closeMenu(); closeSidebar();
      $("more-menu").classList.add("hidden");
    }
    const tag = (e.target.tagName || "").toLowerCase();
    const inField = tag === "input" || tag === "textarea" || tag === "select";
    if (e.key === "/" && !inField) {
      const s = $("problem-search");
      if (s && !$("main-view").classList.contains("hidden")) { e.preventDefault(); s.focus(); }
    }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && !inField &&
        !$("main-view").classList.contains("hidden") && $("batch-modal").classList.contains("hidden")) {
      e.preventDefault();
      moveSelection(e.key === "ArrowDown" ? 1 : -1);
    }
  });

  $("problem-search").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      moveSelection(e.key === "ArrowDown" ? 1 : -1);
    }
  });

  const savedScope = localStorage.getItem("pta_scope");
  if (savedScope) {
    state.scope = savedScope;
    document.querySelectorAll("#scope-seg .seg-btn").forEach((x) => x.classList.toggle("active", x.dataset.scope === savedScope));
  }
  const savedFilter = localStorage.getItem("pta_status_filter");
  if (savedFilter && ["all", "todo", "done"].includes(savedFilter)) {
    state.statusFilter = savedFilter;
    document.querySelectorAll("#filter-chips .fchip").forEach((x) => x.classList.toggle("active", x.dataset.f === savedFilter));
  }
  const savedFs = parseInt(localStorage.getItem("pta_code_fs") || "0", 10);
  if (savedFs) applyCodeFs(savedFs, false);
  const savedTheme = localStorage.getItem("pta_theme");
  if (savedTheme) applyTheme(savedTheme);

  initSplitter();
}

async function init() {
  bind();
  const s = await refreshStatus();
  if (s && s.loggedIn) {
    loadProblemSets();
    if (s.problemSetId) openSet(s.problemSetId); // 恢复上次打开的题集（含上次题目）
  }
}

init();
