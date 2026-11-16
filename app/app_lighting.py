"""App 的一部分：灯光效果引擎、广播、状态快照。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import collections
import json
import math
import re
import os
import queue
import threading
import time

from core import (  # noqa: F401
    DATA_DIR,
    DEFAULT_LIGHTING,
    EDITION,
    KEY_COUNT,
    KEY_POINTS,
    LED_COUNT,
    LED_POINTS,
    MODES,
    action_label,
    action_value,
    hex_to_rgb,
    hsv,
    log,
)


class LightingMixin:

    # -- 灯光 -------------------------------------------------------------- #
    def _save_soon(self, delay: float = 1.2) -> None:
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(delay, self._save_config)
        self._save_timer.daemon = True
        self._save_timer.start()

    def reset_lighting(self) -> None:
        with self.lock:
            self.lighting.update(DEFAULT_LIGHTING)
            self._dirty = True
        self.effect_reverse = False
        self.device.set_led_brightness(self.lighting["brightness"])
        self._save_config()

    def add_ripple(self, key: int) -> None:
        if self.lighting.get("mode") != "ripple" or key not in KEY_POINTS:
            return
        now = time.time()
        self._ripples = [r for r in self._ripples if now - r[1] < 3.0][-6:] + [(key, now)]

    def set_lighting(self, patch: dict) -> None:
        self._save_soon()
        clean = {}
        try:
            if patch.get("mode") in MODES:
                clean["mode"] = patch["mode"]
            color = str(patch.get("color") or "")
            if re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
                clean["color"] = color if color.startswith("#") else "#" + color
            if patch.get("speed") is not None:
                clean["speed"] = max(0.1, min(10.0, float(patch["speed"])))
            if patch.get("brightness") is not None:
                # 设备端 LED 亮度是 0–100 的百分比，超过 100 固件会走到奇怪的分支
                clean["brightness"] = max(0, min(100, int(float(patch["brightness"]))))
        except (TypeError, ValueError):
            log(f"灯光参数不合法，已忽略：{patch}")
        with self.lock:
            self.lighting.update(clean)
            self._dirty = True
            snap = {"mode": self.lighting["mode"], "color": self.lighting["color"],
                    "speed": self.lighting["speed"], "brightness": self.lighting["brightness"]}
        self.device.set_led_brightness(self.lighting["brightness"])
        # 按键里的「控制台 → 灯光效果切换」改了模式，界面上的灯效预设也要跟着高亮
        self.broadcast({"type": "lighting", "lighting": snap})

    def set_custom(self, colors) -> None:
        self._save_soon()
        with self.lock:
            cleaned = []
            for i in range(LED_COUNT):
                c = colors[i] if i < len(colors) else [0, 0, 0]
                cleaned.append([int(c[0]), int(c[1]), int(c[2])])
            self.lighting["custom"] = cleaned
            self.lighting["mode"] = "custom"
            self._dirty = True

    def _compute(self, t: float):
        with self.lock:
            cfg = dict(self.lighting)
        mode = cfg["mode"]
        n = LED_COUNT - 2          # 外圈 22 颗
        if mode == "custom":
            return [list(c) for c in cfg["custom"]]
        if mode == "ripple":
            return self._ripple_frame(cfg, time.time())
        if mode == "off":
            ring = [[0, 0, 0]] * n
        elif mode == "solid":
            ring = [list(hex_to_rgb(cfg["color"]))] * n
        elif mode == "gradient":
            base = hex_to_rgb(cfg["color"])
            second = [255 - base[0], 255 - base[1], 255 - base[2]]
            ring = [
                [int(base[j] + (second[j] - base[j]) * (i / (n - 1))) for j in range(3)]
                for i in range(n)
            ]
        elif mode == "rainbow":
            ring = [hsv(t * 0.15 * cfg["speed"] + i / n, 1.0, 1.0) for i in range(n)]
        elif mode == "breathe":
            v = 0.12 + 0.88 * (0.5 + 0.5 * math.sin(t * 2.0 * cfg["speed"]))
            base = hex_to_rgb(cfg["color"])
            ring = [[int(c * v) for c in base]] * n
        elif mode == "chase":
            base = hex_to_rgb(cfg["color"])
            ring = []
            for i in range(n):
                d = (i - t * cfg["speed"] * 5) % n
                k = max(0.0, 1.0 - min(d, n - d) / 2.5)
                ring.append([int(c * k) for c in base])
        else:
            ring = [[0, 0, 0]] * n

        # 正面两颗跟随"显示顺序"上固定位置的外圈灯，正反向都保持一致
        if self.effect_reverse:
            ring = ring[::-1]
        front = [list(ring[4]), list(ring[11])]
        return list(ring) + front

    def _ripple_frame(self, cfg: dict, now: float):
        """涟漪：从按下的键向四周扩散的光环，按灯珠到按键的真实距离点亮；没按键时整圈微亮。"""
        base = hex_to_rgb(cfg["color"])
        speed = 700.0 * max(0.2, float(cfg["speed"]))          # 每秒扩散多远（坐标单位）
        width = 95.0
        out = []
        ripples = list(self._ripples)
        for (x, y) in LED_POINTS:
            k = 0.0                       # 平时全灭，只有按键时才亮
            for key, t0 in ripples:
                kx, ky = KEY_POINTS[key]
                radius = (now - t0) * speed
                d = math.hypot(x - kx, y - ky)
                fade = max(0.0, 1.0 - radius / 1300.0)
                k = max(k, math.exp(-((d - radius) / width) ** 2) * fade)
            out.append([int(c * min(1.0, k)) for c in base])
        if ripples and now - ripples[-1][1] > 3.0:
            self._ripples = []
        return out

    def _engine_loop(self) -> None:
        t0 = time.time()
        ui_hash = None
        last_tick = time.time()
        gaps = collections.deque(maxlen=240)        # 每帧间隔（诊断灯效是否顺滑）
        sends = collections.deque(maxlen=240)       # 每次写灯带耗时
        skipped = 0
        fails = 0                                   # 写灯带失败次数（转发到设备失败）
        while True:
            time.sleep(1 / 30)
            now_tick = time.time()
            gaps.append((now_tick - last_tick) * 1000.0)
            last_tick = now_tick
            t = time.time() - t0
            if self._identify and time.time() < self._identify[1]:
                frame = [[0, 0, 0] for _ in range(LED_COUNT)]
                frame[self._identify[0]] = [255, 255, 255]
            else:
                frame = self._compute(t)
            static = self.lighting["mode"] in ("off", "solid", "gradient", "custom") or (
                self.lighting["mode"] == "ripple" and not self._ripples)
            h = hash(tuple(tuple(c) for c in frame))
            # 让位 / 静默 / 暂停写入 / 图片传输中，一律不碰灯带。
            # 灯珠和图片共用同一条通道，推图时不去抢——这样图片发得快，灯带也不会被挤得断流。
            # 例外：涟漪正在扩散时照写（灯带一帧很小）。
            rippling = self.lighting["mode"] == "ripple" and bool(self._ripples)
            blocked = self.writes_paused or self.quiet or self.yielded
            can_write = (not blocked) and not (self._pushing and not rippling)

            if static:
                if can_write and self._dirty and h != self._frame_hash:
                    ts = time.perf_counter()
                    ok = self.device.set_leds(self._to_hw(frame))
                    sends.append((time.perf_counter() - ts) * 1000.0)
                    if ok:
                        self._frame_hash = h
                        self._dirty = False
                    else:
                        fails += 1
            elif can_write:
                ts = time.perf_counter()
                ok = self.device.set_leds(self._to_hw(frame))
                sends.append((time.perf_counter() - ts) * 1000.0)
                if ok:
                    self._frame_hash = h
                else:
                    fails += 1
            elif not can_write:
                skipped += 1
            self._perf = {
                "gaps": list(gaps), "sends": list(sends),
                "skipped": skipped, "fails": fails, "pushing": bool(self._pushing),
                "mode": self.lighting["mode"],
            }

            # 只在画面真的变了时才推给界面（静态灯效不再每秒刷 30 条 SSE）
            if h != ui_hash:
                ui_hash = h
                self._frame = frame
                if self.clients:
                    self.broadcast({"type": "frame", "colors": frame})

    # -- 广播 -------------------------------------------------------------- #
    def broadcast(self, obj) -> None:
        data = json.dumps(obj, separators=(",", ":"))
        for q in list(self.clients):
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "device": {
                    "connected": self.device.connected,
                    "error": self.device.error,
                    "info": self.device.info,
                },
                "lighting": {
                    "mode": self.lighting["mode"],
                    "color": self.lighting["color"],
                    "speed": self.lighting["speed"],
                    "brightness": self.lighting["brightness"],
                    "custom": self.lighting["custom"],
                },
                "screenBrightness": self.screen_brightness,
                "frame": self._compute(time.time() - self._t0),
                "wallpaper": {
                    "enabled": self.wallpaper_enabled,
                    "available": os.path.isfile(self.wallpaper_file),
                    "flip": self.wallpaper_flip,
                    "gap": self.wallpaper_gap,
                    "file": os.path.basename(self.wallpaper_file or ""),
                },
                "led": {"offset": self.led_offset, "reverse": self.led_reverse},
                "effectReverse": self.effect_reverse,
                "inputMuted": self.input_muted,
                "repaintOnPress": self.repaint_on_press,
                "keepAwake": self.keep_awake,
                "closeToTray": self.close_to_tray,
                "writesPaused": self.writes_paused,
                "quiet": self.quiet,
                "yielded": self.yielded,
                "iconCount": len(self.list_icons()),
                "lightingOnly": self.lighting_only,
                "standby": self.standby_sec,
                "standbyBrightness": self.standby_brightness,
                "wallpaperEnabled": self.wallpaper_enabled,
                "autoBlank": self.autoblank_sec,
                "screenOff": self.screen_off,
                "customScene": self.custom_scene_name,
                "activeScene": self.active_scene,
                "autostart": self.autostart,
                "autostartOther": self.autostart_state()["other"],
                "edition": EDITION,
                "officialReady": self.official_ready(),
                "dataDir": DATA_DIR,
                "inStandby": self.in_standby,
                "album": self.album_state(),
                "library": self.library_items(),
                "inputStats": dict(self.device.stats),
                "lastResponse": getattr(self.device, "last_response", None),
                "rotation": self.rotation,
                "page": self.page,
                "pageCount": len(self.pages),
                "pageNames": [p.get("name", "") for p in self.pages],
                "pageFolder": [bool(p.get("folder")) for p in self.pages],
                "mainPages": self._main_pages(),
                "folderPath": self._folder_path(),
                "view": {
                    "fit": self.wallpaper_fit,
                    "zoom": self.wallpaper_zoom,
                    "ox": self.wallpaper_ox,
                    "oy": self.wallpaper_oy,
                },
                "keyActions": {str(k): action_label(v) for k, v in self.key_actions.items()},
                "keyActionTypes": {str(k): v.get("type", "launch")
                                   for k, v in self.key_actions.items()},
                "keyStyles": {str(k): v for k, v in self.key_styles.items()},
                "keyActionValues": {str(k): action_value(v) for k, v in self.key_actions.items()},
                "keyActionSpecs": {str(k): {f: v.get(f) for f in ("enter", "secret", "button", "pos",
                                                                  "action", "state", "op", "page")}
                                   for k, v in self.key_actions.items() if isinstance(v, dict)},
                "keyGestures": {
                    str(k): {gname: {"type": gv.get("type"), "label": action_label(gv),
                                     "value": action_value(gv),
                                     "spec": {f: gv.get(f) for f in ("enter", "secret", "button", "pos",
                                                                     "action", "name", "macro", "op")}}
                             for gname, gv in g.items() if isinstance(gv, dict)}
                    for k, g in self.key_gestures.items() if g},
                "keyImages": {str(k): v.get("type") for k, v in self.key_overrides.items()},
                "keyPlugin": {
                    str(k): {"plugin": v.get("plugin"), "name": v.get("name"),
                             "status": (self.plugin_host.status().get(v.get("plugin"), "idle")
                                        if self.plugin_host else "unavailable"),
                             "rev": (self.plugin_visual.get(v.get("context") or "") or {}).get("rev", 0)}
                    for k, v in self.key_actions.items()
                    if isinstance(v, dict) and v.get("type") == "plugin"},
                # 图片换了文件名就变：界面据此决定要不要重新拉预览（切场景 / 翻页时同一个键会换图）
                "keyImageRev": {str(k): os.path.basename(v.get("file") or "") or str(id(v))
                                for k, v in self.key_overrides.items()},
                # 场景每切一次就 +1：两个场景用了同名图片文件时，靠这个让预览也跟着换
                "sceneRev": self.scene_rev,
                "ledCount": LED_COUNT,
                "keyCount": KEY_COUNT,
                "officialRunning": self.official_running,
                "events": list(self.events)[:12],
            }
