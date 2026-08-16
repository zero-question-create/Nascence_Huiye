/* gateway/client/app.js —— Nascence辉夜 对话客户端
   集成原导航站（nav_script.js 原封不动）+ 标签卡对话窗 + 登录门禁
   主题与导航站统一（nav_settings + body.dark-mode）
   发送用 Enter，换行用 Shift+Enter / Ctrl+Enter */
(function () {
    "use strict";

    // ========== 全局状态 ==========
    // 网关地址解析（优先级）：
    //   1. localStorage 记住的"上次手动连接的端口"（保证无法自动获取时仍连上次端口）
    //   2. 服务端下发的 client_config.json（同源可拉取时）
    //   3. 默认 127.0.0.1:8787
    const DEFAULT_GATEWAY = "http://127.0.0.1:8787";
    const LAST_GATEWAY_KEY = "gateway_last";

    function getLastGateway() {
        try {
            const raw = localStorage.getItem(LAST_GATEWAY_KEY);
            if (!raw) return null;
            const g = String(raw).trim();
            // 仅接受本机 http 地址，防止记忆被污染成任意 URL
            if (/^http:\/\/(127\.0\.0\.1|localhost):\d{1,5}$/.test(g)) return g;
        } catch (e) { /* ignore */ }
        return null;
    }
    function saveLastGateway(g) {
        try { localStorage.setItem(LAST_GATEWAY_KEY, String(g)); } catch (e) { /* ignore */ }
    }

    const lastGateway = getLastGateway();

    const state = {
        gateway: lastGateway || DEFAULT_GATEWAY,
        token: localStorage.getItem("gateway_token") || "",
        username: localStorage.getItem("gateway_user") || "",
        loggedIn: !!localStorage.getItem("gateway_token"),
        pendingChats: 0,
        botName: "辉夜",
        authMode: "login",
        navOpen: false,
        theme: "follow",
    };

    function $(id) { return document.getElementById(id); }
    const authOverlay = $("auth-overlay");
    const navMask = $("nav-mask");
    const navStation = $("nav-station");
    const logoContainer = $("logo-container");
    const avatarMenu = $("avatarMenu");

    function escapeHtml(s) {
        return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>");
    }
    function isMobile() { return window.innerWidth <= 768; }

    // ========== 主题：与原导航站统一（nav_settings + body.dark-mode） ==========
    const THEME_NAMES = { follow: "跟随系统", light: "浅色模式", dark: "深色模式" };
    function readThemeFromNav() {
        try {
            const raw = localStorage.getItem("nav_settings");
            if (raw) {
                const d = JSON.parse(raw);
                if (d.themeMode) state.theme = d.themeMode;
            }
        } catch (e) { /* default follow */ }
    }
    function applyTheme() {
        const isDark = state.theme === "dark" ||
            (state.theme === "follow" && window.matchMedia("(prefers-color-scheme: dark)").matches);
        document.body.classList.toggle("dark-mode", isDark);
        $("btnTheme").textContent = THEME_NAMES[state.theme] || "跟随系统";
    }
    function saveThemeToNav() {
        try {
            const raw = localStorage.getItem("nav_settings");
            const d = raw ? JSON.parse(raw) : {};
            d.themeMode = state.theme;
            localStorage.setItem("nav_settings", JSON.stringify(d));
        } catch (e) { /* ignore */ }
    }
    function toggleTheme() {
        const cycle = { follow: "light", light: "dark", dark: "follow" };
        state.theme = cycle[state.theme] || "follow";
        saveThemeToNav();
        applyTheme();
    }
    $("btnTheme").addEventListener("click", toggleTheme);

    // ========== 顶栏头像菜单 ==========
    $("userAvatar").addEventListener("click", (e) => {
        e.stopPropagation();
        avatarMenu.classList.toggle("open");
    });
    document.addEventListener("click", () => avatarMenu.classList.remove("open"));

    // ========== 配置：读取网关地址 ==========
    async function loadConfig() {
        // 同源时尝试拉取服务端下发的 client_config.json 获取最新 gateway；
        // 但以 localStorage 记住的"上次连接的端口"优先（无法自动获取时仍连上次端口）。
        try {
            const cfg = await (await fetch("client_config.json")).json();
            if (cfg && cfg.gateway && !getLastGateway()) {
                state.gateway = cfg.gateway;
            }
        } catch (e) { /* 独立打开/跨源时无法拉取，保持当前 gateway */ }
    }

    // ========== 网关请求 ==========
    async function gatewayApi(path, method = "GET", body = null) {
        const url = state.gateway.replace(/\/+$/, "") + path;
        const opts = { method, headers: {} };
        if (body !== null) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(url, opts);
        const data = await resp.json().catch(() => ({}));
        // 请求成功即记住当前端口（作为"上次访问的端口号"，下次启动优先使用）
        if (resp.ok) saveLastGateway(state.gateway);
        if (!resp.ok) {
            if (resp.status === 401 && state.loggedIn) {
                doLogout();
                $("authHint").textContent = "登录状态已过期，请重新登录。";
            }
            throw new Error(data.message || `HTTP ${resp.status}`);
        }
        return data;
    }

    // ========== 密码显示 / 隐藏 ==========
    document.querySelectorAll(".btn-pwd-eye").forEach(btn => {
        btn.addEventListener("click", () => {
            const targetId = btn.dataset.target;
            const input = $(targetId);
            if (!input) return;
            const isPwd = input.type === "password";
            input.type = isPwd ? "text" : "password";
            const icon = btn.querySelector(".eye-icon");
            if (icon) icon.textContent = isPwd ? "🙈" : "👁️";
        });
    });

    // ========== 登录门禁 ==========
    function requireAuth() {
        if (!state.loggedIn) { showAuthOverlay(); return false; }
        return true;
    }
    function showAuthOverlay() {
        avatarMenu.classList.remove("open");
        authOverlay.classList.add("active");
        setTimeout(() => $("authUsername").focus(), 60);
    }
    function hideAuthOverlay() { authOverlay.classList.remove("active"); }

    $("tabLogin").addEventListener("click", () => {
        state.authMode = "login";
        $("tabLogin").classList.add("active");
        $("tabRegister").classList.remove("active");
        $("btnAuthSubmit").textContent = "登 录";
        const confirmField = $("fieldConfirmPassword");
        if (confirmField) confirmField.style.display = "none";
        $("authHint").textContent = "";
    });
    $("tabRegister").addEventListener("click", () => {
        state.authMode = "register";
        $("tabRegister").classList.add("active");
        $("tabLogin").classList.remove("active");
        $("btnAuthSubmit").textContent = "注 册";
        const confirmField = $("fieldConfirmPassword");
        if (confirmField) confirmField.style.display = "block";
        $("authHint").textContent = "";
    });

    async function doAuth() {
        const gwElem = $("gwInput");
        if (gwElem && gwElem.value.trim()) state.gateway = gwElem.value.trim();
        const username = $("authUsername").value.trim();
        const password = $("authPassword").value;
        const confirmPassword = $("authConfirmPassword") ? $("authConfirmPassword").value : "";

        if (!username || !password) {
            $("authHint").textContent = "请填写用户名和密码。";
            return;
        }

        if (state.authMode === "register") {
            if (!confirmPassword) {
                $("authHint").textContent = "请再次输入密码以确认。";
                return;
            }
            if (password !== confirmPassword) {
                $("authHint").textContent = "两次输入的密码不一致，请重新核对。";
                return;
            }
        }

        const btn = $("btnAuthSubmit");
        btn.disabled = true;
        try {
            const r = await gatewayApi(`/gateway/${state.authMode}`, "POST", { username, password });
            if (!r.ok) throw new Error(r.message || "操作失败");
            state.token = r.token;
            state.username = r.username;
            state.loggedIn = true;
            localStorage.setItem("gateway_token", r.token);
            localStorage.setItem("gateway_user", r.username);
            $("authHint").textContent = "";
            hideAuthOverlay();
            enterChat();
        } catch (e) {
            $("authHint").textContent = e.message;
        } finally {
            btn.disabled = false;
        }
    }
    $("btnAuthSubmit").addEventListener("click", doAuth);
    $("authPassword").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth(); });
    const confirmInput = $("authConfirmPassword");
    if (confirmInput) {
        confirmInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth(); });
    }

    // ========== 对话页 ==========
    function enterChat() {
        $("userAvatar").textContent = (state.username || "?").slice(0, 1).toUpperCase();
        $("userName").textContent = state.username;
        $("userSub").textContent = "已登录 · 独立记忆空间";
        loadBotName();
        loadHistory();
    }

    // ========== WebSocket 实时推送（认知循环主动发言等消息） ==========
    let ws = null;
    let wsReconnectTimer = null;
    function connectWs() {
        try {
            if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
            // WebSocket 连接到网关主机（客户端独立存放/跨源时也能收到服务端推送）
            const gw = new URL(state.gateway);
            const proto = gw.protocol === "https:" ? "wss" : "ws";
            ws = new WebSocket(`${proto}://${gw.host}/ws`);
            ws.onmessage = (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    if (msg.type === "message") {
                        // 收到 WebSocket 推送时立即触发增量同步（精准对齐条数）
                        syncNewMessages();
                    }
                } catch (e) { /* ignore */ }
            };
            ws.onclose = () => {
                if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
                wsReconnectTimer = setTimeout(connectWs, 2000);
            };
        } catch (e) { /* ignore */ }
    }

    // 游标：历史中已渲染的 Bot 消息条数（用户消息由发送时本地实时渲染，
    // 不参与游标，避免与历史重复；Bot 回复按历史顺序增量渲染）
    let botRenderedCount = 0;
    let pollTimer = null;

    // 本地待确认的用户消息（实时渲染但尚未被服务端历史确认）。
    // 发送时入队，服务端历史确认（前缀匹配）后移除。
    // 仅在全量刷新（loadHistory）后调用 flushPending 补渲未确认项，防止丢失。
    let localPending = [];

    function prunePending(histUserMsgs) {
        // histUserMsgs：服务端历史中的用户消息文本（按时间序）。
        // pending 按发送序（FIFO），服务端按序写入 → 前缀匹配即确认
        let k = 0;
        while (k < localPending.length && k < histUserMsgs.length &&
               histUserMsgs[k] === localPending[k].text) {
            k++;
        }
        if (k > 0) localPending = localPending.slice(k);
    }

    function flushPending(histUserMsgs) {
        if (!localPending.length) return;
        prunePending(histUserMsgs);
        if (!localPending.length) return;  // 全部已被历史确认，无需补渲
        // 调用前提：DOM 刚被 loadHistory 全量重建（无 data-local 残留）。
        // 未确认的消息按序重渲，插在第一个 Bot 回复之前（保持对话顺序）。
        // 注意：pending 在此不清空——只有服务端历史确认（prunePending）才移除，
        // 否则下次全量刷新时会重复渲染。
        const view = $("chatView");
        const anchor = view.querySelector('.msg-row:not(.user):not(.system)');
        for (const p of localPending) {
            const row = document.createElement("div");
            row.className = "msg-row user";
            row.dataset.local = "1";
            const avatar = document.createElement("div");
            avatar.className = "msg-avatar";
            avatar.textContent = (state.username || "我").slice(0, 1).toUpperCase();
            row.appendChild(avatar);
            const bubble = document.createElement("div");
            bubble.className = "msg-bubble";
            const textNode = document.createElement("div");
            textNode.className = "msg-text";
            textNode.textContent = p.text;
            bubble.appendChild(textNode);
            row.appendChild(bubble);
            if (anchor) view.insertBefore(row, anchor);
            else view.appendChild(row);
        }
        scrollChatToBottom();
    }

    async function syncNewMessages() {
        if (!state.loggedIn || !state.token) return;
        try {
            const r = await gatewayApi(`/gateway/history?token=${encodeURIComponent(state.token)}`);
            const msgs = r.messages || [];
            if (!msgs.length) return;
            $("welcome").style.display = "none";
            // 只增量渲染 Bot 消息（用户消息已在发送时实时渲染）
            let botSeen = 0;
            for (let i = 0; i < msgs.length; i++) {
                const m = msgs[i];
                if (m.sender !== state.botName) continue;
                if (botSeen < botRenderedCount) { botSeen++; continue; }
                // 思考时间 = 该回复时间 − 上一条消息（无论谁发的）时间
                let think = null;
                if (i > 0 && m.time != null && msgs[i - 1].time != null) {
                    think = (m.time - msgs[i - 1].time).toFixed(1);
                }
                appendBubble(m.sender, m.content, m.source, think);
                botSeen++;
            }
            botRenderedCount = botSeen;
            // 轮询路径只做 pending 确认记账（服务端已收到 → 从待确认队列移除）。
            // 不补渲：DOM 中的 data-local 气泡仍在，补渲会重复；补渲仅在 loadHistory 全量重建后执行。
            prunePending(msgs.filter(m => m.sender !== state.botName).map(m => m.content));
        } catch (e) { /* silent */ }
    }

    function startMessagePolling() {
        if (pollTimer) clearInterval(pollTimer);
        // 1秒极速增量同步：保证后台任何渠道发出消息，前端均能在1秒内即刻捕获并上屏
        pollTimer = setInterval(syncNewMessages, 1000);
    }
    function stopMessagePolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    async function loadBotName() {
        try {
            const r = await gatewayApi("/gateway/bot");
            if (r.bot_name) { state.botName = r.bot_name; $("mentionName").textContent = r.bot_name; }
        } catch (e) { /* silent */ }
    }
    async function loadHistory() {
        if (!state.loggedIn) return;
        try {
            const r = await gatewayApi(`/gateway/history?token=${encodeURIComponent(state.token)}`);
            const view = $("chatView");
            view.innerHTML = "";
            const msgs = r.messages || [];
            $("welcome").style.display = msgs.length ? "none" : "";
            // 全量渲染（含用户消息与 Bot 回复），相邻消息时间差作为思考时间
            let prevTime = null;
            msgs.forEach(m => {
                let think = null;
                if (m.sender === state.botName && m.time != null && prevTime != null) {
                    think = (m.time - prevTime).toFixed(1);
                }
                appendBubble(m.sender, m.content, m.source, think);
                if (m.time != null) prevTime = m.time;
            });
            botRenderedCount = msgs.filter(m => m.sender === state.botName).length;
            // 页面刷新/重新进入时，历史加载完成后直接滑到底部
            scrollChatToBottom();
            // 启动毫秒级增量轮询双保险
            startMessagePolling();
            // 同步本地待确认消息（发送与 loadHistory 竞态时补渲）
            flushPending(msgs.filter(m => m.sender !== state.botName).map(m => m.content));
        } catch (e) { /* silent */ }
    }

    // 返回实际的滚动容器（#chat-body 是 overflow-y:auto 的滚动层，#chatView 只是内部消息列表）
    function getChatScroller() {
        return $("chat-body") || $("chatView");
    }

    function scrollChatToBottom() {
        const scroller = getChatScroller();
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }

    // 判断是否接近底部（用户没有向上翻阅历史时自动跟随到底部）
    function isNearBottom(scroller, threshold = 80) {
        return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < threshold;
    }

    function appendBubble(name, text, source, thinkSeconds = null) {
        const view = $("chatView");
        const scroller = getChatScroller();
        const isBot = name === state.botName;
        const isSystem = name === "系统" || source === "错误" || source === "系统" || text.includes("请联系管理员");
        
        const row = document.createElement("div");
        row.className = "msg-row" + (isSystem ? " system" : isBot ? "" : " user");
        
        if (!isSystem) {
            const avatar = document.createElement("div");
            avatar.className = "msg-avatar";
            avatar.textContent = isBot ? "♾" : (state.username || "我").slice(0, 1).toUpperCase();
            row.appendChild(avatar);
        }
        
        const bubble = document.createElement("div");
        bubble.className = "msg-bubble";
        
        // 仿 DeepSeek 思考时间：Bot 回答顶部显示灰色小字
        // 思考时间 = 该条消息时间 − 上一条消息时间（无论谁发的），由服务端时间戳计算
        if (isBot && !isSystem && thinkSeconds != null && thinkSeconds !== undefined) {
            const thinkBadge = document.createElement("div");
            thinkBadge.className = "think-badge";
            thinkBadge.textContent = `已深度思考（用时 ${thinkSeconds} 秒）`;
            bubble.appendChild(thinkBadge);
        }
        
        const textNode = document.createElement("div");
        textNode.className = "msg-text";
        textNode.textContent = text;
        bubble.appendChild(textNode);
        
        row.appendChild(bubble);
        const shouldScroll = isNearBottom(scroller);
        view.appendChild(row);
        // 仅在用户靠近底部时自动滚动到底部；向上翻阅历史时保持当前位置
        if (shouldScroll) scrollChatToBottom();
        $("welcome").style.display = "none";
    }

    // ========== 发送消息与增量同步 ==========
    // 发送用 Enter，换行用 Shift+Enter / Ctrl+Enter
    async function sendChat() {
        if (!requireAuth()) return;
        const text = $("chatInput").value.trim();
        if (!text) return;
        const sender = state.username || "群友";

        // 实时渲染：用户消息立即上屏（不等模型回复，支持连发多条）
        appendBubble(sender, text, "客户端");
        // 标记为本地实时渲染（供 flushPending 在全量刷新时去重/补渲）
        const _userRows = document.querySelectorAll("#chatView .msg-row.user");
        if (_userRows.length) _userRows[_userRows.length - 1].dataset.local = "1";
        localPending.push({ sender, text });
        $("chatInput").value = "";

        // 后台发送：不阻塞界面；Bot 回复由 1 秒轮询按历史顺序增量渲染
        // （后端将消息合成「xx说：」交给 process_dialogue，并同步写入历史）
        gatewayApi("/gateway/chat", "POST", {
            token: state.token, sender, text, mentioned: true,
        }).then(() => {
            // 发送完成（消息已入历史）后主动同步一次，及时拉取新增内容
            syncNewMessages();
        }).catch((e) => {
            // 系统错误提示（如：请联系管理员启动认知循环）以灰色居中小字呈现
            appendBubble("系统", e.message || "请联系管理员启动认知循环", "错误");
        });
    }
    $("btnChatSend").addEventListener("click", sendChat);
    $("chatInput").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey) {
            e.preventDefault();
            sendChat();
        }
        // Shift+Enter / Ctrl+Enter：默认换行
    });

    // ========== 退出登录 ==========
    function doLogout() {
        stopMessagePolling();
        state.token = "";
        state.username = "";
        state.loggedIn = false;
        botRenderedCount = 0;
        localPending = [];
        localStorage.removeItem("gateway_token");
        localStorage.removeItem("gateway_user");
        const chatView = $("chatView");
        if (chatView) chatView.innerHTML = "";
        const welcome = $("welcome");
        if (welcome) welcome.style.display = "";
        const userName = $("userName");
        if (userName) userName.textContent = "未登录";
        const userSub = $("userSub");
        if (userSub) userSub.textContent = "";
        const userAvatar = $("userAvatar");
        if (userAvatar) userAvatar.textContent = "?";
        $("authPassword").value = "";
        if ($("authConfirmPassword")) $("authConfirmPassword").value = "";
        showAuthOverlay();
    }
    const btnLogout = $("btnLogout");
    if (btnLogout) {
        btnLogout.addEventListener("click", (e) => {
            e.stopPropagation();
            if (typeof closeNav === "function") closeNav();
            avatarMenu.classList.remove("open");
            doLogout();
        });
    }

    // ========== 导航站（顶栏 LOGO 点击展开，logo 从顶栏飞到中心） ==========
    // nav_script.js 负责 logo 点击→展开菜单（state 0→openLevel1）及 ESC→closeAll。
    // 这里只负责遮罩/标签卡层级联动与恢复顶栏。
    function resetNavScriptState() {
        if (typeof window.__closeNavStation === "function") {
            window.__closeNavStation();
        } else {
            // 触发原导航站的 ESC 处理器（closeAll），把其内部 state 归零，保证下次可正常展开
            document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
        }
    }
    function openNav() {
        if (!requireAuth()) return;
        resetNavScriptState();
        state.navOpen = true;
        logoContainer.style.opacity = "";
        navMask.classList.add("nav-open");
        navStation.classList.add("nav-open");
        const chatPage = $("chat-page");
        if (chatPage) chatPage.classList.add("hidden");
        const backBtn = $("btnBackChat");
        if (backBtn) backBtn.classList.add("visible");
        // 隐藏顶栏 LOGO 按钮，让导航站的 logo 从该位置飞向中心
        $("btnOpenNav").style.visibility = "hidden";
        // 延迟触发展开一级菜单
        setTimeout(() => {
            if (typeof window.__openNavLevel1 === "function") {
                window.__openNavLevel1();
            }
        }, 500);
    }
    let isClosingNav = false;
    function closeNav() {
        if (isClosingNav) return;
        isClosingNav = true;
        try {
            state.navOpen = false;
            navMask.classList.remove("nav-open");
            navStation.classList.remove("nav-open");
            const chatPage = $("chat-page");
            if (chatPage) chatPage.classList.remove("hidden");
            $("btnOpenNav").style.visibility = "";
            const backBtn = $("btnBackChat");
            if (backBtn) backBtn.classList.remove("visible");
            // 恢复导航站 logo 并直接隐形，避免向顶栏过渡时发生缩放膨胀
            logoContainer.style.opacity = "0";
            logoContainer.classList.remove("active");
            if (typeof window.__closeNavStation === "function") {
                window.__closeNavStation();
            }
            setTimeout(() => {
                const menuLayer = $("menu-layer");
                if (menuLayer) {
                    menuLayer.style.opacity = "0";
                    menuLayer.style.pointerEvents = "none";
                    menuLayer.innerHTML = "";
                }
            }, 500);
            // 从导航菜单切换过的主题同步到顶栏按钮
            readThemeFromNav();
            applyTheme();
        } finally {
            isClosingNav = false;
        }
    }
    $("btnOpenNav").addEventListener("click", openNav);
    $("btnBackChat").addEventListener("click", closeNav);
    window.__onNavClosed = closeNav;
    window.addEventListener("navStationClosed", closeNav);

    // ESC：关闭导航站（回退对话页）或登录弹窗
    // 说明：nav_script.js 自身也监听 ESC 并执行 closeAll（菜单收起、state 归零），
    // 本处理器负责把遮罩/标签卡层级收起回对话页，二者互补。
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (state.navOpen) { closeNav(); return; }
            if (authOverlay.classList.contains("active") && !state.loggedIn) return;
            if (authOverlay.classList.contains("active")) hideAuthOverlay();
        }
    });

    // 点击导航站的 logo 展开菜单（由 nav_script.js 处理），无需在此重复逻辑。

    // ========== 像素网格（对话标签卡顶部，灰色渐变至主题色，自适应宽度） ==========
    let breathTimers = [];
    function stopBreathing() { breathTimers.forEach(t => clearTimeout(t)); breathTimers = []; }

    function getDynamicColumns(container) {
        const width = (container && container.offsetWidth) || 860;
        const cellSize = 5;
        return Math.max(48, Math.floor(width / cellSize));
    }

    function generateGridData(cols = 48) {
        const rows = 3;
        let gridRows = [];
        let breathableIndices = [];
        const cutoff1 = Math.round(cols * 0.25);
        const cutoff2 = Math.round(cols * 0.45);
        const cutoff3 = Math.round(cols * 0.70);
        const cutoffBreath = Math.round(cols * 0.50);

        for (let r = 0; r < rows; r++) {
            let row = [];
            for (let c = 0; c < cols; c++) {
                let isFilled = false;
                if (c < cutoff1) isFilled = true;
                else if (c < cutoff2) isFilled = Math.random() < 0.75;
                else if (c < cutoff3) isFilled = Math.random() < 0.45;
                else isFilled = Math.random() < 0.18;
                row.push(isFilled);
                if (c >= cutoffBreath && Math.random() < 0.35) {
                    breathableIndices.push(r * cols + c);
                }
            }
            gridRows.push(row);
        }
        return { gridRows, breathableIndices, cols };
    }

    function startBreathing(cardElement, breathableIndices) {
        if (!cardElement) return;
        const pixelGrid = cardElement.querySelector('.pixel-grid');
        if (!pixelGrid) return;
        const cells = pixelGrid.children;
        if (!cells.length) return;
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        const ratePerSecond = isMobile() ? 1 : 4;
        let pool = breathableIndices.slice();
        if (pool.length === 0) return;
        function breatheOne() {
            if (!cardElement || !pixelGrid.parentNode) return;
            const randIdx = Math.floor(Math.random() * pool.length);
            const index = pool[randIdx];
            if (index < cells.length) {
                cells[index].classList.toggle('filled');
            }
            const timer = setTimeout(breatheOne, 1000 / ratePerSecond);
            breathTimers.push(timer);
        }
        const delay = Math.random() * 1000;
        const startTimer = setTimeout(breatheOne, delay);
        breathTimers.push(startTimer);
    }

    function buildPixelGrid(container) {
        if (!container) return [];
        container.innerHTML = "";
        const cols = getDynamicColumns(container);
        const { gridRows, breathableIndices } = generateGridData(cols);
        gridRows.forEach(rowArr => rowArr.forEach((filled, colIdx) => {
            const cell = document.createElement("div");
            cell.className = "pixel-cell" + (filled ? " filled" : "");
            const ratio = Math.round((colIdx / Math.max(1, cols - 1)) * 100);
            cell.style.setProperty('--grad-pos', `${ratio}%`);
            container.appendChild(cell);
        }));
        return breathableIndices;
    }

    function initChatPixelGrid() {
        stopBreathing();
        const container = $("chatPixelGrid");
        if (container) {
            const breathable = buildPixelGrid(container);
            startBreathing($("chat-card"), breathable);
        }
    }
    initChatPixelGrid();
    window.addEventListener("resize", () => {
        clearTimeout(window._pixelResizeTimer);
        window._pixelResizeTimer = setTimeout(initChatPixelGrid, 250);
    });

    // ========== 初始化 ==========
    window.addEventListener("beforeunload", stopBreathing);
    loadConfig();
    readThemeFromNav();
    applyTheme();
    connectWs();
    if (state.loggedIn) {
        enterChat();
    } else {
        showAuthOverlay();
    }
})();
