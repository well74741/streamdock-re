# Stream Dock M18 自研驱动方案

> 目标：替换官方 Stream Dock 软件，做出一个**能控灯、映射更顺、图片更好设、插件更开放**的驱动。
> 本文所有"实测"数据均来自你这台机器（`VID_6603 / PID_1012`，序列号 `08A903782C30`）。

---

## 0. 结论先行

**可行，而且比预想的简单。** 原因是一共三层，其中两层已经有官方或开源的现成件：

| 层 | 内容 | 现状 |
|---|---|---|
| 设备层 | HID 报文、图片分包、灯带、输入上报 | ✅ 官方 MIT 许可的设备级 SDK 已提供，**绕过官方软件直接控制** |
| 插件层 | 插件进程 ↔ 宿主 的 WebSocket JSON 协议 | ✅ 文档公开，且**与 Elgato 同名同构**，现成插件可直接复用 |
| 应用层 | 界面、配置模型、动作引擎、图片编辑、灯光效果 | ⬜ **这才是要写的东西**，也是官方软件真正烂的地方 |

换句话说：不需要逆向硬件协议（虽然也能做），工作量集中在"做一个更好的应用层"。

**最小可用路径**：设备层用官方 SDK + 应用层用 Electron/TypeScript 重写，插件层照抄 Elgato 协议。
单人全职约 **6–8 周**做到日常可替代；**半天**就能先拿到"能控灯"这个最痛的点（工具已写好，见第 9 节）。

---

## 1. 已在你机器上实测到的事实

这些不是推测，是刚才跑出来的结果。

| 项目 | 实测值 |
|---|---|
| USB 标识 | `VID 0x6603` / `PID 0x1012`（M18-E 变体），序列号 `08A903782C30` |
| 设备节点 | 复合设备两个接口：`MI_00` 厂商自定义 HID（控制口）、`MI_01` 键盘 HID |
| 固件 | `V3.M18V3_E_HXJDF.03.021` → 代码按 **V3 分支**处理 |
| 按键 | **15 个**，每个 **64×64** JPEG |
| 布局 | **5 列 × 3 行**（官方配置坐标 `0..4` × `0..2`，与 profile 完全对得上） |
| 额外按键 | **3 个无显示的实体键**（第 16/17/18），上报码 `0x25 / 0x30 / 0x31` |
| 附加屏 | 480×272 **开机 Logo**（写入 ROM，厂商明确警告乱写会变砖，**不建议实现**） |
| 灯带 | **24 颗 RGB 灯珠**，支持整条单色 / 逐颗独立色 / 全局亮度 / 复位 |
| 报文长度 | output 1025 字节（旧固件 512 模式为 513） |
| 独占性 | **官方软件运行时，第二个进程仍能 open / 读固件 / 写灯光**（实测 `open() == True`） |
| 写调用耗时 | 单次返回值 ≈ 0.01 ms（异步入队，不是同步 USB 传输） |
| 队列行为 | 连续推 3000 帧耗时 26 ms，`can_write()` 始终 true → **没有背压信号，必须自己控速** |

输入码到物理键的映射（官方 SDK 源码，注意不是顺序的）：

```
物理键(1=左下角, 左→右, 下→上)  上报码
 1 2 3 4 5                    0x0B 0x0C 0x0D 0x0E 0x0F
 6 7 8 9 10                   0x06 0x07 0x08 0x09 0x0A
11 12 13 14 15                0x01 0x02 0x03 0x04 0x05
16 17 18(底部三键)             0x25 0x30 0x31
```

> ⚠️ 一个容易踩的坑：**图片槽位编号和输入上报码不是同一套**。图片按 `1..15` 顺序写，输入按上表解码。官方软件自己也是这么处理的。

---

## 2. 官方软件为什么不好用（结构性原因）

不是它"没做好"，是它的架构决定了它没法让你改：

1. **单体 Qt/C++ 应用**（`StreamDock.exe` 45 MB + `SDLibrary1.dll`），UI（CefView）、设备、配置、插件宿主全耦合在一个进程里。想只换界面？做不到，只能整体重写。
2. **灯带能力其实存在，但没对你开放**。`StreamDock.pdb` 里有 `LightSpripColor` / `LightSpripBrightness` / `RGBWidget` / `lightSpripLedCounts` 这些符号，说明官方代码里就有灯带控制模块——但你的 M18 大概率没在 UI 里暴露入口，或藏在很深的设置页。而设备本身是**完全支持 24 灯珠逐颗控制**的。
3. **配置模型是"逐键拖拽"**：`manifest.json` 里明明是干净的 JSON（见 5.6），但 UI 只给了单个键的属性面板，没有批量、没有模板、没有样式复用。
4. **插件宿主是封闭的**：插件模板少不是巧合——官方只鼓励"上商店"，没提供"在宿主里直接写一段脚本"这种低门槛扩展方式。

---

## 3. 三条路线对比

| | A. 全自研宿主 | B. 基于 OpenDeck 二次开发 | C. 伴生小工具 |
|---|---|---|---|
| 做法 | 自己写设备层+应用层+插件宿主 | Fork Rust 项目 OpenDeck，加 M18 设备插件（用 `mirajazz`） | 只做灯光/宏，官方软件继续用 |
| 能否控灯 | ✅ 全部 24 灯珠 + 效果 | ⚠️ 受限于它的灯光 API | ✅ 但只能控灯 |
| 映射体验 | ✅ 完全自定义 | ❌ 被它的 UI 框死 | ❌ 不涉及 |
| 图片体验 | ✅ 可做画布编辑器 | ⚠️ 改造成本高 | ❌ |
| 插件兼容 | ✅ 可兼容 Elgato/Mirabox | ✅ 已兼容 Elgato 插件 | ❌ |
| 周期 | 6–8 周（单人全职） | 3–4 周，但天花板明显 | 1 天 |
| 风险 | 中（协议细节、兼容垫片） | 低（有现成轮子） | 极低 |

**推荐 A**，理由：你四个痛点里有三个（映射、图片、自定义程度）本质是**界面和交互问题**，而这些在 B 里恰恰是最难改的部分——fork 别人的 UI 往往比自己写更痛苦。C 不做推荐，但它是 A 的 M0。

**A 的关键杠杆**：插件宿主照抄 Elgato 协议 → 你机器上现成的 25 个 `.sdPlugin` 和 Elgato 生态大部分插件**不用改就能跑**。"插件不够多"这个问题一次性解决，而且是靠"兼容"而不是靠"自己写"。

---

## 4. 推荐架构

```
┌──────────────────────────────────────────────────────────────┐
│  UI 层  (React + TypeScript, Electron renderer)               │
│  设备画布 5×3 │ 动作库 │ 属性检查器 │ 图片编辑器 │ 灯光编辑器   │
└───────────────────────────┬──────────────────────────────────┘
                            │ IPC
┌───────────────────────────┴──────────────────────────────────┐
│  Core 层  (Electron main / Node)                              │
│                                                               │
│  ProfileStore ── 读取/写入 .sdProfile（官方格式，可双向迁移）   │
│  Renderer     ── Canvas → 64×64 JPEG，按 key 做 hash 增量        │
│  InputRouter  ── 原始码 → 逻辑键 → 短按/长按/双击/宏            │
│  ActionEngine ── 内置动作 + 脚本动作 + 插件动作                 │
│  PluginHost   ── Elgato 兼容 WS 协议 ─┬─ 隐藏 BrowserWindow(JS) │
│                                       └─ child_process(exe)    │
│  LightingEngine ─ 效果调度，与图像写共享同一条 HID 队列          │
│  DeviceService  ─ DeviceBackend 抽象                            │
└───────────────────────────┬──────────────────────────────────┘
                            │
        ┌───────────────────┴────────────────────┐
        │                                        │
┌───────┴────────────┐              ┌────────────┴──────────────┐
│ 快车道：WS 桥       │              │ 正式：transport.dll FFI    │
│ WebsocketSDK.exe   │              │ koffi 直接调 C API         │
│ :9002, JSON        │              │ 少一个进程、延迟更低        │
└───────┬────────────┘              └────────────┬──────────────┘
        └───────────────────┬────────────────────┘
                            │ HID
                     Stream Dock M18
```

**技术选型**

| 位置 | 选择 | 理由 |
|---|---|---|
| 壳 | Electron 30+ | 插件本身就是 HTML/JS，**必须有 Chromium 承载插件和它的属性检查器**；Tauri 还得再塞个 Node sidecar，反而更绕 |
| 语言 | TypeScript 全栈 | 与插件生态同语言，调试链路短 |
| UI | React 18 + Vite + Zustand | 设备画布是高频局部更新，Zustand 够用且轻 |
| FFI | `koffi` | 比 `ffi-napi` 维护活跃，支持回调，Win 预编译 |
| 图像 | OffscreenCanvas + `sharp`(兜底) | 64×64 编码量小，Canvas 足够 |
| 插件通道 | `ws` | 协议就是 WebSocket JSON |
| 打包 | electron-builder + NSIS | 顺带做开机自启 |

内存占用会比官方软件高一些（Electron 常态 150–250 MB）。如果在意，后期可以把 Core 抽成独立 headless 进程，UI 变成"可关闭的外壳"——这也是官方软件的做法方向。

---

## 5. 关键实现要点

### 5.1 设备层

官方 `transport.dll` 的 C API（MIT 许可，可随你的应用分发）已经覆盖了全部需要的操作：

```
transport_open / close
transport_set_reportSize(input, output, feature)
transport_get_firmware_version
transport_set_key_image_stream(jpeg, len, key)   // 64×64
transport_clear_key / clear_all_keys
transport_refresh                                // 批量提交
transport_set_key_brightness(0-100)
transport_set_led_brightness(0-255)
transport_set_led_color(count, r, g, b)
transport_set_single_led_color(count, colors[][3])
transport_reset_led_color
transport_read(buffer, len, timeout)
transport_sleep / wakeup_screen / heartbeat
```

封装成接口，方便将来替换实现：

```ts
interface DeviceBackend {
  open(): Promise<void>
  writeImage(key: number, jpeg: Uint8Array): void
  clearKey(key: number): void
  flush(): void                       // refresh
  setScreenBrightness(pct: number): void
  setLedBrightness(v: number): void
  setAllLeds(rgb: [number,number,number]): void
  setLeds(colors: [number,number,number][]): void
  resetLeds(): void
  onInput(cb: (code: number, state: 0|1) => void): void
}
```

**两个实现都要写，但顺序不同：**
- **快车道（先做）**：spawn `WebsocketSDK.exe`，用 JSON 走 `ws://127.0.0.1:9002`。**一行原生代码都不用写**，M1 一周内能跑起来。
- **正式版（后换）**：`koffi` 直接调 `transport.dll`，去掉额外进程。同时把 `mirajazz` 的协议实现作为第三种兜底（不依赖厂商二进制）。

> 写队列必须自己控速：实测 `can_write()` 没有背压意义，推 3000 帧也不报错。**灯光按 20–30 fps 节流，图像按"变化才写 + 一帧最多 N 个键"节流**，否则队列会越积越久，表现为"按键反应慢半拍"。

### 5.2 图像管线

- 尺寸 **64×64**，JPEG（质量 80–85），单帧约 2–5 KB。
- **边框补偿（重要）**：15 个键之间有物理缝隙，若把图直接按 320×192 等分成 15 块，画面会被缝隙"拉开"——相邻键的内容在物理上多出一段距离，观感是被撑开、切断的。
  正确做法按"视频墙"的思路：把原图缩放到 **含缝隙的总尺寸**（`5×64+4×gap` × `3×64+2×gap`），再裁掉落在缝隙上的那几条，逐键写入 64×64。
  这样跨键的直线才连续、圆才不会被压成椭圆。`gap` 是本项目的可调参数（0–64，默认 8），界面上有「校准图」，
  并实时渲染**与设备同构的预览**（键框 + 被裁掉的缝隙条），用来对照实物调参。
- 全屏 15 键刷新约 45–75 KB，按 1025 字节/包算约 45–75 个包，**估计 40–80 ms**（这个数字要你上机实测后校准）。
- **增量**：每个 key 记住上一帧 hash，只写变化的键，写完统一 `refresh`。
- 状态图：一个动作可以有多个 `States`（开/关、按下/释放），渲染器按状态取图。
- V3 固件支持**按键 GIF**（`supportKeyGif = true`），动图键可以做——这是官方软件没好好利用的能力。
- ⚠️ 480×272 的"开机 Logo"写的是 ROM，**不要实现**，也别拿它当动态背景。

### 5.3 输入

```
HID read → (readValue, state) → 逻辑键(1..18) → InputRouter
                                                  ├ 短按 / 长按(>400ms) / 双击(<300ms) / 连击
                                                  └ 交给 ActionEngine 或插件
```

- 三个实体键没有屏幕，天然适合做"层切换 / 静音 / PTT"。
- V3 固件**同时上报按下和松开**（`supports_both_keypress_states`），所以长按、双击、按住说话都能做——官方软件这块支持得也很弱。

### 5.4 插件宿主（整个方案里最关键的一块）

Mirabox 的协议就是 Elgato 的协议，连函数名都叫 `connectElgatoStreamDeckSocket`，文档里也明说了"为了兼容 Elgato 我们使用和他们一样的函数名称"。

**注册方式**（三条路都要支持）：

| 插件形态 | 启动方式 |
|---|---|
| JS/HTML 插件 | 隐藏 `BrowserWindow` 加载 `CodePath`，preload 里注入 `connectElgatoStreamDeckSocket(port, uuid, registerEvent, info)` |
| exe 插件 | `spawn(exe, ['-port',p,'-pluginUUID',u,'-registerEvent',e,'-info',json])` |
| 属性检查器 | 第二个 `BrowserWindow` + 额外的 `inActionInfo` 参数 |

插件连上来后发 `{event: registerEvent, uuid: pluginUUID}` 即完成注册，然后就是 JSON 双向收发。

**要实现的入站事件**：`didReceiveSettings`、`didReceiveGlobalSettings`、`keyDown/Up`、`dialDown/Up/Rotate`、`willAppear/Disappear`、`titleParametersDidChange`、`deviceDidConnect/Disconnect`、`applicationDidLaunch/Terminate`、`systemDidWakeUp`、`propertyInspectorDidAppear/Disappear`、`sendToPlugin`

**要实现的出站事件**：`setSettings`、`getSettings`、`setGlobalSettings`、`getGlobalSettings`、`openUrl`、`logMessage`、`setTitle`、`setImage`（PNG/JPG/GIF/**SVG**）、`showAlert`、`showOk`、`setState`、`sendToPropertyInspector`

**兼容垫片**（这些不做，插件就会零星出问题）：
- `info.devices[].size` 要报 `{columns:5, rows:3}`，有些插件靠它算坐标
- `setImage` 支持 `data:image/svg+xml;charset=utf8,...`（很多插件传 SVG）
- `openUrl`、`logMessage`（顺手写到自己的日志）
- 相对路径解析（插件常写 `images/xxx.png`）
- `localStorage` 可用（Electron 天然支持）

### 5.5 灯光引擎

硬件能力：24 灯珠、整条单色、逐颗独立、全局亮度、复位。

```
LightingEngine (目标 20–30 fps, 帧跳过)
  ├ Solid / Gradient / Breathe / Rainbow / Chase
  ├ 按键联动：按下某键 → 对应区段闪白
  ├ 音频律动：WASAPI loopback → FFT → 频带映射到 24 灯珠
  └ 场景绑定：每个 profile / 页面独立灯光
```

两个工程要点：
1. **和图像共用一条 HID 管道**，必须统一排队，优先级"图像 > 灯光"，灯光来不及就丢帧，绝不堆积。
2. 灯带是 24 颗**顺序**排列的，需要先在 UI 里做一次"点哪颗亮哪颗"的标定，否则做流动效果方向会反。

### 5.6 数据模型与迁移

官方 profile 就是干净的 JSON，目录结构：

```
<uuid>.sdProfile/
  manifest.json          # 见下
  Images/                # 内置图标
  *.png                  # 每个键一张渲染好的图
```

```json
{
  "Actions": {
    "0,0": {
      "ActionID": "…", "Controller": "Keypad", "State": 0,
      "UUID": "com.hotspot.streamdock.system.website",
      "Settings": { "path": "https://bbs.key123.vip/", "openInBrowser": true },
      "States": [{ "Image": "LI92….png", "Title": "论坛",
                   "TitleAlignment": "bottom", "TitleColor": "#ffffff",
                   "FontSize": 10, "ShowTitle": true }]
    }
  },
  "DeviceUUID": "M18V25E",
  "DeviceModel": "20GBA9901",
  "DeviceSerialNumber": "08A903782C30",
  "Name": "操作教程",
  "Pages": { "Current": "…", "Pages": ["…"] }
}
```

**直接读写这个格式**，你就能：
- 首次启动**一键导入**你现在的全部配置（无需重配）
- 随时**导出回官方格式**，想回退就回退

### 5.7 动作引擎

自研宿主最大的好处：**大部分常用动作不需要插件进程**，直接内置。

内置动作集（覆盖日常 80%）：热键/组合键、输入文本、打开程序/URL/文件夹、多媒体控制、音量、窗口切换、切换 profile/页面、屏幕亮度、灯光场景、延时、宏序列、OBS WebSocket、HTTP 请求、运行脚本。

再加一个杀手锏——**脚本动作**：在宿主里直接写一段 JS，暴露 `setImage/setTitle/fetch/exec/http` 等 API。官方"插件模板不够多"的本质是"做一个小功能也得建一个完整插件工程"，脚本动作把这个门槛降到 10 行代码。

---

## 6. 四个痛点 → 具体设计

| 你的痛点 | 设计 | 具体做什么 |
|---|---|---|
| **无法控制灯光** | 独立灯光页 | 24 灯珠可视化（点哪颗亮哪颗）、效果库、亮度、按键联动闪白、音频律动、按 profile 绑定；已用官方 SDK 实测打通 |
| **按钮映射不方便** | "按下即定位" | 直接按设备上的键 → 右侧立刻打开该键编辑器；热键录制器直接抓真实组合键；动作库拖拽；多选批量；右键"复制到…"；**短按/长按/双击**三套动作 |
| **图片设置不便** | 内置画布编辑器 | 图标库（Iconify/Lucide）+ 文字 + 背景色/渐变 + 圆角 + 状态徽标；模板一键套用；批量应用样式；文件直接拖到键上；GIF 动图键；右侧实时预览真机效果 |
| **插件少、自定义低** | 兼容 + 开放 | Elgato/Mirabox 插件直接跑（现成 25 个可用）+ 内置动作集 + 脚本动作 + 插件脚手架 CLI（`m18 plugin new` 生成可运行模板）+ 主题定制 |

---

## 7. 里程碑

| 阶段 | 内容 | 产出 | 工作量 |
|---|---|---|---|
| **M0** | 设备层打通 + 控制台 UI（灯光/按键/设备） | ✅ 已交付，见第 9 节 | ✅ 已完成 |
| **M1** | 按键渲染 + 静态 profile + 内置动作 | 能替代日常使用：15 键显示图标、按下去执行热键、亮度可调 | 1–2 周 |
| **M2** | 插件宿主 + 属性检查器 + 官方 profile 导入 | 现有 25 个插件可用，配置不用重配 | 2–3 周 |
| **M3** | 灯光引擎 + 灯光编辑器 | 效果库、按键联动、音频律动 | 1 周 |
| **M4** | 图片编辑器 / 模板 / 脚本动作 | 拖拽设图、批量、10 行脚本做新功能 | 2 周 |
| **M5** | 打包、自启、更新、崩溃恢复 | 可发给别人用 | 2–3 天 |

M1 完成就"能用"，M2 完成就"不比官方差"，M3/M4 才是"比官方好"。

---

## 8. 风险与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| `transport.dll` 是黑盒，未来 ABI 可能变 | 升级即挂 | 抽象 `DeviceBackend`；同时维护 `mirajazz` 协议实现作兜底；锁定 SDK 版本 |
| 开机 Logo 写 ROM 可能变砖 | 设备报废 | **不实现该功能**，UI 里也不放 |
| 与官方软件抢设备 | 画面闪烁、灯光被覆盖 | 检测 `StreamDock.exe` 运行中则提示"二选一"；固件升级仍走官方软件（保留安装） |
| Elgato 插件不是 100% 兼容 | 个别插件报错 | 建兼容性清单 + 垫片；失败时降级为"仅 setImage/setTitle" |
| 无背压，写太快堆积 | 按键延迟 | 自建调度器：灯光 20–30 fps、图像增量、队列长度上限 |
| HID 吞吐未知 | 全屏刷新慢 | M1 第一件事就是实测 15 键全刷耗时，据此定刷新策略 |
| 法律/分发 | — | SDK 是 MIT，可随应用分发；**不要**再分发官方软件本体或其资源文件 |

---

## 9. 现在就能做的

### 9.1 打开控制台（图形界面）

**双击桌面上的「Stream Dock M18 控制台」** 即可。

也可以双击项目里的 `启动 M18 控制台.bat`。

它会做三件事：启动本地 Core 服务（`127.0.0.1:8642`）→ 用 Edge/Chrome 的**应用窗口模式**打开 UI（没有地址栏和标签页，看起来就是一个桌面应用）→ 自动连接 M18。

界面分三个页签：

| 页签 | 能做什么 |
|---|---|
| **灯光** | 24 颗灯珠**按设备一圈排布**（外围 22 + 正面 2），支持拖动连续上色、7 种效果、亮度/速度、起点与方向校正 |
| **按键** | 5×3 布局图，按键实时高亮；**点键换图片或 GIF**（GIF 由主机侧逐帧推送循环）；输入事件日志 |
| **设备** | **画面预览**（与设备同构，标出被缝隙吃掉的像素）、间隙补偿、**选择壁纸**、校准图、上下翻转、设备信息、退出控制台 |

顶部常驻：屏幕亮度滑块、连接状态。检测到官方软件在跑时会显示黄色提示条。

> 连接设备后会自动把 `app/assets/wallpaper.jpg` 裁成 320×192（5×3 个 64×64）铺满按键阵列。
> 注意：**设备不存储画面**，官方软件退出后黑屏就是因为没人再给它画图；画面必须由宿主进程持续接管。

> 控制台是"单实例"的：重复双击只会把已有窗口唤到前台，不会启动第二个进程抢设备。
> 关掉窗口不等于退出服务——要彻底退出，用「设备 → 退出控制台」。

这套 UI 是纯静态 HTML/CSS/JS，Core 通过 HTTP + SSE 与它通信；**M1 包进 Electron 时这套界面可以直接复用**。

### 9.2 命令行工具（调试用）

```powershell
$py = "C:\Users\well\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

& $py .\probe_readonly.py          # 只读探测：设备、固件、灯带能力
& $py .\m18_light.py --effect rainbow --fps 25
& $py .\m18_light.py --effect chase --color 00ff88 --speed 6
& $py .\m18_light.py --effect off
```

### 9.3 项目结构

```
streamdock-re/
├─ 启动 M18 控制台.bat        # 启动入口（找 python 并拉起服务）
├─ app/
│  ├─ server.py               # Core：设备/HID/灯光引擎/HTTP+SSE
│  ├─ ui/                     # UI：index.html + style.css + app.js
│  ├─ icon.ico                # 图标
│  └─ server.log              # 无控制台启动时的日志
├─ m18_light.py               # 命令行灯光工具
├─ probe_readonly.py          # 只读设备探测
└─ refs/                      # 官方 SDK 与参考实现（依赖来源）
```

### 9.4 下一步（M1）

Electron 外壳 → 按键图片渲染（64×64 JPEG）→ 读官方 `.sdProfile` → 内置热键动作。做完就能卸掉官方软件日常用。

---

## 附录：参考资源

- 插件 SDK 文档：<https://sdk.key123.vip/guide/overview.html>
- 插件模板仓库：<https://github.com/MiraboxSpace/StreamDock-Plugin-SDK>
- **设备级 SDK（本方案的基础，MIT）**：<https://github.com/MiraboxSpace/StreamDock-Device-SDK>
- 设备 SDK 文档（C++ / Python / WebSocket）：<https://creator.key123.vip/python/overview.html>
- 第三方逆向协议库（兜底用，含 N3/N4/293 系列）：<https://github.com/4ndv/mirajazz>
- 早期逆向示例（参考价值有限，M18 无需再用）：<https://github.com/rigor789/mirabox-streamdock-node>
