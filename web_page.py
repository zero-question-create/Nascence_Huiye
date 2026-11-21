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
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<link rel="shortcut icon" href="/favicon.ico" type="image/x-icon">
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
  .field-head{display:flex;align-items:center;gap:6px}
  .field-head label{margin:8px 0 4px}
  .help-dot{width:17px;height:17px;border-radius:50%;border:1px solid #35445a;background:#0f1621;
            color:var(--muted);font-size:11px;line-height:15px;text-align:center;cursor:pointer;
            padding:0;flex:none;margin-top:4px}
  .help-dot:hover,.help-dot:focus{color:#fff;border-color:var(--accent2);outline:none}
  .help-dot.open{color:#fff;background:#1b2b45;border-color:var(--accent2)}
  .help-pop{display:none;background:#0b1220;border:1px solid #35445a;border-radius:8px;
            padding:8px 10px;margin:2px 0 4px;color:var(--text);font-size:12px;line-height:1.55}
  .help-pop.open{display:block}
  .kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:13px}
  .kv .k{color:var(--muted)}
  #toast{position:fixed;right:20px;bottom:20px;background:#1b2b45;border:1px solid var(--border);
         padding:10px 16px;border-radius:8px;display:none;max-width:60vw}
  /* ===== 插件社区：对齐 Nascence_Huiye_Server 的卡片/列表/Markdown 语言 ===== */
  .community-toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  .community-toolbar input{flex:1;min-width:220px}
  .community-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .community-card{background:#0f1621;border:1px solid #1f2a3a;border-radius:8px;padding:12px 14px;
                  cursor:pointer;transition:border-color .18s,background .18s;display:flex;flex-direction:column;gap:6px}
  .community-card:hover{border-color:var(--accent2);background:#121c2a}
  .community-card.active{border-color:var(--accent2);background:#17263a}
  .community-card-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
  .community-name{font-weight:800;font-size:14px}
  .community-meta{font-size:11.5px;color:var(--muted);word-break:break-all}
  .community-desc{font-size:12.5px;color:var(--text);opacity:.9;line-height:1.55}
  .community-badges{display:flex;gap:6px;flex-wrap:wrap}
  .tag{font-size:11px;padding:1px 7px;border:1px solid #35445a;border-radius:999px;color:var(--muted)}
  .tag.ok{color:var(--ok);border-color:#286653;background:#183a31}
  .tag.warn{color:var(--warn);border-color:#655a32;background:#3a3320}
  .tag.err{color:var(--err);border-color:#653232;background:#3a1e1e}
  .community-detail{display:none;margin-top:14px}
  .community-detail.open{display:block}
  .community-detail-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
  .community-actions{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
  .md-preview{background:#0b1220;border:1px solid #2a3850;border-radius:8px;padding:14px 16px;
              max-height:60vh;overflow:auto}
  .markdown-body{font-size:13.5px;line-height:1.75;color:var(--text);word-break:break-word}
  .markdown-body h1,.markdown-body h2,.markdown-body h3,.markdown-body h4{margin:14px 0 8px;font-weight:800;line-height:1.35}
  .markdown-body h1{font-size:19px}.markdown-body h2{font-size:17px}.markdown-body h3{font-size:15px}
  .markdown-body p{margin:8px 0}
  .markdown-body ul,.markdown-body ol{margin:8px 0;padding-left:22px}
  .markdown-body blockquote{margin:10px 0;padding:6px 12px;border-left:3px solid var(--accent);background:#101722;color:var(--muted)}
  .markdown-body code{padding:1px 5px;font-size:12.5px;background:#101722;border:1px solid #2a3850;border-radius:4px;font-family:Consolas,Monaco,monospace}
  .markdown-body pre{margin:10px 0;padding:10px 12px;overflow-x:auto;background:#101722;border:1px solid #2a3850;border-radius:8px}
  .markdown-body pre code{border:0;padding:0;background:transparent}
  .markdown-body a{color:var(--accent2)}
  .markdown-body table{border-collapse:collapse;margin:10px 0;width:100%}
  .markdown-body th,.markdown-body td{border:1px solid #2a3850;padding:6px 10px;text-align:left}
  .markdown-body th{background:#101722;font-weight:700}
  .markdown-body img{max-width:100%;height:auto;border-radius:6px}
  .import-box{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}
  .plugin-meta{display:grid;grid-template-columns:100px 1fr;gap:5px 10px;font-size:12.5px;margin:8px 0}
  .plugin-meta .k{color:var(--muted)}
  @media(max-width:760px){.import-box{grid-template-columns:1fr}}

  /* ===== 视觉基准：仿 Nascence_Huiye_Server 控制面板（顶栏 + 标签导航 + 卡片）===== */
  :root{--surface:#141d2a;--surface-2:#0f1621;--menu-bg:#141d2a;--accent-text:#fff;}
  body.light-mode{
    --bg:#f2f2f3;--panel:#ffffff;--border:#e6e6e6;--text:#1a1a1a;--muted:#8a8a8a;
    --accent:#2d6cdf;--accent2:#2d6cdf;--ok:#17a673;--warn:#c07f00;--err:#d64545;
    --surface:#ffffff;--surface-2:#f5f5f6;--menu-bg:#ffffff;--accent-text:#ffffff;
    background:var(--bg);color:var(--text);
  }
  #topbar{position:sticky;top:0;z-index:60;height:56px;padding:0 18px;display:flex;
           align-items:center;gap:12px;background:var(--menu-bg);
           border-bottom:1px solid var(--border);flex-wrap:nowrap}
  .topbar-left{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
  .topbar-center{display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .topbar-right{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex:1;min-width:0}
  .brand-logo{width:28px;height:28px;flex:none;display:flex;align-items:center;justify-content:center;
              background:linear-gradient(135deg,#1c1c1c 0%,#8a8a8a 55%,#ffffff 100%);
              color:#fff;font-size:16px;text-shadow:0 0 3px rgba(0,0,0,.5)}
  .brand-text{min-width:0}
  .brand-title{font-size:15px;font-weight:800;letter-spacing:.04em;white-space:nowrap;
               color:var(--text);line-height:1.2}
  .brand-sub{font-size:10px;color:var(--muted);letter-spacing:.1em;line-height:1.2}
  #nav{display:flex;align-items:center;gap:4px;padding:0;background:transparent}
  #nav button{position:relative;border:1px solid transparent;background:transparent;color:var(--muted);
              padding:7px 16px;font-size:14px;cursor:pointer;border-radius:0;font-family:inherit;
              transition:color .18s,background .18s,border-color .18s;white-space:nowrap}
  #nav button:hover{color:var(--text);background:var(--surface-2)}
  #nav button.active{color:var(--text);font-weight:700;background:var(--surface-2);
                     border-color:var(--border)}
  #nav button.active::after{content:"";position:absolute;left:12px;right:12px;bottom:2px;height:2px;
                            background:var(--accent)}
  #topbar .badge{border-radius:0;padding:3px 9px;font-size:11.5px;background:transparent;
                 white-space:nowrap;flex:none}
  #topbar .badge.off{background:var(--surface-2);color:var(--muted);border-color:var(--border)}
  #topbar .badge.wait{background:transparent;color:var(--warn);border-color:var(--warn)}
  #topbar .theme-toggle,#topbar #restartBtn,#topbar #shutdownBtn{
    border:1px solid var(--border);background:transparent;color:var(--muted);padding:6px 12px;
    font-size:12px;cursor:pointer;border-radius:0;font-family:inherit;white-space:nowrap;margin:0}
  #topbar .theme-toggle:hover,#topbar #restartBtn:hover{color:var(--accent);border-color:var(--accent)}
  #topbar #shutdownBtn{color:var(--danger,#d64545);border-color:var(--border)}
  #topbar #shutdownBtn:hover{color:#fff;background:#d64545;border-color:#d64545}
  main{padding:18px 24px 40px}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:0;
         padding:16px 18px;position:relative}
  .panel::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;
                 background:linear-gradient(90deg,#1c1c1c 0%,#8a8a8a 55%,#fff 100%)}
  body.light-mode .panel::before{background:linear-gradient(90deg,#2d6cdf 0%,#9bbcf5 55%,#ffffff 100%)}
  .panel h3{font-size:15px;font-weight:800;letter-spacing:.02em}
  .cards{gap:12px}
  .card,.sub-box,.community-card,.md-preview,#logs,.msg,.help-pop,#toast{
    background:var(--surface-2);border-color:var(--border);border-radius:0;color:var(--text)}
  .card{padding:12px 14px}
  .card .k,.hint,.muted,.card .c,.community-meta,.plugin-meta .k{color:var(--muted)}
  .card .v{color:var(--accent2)}
  button.act{border-radius:0;background:var(--accent);color:var(--accent-text);font-weight:700}
  button.act.sec{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
  input,textarea,select{border-radius:0;background:var(--surface-2);border-color:var(--border);color:var(--text)}
  body.light-mode input,body.light-mode textarea,body.light-mode select{background:#fff}
  .badge{border-radius:0}
  .community-card{border-radius:0}
  .community-detail-badge,.tag{border-radius:0}
  /* 浅色模式悬停必须保持深色文字，避免“文字=背景”不可读 */
  body.light-mode .community-card:hover,
  body.light-mode .community-card.active{background:#eef4ff;color:#1a1a1a;border-color:var(--accent)}
  body.light-mode .community-card:hover .community-desc,
  body.light-mode .community-card:hover .community-meta{color:#1a1a1a}
  body.light-mode .help-dot:hover,body.light-mode .help-dot:focus{color:var(--accent);background:#eef4ff}
  body.light-mode .tag.ok{color:#0b6b49;background:#e6f7ef;border-color:#8fd3b8}
  body.light-mode .tag.warn{color:#8a5a00;background:#fff4dc;border-color:#e2b877}
  /* Markdown 的 code/pre 使用深色底，浅色模式必须同步改文字色，否则黑底黑字 */
  body.light-mode .markdown-body code{background:#eef1f5;color:#1a1a1a;border-color:#d9dee7}
  body.light-mode .markdown-body pre{background:#f5f5f6;color:#1a1a1a;border-color:#e6e6e6}
  body.light-mode .markdown-body pre code{background:transparent;color:inherit;border:0}
  body.light-mode .markdown-body blockquote{background:#f5f5f6;color:#555;border-left-color:var(--accent)}
  body.light-mode .markdown-body a{color:#2d6cdf}
  body.light-mode .markdown-body th{background:#f5f5f6;color:#1a1a1a}
  body.light-mode .markdown-body td{color:#1a1a1a}

  /* NHYS（Nascence_Huiye_Server）风格动效：由控制面板设置开关控制 */
  @keyframes nhys-page-in{from{opacity:0;transform:translateY(-10px) scale(.98)}to{opacity:1;transform:none}}
  @keyframes nhys-card-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  @keyframes nhys-spin{to{transform:rotate(360deg)}}
  body.nhys-motion .page.active{animation:nhys-page-in .35s cubic-bezier(.34,1.56,.64,1) both}
  body.nhys-motion .page.active .panel{animation:nhys-card-in .28s ease both}
  body.nhys-motion .panel{transition:border-color .18s,box-shadow .18s}
  body.nhys-motion .panel:hover{box-shadow:0 6px 20px rgba(0,0,0,.08)}
  body.nhys-motion #nav button,body.nhys-motion button.act,body.nhys-motion .community-card,
  body.nhys-motion .tag,body.nhys-motion #topbar .theme-toggle{transition:color .18s,background .18s,border-color .18s,transform .18s,box-shadow .18s}
  body.nhys-motion .community-card:hover{transform:translateY(-2px);box-shadow:0 6px 18px rgba(0,0,0,.12)}
  body.nhys-motion button.act:hover{filter:brightness(1.06);transform:translateY(-1px)}
  @media(prefers-reduced-motion:reduce){
    body.nhys-motion *,body.nhys-motion *::before,body.nhys-motion *::after{
      animation:none!important;transition:none!important}
  }

  /* 插件安装/更新进行中：显性遮罩提示 */
  .update-overlay{position:fixed;inset:0;z-index:200;display:none;align-items:center;justify-content:center;
                  background:rgba(0,0,0,.35);backdrop-filter:blur(1px)}
  .update-overlay.show{display:flex}
  .update-box{display:flex;align-items:center;gap:14px;min-width:320px;max-width:80vw;
              background:var(--surface);border:1px solid var(--border);padding:20px 24px;
              box-shadow:0 12px 40px rgba(0,0,0,.25);color:var(--text)}
  .update-spinner{width:28px;height:28px;flex:none;border:3px solid var(--border);
                  border-top-color:var(--accent);border-radius:50%;animation:nhys-spin .8s linear infinite}
  .update-text{font-size:14px;font-weight:700;line-height:1.5}

  @media(max-width:760px){
    #topbar{height:auto;min-height:56px;padding:8px 12px;flex-wrap:wrap}
    .topbar-left{flex:1 1 100%}.topbar-center{order:3;width:100%;justify-content:flex-start;overflow:auto}
    .topbar-right{flex:1 1 100%;justify-content:flex-start}
    #nav button{padding:7px 10px;font-size:13px}
    main{padding:14px 12px 30px}
    .update-box{min-width:0;width:88vw}
  }
</style>
</head>
<body class="light-mode">
<header id="topbar">
  <div class="topbar-left">
    <div class="brand-logo">☾</div>
    <div class="brand-text">
      <div class="brand-title">NASCENCE · {{BOT_NAME}}</div>
      <div class="brand-sub">控制面板</div>
    </div>
  </div>
  <div class="topbar-center">
    <nav id="nav">
      <button class="active" data-page="overview">总览</button>
      <button data-page="plugins">插件</button>
      <button data-page="community">插件社区</button>
      <button data-page="logs">日志</button>
      <button data-page="config">配置</button>
      <button data-page="maint">维护</button>
    </nav>
  </div>
  <div class="topbar-right">
    <span id="cognitiveBadge" class="badge off">认知循环：已停止</span>
    <span id="stateBadge" class="badge wait">加载中</span>
    <button id="btnTheme" class="theme-toggle" title="切换日间/夜间主题">跟随系统</button>
    <button id="restartBtn" class="theme-toggle" title="保存数据并重启面板">重启面板</button>
    <button id="shutdownBtn" title="停止认知循环与全部插件服务、保存全部数据并退出面板">关闭面板</button>
  </div>
</header>

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
      <div class="plugin-meta" id="pluginManifestMeta"></div>
      <div class="row"><button class="act sec" id="btnPluginReadme">查看插件 README</button></div>
      <div id="pluginReadmeBox" class="md-preview markdown-body" style="display:none;margin-bottom:12px"></div>
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
      <div id="pluginPanelSection" style="margin-top:16px">
        <div class="kv" id="pluginPanelStatus"></div>
        <div id="pluginPanelFields" class="grid2" style="margin-top:12px"></div>
        <div class="row" style="margin-top:12px"><button class="act" id="btnSavePluginPanel" style="display:none">保存插件配置</button></div>
        <div class="row" id="pluginPanelActions" style="margin-top:10px"></div>
        <div class="hint" id="pluginPanelHint" style="margin-top:12px"></div>
      </div>
    </div>
  </section>

  <!-- 插件社区 -->
  <section class="page" id="page-community">
    <div class="panel">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">插件社区</h3>
        <div class="row" style="margin:0">
          <button class="act sec" id="btnCommunityRefresh">刷新目录</button>
          <button class="act sec" id="btnCommunityRestart">一键重启</button>
        </div>
      </div>
      <div class="hint">从 GitHub 检索带 <code>nhy-plugin</code> topic 的插件仓库；只安装 Release ZIP，并校验清单、API 版本和 SHA256。安装/更新后需要重启面板。</div>
      <div class="community-toolbar">
        <input id="communitySearch" placeholder="搜索插件名称、作者或描述…">
        <select id="communityFilter" style="max-width:160px">
          <option value="all">全部插件</option>
          <option value="installed">已安装</option>
          <option value="update">可更新</option>
        </select>
      </div>
      <div id="communityGrid" class="community-grid"></div>
      <div id="communityEmpty" class="hint" style="display:none">目录为空。检查网络或刷新目录。</div>
    </div>

    <div class="panel community-detail" id="communityDetail">
      <div class="community-detail-head">
        <button class="act sec" id="btnCommunityBack">← 返回列表</button>
        <h3 id="communityDetailTitle" style="margin:0"></h3>
        <span id="communityDetailBadge" class="tag"></span>
      </div>
      <div class="community-meta" id="communityDetailMeta"></div>
      <div class="community-actions" id="communityDetailActions"></div>
      <div class="md-preview markdown-body" id="communityReadme">加载 README…</div>
    </div>

    <div class="panel">
      <h3>社区设置</h3>
      <div class="hint">GitHub token 只用于提高检索限额/访问私有仓库；留空表示不修改。默认只检索 <code>nhy-plugin</code> topic。</div>
      <div class="grid3" style="margin-top:10px">
        <div><label>GitHub Token（留空不修改）</label><input id="communityGithubToken" type="password" placeholder="已配置则留空"></div>
        <div><label>限定 GitHub 账号（可选）</label><input id="communityGithubUser" placeholder="例如 zero-question-create"></div>
        <div><label>Topic</label><input id="communityTopic" placeholder="nhy-plugin"></div>
      </div>
      <label style="display:flex;gap:8px;align-items:center;margin-top:8px"><input type="checkbox" id="communityInstallEnabled" style="width:auto"> 允许从社区安装/更新</label>
      <label style="display:flex;gap:8px;align-items:center;margin-top:6px"><input type="checkbox" id="communityAllowGit" style="width:auto"> 没有 Release ZIP 时允许内部 git clone</label>
      <div class="row" style="margin-top:10px"><button class="act" id="btnCommunitySaveConfig">保存社区设置</button></div>
    </div>

    <div class="panel">
      <h3>导入本地插件</h3>
      <div class="hint">用于辉夜自建插件或离线安装。支持 ZIP，或本机插件目录（必须包含 plugin.json）。导入后需重启面板生效。</div>
      <div class="import-box" style="margin-top:10px">
        <div>
          <label>本地 ZIP 文件</label>
          <input type="file" id="communityImportFile" accept=".zip">
          <label style="margin-top:8px">或本机目录/文件路径</label>
          <input id="communityImportPath" placeholder="例如 data/plugin_staging/my_skill 或 C:\path\plugin.zip">
        </div>
        <button class="act" id="btnCommunityImport">导入插件</button>
      </div>
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
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="f-panel-animations" style="width:auto"> 启用 NHYS 面板动效（Nascence_Huiye_Server 风格）</label>
      <div class="row" style="margin-top:12px"><button class="act" id="btnSavePanel">保存面板设置</button></div>
      <div class="hint">端口号修改后下次启动生效；动效开关即时生效。</div>
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
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="f-never-sleep" style="width:auto"> 永不会困（不推进睡眠、不会入睡；取消勾选时恢复启动时的精力值）</label>
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
<div id="updateOverlay" class="update-overlay" aria-live="polite" aria-hidden="true">
  <div class="update-box">
    <div class="update-spinner"></div>
    <div class="update-text" id="updateText">正在更新插件…</div>
  </div>
</div>

<script>
const BOT = "{{BOT_NAME}}";
const $ = (id) => document.getElementById(id);
const MAX_LOG_LINES = 500;

// ===== 日间/夜间主题（参考 Nascence_Huiye_Server 面板）=====
const THEME_KEY = 'nhy-panel-theme';
let themeMode = localStorage.getItem(THEME_KEY) || 'follow';
const themeMedia = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
function applyTheme(mode){
  themeMode = mode || 'follow';
  const light = themeMode === 'light' || (themeMode === 'follow' && (!themeMedia || themeMedia.matches));
  document.body.classList.toggle('light-mode', light);
  const btn = $('btnTheme');
  if(btn) btn.textContent = themeMode === 'follow' ? '跟随系统' : (themeMode === 'light' ? '日间' : '夜间');
  localStorage.setItem(THEME_KEY, themeMode);
}
applyTheme(themeMode);
if(themeMedia && themeMedia.addEventListener){
  themeMedia.addEventListener('change', () => { if(themeMode === 'follow') applyTheme('follow'); });
}
function applyMotion(enabled){
  document.body.classList.toggle('nhys-motion', !!enabled);
}
function showUpdate(text){
  const overlay = $('updateOverlay');
  if(!overlay) return;
  $('updateText').textContent = text;
  overlay.classList.add('show');
  overlay.setAttribute('aria-hidden', 'false');
}
function hideUpdate(){
  const overlay = $('updateOverlay');
  if(!overlay) return;
  overlay.classList.remove('show');
  overlay.setAttribute('aria-hidden', 'true');
}

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

// ===== 插件社区 / README 渲染（对齐 Nascence_Huiye_Server 的 Markdown 语言）=====
function safeUrl(url){
  const value = String(url || '').trim();
  return /^https?:\/\//i.test(value) ? value : '#';
}
function renderMarkdown(src){
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  let text = String(src == null ? '' : src).replace(/\r\n?/g,'\n');
  const blocks = [];
  text = text.replace(/```([\s\S]*?)```/g, (m, code) => {
    blocks.push(`<pre><code>${esc(code.replace(/^\n/,''))}</code></pre>`);
    return `\u0000BLOCK${blocks.length - 1}\u0000`;
  });
  const inline = (s) => {
    let out = esc(s);
    out = out.replace(/`([^`]+)`/g, (m,c)=>`<code>${c}</code>`);
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      (m,alt,url)=>`<img src="${safeUrl(url)}" alt="${alt}">`);
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      (m,label,url)=>`<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    out = out.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    out = out.replace(/__([^_]+)__/g,'<strong>$1</strong>');
    out = out.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<em>$2</em>');
    out = out.replace(/~~([^~]+)~~/g,'<del>$1</del>');
    return out;
  };
  const lines = text.split('\n');
  const html = [];
  let listType = null, para = [];
  const flushPara = () => { if(para.length){ html.push(`<p>${inline(para.join('<br>'))}</p>`); para=[]; } };
  const closeList = () => { if(listType){ html.push(`</${listType}>`); listType=null; } };
  for(let i=0;i<lines.length;i++){
    const raw = lines[i]; const line = raw.trimEnd();
    const ph = line.match(/^\u0000BLOCK(\d+)\u0000$/);
    if(ph){ flushPara(); closeList(); html.push(blocks[Number(ph[1])]); continue; }
    if(!line.trim()){ flushPara(); closeList(); continue; }
    if(/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)){ flushPara(); closeList(); html.push('<hr>'); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flushPara(); closeList(); const lv=Math.min(h[1].length,4); html.push(`<h${lv}>${inline(h[2])}</h${lv}>`); continue; }
    const q = line.match(/^>\s?(.*)$/);
    if(q){ flushPara(); closeList(); html.push(`<blockquote>${inline(q[1])}</blockquote>`); continue; }
    // 简单表格：表头 | 分隔行 | 数据行
    if(line.includes('|') && i+1<lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*\|?\s*$/.test(lines[i+1])){
      flushPara(); closeList();
      const cells = line.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(x=>x.trim());
      i += 1;
      const rows = [];
      while(i+1<lines.length && lines[i+1].includes('|') && lines[i+1].trim()){
        i += 1;
        rows.push(lines[i].replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(x=>x.trim()));
      }
      html.push('<table><thead><tr>'+cells.map(c=>`<th>${inline(c)}</th>`).join('')+'</tr></thead><tbody>'+
        rows.map(r=>'<tr>'+r.map(c=>`<td>${inline(c)}</td>`).join('')+'</tr>').join('')+'</tbody></table>');
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if(ul){ flushPara(); if(listType!=='ul'){ closeList(); html.push('<ul>'); listType='ul'; } html.push(`<li>${inline(ul[1])}</li>`); continue; }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if(ol){ flushPara(); if(listType!=='ol'){ closeList(); html.push('<ol>'); listType='ol'; } html.push(`<li>${inline(ol[1])}</li>`); continue; }
    closeList(); para.push(line);
  }
  flushPara(); closeList();
  return html.join('\n');
}

let communityCache = [];
let communityInstalled = {};
let currentCommunityId = '';
function versionTuple(v){ return String(v||'0').split(/[.+-]/).slice(0,3).map(x=>parseInt(x||'0',10)||0); }
function versionNewer(a,b){
  const x=versionTuple(a), y=versionTuple(b);
  for(let i=0;i<3;i++){ if(x[i]!==y[i]) return x[i]>y[i]; }
  return false;
}
async function loadCommunity(force=false){
  const grid = $('communityGrid');
  if(grid && !grid.children.length) grid.innerHTML = '<div class="hint">正在加载插件目录…</div>';
  try{
    const d = await getJSON('/api/community/plugins' + (force?'?refresh=1':''));
    if(!d.ok) throw new Error(d.error || '目录读取失败');
    communityCache = d.plugins || [];
    communityInstalled = d.installed || {};
    $('communityEmpty').style.display = communityCache.length ? 'none' : 'block';
    if(d.error) toast('社区目录：' + d.error);
    renderCommunityGrid();
    if(currentCommunityId) openCommunity(currentCommunityId);
  }catch(e){
    toast('社区目录读取失败：' + e.message);
    if(grid) grid.innerHTML = '<div class="hint">社区目录读取失败：' + escHtml(e.message) + '</div>';
  }
}
function communityMatches(p){
  const q = String($('communitySearch').value || '').trim().toLowerCase();
  if(!q) return true;
  return [p.name,p.id,p.description,p.author].some(x=>String(x||'').toLowerCase().includes(q));
}
function renderCommunityGrid(){
  const grid = $('communityGrid'); grid.innerHTML='';
  const filter = $('communityFilter').value;
  let count = 0;
  for(const p of communityCache){
    const inst = communityInstalled[p.id] || {};
    const installed = !!inst.installed;
    const update = installed && versionNewer(p.version, inst.version);
    if(filter==='installed' && !installed) continue;
    if(filter==='update' && !update) continue;
    if(!communityMatches(p)) continue;
    count++;
    const card = document.createElement('div');
    card.className = 'community-card' + (p.id===currentCommunityId?' active':'');
    card.onclick = () => openCommunity(p.id);
    card.innerHTML =
      `<div class="community-card-head"><span class="community-name">${escHtml(p.name || p.id)}</span>`+
      `<span class="tag ${p.verified?'ok':'warn'}">${p.verified?'已校验':'未校验'}</span></div>`+
      `<div class="community-desc">${escHtml(p.description || '')}</div>`+
      `<div class="community-meta">${escHtml(p.author || p.full_name || '')} · v${escHtml(p.version || '')}</div>`+
      `<div class="community-badges">${installed?`<span class="tag ok">已安装 ${escHtml(inst.version||'')}</span>`:''}`+
      `${update?'<span class="tag warn">可更新</span>':''}`+
      `${(p.kind==='skill')?'<span class="tag">技能</span>':''}</div>`;
    grid.appendChild(card);
  }
  if(!count) grid.innerHTML = '<div class="hint">没有匹配的插件。</div>';
}
function openCommunity(id){
  const p = communityCache.find(x => x.id === id);
  if(!p) return;
  currentCommunityId = id;
  renderCommunityGrid();
  $('communityDetail').classList.add('open');
  $('communityDetailTitle').textContent = p.name || p.id;
  $('communityDetailBadge').textContent = p.verified ? '已验证发布包' : '未验证发布包';
  $('communityDetailBadge').className = 'tag ' + (p.verified?'ok':'warn');
  const inst = communityInstalled[id] || {};
  $('communityDetailMeta').innerHTML =
    `<span class="k">描述</span><span>${escHtml(p.description || '')}</span>`+
    `<span class="k">版本</span><span>${escHtml(p.version || '')}${inst.installed?'（已安装 '+escHtml(inst.version||'')+'）':''}</span>`+
    `<span class="k">作者</span><span>${escHtml(p.author || '')}</span>`+
    `<span class="k">仓库</span><span><a href="${safeUrl(p.repo)}" target="_blank" rel="noopener noreferrer">${escHtml(p.full_name || p.repo || '')}</a></span>`+
    `<span class="k">平台</span><span>${escHtml((p.platforms||[]).join(' / '))}</span>`+
    `<span class="k">权限</span><span>${escHtml((p.permissions||[]).join('、') || '无声明')}</span>`+
    `<span class="k">校验</span><span>${p.sha256?escHtml(p.sha256.slice(0,16)+'…'):'缺少 SHA256（不能安装）'}</span>`;
  const actions = $('communityDetailActions'); actions.innerHTML='';
  const mk = (label, cls, fn, disabled=false) => {
    const b = document.createElement('button'); b.className='act '+cls; b.textContent=label;
    b.disabled=disabled; b.onclick=fn; actions.appendChild(b);
  };
  if(!inst.installed) mk('安装','',()=>communityAction('install',id), !p.verified);
  else {
    mk('更新','sec',()=>communityAction('update',id), !p.verified);
    mk('卸载','sec',()=>communityAction('uninstall',id));
  }
  mk('重启面板','sec',restartPanel);
  $('communityReadme').innerHTML = '加载 README…';
  loadCommunityReadme(id);
}
async function loadCommunityReadme(id){
  try{
    const d = await getJSON('/api/community/readme?id=' + encodeURIComponent(id));
    $('communityReadme').innerHTML = d.ok && d.readme ? renderMarkdown(d.readme) : '<p class="hint">没有 README。</p>';
  }catch(e){ $('communityReadme').innerHTML = '<p class="hint">README 读取失败：'+escHtml(e.message)+'</p>'; }
}
async function communityAction(kind,id){
  if(kind==='uninstall' && !confirm('确定卸载这个插件？')) return;
  const p = communityCache.find(x => x.id === id) || {};
  const name = p.name || id;
  const buttons = Array.from(document.querySelectorAll('#communityDetailActions button'));
  const labels = {
    install: `正在安装 ${name}…`,
    update: `正在更新 ${name}…`,
    uninstall: `正在卸载 ${name}…`,
  };
  if(kind !== 'uninstall') showUpdate(labels[kind] || '正在处理插件…');
  buttons.forEach(b => b.disabled = true);
  try{
    const d = await post('/api/community/' + kind, {id});
    hideUpdate();
    toast(d.detail || (kind==='install'?'安装完成':'操作完成'));
    await loadCommunity(true);
    if(d.restart_required && confirm('插件文件已更新，需要重启面板生效。现在重启吗？')) restartPanel();
  }catch(e){
    hideUpdate();
    toast('操作失败：' + e.message);
  }finally{
    buttons.forEach(b => b.disabled = false);
  }
}
async function communityImport(){
  const file = $('communityImportFile').files[0];
  const path = $('communityImportPath').value.trim();
  if(!file && !path){ toast('请选择 ZIP 或填写路径'); return; }
  showUpdate('正在导入本地插件…');
  try{
    let d;
    if(file){
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch('/api/community/import', {method:'POST', body:fd});
      d = await r.json();
    }else{
      d = await post('/api/community/import', {path});
    }
    if(!d.ok) throw new Error(d.error || '导入失败');
    hideUpdate();
    toast('插件已导入：' + (d.id || ''));
    $('communityImportFile').value=''; $('communityImportPath').value='';
    await loadCommunity(true);
    if(confirm('插件已导入，需要重启面板生效。现在重启吗？')) restartPanel();
  }catch(e){ hideUpdate(); toast('导入失败：' + e.message); }
}
async function restartPanel(){
  if(!confirm('重启会短暂停止认知循环与插件服务，保存数据后重新启动。继续吗？')) return;
  try{ await post('/api/restart', {}); toast('面板正在重启…'); }
  catch(e){ toast('重启失败：' + e.message); }
}
async function loadCommunityConfig(){
  try{
    const d = await getJSON('/api/community/config');
    if(!d.ok) return;
    $('communityGithubUser').value = d.github_user || '';
    $('communityTopic').value = d.topic || 'nhy-plugin';
    $('communityInstallEnabled').checked = !!d.install_enabled;
    $('communityAllowGit').checked = !!d.allow_git;
    $('communityGithubToken').placeholder = d.github_token_configured ? '已配置，留空不修改' : '未配置';
  }catch(e){ /* 设置读取失败不阻塞目录 */ }
}
async function saveCommunityConfig(){
  try{
    const d = await post('/api/community/config', {
      github_token: $('communityGithubToken').value.trim(),
      github_user: $('communityGithubUser').value.trim(),
      topic: $('communityTopic').value.trim() || 'nhy-plugin',
      install_enabled: $('communityInstallEnabled').checked,
      allow_git: $('communityAllowGit').checked,
    });
    $('communityGithubToken').value = '';
    toast('社区设置已保存');
    await loadCommunity(true);
  }catch(e){ toast('保存失败：' + e.message); }
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
    $('f-twake').value = c.biorhythm_wake_seconds ?? '';
    $('f-tsleep').value = c.biorhythm_sleep_seconds ?? '';
    $('f-onset').value = c.biorhythm_onset_threshold ?? '';
    $('f-wake').value = c.biorhythm_wake_threshold ?? '';
    $('f-rw').value = c.biorhythm_rhythm_weight ?? 0.25;
    $('f-idle').value = c.biorhythm_idle_to_sleep ?? 300;
    $('f-never-sleep').checked = !!c.biorhythm_never_sleep;
    $('f-panel-port').value = c.panel_port ?? 32123;
    $('f-panel-animations').checked = !!c.panel_animations;
    applyMotion(!!c.panel_animations);
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
  feed:'信息流', search:'搜索', proactive:'主动发言', computer:'电脑'
};
let pluginCache = [];
let currentPluginDetailId = null;
let currentPluginHasKey = false;
const PLUGIN_FIELD_TYPES = ['text', 'password', 'number', 'bool', 'select', 'textarea'];
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
          `<div><b>${escHtml(p.label)}</b>${p.conflict_note && !running ? ' <span class="hint">⚠ 与其他插件可能冲突</span>' : ''} <span class="hint" id="prun-state-${escHtml(p.id)}">${escHtml(p.id)} · ${p.is_app ? '软件型' : '工具型'} · ${running ? '运行中' : '已停止'}</span>` +
          `<div class="hint">${escHtml(p.description || '')}${caps ? ' · 能力：' + escHtml(caps) : ''}</div></div>` +
          `<button id="prun-btn-${escHtml(p.id)}" class="act ${running ? 'sec' : ''}" ${p.implemented ? '' : 'disabled'} onclick="pluginAction('${escHtml(p.id)}','${running ? 'stop' : 'start'}')">${running ? '停止' : '启动'}</button>` +
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
async function refreshPluginRunning(){
  // 只更新运行状态与按钮，不重建列表，避免打断点击。
  try{
    const d = await getJSON('/api/plugins');
    pluginCache = d.plugins || [];
    for(const p of pluginCache){
      const running = pluginRunning(p);
      const state = $('prun-state-' + p.id);
      if(state){
        state.textContent = `${p.id} · ${p.is_app ? '软件型' : '工具型'} · ${running ? '运行中' : '已停止'}`;
      }
      const btn = $('prun-btn-' + p.id);
      if(btn && p.implemented){
        btn.textContent = running ? '停止' : '启动';
        btn.className = 'act ' + (running ? 'sec' : '');
        btn.onclick = () => pluginAction(p.id, running ? 'stop' : 'start');
      }
    }
  }catch(e){ /* 轮询失败忽略 */ }
}
async function pluginAction(id, action){
  if(action === 'start'){
    // 通用必填检查：插件自述面板里的 required 字段为空时先引导去填写。
    const p = pluginCache.find(x => x.id === id);
    const schema = (p && p.panel && p.panel.schema) || {};
    const values = (p && p.panel && p.panel.values) || {};
    const missing = (schema.fields || []).filter(f => {
      if(!f.required) return false;
      const filled = String(values[f.key] ?? '').trim();
      if(filled) return false;
      return !(f.type === 'password' && values['_has_' + f.key]);
    });
    if(missing.length){
      openPluginDetail(id);
      toast('还需要填写：' + missing.map(f => f.label || f.key).join('、'));
      return;
    }
    if(p && p.conflict_note && !confirm(p.conflict_note + '\n\n仍然启动吗？')){
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
  const meta = p.manifest || {};
  $('pluginManifestMeta').innerHTML =
    `<span class="k">版本</span><span>${escHtml(meta.version || p.version || '0.0.0')}（API ${escHtml(String(meta.api_version || ''))}）</span>`+
    `<span class="k">类型</span><span>${escHtml(meta.kind || p.kind || 'app')}</span>`+
    `<span class="k">作者</span><span>${escHtml(meta.author || p.author || '未声明')}</span>`+
    `<span class="k">来源</span><span>${escHtml(meta.source || p.source || '内置/本地')}</span>`+
    `<span class="k">权限</span><span>${escHtml((meta.permissions || p.permissions || []).join('、') || '无声明')}</span>`+
    `<span class="k">平台</span><span>${escHtml((meta.platforms || []).join(' / ') || 'any')}</span>`;
  $('pluginReadmeBox').style.display = 'none';
  $('pluginReadmeBox').innerHTML = '';
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
  renderPluginPanel(p);
}
// 插件自述面板：状态行 + 字段全部由 /api/plugins 返回的 panel 决定。
// 新插件只要在插件目录放 panel.py（或实现 panel_schema/panel_values/panel_save），
// 这里不需要任何改动。
function renderPluginPanel(p){
  const panel = p.panel || {};
  const schema = panel.schema || {};
  const values = panel.values || {};
  const statusEl = $('pluginPanelStatus');
  const fieldsEl = $('pluginPanelFields');
  const saveBtn = $('btnSavePluginPanel');
  statusEl.innerHTML = '';
  fieldsEl.innerHTML = '';

  const live = values._status || {};
  const statusRows = schema.status || [];
  for(const row of statusRows){
    const k = document.createElement('div');
    k.className = 'k';
    k.textContent = row.label || row.key;
    const v = document.createElement('div');
    v.id = 'pst-' + row.key;
    v.textContent = live[row.key] ?? '—';
    statusEl.appendChild(k);
    statusEl.appendChild(v);
  }
  statusEl.style.display = statusRows.length ? '' : 'none';

  const fields = schema.fields || [];
  for(const f of fields){
    const box = document.createElement('div');
    const head = document.createElement('div');
    head.className = 'field-head';
    const label = document.createElement('label');
    label.textContent = f.label || f.key;
    head.appendChild(label);
    const helpText = String(f.help || f.hint || '').trim();
    let pop = null;
    if(helpText){
      const dot = document.createElement('button');
      dot.type = 'button';
      dot.className = 'help-dot';
      dot.textContent = '?';
      dot.title = helpText;
      dot.setAttribute('aria-label', '说明：' + helpText);
      pop = document.createElement('div');
      pop.className = 'help-pop';
      pop.textContent = helpText;
      dot.onclick = () => {
        const open = !pop.classList.contains('open');
        document.querySelectorAll('.help-pop.open').forEach(el => el.classList.remove('open'));
        document.querySelectorAll('.help-dot.open').forEach(el => el.classList.remove('open'));
        if(open){
          pop.classList.add('open');
          dot.classList.add('open');
        }
      };
      head.appendChild(dot);
    }
    box.appendChild(head);

    const type = PLUGIN_FIELD_TYPES.includes(f.type) ? f.type : 'text';
    let input;
    if(type === 'bool'){
      input = document.createElement('input');
      input.type = 'checkbox';
      input.style.width = 'auto';
      input.checked = !!values[f.key];
    }else if(type === 'select'){
      input = document.createElement('select');
      for(const opt of (f.options || [])){
        const item = document.createElement('option');
        const value = (opt && typeof opt === 'object') ? opt.value : opt;
        const text = (opt && typeof opt === 'object') ? (opt.label ?? opt.value) : opt;
        item.value = String(value ?? '');
        item.textContent = String(text ?? value ?? '');
        input.appendChild(item);
      }
      input.value = String(values[f.key] ?? '');
    }else if(type === 'textarea'){
      input = document.createElement('textarea');
      input.value = String(values[f.key] ?? '');
    }else{
      input = document.createElement('input');
      input.type = type === 'password' ? 'password' : (type === 'number' ? 'number' : 'text');
      input.value = type === 'password' ? '' : String(values[f.key] ?? '');
      if(type === 'password' && values['_has_' + f.key]){
        input.placeholder = '已配置，留空不修改';
      }else if(f.placeholder){
        input.placeholder = f.placeholder;
      }
    }
    input.id = 'pp-' + f.key;
    input.dataset.key = f.key;
    input.dataset.type = type;
    input.dataset.label = f.label || f.key;
    if(f.required) input.dataset.required = '1';
    if(values['_has_' + f.key]) input.dataset.has = '1';
    box.appendChild(input);
    if(pop) box.appendChild(pop);
    fieldsEl.appendChild(box);
  }
  fieldsEl.style.display = fields.length ? '' : 'none';
  saveBtn.textContent = schema.save_label || '保存插件配置';
  saveBtn.style.display = fields.length ? '' : 'none';
  saveBtn.onclick = () => savePluginPanel(p.id);
  const actionsEl = $('pluginPanelActions');
  actionsEl.innerHTML = '';
  for(const a of (schema.actions || [])){
    const btn = document.createElement('button');
    btn.className = 'act sec';
    btn.style.marginRight = '8px';
    btn.textContent = a.label || a.key;
    btn.onclick = () => runPluginAction(p.id, a.key);
    actionsEl.appendChild(btn);
  }
  const conflict = p.conflict_note ? '⚠ ' + p.conflict_note + ' ' : '';
  $('pluginPanelHint').textContent = conflict + (schema.hint || (fields.length ? '' : '该插件暂无额外配置。'));
}
async function runPluginAction(id, key){
  try{
    const d = await post('/api/plugins/panel/action', {id, key});
    toast(d.detail || '已执行');
    await loadPlugins();
    if(currentPluginDetailId) openPluginDetail(currentPluginDetailId);
  }catch(e){ toast('执行失败：' + e.message); }
}
function pluginPanelStatusValues(p){
  const schema = (p.panel && p.panel.schema) || {};
  const values = (p.panel && p.panel.values) || {};
  const live = values._status || {};
  for(const row of (schema.status || [])){
    const el = $('pst-' + row.key);
    if(el) el.textContent = live[row.key] ?? '—';
  }
}
async function savePluginPanel(id){  const values = {};
  const missing = [];
  for(const input of document.querySelectorAll('#pluginPanelFields [data-key]')){
    const key = input.dataset.key;
    const type = input.dataset.type || 'text';
    let value = type === 'bool' ? !!input.checked : String(input.value ?? '').trim();
    if(type === 'password' && !value){
      if(input.dataset.required && input.dataset.has !== '1') missing.push(input.dataset.label || key);
      continue;
    }
    if(input.dataset.required && !value) missing.push(input.dataset.label || key);
    if(type === 'number' && value !== '') value = Number(value);
    values[key] = value;
  }
  if(missing.length){
    toast('还需要填写：' + missing.join('、'));
    return;
  }
  try{
    await post('/api/plugins/panel', {id, values});
    toast('插件配置已保存');
    await loadPlugins();
    openPluginDetail(id);
  }catch(e){ toast('保存失败：' + e.message); }
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
    const animations = !!$('f-panel-animations').checked;
    await post('/api/config', { panel_port: p, panel_animations: animations });
    applyMotion(animations);
    toast('面板设置已保存（端口下次启动生效，动效已立即应用）');
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
      biorhythm_never_sleep: $('f-never-sleep').checked,
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

$('btnTheme').onclick = () => {
  applyTheme(themeMode === 'follow' ? 'light' : (themeMode === 'light' ? 'dark' : 'follow'));
};
// 社区页事件
$('restartBtn').onclick = restartPanel;
$('btnCommunityRefresh').onclick = () => loadCommunity(true);
$('btnCommunityRestart').onclick = restartPanel;
$('communitySearch').oninput = renderCommunityGrid;
$('communityFilter').onchange = renderCommunityGrid;
$('btnCommunityBack').onclick = () => {
  $('communityDetail').classList.remove('open');
  currentCommunityId = '';
  renderCommunityGrid();
};
$('btnCommunityImport').onclick = communityImport;
$('btnCommunitySaveConfig').onclick = saveCommunityConfig;
$('btnPluginReadme').onclick = async () => {
  if(!currentPluginDetailId) return;
  const box = $('pluginReadmeBox');
  if(box.style.display !== 'none'){ box.style.display='none'; return; }
  box.style.display='block'; box.textContent='加载 README…';
  try{
    const d = await getJSON('/api/plugins/readme?id=' + encodeURIComponent(currentPluginDetailId));
    box.innerHTML = d.ok && d.readme ? renderMarkdown(d.readme) : '<p class="hint">没有 README。</p>';
  }catch(e){ box.innerHTML = '<p class="hint">README 读取失败。</p>'; }
};

connectWS();
refreshStats();
loadConfig();
loadPlugins();
loadCommunityConfig();
loadCommunity(false);
setInterval(refreshStats, 500);
// 插件页显示时轮询运行状态：自动启动/初始化完成后按钮会自动变成“停止”。
setInterval(() => {
  const page = $('page-plugins');
  if(!page || !page.classList.contains('active')) return;
  if($('pluginDetailPanel').style.display !== 'none') return;
  refreshPluginRunning();
}, 2000);
// 插件详情页打开时，每 3 秒刷新一次状态行（不改动用户正在编辑的字段）。
setInterval(async () => {
  if(!currentPluginDetailId) return;
  if($('pluginDetailPanel').style.display === 'none') return;
  try{
    const d = await getJSON('/api/plugins');
    pluginCache = d.plugins || [];
    const p = pluginCache.find(x => x.id === currentPluginDetailId);
    if(p) pluginPanelStatusValues(p);
  }catch(e){ /* 状态刷新失败不影响操作 */ }
}, 3000);
</script>
</body>
</html>
"""
