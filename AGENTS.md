# AGENTS.md — 给 AI 智能体（Codex / DeepSeek Harness / Claude / Cursor …）

这是 **Stream Dock M18 控制台**：一个本地程序，接管 Stream Dock M18（15 个带屏幕的按键 + 3 个导航键 + 24 颗灯珠）。
所有回复请用**中文**。

## 一、用 MCP 开发 / 测试插件（最常见的任务）

控制台自带 MCP 服务（stdio）。接入配置在控制台「自制插件 → 接入 AI」里一键复制，或：

| 运行方式 | command | args |
|---|---|---|
| 绿色版（解压目录） | `<解压目录>\mcp\m18-mcp.exe` | 无 |
| 源码 | `python`（带控制台的 python.exe） | `app/mcp_server.py` |

控制台没开时 MCP 会在后台自动拉起。工具一览：

| 工具 | 用途 |
|---|---|
| `m18_plugin_api` | **先读**：自制插件开发指南（事件、`key` 对象、`M18.*` 函数、示例） |
| `m18_status` | 设备 / 场景状态，15 个键各绑了什么（动手前先看，测试优先用空键） |
| `m18_save_custom_plugin` / `m18_get_custom_plugin` / `m18_list_custom_plugins` / `m18_delete_custom_plugin` | 自制插件增删改查（保存即热重载） |
| `m18_bind_key` / `m18_list_plugins` | 把插件动作绑到键上 |
| `m18_press_key` | 模拟按键（短按 / 长按 / 双击），返回这次的日志和按后的键面图 |
| `m18_view_keys` | 15 键整版截图（或单键） |
| `m18_plugin_logs` | 插件日志、报错（带「代码第 N 行」）、联网请求记录 |
| `m18_set_key_action` / `m18_set_key_style` / `m18_clear_key` | 内置动作（程序 / 网址 / 热键 / 宏 / 媒体 / 窗口…）、标题样式、清空 |
| `m18_list_scenes` / `m18_apply_scene` | 普通 / 相册 / 灯效场景 |
| `m18_effect_api` | **写灯效前先读**：M18FX 文档、真实几何、音频与输入事件 |
| `m18_list_effects` / `m18_get_effect` / `m18_save_effect` / `m18_delete_effect` | 灯效库（左侧「灯效」分类）增删改查；新建不自动运行，更新运行中的灯效会热重载 |
| `m18_run_effect` / `m18_effect_scene` | 试运行灯效库里的一个灯效（不改场景）；查看/编辑灯效场景的页（加页、排序、移除、切页） |
| `m18_effect_input` | 当前灯效的 1–18 键下/上事件（不执行底层程序或热键） |
| `m18_view_effect` / `m18_effect_logs` / `m18_stop_effect` | 整板软件预览、状态与日志、退出并恢复原场景 |

**开发循环**：读指南 → 保存代码 → 绑到空键 → 按一下 → 看图和日志 → 修 → 再按，直到画面和日志都对。
键号：底行 1–5、中行 6–10、顶行 11–15。

注意：
- 改动立刻作用在真实设备上；不要动用户已经配置好的键，除非用户要求
- `m18_press_key` 会真的执行动作（打开程序、发热键…）；测试插件时绑在空键上
- 插件联网用 `M18.http()`（控制台代发，不受跨域限制），不要用 `fetch`
- 自制插件只能用浏览器能力；需要 Node.js / 文件系统时写标准 Elgato `.sdPlugin`
- 灯效代码用 `M18FX`，不是插件的 `M18`。不能联网、不能访问文件；文档在 `app/effect_guide.md`。
- 灯效测试先记下原场景；用 `m18_save_effect` 存进灯效库、`m18_run_effect` 试运行，不改原按键；下/上事件分别传 `down:true/false`，最后 `m18_stop_effect` 恢复。
- 灯效场景是多页的（每页引用灯效库的一个灯效）。相册 / 灯效场景各有一层自己的按键动作（存在场景文件 `pages[0]`，只在该场景运行时生效，不改底层普通场景，不能放图片/图标）：相册屏幕键默认单击「下一场景」；灯效屏幕键短按/双击空、长按「下一场景」，◀ ▶ 长按「上一/下一灯效页」。
- 官方 SDK 每写一张键图在该连接里固定 sleep 50ms、同连接串行（单连接整板≈770ms，自下而上分层）。`DeviceWS` 因此给每个键固定一条写图连接：所有按键级命令（`send_image`/`set_key_gif`/`clear_key_gif`/灯效批次）走该键连接，同键顺序不变；每批带 `gifLoopStatus` 作回执，同键积压≥2批时 `send_image` 等回执（真背压）；全局命令（`clear_keys`/GIF 循环启停）先等各键写完再在主连接执行（屏障）。整板≈57ms：切场景、相册轮播、灯效都受益；灯效等 15 条回执后只发最新快照（≈17fps），退出先等写完再恢复。本机 M18 固件不显示 `setTemporaryBackgroundImgData` 整屏背景，不要用。SDK 无回执支持时回退单连接：
- 单连接模式实时灯效屏幕共享60张JPEG/秒的总预算（15屏全部变化仍约4fps）；少数屏变化可更快。同批必须固定同一帧快照，不能逐键取不同时刻的新像素，也不能把变化集拆成跨帧的几行。预算等待放批次之间，图片/refresh通过一次socket写入；这不是硬件原子翻帧。灯珠限25fps，只由最新颜色线程写入，屏幕旧快照不能回灌灯色；全黑时清除单珠/全灯缓存并少量补发，退出灯效后停止补发。预览不是设备回读。普通动图仍用原生 GIF。全局间隙固定 `KEY_GAP=27`。

## 二、修改控制台源码（只在源码仓库里）

```
浏览器 UI ──HTTP/SSE── app/server.py ──WebSocket── ws-sdk/StreamDockWebsocketSDK.exe ──HID── M18
```

| 文件 | 内容 |
|---|---|
| `app/server.py` | `App` 主类（`__init__`、配置读写）、HTTP 接口、插件专用端口、启动入口 |
| `app/core.py` | 公共部分：常量、数据目录、输入模拟（热键 / 文字 / 鼠标）、动作标签、路径工具 |
| `app/app_render.py` | 按键画面渲染、待机画面、Dock 同步预览、原生动图 |
| `app/app_pages.py` | 多页面、文件夹、配置加载 |
| `app/app_media.py` | 按键图片素材、旋转、相册、相册场景 |
| `app/app_scenes.py` | 按键输入与动作、场景、场景导出导入、官方场景同步、前台程序自动切换 |
| `app/app_scene_keys.py` | 相册 / 灯效场景自己的按键层（默认动作、读写场景文件、输入分发） |
| `app/app_effect_scenes.py` | 灯效库、多页灯效场景、旧数据迁移、试运行、技术文档导出 |
| `app/app_catalog.py` | 官方图标包 / 插件目录、回收站、裁图工具 |
| `app/app_plugins.py` | 插件宿主集成、自制插件、AI 测试接口 |
| `app/app_lighting.py` | 灯光效果引擎、状态快照 |
| `app/pluginhost.py` | 兼容官方 / Elgato 协议的插件宿主（Node / exe / 无头 Edge 网页插件） |
| `app/official.py` | 读取官方驱动的场景、图标包、插件（含 `.mbg` 解密） |
| `app/wssdk.py` | WebSocket 客户端 + 官方 SDK 设备封装 |
| `app/mcp_server.py` | MCP 服务（只用标准库） |
| `app/plugin_guide.md` | 自制插件开发指南（`m18_plugin_api` 返回的就是它） |
| `app/ui/` | 界面（原生 HTML / CSS / JS，无构建步骤；改完把 `index.html` 里的 `?v=` 版本戳加一） |
| `packaging/build.py` | 打绿色版 zip（PyInstaller） |
| `tools/cssdiff.py` | 改 CSS 前后对比所有元素的计算样式（0 处不同 = 外观没变） |

- 运行：`python app/server.py`（`--no-browser` 只起服务）；改了 Python 要重启：
  `POST /api/quit`（带 `Origin: http://127.0.0.1:8642`、`Content-Type: application/json`）后再启动
- HTTP 写接口要求同源 `Origin` + JSON，自己写脚本时带上这两个头
- 网页插件、插件设置页、后台宿主页由**插件专用端口**提供（和控制台不同源）；插件注册只认启动时发的随机令牌（pluginUUID），不认插件 id
- 用户数据（配置、场景、自制插件）：源码运行在 `app/` 下，绿色版在 `%APPDATA%\M18Console`
- 设备命令本身没有回执；按键写图请走 `DeviceWS` 的方法（已用每键连接 + `gifLoopStatus` 回执做背压），不要绕过它直接狂发；动图用 SDK 原生 `setKeyGif`
- 代码注释、界面文字用中文

## 三、每轮修改后的测试交付

用户要求：**每次修改完成后，都更新本地免安装版，方便立即测试。**

- 每轮递增 `app/version.py` 的版本号，构建最新代码；不要只改源码后留旧 exe。
- 通常运行 `python packaging/build.py --release`：这里仅使用已提交的默认预设，不会上传，也不收集本机新场景或图标。
- 构建前检查正在运行的控制台归属；不要删除或覆盖仍在运行的程序目录。必要时先按项目规定退出，或选择独立构建目录。
- 保留上一个免安装包作为回退，验证 ZIP 完整性、必需资产、MCP 和灯效文档/运行时是否随包带齐。
- 灯效宿主必须由真实免安装 exe 父进程启动测试；仅在普通 Python 中执行冻结字节码不能覆盖资源管理器传入的 `__COMPAT_LAYER=DetectorsAppHealth`。宿主子进程应隔离该兼容层变量，不改主进程环境或系统设置。
- 可用 `M18Console.exe --check-effect-host <临时结果JSON路径>` 做无设备宿主检查；测试时设 `M18_DATA_DIR` 为临时目录，不接管 SDK、不采集音频。
- 完成后给出最新 ZIP 与 `dist/M18Console/M18Console.exe` 的本地路径；默认不替用户启动并接管设备。
- **更新本地免安装版不等于提交/推送/公开发布**；这些仍须用户明确要求。
