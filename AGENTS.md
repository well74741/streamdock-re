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
| `m18_list_scenes` / `m18_apply_scene` | 场景 |

**开发循环**：读指南 → 保存代码 → 绑到空键 → 按一下 → 看图和日志 → 修 → 再按，直到画面和日志都对。
键号：底行 1–5、中行 6–10、顶行 11–15。

注意：
- 改动立刻作用在真实设备上；不要动用户已经配置好的键，除非用户要求
- `m18_press_key` 会真的执行动作（打开程序、发热键…）；测试插件时绑在空键上
- 插件联网用 `M18.http()`（控制台代发，不受跨域限制），不要用 `fetch`
- 自制插件只能用浏览器能力；需要 Node.js / 文件系统时写标准 Elgato `.sdPlugin`

## 二、修改控制台源码（只在源码仓库里）

```
浏览器 UI ──HTTP/SSE── app/server.py ──WebSocket── ws-sdk/StreamDockWebsocketSDK.exe ──HID── M18
```

| 文件 | 内容 |
|---|---|
| `app/server.py` | 本地服务、渲染、动作、场景、插件宿主集成、HTTP 接口 |
| `app/pluginhost.py` | 兼容官方 / Elgato 协议的插件宿主（Node / exe / 无头 Edge 网页插件） |
| `app/official.py` | 读取官方驱动的场景、图标包、插件（含 `.mbg` 解密） |
| `app/wssdk.py` | WebSocket 客户端 + 官方 SDK 设备封装 |
| `app/mcp_server.py` | MCP 服务（只用标准库） |
| `app/plugin_guide.md` | 自制插件开发指南（`m18_plugin_api` 返回的就是它） |
| `app/ui/` | 界面（原生 HTML / CSS / JS，无构建步骤；改完把 `index.html` 里的 `?v=` 版本戳加一） |
| `packaging/build.py` | 打绿色版 zip（PyInstaller） |

- 运行：`python app/server.py`（`--no-browser` 只起服务）；改了 Python 要重启：
  `POST /api/quit`（带 `Origin: http://127.0.0.1:8642`、`Content-Type: application/json`）后再启动
- HTTP 写接口要求同源 `Origin` + JSON，自己写脚本时带上这两个头
- 用户数据（配置、场景、自制插件）：源码运行在 `app/` 下，绿色版在 `%APPDATA%\M18Console`
- 设备命令没有回执和背压，别一次性狂发图片；动图用 SDK 原生 `setKeyGif`
- 代码注释、界面文字用中文
