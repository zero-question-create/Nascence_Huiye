# web_page.py
# ========================================================================
# Web 控制面板的前端页面（内联字符串，无需构建工具与静态目录）。
# 由 web_panel.py 提供；{{BOT_NAME}} 会被替换成实际角色名。
#
# 页面结构对齐桌面面板（实施文档 5.3）：
#   总览：记忆数/链接数/词网/运行时长/时钟/精力；启停全局认知循环
#   插件：每个插件启动/停止；点击进入插件自己的配置
#   日志：分栏（运行/思考/QQ/Ollama）+ 500 行上限 + 清空
#   配置：API 配置 + 生物钟参数
#   维护：伪造思考 / 管理员消息注入
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
  input.missing,textarea.missing,select.missing{border-color:var(--err);box-shadow:0 0 0 1px var(--err)}
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
  <span id="cognitiveBadge" class="badge off">认知循环：已停止</span>
  <span id="stateBadge" class="badge wait">加载中</span>
  <button id="shutdownBtn" title="停止认知循环与全部插件服务、保存全部数据并退出面板">关闭面板</button>
</header>

<nav>
  <button class="active" data-page="overview">总览</button>
  <button data-page="plugins">插件</button>
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
      <div class="card"><div class="k">缓存命中</div><div class="v" id="c-cache">--</div><div class="c" id="c-cache-detail"></div></div>
    </div>
    <div class="panel" style="margin-top:14px">
      <h3>运行控制</h3>
      <div class="row">
        <button class="act" id="ovCognitiveStart">启动认知循环</button>
        <button class="act sec" id="ovCognitiveStop">停止认知循环</button>
        <button class="act sec" id="btnSave">立即保存全部数据</button>
      </div>
      <div class="hint">这里控制全局认知循环；插件服务在“插件”页分别启动/停止。停止认知循环不会断开已运行的插件，但不会产生新的思考与行为。</div>
    </div>
    <div class="panel">
      <h3>最近消息</h3>
      <div id="feed"><div class="hint">暂无消息</div></div>
    </div>
  </section>

  <!-- 插件 -->
  <section class="page" id="page-plugins">
    <div class="panel" id="pluginRunPanel">
      <h3>插件运行</h3>
      <div class="hint">每个插件只有启动/停止一个开关。启动后独立收消息、缓存未读；停止会同时退出软件门控。</div>
      <div id="pluginRunList" style="margin-top:12px"></div>
    </div>
    <div class="panel" id="pluginConfigPanel" style="margin-top:14px">
      <h3>插件配置</h3>
      <div class="hint">点击插件进入它的具体配置。语言模型全局统一；这里只配置插件自己的行为模型。</div>
      <div id="pluginConfigList" style="margin-top:12px"></div>
    </div>
    <div class="panel" id="pluginDetailPanel" style="display:none">
      <div class="row">
        <button class="act sec" id="btnPluginBack">← 返回插件列表</button>
        <h3 id="pluginDetailTitle" style="margin:0"></h3>
      </div>
      <div class="hint" id="pluginDetailDesc"></div>
      <div id="pluginCommonConfig" style="margin-top:12px">
        <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="pd-follow" style="width:auto" onchange="toggleDetailFollow()"> 跟随全局行为模型</label>
        <div class="grid3" style="margin-top:8px">
          <div><label>行为模型 Base URL（不跟随时使用）</label><input id="pd-abase"></div>
          <div><label>行为模型名（不跟随时使用）</label><input id="pd-amodel"></div>
          <div><label>行为 API Key</label><input id="pd-akey" type="password" placeholder="留空不修改"></div>
        </div>
        <div class="hint" style="margin-top:6px">留空字段回落到全局行为模型；全局行为模型未配置时使用主模型。行为模型只做软件动作，不决定是否发送。</div>
        <label style="display:flex;gap:8px;align-items:center;margin-top:8px"><input type="checkbox" id="pd-reasoning" style="width:auto"> 行为模型接收语言层思考过程（思维链）</label>
        <div class="row" style="margin-top:12px"><button class="act" id="btnSavePluginDetail">保存插件配置</button></div>
        <div class="hint">不跟随全局行为模型时，Base URL、模型名和 API Key 为必填；未填写保存会提示。</div>
      </div>
      <div id="pluginSpecificQQ" style="display:none;margin-top:16px">
        <h4>QQ / NapCat</h4>
        <div class="kv" style="margin-top:10px">
          <div class="k">服务状态</div><div id="q-status">未运行</div>
          <div class="k">NapCat 连接</div><div id="q-napcat">未连接</div>
          <div class="k">QQ 身份（自动获取）</div><div id="q-iden">未连接</div>
          <div class="k">软件状态</div><div id="q-app">未打开</div>
          <div class="k">当前对话</div><div id="q-conv">无</div>
        </div>
        <div class="grid2" style="margin-top:12px">
          <div><label>机器人 QQ（连接后自动获取；此处仅作未连接时兜底）</label><input id="q-bot"></div>
          <div><label>主动发言群号</label><input id="q-group"></div>
          <div><label>NapCat 鉴权 Token</label><input id="q-token"></div>
          <div><label>NapCat HTTP 接口地址（正向 HTTP，用于语音下载）</label><input id="q-http" placeholder="http://127.0.0.1:5700"></div>
        </div>
        <div class="row" style="margin-top:12px"><button class="act" id="btnSaveNapcat">保存 QQ 配置</button></div>
        <div class="hint">保存后立即生效。主动发言群号、NapCat Token、HTTP 地址为必填；机器人 QQ 可留空，连接后由 NapCat 自动获取。QQ 昵称与 QQ 号在连接后由 get_login_info 自动获取；意识体名字（{{BOT_NAME}}）只用于记忆与思考。</div>
      </div>
      <div class="hint" id="pluginSpecificHint" style="margin-top:12px"></div>
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
          <option value="plugin">插件</option>
          <option value="ollama">Ollama</option>
          <option value="connection">连接</option>
        </select>
        <select id="logLevel" style="max-width:140px">
          <option value="all">全部等级</option>
          <option value="error">报错</option>
          <option value="warning">警告</option>
          <option value="info">日志</option>
        </select>
        <button class="act sec" id="btnClearLog">清空显示</button>
        <span class="hint">仅保留最近 500 行；网络连接、LLM 请求在“连接”，插件行为在“插件”</span>
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
        <h4>主模型（Primary LLM）— 全局语言模型；所有插件共用的唯一意识声音</h4>
        <div class="grid3">
          <div><label>服务地址 (Base URL)</label><input id="f-pburl" placeholder="https://api.deepseek.com"></div>
          <div><label>模型名称 (Model)</label><input id="f-pmodel" placeholder="deepseek-v4-flash"></div>
          <div><label>API Key（留空表示不修改）</label><input id="f-pkey" type="password" placeholder="已配置则留空"></div>
        </div>
      </div>

      <!-- 全局行为模型区块 -->
      <div class="sub-box">
        <h4>全局行为模型（Global Behavior LLM）— 只决定软件内动作，不参与语言生成</h4>
        <div class="grid3">
          <div><label>服务地址 (Base URL)（留空用主模型）</label><input id="f-bburl" placeholder="https://api.deepseek.com"></div>
          <div><label>模型名称 (Model)（留空用主模型）</label><input id="f-bmodel" placeholder="deepseek-v4-flash"></div>
          <div><label>API Key（留空表示不修改）</label><input id="f-bkey" type="password" placeholder="已配置则留空"></div>
        </div>
        <div class="hint">插件勾选“跟随全局行为模型”时使用这里的配置；未勾选时使用插件自己的行为模型。语言模型永远是上面的主模型。</div>
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
        <h4>向量嵌入模型（Ollama / OpenAI 兼容网络）— 语义记忆向量化与检索</h4>
        <div class="grid3">
          <div><label>接口模式</label>
            <select id="f-oemode">
              <option value="ollama">Ollama 原生</option>
              <option value="openai">OpenAI 兼容网络</option>
            </select>
          </div>
          <div><label>服务地址（Ollama 原生填 http://host:11434；网络接口填 https://host/v1）</label><input id="f-oburl" placeholder="http://127.0.0.1:11434"></div>
          <div><label>嵌入模型名称（Ollama 需已 pull；网络接口填 API 模型名）</label><input id="f-omodel" placeholder="shaw/dmeta-embedding-zh"></div>
          <div><label>API Key（Ollama 可留空；网络接口通常必填）</label><input id="f-okey" type="password" placeholder="留空不修改"></div>
          <div><label>向量维度（需与模型输出一致）</label><input id="f-odim" type="number" min="1" placeholder="768"></div>
        </div>
        <div class="hint">OpenAI 兼容模式会请求“服务地址 + /embeddings”（地址以 /v1 结尾时直接拼接）。更换模型或维度后需重建向量索引；维度不一致会在启动时提示。</div>
      </div>

      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveCfg">保存模型与 API 配置</button></div>
      <div class="hint">红色边框为必填项；未填写时保存会提示。API Key 留空表示保留已配置的值。</div>
      <div class="hint">保存后立即生效：API 客户端与向量模型配置实时热刷新，无需重启面板或容器。</div>
    </div>
    <div class="panel">
      <h3>本地软件门控（打开/退出软件，不参与语言回复）</h3>
      <label style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><input type="checkbox" id="f-ar-follow" style="width:auto" onchange="toggleRouterFollow()"> 跟随全局行为模型</label>
      <div class="hint">勾选后使用“配置”页的全局行为模型（未配置行为模型时使用主模型），下面的门控模型字段会被忽略。</div>
      <div class="grid3">
        <div><label>模式</label>
          <select id="f-ar-mode">
            <option value="rule">规则</option>
            <option value="local_llm">LLM 门控（本地/网络）</option>
            <option value="off">关闭自动开关</option>
          </select>
        </div>
        <div><label>门控模型 Base URL（OpenAI 兼容：本地 Ollama 或网络模型）</label><input id="f-ar-base" placeholder="http://127.0.0.1:11434/v1"></div>
        <div><label>门控模型名（local_llm 模式必填）</label><input id="f-ar-model" placeholder="如 qwen2.5:3b / deepseek-chat"></div>
        <div><label>门控模型 API Key（本地 Ollama 可填 ollama；网络模型填真实 Key）</label><input id="f-ar-key" type="password" placeholder="留空不修改"></div>
      </div>
      <div class="grid3" style="margin-top:8px">
        <div><label>未读持续几轮后打开软件</label><input id="f-ar-open" type="number" min="1"></div>
        <div><label>对话空闲几轮后退出对话</label><input id="f-ar-closeconv" type="number" min="1"></div>
        <div><label>软件空闲几轮后退出软件</label><input id="f-ar-closeapp" type="number" min="1"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveRouter">保存软件门控</button></div>
      <div class="hint">规则模式不需要额外模型；local_llm 可使用本地 Ollama 或任意 OpenAI 兼容网络模型，只决定 open_app / close_app / close_conversation / none，不参与语言回复。</div>
    </div>
    <div class="panel">
      <h3>浏览器插件（网页解析）</h3>
      <div class="grid3">
        <div><label>解析模式</label>
          <select id="f-br-mode">
            <option value="static">静态：本地抓取 + 本地解析</option>
            <option value="dynamic">动态：调用网页解析接口</option>
          </select>
        </div>
        <div><label>动态解析接口（DeepSeek Fetch 形态）</label><input id="f-br-durl" placeholder="https://api.deepseek.com/v1/tools/fetch"></div>
        <div><label>动态解析 API Key（留空用主模型 Key）</label><input id="f-br-dkey" type="password" placeholder="留空不修改"></div>
        <div><label>摘要 LLM Base URL（留空用主模型）</label><input id="f-br-surl" placeholder="留空用主模型"></div>
        <div><label>摘要模型名（留空用主模型）</label><input id="f-br-smode" placeholder="留空用主模型"></div>
        <div><label>摘要 API Key（留空用主模型 Key）</label><input id="f-br-skey" type="password" placeholder="留空不修改"></div>
      </div>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSaveBrowser">保存浏览器配置</button></div>
      <div class="hint">静态模式直接访问网页并用本地 HTMLParser 提取正文；动态模式把 URL 交给网页解析接口，拿到 Markdown 后再概括。两种模式都会先概括，再写入短期记忆。</div>
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
      <h3>伪造思考</h3>
      <div class="hint">模拟一次内部思考：使用全局语言模型与近期想法，不向任何插件发送消息，只写入“我想”和历史。</div>
      <textarea id="m-think" placeholder="输入要让她思考的内容"></textarea>
      <div class="row" style="margin-top:8px"><button class="act" id="btnFakeThink">伪造思考</button></div>
    </div>
    <div class="panel">
      <h3>管理员消息注入</h3>
      <div class="hint">写入记忆图，并加入下一轮认知检索的关键词信箱；不会冒充她自己的发言。</div>
      <textarea id="m-inject" placeholder="输入要注入的消息/记忆（第一人称）"></textarea>
      <div class="row" style="margin-top:8px"><button class="act" id="btnInject">注入并保存</button></div>
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
let hasPrimaryKey = false;
let hasEmbeddingKey = false;
let hasRouterKey = false;
function ensureRequired(items, silent=false){
  const missing = [];
  let first = null;
  for(const [id, label] of items){
    const el = $(id);
    if(!el) continue;
    const empty = !String(el.value || '').trim();
    el.classList.toggle('missing', empty);
    if(empty){
      missing.push(label);
      if(!first) first = el;
    }
  }
  if(missing.length){
    if(!silent){
      toast('请先填写：' + missing.join('、'));
      if(first) first.focus();
    }
    return false;
  }
  return true;
}
function clearMissing(ids){
  for(const id of ids){
    const el = $(id);
    if(el) el.classList.remove('missing');
  }
}
function toggleRouterFollow(){
  const follow = !!$('f-ar-follow').checked;
  for(const id of ['f-ar-base', 'f-ar-model', 'f-ar-key']){
    const el = $(id);
    if(!el) continue;
    el.disabled = follow;
    if(follow) el.classList.remove('missing');
  }
}
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
      const stateText = d.state === 'asleep' ? '睡眠中'
        : d.state === 'wind_down' ? '准备睡'
        : (d.state === 'waking' ? '刚醒' : '清醒');
      $('c-energy').textContent = pct + '% ' + stateText;
      let cap = '作息积累中';
      if(d.rhythm_nights > 0){
        const h = d.sleep_hours || [];
        cap = `作息${(d.circadian??0).toFixed(2)} ${d.rhythm_nights}晚` +
              (h.length ? ` 常睡${Math.min(...h)}-${Math.max(...h)}点` : '');
      }
      $('c-rhythm').textContent = cap;
      // 缓存指标：文本（LLM token）缓存命中在前，图片描述缓存在后
      try{
        const probe = d.probe || {};
        const c = probe.counters || {};
        const visionCalls = c['vision.calls'] || 0;
        const visionHits = c['vision.cache_hit'] || 0;
        // 文本只统计对话调用；视觉调用自己的 token 缓存不计入文本
        const sumTokens = suffix => Object.keys(c)
          .filter(k => k.startsWith('tokens.') && k.endsWith(suffix) && !k.startsWith('tokens.vision.'))
          .reduce((a, k) => a + c[k], 0);
        const hitTokens = sumTokens('.cache_hit');
        const missTokens = sumTokens('.cache_miss');
        const tokenTotal = hitTokens + missTokens;
        const visionTotal = visionCalls + visionHits;

        const parts = [];
        let pctText = '--';
        if(tokenTotal > 0){
          pctText = Math.round(100 * hitTokens / tokenTotal) + '%';
          parts.push(`文本 ${hitTokens}/${tokenTotal}`);
        }else{
          parts.push('文本 暂无数据');
        }
        if(visionTotal > 0){
          parts.push(`视觉 ${Math.round(100 * visionHits / visionTotal)}% (${visionHits}/${visionTotal})`);
        }
        $('c-cache').textContent = pctText;
        $('c-cache-detail').textContent = parts.join(' · ') || '暂无数据';
      }catch(e){}
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
    const cb = $('cognitiveBadge');
    const cogRunning = !!d.cognitive_running;
    cb.textContent = '认知循环：' + (cogRunning ? '运行中' : '已停止');
    cb.className = 'badge' + (cogRunning ? '' : ' off');
    if($('q-status')){
      $('q-status').textContent = d.qq_running ? '运行中' : '未运行';
      $('q-napcat').textContent = d.napcat_connected ? '已连接' : (d.qq_running ? '等待 NapCat 连入…' : '未连接');
      const ident = d.qq_identity || {};
      if(ident.nickname){
        $('q-iden').textContent = `${ident.nickname}（${ident.user_id || '?'}，${ident.source === 'napcat' ? 'NapCat 自动获取' : '兜底'}）`;
      }else if(ident.user_id){
        $('q-iden').textContent = `${ident.user_id}（未连接，使用配置兜底）`;
      }else{
        $('q-iden').textContent = '未获取';
      }
      const qapp = d.qq_app || {};
      $('q-app').textContent = qapp.open
        ? `已打开${qapp.unread ? `，另有 ${qapp.unread} 条未读` : ''}`
        : `未打开${qapp.unread ? `，${qapp.unread} 条未读` : ''}`;
      $('q-conv').textContent = qapp.current_conversation || '无';
    }
  }catch(e){ /* 面板不因统计失败而中断 */ }
  finally{ statsInFlight = false; }
}
async function loadConfig(){
  try{
    const c = await getJSON('/api/config');
    hasPrimaryKey = !!c._has_primary_key;
    hasEmbeddingKey = !!c._has_embedding_key;
    hasRouterKey = !!c._has_app_router_key;
    $('f-pkey').placeholder = hasPrimaryKey ? '已配置，留空不修改' : '必填';
    $('f-okey').placeholder = hasEmbeddingKey ? '已配置，留空不修改' : '网络接口通常必填';
    $('f-ar-key').placeholder = hasRouterKey ? '已配置，留空不修改' : '必填';
    $('f-pburl').value = c.primary_base_url || '';
    $('f-pmodel').value = c.primary_model || '';
    $('f-bburl').value = c.behavior_base_url || '';
    $('f-bmodel').value = c.behavior_model || '';
    $('f-sburl').value = c.secondary_base_url || '';
    $('f-smode').value = c.secondary_model || '';
    $('f-oemode').value = c.embedding_api_mode || 'ollama';
    $('f-odim').value = c.embedding_dim ?? 768;
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
    $('f-ar-mode').value = c.app_router_mode || 'rule';
    $('f-ar-follow').checked = !!c.app_router_follow_global_behavior;
    $('f-ar-base').value = c.app_router_base_url || '';
    $('f-ar-model').value = c.app_router_model || '';
    toggleRouterFollow();
    $('f-ar-open').value = c.app_open_after_unread_rounds ?? 2;
    $('f-ar-closeconv').value = c.app_close_conversation_idle_rounds ?? 15;
    $('f-ar-closeapp').value = c.app_close_idle_rounds ?? 30;
    $('f-br-mode').value = c.browser_mode || 'static';
    $('f-br-durl').value = c.browser_dynamic_url || '';
    $('f-br-surl').value = c.browser_summary_base_url || '';
    $('f-br-smode').value = c.browser_summary_model || '';
    markLoadedRequired();
  }catch(e){}
}
function markLoadedRequired(){
  const primary = [
    ['f-pburl', '主模型 Base URL'],
    ['f-pmodel', '主模型名称'],
    ['f-oburl', 'Embedding 服务地址'],
    ['f-omodel', 'Embedding 模型名称'],
    ['f-odim', '向量维度'],
  ];
  if(!hasPrimaryKey) primary.push(['f-pkey', '主模型 API Key']);
  if($('f-oemode').value === 'openai' && !hasEmbeddingKey && !$('f-okey').value.trim()){
    primary.push(['f-okey', 'Embedding API Key']);
  }
  ensureRequired(primary, true);
  if(hasPrimaryKey) clearMissing(['f-pkey']);
  if($('f-oemode').value !== 'openai' || hasEmbeddingKey) clearMissing(['f-okey']);
  ensureRequired([
    ['q-group', '主动发言群号'],
    ['q-token', 'NapCat 鉴权 Token'],
    ['q-http', 'NapCat HTTP 接口地址'],
  ], true);
  if($('f-ar-mode').value === 'local_llm' && !$('f-ar-follow').checked){
    const required = [
      ['f-ar-base', '门控模型 Base URL'],
      ['f-ar-model', '门控模型名'],
    ];
    if(!hasRouterKey) required.push(['f-ar-key', '门控模型 API Key']);
    ensureRequired(required, true);
  }else{
    clearMissing(['f-ar-base', 'f-ar-model', 'f-ar-key']);
  }
  if($('f-br-mode').value === 'dynamic'){
    ensureRequired([['f-br-durl', '动态解析接口']], true);
  }else{
    clearMissing(['f-br-durl', 'f-br-dkey']);
  }
  const bb = $('f-bburl').value.trim();
  const bm = $('f-bmodel').value.trim();
  $('f-bburl').classList.toggle('missing', !bb && !!bm);
  $('f-bmodel').classList.toggle('missing', !!bb && !bm);
}

// ---------- 插件配置 ----------
const CAP_LABELS = {
  chat:'文本', group_chat:'群聊', direct_chat:'私聊', media:'媒体',
  page_read:'网页', mini_program:'小程序', account:'账号',
  feed:'信息流', search:'搜索', proactive:'主动发言'
};
let pluginCache = [];
let currentPluginDetailId = null;
let currentPluginHasKey = false;
const PLUGIN_EXTRA_HINTS = {
  wechat: '微信桥接地址、Token 与会话白名单在“配置”页和 config/wechat_manifest.json；桥接协议见 plugins/wechat/BRIDGE.md。',
  bilibili: 'Bilibili Cookie 在 config/bilibili_credentials.json；推荐、搜索、评论等参数在“配置”页。',
  browser: '网页解析模式（static/dynamic）、DeepSeek Fetch 地址与摘要模型在“配置”页的浏览器插件区域。',
  web: '网页/小程序读取接口尚未实现。',
};
function escHtml(value){
  return String(value ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}
function pluginRunning(p){
  return p && p.running !== undefined ? !!p.running : !!(p && p.enabled);
}
async function loadPlugins(){
  try{
    const d = await getJSON('/api/plugins');
    pluginCache = d.plugins || [];
    const runList = $('pluginRunList');
    const cfgList = $('pluginConfigList');
    runList.innerHTML = '';
    cfgList.innerHTML = '';
    for(const p of pluginCache){
      const running = pluginRunning(p);
      const caps = (p.capabilities || []).map(c => CAP_LABELS[c] || c).join(' · ');
      const runBox = document.createElement('div');
      runBox.className = 'sub-box';
      runBox.innerHTML =
        `<div class="row" style="justify-content:space-between;align-items:center;gap:12px">` +
          `<div><b>${escHtml(p.label)}</b> <span class="hint">${escHtml(p.id)} · ${p.is_app ? '软件型' : '工具型'} · ${running ? '运行中' : '已停止'}</span>` +
          `<div class="hint">${escHtml(p.description || '')}${caps ? ' · 能力：' + escHtml(caps) : ''}</div></div>` +
          `<button class="act ${running ? 'sec' : ''}" ${p.implemented ? '' : 'disabled'} onclick="pluginAction('${escHtml(p.id)}','${running ? 'stop' : 'start'}')">${running ? '停止' : '启动'}</button>` +
        `</div>`;
      runList.appendChild(runBox);

      const cfgBtn = document.createElement('button');
      cfgBtn.className = 'act sec';
      cfgBtn.style.cssText = 'width:100%;text-align:left;margin-bottom:8px';
      cfgBtn.innerHTML = `${escHtml(p.label)} <span class="hint">· 点击进入配置${p.implemented ? '' : '（未实现）'}</span>`;
      cfgBtn.onclick = () => openPluginDetail(p.id);
      cfgList.appendChild(cfgBtn);
    }
    if(!pluginCache.length){
      runList.innerHTML = '<div class="hint">未发现插件</div>';
      cfgList.innerHTML = '<div class="hint">未发现插件</div>';
    }
  }catch(e){ toast('插件配置读取失败：' + e.message); }
}
async function pluginAction(id, action){
  if(action === 'start' && id === 'qq_napcat'){
    const required = [
      ['q-group', '主动发言群号'],
      ['q-token', 'NapCat 鉴权 Token'],
      ['q-http', 'NapCat HTTP 接口地址'],
    ];
    if(!ensureRequired(required, true)){
      openPluginDetail('qq_napcat');
      ensureRequired(required);
      return;
    }
  }
  try{
    await post('/api/plugins/' + action, {id});
    toast((action === 'start' ? '插件已启动' : '插件已停止'));
    await loadPlugins();
    if(currentPluginDetailId) openPluginDetail(currentPluginDetailId);
  }catch(e){ toast('操作失败：' + e.message); }
}
function toggleDetailFollow(){
  const disabled = !!$('pd-follow').checked;
  for(const id of ['pd-abase', 'pd-amodel', 'pd-akey']){
    $(id).disabled = disabled;
    if(disabled) $(id).classList.remove('missing');
  }
}
function openPluginDetail(id){
  const p = pluginCache.find(x => x.id === id);
  if(!p) return;
  currentPluginDetailId = id;
  $('pluginRunPanel').style.display = 'none';
  $('pluginConfigPanel').style.display = 'none';
  $('pluginDetailPanel').style.display = '';
  $('pluginDetailTitle').textContent = p.label;
  $('pluginDetailDesc').textContent = (p.description || '') + (p.implemented ? '' : '（未实现）');
  const al = p.action_llm || {};
  currentPluginHasKey = !!al._has_api_key;
  $('pd-follow').checked = p.follow_global_behavior !== false;
  $('pd-abase').value = al.base_url || '';
  $('pd-amodel').value = al.model || '';
  $('pd-akey').value = '';
  $('pd-akey').placeholder = currentPluginHasKey ? '已配置，留空不修改' : '必填（不跟随时）';
  $('pd-reasoning').checked = !!p.action_include_reasoning;
  toggleDetailFollow();
  if(!$('pd-follow').checked){
    const required = [
      ['pd-abase', '行为模型 Base URL'],
      ['pd-amodel', '行为模型名'],
    ];
    if(!currentPluginHasKey) required.push(['pd-akey', '行为 API Key']);
    ensureRequired(required, true);
  }
  const isQQ = p.id === 'qq_napcat';
  $('pluginSpecificQQ').style.display = isQQ ? '' : 'none';
  $('pluginSpecificHint').textContent = isQQ ? '' : (PLUGIN_EXTRA_HINTS[p.id] || '该插件暂无额外配置。');
}
function closePluginDetail(){
  currentPluginDetailId = null;
  $('pluginDetailPanel').style.display = 'none';
  $('pluginRunPanel').style.display = '';
  $('pluginConfigPanel').style.display = '';
}
async function savePluginDetail(){
  const id = currentPluginDetailId;
  if(!id) return;
  if(!$('pd-follow').checked){
    const required = [
      ['pd-abase', '行为模型 Base URL'],
      ['pd-amodel', '行为模型名'],
    ];
    if(!currentPluginHasKey && !$('pd-akey').value.trim()){
      required.push(['pd-akey', '行为 API Key']);
    }
    if(!ensureRequired(required)) return;
    clearMissing(['pd-akey']);
  }else{
    clearMissing(['pd-abase', 'pd-amodel', 'pd-akey']);
  }
  const body = {plugins: {}};
  body.plugins[id] = {
    follow_global_behavior: $('pd-follow').checked,
    action_include_reasoning: $('pd-reasoning').checked,
    action_llm: {
      base_url: $('pd-abase').value.trim(),
      model: $('pd-amodel').value.trim(),
      api_key: $('pd-akey').value.trim(),
    },
  };
  try{
    await post('/api/plugins', body);
    toast('插件配置已保存并热加载');
    await loadPlugins();
    openPluginDetail(id);
  }catch(e){ toast('保存失败：' + e.message); }
}

// 日志：环形缓冲。
// 每条日志进入环形缓冲后，按当前分类/等级筛选重建视图；上限 MAX_LOG_LINES。
const logs = $('logs');
const LOG_CATS = ['runtime', 'thinking', 'plugin', 'ollama', 'connection'];
const LOG_LEVEL_LABELS = { error: '报错', warning: '警告', info: '日志' };
const logRing = [];               // 环形缓冲：{cat, line, level, cls}[]
function normalizeLogLevel(level, line){
  if(level === 'error' || level === 'warning' || level === 'info') return level;
  if(/ERROR|失败|异常|错误|Traceback/i.test(line)) return 'error';
  if(/WARNING|警告|超时|未找到/i.test(line)) return 'warning';
  return 'info';
}
function logMatches(entry){
  const cat = $('logCat').value;
  const level = $('logLevel').value;
  if(cat !== 'all' && cat !== entry.cat) return false;
  if(level !== 'all' && level !== entry.level) return false;
  return true;
}
function pushLog(cat, line, level){
  const lvl = normalizeLogLevel(level, line);
  const cls = lvl === 'error' ? 'e' : (lvl === 'warning' ? 'w' : 't');
  const entry = {cat, line, level: lvl, cls};
  logRing.push(entry);
  if(logRing.length > MAX_LOG_LINES) logRing.shift();
  if(!logMatches(entry)) return;
  appendLogNode(entry);
  while(logs.childNodes.length > MAX_LOG_LINES) logs.removeChild(logs.firstChild);
  logs.scrollTop = logs.scrollHeight;
}
function appendLogNode(entry){
  const div = document.createElement('div');
  div.className = entry.cls || 't';
  div.textContent = `[${LOG_LEVEL_LABELS[entry.level] || '日志'}] ${entry.line}`;
  logs.appendChild(div);
}
function rebuildLogView(){
  logs.innerHTML = '';
  for(const entry of logRing){
    if(logMatches(entry)) appendLogNode(entry);
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
    if(m.type === 'log') pushLog(m.cat, m.line, m.level);
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
  if(!confirm('确定要关闭面板吗？\n将停止认知循环与全部插件服务、保存全部数据并退出。')) return;
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
$('ovCognitiveStart').onclick = async () => {
  toast('认知循环启动中…');
  try{ await post('/api/cognitive/start'); toast('认知循环已启动'); refreshStats(); }
  catch(e){ toast('启动失败：'+e.message); }
};
$('ovCognitiveStop').onclick = async () => {
  toast('正在停止认知循环…');
  try{ await post('/api/cognitive/stop'); toast('认知循环已停止'); refreshStats(); }
  catch(e){ toast('停止失败：'+e.message); }
};
$('btnPluginBack').onclick = () => closePluginDetail();
$('btnSavePluginDetail').onclick = () => savePluginDetail();
$('btnClearLog').onclick = () => { logs.innerHTML = ''; logRing.length = 0; };
$('logCat').onchange = () => { rebuildLogView(); };
$('logLevel').onchange = () => { rebuildLogView(); };
$('btnSaveCfg').onclick = async () => {
  const required = [
    ['f-pburl', '主模型 Base URL'],
    ['f-pmodel', '主模型名称'],
    ['f-oburl', 'Embedding 服务地址'],
    ['f-omodel', 'Embedding 模型名称'],
    ['f-odim', '向量维度'],
  ];
  if(!hasPrimaryKey && !$('f-pkey').value.trim()){
    required.push(['f-pkey', '主模型 API Key']);
  }
  if($('f-oemode').value === 'openai' && !hasEmbeddingKey && !$('f-okey').value.trim()){
    required.push(['f-okey', 'Embedding API Key']);
  }
  if(!ensureRequired(required)) return;
  clearMissing(['f-pkey']);
  if($('f-oemode').value !== 'openai' || hasEmbeddingKey) clearMissing(['f-okey']);
  const behaviorBase = $('f-bburl').value.trim();
  const behaviorModel = $('f-bmodel').value.trim();
  if((behaviorBase && !behaviorModel) || (!behaviorBase && behaviorModel)){
    toast('全局行为模型的 Base URL 与模型名需要同时填写');
    $('f-bburl').classList.toggle('missing', !behaviorBase);
    $('f-bmodel').classList.toggle('missing', !behaviorModel);
    return;
  }
  clearMissing(['f-bburl', 'f-bmodel']);
  try{
    await post('/api/config', {
      primary_base_url:$('f-pburl').value.trim(),
      primary_model:$('f-pmodel').value.trim(),
      primary_api_key:$('f-pkey').value.trim(),
      behavior_base_url:$('f-bburl').value.trim(),
      behavior_model:$('f-bmodel').value.trim(),
      behavior_api_key:$('f-bkey').value.trim(),
      secondary_base_url:$('f-sburl').value.trim(),
      secondary_model:$('f-smode').value.trim(),
      secondary_api_key:$('f-skey').value.trim(),
      ollama_base_url:$('f-oburl').value.trim(),
      ollama_embed_model:$('f-omodel').value.trim(),
      embedding_api_mode:$('f-oemode').value,
      embedding_api_key:$('f-okey').value.trim(),
      embedding_dim:parseInt($('f-odim').value) || 768,
    });
    if($('f-pkey').value.trim()) hasPrimaryKey = true;
    if($('f-okey').value.trim()) hasEmbeddingKey = true;
    $('f-pkey').value = ''; $('f-bkey').value = ''; $('f-skey').value = ''; $('f-okey').value = '';
    $('f-pkey').placeholder = hasPrimaryKey ? '已配置，留空不修改' : '必填';
    $('f-okey').placeholder = hasEmbeddingKey ? '已配置，留空不修改' : '网络接口通常必填';
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
  if(!ensureRequired([
    ['q-group', '主动发言群号'],
    ['q-token', 'NapCat 鉴权 Token'],
    ['q-http', 'NapCat HTTP 接口地址'],
  ])) return;
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
$('btnSaveRouter').onclick = async () => {
  const num = (id) => { const v = parseInt($(id).value); return isFinite(v) ? v : undefined; };
  if($('f-ar-mode').value === 'local_llm' && !$('f-ar-follow').checked){
    const required = [
      ['f-ar-base', '门控模型 Base URL'],
      ['f-ar-model', '门控模型名'],
    ];
    if(!hasRouterKey && !$('f-ar-key').value.trim()){
      required.push(['f-ar-key', '门控模型 API Key']);
    }
    if(!ensureRequired(required)) return;
    clearMissing(['f-ar-key']);
  }else{
    clearMissing(['f-ar-base', 'f-ar-model', 'f-ar-key']);
  }
  try{
    await post('/api/config', {
      app_router_mode: $('f-ar-mode').value,
      app_router_follow_global_behavior: $('f-ar-follow').checked,
      app_router_base_url: $('f-ar-base').value.trim(),
      app_router_model: $('f-ar-model').value.trim(),
      app_router_api_key: $('f-ar-key').value.trim(),
      app_open_after_unread_rounds: num('f-ar-open'),
      app_close_conversation_idle_rounds: num('f-ar-closeconv'),
      app_close_idle_rounds: num('f-ar-closeapp'),
    });
    if($('f-ar-key').value.trim()) hasRouterKey = true;
    $('f-ar-key').value = '';
    $('f-ar-key').placeholder = hasRouterKey ? '已配置，留空不修改' : '必填';
    toast('软件门控配置已保存并热加载');
  }catch(e){ toast('保存失败：'+e.message); }
};
$('btnSaveBrowser').onclick = async () => {
  if($('f-br-mode').value === 'dynamic'){
    const required = [['f-br-durl', '动态解析接口']];
    if(!hasPrimaryKey && !$('f-br-dkey').value.trim()){
      required.push(['f-br-dkey', '动态解析 API Key']);
    }
    if(!ensureRequired(required)) return;
    clearMissing(['f-br-dkey']);
  }else{
    clearMissing(['f-br-durl', 'f-br-dkey']);
  }
  try{
    await post('/api/config', {
      browser_mode: $('f-br-mode').value,
      browser_dynamic_url: $('f-br-durl').value.trim(),
      browser_dynamic_api_key: $('f-br-dkey').value.trim(),
      browser_summary_base_url: $('f-br-surl').value.trim(),
      browser_summary_model: $('f-br-smode').value.trim(),
      browser_summary_api_key: $('f-br-skey').value.trim(),
    });
    $('f-br-dkey').value = ''; $('f-br-skey').value = '';
    toast('浏览器配置已保存并热加载');
  }catch(e){ toast('保存失败：'+e.message); }
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
loadPlugins();
setInterval(refreshStats, 500);
</script>
</body>
</html>
"""
