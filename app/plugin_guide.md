# M18 自制插件开发指南（给人和 AI 智能体看）

自制插件是一段跑在后台浏览器（无头 Edge）里的 JavaScript。控制台把它包装成标准 Stream Dock / Elgato
`.sdPlugin`，存在 `%APPDATA%\M18Console\custom-plugins\`（源码运行时是 `app/custom-plugins/`）。
**只写函数体里的代码**——运行时、manifest、图标都由控制台生成；保存即热重载。

## 开发循环（推荐）

1. `m18_save_custom_plugin` 保存代码 → 返回插件 id（再次保存同一 id = 更新）
2. `m18_bind_key` 把插件绑到某个键（1–15，左下角是 1，顶行 11–15）
3. `m18_press_key` 模拟按一下 → 返回这次按键产生的插件日志
4. `m18_view_keys` 看键面画面（图片）；`m18_plugin_logs` 看日志 / 报错（报错带「代码第 N 行」）
5. 改代码再保存，回到 3

设备没连上、官方软件占着设备时，以上流程照样能跑：画面在控制台里渲染，`m18_view_keys` 能看到。

## 事件

```js
M18.on("ready", () => {});                    // 插件进程连上控制台
M18.on("willAppear", (key) => {});            // 键出现在屏幕上（绑定、翻页、切场景后都会触发）
M18.on("willDisappear", (key) => {});         // 键离开屏幕（key.every 的定时器自动清掉）
M18.on("keyDown", (key) => {});               // 按下（长按 / 双击归插件自己判断）
M18.on("keyUp", (key) => {});                 // 松开
M18.on("didReceiveSettings", (key) => {});    // 设置变了（key.settings 已更新）
```

同一个插件可以绑在多个键上，每个键是独立的 `key` 对象，各自有 `settings`。

## key 对象

| 成员 | 说明 |
|---|---|
| `key.setTitle(text)` | 键面文字（`\n` 换行，2 行内最好） |
| `key.setImage(dataUrl)` | 键面图片：`data:image/png;base64,…` 或 `data:image/svg+xml,…`；空串恢复默认图标 |
| `key.draw((g, size) => {…}, size = 144)` | 用 Canvas 2D 画键面，`g` 是 `CanvasRenderingContext2D` |
| `key.every(ms, fn)` | 定时刷新（≥100 ms），键消失时自动停 |
| `key.settings` / `key.save(obj)` | 每个键自己的持久化数据（合并保存，重启后还在） |
| `key.setState(0/1)`、`key.state` | 双状态 |
| `key.showOk()` / `key.showAlert()` | 键面闪一下 ✓ / ⚠ |
| `key.coordinates` | `{column, row}` |
| `key.context` | 键的唯一标识 |

## M18 全局函数

| 函数 | 说明 |
|---|---|
| `M18.log(...args)` | 写日志（`m18_plugin_logs` 能拉到；对象自动转 JSON） |
| `M18.http(url, {method, headers, body})` | **联网请用它**：由控制台代发，不受跨域限制；返回 `{ok, status, text, json(), headers}`；15 秒超时、最大 2 MB、只支持 http/https |
| `M18.openUrl(url)` | 用默认浏览器打开网址 |
| `M18.hotkey("ctrl+shift+s")` | 发送热键（键名：ctrl/shift/alt/win、a–z、0–9、f1–f24、enter、esc、tab、space、up/down/left/right、home/end、pageup/pagedown、backspace、delete、insert、printscreen、mediaplaypause、volumeup…） |
| `M18.launch(path)` | 打开程序 / 文件 / 文件夹 |
| `M18.type(text, enter=false)` | 往当前窗口输入文本（支持中文） |
| `M18.media(action)` | `playpause` `next` `prev` `stop` `mute` `volup` `voldown` |
| `M18.sleep(ms)` | `await M18.sleep(500)` |
| `M18.keys()` | 当前插件在屏幕上的所有键 |

## 示例

计数器：

```js
M18.on("willAppear", (key) => key.setTitle(String(key.settings.n || 0)));
M18.on("keyDown", (key) => {
  key.save({ n: (key.settings.n || 0) + 1 });
  key.setTitle(String(key.settings.n));
});
```

时钟（Canvas）：

```js
function paint(key) {
  key.draw((g, s) => {
    g.fillStyle = "#111"; g.fillRect(0, 0, s, s);
    g.fillStyle = "#fff"; g.font = "bold 40px sans-serif"; g.textAlign = "center";
    g.fillText(new Date().toTimeString().slice(0, 5), s / 2, s / 2 + 14);
  });
}
M18.on("willAppear", (key) => { paint(key); key.every(1000, paint); });
```

网络数据：

```js
async function refresh(key) {
  try {
    const r = await M18.http("https://api.github.com/repos/microsoft/vscode");
    key.setTitle("★\n" + (r.json().stargazers_count / 1000).toFixed(0) + "k");
  } catch (e) { M18.log("请求失败", e.message); key.showAlert(); }
}
M18.on("willAppear", (key) => { refresh(key); key.every(10 * 60 * 1000, refresh); });
M18.on("keyDown", refresh);
```

## 注意

- 键面 64×64 像素，标题文字要短；画图用 144 尺寸会自动缩放
- 插件跑在浏览器环境：有 `fetch`、`Canvas`、`setTimeout`，**没有** Node.js 的 `require` / 文件系统
- 不要死循环或同步长时间计算，会卡住同一浏览器里的其他插件
- 错误会写进日志：`keyDown 出错：… 代码第 3 行`
- 需要 Node.js / 文件系统 / 原生能力时，写标准 `.sdPlugin`（Elgato SDK）放进官方插件目录，控制台照样能跑
