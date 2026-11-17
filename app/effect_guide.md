# M18 浏览器灯效开发指南（M18FX v1）

一个灯效同时接管 15 个屏幕和 24 颗 LED，并收到 1–18 键的按下/松开事件。灯效写好后保存进**灯效库**（控制台左侧「灯效」分类）；**灯效场景**由若干「页」组成，每页引用灯效库里的一个灯效，Dock 上 ◀ ▶ 长按切换。灯效场景的按键默认：屏幕键短按、双击留空（全部交给灯效互动），长按切下一个场景；◀ ▶ 长按切上一/下一灯效页。用户可以在控制台改这些按键动作，但灯效代码只负责画面和灯珠，不负责这些。代码是浏览器 JavaScript，只在独立后台浏览器的 Worker 中执行，不在 Python、Node 或控制台 UI 窗口执行。关闭控制台主窗口后，后台宿主仍持续运行；退出灯效或退出程序才停止。

## 一、坐标与显示速度

- 整板画布 `1000×740`，原点左上，x 向右、y 向下。按键、导航键、灯珠均使用 `core.KEY_POINTS / LED_POINTS` 的同一真实几何坐标。
- 屏幕键 1–5 是底行，6–10 是中行，11–15 是顶行。导航键是 16、17、18。
- `keys` 是 18 个几何对象；`screen=true` 的 15 个方形区域会缩放为设备单键 `64×64`。
- `leds` 是 24 个几何对象，`index` 从 0 开始，顺序与控制台灯珠一致；发送前仍应用控制台的硬件 offset/reverse 映射。
- **15键并行写屏（默认）**：官方 SDK 每写完一张键图都在**该 WebSocket 连接里固定 sleep 50ms**，同一连接的命令严格串行，所以单连接写 15 键要约 770ms、固件按键号自下而上一张张出现（分层错位的根源）。sleep 只阻塞自己那条连接：设备层为每个键固定开一条写图连接（同一键先后顺序不变，按键/灯珠仍走主连接；普通切场景、相册轮播的写图也走这套连接），每批末尾带 `gifLoopStatus` 作为该连接"已处理完"的回执。后端等上一批 15 条连接都回执后才发下一批（只发最新快照、不在 SDK 里排队），整板实测约 57ms、约 17 整板/秒，上限 20fps；宿主预算相应放宽到每秒 300 张。SDK 不支持 `gifLoopStatus` 或开不出额外连接时回退到下面的单连接预算模式。退出灯效先等写图连接写完再恢复原按键。M18 固件 V3.M18V3_E_HXJDF.03.021 实测 `setTemporaryBackgroundImgData` 整屏背景不显示、`setBackgroundGif` 返回 operation not supported，不能用整屏帧。
- Worker 最多约 **25fps**，主窗口整板 JPEG 预览编码/读取约 **10fps**（双缓冲拉取，与状态轮询解耦）。LED 独立最多 **25fps**，只由最新颜色线程写入；屏幕批次不能把旧快照颜色回灌。快速图片批次内暂时让位，批次之间继续响应。最新颜色变全黑时同时清除单珠缓存与全灯颜色，约120ms、350ms后补发，稳定全黑时低频1秒补发一次；这不是硬件回执。新效果开始或退出灯效时停止清黑补发，不改变全局亮度/原场景。
- **单连接回退模式：屏幕共享每秒最多 60 张 JPEG 的总预算**，保持原全板 4×15 的传输量不增加。预算等待放在批次之间，不能在15个键之间强制等待16.7ms制造扫屏。整份变化集必须来自同一张画布快照，预算不足就等下一帧，不能拆成不同时间的几行；前端仅在后端接受之后更新像素缓存，慢上传时不混合旧待发帧。后端同一批次固定快照，图片/一次refresh使用一份有界socket写入（多条合法WebSocket消息，不是JSON数组）。这不是硬件双缓冲：SDK没有显示回执，仍可能存在HID/面板扫描延迟。只有中排变化时一般约10–12次/屏/秒，全15屏变化仍约4次/屏/秒，实际取决于主机/SDK/设备。切换场景/设备会话取消旧任务，不累计帧队列。
- SDK 没有 ACK 和背压，返回成功只表示命令已发送，不保证设备已显示完成。屏幕实时音频响应受上述节流限制，不能达到预览的 20fps。这里使用动态 JPEG，**不是用原生 GIF 假装实时音频动画**；进入灯效时停止普通原生 GIF，退出由场景集成恢复。

## 二、完整 API

`M18FX` 是只读 API 对象。几何数组和音频快照只读；画布和 2D 上下文可以绘制。不要改变画布尺寸。

```javascript
M18FX.version                 // 1
M18FX.width                   // 1000
M18FX.height                  // 740
M18FX.canvas                  // OffscreenCanvas，固定 1000×740
M18FX.ctx                     // OffscreenCanvasRenderingContext2D
M18FX.keys                    // [{key:1..18,x,y,size,screen:boolean}, ...]
M18FX.leds                    // [{index:0..23,x,y}, ...]
M18FX.audio                   // 每次 tick 更新的只读快照
// { level:0..1, bands:[8个0..1], available:boolean, status:string }

const off = M18FX.on('key', e => {
  // e={key:1..18, down:boolean, x:number, y:number, time:number}
  // time 是从会话开始计算的毫秒；down=true 为按下，false 为松开。
});
off();                        // 取消本处理器；每种事件最多 32 个处理器

M18FX.on('frame', e => {
  // e={time:number, dt:number, frame:number}
  // time/dt 单位是秒，frame 从 1 递增；dt 最大 0.25。
  // 本 tick 的 key 事件先派发，再派发 frame；此时 audio 已更新。
});

M18FX.render();               // 提交当前画布，并在每颗灯珠位置采样 3×3 平均 RGB
M18FX.render(colors);         // colors 必须是 24 组 [r,g,b]，按 leds 顺序
// RGB 为有限数，运行时四舍五入并限制至 0..255。
// 同一次 frame 多次 render，只提交最终画布和最后一次的颜色。

M18FX.log('消息', {任意: '可JSON序列化的值'});
// 不显示会话 token；每帧最多 16 条，宿主缓存最多 64 条，后端最多 16 条/秒。
// 后端环形日志最多 200 条、每条最多 2048 字符。
```

所有事件处理器必须是**同步函数**，不得返回 Promise。使用 `on('frame')` 而不是自建定时器，不用 `requestAnimationFrame`。每一帧从头绘制整张画布：提交使用 `transferToImageBitmap()`，提交后原画布会清空；需要残影时保留你自己创建的有界辅助 OffscreenCanvas，或者用有界状态重画。

`render()` 只声明本 tick 要提交。进入时引擎先用同一有界管线接管为黑底，因此没有调用 render 的代码也不会让旧 GIF 继续占屏。没有调用时仍有会话心跳，但不产生新的用户画面。请在 `frame` 中绘制和调用 `render()`，不要仅在初始化或 key 回调中提交。

## 三、电脑播放音频，不是麦克风

采集器使用 Windows WASAPI **默认 eRender/eMultimedia 播放端的 loopback**，通常涵盖这台电脑正在播放的音乐、视频、游戏等声音。没有麦克风回退、没有 `getUserMedia`、没有第三方采集库。

只有灯效激活才初始化采集线程和 COM；停止灯效/退出会释放该线程自己的 COM 接口、停止音频客户端并清空临时样本。未激活时不采集。默认播放设备改变、设备断开或音频服务暂不可用时，清空能量并重建播放客户端。设备变化约每秒检查一次，不保证无缝切换。

`audio.level` 是经过显示增益的归一化 RMS，`bands` 是 2048 点 Hann/FFT 的八段归一化能量，并非精密测量仪。频段依次为：

| bands 索引 | 频率范围（Hz） |
|---|---|
| 0 | 20–60 |
| 1 | 60–150 |
| 2 | 150–400 |
| 3 | 400–1000 |
| 4 | 1000–2500 |
| 5 | 2500–6000 |
| 6 | 6000–12000 |
| 7 | 12000–24000（受采样率/Nyquist 限制） |

低频分辨率受采样率/2048 限制；输入是混合声道能量的近似显示，不是逐声道 PCM。安静时电平/频段为零。通常约 20 次/秒更新，延迟也受 Windows 播放缓冲影响。

`status`：`stopped`、`starting`、`ok`、`silent`、`unavailable`、`recovering`。`silent` 时 `available=true`，表示有播放端但没有声音；`unavailable/recovering` 时为 false。即使无音频，按键波纹仍可正常工作。

**对浏览器仅输出 level、8 bands、available、status。没有 PCM API、磁盘录音、持久 PCM 缓存或音频外传。** 只有采集线程短暂的播放包和最多 2048 个降混浮点样本驻留 RAM，用于计算后丢弃；停止时清空。不提供音频文件保存或联网功能。

## 四、可以直接复制的最小例子

全屏青色随声音变亮，按下任一屏幕/导航键暂时变橙，松开恢复。屏幕和 LED 都接管。

```javascript
const {ctx, width, height, leds} = M18FX;
let held = false;
M18FX.on('key', e => {
  held = e.down;
  M18FX.log('按键', e.key, e.down ? '按下' : '松开');
});
M18FX.on('frame', () => {
  const energy = M18FX.audio.available ? M18FX.audio.level : 0;
  const brightness = Math.round(25 + 180 * energy);
  const color = held ? [255, 90, 20] : [0, brightness, brightness];
  ctx.fillStyle = `rgb(${color.join(',')})`;
  ctx.fillRect(0, 0, width, height);
  M18FX.render(leds.map(() => [...color]));
});
```

## 五、可复制的音频波纹例子

深色科幻线条加闪烁星空（固定种子，尖峰型闪烁带十字星芒），音频驱动横穿三排屏幕的三股叠加波形（快起慢落，静音时是直线）；按下任一键，以真实空间距离向屏幕与灯圈扩散同色彩色块。没有扩散时灯珠全黑，所有列表有上限，扩散会自动渐隐。

```javascript
const {ctx, width, height, keys, leds} = M18FX;
const waves = [];
let now = 0;
const clamp = v => Math.max(0, Math.min(1, v));
const middle = keys.filter(k => k.screen && k.key >= 6 && k.key <= 10);
const lineY = middle.reduce((sum, k) => sum + k.y, 0) / middle.length;
const left = Math.min(...middle.map(k => k.x - k.size / 2)) + 8;
const right = Math.max(...middle.map(k => k.x + k.size / 2)) - 8;
function rgb(h, v) {
  return [0, 2 / 3, 1 / 3].map(offset => {
    const x = ((h + offset) % 1) * 6;
    return Math.round(255 * v * clamp(Math.abs(x - 3) - 1));
  });
}
M18FX.on('key', e => {
  if (!e.down) return;
  waves.push({x:e.x, y:e.y, start:e.time / 1000, hue:(e.key * 0.137) % 1});
  if (waves.length > 8) waves.shift();
});
function waveAt(x, y) {
  let value = 0, hue = 0.55;
  for (const w of waves) {
    const age = Math.max(0, now - w.start), radius = age * 650;
    const distance = Math.hypot(x - w.x, y - w.y);
    const v = Math.exp(-(((distance - radius) / 65) ** 2)) * clamp(1 - age / 1.8);
    if (v > value) { value = v; hue = w.hue + distance / 1800; }
  }
  return {value, hue};
}
M18FX.on('frame', ({time}) => {
  now = time;
  while (waves.length && now - waves[0].start > 1.8) waves.shift();
  const audio = M18FX.audio;
  ctx.fillStyle = '#030713'; ctx.fillRect(0, 0, width, height);
  for (const key of keys.filter(k => k.screen)) {
    const half = key.size / 2;
    ctx.strokeStyle = '#124b68';
    ctx.strokeRect(key.x - half + 3, key.y - half + 3, key.size - 6, key.size - 6);
  }
  ctx.strokeStyle = '#38bfe6'; ctx.beginPath();
  for (let i = 0; i <= 96; i++) {
    const x = left + i * (right - left) / 96;
    const band = audio.available ? (audio.bands[Math.min(7, Math.floor(i * 8 / 97))] || 0) : 0;
    const y = lineY + Math.sin(time * 13 + i * 0.48) * (band * 27 + (audio.available ? audio.level * 10 : 0));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();
  if (waves.length) for (let y = 82; y < 690; y += 24) for (let x = 82; x < 930; x += 24) {
    const w = waveAt(x, y);
    if (w.value < 0.015) continue;
    ctx.fillStyle = `rgb(${rgb(w.hue, w.value).join(',')})`;
    const size = 8 + w.value * 13;
    ctx.fillRect(x - size / 2, y - size / 2, size, size);
  }
  const colors = leds.map(led => {
    const w = waveAt(led.x, led.y);
    const color = w.value >= 0.015 ? rgb(w.hue, w.value) : [0, 0, 0];
    ctx.fillStyle = `rgb(${color.join(',')})`;
    ctx.fillRect(led.x - 5, led.y - 5, 10, 10);
    return color;
  });
  M18FX.render(colors);
});
```

随包的 `ui/effect-pad.js` 是上述思路的完整默认版本，还包含空间网格和预览灯珠。未编辑过的旧示例会按代码指纹安全升级；手工修改过的代码不会覆盖，可手动载入新示例。用 `load_effect({name:'名称', code:null})` 可加载默认版本。

## 六、安全边界与错误处理

- 独立本机插件 origin，不能放在控制台 UI origin。宿主只执行可信 `effect-runtime.js`，用户代码只由专用 Worker 编译运行。
- 会话使用每次启动随机的新 token；只在宿主 URL fragment/可信请求的 Authorization 中出现，不放 query，不进入 Worker、用户日志、SSE 或状态 JSON。宿主读取后立即移除 fragment。其它本机有权限读取进程命令行的软件仍可看到启动 fragment，这不是操作系统级秘密保管。
- Worker 响应头必须使用 `effect_asset()` 返回的 CSP：`default-src 'none'; script-src 'unsafe-eval'; connect-src 'none'; worker-src 'none'; child-src 'none'; base-uri 'none'`。没有 `self` 脚本来源，dynamic import、importScripts 和远程脚本均被阻止；`unsafe-eval` 只用于在这个 Worker 编译用户 JS。
- Worker 不开放 fetch、XHR、WebSocket、EventSource、WebTransport、嵌套 Worker、定时器或 postMessage API。真正的网络限制来自 CSP，不是只靠覆盖几个 JS 名字。
- 不开放 DOM、控制台 localStorage、文件系统、Node、麦克风、USB 或插件网络代理。已有插件 API `M18.http()` 不属于灯效 API。
- 初始化超过 2 秒、单个 tick 超过 750ms，可信宿主会 terminate Worker。后端另有 10 秒启动看门狗和 3 秒画面/轮询失联看门狗；宿主页/浏览器崩溃也会停止。
- 异常、未处理 Promise 拒绝、异步处理器或协议错误都会停止，不自动无限重启。运行时栈包含 `m18-effect-user.js`，能定位的异常会附「代码第 N 行」；语法错误不保证浏览器提供精确用户行号。
- 这是浏览器执行隔离和有界看门狗，不是专门抵抗浏览器漏洞或恶意超大内存分配的操作系统沙箱。写正常有界绘图代码，不创建巨大画布/数组、无界事件处理器或死循环。

## 六·五、灯效库、多页灯效场景与智能体工具

- 灯效库：`数据目录/effects/<名称>.json`（`{name, description, code}`），缩略图 `effects/.thumbs/`（运行约 2.5 秒后自动截整板预览），排序 `effects/.order.json`。
- 灯效场景文件：`{"sceneType":"effect","effect":{"items":[灯效名…],"index":当前页,"description"},"pages":[{按键层}]}`。旧版本「一个场景一段 code」在启动时自动拆成「同名灯效库条目 + 一页的场景」，代码不丢；未改动过的历代打击垫示例会升级成当前版本。
- 按键层：`pages[0]` 的 `actions`（短按）/`gestures`（long/double）只在该场景运行时生效，不改底层普通场景；相册场景同样有一层（屏幕键默认单击下一场景，◀ ⌂ ▶ 上一张/第一张/下一张）。两类场景的按键都不能放图片 / 图标。
- 导航动作新增 `上一灯效页` / `下一灯效页`（`{"type":"nav","name":"下一灯效页"}`）。
- HTTP `/api/effects`：`list/get/save/create/delete/reorder/run`（灯效库，`run` 是试运行，不改任何场景）、`scene/pageAdd/pageRemove/pageMove/pageGoto`（某个灯效场景的页）、`stop/status/logs/guide/revealGuide`。
- MCP：`m18_save_effect`（存进灯效库）→ `m18_run_effect`（试运行）→ `m18_effect_input` / `m18_view_effect` / `m18_effect_logs` → `m18_stop_effect`；`m18_effect_scene` 查看/编辑场景的页；`m18_list_effects` / `m18_get_effect` / `m18_delete_effect` 操作灯效库。
- 「打开技术文档」把本文写到 `数据目录/灯效开发文档/M18FX 灯效技术文档.md`（附「示例-打击垫.js」）并在资源管理器中选中，方便直接拖进其他智能体的对话框。

## 七、App 生命周期对接（源码开发者）

引擎不负责持久 CRUD、场景文件、HTTP 服务器注册或 UI。Mixin 方法与兼容接口如下：

```python
app.init_effects()                       # App __init__；别名 _init_effects()
app.start_effects(plugin_origin)          # 插件 HTTP 监听成功后；此时不采集音频
# plugin_origin 如 http://127.0.0.1:<独立插件端口>，必须和控制台 origin 不同

app.load_effect({"name": name, "code": code})  # 返回状态；code=None 用默认示例
app._start_effect_scene(name, code)       # 兼容场景层，启动成功 True，宿主启动失败 False
app.effect_active()                      # 当前是否接管
app.effect_input(key, down)               # key 为1..18整数，down必须bool；已消费返回True
# SDK上报0/1时必须 effect_input(key, bool(state))，模拟输入同样发送按下与松开。
app.effect_state()                       # 不含token/code/PID；见下文
app.effect_logs(since=0, limit=50)         # [{seq,time,level,message}]，since为日志seq
app.effect_preview()                     # 全板500×370 JPEG bytes或None
app.effect_preview(key)                  # 单键64×64 JPEG bytes或None
app.effect_repaint()                     # 同session唤醒/旋转/映射变化，清JPEG/LED去重缓存
app.effect_guide()                       # 纯中文文档字符串；模块effect_guide()也可用

app.stop_effect(restore=True)             # 停token/宿主/音频，重画底层普通按键
app._stop_effect_runtime(restore=False)   # 场景切换用，避免旧场景额外重画
app.close_effects()                       # 程序退出，停线程/宿主/音频，不重画设备
```

进入前由场景层停止相册（含相册原生 GIF），并使普通渲染/相册后台/待机/导航动作让位。引擎首个屏幕批次会停止跟踪中的普通 GIF 和 SDK GIF loop。相册/普通配置不由引擎修改，故退出可按原场景恢复。

激活时 LED 由独立 25Hz 写线程接管；`LightingMixin._engine_loop` 只读取/广播灯效颜色，不再写旧灯效（避免用 30Hz tick 做 40ms 限流时退化成 15Hz）。`_compute` 和 `snapshot.effect` 返回灯效状态。所有设备写仍尊重 `quiet/yielded/writes_paused/device.connected`，屏幕还尊重 `screen_off/lighting_only` 和 `_paint_lock/_pushing`。原场景的插件推图在灯效期间暂停，防止时钟等后台插件覆盖灯效屏幕；退出后恢复。设备 session 改变自动使 JPEG/LED 缓存失效；同 session 息屏后唤醒必须调用 `effect_repaint()`。

故障时引擎停止后，如场景层实现 `_effect_runtime_failed(reason)`，会调用该 hook 代替 `apply_keys()`；没有 hook 的离线/简化 App 才直接重画底层普通按键。**场景名称和持久化由场景层负责**：hook 应切回 `_effect_return` 或明确保留失败状态，不能在 `_restore_display` 中每次唤醒自动重启相同坏代码。正常场景切换先停灯效 runtime（`restore=False`），再恢复目标场景。关闭主窗口到托盘不要调用 `close_effects()`；真实程序退出/让位关闭才调用。

后台浏览器使用新的临时 profile，`--headless=new`；日志不输出命令行。Windows 优先把本次 Popen 绑定到专属 Job Object（KILL_ON_JOB_CLOSE），关闭时只收自己的浏览器子树；不支持 Job 时回退到该 Popen 的精确 PID 树。不按 Edge/Chrome 进程名清理，不碰用户浏览器和已有服务。

状态示意：

```json
{"active":true,"name":"灯效 1","status":"running","revision":2,"previewRev":30,
 "audio":{"level":0.4,"bands":[0,0,0.2,0.1,0,0,0,0],"available":true,"status":"ok"},
 "fps":{"preview":10,"screens":20,"leds":25,"fullBoard":15,"imageBudget":60},
 "parallel":true,
 "metrics":{"frames":30,"dropped":2,"images":40,"leds":15}}
```

`revision` 是会话代次，`previewRev` 是当前提交序号。主源建议提供 `/api/effect/preview.jpg` 代理 `effect_preview()`，以最多 4fps 获取全板预览；无图返回 204。不要把凭证或用户 JS 放入主窗口执行。SSE `type:'effect'` 只发无 token 状态，LED 颜色仍使用已有 `type:'frame'`。`frames/dropped` 是接收/丢旧统计，不是设备 ACK 数。

## 八、专用 origin 路由协议

静态 GET：`/effects/host`、`/effects/effect-runtime.js`、`/effects/effect-worker.js`。调用模块 `effect_asset(path)` 得到 `(bytes, MIME, headers)`，必须应用返回的所有响应头（尤其 Worker CSP），未知返回 None。宿主由引擎自动启动，无需主窗口 iframe。

动态接口统一调用：

```python
app.effect_host_request(method, path, token, body=None)  # 返回JSON兼容对象
# PermissionError -> 403；ValueError/TypeError -> 400；不打印token或Authorization
```

| 方法 / 路径 | 数据 |
|---|---|
| GET `/effects/session` | 返回 `{code,width:1000,height:740,keys:[{key,x,y,size,screen}],leds:[{index,x,y}],fps:25,screenFps:20,imageBudget:60}`；poll 另返回 `parallel`（并行写屏时宿主预算放宽） |
| GET `/effects/poll` | 返回 `{events:[{key,down,x,y,time}],audio:{level,bands,available,status},time}`；事件被消费，time单位毫秒 |
| POST `/effects/frame` | `{seq,colors:[[r,g,b]×24],keys?:{"1":"data:image/jpeg;base64,...",...},screenSeq?:同一屏幕快照编号,screenColors?:配对24组RGB,preview?:"data:image/jpeg;base64,..."}` |
| POST `/effects/log` | `{messages:[字符串,...]}` |
| POST `/effects/error` | `{message:字符串}`；停止该会话，不自动重启 |

frame 的 seq 是递增正整数；屏幕键仅 1–15、每张 JPEG 固定 64×64 且解码后最多 20KiB；preview 固定 500×370 且最多 160KiB；LED 必须为 0–255 整数三元组；最多接收 30 个请求/秒（宿主按 25fps 发）。keys 缺省表示保留之前屏幕图；可只提交变化的键，但一整份变化集必须是同一画布时刻，未提交的键应与该快照像素相同。screenSeq应不大于seq且不倒退，screenColors保留该屏幕快照颜色用于诊断，不写入硬件灯珠；硬件LED只采用最新colors。旧客户端省略时用seq/colors。可信宿主做同帧像素差分与60张/秒预算，后端做批次间节流，不能通过频繁调用绕过预算。成功返回 `{ok:true,seq}`。

所有动态接口只认 `Authorization: Bearer <当前token>`，不接受 query token、场景 id 或插件 id。路由必须验证 Host 是绑定的 loopback origin；拒绝跨 origin 的 Origin / Sec-Fetch-Site 请求；POST 必须同专用源 Origin 与 `Content-Type: application/json`，JSON body <= 模块 `MAX_EFFECT_BODY`（512KiB）。GET fetch 通常没有 Origin，因此结合 Host、token 和 Sec-Fetch-Site。不给 CORS 放行，所有响应 no-store。无需也不能把 token 交给主源 API 或 Worker。

## 九、免安装宿主诊断

资源管理器可能把 Windows PCA 的 `__COMPAT_LAYER=DetectorsAppHealth` 传入控制台，后台 Edge 继承后会立即以退出码 0 结束。本程序只在创建灯效浏览器子进程时剔除该变量，不修改主程序环境、兼容性注册表或用户浏览器设置。

可用 `M18Console.exe --check-effect-host <临时结果JSON路径>` 检查真正的 exe 父进程能否启动宿主。测试设 `M18_DATA_DIR` 为临时目录；它不会构造设备 App、不连接 SDK、不采集音频。结果包含 `ok/status/frames`，不会输出会话凭证。不要仅从 Python 执行冻结字节码来替代此测试。

## 十、离线测试

```text
python -B -m unittest discover -s tests -p "test_effect*.py" -v
```

`tests/test_effects.py` 用假设备和假采集器，覆盖队列/帧大小/60张图片预算/同批固定快照/重连/LED最新颜色唯一写入/结束双重清零与补发/旧插件推图暂停/COM所属线程/默认播放端恢复/八频带。它只执行 core 的纯几何函数 AST，不导入会创建用户数据目录的 core/server。

`tests/test_effect_runtime.py` 在临时随机端口起测试 HTTP、临时 profile 起真实 Edge/Chrome，并用标准库 CDP/WebSocket 验证 Worker、完整 JPEG、导航波纹、假音频、realm 隔离、网络及 dynamic import CSP、异常/语法错误/异步处理器、初始化和帧死循环退出。不存在浏览器时明确 skip。设备与音频都仍是假对象；不接真实 Stream Dock、不采集真实播放端、不调用现有控制台服务、不改用户数据。仅关测试自己创建的 PID。
