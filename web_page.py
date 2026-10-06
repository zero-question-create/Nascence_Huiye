# web_page.py
# ========================================================================
# Web 控制面板的前端页面（内联字符串，无需构建工具与静态目录）。
# 由 web_panel.py 提供；{{BOT_NAME}} 会被替换成实际角色名。
#
# 页面结构对齐桌面面板（实施文档 5.3）：
#   总览：记忆数/链接数/词网/运行时长/时钟/精力（含作息强度与常睡时段）
#   QQ 服务：启停 + 连接状态 + NapCat 配置
#   日志：分栏（运行/思考/QQ/Ollama）+ 500 行上限 + 清空
#   配置：API 配置 + 生物钟参数
#   维护：伪造发送 / 伪造思考 / 记忆注入
# ========================================================================

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nascence {{BOT_NAME}} · 控制面板</title>
<style>
  :root{--bg:#0d1118;--panel:#141d2a;--border:#263247;--text:#e7edf6;--muted:#8493aa;
        --accent:#2d6cdf;--accent2:#7eb0ff;--ok:#77e2bd;--warn:#e2b877;--err:#e27777;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Noto Sans CJK SC","Microsoft YaHei",sans-serif}
  header{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  h1{margin:0;font-size:19px;letter-spacing:2px;font-weight:800}
  .badge{padding:5px 12px;border-radius:12px;font-size:12px;background:#183a31;color:var(--ok);border:1px solid #286653}
  .badge.off{background:#3a1e1e;color:var(--err);border-color:#653232}
  .badge.wait{background:#3a3320;color:var(--warn);border-color:#655a32}
  #shutdownBtn{margin-left:auto;background:#3a1e1e;color:#ffb3b3;border:1px solid #653232;
               border-radius:8px;padding:8px 16px;cursor:pointer;font-weight:700}
  #shutdownBtn:hover{background:#4d2626}
  #shutdownBtn:disabled{opacity:.5;cursor:not-allowed}
  nav{display:flex;gap:6px;padding:12px 24px 0;flex-wrap:wrap}
  nav button{background:var(--panel);color:var(--muted);border:1px solid var(--border);
             padding:9px 18px;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px}
  nav button.active{color:#fff;background:#1b2b45;border-bottom-color:transparent}
  main{padding:18px 24px 40px}
  .page{display:none}.page.active{display:block}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
  .card .k{color:var(--muted);font-size:12px}
  .card .v{font-size:24px;font-weight:800;color:var(--accent2);margin-top:2px}
  .card .c{font-size:11px;color:var(--muted);margin-top:4px}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:14px}
  .panel h3{margin:0 0 10px;font-size:15px}
  label{display:block;color:var(--muted);font-size:12px;margin:8px 0 4px}
  input,textarea,select{width:100%;background:#0f1621;border:1px solid #2a3850;border-radius:7px;
                        padding:8px 10px;color:var(--text);font:inherit}
  textarea{min-height:80px;resize:vertical}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:0 16px}
  .grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px 16px}
  .sub-box{background:#0f1621;border:1px solid #1f2a3a;border-radius:8px;padding:12px 14px;margin-bottom:14px}
  .sub-box h4{margin:0 0 8px;font-size:13px;color:var(--accent2);font-weight:700}
  button.act{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:9px 16px;
             font-weight:700;cursor:pointer}
  button.act.sec{background:#222d3d;border:1px solid #35445a}
  button.act:disabled{background:#344156;color:#8b98aa;cursor:not-allowed}
  #logs{background:#101722;border:1px solid var(--border);border-radius:8px;padding:10px;
        height:55vh;overflow:auto;font:12px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap}
  #logs .t{color:#6a7d96}#logs .w{color:var(--warn)}#logs .e{color:var(--err)}
  #feed{max-height:38vh;overflow:auto}
  .msg{border-left:3px solid var(--accent);padding:6px 10px;margin:6px 0;background:#101722;border-radius:0 6px 6px 0}
  .msg .who{color:var(--muted);font-size:11px}
  .hint{color:var(--muted);font-size:12px;margin-top:6px}
  .kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:13px}
  .kv .k{color:var(--muted)}
  #toast{position:fixed;right:20px;bottom:20px;background:#1b2b45;border:1px solid var(--border);
         padding:10px 16px;border-radius:8px;display:none;max-width:60vw}
</style>
</head>
<body>
<header>
  <h1>Nascence · {{BOT_NAME}}</h1>
  <span id="qqBadge" class="badge off">QQ 服务：未运行</span>
  <span id="stateBadge" class="badge wait">加载中</span>
  <button id="shutdownBtn" title="停止 QQ 服务、保存全部数据并退出面板">关闭面板</button>
</header>

<nav>
  <button class="active" data-page="overview">总览</button>
  <button data-page="qq">QQ 服务</button>
  <button data-page="logs">日志</button>
  <button data-page="config">配置</button>
  <button data-page="maint">维护</button>
</nav>

<main>
  <!-- 总览 -->
  <section class="page active" id="page-overview">
    <div class="cards">
      <div class="card"><div class="k">热记忆节点</div><div class="v" id="c-mem">--</div></div>
      <div class="card"><div class="k">记忆链接</div><div class="v" id="c-link">--</div></div>
      <div class="card"><div class="k">词网规模</div><div class="v" id="c-word">--</div></div>
      <div class="card"><div class="k">运行时间</div><div class="v" id="c-runtime">--</div></div>
      <div class="card"><div class="k">虚拟时间</div><div class="v" id="c-clock">--</div></div>
      <div class="card"><div class="k">精力</div><div class="v" id="c-energy">--</div><div class="c" id="c-rhythm"></div></div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h3>运行控制</h3>
      <div class="row">
        <button class="act" id="ovQQStart">启动 QQ 服务</button>
        <button class="act sec" id="ovQQStop">停止 QQ 服务</button>
        <button class="act sec" id="btnSave">立即保存全部数据</button>
      </div>
      <div class="hint">QQ 服务在后台线程独立运行；关闭浏览器不会停止服务与认知循环。</div>
    </div>
    <div class="panel">
      <h3>最近的群消息</h3>
      <div id="feed"><div class="hint">暂无消息</div></div>
    </div>
  </section>

  <!-- QQ -->
  <section class="page" id="page-qq">
    <div class="panel">
      <h3>服务控制</h3>
      <div class="row">
        <button class="act" id="btnQQStart">启动 QQ 服务</button>
        <button class="act sec" id="btnQQStop">停止 QQ 服务</button>
      </div>
      <div class="kv" style="margin-top:12px">
        <div class="k">服务状态</div><div id="q-status">未运行</div>
        <div class="k">NapCat 连接</div><div id="q-napcat">未连接</div>
        <div class="k">WebSocket</div><div>ws://127.0.0.1:6700/ws（NapCat 作为主动客户端连入）</div>
        <div class="k">消息发送</div><div>NapCat WebSocket Action</div>
      </div>
      <div class="hint">QQ 服务在后台线程独立运行；关闭浏览器不会停止服务与认知循环。</div>
    </div>
    <div class="panel">
      <h3>NapCat 配置</h3>
      <div class="grid2">
        <div><label>机器人 QQ</label><input id="q-bot"></div>
        <div><label>主动发言群号</label><input id="q-group"></div>
        <div><label>NapCat 鉴权 Token</label><input id="q-token"></div>
        <div><label>NapCat HTTP 接口地址（正向 HTTP，用于语音下载）</label><input id="q-http" placeholder="http://127.0.0.1:5700"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveNapcat">保存并热刷新</button></div>
      <div class="hint">保存后立即生效：运行中的 QQ 服务会直接使用新值，无需重启。</div>
    </div>
  </section>

  <!-- 日志 -->
  <section class="page" id="page-logs">
    <div class="panel">
      <div class="row">
        <select id="logCat" style="max-width:180px">
          <option value="all">全部</option>
          <option value="runtime">运行日志</option>
          <option value="thinking">思考过程</option>
          <option value="qqbot">QQbot</option>
          <option value="ollama">Ollama</option>
        </select>
        <button class="act sec" id="btnClearLog">清空显示</button>
        <span class="hint">仅保留最近 500 行，避免长时间运行占用内存</span>
      </div>
      <div id="logs" style="margin-top:10px"></div>
    </div>
  </section>

  <!-- 配置 -->
  <section class="page" id="page-config">
    <div class="panel">
      <h3>控制面板设置</h3>
      <div class="grid2">
        <div><label>Web 控制面板端口（默认 32123，修改后下次启动生效）</label><input id="f-panel-port" type="number" min="1" max="65535" placeholder="32123"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSavePanel">保存面板端口</button></div>
      <div class="hint">端口号修改后写入配置文件，将在下次启动控制面板时生效。</div>
    </div>
    <div class="panel">
      <h3>大模型 (LLM) 与向量嵌入配置</h3>

      <!-- 主大模型区块 -->
      <div class="sub-box">
        <h4>主模型（Primary LLM）— 文本理解、思考与回复生成</h4>
        <div class="grid3">
          <div><label>服务地址 (Base URL)</label><input id="f-pburl" placeholder="https://api.deepseek.com"></div>
          <div><label>模型名称 (Model)</label><input id="f-pmodel" placeholder="deepseek-v4-flash"></div>
          <div><label>API Key（留空表示不修改）</label><input id="f-pkey" type="password" placeholder="已配置则留空"></div>
        </div>
      </div>

      <!-- 次大模型区块（独立换行展示） -->
      <div class="sub-box">
        <h4>次模型（Secondary LLM）— 多模态图片、表情包与音视频理解</h4>
        <div class="grid3">
          <div><label>服务地址 (Base URL)</label><input id="f-sburl" placeholder="https://api.deepseek.com"></div>
          <div><label>模型名称 (Model)</label><input id="f-smode" placeholder="deepseek-v4-flash-vision-exp"></div>
          <div><label>API Key（留空表示不修改）</label><input id="f-skey" type="password" placeholder="已配置则留空"></div>
        </div>
      </div>

      <!-- 向量嵌入模型区块（独立换行展示） -->
      <div class="sub-box">
        <h4>向量嵌入模型（Ollama Embedding）— 语义记忆向量化与检索</h4>
        <div class="grid2">
          <div><label>Ollama 服务地址（本地填 http://127.0.0.1:11434，容器访问宿主机填 http://host.docker.internal:11434）</label><input id="f-oburl" placeholder="http://127.0.0.1:11434"></div>
          <div><label>嵌入模型名称（须已执行 ollama pull）</label><input id="f-omodel" placeholder="shaw/dmeta-embedding-zh"></div>
        </div>
      </div>

      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveCfg">保存模型与 API 配置</button></div>
      <div class="hint">保存后立即生效：API 客户端与向量模型配置实时热刷新，无需重启面板或容器。</div>
    </div>
    <div class="panel">
      <h3>生物钟参数（动力学速率，非钟点）</h3>
      <div class="grid2">
        <div><label>清醒时间常数（秒，默认 18.40h）</label><input id="f-twake" type="number" step="600"></div>
        <div><label>睡眠时间常数（秒，默认 9.1h）</label><input id="f-tsleep" type="number" step="600"></div>
        <div><label>入睡阈值（默认 0.62）</label><input id="f-onset" type="number" step="0.01"></div>
        <div><label>醒来阈值（默认 0.20）</label><input id="f-wake" type="number" step="0.01"></div>
        <div><label>作息强度权重（默认 0.25）</label><input id="f-rw" type="number" step="0.05"></div>
        <div><label>入睡所需安静秒数（默认 300）</label><input id="f-idle" type="number" step="30"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveBio">保存生物钟参数</button></div>
      <div class="hint">参数保存后立即作用于睡眠动力学；"常睡时段"始终由她自己的经历学得，不受此设置影响。</div>
    </div>
  </section>

  <!-- 维护 -->
  <section class="page" id="page-maint">
    <div class="panel">
      <h3>伪造发送</h3>
      <div class="hint">以 {{BOT_NAME}} 身份向当前主动发言群发送消息，并写入历史对话与记忆库。</div>
      <textarea id="m-send" placeholder="输入要以 {{BOT_NAME}} 身份发送的内容"></textarea>
      <div class="row" style="margin-top:8px"><button class="act" id="btnFakeSend">伪造发送</button></div>
    </div>
    <div class="panel">
      <h3>伪造思考</h3>
      <div class="hint">模拟内心思考流程（LLM 拆解→检索→扩散→拼接），不向 QQ 发送消息，仅写入历史与记忆库。</div>
      <textarea id="m-think" placeholder="输入要让她思考的内容"></textarea>
      <div class="row" style="margin-top:8px"><button class="act" id="btnFakeThink">伪造思考</button></div>
    </div>
    <div class="panel">
      <h3>管理员记忆注入</h3>
      <div class="hint">直接写入记忆图并立即持久化。</div>
      <textarea id="m-inject" placeholder="输入要植入的记忆（第一人称）"></textarea>
      <div class="row" style="margin-top:8px"><button class="act" id="btnInject">注入并保存记忆</button></div>
    </div>
  </section>
</main>

<div id="toast"></div>

<script>
const BOT = "{{BOT_NAME}}";
const $ = (id) => document.getElementById(id);
const MAX_LOG_LINES = 500;

function toast(text){
  const el = $('toast'); el.textContent = text; el.style.display = 'block';
  setTimeout(()=> el.style.display = 'none', 2500);
}
async function post(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify(body||{})});
  const data = await r.json().catch(()=>({}));
  if(!r.ok || data.ok === false) throw new Error(data.error || ('HTTP '+r.status));
  return data;
}
async function getJSON(url){
  const r = await fetch(url); return await r.json();
}

// 页签切换
document.querySelectorAll('nav button').forEach(btn=>{
  btn.onclick = () => {
    document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    btn.classList.add('active');
    $('page-'+btn.dataset.page).classList.add('active');
  };
});

// 总览与配置刷新
let sysState = '';
let shuttingDown = false;
let statsInFlight = false;
async function refreshStats(){
  // 0.5s 一轮且请求未返回时不叠加：避免慢响应把线程池堆满（表现为界面越来越卡）
  if(statsInFlight) return;
  statsInFlight = true;
  try{
    const d = await getJSON('/api/stats');
    $('c-mem').textContent = d.memory ?? '--';
    $('c-link').textContent = d.links ?? '--';
    $('c-word').textContent = d.words ?? '--';
    $('c-runtime').textContent = d.runtime ?? '--';
    $('c-clock').textContent = d.clock_text ?? '--';
    if(d.energy !== undefined){
      const pct = Math.round(d.energy*100);
      $('c-energy').textContent = pct + '% ' + (d.state === 'asleep' ? '睡眠中' : '清醒');
      let cap = '作息积累中';
      if(d.rhythm_nights > 0){
        const h = d.sleep_hours || [];
        cap = `作息${(d.circadian??0).toFixed(2)} ${d.rhythm_nights}晚` +
              (h.length ? ` 常睡${Math.min(...h)}-${Math.max(...h)}点` : '');
      }
      $('c-rhythm').textContent = cap;
      const b = $('stateBadge');
      sysState = d.state === 'asleep' ? '睡眠中' : '清醒';
      // 核心初始化期间显示具体阶段与已用时长，而不是笼统的"加载中"
      if(d.core_ready){
        b.textContent = sysState;
      }else if(d.init_stage && d.init_stage !== '未开始'){
        const el = d.init_elapsed ? `（${d.init_elapsed}s）` : '';
        b.textContent = '核心加载：' + d.init_stage + el;
      }else{
        b.textContent = '核心加载中';
      }
      b.className = 'badge' + (d.state === 'asleep' ? ' wait' : '');
    } else if(d.biorhythm_error){
      $('stateBadge').textContent = d.init_stage && d.init_stage !== '未开始'
        ? ('核心加载：' + d.init_stage) : '生物钟未就绪';
      $('stateBadge').className = 'badge wait';
    }
    const qb = $('qqBadge');
    qb.textContent = 'QQ 服务：' + (d.qq_running ? '运行中' : '未运行');
    qb.className = 'badge' + (d.qq_running ? '' : ' off');
    $('q-status').textContent = d.qq_running ? '运行中' : '未运行';
    $('q-napcat').textContent = d.napcat_connected ? '已连接' : (d.qq_running ? '等待 NapCat 连入…' : '未连接');
  }catch(e){ /* 面板不因统计失败而中断 */ }
  finally{ statsInFlight = false; }
}
async function loadConfig(){
  try{
    const c = await getJSON('/api/config');
    $('f-pburl').value = c.primary_base_url || '';
    $('f-pmodel').value = c.primary_model || '';
    $('f-sburl').value = c.secondary_base_url || '';
    $('f-smode').value = c.secondary_model || '';
    $('f-oburl').value = c.ollama_base_url || '';
    $('f-omodel').value = c.ollama_embed_model || '';
    $('q-bot').value = c.bot_qq || '';
    $('q-group').value = c.active_group_id || '';
    $('q-token').value = c.napcat_token || '';
    $('q-http').value = c.napcat_http_url || '';
    $('f-twake').value = c.biorhythm_wake_seconds ?? '';
    $('f-tsleep').value = c.biorhythm_sleep_seconds ?? '';
    $('f-onset').value = c.biorhythm_onset_threshold ?? '';
    $('f-wake').value = c.biorhythm_wake_threshold ?? '';
    $('f-rw').value = c.biorhythm_rhythm_weight ?? 0.25;
    $('f-idle').value = c.biorhythm_idle_to_sleep ?? 300;
    $('f-panel-port').value = c.panel_port ?? 32123;
  }catch(e){}
}

// 日志：环形缓冲。
// 每条日志进入环形缓冲后，按当前筛选重建视图；切换类型不会再丢之前的日志，
// 也不会因为反复切换而无限增长（上限 MAX_LOG_LINES 条，只滚不丢）。
const logs = $('logs');
const LOG_CATS = ['runtime', 'thinking', 'qqbot', 'ollama'];
const logRing = [];               // 环形缓冲：{cat, line, cls}[]
function pushLog(cat, line){
  const cls = /ERROR|失败|异常/.test(line) ? 'e' : (/WARNING|警告/.test(line) ? 'w' : '');
  logRing.push({cat, line, cls});
  if(logRing.length > MAX_LOG_LINES) logRing.shift();
  // 仅在当前筛选可见时追加，避免无关类型触发重绘
  const filter = $('logCat').value;
  if(filter !== 'all' && filter !== cat) return;
  appendLogNode(logRing[logRing.length - 1]);
  while(logs.childNodes.length > MAX_LOG_LINES) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;
}
function appendLogNode(entry){
  const div = document.createElement('div');
  div.className = entry.cls || '';
  div.textContent = entry.line;
  logs.appendChild(div);
}
function rebuildLogView(){
  const filter = $('logCat').value;
  logs.innerHTML = '';
  for(const entry of logRing){
    if(filter !== 'all' && filter !== entry.cat) continue;
    appendLogNode(entry);
  }
  logs.scrollTop = logs.scrollHeight;
}
function addFeed(sender, content, source){
  const feed = $('feed');
  if(feed.querySelector('.hint')) feed.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'msg';
  const safe = String(content).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
  div.innerHTML = `<div class="who">${sender} · ${source||''}</div><div>${safe}</div>`;
  feed.prepend(div);
  // 条数上限：超出后丢弃最旧的一条，容器另有 CSS 限高，双击查看需滚动
  while(feed.childNodes.length > 30) feed.removeChild(feed.lastChild);
}

function connectWS(){
  if(shuttingDown) return;   // 面板关闭中，不再重连
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  // 连接（含重连）时清空日志窗与环形缓冲：服务端会补发本次会话日志尾部，
  // 避免重复堆叠（重连时缓冲里的内容会被补发内容覆盖）
  ws.onopen = () => { logs.innerHTML = ''; logRing.length = 0; };
  ws.onmessage = (ev) => {
    let m; try{ m = JSON.parse(ev.data); }catch(_){ return; }
    if(m.type === 'log') pushLog(m.cat, m.line);
    else if(m.type === 'status') toast(m.text);
    else if(m.type === 'error') toast('错误：' + m.text);
    else if(m.type === 'message') addFeed(m.sender, m.content, m.source);
    else if(m.type === 'stage'){
      const b = $('stateBadge');
      if(m.done){ b.textContent = sysState || '就绪'; b.className = 'badge'; toast('核心已就绪'); }
      else { b.textContent = '核心加载：' + m.text; b.className = 'badge wait'; }
    }    else if(m.type === 'shutdown'){
      shuttingDown = true;
      toast('面板正在关闭…');
      document.body.style.opacity = '0.6';
    }
  };
  ws.onclose = () => { if(!shuttingDown) setTimeout(connectWS, 3000); };   // 断线自动重连（关闭中除外）
}

// 按钮绑定
$('btnSave').onclick = async () => {
  try{ await post('/api/save'); toast('已强制保存全部数据'); }
  catch(e){ toast('保存失败：'+e.message); }
};
$('shutdownBtn').onclick = async () => {
  if(!confirm('确定要关闭面板吗？\n将停止 QQ 服务、保存全部数据并退出。')) return;
  const btn = $('shutdownBtn');
  btn.disabled = true; btn.textContent = '正在关闭…';
  shuttingDown = true;
  try{
    await post('/api/shutdown');
    toast('正在停止服务并保存数据，页面稍后失效即可关闭');
    setTimeout(()=>{ document.body.style.opacity = '0.5'; }, 500);
  }catch(e){
    shuttingDown = false;
    btn.disabled = false; btn.textContent = '关闭面板';
    toast('关闭请求失败：'+e.message);
  }
};
$('btnQQStart').onclick = async () => {
  toast('QQ 服务启动中（首次需初始化，请稍候）');
  try{ await post('/api/qq/start'); toast('QQ 服务已启动'); refreshStats(); }
  catch(e){ toast('启动失败：'+e.message); }
};
$('btnQQStop').onclick = async () => {
  toast('正在停止 QQ 服务…');
  try{ await post('/api/qq/stop'); toast('QQ 服务已停止'); refreshStats(); }
  catch(e){ toast('停止失败：'+e.message); }
};
$('ovQQStart').onclick = async () => {
  toast('QQ 服务启动中（首次需初始化，请稍候）');
  try{ await post('/api/qq/start'); toast('QQ 服务已启动'); refreshStats(); }
  catch(e){ toast('启动失败：'+e.message); }
};
$('ovQQStop').onclick = async () => {
  toast('正在停止 QQ 服务…');
  try{ await post('/api/qq/stop'); toast('QQ 服务已停止'); refreshStats(); }
  catch(e){ toast('停止失败：'+e.message); }
};
$('btnClearLog').onclick = () => { logs.innerHTML = ''; logRing.length = 0; };
$('logCat').onchange = () => { rebuildLogView(); };
$('btnSaveCfg').onclick = async () => {
  try{
    await post('/api/config', {
      primary_base_url:$('f-pburl').value.trim(),
      primary_model:$('f-pmodel').value.trim(),
      primary_api_key:$('f-pkey').value.trim(),
      secondary_base_url:$('f-sburl').value.trim(),
      secondary_model:$('f-smode').value.trim(),
      secondary_api_key:$('f-skey').value.trim(),
      ollama_base_url:$('f-oburl').value.trim(),
      ollama_embed_model:$('f-omodel').value.trim(),
    });
    $('f-pkey').value = ''; $('f-skey').value = '';
    toast('模型与 API 配置已保存并热刷新');
  }catch(e){ toast('保存失败：'+e.message); }
};
$('btnSavePanel').onclick = async () => {
  const p = parseInt($('f-panel-port').value);
  if (!p || p < 1 || p > 65535) {
    toast('请输入有效端口号 (1-65535)');
    return;
  }
  try{
    await post('/api/config', { panel_port: p });
    toast('面板端口已保存为 ' + p + '，下次启动生效');
  }catch(e){ toast('保存失败：'+e.message); }
};
$('btnSaveNapcat').onclick = async () => {
  try{
    await post('/api/config', {
      bot_qq:$('q-bot').value.trim(),
      active_group_id:$('q-group').value.trim(),
      napcat_token:$('q-token').value.trim(),
      napcat_http_url:$('q-http').value.trim(),
    });
    toast('NapCat 设置已保存并热刷新');
    refreshStats();
  }catch(e){ toast('保存失败：'+e.message); }
};
$('btnSaveBio').onclick = async () => {
  const num = (id) => { const v = parseFloat($(id).value); return isFinite(v) ? v : undefined; };
  try{
    await post('/api/config', {
      biorhythm_wake_seconds: num('f-twake'),
      biorhythm_sleep_seconds: num('f-tsleep'),
      biorhythm_onset_threshold: num('f-onset'),
      biorhythm_wake_threshold: num('f-wake'),
      biorhythm_rhythm_weight: num('f-rw'),
      biorhythm_idle_to_sleep: num('f-idle'),
    });
    toast('生物钟参数已保存并热加载');
  }catch(e){ toast('保存失败：'+e.message); }
};
$('btnFakeSend').onclick = async () => {
  try{ const r = await post('/api/maintenance', {kind:'fake_send', text:$('m-send').value});
       $('m-send').value=''; toast('已发送（'+r.detail+'）'); }
  catch(e){ toast('失败：'+e.message); }
};
$('btnFakeThink').onclick = async () => {
  try{ const r = await post('/api/maintenance', {kind:'fake_think', text:$('m-think').value});
       $('m-think').value=''; toast('思考完成'); addFeed(BOT + '（内心）', r.detail, '伪造思考'); }
  catch(e){ toast('失败：'+e.message); }
};
$('btnInject').onclick = async () => {
  try{ const r = await post('/api/maintenance', {kind:'inject', text:$('m-inject').value});
       $('m-inject').value=''; toast('已注入（'+r.detail+'）'); }
  catch(e){ toast('失败：'+e.message); }
};

connectWS();
refreshStats();
loadConfig();
setInterval(refreshStats, 500);
</script>
</body>
</html>
"""
