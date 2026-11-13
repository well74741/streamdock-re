"""M18 控制台的 MCP 服务（stdio）：让 Codex / DeepSeek Harness / Claude / Cursor 等 AI 智能体
连上控制台，开发、绑定、测试自制插件。

只用标准库。智能体启动它（命令见控制台「接入 AI」），它通过本机 HTTP 接口操作控制台；
控制台没开就在后台拉起来。

    python app/mcp_server.py           # 源码
    m18-mcp.exe                        # 安装版（和 M18Console.exe 同目录）

环境变量：M18_URL=http://127.0.0.1:8642 指定控制台地址（默认自动在 8642–8653 里找）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    from version import VERSION
except Exception:  # noqa: BLE001
    VERSION = "1.0.0"

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = getattr(sys, "_MEIPASS", HERE)
EXE_DIR = os.path.dirname(sys.executable) if FROZEN else HERE
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """这是 Stream Dock M18（15 键带屏幕的宏键盘）控制台。你可以：
- 写「自制插件」（一段浏览器 JavaScript，控制按键画面、响应按键）：先调 m18_plugin_api 读开发指南
- 开发循环：m18_save_custom_plugin → m18_bind_key → m18_press_key → m18_view_keys / m18_plugin_logs → 改代码再保存
- 也能给按键直接设内置动作（m18_set_key_action）、标题样式、切换场景
键号：底行 1–5，中行 6–10，顶行 11–15。改动会立刻反映到真实设备上；动别人已配置好的键之前先 m18_status 看一下，
测试时优先用空键。"""


# --------------------------------------------------------------------------- #
# 控制台连接
# --------------------------------------------------------------------------- #
class ConsoleError(Exception):
    pass


class Console:
    def __init__(self) -> None:
        self.base = (os.environ.get("M18_URL") or "").rstrip("/")
        self.launched = False

    def _probe(self, base: str) -> bool:
        # Windows 上连本机没人监听的端口要等 ~2 秒才报拒绝，先用短超时的 TCP 探一下
        try:
            host_port = base.split("//", 1)[1].split("/", 1)[0]
            host, _, port = host_port.partition(":")
            socket.create_connection((host, int(port or 80)), timeout=0.3).close()
        except (OSError, ValueError):
            return False
        try:
            with urllib.request.urlopen(base + "/api/state", timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and "keyCount" in data
        except Exception:  # noqa: BLE001
            return False

    def _find(self) -> str:
        if self.base:
            return self.base if self._probe(self.base) else ""
        for port in range(8642, 8654):
            base = f"http://127.0.0.1:{port}"
            if self._probe(base):
                return base
        return ""

    def _launch(self) -> None:
        flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        if FROZEN:
            exe = os.path.join(EXE_DIR, "M18Console.exe")
            if not os.path.isfile(exe):
                raise ConsoleError(f"找不到控制台程序：{exe}")
            cmd = [exe, "--no-browser"]
        else:
            py = sys.executable
            if py.lower().endswith("python.exe") and os.path.isfile(py[:-10] + "pythonw.exe"):
                py = py[:-10] + "pythonw.exe"
            cmd = [py, os.path.join(HERE, "server.py"), "--no-browser"]
        subprocess.Popen(cmd, cwd=os.path.dirname(cmd[-2] if not FROZEN else cmd[0]), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        self.launched = True

    def ensure(self) -> str:
        if self.base and self._probe(self.base):
            return self.base
        found = self._find()
        if not found and not os.environ.get("M18_URL"):
            self._launch()
            for _ in range(40):
                time.sleep(0.5)
                found = self._find()
                if found:
                    break
        if not found:
            raise ConsoleError("连不上 M18 控制台：请先打开「Stream Dock M18 控制台」再试")
        self.base = found
        return found

    def request(self, path: str, body: dict | None = None, raw: bool = False, timeout: float = 60):
        base = self.ensure()
        if body is None:
            req = urllib.request.Request(base + path)
        else:
            req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "Origin": base})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            raise ConsoleError(f"控制台返回 {exc.code}：{path}") from exc
        except OSError as exc:
            self.base = ""
            raise ConsoleError(f"请求控制台失败：{exc}") from exc
        return data if raw else json.loads(data.decode("utf-8"))

    def get(self, path: str):
        return self.request(path)

    def post(self, path: str, body: dict | None = None):
        return self.request(path, body or {})


CON = Console()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _text(obj) -> dict:
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)}


def _image(png: bytes, mime: str = "image/png") -> dict:
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": mime}


def _key_schema(desc: str = "键号 1–15（底行 1–5，中行 6–10，顶行 11–15）") -> dict:
    return {"type": "integer", "minimum": 1, "maximum": 15, "description": desc}


def _last_seq() -> int:
    items = CON.post("/api/plugins/logs", {"limit": 1}).get("items") or []
    return items[-1]["seq"] if items else 0


def _key_summary(state: dict) -> dict:
    out = {}
    for k in range(1, 16):
        s = str(k)
        item = {}
        if s in state.get("keyPlugin", {}):
            p = state["keyPlugin"][s]
            item["plugin"] = f"{p.get('name')}（{p.get('plugin')}，{p.get('status')}）"
        elif s in state.get("keyActions", {}):
            item["action"] = f"{state['keyActionTypes'].get(s)}：{state['keyActions'][s]}"
        g = state.get("keyGestures", {}).get(s)
        if g:
            item["gestures"] = {name: v.get("label") or v.get("value") for name, v in g.items()}
        if s in state.get("keyImages", {}):
            item["image"] = state["keyImages"][s]
        title = (state.get("keyStyles", {}).get(s) or {}).get("text")
        if title:
            item["title"] = title
        out[s] = item or "空"
    return out


def t_status(_a: dict):
    st = CON.get("/api/state")
    dev = st.get("device") or {}
    info = {
        "console": CON.base, "launchedByMcp": CON.launched,
        "deviceConnected": dev.get("connected"),
        "deviceNote": ("" if dev.get("connected") else
                       "设备没连上（可能官方 Stream Dock 软件占着设备）；画面仍在控制台里渲染，测试照常"),
        "scene": st.get("activeScene"), "page": f"{st.get('page', 0) + 1}/{st.get('pageCount')}",
        "inStandby": st.get("inStandby"), "officialSoftwareRunning": st.get("officialRunning"),
        "keys": _key_summary(st),
    }
    return [_text(info)]


def t_plugin_api(_a: dict):
    for base in (BUNDLE, HERE, EXE_DIR):
        path = os.path.join(base, "plugin_guide.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return [_text(fh.read())]
    return [_text("没找到 plugin_guide.md")]


def t_list_custom(_a: dict):
    items = CON.post("/api/custom-plugins").get("items") or []
    return [_text([{"id": p["id"], "name": p.get("name"), "actionName": p.get("actionName"),
                    "description": p.get("description"), "codeLines": len((p.get("code") or "").splitlines())}
                   for p in items] or "还没有自制插件")]


def _find_custom(pid: str) -> dict:
    for p in CON.post("/api/custom-plugins").get("items") or []:
        if p["id"] == pid or p.get("name") == pid:
            return p
    raise ConsoleError(f"没有这个自制插件：{pid}（先用 m18_list_custom_plugins 查 id）")


def t_get_custom(a: dict):
    p = _find_custom(str(a.get("id") or ""))
    return [_text({k: p.get(k) for k in ("id", "name", "actionName", "description")}),
            _text("```js\n" + (p.get("code") or "") + "\n```")]


def t_save_custom(a: dict):
    body = {k: a[k] for k in ("id", "name", "actionName", "description", "code", "icon") if a.get(k) is not None}
    if body.get("id") and not body.get("name"):
        body["name"] = _find_custom(body["id"]).get("name")
    r = CON.post("/api/custom-plugins/save", body)
    if not r.get("ok"):
        raise ConsoleError(r.get("error") or "保存失败")
    st = CON.get("/api/state")
    bound = [k for k, v in (st.get("keyPlugin") or {}).items() if v.get("plugin") == r["id"]]
    tip = (f"已热重载，绑定在键 {', '.join(bound)} 上；等 1–2 秒后用 m18_view_keys / m18_plugin_logs 检查"
           if bound else "还没绑到按键：用 m18_bind_key 绑上再测试")
    return [_text({"ok": True, "id": r["id"], "next": tip})]


def t_delete_custom(a: dict):
    pid = _find_custom(str(a.get("id") or ""))["id"]
    return [_text({"ok": CON.post("/api/custom-plugins/delete", {"id": pid}).get("ok"), "id": pid})]


def t_list_plugins(a: dict):
    q = str(a.get("query") or "").lower()
    out = []
    for p in CON.get("/api/plugins").get("plugins") or []:
        hay = (p.get("id", "") + p.get("name", "") + " ".join(x.get("name", "") for x in p.get("actions") or [])).lower()
        if q and q not in hay:
            continue
        kind = "自制" if p.get("custom") else "官方内置（会换成原生动作）" if p.get("builtin") else \
            "热键包" if p.get("hotkeyPack") else "第三方"
        out.append({"id": p["id"], "name": p.get("name"), "kind": kind,
                    "actions": [f"{i}: {x.get('name')}" + ("" if x.get("supported", True) else "（不支持）")
                                for i, x in enumerate(p.get("actions") or [])]})
    return [_text(out or "没有匹配的插件")]


def t_bind_key(a: dict):
    key = int(a["key"])
    pid = str(a.get("plugin") or "")
    if pid and not pid.startswith("com.") and "." not in pid:
        pid = _find_custom(pid)["id"]
    r = CON.post("/api/key/plugin", {"key": key, "plugin": pid, "index": int(a.get("index") or 0)})
    if not r.get("ok"):
        raise ConsoleError(r.get("error") or "绑定失败")
    time.sleep(1.5)
    return [_text({"ok": True, "key": key, **{k: v for k, v in r.items() if k != "ok"},
                   "next": "用 m18_press_key 测试，m18_view_keys 看画面"})]


ACTION_TYPES = ("launch", "url", "hotkey", "toggle", "media", "text", "macro", "mouse", "window",
                "close", "scene", "sound", "nav")


def t_set_action(a: dict):
    key = int(a["key"])
    spec = {k: v for k, v in a.items() if k != "key" and v is not None}
    if spec.get("type") == "none":
        ok = CON.post("/api/key/action/clear", {"key": key, "gesture": spec.get("gesture") or ""}).get("ok")
    else:
        ok = CON.post("/api/key/action", {"key": key, **spec}).get("ok")
    if not ok:
        raise ConsoleError("设置失败：检查参数（launch 的 path 必须存在；hotkey 如 ctrl+shift+s；"
                           "toggle 用 a|b；nav 的 name 为 上一页/下一页/主页/下一场景/返回上一场景）")
    return [_text({"ok": True, "key": key})]


def t_set_style(a: dict):
    key = int(a["key"])
    spec = {k: v for k, v in a.items() if k != "key" and v is not None}
    return [_text({"ok": CON.post("/api/key/style", {"key": key, **spec}).get("ok"), "key": key})]


def t_clear_key(a: dict):
    key = int(a["key"])
    return [_text({"ok": CON.post("/api/key/clear-all", {"key": key}).get("ok"), "key": key})]


def t_press(a: dict):
    key = int(a["key"])
    gesture = str(a.get("gesture") or "press")
    wait = max(0, min(10000, int(a.get("wait_ms", 1200))))
    since = _last_seq()
    if not CON.post("/api/key/simulate", {"key": key, "gesture": gesture}).get("ok"):
        raise ConsoleError("模拟按键失败")
    time.sleep((wait + (800 if gesture == "long" else 0)) / 1000)
    logs = CON.post("/api/plugins/logs", {"since": since, "limit": 100}).get("items") or []
    out = [_text({"ok": True, "key": key, "gesture": gesture,
                  "logs": [f"[{x['plugin']}] {x['message']}" for x in logs] or "（这次按键没有产生插件日志）"})]
    if a.get("view", True):
        out.append(_image(CON.request(f"/api/key/{key}/live.jpg", raw=True), "image/jpeg"))
    return out


def t_view(a: dict):
    if a.get("key"):
        return [_text(f"键 {int(a['key'])} 当前画面（64×64）"),
                _image(CON.request(f"/api/key/{int(a['key'])}/live.jpg", raw=True), "image/jpeg")]
    return [_text("15 个键的当前画面，排布和设备一致：顶行 11–15，中行 6–10，底行 1–5（左上角小号是键号）"),
            _image(CON.request("/api/keys/sheet.png", raw=True))]


def t_logs(a: dict):
    body = {"plugin": str(a.get("plugin") or ""), "since": int(a.get("since") or 0), "limit": int(a.get("limit") or 50)}
    items = CON.post("/api/plugins/logs", body).get("items") or []
    lines = [f"#{x['seq']} {time.strftime('%H:%M:%S', time.localtime(x['t']))} [{x['plugin']}] "
             f"{'⚠ ' if x['level'] == 'error' else ''}{x['message']}" for x in items]
    return [_text("\n".join(lines) if lines else "没有日志"),
            _text(f"最新序号 {items[-1]['seq'] if items else body['since']}（下次传 since 只看新的）")]


def t_scenes(_a: dict):
    items = CON.post("/api/scenes").get("items") or []
    return [_text([{"name": s.get("name"), "active": s.get("active"), "official": s.get("official")} for s in items])]


def t_apply_scene(a: dict):
    return [_text({"ok": CON.post("/api/scenes", {"apply": str(a.get("name") or "")}).get("ok")})]


TOOLS = [
    ("m18_status", "查看控制台和设备状态、当前场景，以及 15 个键各自绑了什么（动手改键之前先看）", {}, t_status, True),
    ("m18_plugin_api", "读自制插件开发指南（事件、key 对象、M18.* 函数、示例）。写插件前先读", {}, t_plugin_api, True),
    ("m18_list_custom_plugins", "列出自制插件", {}, t_list_custom, True),
    ("m18_get_custom_plugin", "读取一个自制插件的完整代码",
     {"id": {"type": "string", "description": "插件 id（或名称）"}}, t_get_custom, True),
    ("m18_save_custom_plugin", "新建或更新自制插件（传 id 就是更新）。保存后自动热重载，已绑定的键立即生效",
     {"id": {"type": "string", "description": "要更新的插件 id；新建时不传"},
      "name": {"type": "string", "description": "插件名（≤30 字，新建必填）"},
      "actionName": {"type": "string", "description": "动作名（显示在按键上，默认同插件名）"},
      "description": {"type": "string"},
      "code": {"type": "string", "description": "插件 JavaScript 代码（见 m18_plugin_api）"},
      "icon": {"type": "string", "description": "可选：侧栏图标 data:image/png;base64,…；不传自动生成"}},
     t_save_custom, False),
    ("m18_delete_custom_plugin", "删除自制插件", {"id": {"type": "string"}}, t_delete_custom, False),
    ("m18_list_plugins", "列出所有可用插件（第三方 / 官方内置 / 自制）和它们的动作序号",
     {"query": {"type": "string", "description": "按名称或 id 过滤"}}, t_list_plugins, True),
    ("m18_bind_key", "把插件的某个动作绑到按键上",
     {"key": _key_schema(), "plugin": {"type": "string", "description": "插件 id（自制插件也可传名称）"},
      "index": {"type": "integer", "description": "动作序号，默认 0"}}, t_bind_key, False),
    ("m18_set_key_action", "给按键设内置动作（不用插件）。type=none 表示清除该触发方式的动作",
     {"key": {"type": "integer", "minimum": 1, "maximum": 18, "description": "1–15；16/17/18 是三个导航键"},
      "type": {"type": "string", "enum": list(ACTION_TYPES) + ["none"]},
      "gesture": {"type": "string", "enum": ["press", "long", "double"], "description": "触发方式，默认短按"},
      "path": {"type": "string", "description": "launch：程序 / 文件 / 文件夹；sound：音频文件"},
      "url": {"type": "string"}, "keys": {"type": "string", "description": "hotkey：ctrl+shift+s；toggle：a|b"},
      "action": {"type": "string", "description": "media：playpause/next/prev/stop/mute/volup/voldown/brightup/brightdown"},
      "text": {"type": "string"}, "enter": {"type": "boolean"}, "secret": {"type": "boolean"},
      "macro": {"type": "string", "description": "每行一步：hotkey ctrl+c / text 你好 / delay 0.5（秒）/ launch 路径 / url 地址"},
      "button": {"type": "string", "enum": ["click", "double", "right", "middle", "scrollup", "scrolldown"]},
      "pos": {"type": "string", "description": "mouse：可选坐标 x,y"},
      "op": {"type": "string", "enum": ["minimize", "maximize", "desktop"]},
      "target": {"type": "string", "description": "close：进程名，空 = 当前窗口"},
      "scene": {"type": "string"}, "name": {"type": "string", "description": "nav：上一页/下一页/主页/下一场景/返回上一场景"}},
     t_set_action, False),
    ("m18_set_key_style", "设置按键标题文字和颜色（插件键的画面以插件为准）",
     {"key": _key_schema(), "text": {"type": "string", "description": "≤12 字"},
      "color": {"type": "string", "description": "#rrggbb"}, "bg": {"type": "string", "description": "#rrggbb"},
      "size": {"type": "integer", "minimum": 8, "maximum": 40},
      "pos": {"type": "string", "enum": ["top", "center", "bottom"]}}, t_set_style, False),
    ("m18_clear_key", "清空一个键（动作、图片、标题、插件全部去掉）", {"key": _key_schema()}, t_clear_key, False),
    ("m18_press_key", "模拟按一下设备上的键（会真的执行动作！），返回这次产生的插件日志和按后的键面",
     {"key": _key_schema(), "gesture": {"type": "string", "enum": ["press", "long", "double"]},
      "wait_ms": {"type": "integer", "description": "按完等多久再收日志，默认 1200"},
      "view": {"type": "boolean", "description": "是否附带键面截图，默认 true"}}, t_press, False),
    ("m18_view_keys", "看键面画面（图片）。不传 key 返回 15 键整版",
     {"key": _key_schema("只看某一个键")}, t_view, True),
    ("m18_plugin_logs", "拉插件日志（M18.log、报错、联网请求、插件进程启动 / 退出）",
     {"plugin": {"type": "string", "description": "只看某个插件 id"},
      "since": {"type": "integer", "description": "只要序号大于它的"},
      "limit": {"type": "integer", "description": "默认 50"}}, t_logs, True),
    ("m18_list_scenes", "列出场景", {}, t_scenes, True),
    ("m18_apply_scene", "切换到某个场景", {"name": {"type": "string"}}, t_apply_scene, False),
]
TOOL_MAP = {name: (fn, schema) for name, _d, schema, fn, _ro in TOOLS}
REQUIRED = {"m18_get_custom_plugin": ["id"], "m18_save_custom_plugin": ["code"], "m18_delete_custom_plugin": ["id"],
            "m18_bind_key": ["key", "plugin"], "m18_set_key_action": ["key", "type"], "m18_set_key_style": ["key"],
            "m18_clear_key": ["key"], "m18_press_key": ["key"], "m18_apply_scene": ["name"]}


def tools_list() -> list:
    out = []
    for name, desc, props, _fn, readonly in TOOLS:
        schema = {"type": "object", "properties": props}
        if name in REQUIRED:
            schema["required"] = REQUIRED[name]
        out.append({"name": name, "description": desc, "inputSchema": schema,
                    "annotations": {"readOnlyHint": readonly, "destructiveHint": name in (
                        "m18_delete_custom_plugin", "m18_clear_key"), "openWorldHint": False}})
    return out


# --------------------------------------------------------------------------- #
# JSON-RPC over stdio
# --------------------------------------------------------------------------- #
def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mid is None:                      # 通知，不回
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "m18-console", "title": "Stream Dock M18 控制台", "version": VERSION},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools_list()})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_MAP:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"未知工具：{name}"}}
        args = params.get("arguments") or {}
        missing = [r for r in REQUIRED.get(name, []) if args.get(r) in (None, "")]
        if missing:
            return ok({"content": [_text(f"缺少参数：{', '.join(missing)}")], "isError": True})
        try:
            return ok({"content": TOOL_MAP[name][0](args), "isError": False})
        except ConsoleError as exc:
            return ok({"content": [_text(str(exc))], "isError": True})
        except Exception as exc:  # noqa: BLE001
            return ok({"content": [_text(f"{type(exc).__name__}: {exc}")], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def main() -> int:
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}}
        else:
            if isinstance(msg, list):
                reply = [r for r in (handle(m) for m in msg if isinstance(m, dict)) if r] or None
            else:
                reply = handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()


if __name__ == "__main__":
    sys.exit(main())
