/* webui/app.js —— Nascence 辉夜 Web 控制面板前端逻辑 */
"use strict";

// ========== 全局状态 ==========
const state = {
  activeTab: "overview",
  logCat: "runtime",
  pendingChats: 0,   // 等待回复的对话数（不阻塞输入，仅作状态提示）
  botName: "辉夜",
};

// ========== 工具函数 ==========
async function api(url, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function $(id) { return document.getElementById(id); }

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>");
}

function setGlobalStatus(text, isErr = false) {
  const el = $("globalStatus");
  el.textContent = text;
  el.classList.toggle("err", isErr);
}

function showError(msg) {
  setGlobalStatus(msg, true);
  console.error(msg);
  setTimeout(() => setGlobalStatus("运行中"), 5000);
}

// ========== Tab 切换 ==========
function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("active", p.id === name);
  });
  if (name === "overview") refreshStats();
  if (name === "chat") loadHistory();
  if (name === "logs") renderLog();
  if (name === "config") { loadBotName(); loadModelConfig(); }
  if (name === "maintenance") refreshDimStatus();
}

document.querySelectorAll(".tab").forEach(t => {
  t.addEventListener("click", () => switchTab(t.dataset.tab));
});

// ========== WebSocket 实时推送 ==========
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "log") {
        // 无论当前在哪个页，都先入缓冲，保证切回日志页时日志完整
        appendLogToBuffer(msg.cat, msg.line);
        if (msg.cat === state.logCat) appendLogLine(msg.line);
      } else if (msg.type === "message") {
        appendBubble(msg.sender, msg.content, msg.source);
      } else if (msg.type === "status") {
        setGlobalStatus(msg.text);
        if (msg.text.includes("认知循环")) $("qqStatusBadge").textContent = msg.text;
      }
    } catch (e) { /* ignore */ }
  };
  ws.onclose = () => setTimeout(connectWs, 3000);
}

// ========== 总览 ==========
async function refreshStats() {
  try {
    const s = await api("/api/status");
    $("statMem").textContent = s.mem;
    $("statLinks").textContent = s.links;
    $("statWords").textContent = s.words;
    $("statRuntime").textContent = s.runtime;
    $("statClock").textContent = s.clock;
    $("qqStatusBadge").textContent = s.cognition.running ? "运行中" : "已停止";
    $("qqStatusBadge").classList.toggle("off", !s.cognition.running);
    if (s.bot_name) {
      state.botName = s.bot_name;
      const mention = $("mentionName");
      if (mention) mention.textContent = s.bot_name;
    }
    setGlobalStatus("运行中");
  } catch (e) { /* silent */ }
}
setInterval(refreshStats, 3000);

// ========== 日志 ==========
// allLogs 作为全量日志缓冲（各分类），新日志无论是否在当前页都先入缓冲，
// 切回日志页时按缓冲渲染，避免"切走再切回只剩初始几条"。
let allLogs = { runtime: [], thinking: [], model: [], connection: [] };
// 前端缓冲上限（各分类统一 1000，与后端一致）
const LOG_BUFFER_LIMIT = 1000;
let currentLogCat = "runtime";

function appendLogToBuffer(cat, line) {
  const buf = (allLogs[cat] = allLogs[cat] || []);
  buf.push(line);
  if (buf.length > LOG_BUFFER_LIMIT) buf.splice(0, buf.length - LOG_BUFFER_LIMIT);
}

function appendLogLine(line) {
  const view = $("logView");
  const div = document.createElement("div");
  div.textContent = line;
  view.appendChild(div);
  view.scrollTop = view.scrollHeight;
  // 限制行数
  while (view.children.length > LOG_BUFFER_LIMIT) view.removeChild(view.firstChild);
}

function renderLog() {
  const view = $("logView");
  view.innerHTML = "";
  (allLogs[currentLogCat] || []).forEach(l => appendLogLine(l));
  view.scrollTop = view.scrollHeight;
}

async function loadLogs() {
  try {
    const data = await api("/api/logs");
    allLogs = data.logs;
    // 补全缺失的分类缓冲
    for (const cat of ["runtime", "thinking", "model", "connection"]) {
      if (!allLogs[cat]) allLogs[cat] = [];
      // 按统一上限裁剪（后端已限，这里兜底）
      if (allLogs[cat].length > LOG_BUFFER_LIMIT) allLogs[cat] = allLogs[cat].slice(-LOG_BUFFER_LIMIT);
    }
    renderLog();
  } catch (e) { /* silent */ }
}

document.querySelectorAll(".log-tab").forEach(t => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".log-tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    currentLogCat = t.dataset.cat;
    state.logCat = currentLogCat;
    renderLog();
  });
});

$("btnClearLogs").addEventListener("click", async () => {
  // 清空视图 + 前端缓冲 + 后端缓冲：否则切页/新日志到达/刷新页面时会重新渲染出已清空的旧日志
  for (const cat of Object.keys(allLogs)) allLogs[cat] = [];
  $("logView").innerHTML = "";
  try { await api("/api/logs/clear", "POST", {}); } catch (e) { /* silent */ }
});

// ========== 对话测试 ==========
function appendBubble(name, text, source) {
  const view = $("chatView");
  const div = document.createElement("div");
  const who = source ? `[${source}] ` : "";
  let cls = "other";
  if (name === state.botName) cls = "bot";
  else if (name === "彩叶") cls = "caiye";
  div.className = `bubble ${cls}`;
  div.innerHTML = `<div class="who">${escapeHtml(who + name)}</div>` +
    `<div class="content">${escapeHtml(text)}</div>`;
  view.appendChild(div);
  view.scrollTop = view.scrollHeight;
}

async function sendChat() {
  const text = $("chatInput").value.trim();
  if (!text) return;
  const sender = $("chatSender").value.trim() || "测试群友";
  const mentioned = $("chatMention").checked;
  appendBubble(sender, text, "测试");
  $("chatInput").value = "";
  // 不阻塞输入：用户可以随时继续发言，回复到达后依次追加
  state.pendingChats += 1;
  $("btnChatSend").disabled = false;
  $("btnChatSend").textContent = state.pendingChats > 0 ? `发送（${state.pendingChats} 条待回复）` : "发送";
  try {
    const r = await api("/api/chat", "POST", { sender, text, mentioned });
    appendBubble(state.botName, r.reply || "（静默）", "测试");
  } catch (e) {
    showError(e.message);
  } finally {
    state.pendingChats = Math.max(0, state.pendingChats - 1);
    $("btnChatSend").disabled = false;
    $("btnChatSend").textContent = state.pendingChats > 0 ? `发送（${state.pendingChats} 条待回复）` : "发送";
  }
}
$("btnChatSend").addEventListener("click", sendChat);
$("chatInput").addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "Enter") sendChat();
});

async function loadHistory() {
  try {
    const data = await api("/api/history");
    $("chatView").innerHTML = "";
    data.messages.forEach(m => appendBubble(m.sender, m.content, m.source));
  } catch (e) { /* silent */ }
}

// ========== 模型管理 ==========
const MODEL_LABELS = {
  text: "文本能力",
  embed: "语义向量",
  multimodal: "多模态图片",
};

async function loadModelConfig() {
  try {
    const cfg = await api("/api/config");
    const status = await api("/api/status");
    const box = $("modelCards");
    box.innerHTML = "";
    for (const [name, label] of Object.entries(MODEL_LABELS)) {
      const b = (cfg.backends || {})[name] || {};
      const m = (status.models || {})[name] || {};
      const card = document.createElement("div");
      card.className = "model-card";
      card.innerHTML = `
        <div class="mc-head">
          <div class="mc-title">${label}</div>
          <label class="switch">
            <input type="checkbox" data-name="${name}" ${b.use_default ? "checked" : ""}>
            <span class="track"></span>
          </label>
        </div>
        <div class="muted">当前：${b.use_default
          ? (m.local_ready ? `本地默认模型（端口 ${m.port}，已就绪）` : `本地默认模型（端口 ${m.port}，未加载）`)
          : `外部 API → ${b.api_model || "（未配置模型名）"}`}</div>
        <div class="row">
          <div class="label">Base URL</div>
          <input type="text" data-field="api_base_url" data-name="${name}" value="${escapeHtml(b.api_base_url || "")}" style="flex:1">
        </div>
        <div class="row">
          <div class="label">API Key</div>
          <input type="text" data-field="api_key" data-name="${name}" value="${escapeHtml(b.api_key || "")}" placeholder="留空则保留原 Key" style="flex:1" autocomplete="off">
        </div>
        <div class="row">
          <div class="label">模型名</div>
          <input type="text" data-field="api_model" data-name="${name}" value="${escapeHtml(b.api_model || "")}" style="flex:1">
        </div>
        <div class="row">
          <button class="btn" data-save="${name}">保存并应用</button>
          <span class="muted" data-hint="${name}"></span>
        </div>
      `;
      box.appendChild(card);
    }
  } catch (e) { showError(e.message); }
}

$("modelCards").addEventListener("click", async (ev) => {
  const saveBtn = ev.target.closest("[data-save]");
  if (!saveBtn) return;
  const name = saveBtn.dataset.save;
  const card = saveBtn.closest(".model-card");
  const useDefault = card.querySelector(`input[type=checkbox][data-name="${name}"]`).checked;
  const apiBaseUrl = card.querySelector(`input[data-field=api_base_url][data-name="${name}"]`).value;
  const apiKey = card.querySelector(`input[data-field=api_key][data-name="${name}"]`).value;
  const apiModel = card.querySelector(`input[data-field=api_model][data-name="${name}"]`).value;
  try {
    const r = await api("/api/model/switch", "POST", {
      name, use_default: useDefault,
      api_base_url: apiBaseUrl, api_key: apiKey, api_model: apiModel,
    });
    const hint = card.querySelector(`[data-hint="${name}"]`);
    hint.textContent = r.message;
    if (r.needs_restart) {
      hint.textContent += "（需要重启进程才能加载本地默认模型）";
    }
    loadModelConfig();
  } catch (e) { showError(e.message); }
});

// ========== 配置页：Bot 名字 / 服务端监听端口 ==========
async function loadBotName() {
  try {
    const cfg = await api("/api/config");
    const input = $("botNameInput");
    if (input) input.value = cfg.bot_name || "";
    const portInput = $("serverPortInput");
    if (portInput) portInput.value = cfg.server_port || "";
    const clientPortInput = $("clientPortInput");
    if (clientPortInput) clientPortInput.value = cfg.client_port || "";
    const hint = $("botNameHint");
    if (hint) hint.textContent = "";
    const portHint = $("portHint");
    if (portHint) portHint.textContent = "";
    const clientPortHint = $("clientPortHint");
    if (clientPortHint) clientPortHint.textContent = "";
  } catch (e) { /* silent */ }
}

bind("btnSaveBotName", async () => {
  const input = $("botNameInput");
  const name = (input ? input.value : "").trim();
  if (!name) { showError("Bot 名字不能为空"); return; }
  const r = await runAction("btnSaveBotName", () => api("/api/config", "POST", { bot_name: name }));
  if (r) {
    const hint = $("botNameHint");
    hint.textContent = "已保存，需重启进程后生效。";
    alert(`Bot 名字已改为「${name}」，重启进程后生效。`);
  }
});

bind("btnSavePort", async () => {
  const portInput = $("serverPortInput");
  const port = parseInt((portInput ? portInput.value : "").trim(), 10);
  if (!port || port < 1 || port > 65535) { showError("端口需为 1-65535 的整数"); return; }
  const r = await runAction("btnSavePort", () => api("/api/config", "POST", { server_port: port }));
  if (r) {
    const hint = $("portHint");
    hint.textContent = r.message || "端口已保存。";
    alert(r.message || `端口已保存为 ${port}`);
  }
});

bind("btnSaveClientPort", async () => {
  const portInput = $("clientPortInput");
  const port = parseInt((portInput ? portInput.value : "").trim(), 10);
  if (!port || port < 1 || port > 65535) { showError("网页版客户端端口需为 1-65535 的整数"); return; }
  const r = await runAction("btnSaveClientPort", () => api("/api/config", "POST", { client_port: port }));
  if (r) {
    const hint = $("clientPortHint");
    hint.textContent = r.message || "端口已保存。";
    alert(r.message || `网页版客户端端口已保存为 ${port}`);
  }
});

// ========== 维护页：语义向量维度重建 ==========
let dimPollTimer = null;

async function refreshDimStatus() {
  try {
    const s = await api("/api/maintenance/embed_dim");
    const setText = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    setText("dimStored", s.stored_dim ?? "--");
    setText("dimModel", s.model_dim ?? "--");
    setText("dimCount", s.memory_count ?? "--");
    const btn = $("btnRebuildDim");
    const progress = $("dimProgress");
    if (s.running) {
      if (btn) btn.disabled = true;
      if (progress) progress.textContent = `重建中… ${s.done}/${s.total}${s.target_dim ? ` → ${s.target_dim} 维` : ""}`;
      if (!dimPollTimer) dimPollTimer = setInterval(refreshDimStatus, 2000);
    } else {
      if (btn) btn.disabled = false;
      if (progress) progress.textContent = s.error ? `任务失败：${s.error}` : "（空闲）";
      if (dimPollTimer) { clearInterval(dimPollTimer); dimPollTimer = null; }
    }
    const logEl = $("dimLog");
    if (logEl && Array.isArray(s.log) && s.log.length) {
      logEl.innerHTML = s.log.map(l => escapeHtml(l)).join("<br>");
      logEl.scrollTop = logEl.scrollHeight;
    }
  } catch (e) { /* silent */ }
}

bind("btnRebuildDim", async () => {
  const input = $("dimTarget");
  const raw = input ? input.value.trim() : "";
  const target_dim = raw ? (parseInt(raw, 10) || null) : null;
  if (!confirm("确认开始全量维度重建？\n该操作会重新生成全部记忆向量并重建索引，期间请勿进行其他记忆操作。")) return;
  const r = await runAction("btnRebuildDim", () => api("/api/maintenance/embed_rebuild", "POST", { target_dim }));
  if (r && r.ok) {
    setGlobalStatus("维度重建已开始，请留意进度");
    refreshDimStatus();
  } else if (r) {
    showError(r.message || "启动失败");
  }
});

// ========== 按钮绑定 ==========
function bind(btnId, fn) {
  const el = $(btnId);
  if (el) el.addEventListener("click", fn);
}

async function runAction(btnId, fn, okMsg) {
  const btn = $(btnId);
  btn.disabled = true;
  try {
    const r = await fn();
    if (okMsg) setGlobalStatus(okMsg);
    return r;
  } catch (e) {
    showError(e.message);
    return null;
  } finally {
    btn.disabled = false;
  }
}

bind("btnQQStart", () => runAction("btnQQStart", () => api("/api/cognition/start", "POST", {}), "认知循环启动中"));
bind("btnQQStop", () => runAction("btnQQStop", () => api("/api/cognition/stop", "POST", {}), "认知循环已停止"));
bind("btnSave", () => runAction("btnSave", () => api("/api/save", "POST", {}), "数据已保存"));

bind("btnFakeThink", async () => {
  const text = $("fakeThinkInput").value.trim();
  if (!text) return;
  const r = await runAction("btnFakeThink", () => api("/api/fake_think", "POST", { text }));
  if (r) { $("fakeThinkInput").value = ""; appendBubble(state.botName, `（内心）${r.reply}`, "伪造思考"); }
});

bind("btnInject", async () => {
  const content = $("memInput").value.trim();
  if (!content) return;
  const r = await runAction("btnInject", () => api("/api/inject_memory", "POST", { content }));
  if (r) { $("memInput").value = ""; setGlobalStatus(`记忆注入成功 ID=${r.memory_id}`); }
});

bind("btnShutdown", async () => {
  if (!confirm("确认安全退出全部服务？")) return;
  try {
    await api("/api/shutdown", "POST", {});
    setGlobalStatus("正在退出...");
  } catch (e) { showError(e.message); }
});

bind("btnClearLogs", () => { $("logView").innerHTML = ""; });

// ========== 初始化 ==========
(async function init() {
  connectWs();
  loadLogs();
  refreshStats();
})();
