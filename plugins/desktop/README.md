# 电脑插件（desktop）

让辉夜操作本机软件。**感知走 Windows UI Automation 的无障碍树，动作优先走元素语义
Pattern；语义动作不可用时才在坐标围栏内用虚拟鼠标/键盘兜底；默认全程不截图。**
默认把程序作为浮动窗口放在工作区里，宿主键鼠可直接输入；需要 True 子窗口时
把 `desktop_window_mode` 改成 `embed`。

## 结构

```
plugins/desktop/
├── plugin.py      # DesktopPlugin：NHY 的“电脑”软件（is_app、可动手、任务队列）
├── desktop_operator.py  # local 后端：一步一个 JSON 动作的操作手
├── uia.py         # UIA 执行器：树压缩、引用解析、Pattern 动作、任意 exe/路径启动
├── input.py       # SendInput：坐标鼠标、Unicode 键入、组合键、拖拽、滚轮
├── worker.py      # 虚拟桌面宿主窗口（PyQt5，独立进程）
├── worker_client.py     # 插件侧启动/控制宿主窗口
├── mcp_server.py  # 标准 MCP stdio 服务（供外部 MCP 客户端；当前运行路径不经过它）
├── dsh_bridge.py  # 预留给“外联 dsh”的命令模板（未接线）
├── acp_client.py  # 预留给“外联 dsh”的 ACP 客户端（未接线）
└── panel.py       # 后台面板自述（registry 自动发现）
```

## 为什么默认不截图

同类的 Windows 控件优先方案（sbroenne/mcp-windows、FlaUI-MCP、bun-uia）实测：

- 文本树每步约 200~700 token，截图每步约 2000~3500 token；
- 只回传变化部分（diff）比整窗描述少 84%~96.6% token；
- UIA Pattern 动作不移动鼠标、不抢焦点，后台窗口也能操作。

本插件的 `snapshot` 默认压缩树（深度 12、节点上限 400、折叠标题栏/滚动条），
动作后 `diff=true` 只回变化。

## 工具（MCP）

`list_windows / snapshot / click / set_value / type_text / press_key / focus /
scroll / close_window / launch_app / screenshot / focus_window / move / click_at /
type_at / drag / scroll_at`

- 引用格式 `w<hwnd>e<序号>g<世代>`；解析时按路径重新定位并校验“角色 + 名字”，
  对不上直接拒绝，避免误点。
- `click` 依次尝试 Invoke → Toggle → Select → Expand → LegacyIAccessible；
  全部失败且元素有可见矩形时，才在坐标围栏内退到 `click_at`。
- `click_at / type_at / drag / scroll_at / move` 用 SendInput 操作真实光标；
  `type_at` 先点击坐标再以 Unicode 键入（中文/emoji 不依赖输入法）。
  坐标必须来自 `snapshot(with_bounds=true)` 的 `@l,t,r,b`，并且默认受
  “目标窗口/工作桌面矩形”围栏限制；目标窗口缺失时直接拒绝。
- `snapshot` 默认附带可操作元素的屏幕矩形（`with_bounds`），坐标就是屏幕坐标。
- `screenshot` 必须显式调用，且受 `desktop_allow_screenshot` 控制。
- MCP 的 stdin/stdout 强制 UTF-8，避免 Windows 管道把中文变成 GBK 乱码。

## 配置（`config/api_config.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `desktop_max_steps` | 12 | 单个任务最多几步 |
| `desktop_task_timeout` | 180 | 单个任务超时（秒） |
| `desktop_thought_window` | 10 | 插件运行时把短期思考窗口设为 9~11 |
| `desktop_allow_screenshot` | false | 是否允许显式截图 |
| `desktop_observe_software` | true | 软件打开后由电脑插件读窗口 |
| `desktop_use_real_desktop` | false | true=直接操作真实 Windows 桌面，不启动虚拟工作桌面 |
| `desktop_launch_mode` | auto | auto=已打开就复用镜像，否则新开；new=总是新开；reuse=只复用镜像 |
| `desktop_window_mode` | float | float=窗口浮在工作区内（宿主键鼠可输入）；embed=真子窗口（可能无法键入） |
| `desktop_allow_coordinate_input` | true | 允许坐标鼠标/指定位置键入 |
| `desktop_allow_synthetic_keys` | true | 允许 Enter/快捷键/焦点键入 |
| `desktop_allow_shell_windows` | true | 放行 Progman/WorkerW/任务栏等 shell 窗口 |
| `desktop_coordinate_fence` | true | 坐标必须落在目标窗口/工作桌面内 |
| `desktop_wallpaper` | 空 | 壁纸图片路径；留空跟随系统壁纸 |
| `desktop_worker_width` | 1280 | 虚拟桌面窗口宽 |
| `desktop_worker_height` | 800 | 虚拟桌面窗口高 |

内置程序表：`config/desktop_apps.json`（没有时读 `config/desktop_apps.example.json`）。
它只作为常用程序提示和启动参数来源，不再是启动白名单：
`launch` 还接受绝对 exe/lnk/bat 路径，以及 PATH 里能解析到的程序名。

每个程序条目可以用 `launch_mode` 覆盖全局的 `desktop_launch_mode`：

```json
"QQ": { "path": "E:\\Tencent\\QQ\\QQ.exe", "launch_mode": "reuse" }
```

- `reuse`：只找已打开的窗口，复制渲染（DWM 缩略图，失败则 PrintWindow 抓帧），
  不启动新进程，也不移动/最小化用户原窗口。适合已经登录的 QQ/微信。
- `new`：先启动程序；新进程的窗口能嵌入就嵌入，不能嵌入就退回复制；如果程序
  只能复用旧窗口，会改为复制旧窗口，不会把用户窗口拖进桌面。
- `auto`：有可见窗口就复用+镜像，没有才新开。

运行路径只有一条：**NHY 自己的操作手**（`desktop_operator.py` + `uia.py`），
用全局行为模型的 `action_llm` 逐步决策，不经过任何外部 agent。

## 虚拟鼠标 / 键盘（`input.py`）

- 语义动作优先：`click(ref)` 用 UIA Pattern，不移动光标、不抢焦点。
- 需要位置时才用坐标：`move / click_at / type_at / drag / scroll_at`，
  统一走 `SendInput`；`type_text` 用 `KEYEVENTF_UNICODE`，中文和 emoji
  不经过输入法。
- 操作手提示词要求“坐标只能来自 snapshot 的 `@l,t,r,b` 或任务明确给出的值”，
  并禁止重复点击已经生效的同一位置。
- 两个面板开关分别控制坐标鼠标/键入、合成按键/键入；围栏默认开启，
  只允许落在目标窗口或工作桌面矩形内。目标窗口缺失时直接拒绝，
  不会退化成“点全屏”。

## 行为边界

操作手只输出 JSON 动作，**不组织语言**；非 JSON 输出直接丢弃，动作结果
不会写进聊天历史。只有语言层能决定说什么。任务进度与最终摘要只写进
`APP_CONTEXT` 作为她“看到的事实”。

## 真实桌面模式

把 `desktop_use_real_desktop` 设为 `true`（面板里是“直接使用真实桌面”）：

- 不再启动虚拟工作桌面/worker；
- 操作手直接列真实顶层窗口、读 UIA、点击和输入；
- 没有工作区镜像/嵌入，任务会直接动到你正在用的窗口；
- 记事本回复桥仍然工作：会找顶层记事本窗口，把回复追加进去；
- 坐标围栏仍按目标窗口矩形生效。

适合虚拟桌面太复杂、只需要“真的去点本机软件”的场景；需要隔离和可视工作区时再切回 false。

## 工作桌面（worker.py）

- 用**系统壁纸**铺底（读 `HKCU\Control Panel\Desktop\WallPaper`，读不到时退回深色），
  左侧是真实 exe 图标的桌面图标，底部是任务栏 + “已打开”芯片 + 状态行。
- `launch` 时启动程序。默认 `desktop_window_mode=float`：窗口按工作区坐标
  作为**顶层窗口**摆放，宿主键鼠可以直接操作；350ms 的同步器会让它跟着宿主移动。
- 选 `embed` 时用 **`SetParent` 真嵌入**：改成 `WS_CHILD`、去掉 `WS_POPUP`，
  先对齐目标窗口的 DPI 上下文，再按桌面客户区坐标 `SetWindowPos`。
  窗口会随宿主移动、被宿主裁剪，不再浮在 Windows 桌面上；但跨进程子窗口
  通常收不到宿主键盘输入，记事本可能无法键入。
- 默认窗口占工作区约 72%，四周露出壁纸和图标；多个窗口按 26px 梯级错开。
- 嵌入失败（UWP、跨 DPI 拒绝 SetParent 等）时退回 float，状态行会写
  “浮在工作区（可键鼠输入）”，`state.embedded` 为 false。
- “已打开”芯片点一下把窗口重新归位并置前；“整理窗口”一键级联排列。
- **关闭语义**：点工作桌面右上角 X 只是最小化（程序仍嵌在桌面里，重新显示就回来了）；
  插件 stop / 面板“停止”才是真正退出：先解除嵌入，再把里面的程序**最小化交还系统**，
  不会让软件窗口突然弹到你的桌面上。`show` 会从最小化恢复并回到前台。
- 插件启动时拉起、停止/关闭时自动 unembed（恢复原父窗口和样式），不会把程序留在异常状态。
- 控制接口只监听 `127.0.0.1`，带一次性 token（`/state /launch /place /tidy /show /hide /shutdown`）。
- 应用列表优先读 `config/desktop_apps.json`（本机真实路径，已加入 .gitignore），
  没有时读 `config/desktop_apps.example.json`；同路径的别名在界面上只显示一次。
- 安全护栏：`Progman`、`WorkerW`、`Shell_TrayWnd`、`Shell_SecondaryTrayWnd`
  以及 `Program Manager` 桌面窗口永远不会被嵌入、镜像或移动。
- 保存的 `.txt` 笔记会自动显示在虚拟桌面左侧最上方：扫描
  `desktop_notes_dirs`（留空时扫描项目所在桌面、用户桌面/文档/下载），
  最近 8 个按修改时间排序；点击笔记图标会在工作区里用记事本打开。
- `qq_desktop` 现在也提供行为通道：语言层回复会输入到当前 QQ 会话的输入框
  并点击“发送”（失败则回车）；只有 QQ 不在前台/没有可用会话时，才回退到
  虚拟桌面记事本。
- 弹窗（另存为/下载框/小程序窗口）这类独立窗口也会被收进工作区：
  worker 每 800ms 检查宿主拥有的、或属于已启动程序的顶层窗口；所属程序是
  float 时弹窗也浮动（可键鼠输入），是 embed 时才优先真嵌入；不会再把它们
  留在 Windows 桌面上。
- 默认 `desktop_window_mode=float`：程序作为**顶层窗口**放在工作区坐标里，
  宿主鼠标、键盘、输入法可以直接使用（记事本、浏览器都能正常打字）。
  代价是它不是 True 子窗口，宿主移动时由 350ms 的同步器跟着移动。
- 需要视觉上真正嵌进桌面时选 `embed`：`SetParent` 成子窗口、随宿主裁剪；
  但跨进程子窗口通常收不到宿主键盘输入，记事本可能无法键入。
  单个程序可在 `desktop_apps.json` 条目里用 `window_mode` 覆盖。
- 嵌入后的程序是**子窗口**，`list_windows`（只枚举顶层）不会列出它；
  操作手使用 `launch` 返回的 hwnd 即可，UIA 的 snapshot/click/set_value 照常工作。
- 镜像本身**只是画面**，没有自己的无障碍树。`worker_launch` 在镜像成功后
  会对真实窗口再做一次 UIA snapshot（先深树 12，不够再 24/800/chrome），
  把内容写进 `desktop_app_<名字>` 状态，并在返回值里带 `parsed`；
  树里没有文本时明确写“只是镜像显示，窗口树里没有可读文本”。
- 浮动窗口和镜像现在统一平铺在工作区里；QQ 镜像不会再被 QQ 音乐等
  浮动窗口盖在最底层。
- 嵌入后的窗口仍然保留标题栏和边框，可以用鼠标在桌面里拖动/缩放（实测拖动 80×48 像素后
  窗口矩形同步变化），不需要宿主额外处理。

## 面板动作

插件详情页底部会按 `schema["actions"]` 渲染按钮（registry 自动发现）：

| key | 作用 |
|---|---|
| `self_test` | 自测：打开记事本写一行“自测成功”，走完整 `app_do → 操作手 → UIA` 链路 |
| `show_workspace` / `hide_workspace` | 显示 / 收起工作桌面 |
| `reload_wallpaper` | 按 `desktop_wallpaper` 重新加载壁纸，不用重启 worker |

壁纸说明：默认读系统壁纸（`HKCU\Control Panel\Desktop\WallPaper`）。如果系统装了
Wallpaper Engine 这类动态壁纸，注册表指向的是静态回调图，工作桌面只能显示这张静态图，
不会同步动态壁纸。

## 与其他插件的关系：可同时装，但有提示

电脑插件能直接操作 QQ/微信等本机窗口，**理论上可以替代平台插件**；但平台插件在
富消息、未读缓冲、断线重连上更稳，所以默认按“两种接入方式”处理：

- `DesktopPlugin.conflict_note()` 会在有其他插件运行时返回提示；
- 面板启动前弹确认框（“仍然启动吗？”），运行列表也会标 ⚠；
- 不阻止同时安装/同时运行。

## 与软件插件兼容（QQ/微信）

- 软件插件继续负责“有 N 条未读”的提示，行为不变。
- 当动作层打开某个软件（`open_app` / `open_conversation`）成功后，`core/apps.py` 会调用
  实现了 `provides_reading()` 的插件（目前就是电脑插件）的 `observe_app(app_id)`。
- 电脑插件按 exe / 标题找到该软件的真实窗口，用 UIA 读窗口树，写进
  `APP_CONTEXT.remember_state("desktop_app_<id>", …)`；同一个软件只保留最新一份。
- 开关：`desktop_observe_software`（面板里是“软件打开后由电脑读窗口”，默认开）。
- 限制：Chromium/Electron 系软件（QQNT、微信 4）默认无障碍树很浅，可能只读到
  空 Pane/Document；先加深树重试，仍读不到就截图交给视觉模型，最后才强制移进工作桌面。
- 如果窗口正被宿主使用，优先做**镜像**：先试 DWM 缩略图；DWM 不可用时退回
  PrintWindow 抓帧（每 500ms 刷新），不动原窗口。多个镜像会平铺在工作区，
  不会互相盖住；“显示工作桌面”会把宿主窗口抬到普通窗口最前（不设置置顶）。
  Pillow 12 的 ImageQt 只支持 Qt6，worker 用 PyQt5，所以抓帧结果直接按 BGRA
  构造 QImage，不依赖 ImageQt。
- 它不会替代平台插件自己的消息解析：QQ 打开对话时仍会把未读预览写进记忆。
  若要完全改成“只看电脑窗口”，需要平台插件配合关闭解析（不在本插件范围内）。

## 浏览器页面与链接

Chromium 的网页 `Document` 在 UIA 树第 13 层左右；深度 12 的浅树只能看到
地址栏和工具栏，操作手会误以为页面是空的。操作手现在：

- 默认用 depth 20 / 1200 节点读窗口；发现只有浏览器外壳时自动再深取一次；
- 在树前附上【页面链接】摘要（标题 + URL），并优先列 `/video/` 这类结果，
  避免 6000/9000 字截断把结果链接切掉；
- 提供 `links`（只读页面链接）、`url`（读地址栏）、`open_url`（打开网址）三个动作；
- 同程序多窗口时按任务标题匹配，优先复用 B 站等结果页，跳过“控制面板”；
- 浏览器复用旧进程时，新窗口属于旧 pid；`_find_new_window` 会按同 exe 的
  “启动前不存在”窗口来找，避免把控制面板当成任务窗口；
- 没有可用浏览器窗口或当前窗口是控制面板时，`open_url` 直接新开一个窗口，
  不会覆盖用户正在看的控制面板页。

## 放行策略

- 操作手不再维护窗口黑名单：ZCode、NHY 控制面板、Program Manager 等窗口
  都可以被 snapshot / click / set_value；
- `launch` 不再要求程序在 `config/desktop_apps.json` 里：支持内置表名字、
  绝对 exe/lnk/bat 路径，以及 PATH 里能解析到的程序名；
- 内置表仍作为常用程序的提示和启动参数来源，但不再是白名单限制；
- `desktop_allow_shell_windows` 默认开启，shell 窗口也不再被 `_is_shell_window`
  拦截；如果不想让工作桌面搬动系统桌面/任务栏，可在面板关掉它。

## 与认知循环的接线

1. 动作层新增 `app_do`：`{"action":"app_do","task":"打开记事本写一行字"}`
2. `core/apps.py` → `plugin.perform_task()` → 后台线程
3. 进度写回 `APP_CONTEXT.remember("正在操作：第 N 步 …")`
4. 动作层提示词里电脑显示为“可动手”，任务进行中显示“正在操作：…”
5. 操作手每一步都能看到【可启动的程序】清单；也可以直接给 exe/lnk 路径，不再受白名单限制

## dsh 预留（未接线）

当前版本**不经过 dsh**：`DesktopPlugin._run_task` 只调用 `desktop_operator.run_task`。

`acp_client.py` / `dsh_bridge.py` 保留为以后外联 DeepSeek Harness 的参考实现，
面板不提供切换，配置里的 `desktop_backend` / `desktop_dsh_*` 也不会被读取。
真要启用时再改动 `_run_task` 一处即可；返回值统一是 `{"ok","summary","lines"}`，
认知循环与插件其余部分无需改动。

（备注：dsh 的 `acp` profile 默认没有 `dsh-mcp-client`，需要 `--patch` +
`- insert:` 才能挂上 MCP 服务器；这条经验记在 `acp_client.py` 顶部，免得以后重查。）

## 已知盲区

- 管理员权限程序（UIPI）：非提权进程读不到，直接报“需要手动处理”。
- Electron/Chromium：部分应用默认不建无障碍树；Edge/Chrome 会在被查询时启用，
  Electron 应用可能需要 `--force-renderer-accessibility`。
- 画布/游戏/重自绘界面：树为空时才允许显式截图兜底。
