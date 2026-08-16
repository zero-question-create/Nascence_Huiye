(function() {
    "use strict";
    
    /* ---------- 状态机 ---------- */
    let state = 0;          // 0:关闭, 1:一级, 2:二级, 3:三级
    let isAnimating = false;
    let timeoutId = null;
    
    const logoContainer = document.getElementById('logo-container');
    const menuLayer = document.getElementById('menu-layer');
    const appBg = document.getElementById('app-bg');
    
    /* ---------- 设置状态 ---------- */
    let snapToGrid = false;
    let themeMode = 'follow';
    
    /* ---------- 呼吸定时器 ---------- */
    let breathTimers = [];
    let cardOffsets = [];
    
    /* ---------- 完整菜单数据 ---------- */
    const menuData = [
        {
            title: '项目情况',
            sub: 'PROJECT STATUS',
            type: 'folder',
            children: [
                { title: '项目网站', sub: 'PROJECT SITE', type: 'link', url: 'https://github.com/zero-question-create/Nascence_Huiye' },
                { title: '社交媒体', sub: 'SOCIAL MEDIA', type: 'link', url: 'https://space.bilibili.com/3546696973814527' },
                { title: '最新资讯', sub: 'LATEST NEWS', type: 'link', url: 'https://www.zhihu.com/people/zhi5atrei' },
                { title: '提供反馈', sub: 'FEEDBACK', type: 'status', detail: '感谢使用' }
            ]
        },
        {
            title: '资源整合',
            sub: 'RESOURCES',
            type: 'folder',
            children: [
                { title: '弥生计划论坛', sub: 'NASCNECE', type: 'status', detail: '无' },
                { title: '八千代的小屋', sub: 'YACHIYO', type: 'link', url: 'https://yachiyo.cn' },
                { title: '月读空间', sub: 'TSUKUYOMI', type: 'link', url: 'https://yachiyo.hk' },
                { 
                    title: '粉丝社区', sub: 'FAN COMMUNITY', type: 'folder',
                    children: [
                        { title: 'QQ群', sub: 'QQ GROUP', type: 'status', detail: '在建' },
                        { title: '微信群', sub: 'WECHAT', type: 'status', detail: '在建' },
                        { title: '论坛', sub: 'FORUM', type: 'status', detail: '在建' },
                        { title: '八千代的小屋-BBS论坛', sub: 'YACHIYO BBS', type: 'link', url: 'https://bbs.yachiyo.cn' }
                    ]
                }
            ]
        },
        {
            title: '网站设置',
            sub: 'SETTINGS',
            type: 'folder',
            children: [
                { title: '布局模式', sub: 'LAYOUT', type: 'setting', settingKey: 'snap' },
                { title: '色彩主题', sub: 'THEME', type: 'setting', settingKey: 'theme' }
            ]
        },
        {
            title: '关于我们',
            sub: 'ABOUT US',
            type: 'folder',
            children: [
                { 
                    title: '项目介绍', sub: 'INTRODUCTION', type: 'folder',
                    children: [
                        { title: '产品MV', sub: 'PRODUCT MV', type: 'status', detail: '在建' },
                        { title: '产品github', sub: 'PRODUCT GITHUB', type: 'link', url: 'https://github.com/zero-question-create/Nascence_Huiye' },
                        { title: '设计理念', sub: 'PHILOSOPHY', type: 'status', detail: '在建' },
                        { title: '项目规划', sub: 'PROJECT PLAN', type: 'status', detail: '在建' }
                    ]
                },
                { title: '隐私政策', sub: 'PRIVACY POLICY', type: 'status', detail: '在建' },
                { title: '开发日志', sub: 'DEV LOG', type: 'status', detail: '在建' },
                { title: '加入我们', sub: 'JOIN US', type: 'status', detail: '进入QQ群：在建' }
            ]
        }
    ];
    
    /* ---------- 工具函数 ---------- */
    function isMobile() {
        return window.innerWidth <= 768;
    }
    function getSettingDisplay(settingKey) {
        if (settingKey === 'snap') {
            return snapToGrid ? '对齐网格' : '随机偏移';
        }
        if (settingKey === 'theme') {
            const map = { follow: '跟随系统', light: '浅色模式', dark: '深色模式' };
            return map[themeMode] || '跟随系统';
        }
        return '';
    }
    function toggleSetting(settingKey) {
        if (settingKey === 'snap') {
            snapToGrid = !snapToGrid;
            applyOffsets();
            saveSettings();
            document.querySelectorAll('.menu-card[data-setting-key="snap"] .menu-status.setting-status').forEach(el => {
                el.textContent = getSettingDisplay('snap');
            });
            return;
        }
        if (settingKey === 'theme') {
            const cycle = { follow: 'light', light: 'dark', dark: 'follow' };
            themeMode = cycle[themeMode] || 'follow';
            applyTheme();
            saveSettings();
            document.querySelectorAll('.menu-card[data-setting-key="theme"] .menu-status.setting-status').forEach(el => {
                el.textContent = getSettingDisplay('theme');
            });
            return;
        }
    }
    function applyTheme() {
        const isDark = themeMode === 'dark' || (themeMode === 'follow' && window.matchMedia('(prefers-color-scheme: dark)').matches);
        document.body.classList.toggle('dark-mode', isDark);
    }
    function applyOffsets() {
        cardOffsets.forEach(({ wrapper, dx, dy }) => {
            if (snapToGrid) {
                wrapper.style.transform = '';
            } else {
                wrapper.style.transform = `translate(${dx}px, ${dy}px)`;
            }
        });
    }
    function saveSettings() {
        try {
            localStorage.setItem('nav_settings', JSON.stringify({ themeMode, snapToGrid }));
        } catch (e) {}
        syncToServer();
    }
    function loadSettings() {
        try {
            const raw = localStorage.getItem('nav_settings');
            if (raw) {
                const data = JSON.parse(raw);
                if (data.themeMode) themeMode = data.themeMode;
                if (data.snapToGrid !== undefined) snapToGrid = data.snapToGrid;
            }
        } catch (e) {}
        applyTheme();
    }
    function syncToServer() {}
    
    /* ---------- 像素网格 ---------- */
    function generateGridData() {
        const cols = 48, rows = 3;
        let gridRows = [];
        let breathableIndices = [];
        for (let r = 0; r < rows; r++) {
            let row = [];
            for (let c = 0; c < cols; c++) {
                let isFilled = false;
                if (c < 16) isFilled = true;
                else if (c < 24) isFilled = Math.random() < 0.7;
                else if (c < 36) isFilled = Math.random() < 0.4;
                else isFilled = Math.random() < 0.15;
                row.push(isFilled);
                if (c >= 32 && Math.random() < 0.3) {
                    breathableIndices.push(r * cols + c);
                }
            }
            gridRows.push(row);
        }
        return { gridRows, breathableIndices };
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
    function stopBreathing() {
        breathTimers.forEach(t => clearTimeout(t));
        breathTimers = [];
    }
    
    function generateOffset() {
        const maxOffset = isMobile() ? 18 : 35;
        let dx = (Math.random() - 0.5) * 2 * maxOffset;
        let dy = (Math.random() - 0.5) * 2 * maxOffset;
        return { dx, dy };
    }
    
    /* ---------- 层级栈 ---------- */
    let levelStack = [];
    let currentLevel = 0;
    let currentItems = [];
    
    function supportsHover() {
        return window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    }
    
    /* ---------- 渲染菜单 ---------- */
    function renderMenu(level, items) {
        stopBreathing();
        menuLayer.innerHTML = '';
        cardOffsets = [];
        const gridContainer = document.createElement('div');
        gridContainer.id = 'menu-grid';
        menuLayer.appendChild(gridContainer);
        
        items.forEach((item, index) => {
            const wrapper = document.createElement('div');
            wrapper.className = 'card-wrapper';
            const { dx, dy } = generateOffset();
            cardOffsets.push({ wrapper, dx, dy });
            if (!snapToGrid) {
                wrapper.style.transform = `translate(${dx}px, ${dy}px)`;
            }
            
            const card = document.createElement('div');
            card.className = 'menu-card';
            
            const cols = Math.min(items.length, 2);
            const row = Math.floor(index / cols);
            const col = index % cols;
            let slideClass = '';
            if (row === 0 && col === 0) slideClass = 'slide-br';
            else if (row === 0 && col === 1) slideClass = 'slide-bl';
            else if (row === 1 && col === 0) slideClass = 'slide-tr';
            else if (row === 1 && col === 1) slideClass = 'slide-tl';
            else slideClass = 'slide-tl';
            card.classList.add(slideClass);
            card.style.animationDelay = (0.1 + index * 0.1) + 's';
            
            if (item.type === 'status' || item.type === 'setting') {
                card.classList.add('no-click');
            }
            if (item.type === 'setting') {
                card.dataset.settingKey = item.settingKey;
            }
            
            // 像素区块
            const pixelBlock = document.createElement('div');
            pixelBlock.className = 'pixel-block';
            const { gridRows, breathableIndices } = generateGridData();
            const pixelGrid = document.createElement('div');
            pixelGrid.className = 'pixel-grid';
            const totalCols = gridRows[0] ? gridRows[0].length : 48;
            gridRows.forEach(row => {
                row.forEach((filled, colIdx) => {
                    const cell = document.createElement('div');
                    cell.className = 'pixel-cell' + (filled ? ' filled' : '');
                    const ratio = Math.round((colIdx / Math.max(1, totalCols - 1)) * 100);
                    cell.style.setProperty('--grad-pos', `${ratio}%`);
                    pixelGrid.appendChild(cell);
                });
            });
            pixelBlock.appendChild(pixelGrid);
            card.appendChild(pixelBlock);
            
            // 内容区
            const content = document.createElement('div');
            content.className = 'card-content';
            const titleWrap = document.createElement('div');
            titleWrap.className = 'title-wrapper';
            const title = document.createElement('div');
            title.className = 'menu-title';
            title.textContent = item.title;
            titleWrap.appendChild(title);
            if (item.sub) {
                const subtitle = document.createElement('div');
                subtitle.className = 'menu-subtitle';
                subtitle.textContent = item.sub;
                titleWrap.appendChild(subtitle);
            }
            content.appendChild(titleWrap);
            
            // 创建 detail 元素（所有卡片都可能需要）
            let detailEl = null;
            let detailText = '';
            
            if (item.type === 'status' && item.detail) {
                detailText = item.detail;
            } else if (item.type === 'folder' && item.children && item.children.length > 0) {
                // 显示子项中文标题，用顿号分隔
                detailText = item.children.map(child => child.title).join("、");
            } else if (item.type === 'link') {
                detailText = item.url || '链接';
            }
            
            if (detailText) {
                detailEl = document.createElement('div');
                detailEl.className = 'card-detail';
                detailEl.textContent = detailText;
                content.appendChild(detailEl);
            }
            
            if (item.type === 'setting') {
                const status = document.createElement('div');
                status.className = 'menu-status setting-status';
                status.textContent = getSettingDisplay(item.settingKey);
                content.appendChild(status);
            }
            
            card.appendChild(content);
            wrapper.appendChild(card);
            gridContainer.appendChild(wrapper);
            
            if (breathableIndices.length > 0) {
                startBreathing(card, breathableIndices);
            }
            
            // ---------- 交互事件 ----------
            // 统一悬停预览逻辑（仅PC），适用于所有有 detail 的卡片
            if (detailEl && supportsHover()) {
                let hover = false;
                // 对于 status 卡片，额外管理锁定状态
                let locked = false;
                if (item.type === 'status') {
                    // 点击锁定（仅 status）
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (!locked) {
                            locked = true;
                            detailEl.classList.add('visible');
                            detailEl.classList.add('locked');
                        }
                        card.classList.add('press');
                        setTimeout(() => card.classList.remove('press'), 200);
                    };
                }
                
                // 悬停事件
                wrapper.addEventListener('mouseenter', () => {
                    hover = true;
                    if (item.type === 'status' && locked) {
                        // 锁定状态下悬停不改变
                        return;
                    }
                    detailEl.classList.add('visible');
                    detailEl.classList.remove('locked');
                });
                wrapper.addEventListener('mouseleave', () => {
                    hover = false;
                    if (item.type === 'status' && locked) {
                        // 锁定状态下不隐藏
                        return;
                    }
                    detailEl.classList.remove('visible');
                    detailEl.classList.remove('locked');
                });
                
                // 对于非 status 卡片（folder, link），点击仍执行原有逻辑
                if (item.type === 'folder') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (isAnimating) return;
                        const targetIndex = Array.from(gridContainer.children).indexOf(wrapper);
                        if (targetIndex === -1) return;
                        const targetItem = items[targetIndex];
                        if (targetItem.children) {
                            levelStack.push({ level: currentLevel, items: currentItems });
                            enterLevel(level + 1, targetItem.children);
                        }
                    };
                    wrapper.addEventListener('mousedown', () => card.classList.add('press'));
                    wrapper.addEventListener('mouseup', () => card.classList.remove('press'));
                    wrapper.addEventListener('mouseleave', () => card.classList.remove('press'));
                    wrapper.addEventListener('touchstart', () => card.classList.add('press'));
                    wrapper.addEventListener('touchend', () => setTimeout(() => card.classList.remove('press'), 200));
                } else if (item.type === 'link') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (item.url) {
                            window.open(item.url, '_blank');
                        }
                    };
                    wrapper.addEventListener('mousedown', () => card.classList.add('press'));
                    wrapper.addEventListener('mouseup', () => card.classList.remove('press'));
                    wrapper.addEventListener('mouseleave', () => card.classList.remove('press'));
                    wrapper.addEventListener('touchstart', () => card.classList.add('press'));
                    wrapper.addEventListener('touchend', () => setTimeout(() => card.classList.remove('press'), 200));
                } else if (item.type === 'setting') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        toggleSetting(item.settingKey);
                        const statusEl = card.querySelector('.menu-status.setting-status');
                        if (statusEl) {
                            statusEl.textContent = getSettingDisplay(item.settingKey);
                        }
                        card.classList.add('press');
                        setTimeout(() => card.classList.remove('press'), 200);
                    };
                }
            } else {
                // 不支持悬停（移动端或 detail 不存在），保留原有点击事件（无悬停）
                if (item.type === 'folder') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (isAnimating) return;
                        const targetIndex = Array.from(gridContainer.children).indexOf(wrapper);
                        if (targetIndex === -1) return;
                        const targetItem = items[targetIndex];
                        if (targetItem.children) {
                            levelStack.push({ level: currentLevel, items: currentItems });
                            enterLevel(level + 1, targetItem.children);
                        }
                    };
                    wrapper.addEventListener('touchstart', () => card.classList.add('press'));
                    wrapper.addEventListener('touchend', () => setTimeout(() => card.classList.remove('press'), 200));
                } else if (item.type === 'link') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (item.url) {
                            window.open(item.url, '_blank');
                        }
                    };
                    wrapper.addEventListener('touchstart', () => card.classList.add('press'));
                    wrapper.addEventListener('touchend', () => setTimeout(() => card.classList.remove('press'), 200));
                } else if (item.type === 'status' && detailEl) {
                    // 移动端：点击锁定（不可逆）
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        if (!detailEl.classList.contains('visible')) {
                            detailEl.classList.add('visible');
                            detailEl.classList.add('locked');
                        }
                        card.classList.add('press');
                        setTimeout(() => card.classList.remove('press'), 200);
                    };
                } else if (item.type === 'setting') {
                    wrapper.onclick = (e) => {
                        e.stopPropagation();
                        toggleSetting(item.settingKey);
                        const statusEl = card.querySelector('.menu-status.setting-status');
                        if (statusEl) {
                            statusEl.textContent = getSettingDisplay(item.settingKey);
                        }
                        card.classList.add('press');
                        setTimeout(() => card.classList.remove('press'), 200);
                    };
                }
            }
        });
        
        currentLevel = level;
        currentItems = items;
    }
    
    /* ---------- 状态切换 (无延迟，但保留菜单淡入淡出) ---------- */
    function openLevel1() {
        if (isAnimating) return;
        isAnimating = true;
        logoContainer.classList.add('active');
        appBg.style.opacity = '0';
        menuLayer.style.pointerEvents = 'auto';
        menuLayer.style.opacity = '1';
        levelStack = [];
        renderMenu(1, menuData);
        isAnimating = false;
        state = 1;
    }
    
    function enterLevel(level, items) {
        if (isAnimating) return;
        isAnimating = true;
        clearTimeout(timeoutId);
        renderMenu(level, items);
        menuLayer.style.opacity = '1';
        isAnimating = false;
        state = level;
    }
    
    function goBack() {
        if (isAnimating) return;
        if (state === 2 || state === 3) {
            const prev = levelStack.pop();
            if (prev) {
                isAnimating = true;
                clearTimeout(timeoutId);
                renderMenu(prev.level, prev.items);
                menuLayer.style.opacity = '1';
                isAnimating = false;
                state = prev.level;
            } else {
                isAnimating = true;
                clearTimeout(timeoutId);
                renderMenu(1, menuData);
                menuLayer.style.opacity = '1';
                isAnimating = false;
                state = 1;
            }
        }
    }
    
    let isClosingAll = false;
    function closeAll(notify = true) {
        if (isClosingAll) return;
        isClosingAll = true;
        try {
            clearTimeout(timeoutId);
            menuLayer.style.pointerEvents = 'none';
            menuLayer.style.opacity = '0';
            stopBreathing();
            menuLayer.innerHTML = '';
            appBg.style.opacity = '1';
            logoContainer.classList.remove('active');
            state = 0;
            levelStack = [];
            currentLevel = 0;
            currentItems = [];
            if (notify) {
                try {
                    window.dispatchEvent(new CustomEvent('navStationClosed'));
                    if (typeof window.__onNavClosed === 'function') window.__onNavClosed();
                } catch (e) {}
            }
        } finally {
            isClosingAll = false;
            isAnimating = false;
        }
    }
    
    /* ---------- 暴露给外部应用调用 ---------- */
    window.__openNavLevel1 = openLevel1;
    window.__closeNavStation = () => closeAll(false);
    window.__navGoBack = goBack;

    /* ---------- Logo 点击 ---------- */
    logoContainer.addEventListener('click', () => {
        if (isAnimating) return;
        if (state === 0) openLevel1();
        else if (state === 1) closeAll();
        else if (state === 2 || state === 3) goBack();
    });
    
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && state !== 0) {
            e.preventDefault();
            closeAll();
        }
    });
    
    loadSettings();
    
    window.addEventListener('beforeunload', function() {
        stopBreathing();
        clearTimeout(timeoutId);
    });
    
})();