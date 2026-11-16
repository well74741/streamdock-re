"""App 的一部分：插件宿主集成、自制插件、给 AI 智能体用的测试接口。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import base64
import colorsys
import glob
import hashlib
import io
import json
import re
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw, ImageFont, ImageOps
import official

from core import (  # noqa: F401
    CUSTOM_PLUGIN_DIR,
    CUSTOM_PREFIX,
    GIF_CACHE,
    GRID_COLS,
    GRID_ROWS,
    KEY_COUNT,
    KEY_SIZE,
    MEDIA_LABELS,
    PLUGIN_DATA,
    UPLOAD_DIR,
    action_label,
    log,
    send_hotkey,
    type_text,
)


class PluginsMixin:

    # -- 插件 ------------------------------------------------------------ #
    PLUGIN_GLOBAL = os.path.join(PLUGIN_DATA, "global-settings.json")

    def _device_id(self) -> str:
        return self.plugin_host.device_info()["id"] if self.plugin_host else "M18"

    @staticmethod
    def _coords(key: int) -> dict:
        return {"column": (key - 1) % GRID_COLS, "row": (key - 1) // GRID_COLS}

    def _plugin_payload(self, key: int, spec: dict) -> dict:
        return {"settings": spec.get("settings") or {}, "coordinates": self._coords(key),
                "state": int(spec.get("state") or 0), "isInMultiAction": False, "controller": "Keypad"}

    def _plugin_event(self, event: str, key: int, spec: dict, payload: dict | None = None) -> None:
        if not self.plugin_host:
            return
        msg = {"event": event, "action": spec.get("action"), "context": spec.get("context"),
               "device": self._device_id(), "payload": payload or self._plugin_payload(key, spec)}
        self.plugin_host.send(spec.get("plugin"), msg)

    def _plugin_sync(self) -> None:
        """当前页上的插件动作 = 可见实例：新出现的发 willAppear，离开的发 willDisappear。"""
        if not self._plugins_live:
            return
        with self._plugin_lock:
            now = {}
            for key, spec in list(self.key_actions.items()):
                if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("context"):
                    now[spec["context"]] = (key, spec)
            for ctx, (key, spec) in list(self._plugin_visible.items()):
                if ctx not in now or now[ctx][0] != key:
                    self._plugin_event("willDisappear", key, spec)
                    self._plugin_visible.pop(ctx, None)
            for ctx, (key, spec) in now.items():
                if ctx not in self._plugin_visible:
                    self._plugin_visible[ctx] = (key, spec)
                    self._plugin_appear(key, spec)
                else:
                    self._plugin_visible[ctx] = (key, spec)

    def _title_params(self, spec: dict) -> dict:
        """标题样式：官方场景里存的 > 插件 manifest 里该状态的默认值 > 通用默认。"""
        tp = spec.get("titleParameters")
        if isinstance(tp, dict) and tp:
            src = tp
        else:
            states, _ = self._plugin_states(spec)
            st = int(spec.get("state") or 0)
            raw = states[st] if 0 <= st < len(states) and isinstance(states[st], dict) else {}
            src = {"fontFamily": raw.get("FontFamily"), "fontSize": raw.get("FontSize"),
                   "fontStyle": raw.get("FontStyle"), "fontUnderline": raw.get("FontUnderline"),
                   "showTitle": raw.get("ShowTitle"), "titleAlignment": raw.get("TitleAlignment"),
                   "titleColor": raw.get("TitleColor")}
        try:
            size = int(float(src.get("fontSize") or 10))
        except (TypeError, ValueError):
            size = 10
        return {
            "fontFamily": str(src.get("fontFamily") or ""),
            "fontSize": size,
            "fontStyle": str(src.get("fontStyle") or "Regular"),
            "fontUnderline": bool(src.get("fontUnderline")),
            "showTitle": src.get("showTitle") is not False,
            "titleAlignment": str(src.get("titleAlignment") or "bottom"),
            "titleColor": str(src.get("titleColor") or "#ffffff"),
        }

    def _plugin_appear(self, key: int, spec: dict) -> None:
        """willAppear 后紧跟 titleParametersDidChange——不少插件要拿到字体 / 颜色才开始画（官方也是这个顺序）。"""
        self._plugin_event("willAppear", key, spec)
        payload = dict(self._plugin_payload(key, spec))
        payload.update({"title": "", "titleParameters": self._title_params(spec)})
        self._plugin_event("titleParametersDidChange", key, spec, payload=payload)

    def _on_plugin_ready(self, uid: str) -> None:
        with self._plugin_lock:
            for ctx, (key, spec) in self._plugin_visible.items():
                if spec.get("plugin") == uid:
                    # 插件（重新）连上：清掉旧实例留下的画面 / 标题，免得热重载后残留
                    if self.plugin_visual.pop(ctx, None) is not None:
                        self._plugin_dirty[key] = time.time()
                    self._plugin_appear(key, spec)

    def _plugin_key(self, key: int, spec: dict, state: int) -> None:
        self._plugin_event("keyDown" if state == 1 else "keyUp", key, spec)
        if state != 0:
            return
        # 多状态动作：松开后宿主自动切到下一个状态（官方 / Elgato 的默认行为）
        states, auto = self._plugin_states(spec)
        if len(states) > 1 and auto:
            spec["state"] = (int(spec.get("state") or 0) + 1) % len(states)
            self._plugin_state_image(spec)
            self._save_config()

    def _plugin_manifest_action(self, spec: dict) -> tuple[str | None, dict]:
        p = self._catalog()["plugins"].get(spec.get("plugin") or "")
        if not p:
            return None, {}
        man = official._plugin_manifest(p["folder"]) or {}
        for act in man.get("Actions") or []:
            if isinstance(act, dict) and act.get("UUID") == spec.get("action"):
                return p["folder"], act
        return p["folder"], {}

    def _plugin_states(self, spec: dict) -> tuple[list, bool]:
        _, act = self._plugin_manifest_action(spec)
        return list(act.get("States") or []), not act.get("DisableAutomaticStates")

    def _plugin_state_image(self, spec: dict) -> None:
        folder, act = self._plugin_manifest_action(spec)
        states = act.get("States") or []
        st = int(spec.get("state") or 0)
        if not (0 <= st < len(states)):
            return
        path = official.find_file(folder, (states[st] or {}).get("Image"))
        img = official.read_image(path) if path else None
        if img:
            self._plugin_apply_raster(spec.get("context"), img[1])

    def _find_context(self, ctx: str):
        """在当前场景所有页里找这个实例。返回 (页号, 键号, 动作)。"""
        for pi, page in enumerate(self.pages):
            for key, spec in page.get("actions", {}).items():
                if isinstance(spec, dict) and spec.get("context") == ctx:
                    return pi, key, spec
        return None, None, None

    def _plugin_mark(self, ctx: str) -> None:
        hit = self._plugin_visible.get(ctx)
        if hit:
            self._plugin_dirty[hit[0]] = time.time()

    def _vis(self, ctx: str) -> dict:
        return self.plugin_visual.setdefault(ctx, {"rev": 0})

    def _plugin_apply_raster(self, ctx: str, raw: bytes) -> None:
        """插件发来的位图（PNG/JPG/GIF）→ 64×64 键面；多帧 GIF 交给 SDK 原生播放。"""
        try:
            im = Image.open(io.BytesIO(raw))
            frames = int(getattr(im, "n_frames", 1) or 1)
            first = ImageOps.fit(self._flat_rgb(im), (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
        except Exception as exc:  # noqa: BLE001
            log(f"插件图片解析失败：{exc}")
            return
        buf = io.BytesIO()
        first.save(buf, "JPEG", quality=90)
        vis = self._vis(ctx)
        vis["jpeg"] = buf.getvalue()
        vis["gif"] = None
        if frames > 1:
            os.makedirs(GIF_CACHE, exist_ok=True)
            path = os.path.join(GIF_CACHE, f"plg_{hashlib.sha1(raw).hexdigest()[:16]}.gif")
            if not os.path.isfile(path):
                with open(path, "wb") as fh:
                    fh.write(raw)
            vis["gif"] = path
        vis["rev"] += 1
        self._plugin_mark(ctx)

    def _plugin_set_image(self, uid: str, ctx: str, image) -> None:
        if not ctx:
            return
        if not image:
            vis = self._vis(ctx)
            vis["jpeg"] = vis["gif"] = None
            vis["rev"] += 1
            self._plugin_mark(ctx)
            return
        image = str(image)
        raw, is_svg = None, False
        if image.startswith("data:"):
            header, _, data = image.partition(",")
            is_svg = "svg" in header
            try:
                raw = base64.b64decode(data) if ";base64" in header else \
                    urllib.parse.unquote(data).encode("utf-8")
            except Exception:  # noqa: BLE001
                return
        else:
            folder = (self._catalog()["plugins"].get(uid) or {}).get("folder")
            path = official.find_file(folder, image)
            if path:
                with open(path, "rb") as fh:
                    raw = fh.read()
                is_svg = path.lower().endswith(".svg")
        if not raw:
            return
        if is_svg or raw.lstrip()[:5] in (b"<svg ", b"<?xml") or raw.lstrip().startswith(b"<svg"):
            # SVG 交给后台浏览器画成 PNG（Pillow 不会画 SVG）
            rid = os.urandom(6).hex()
            self._raster_pending[rid] = ctx
            if len(self._raster_pending) > 200:
                self._raster_pending.pop(next(iter(self._raster_pending)))
            svg_uri = "data:image/svg+xml;base64," + base64.b64encode(raw).decode()
            if self.plugin_host and not self.plugin_host.send_host(
                    {"event": "rasterize", "id": rid, "image": svg_uri, "size": 144}):
                self.plugin_host._ensure_browser()
            return
        self._plugin_apply_raster(ctx, raw)

    def _plog(self, plugin: str, message: str, level: str = "info") -> None:
        self._plog_seq += 1
        self.plugin_logs.append({"seq": self._plog_seq, "t": round(time.time(), 3), "plugin": plugin,
                                 "level": level, "message": str(message)[:2000]})

    def _host_log(self, message: str) -> None:
        log(message)
        self._plog("host", message, "error" if any(w in message for w in ("失败", "退出", "不支持")) else "info")

    def plugin_log_items(self, plugin: str = "", since: int = 0, limit: int = 200) -> list:
        items = [x for x in list(self.plugin_logs) if x["seq"] > since and (not plugin or x["plugin"] == plugin
                                                         or (x["plugin"] == "host" and plugin in x["message"]))]
        return items[-max(1, min(1000, limit)):]

    def _on_host_msg(self, msg: dict) -> None:
        if msg.get("event") == "rasterized":
            ctx = self._raster_pending.pop(str(msg.get("id")), None)
            data = str(msg.get("image") or "")
            if ctx and "," in data:
                try:
                    self._plugin_apply_raster(ctx, base64.b64decode(data.split(",", 1)[1]))
                except Exception:  # noqa: BLE001
                    pass
        elif msg.get("event") == "log":
            log(f"[插件浏览器] {msg.get('message')}")
            text = str(msg.get("message") or "")
            uid = text.split("：", 1)[0] if "：" in text else "host"
            self._plog(uid, text, "error" if "出错" in text or "没有" in text else "info")

    def _plugin_global(self) -> dict:
        try:
            with open(self.PLUGIN_GLOBAL, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _set_plugin_global(self, uid: str, settings) -> None:
        data = self._plugin_global()
        data[uid] = settings if isinstance(settings, dict) else {}
        try:
            os.makedirs(PLUGIN_DATA, exist_ok=True)
            with open(self.PLUGIN_GLOBAL, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            log(f"保存插件全局设置失败：{exc}")

    def _did_receive_settings(self, key: int, spec: dict) -> dict:
        return {"event": "didReceiveSettings", "action": spec.get("action"), "context": spec.get("context"),
                "device": self._device_id(), "payload": self._plugin_payload(key, spec)}

    def _common_plugin_msg(self, uid: str, ev: str, msg: dict, reply) -> bool:
        """插件和设置页共有的消息。reply(obj) 回给发送方。"""
        payload = msg.get("payload") or {}
        if ev == "openUrl":
            url = str(payload.get("url") or "")
            if url.lower().startswith(("http://", "https://")):
                try:
                    os.startfile(url)
                except OSError:
                    pass
            else:
                log(f"插件 {uid} 试图打开非网页地址，已忽略：{url[:80]}")
            return True
        if ev == "logMessage":
            message = str(payload.get("message"))
            log(f"[插件 {uid}] {message[:300]}")
            self._plog(uid, message, "error" if any(w in message for w in ("错误", "出错", "Error", "失败")) else "info")
            return True
        if ev == "getGlobalSettings":
            reply({"event": "didReceiveGlobalSettings",
                   "payload": {"settings": self._plugin_global().get(uid) or {}}})
            return True
        if ev == "setGlobalSettings":
            self._set_plugin_global(uid, payload)
            return True
        return False

    def _on_plugin_msg(self, uid: str, msg: dict) -> None:
        ev = msg.get("event")
        ctx = msg.get("context") or ""
        payload = msg.get("payload") or {}
        host = self.plugin_host
        if uid.startswith(CUSTOM_PREFIX):
            if ev == "logMessage":
                self.broadcast({"type": "pluginlog", "plugin": uid,
                                "message": str(payload.get("message"))[:2000], "t": time.time()})
            elif ev and ev.startswith("m18."):
                self._custom_call(uid, ev, payload)
                return
        if self._common_plugin_msg(uid, ev, msg, lambda m: host.send(uid, m)):
            if ev == "setGlobalSettings":
                # 设置页可能开着：同步给它
                for c, (k, sp) in list(self._plugin_visible.items()):
                    if sp.get("plugin") == uid:
                        host.send_pi(c, {"event": "didReceiveGlobalSettings", "payload": {"settings": payload}})
            return
        if ev == "setImage":
            self._plugin_set_image(uid, ctx, payload.get("image"))
        elif ev == "setTitle":
            vis = self._vis(ctx)
            title = payload.get("title")
            vis["title"] = None if title is None else str(title)
            vis["rev"] += 1
            self._plugin_mark(ctx)
        elif ev == "setState":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                spec["state"] = int(payload.get("state") or 0)
                self._plugin_state_image(spec)
        elif ev in ("showOk", "showAlert"):
            vis = self._vis(ctx)
            vis["flash"] = ("ok" if ev == "showOk" else "alert", time.time() + 1.2)
            self._plugin_mark(ctx)
            threading.Timer(1.3, self._plugin_mark, args=(ctx,)).start()
        elif ev == "setSettings":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                spec["settings"] = payload if isinstance(payload, dict) else {}
                self._save_config()
                host.send_pi(ctx, self._did_receive_settings(key, spec))
        elif ev == "getSettings":
            _, key, spec = self._find_context(ctx)
            if spec is not None:
                host.send(uid, self._did_receive_settings(key, spec))
        elif ev == "sendToPropertyInspector":
            host.send_pi(ctx, {"event": "sendToPropertyInspector", "action": msg.get("action"),
                               "context": ctx, "payload": payload})

    def _on_pi_msg(self, ctx: str, msg: dict) -> None:
        ev = msg.get("event")
        payload = msg.get("payload") or {}
        _, key, spec = self._find_context(ctx)
        if spec is None:
            return
        uid = spec.get("plugin")
        host = self.plugin_host
        if self._common_plugin_msg(uid, ev, msg, lambda m: host.send_pi(ctx, m)):
            if ev == "setGlobalSettings":
                host.send(uid, {"event": "didReceiveGlobalSettings", "payload": {"settings": payload}})
            return
        if ev == "setSettings":
            spec["settings"] = payload if isinstance(payload, dict) else {}
            self._save_config()
            host.send(uid, self._did_receive_settings(key, spec))
        elif ev == "getSettings":
            host.send_pi(ctx, self._did_receive_settings(key, spec))
        elif ev == "sendToPlugin":
            host.send(uid, {"event": "sendToPlugin", "action": spec.get("action"),
                            "context": ctx, "payload": payload})

    def _plugin_flush_loop(self) -> None:
        """插件画面推到设备：每键最快 10 帧/秒、每 50ms 最多推 2 个键，界面预览 2 次/秒。"""
        last_dev: dict[int, float] = {}
        last_ui: dict[int, float] = {}
        ui_due: set[int] = set()          # 界面预览被节流掉、需要补发的键
        while True:
            time.sleep(0.05)
            now = time.time()
            for key in list(ui_due):
                if now - last_ui.get(key, 0) >= 0.5:
                    ui_due.discard(key)
                    last_ui[key] = now
                    vis = self._plugin_vis(key) or {}
                    self.broadcast({"type": "keyvisual", "key": key, "rev": vis.get("rev", 0)})
            if not self._plugin_dirty:
                continue
            dirty = dict(self._plugin_dirty)
            ready = sorted(dirty, key=dirty.get)
            can_dev = (self.device.connected and not (self.in_standby or self.writes_paused or self.quiet
                                                      or self.lighting_only or self.yielded or self.screen_off))
            pushed = 0
            for key in ready:
                if now - last_dev.get(key, 0) < 0.1:
                    continue
                self._plugin_dirty.pop(key, None)
                last_dev[key] = now
                if can_dev and 1 <= key <= KEY_COUNT:
                    self._gif_update_key(key)
                    if key not in self._gif_active:
                        self.device.send_image(key, self._render_key(key))
                    pushed += 1
                if self.clients:
                    ui_due.add(key)
                if pushed >= 2:
                    break
            if pushed:
                self.device.refresh()

    # -- 自制插件 ---------------------------------------------------------- #
    def _custom_call(self, uid: str, ev: str, payload: dict) -> None:
        """自制插件专属：发热键 / 打开程序 / 输入文字（第三方插件没有这些权限）。"""
        try:
            if ev == "m18.hotkey":
                ok = send_hotkey(str(payload.get("keys") or ""))
            elif ev == "m18.launch":
                path = str(payload.get("path") or "")
                ok = bool(path) and (os.path.exists(path) or path.lower().startswith(("http://", "https://")))
                if ok:
                    os.startfile(path)
            elif ev == "m18.text":
                ok = type_text(str(payload.get("text") or ""), bool(payload.get("enter")))
            elif ev == "m18.media":
                vk = MEDIA_LABELS.get(str(payload.get("action") or ""), ("", ""))[0]
                ok = bool(vk) and send_hotkey(vk)
            elif ev == "m18.http":
                threading.Thread(target=self._custom_http, args=(uid, payload), daemon=True).start()
                return
            else:
                ok = False
            log(f"[自制插件 {uid[len(CUSTOM_PREFIX):]}] {ev} {'✓' if ok else '✗'}")
        except Exception as exc:  # noqa: BLE001
            log(f"[自制插件 {uid}] {ev} 失败：{exc}")

    CUSTOM_RUNTIME = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title></head><body>
<script>
/* M18 自制插件运行时：把官方插件协议包装成几个简单的函数（由控制台生成，请在控制台里编辑） */
(() => {
  const OFFSET = __OFFSET__;
  // 错误信息里的行号换算成「你写的代码」的行号
  const fmt = (e) => String((e && e.stack) || e).split("\n").slice(0, 3).join("\n")
    .replace(/https?:\/\/[^\s)]*index\.html:(\d+):(\d+)/g, (m, l) => (l > OFFSET ? `代码第 ${l - OFFSET} 行` : "运行时"));
  let ws = null;
  const keys = new Map(), handlers = {}, timers = new Map(), pending = new Map();
  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const M18 = {
    on(ev, fn) { handlers[ev] = fn; },
    log(...a) { send({ event: "logMessage", payload: { message: a.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(" ") } }); },
    openUrl(url) { send({ event: "openUrl", payload: { url } }); },
    hotkey(keysText) { send({ event: "m18.hotkey", payload: { keys: keysText } }); },
    launch(path) { send({ event: "m18.launch", payload: { path } }); },
    type(text, enter = false) { send({ event: "m18.text", payload: { text, enter } }); },
    media(action) { send({ event: "m18.media", payload: { action } }); },
    // 由控制台代发请求：不受浏览器跨域限制。返回 { ok, status, text, json(), headers }
    http(url, opts = {}) {
      return new Promise((resolve, reject) => {
        const id = Math.random().toString(36).slice(2) + Date.now();
        const body = opts.body == null ? null : (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body));
        pending.set(id, { resolve, reject });
        send({ event: "m18.http", payload: { id, url, method: opts.method || "GET", headers: opts.headers || {}, body } });
        setTimeout(() => { if (pending.delete(id)) reject(new Error("请求超时")); }, 20000);
      });
    },
    sleep(ms) { return new Promise((r) => setTimeout(r, ms)); },
    keys() { return [...keys.values()]; },
  };
  const call = (ev, k, extra) => {
    const fn = handlers[ev];
    if (!fn) return;
    try {
      const r = fn(k, extra);
      if (r && r.catch) r.catch((e) => M18.log(`${ev} 出错：${fmt(e)}`));
    } catch (e) { M18.log(`${ev} 出错：${fmt(e)}`); }
  };
  const keyOf = (msg) => {
    const ctx = msg.context;
    let k = keys.get(ctx);
    if (!k) {
      k = {
        context: ctx, settings: {}, state: 0, coordinates: {},
        setTitle: (t) => send({ event: "setTitle", context: ctx, payload: { title: t == null ? "" : String(t), target: 0 } }),
        setImage: (img) => send({ event: "setImage", context: ctx, payload: { image: img || "", target: 0 } }),
        setState: (s) => send({ event: "setState", context: ctx, payload: { state: s } }),
        showOk: () => send({ event: "showOk", context: ctx }),
        showAlert: () => send({ event: "showAlert", context: ctx }),
        save: (obj) => { Object.assign(k.settings, obj || {}); send({ event: "setSettings", context: ctx, payload: k.settings }); },
        draw: (fn, size = 144) => {
          const c = document.createElement("canvas");
          c.width = c.height = size;
          const g = c.getContext("2d");
          try { fn(g, size); } catch (e) { M18.log(`draw 出错：${fmt(e)}`); return; }
          k.setImage(c.toDataURL("image/png"));
        },
        every: (ms, fn) => {
          const id = setInterval(() => { try { fn(k); } catch (e) { M18.log(`every 出错：${fmt(e)}`); } }, Math.max(100, ms));
          if (!timers.has(ctx)) timers.set(ctx, []);
          timers.get(ctx).push(id);
          return id;
        },
      };
      keys.set(ctx, k);
    }
    const p = msg.payload || {};
    if (p.settings) k.settings = p.settings;
    if (p.coordinates) k.coordinates = p.coordinates;
    if (typeof p.state === "number") k.state = p.state;
    return k;
  };
  window.M18 = M18;
  window.connectElgatoStreamDeckSocket = (port, uuid, registerEvent) => {
    ws = new WebSocket("ws://127.0.0.1:" + port);
    ws.onopen = () => { send({ event: registerEvent, uuid }); call("ready"); };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.event === "m18.httpResult") {
        const p = msg.payload || {}, cb = pending.get(p.id);
        if (!cb) return;
        pending.delete(p.id);
        if (p.error) cb.reject(new Error(p.error));
        else cb.resolve({ ok: p.status >= 200 && p.status < 300, status: p.status, headers: p.headers || {},
          text: p.body || "", json: () => JSON.parse(p.body || "null") });
        return;
      }
      if (!msg.context) { if (msg.event === "didReceiveGlobalSettings") call(msg.event, null, msg.payload); return; }
      const k = keyOf(msg);
      if (msg.event === "willDisappear") {
        (timers.get(msg.context) || []).forEach(clearInterval);
        timers.delete(msg.context);
        call("willDisappear", k);
        keys.delete(msg.context);
        return;
      }
      call(msg.event, k, msg.payload);
    };
  };
  window.addEventListener("error", (e) => {
    const line = e.lineno > OFFSET ? `（代码第 ${e.lineno - OFFSET} 行）` : "";
    M18.log(`脚本错误：${e.message}${line}`);
  });
  window.addEventListener("unhandledrejection", (e) => M18.log(`未处理的异步错误：${e.reason}`));
})();
</script>
<script>
__CODE__
</script>
</body></html>
"""

    @staticmethod
    def _valid_custom_id(pid: str) -> bool:
        return bool(re.fullmatch(re.escape(CUSTOM_PREFIX) + r"[0-9a-zA-Z_-]{1,40}", pid or ""))

    def _custom_folder(self, pid: str) -> str:
        if not self._valid_custom_id(pid):
            raise ValueError(f"非法的自制插件 id：{pid!r}")
        return os.path.join(CUSTOM_PLUGIN_DIR, f"{pid}.sdPlugin")

    def list_custom_plugins(self) -> list:
        out = []
        for folder in sorted(glob.glob(os.path.join(CUSTOM_PLUGIN_DIR, "*.sdPlugin"))):
            try:
                with open(os.path.join(folder, "m18.json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            meta["id"] = os.path.basename(folder)[:-len(".sdPlugin")]
            meta["hasIcon"] = os.path.isfile(os.path.join(folder, "icon.png"))
            meta["mtime"] = os.path.getmtime(os.path.join(folder, "m18.json"))
            out.append(meta)
        out.sort(key=lambda m: m.get("name", "").lower())
        return out

    @staticmethod
    def _default_icon(name: str) -> bytes:
        """没选图标时生成一个：彩色圆角方块 + 名称首字。"""
        hue = (int(hashlib.md5(name.encode("utf-8")).hexdigest()[:4], 16) % 360) / 360.0
        r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.78))
        img = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 139, 139], radius=28, fill=(r, g, b, 255))
        ch = (name.strip() or "?")[0]
        font = None
        for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\segoeuib.ttf"):
            try:
                font = ImageFont.truetype(path, 76)
                break
            except Exception:  # noqa: BLE001
                continue
        font = font or ImageFont.load_default()
        w = d.textlength(ch, font=font)
        d.text(((144 - w) / 2, 22), ch, font=font, fill=(255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _custom_icon_bytes(self, icon: str, name: str) -> bytes | None:
        """icon：pack:… / plugin:… / data:image/…；返回 144×144 PNG。"""
        raw = None
        if icon.startswith("pack:"):
            pack_id, _, fname = icon[5:].partition("/")
            path = self.icon_pack_file(pack_id, fname)
            img = official.read_image(path) if path else None
            raw = img[1] if img else None
        elif icon.startswith("plugin:"):
            plugin_id, _, idx = icon[7:].rpartition("/")
            try:
                path = self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                path = None
            img = official.read_image(path) if path else None
            raw = img[1] if img else None
        elif icon.startswith("data:image/") and "," in icon:
            try:
                raw = base64.b64decode(icon.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                raw = None
        if not raw:
            return None
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            im = ImageOps.fit(im, (144, 144), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None

    def save_custom_plugin(self, data: dict) -> dict:
        name = str(data.get("name") or "").strip()[:30]
        action_name = str(data.get("actionName") or name).strip()[:30] or name
        code = str(data.get("code") or "")
        if not name:
            return {"ok": False, "error": "插件名称不能为空"}
        pid = str(data.get("id") or "").strip()
        if not pid.startswith(CUSTOM_PREFIX) or not os.path.isdir(self._custom_folder(pid)):
            slug = "".join(ch for ch in hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:10])
            pid = f"{CUSTOM_PREFIX}{slug}"
        folder = self._custom_folder(pid)
        os.makedirs(folder, exist_ok=True)
        icon = str(data.get("icon") or "")
        icon_png = self._custom_icon_bytes(icon, name) if icon else None
        icon_path = os.path.join(folder, "icon.png")
        if icon_png or not os.path.isfile(icon_path):
            with open(icon_path, "wb") as fh:
                fh.write(icon_png or self._default_icon(name))
        manifest = {
            "Name": name, "UUID": pid, "Version": "1.0", "Author": "M18 控制台（自制）",
            "Description": str(data.get("description") or "")[:200],
            "CodePath": "index.html", "Icon": "icon", "Category": "自制插件", "CategoryIcon": "icon",
            "SDKVersion": 2, "OS": [{"Platform": "windows", "MinimumVersion": "10"}],
            "Software": {"MinimumVersion": "2.9"},
            "Actions": [{"UUID": f"{pid}.action", "Name": action_name, "Icon": "icon",
                         "States": [{"Image": "icon", "TitleAlignment": "middle"}],
                         "Tooltip": str(data.get("description") or name)[:100]}],
        }
        head = self.CUSTOM_RUNTIME.split("__CODE__")[0]
        offset = head.count("\n")
        html = (self.CUSTOM_RUNTIME.replace("__TITLE__", name.replace("<", ""))
                .replace("__OFFSET__", str(offset))
                .replace("__CODE__", code.replace("</script", "<\\/script")))
        meta = {"name": name, "actionName": action_name, "description": manifest["Description"],
                "template": str(data.get("template") or ""), "code": code}
        for fname, content in (("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)),
                               ("index.html", html),
                               ("m18.json", json.dumps(meta, ensure_ascii=False, indent=2))):
            with open(os.path.join(folder, fname), "w", encoding="utf-8") as fh:
                fh.write(content)
        self._refresh_plugins()
        if self.plugin_host:
            self.plugin_host.reload(pid)
        # 已经绑在按键上的：按键名字跟着改
        for page in self.pages:
            for spec in (page.get("actions") or {}).values():
                if isinstance(spec, dict) and spec.get("plugin") == pid:
                    spec["name"] = action_name
        log(f"自制插件已保存：{name}（{pid}）")
        return {"ok": True, "id": pid}

    def delete_custom_plugin(self, pid: str) -> bool:
        if not self._valid_custom_id(pid):
            return False
        folder = self._custom_folder(pid)
        if not os.path.isdir(folder):
            return False
        if self.plugin_host:
            self.plugin_host.forget(pid)
        shutil.rmtree(folder, ignore_errors=True)
        self._refresh_plugins()
        log(f"自制插件已删除：{pid}")
        return True

    def _refresh_plugins(self) -> None:
        self._catalog(refresh=True)
        if self.plugin_host:
            plugins = {}
            for uid, p in self._catalog()["plugins"].items():
                man = official._plugin_manifest(p["folder"])
                if man:
                    plugins[uid] = {"folder": p["folder"], "manifest": man}
            self.plugin_host.register_plugins(plugins)

    def _native_for(self, plugin_id: str, action_uuid: str) -> dict | None:
        """官方内置 / 热键包的动作 → 控制台原生动作；不能转换返回 None。"""
        native = official.builtin_native(action_uuid)
        if native:
            return native
        p = self._catalog()["plugins"].get(plugin_id or "")
        man = official._plugin_manifest(p["folder"]) if p else None
        if man and official.is_hotkey_pack(man):
            for a in man.get("Actions") or []:
                if isinstance(a, dict) and a.get("UUID") == action_uuid:
                    keys = official.hotkey_pack_keys(a, self._vk_reverse())
                    if keys and keys != "?":
                        return {"type": "hotkey", "keys": keys}
        return None

    def _convert_builtin_plugins(self) -> int:
        """以前绑定成「插件」的官方内置动作（创建文件夹、打开、热键…）跑不起来：换成原生动作。"""
        changed = 0
        for page in self.pages:
            for key, spec in list((page.get("actions") or {}).items()):
                if isinstance(spec, dict) and spec.get("type") == "plugin":
                    native = self._native_for(str(spec.get("plugin") or ""), str(spec.get("action") or ""))
                    if native:
                        page["actions"][key] = native
                        changed += 1
        if changed:
            log(f"已把 {changed} 个官方内置插件动作换成控制台原生动作")
        return changed

    # -- 给 AI 智能体用的测试接口 ------------------------------------------- #
    def simulate_press(self, key: int, gesture: str = "press") -> bool:
        """模拟按一下设备上的键（短按 / 长按 / 双击），走和真实按键完全相同的处理流程。"""
        if not (1 <= key <= KEY_COUNT + 3) or gesture not in ("press", "long", "double"):
            return False
        if self.in_standby and not self.album_active():
            self._wake_from_standby()          # 模拟按键不需要「第一下只唤醒」（相册里按键由相册自己处理）
        hold = 0.8 if gesture == "long" else 0.06

        def run():
            for i in range(2 if gesture == "double" else 1):
                self._on_input(key, 1)
                time.sleep(hold)
                self._on_input(key, 0)
                time.sleep(0.08)
        threading.Thread(target=run, daemon=True).start()
        log(f"模拟按键 {key}（{ {'press': '短按', 'long': '长按', 'double': '双击'}[gesture] }）")
        return True

    def keys_sheet_png(self, scale: int = 2) -> bytes:
        """当前页 15 个键的画面拼成一张图（和设备上看到的一致，左下角是 1 号键）。"""
        size, gap = KEY_SIZE * scale, 10
        sheet = Image.new("RGB", (GRID_COLS * size + (GRID_COLS + 1) * gap,
                                  GRID_ROWS * size + (GRID_ROWS + 1) * gap), (24, 28, 36))
        d = ImageDraw.Draw(sheet)
        album = self._canvas_tiles(self._album_canvas(self.album_index)) if self.album_active() else None
        for key in range(1, KEY_COUNT + 1):
            col, row_top = (key - 1) % GRID_COLS, GRID_ROWS - 1 - (key - 1) // GRID_COLS
            x, y = gap + col * (size + gap), gap + row_top * (size + gap)
            try:
                tile = album[key].resize((size, size)) if album else                     Image.open(io.BytesIO(self._number_tile(key))).convert("RGB").resize((size, size))
                sheet.paste(tile, (x, y))
            except Exception:  # noqa: BLE001
                d.rectangle([x, y, x + size, y + size], outline=(200, 60, 60))
            d.rectangle([x, y, x + 15, y + 12], fill=(0, 0, 0))
            d.text((x + 2, y), str(key), fill=(255, 220, 90))
        buf = io.BytesIO()
        sheet.save(buf, "PNG")
        return buf.getvalue()

    def bind_plugin_action(self, key: int, plugin_id: str, idx: int) -> dict:
        if not (1 <= key <= KEY_COUNT + 3):
            return {"ok": False}
        p = self._catalog()["plugins"].get(plugin_id)
        if not p or not (0 <= idx < len(p["actions"])):
            return {"ok": False}
        act = p["actions"][idx]
        native = None
        if p.get("builtin") or p.get("hotkeyPack"):
            # 官方内置插件 / 热键包不能独立运行：换成控制台自己的动作
            native = self._native_for(plugin_id, act["uuid"])
            if native is None:
                return {"ok": False, "error": f"「{act['name']}」是官方程序内置的功能，控制台暂不支持"}
            self.key_actions[key] = native
        else:
            self.key_actions[key] = {
                "type": "plugin", "plugin": plugin_id, "action": act["uuid"], "name": act["name"],
                "settings": {}, "context": os.urandom(16).hex(), "state": 0,
            }
        # 换成插件动作：原来的标题文字不再适用（插件自己画 / 设标题），保留配色
        style = self.key_styles.get(key)
        if style and style.pop("text", None) is not None and not any(style.values()):
            self.key_styles.pop(key, None)
        if act.get("icon") and key <= KEY_COUNT:
            img = official.read_image(act["icon"])
            if img:
                stored = self._store_scene_image(img, act["icon"])
                data = self._load_image_source(os.path.join(UPLOAD_DIR, stored))
                if data:
                    self.key_overrides[key] = data
        self._save_config()
        if self.device.connected and key <= KEY_COUNT:
            self._apply_key(key)
            self.device.refresh()
        if native is not None:
            field = official.NEEDS_INPUT.get(native["type"], "x")
            needs = bool(field) and not native.get(field)
            log(f"按键 {key} 绑定官方内置动作 {act['name']} → 原生动作「{action_label(native)}」")
            return {"ok": True, "native": True, "label": action_label(native), "needsInput": needs}
        status = (self.plugin_host.status().get(plugin_id) if self.plugin_host else "unavailable")
        log(f"按键 {key} 绑定插件动作 {act['name']}（{plugin_id}，{status}）")
        return {"ok": True}

    def plugin_pi_closed(self, key: int) -> None:
        spec = self.key_actions.get(key)
        if isinstance(spec, dict) and spec.get("type") == "plugin":
            self._plugin_event("propertyInspectorDidDisappear", key, spec, payload={})

    def plugin_file(self, uid: str, rel: str) -> str | None:
        p = self._catalog()["plugins"].get(uid)
        if not p:
            return None
        root = os.path.normpath(p["folder"])
        path = os.path.normpath(os.path.join(root, urllib.parse.unquote(rel)))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return None
        return path

    def _official_loop(self) -> None:
        """官方驱动在跑就让路，退出后自动接管。"""
        while True:
            time.sleep(3)
            try:
                running = self._detect_official()
                if running and not self.yielded:
                    self.yielded = True
                    log("检测到官方驱动运行，控制台自动退让（不再写设备）")
                    try:
                        self._gif_stop_all()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        self.device.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    self.broadcast({"type": "status", "connected": False})
                elif not running and self.yielded:
                    self.yielded = False
                    log("官方驱动已退出，重新接管设备")
                    if self.device.connect():
                        self._restore_all()
            except Exception as exc:  # noqa: BLE001
                log(f"官方驱动监测异常：{exc}")
