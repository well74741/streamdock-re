# Stream Dock M18 控制台

自研 Stream Dock M18 驱动：一个本地图形控制台，**不经过官方 Stream Dock 软件**，自己接管设备的按键、屏幕和 24 颗灯珠。

方案细节见 [方案-StreamDock-M18自研驱动.md](方案-StreamDock-M18自研驱动.md)。

## 打开方式

**装好的版本**：双击安装包 `M18Console-Setup-<版本>.exe`，装完开始菜单和桌面上都有「Stream Dock M18 控制台」。

**从源码运行**：双击 `启动 M18 控制台.bat`。

会启动本地服务（`127.0.0.1:8642`）和一个 Edge 应用窗口（无地址栏）。重复启动只会复用已有实例，并替换掉旧窗口。

## 安装包

```bash
python packaging/build.py      # 需要 Python 3.9+、Pillow、PyInstaller，且 ws-sdk/ 已就位
```

产物：`dist/release/M18Console-Setup-<版本>.exe`（单文件，约 44 MB，版本号在 `app/version.py`）。

- 自带 Python 运行时和官方 WebSocket SDK，对方电脑**不需要装 Python**
- 装到当前用户目录（`%LOCALAPPDATA%\Programs\M18 Console`），**不需要管理员权限**
- 开始菜单 / 桌面快捷方式，「设置 → 应用」里可卸载；卸载时可选是否保留用户数据
- 用户数据（配置、场景、上传的图标、自制插件）在 `%APPDATA%\M18Console`，升级重装不丢
- 静默安装：`Setup.exe --silent [--dir 目录] [--no-shortcut] [--no-launch]`

对方电脑的要求：Windows 10/11 64 位、Microsoft Edge（系统自带）、M18 设备。
**不需要官方 Stream Dock 软件**：设备驱动用的是安装包自带的官方 WebSocket SDK。
装了官方软件时还能导入官方场景 / 图标包 / 插件；导入后的内容存在本地，卸载官方软件也不会丢：

| 内容 | 存放 | 官方软件卸载后 |
|---|---|---|
| 官方场景（含图标） | 导入时转换进 `%APPDATA%\M18Console\scenes`、`assets\uploads` | 照常可用 |
| 图标包 | 启动后自动备份到 `official-mirror\icons`（缩小到按键尺寸，约 70 MB） | 照常可用 |
| 第三方插件 + Node 运行环境 | 「同步官方」时选择备份，复制到 `official-mirror\plugins`、`node`（约 500 MB） | 备份过的照常可用 |
| 官方内置功能（打开 / 热键 / 文本 / 页面…） | 拖到按键时换成控制台原生动作 | 照常可用 |

安装包没有数字签名，首次运行 Windows 可能提示「已保护你的电脑」，点「更多信息 → 仍要运行」即可。

## 架构（重要）

```
浏览器 UI ──HTTP/SSE── server.py ──WebSocket── StreamDockWebsocketSDK.exe ──HID── M18
```

- 设备 IO **全部走官方 WebSocket SDK**（`ws-sdk\StreamDockWebsocketSDK.exe`，默认端口 9002）。
  早期版本直接用 `transport.dll` 操作 HID，**收不到按键**——原因是设备必须先收到 `read` 命令
  才会开始上报输入，而 transport.dll 没有暴露这条命令。
- 连接成功后发给 SDK 的第一条设备命令必须是：
  ```json
  { "event": "read", "path": "<设备path的base64>", "payload": {} }
  ```
- 设置按键图片的字段是 **`imgData`**，且必须是带前缀的 data URI：
  `data:image/jpeg;base64,...`（写成 `imageData` 会被 SDK 静默忽略，表现为"命令发了但屏幕不亮"）。
- 其余命令：`setBrightness` / `setLEDBrightness` / `setSingleLedColor` / `refresh` / `clearAllIcon`。

## 当前功能

| 页签 | 内容 |
|---|---|
| 自定义控制台 | 15 个键格 + 外圈 22 颗灯珠 + 正面 2 颗 + 3 个导航键；右侧「灯光」「壁纸」两区 |
| 设备 | 设备信息、输入事件日志、重新连接、退出 |

- **按键**：点选键格后在下方编辑，可绑定
  - 打开路径（exe / 快捷方式 / 脚本 / 文件夹 / 文档）
  - 打开网页
  - 热键（支持「录制热键」，如 `ctrl+shift+s`）
  - 导航（上一页 / 下一页 / 主页 / 跳到第 N 页）
- **按键外观**：可设标题文字（中文自动适配字号、最多两行）+ 文字颜色 + 按键底色；
  拖入整张图片则优先显示图片。
- **多页面**：每页独立的按键图片、动作和样式；支持新建 / 改名 / 删除，导航键切换，控制台页码同步。
- **灯光**：常亮 / 彩虹 / 呼吸 / 流光 / 渐变 / 自定义，24 颗逐颗上色，支持反向、亮度、速度（最高 10×）。
- **待机**：闲置到设定秒数后整屏铺壁纸，设备按键立刻回到键面；界面操作不会打扰待机。
- **壁纸**：支持把图片直接拖到预览区替换；预览上可拖动平移；切换填充方式自动归零偏移。
- **热插拔**：拔掉自动等待，插上自动重连并重推画面、灯光、按键。
- **官方场景**：首次启动自动把官方驱动里 M18 的全部场景（含预设包、多页）转换进场景列表（◈ 标记），
  图标（含官方加密的 `.mbg` 动图）、标题、网址 / 程序 / 热键 / 宏 / 媒体键 / 亮度 / 场景切换都会还原；
  时钟、天气等官方插件控件只保留图标。删掉的官方场景不会再自动出现。
- **图标库**：直接读取官方驱动下载的图标包（按包分组、展开时才加载），点选或拖到按键上即可使用；
  用到的图标会复制一份进 `app/assets/uploads/`，官方删了图标包也不影响。
- **插件**：内置插件宿主，兼容官方 / Elgato 插件协议，直接运行官方驱动里装好的插件：
  Node.js 插件用官方自带的 `node20.exe`，exe 插件直接拉起，网页插件跑在后台无头 Edge 里。
  把侧栏「插件」里的动作拖到按键上即绑定；按键编辑区「插件设置」打开插件自带的设置页。
  插件进程日志在 `app/plugin-data/logs/`。官方「内置」插件（多媒体、热键等）由控制台自己的动作实现。
- **动作类型**：打开路径 / 网页、热键、双状态热键、媒体键、屏幕亮度、输入文本 / 密码（支持中文）、
  鼠标操作、关闭程序、切换到场景 / 返回上一场景、播放音效、宏（多步）、导航。
- **短按 / 长按 / 双击**：每个键三种触发可分别设动作（长按 0.5 秒、双击间隔 0.3 秒）；只设短按的键零延迟。
- **场景关联程序**：场景右键「关联程序…」，前台切到该程序时自动切过去，离开后切回原场景。
- **同步官方**：侧栏「同步官方」重新抓取官方驱动的场景、图标包、插件；场景可选只加新的或全部重导。
- **自制插件**：主页下方「自制插件」里用几行 JavaScript 写插件（带计数器 / 时钟 / 网络数据 / 开关模板、
  API 速查、运行日志、保存即热重载）；保存在 `app/custom-plugins/`，是标准 `.sdPlugin`，
  出现在侧栏「自制插件」里，拖到按键上使用。

## 关键参数

- 按键图片 64×64，屏幕 5×3，键间物理缝隙用「间隙」补偿（默认 27px）
- 逻辑键号：底行 1–5，中行 6–10，顶行 11–15；导航键 16 / 17 / 18
- 灯珠：外圈 22 颗（左 6 / 下 5 / 右 6 / 上 5，含四角）+ 正面 2 颗

## 目录

| 路径 | 说明 |
|---|---|
| `app/server.py` | 本地服务、渲染、灯光引擎、动作执行 |
| `app/wssdk.py` | 极简 WebSocket 客户端 + 官方 SDK 设备封装 |
| `app/ui/` | 界面（HTML/CSS/JS） |
| `app/config.json` | 配置（页面、按键、灯光、待机等） |
| `ws-sdk/` | 官方 WebSocket SDK（Windows） |
| `backups/` | 各阶段可用版本的压缩备份 |
| `docs/` | 抓包指引等排查文档 |
| `refs/` | 官方 SDK 与参考实现（仅查阅） |

## 排错

- 日志：`app/server.log`
- 收不到按键：确认 `StreamDockWebsocketSDK.exe` 在运行，且连接后发过 `read`
- 屏幕不亮：检查 `setKeyImgData` 的 `imgData` 是否带 `data:image/jpeg;base64,` 前缀
- 界面打不开：删掉 `%LOCALAPPDATA%\M18Console\browser` 后重启
- 图标库 / 插件索引为空：官方软件不在常见安装位置时，在 `app/config.json` 里加
  `"officialRoot": "X:\\你的路径\\Stream Dock"`（或设环境变量 `M18_SD_ROOT`）

## 安全

本地服务只接受本机控制台界面自己的请求：`Host` 必须是 `127.0.0.1`/`localhost`，
写操作必须同源且为 JSON。其他网页无法跨站调用 `/api/*`（例如偷偷给按键绑定程序）。
自己写脚本调用接口时，带上 `Content-Type: application/json` 即可。

## 从源码运行

仓库里**不含**以下第三方/本地内容，需要自己准备：

| 缺失项 | 说明 |
|---|---|
| `ws-sdk/` | 官方 WebSocket SDK（`StreamDockWebsocketSDK.exe` 及 DLL），从 Mirabox 官方 SDK 包里解压到项目根目录 |
| `app/config.json` | 首次运行会自动生成；也可复制 `app/config.example.json` 改名 |
| `app/assets/icons/` | 图标库由「同步图标库」从官方软件目录抓取生成 |
| `app/scenes/` | 场景数据，运行时生成 |

准备步骤：

1. 安装 Python 3.9+（勾选 Add to PATH），双击 `setup.bat` 自动装 Pillow
2. 把官方 SDK 的 Windows 包解压到 `ws-sdk/`（其中应有 `StreamDockWebsocketSDK.exe`）
3. 双击 `启动 M18 控制台.bat`（或 `python app/server.py`）
4. 浏览器会自动打开 `http://127.0.0.1:8642`

> 设备 IO 全部经过官方 WebSocket SDK，本仓库不包含其二进制文件。

## 提交到 Git

```bash
git init
git add .
git commit -m "初始提交"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```
