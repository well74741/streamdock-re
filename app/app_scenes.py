"""App 的一部分：按键输入与动作、开机自启、场景、场景导出导入、官方场景同步与本地备份、前台程序自动切换。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import base64
import copy
import hashlib
import io
import json
import zipfile
import os
import shutil
import subprocess
import sys
import threading
import time
from PIL import Image, ImageSequence
import official

import core
from core import (  # noqa: F401
    CONFIG_PATH,
    CONSOLE_LABELS,
    CUSTOM_PLUGIN_DIR,
    CUSTOM_PREFIX,
    EDITION,
    FROZEN,
    HERE,
    ICON_DIR,
    KEY_COUNT,
    LEGACY_ALBUM_NAMES,
    MEDIA_LABELS,
    MIRROR_DIR,
    MOUSE_LABELS,
    PRESET_DIR,
    SCENE_DIR,
    STARTUP_DIR,
    STARTUP_LNKS,
    UPLOAD_DIR,
    VK_NAMES,
    WINDOW_LABELS,
    _inside,
    close_target,
    data_path,
    foreground_app,
    log,
    mouse_action,
    send_hotkey,
    type_text,
    window_action,
)


class ScenesMixin:

    # -- 外部输入 ---------------------------------------------------------- #
    def _on_input(self, key: int, state: int) -> None:
        self._last_input_seen = time.time()
        if state == 1 and self.lighting.get("mode") == "ripple":
            # 涟漪是纯灯光互动：屏蔽按键时也照常亮
            self.add_ripple(key)
            self._dirty = True
        if self.input_muted:
            # 屏蔽按键时，只有设了「控制台 → 屏蔽按键开关」的键还能用，否则关不回来
            acts = [self.key_actions.get(key) or {}]
            acts += [g for g in (self.key_gestures.get(key) or {}).values() if isinstance(g, dict)]
            if not any(a.get("type") == "console" and a.get("op") == "mute" for a in acts if isinstance(a, dict)):
                return
        if self.screen_off and state == 1:
            # 息屏时按任何键（含三个导航键）只负责唤醒，不执行动作：
            # 否则「长按 = 屏幕开关」的那个键会在唤醒的同时又把自己关掉。
            self.screen_off = False
            self._wake_swallow.add(key)
            self.broadcast({"type": "status", "screenOff": False})
            log(f"按键 {key}：屏幕是关的，先唤醒")
            threading.Thread(target=self._wake_screen_now, daemon=True).start()
            return
        if state == 0 and key in self._wake_swallow:
            self._wake_swallow.discard(key)
            return
        if self.album_active():
            # 相册场景播放中：点任意屏幕键都是「下一个场景」，导航键是 上页/主场景/下页；
            # 幻灯片（待机相册）只是待机画面，播放时 ◀ ▶ 翻照片、按其他键回到当前场景。
            self._last_activity = time.time()
            g = self.key_gestures.get(key) or {}
            if self.album_scene_name and (g.get("long") or g.get("double")):
                # 相册场景里给导航键设了长按/双击：照常按手势判定，短按还是 上页/主页/下页
                self._gesture(key, state, g)
                return
            if state == 1:
                self._wake_swallow.add(key)
                if self.album_scene_name:
                    # 相册场景：◀ ▶ 翻相册里的图片，中间键回主场景，其余屏幕键切下一个场景
                    if key == 16:
                        threading.Thread(target=self.album_goto, args=(-1,), daemon=True).start()
                    elif key == 18:
                        threading.Thread(target=self.album_goto, args=(1,), daemon=True).start()
                    elif key == 17:
                        threading.Thread(target=self.go_home, daemon=True).start()
                    else:
                        threading.Thread(target=self.next_scene, daemon=True).start()
                elif key in (16, 18) and len(self._alb_items()) > 1:
                    threading.Thread(target=self.album_goto, args=(-1 if key == 16 else 1,),
                                     daemon=True).start()
                else:
                    threading.Thread(target=self.album_stop, daemon=True).start()
            elif key in self._wake_swallow:
                self._wake_swallow.discard(key)
            return
        idle = time.time() - self._last_activity
        self._last_activity = time.time()
        was_standby = self.in_standby       # 唤醒线程会马上改掉它，先记下来
        # 只有真的在待机、或很久没操作（设备可能自己睡过去显示开机图）才整屏补画。
        # 以前是「距上次整屏绘制 > 1 秒」就补画，几乎每次按键都会先重写 15 个键、停启动图，
        # 这一大批数据堵在 SDK 队列前面，按键动作和插件画面就跟着延迟。
        if was_standby or idle > 600:
            threading.Thread(target=self._wake_from_standby, daemon=True).start()
        item = {"type": "input", "key": key, "state": state, "t": time.time()}
        self.events.appendleft(item)
        self.broadcast(item)
        log(f"按键事件 key={key} state={state} standby={was_standby}")
        # 待机壁纸状态下的第一次按键只用来唤醒：按下和随后的松开都不触发动作（插件也收不到）
        if state == 1 and was_standby:
            self._wake_swallow.add(key)
            log(f"按键 {key}：待机中，仅唤醒，不执行动作")
            return
        if state == 0 and key in self._wake_swallow:
            self._wake_swallow.discard(key)
            return
        act = self.key_actions.get(key)
        g = self.key_gestures.get(key) or {}
        if not (g.get("long") or g.get("double")):
            # 插件动作要收按下 / 松开两个事件，所以先走插件通道
            if isinstance(act, dict) and act.get("type") == "plugin":
                self._plugin_key(key, act, state)
                return
            # 只有短按：按下立即执行，零延迟
            if state == 1:
                if self.repaint_on_press:
                    self._maybe_repaint()
                threading.Thread(target=self._run_action, args=(key,), daemon=True).start()
            return
        # 设了长按 / 双击：一律交给手势判定。以前这里先判断插件就 return，
        # 结果「插件键 + 长按/双击」永远收不到手势，设了也没反应。
        self._gesture(key, state, g)

    def wake_from_console(self, why: str = "") -> bool:
        """息屏时只要在控制台里做了任何操作，就把屏幕点亮并补回画面。"""
        if not self.screen_off:
            return False
        self.screen_off = False
        self._last_activity = time.time()
        self._blanked = False
        self.broadcast({"type": "status", "screenOff": False})
        log(f"控制台操作（{why or 'api'}）→ 屏幕已唤醒")
        threading.Thread(target=self._wake_screen_now, daemon=True).start()
        return True

    def _wake_screen_now(self) -> None:
        """息屏状态下按了键：点亮屏幕并把按键画面补回去。"""
        try:
            self.wake_panel()
            try:
                self.device.resubscribe_input()   # 顺便把按键通道重新订一次，防止它悄悄断掉
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12)
            # 唤醒后回到息屏前那个场景，而不是让闲置计时立刻又进待机
            self._last_activity = time.time()
            if self.in_standby:
                self.in_standby = False
                self.preview_mode = "wallpaper"
            self.apply_keys()
        except Exception as exc:  # noqa: BLE001
            log(f"唤醒屏幕失败：{exc}")

    def _maybe_repaint(self) -> None:
        """设备空闲会自己息屏，醒过来只显示开机图；按键时补一次画面。"""
        if time.time() - self._last_paint < 6:
            return
        self._last_paint = time.time()
        threading.Thread(target=self._repaint_now, daemon=True).start()

    def _repaint_now(self) -> None:
        try:
            if self.preview_mode == "calibration":
                self.preview_mode = "wallpaper"
            self.apply_keys()
        except Exception as exc:  # noqa: BLE001
            log(f"唤醒重绘失败：{exc}")

    def _run_macro(self, key: int, steps: list) -> None:
        log(f"按键 {key} → 宏（{len(steps)} 步）")
        for step in steps:
            op = step.get("op")
            try:
                if op == "delay":
                    time.sleep(max(0.0, min(60.0, float(step.get("sec", 0)))))
                elif op == "hotkey":
                    send_hotkey(step.get("keys", ""))
                elif op == "text":
                    type_text(step.get("text", ""))
                elif op == "url":
                    if step.get("url"):
                        os.startfile(step["url"])
                elif op == "launch":
                    path = step.get("path") or ""
                    if os.path.exists(path):
                        os.startfile(path)
            except Exception as exc:  # noqa: BLE001
                log(f"宏步骤失败 {op}: {exc}")

    # -- 开机自启：启动文件夹被拦时，退而写当前用户的 Run 项 -------------- #
    RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    RUN_NAME = "M18Console"

    _lnk_cache: dict = {}

    def set_autostart(self, enabled: bool) -> bool:
        lnk = os.path.join(STARTUP_DIR, STARTUP_LNKS[EDITION])
        bat = sys.executable if FROZEN else os.path.abspath(os.path.join(HERE, "..", "启动 M18 控制台.bat"))
        # 两个版本只留一个开机自启，否则开机会先后启动两次
        others = [os.path.join(STARTUP_DIR, n) for n in STARTUP_LNKS.values() if n != STARTUP_LNKS[EDITION]]
        q = lambda p: p.replace("'", "''")  # noqa: E731
        if enabled:
            ps = "".join(f"Remove-Item -LiteralPath '{q(o)}' -ErrorAction SilentlyContinue;" for o in others) + (
                f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
                f"$s.TargetPath='{q(bat)}';$s.WorkingDirectory='{q(os.path.dirname(bat))}';"
                f"$s.Description='Stream Dock M18 控制台（{EDITION}）';$s.WindowStyle=7;$s.Save()"
            )
        else:
            ps = f"Remove-Item -LiteralPath '{q(lnk)}' -ErrorAction SilentlyContinue"
            if EDITION == "源码版":           # 旧版本建的同名快捷方式（指向 .bat 的）也一起删
                old = os.path.join(STARTUP_DIR, STARTUP_LNKS["绿色版"])
                if os.path.isfile(old) and self._lnk_target(old).lower().endswith(".bat"):
                    ps += f";Remove-Item -LiteralPath '{q(old)}' -ErrorAction SilentlyContinue"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            log(f"设置开机自启失败：{exc}")
            return False
        if enabled:
            ok = os.path.isfile(lnk)
            if ok:
                self._set_run(False)          # 启动文件夹成功了，清掉旧的注册表项
            else:
                # 「启动」文件夹常被安全软件的勒索防护拦写（火绒等）：
                # 退而写当前用户的 Run 项，效果一样，且不触发受保护文件夹
                ok = self._set_run(True, f'"{bat}"')
                log("启动文件夹写不进去，已改用注册表自启" if ok
                    else "开机自启没设置成功：启动文件夹和注册表都写不进去")
            if not ok:
                self.autostart = self.autostart_state()["this"]
                self._save_config()
                return False
        else:
            self._set_run(False)
        self.autostart = self.autostart_state()["this"]
        self._save_config()
        log(f"开机自启（{EDITION}）：{'开' if enabled else '关'}")
        return True

    def export_config(self) -> dict:
        try:
            return self._read_config_file()
        except (OSError, ValueError):
            return {}

    def import_config(self, data: dict) -> bool:
        if not isinstance(data, dict) or "pages" not in data:
            return False
        try:
            with self._save_lock:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)
        except OSError as exc:
            log(f"导入配置失败：{exc}")
            return False
        self._suspend_snapshot = True
        try:
            self._load_config()
            self._convert_builtin_plugins()
            self._bind_page()
        finally:
            self._suspend_snapshot = False
        self._dirty = True
        if self.device.connected and not self.yielded:
            if not self.screen_off:
                self.device.set_screen_brightness(self.screen_brightness)
            self.apply_keys()
        return True

    def _scene_path(self, name: str) -> str:
        if name == self.custom_scene_name:
            return os.path.join(SCENE_DIR, "__custom__.json")
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (name or ""))[:40]
        return os.path.join(SCENE_DIR, safe.strip() + ".json")

    ALBUM_SCENE = "幻灯片"        # 待机幻灯片的显示名（它只是待机画面，不再占场景列表）

    def _scene_flags(self) -> dict:
        """场景的界面属性（停用等），存在场景目录的 .scene-flags.json 里；自定义 / 相册场景也能设。"""
        try:
            with open(os.path.join(SCENE_DIR, ".scene-flags.json"), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_scene_muted(self, name: str, muted: bool) -> bool:
        name = self._canonical_scene(name)
        flags = self._scene_flags()
        item = flags.setdefault(name, {})
        if muted:
            item["muted"] = True
        else:
            item.pop("muted", None)
            if not item:
                flags.pop(name, None)
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(os.path.join(SCENE_DIR, ".scene-flags.json"), "w", encoding="utf-8") as fh:
                json.dump(flags, fh, ensure_ascii=False, indent=1)
        except OSError:
            return False
        self._links_cache = None
        log(f"场景「{name}」已{'停用（切换时跳过）' if muted else '启用'}")
        return True

    def _scene_order_path(self) -> str:
        return os.path.join(SCENE_DIR, ".scene-order.json")

    def _scene_order(self) -> list:
        """用户拖出来的场景顺序（名字列表），文件不存在就是空。"""
        try:
            with open(self._scene_order_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return [str(n) for n in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def set_scene_order(self, names: list) -> bool:
        exist = [it["name"] for it in self.list_scenes()]
        order = []
        for n in names or []:
            n = str(n)
            if n in exist and n not in order:
                order.append(n)
        order += [n for n in exist if n not in order]
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_order_path(), "w", encoding="utf-8") as fh:
                json.dump(order, fh, ensure_ascii=False, indent=1)
        except OSError:
            return False
        self._links_cache = None
        log("场景顺序已保存：" + " / ".join(order))
        return True

    def list_scenes(self) -> list:
        official_names = self.official_scene_names()
        flags = self._scene_flags()
        cur = self.current_scene()
        items = [{"name": self.custom_scene_name, "custom": True, "sceneType": "normal",
                  "active": cur == self.custom_scene_name}]
        try:
            files = sorted(os.listdir(SCENE_DIR))
        except OSError:
            files = []
        for f in files:
            if not f.lower().endswith(".json") or f.startswith(("__", ".")):
                continue
            name = os.path.splitext(f)[0]
            path = os.path.join(SCENE_DIR, f)
            data = {}
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    loaded = json.load(fh)
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, AttributeError):
                data = {}
            alb = data.get("album") if data.get("sceneType") == "album" else None
            if isinstance(alb, dict):
                items.append({"name": name, "custom": False, "album": True, "albumScene": True,
                              "sceneType": "album", "official": name in official_names,
                              "count": len([n for n in (alb.get("items") or []) if n]),
                              "active": cur == name})
                continue
            items.append({"name": name, "custom": False, "sceneType": "normal",
                          "official": name in official_names,
                          "apps": data.get("apps") or [],
                          "active": cur == name})
        for it in items:
            it["muted"] = bool(flags.get(it["name"], {}).get("muted"))
            if it["sceneType"] == "album":
                it["empty"] = int(it.get("count") or 0) == 0
        order = self._scene_order()
        if order:
            rank = {n: i for i, n in enumerate(order)}
            items.sort(key=lambda it: rank.get(it["name"], len(order)))
        return items

    def current_scene(self) -> str:
        """当前在用的场景名；播放相册场景时就是那个相册场景（幻灯片只是待机画面，不算场景）。"""
        if self.album_active() and self.album_scene_name:
            return self.album_scene_name
        return self.active_scene or self.custom_scene_name

    @staticmethod
    def _fresh_contexts(data: dict) -> dict:
        """场景另存 / 复制后，里面的插件实例要换新 context，否则和原场景共用同一个实例。"""
        for page in data.get("pages") or []:
            for spec in (page.get("actions") or {}).values():
                if isinstance(spec, dict) and spec.get("type") == "plugin":
                    spec["context"] = os.urandom(16).hex()
        return data

    def save_scene(self, name: str) -> bool:
        name = (name or "").strip()[:40]
        if not name:
            return False
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                json.dump(self._fresh_contexts(self.export_config()), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存场景失败：{exc}")
            return False
        # 新建只是保存一份，不切换当前场景
        log(f"已保存场景：{name}")
        return True

    def create_scene(self, name: str, kind: str = "normal") -> dict:
        """新建场景：普通场景（把当前画面存一份）或相册场景（空的图片集，编辑区里再加图）。"""
        name = (name or "").strip()[:40]
        if not name:
            return {"ok": False, "reason": "场景名不能为空"}
        name = self._canonical_scene(name)
        if name == self.custom_scene_name or os.path.isfile(self._scene_path(name)):
            return {"ok": False, "reason": f"「{name}」已经有一个同名场景了"}
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            if kind == "album":
                data = {"sceneType": "album",
                        "album": {"items": [], "index": 0, "slideshow": True, "interval": 8,
                                  "transition": "fade", "fit": "cover"},
                        "pages": [{"name": name, "images": {},
                                   "actions": {str(k): {"type": "nav", "name": v}
                                               for k, v in self.ALBUM_NAV.items()}}],
                        "page": 0}
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
            else:
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(self._fresh_contexts(self.export_config()), fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"新建场景失败：{exc}")
            return {"ok": False, "reason": f"新建失败：{exc}"}
        log(f"已新建{'相册' if kind == 'album' else '普通'}场景：{name}")
        return {"ok": True, "name": name, "kind": "album" if kind == "album" else "normal"}

    def rename_scene(self, old: str, new: str) -> bool:
        new = (new or "").strip()[:40]
        if not new or old == self.custom_scene_name:
            return False
        src = self._scene_path(old)
        if not os.path.isfile(src):
            return False
        try:
            os.replace(src, self._scene_path(new))
        except OSError:
            return False
        if self.active_scene == old:
            self.active_scene = new
            self._save_config()
        return True

    def duplicate_scene(self, name: str) -> bool:
        src = self._scene_path(name)
        if not os.path.isfile(src):
            return False
        base = f"{name} 副本"
        target = base
        i = 2
        while os.path.isfile(self._scene_path(target)):
            target = f"{base} {i}"
            i += 1
        try:
            with open(src, encoding="utf-8-sig") as fh:
                data = self._fresh_contexts(json.load(fh))
            with open(self._scene_path(target), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            return False
        return True

    def rename_custom_scene(self, name: str) -> bool:
        name = (name or "").strip()[:40]
        if not name:
            return False
        self.custom_scene_name = name
        self._save_config()
        return True

    # 场景只负责「按键内容」：页面、图片、动作、标题样式、当前页。
    # 待机时间、亮度、壁纸、间隙、灯带等是全局设置，切场景时保持不变。
    SCENE_KEYS = ("pages", "page")

    def _apply_scene_file(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            return False
        merged = self.export_config()
        if not merged:
            log("读取当前配置失败，取消切换场景（避免全局设置被清空）")
            return False
        for k in self.SCENE_KEYS:
            if k in data:
                merged[k] = data[k]
        return self.import_config(merged)

    # -- 单个场景导出 / 导入（.m18scene = zip：scene.json + 图片 + 用到的自制插件） ----------- #
    SCENE_PKG_VERSION = 1

    def _scene_data(self, name: str) -> dict | None:
        if name == self.custom_scene_name or not name:
            if self.active_scene in ("", self.custom_scene_name):
                return self.export_config()
            path = os.path.join(SCENE_DIR, "__custom__.json")
        else:
            path = self._scene_path(name)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def export_scene(self, name: str) -> dict:
        """打包一个场景：只含按键内容（页面、图片、动作、样式、关联程序），不含全局设置。"""
        if name == self.active_scene and name != self.custom_scene_name:
            self._snapshot_custom_scene()
        data = self._scene_data(name)
        if not data or not isinstance(data.get("pages"), list):
            return {"ok": False, "error": "读取场景失败"}
        scene = {k: data[k] for k in ("pages", "page", "apps") if k in data}
        buf = io.BytesIO()
        images, plugins = set(), set()
        for p in scene["pages"]:
            images.update(str(v) for v in (p.get("images") or {}).values() if v)
            specs = list((p.get("actions") or {}).values())
            for g in (p.get("gestures") or {}).values():
                specs.extend((g or {}).values())
            for s in specs:
                if isinstance(s, dict) and s.get("type") == "plugin" and str(s.get("plugin", "")).startswith(CUSTOM_PREFIX):
                    plugins.add(s["plugin"])
        missing = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("scene.json", json.dumps({"format": "m18scene", "version": self.SCENE_PKG_VERSION,
                                                  "name": name, "app": "M18 Console",
                                                  "scene": scene}, ensure_ascii=False, indent=1))
            for img in sorted(images):
                src = os.path.join(UPLOAD_DIR, os.path.basename(img))
                if not os.path.isfile(src):
                    src = os.path.join(ICON_DIR, os.path.basename(img))
                if os.path.isfile(src):
                    zf.write(src, f"images/{os.path.basename(img)}")
                else:
                    missing.append(img)
            for pid in sorted(plugins):
                folder = self._custom_folder(pid)
                if not os.path.isdir(folder):
                    continue
                for root, _dirs, files in os.walk(folder):
                    for f in files:
                        full = os.path.join(root, f)
                        zf.write(full, "plugins/" + os.path.relpath(full, CUSTOM_PLUGIN_DIR).replace("\\", "/"))
        log(f"已导出场景「{name}」：{len(images) - len(missing)} 张图片，{len(plugins)} 个自制插件")
        return {"ok": True, "name": name, "filename": f"{name}.m18scene",
                "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                "images": len(images) - len(missing), "plugins": len(plugins)}

    def import_scene_package(self, b64: str, name: str = "") -> dict:
        try:
            raw = base64.b64decode(b64.split(",", 1)[-1])
            zf = zipfile.ZipFile(io.BytesIO(raw))
            meta = json.loads(zf.read("scene.json").decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "不是有效的场景文件（.m18scene）"}
        if meta.get("format") != "m18scene" or not isinstance((meta.get("scene") or {}).get("pages"), list):
            return {"ok": False, "error": "不是有效的场景文件（.m18scene）"}
        total = sum(i.file_size for i in zf.infolist())
        if total > 300 * 1024 * 1024 or len(zf.infolist()) > 5000:
            return {"ok": False, "error": "场景文件过大"}
        scene = meta["scene"]
        # 图片：按内容哈希改名放进上传目录，同一张图重复导入不会堆副本
        renamed = {}
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        for info in zf.infolist():
            if not info.filename.startswith("images/") or info.is_dir():
                continue
            base = os.path.basename(info.filename)
            ext = os.path.splitext(base)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp") or not base:
                continue
            data = zf.read(info)
            new = f"scn_{hashlib.sha1(data).hexdigest()[:16]}{ext}"
            dst = os.path.join(UPLOAD_DIR, new)
            if not os.path.isfile(dst):
                with open(dst, "wb") as fh:
                    fh.write(data)
            renamed[base] = new
        # 自制插件：已存在同 id 的不覆盖（保留本机的版本）
        plugins_added = 0
        existing = set(os.listdir(CUSTOM_PLUGIN_DIR)) if os.path.isdir(CUSTOM_PLUGIN_DIR) else set()
        for info in zf.infolist():
            parts = info.filename.split("/")
            if parts[0] != "plugins" or len(parts) < 3 or info.is_dir():
                continue
            if not parts[1].startswith(CUSTOM_PREFIX) or not parts[1].endswith(".sdPlugin") or ".." in parts:
                continue
            dst = os.path.join(CUSTOM_PLUGIN_DIR, *parts[1:])
            if not _inside(dst, CUSTOM_PLUGIN_DIR):
                continue
            if parts[1] in existing:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(zf.read(info))
            if parts[2] == "manifest.json":
                plugins_added += 1
        for p in scene["pages"]:
            p["images"] = {k: renamed.get(os.path.basename(str(v)), os.path.basename(str(v)))
                           for k, v in (p.get("images") or {}).items() if v}
        self._fresh_contexts(scene)
        # 场景名：重名自动加序号
        base_name = ((name or meta.get("name") or "导入的场景").strip() or "导入的场景")[:36]
        if base_name == self.custom_scene_name:
            base_name += " 导入"
        target, i = base_name, 2
        while os.path.isfile(self._scene_path(target)):
            target = f"{base_name} {i}"
            i += 1
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(self._scene_path(target), "w", encoding="utf-8") as fh:
                json.dump(scene, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            return {"ok": False, "error": f"保存失败：{exc}"}
        if plugins_added:
            self._refresh_plugins()
        target = self._canonical_scene(target)
        log(f"已导入场景「{target}」：{len(renamed)} 张图片，{plugins_added} 个自制插件")
        return {"ok": True, "name": target, "images": len(renamed), "plugins": plugins_added}

    def _canonical_scene(self, name: str) -> str:
        """场景名统一成文件名对应的写法（全角括号等会被换成 _），避免同一场景两种名字。"""
        if not name or name == self.custom_scene_name:
            return name
        return os.path.splitext(os.path.basename(self._scene_path(name)))[0]

    @staticmethod
    def _scene_has_content(data) -> bool:
        """场景里到底有没有内容：相册场景看有没有图片，普通场景看图片和非导航动作。"""
        if not isinstance(data, dict):
            return False
        if data.get("sceneType") == "album":
            alb = data.get("album") or {}
            return any(str(n) for n in (alb.get("items") or [])) if isinstance(alb, dict) else False
        for page in data.get("pages") or []:
            if not isinstance(page, dict):
                continue
            if page.get("images"):
                return True
            for act in (page.get("actions") or {}).values():
                if not (isinstance(act, dict) and act.get("type") == "nav"):
                    return True
        return False

    def _scene_file_data(self, path: str) -> dict | None:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def scene_hint(self, name: str) -> str:
        """场景切不了时给界面看的原因；返回空串表示可以切。"""
        name = self._canonical_scene(name)
        if name not in {it["name"] for it in self.list_scenes()}:
            if name in LEGACY_ALBUM_NAMES:
                return "「幻灯片」是待机画面，在右侧「待机画面 → 幻灯片」里设置，不占场景列表"
            return f"没有叫「{name}」的场景"
        target = (os.path.join(SCENE_DIR, "__custom__.json")
                  if name == self.custom_scene_name else self._scene_path(name))
        data = self._scene_file_data(target)
        if data is not None and data.get("sceneType") == "album":
            if not self._album_scene_items(name):
                return f"相册场景「{name}」里还没有图片，先在编辑区里拖几张图进去"
            return ""
        if data is not None and not self._scene_has_content(data):
            if name == self.custom_scene_name:
                return "「自定义」场景还是空的：先在界面上摆好按键，改动会自动存进「自定义」"
            return f"场景「{name}」里没有任何图片和动作，先给它加内容再切换"
        return ""

    def apply_scene(self, name: str) -> bool:
        name = self._canonical_scene(name)
        if not name:
            return False
        self.scene_rev += 1
        # 息屏状态下从控制台换场景：先亮屏，再切过去（否则会先补画旧场景、再被切走）
        if self.screen_off:
            self.screen_off = False
            self._blanked = False
            self.in_standby = False
            self._last_activity = time.time()
            try:
                self.wake_panel()
            except Exception:  # noqa: BLE001
                pass
            self.broadcast({"type": "status", "screenOff": False})
            log("换场景时屏幕是关的，先亮屏")
        if self.is_album_scene(name):
            if self._play_album_scene(name):
                return True
            log(f"相册场景「{name}」里还没有图片，切不过去")
            return False
        if self.album_active():
            self.preview_mode = "wallpaper"            # 退出相册，下面会整屏重画按键
            self.in_standby = False
            self.album_scene_name = ""
            self._album_scene = {}
            self._album_return = None
        if not self._auto_applying:
            self._auto_scene = None
        # 切走之前先把「自定义」里正在改的内容存一下，免得切回来少东西
        if (self.active_scene or self.custom_scene_name) == self.custom_scene_name:
            self._snapshot_custom_scene()
        target = (os.path.join(SCENE_DIR, "__custom__.json")
                  if name == self.custom_scene_name else self._scene_path(name))
        data = self._scene_file_data(target)
        if data is not None and not self._scene_has_content(data):
            # 空场景一切换就会把刚摆好的画面清空，宁可不切
            log(f"场景「{name}」里没有内容（图片和动作都是空的），不切换，免得把当前画面清掉")
            return False
        self._applying_scene = True
        try:
            if name == self.custom_scene_name:
                path = os.path.join(SCENE_DIR, "__custom__.json")
                ok = self._apply_scene_file(path)
                if ok:
                    self.active_scene = self.custom_scene_name
                    self._save_config()
                    log("已切换到自定义场景")
                return ok
            ok = self._apply_scene_file(self._scene_path(name))
            if ok:
                self.active_scene = name
                self._save_config()
                log(f"已切换到场景：{name}")
            return ok
        finally:
            self._applying_scene = False

    def delete_scene(self, name: str) -> bool:
        if name == self.custom_scene_name:
            return False
        path = self._scene_path(name)
        if not os.path.isfile(path):
            return False
        if self.active_scene == name:
            self.apply_scene(self.custom_scene_name)
        try:
            os.remove(path)
        except OSError:
            return False
        return True

    def _snapshot_custom_scene(self) -> None:
        """每次保存配置，把当前按键内容写回「正在使用的场景」——各场景各记各的改动。"""
        if getattr(self, "_suspend_snapshot", False) or getattr(self, "_applying_scene", False):
            return
        name = self.active_scene or self.custom_scene_name
        custom = name == self.custom_scene_name
        path = os.path.join(SCENE_DIR, "__custom__.json") if custom else self._scene_path(name)
        if not custom and not os.path.isfile(path):
            return                      # 场景文件已被删掉，不要再凭空建回来
        cfg = self.export_config()
        if not self._scene_has_content(cfg):
            # 当前画面是空的——多半是刚切场景失败 / 配置丢了。
            # 这时候写进快照会把原来存好的内容一起抹掉，所以只在快照本来就是空的时候才写。
            if os.path.isfile(path) and self._scene_has_content(self._scene_file_data(path)):
                log(f"当前画面是空的，没有写进场景「{name}」（避免覆盖已有内容）")
                return
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        for k in self.SCENE_KEYS:
            if k in cfg:
                data[k] = cfg[k]
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"保存场景「{name}」失败：{exc}")

    def _ensure_custom_snapshot(self) -> None:
        path = os.path.join(SCENE_DIR, "__custom__.json")
        if os.path.isfile(path):
            return
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.export_config(), fh, ensure_ascii=False, indent=2)
            log("已创建自定义场景快照")
        except OSError:
            pass

    # -- 官方场景 ---------------------------------------------------------- #
    # -- 官方场景同步 ------------------------------------------------------ #
    OFFICIAL_MARK = ".official-scenes.json"

    def _official_mark_path(self) -> str:
        return os.path.join(SCENE_DIR, self.OFFICIAL_MARK)

    def official_scene_names(self) -> set:
        try:
            with open(self._official_mark_path(), encoding="utf-8") as fh:
                return set(json.load(fh).get("names") or [])
        except (OSError, ValueError, AttributeError):
            return set()

    @staticmethod
    def _vk_reverse() -> dict:
        rev = {}
        for name, vk in VK_NAMES.items():
            rev.setdefault(vk, name)
        return rev

    SOURCES_FILE = os.path.join(UPLOAD_DIR, ".sources.json")

    def _sources(self) -> dict:
        cache = getattr(self, "_sources_cache", None)
        if cache is None:
            try:
                with open(self.SOURCES_FILE, encoding="utf-8") as fh:
                    cache = json.load(fh)
                if not isinstance(cache, dict):
                    cache = {}
            except (OSError, ValueError):
                cache = {}
            self._sources_cache = cache
        return cache

    def _record_sources(self, mapping: dict) -> None:
        """记下 uploads 里每个副本的原始来源（官方图标包 / 场景 / 插件），供「打开图标位置」用。"""
        src = self._sources()
        changed = False
        for name, path in mapping.items():
            if path and src.get(name) != path:
                src[name] = path
                changed = True
        if not changed:
            return
        try:
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(self.SOURCES_FILE, "w", encoding="utf-8") as fh:
                json.dump(src, fh, ensure_ascii=False, indent=1)
        except OSError as exc:
            log(f"记录图标来源失败：{exc}")

    def _store_scene_image(self, img, source: str | None = None) -> str:
        """场景图标按内容哈希存进 uploads（同一张图只存一份），并记下原始来源。"""
        ext, data = img[0], img[1]
        source = source or (img[2] if len(img) > 2 else None)
        name = f"sd_{hashlib.sha1(data).hexdigest()[:16]}{ext}"
        path = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(path):
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        if source and name not in self._sources():
            self._record_sources({name: source})
        return name

    def key_image_info(self, key: int) -> dict:
        """按键图标的本地副本和原始位置。"""
        item = self.key_overrides.get(key) or {}
        copy = item.get("file") or ""
        if not copy or not os.path.isfile(copy):
            return {"ok": False}
        source = copy
        if os.path.normpath(os.path.dirname(copy)) == os.path.normpath(UPLOAD_DIR):
            source = self._sources().get(os.path.basename(copy)) or ""
        low = source.lower()
        return {"ok": True, "copy": copy, "source": source,
                "sourceExists": bool(source and os.path.exists(source)),
                "sameFile": bool(source) and os.path.normpath(source) == os.path.normpath(copy),
                # 官方场景图标是加密的 .mbg，或者打包在预设压缩包里，直接看不了
                "encrypted": low.endswith((".mbg", ".streamdockprofile"))}

    def export_key_image(self, key: int) -> str | None:
        """把按键图标（已解密）复制到「图片 › M18 图标」，起个看得懂的名字。"""
        info = self.key_image_info(key)
        if not info.get("ok"):
            return None
        pictures = os.path.join(os.environ.get("USERPROFILE") or HERE, "Pictures", "M18 图标")
        scene = self.active_scene or self.custom_scene_name
        page = self.pages[self.page].get("name", "") if 0 <= self.page < len(self.pages) else ""
        stem = f"{scene}-{page}-键{key}" if page and page != scene else f"{scene}-键{key}"
        stem = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in stem).strip()
        ext = os.path.splitext(info["copy"])[1].lower() or ".png"
        try:
            os.makedirs(pictures, exist_ok=True)
            dst = os.path.join(pictures, stem + ext)
            shutil.copyfile(info["copy"], dst)
            return dst
        except OSError as exc:
            log(f"导出图标失败：{exc}")
            return None

    @staticmethod
    def reveal_in_explorer(path: str) -> bool:
        """在资源管理器里打开所在文件夹并选中该文件。"""
        path = os.path.normpath(path or "")
        if not path or not os.path.exists(path):
            return False
        try:
            subprocess.Popen(f'explorer /select,"{path}"')
            return True
        except OSError as exc:
            log(f"打开文件位置失败：{exc}")
            return False

    def index_official_sources(self) -> None:
        """老版本同步的官方场景没记来源：只转换不写场景文件，把来源补上。"""
        if any(str(v).startswith(core.SD_ROOT) or "HotSpot" in str(v) for v in self._sources().values()):
            return
        try:
            plugins = official.PluginIndex(core.SD_ROOT)
            rev = self._vk_reverse()
            mapping = {}
            for item in official.list_scene_sources():
                sc = official.convert_scene(item, plugins, core.SD_ROOT, rev)
                for page in sc["pages"]:
                    for img in page["images"].values():
                        if len(img) > 2 and img[2]:
                            name = f"sd_{hashlib.sha1(img[1]).hexdigest()[:16]}{img[0]}"
                            mapping.setdefault(name, img[2])
            self._record_sources(mapping)
            log(f"已补记官方场景图标来源 {len(mapping)} 个")
        except Exception as exc:  # noqa: BLE001
            log(f"补记图标来源失败：{exc}")

    # -- 官方资源本地备份 ---------------------------------------------------- #
    @staticmethod
    def _shrink_gif(data: bytes, size: int) -> bytes:
        """动图逐帧缩小（按键只有 64×64），保留每帧时长和循环；本来就小的原样返回。"""
        try:
            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) <= size:
                    return data
                frames, durations = [], []
                for frame in ImageSequence.Iterator(im):
                    f = frame.convert("RGBA")
                    f.thumbnail((size, size), Image.Resampling.LANCZOS)
                    frames.append(f)
                    durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
            buf = io.BytesIO()
            frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=durations,
                           loop=0, disposal=2, optimize=True)
            out = buf.getvalue()
            return out if len(out) < len(data) else data
        except Exception:  # noqa: BLE001
            return data

    def mirror_icons(self) -> int:
        """官方图标包备份到数据目录：PNG/JPG 缩成 144×144（按键只要 64×64），动图原样复制。"""
        root = os.path.join(MIRROR_DIR, "icons")
        made = 0
        for folder in official.icon_pack_dirs(core.SD_ROOT):
            if os.path.normpath(folder).startswith(os.path.normpath(root)):
                continue                                   # 已经是备份
            dst = os.path.join(root, os.path.basename(folder))
            info = official.icon_pack_info(folder)
            os.makedirs(os.path.join(dst, "icons"), exist_ok=True)
            for fname in os.listdir(folder):
                if fname.lower().endswith(".json"):
                    shutil.copyfile(os.path.join(folder, fname), os.path.join(dst, fname))
            for src in info["files"]:
                name = os.path.basename(src)
                ext = os.path.splitext(name)[1].lower()
                out = os.path.join(dst, "icons", name if ext in (".gif",) else os.path.splitext(name)[0] + ".png")
                if os.path.isfile(out):
                    continue
                try:
                    if ext in (".gif", ".mbg"):
                        img = official.read_image(src)
                        if img:
                            with open(os.path.join(dst, "icons", os.path.splitext(name)[0] + ".gif"), "wb") as fh:
                                fh.write(self._shrink_gif(img[1], 72))
                    else:
                        with Image.open(src) as im:
                            im = im.convert("RGBA")
                            im.thumbnail((96, 96), Image.Resampling.LANCZOS)
                            im.save(out, "PNG", optimize=True)
                    made += 1
                except Exception:  # noqa: BLE001
                    continue
        if made:
            log(f"官方图标包已备份到本地：新增 {made} 个图标")
            self._catalog(refresh=True)
        return made

    def mirror_plugins(self) -> dict:
        """第三方插件 + 官方自带的 Node 复制到数据目录：卸载官方驱动后插件照样能跑。"""
        root = os.path.join(MIRROR_DIR, "plugins")
        os.makedirs(root, exist_ok=True)
        copied = 0
        for p in self._catalog()["plugins"].values():
            folder = p["folder"]
            if p.get("builtin") or p.get("custom") or os.path.normpath(folder).startswith(os.path.normpath(MIRROR_DIR)):
                continue
            dst = os.path.join(root, os.path.basename(folder))
            man_old = official._plugin_manifest(dst) or {}
            if man_old.get("Version") == p.get("version") and os.path.isdir(dst):
                continue
            shutil.rmtree(dst, ignore_errors=True)
            try:
                shutil.copytree(folder, dst)
                copied += 1
            except OSError as exc:
                log(f"备份插件失败 {p['id']}：{exc}")
        node_src = os.path.join(core.SD_ROOT, "node", "node20.exe")
        node_dst = os.path.join(MIRROR_DIR, "node", "node20.exe")
        if os.path.isfile(node_src) and (not os.path.isfile(node_dst)
                                         or os.path.getsize(node_src) != os.path.getsize(node_dst)):
            os.makedirs(os.path.dirname(node_dst), exist_ok=True)
            shutil.copyfile(node_src, node_dst)
        if self.plugin_host:
            self.plugin_host.fallback_node = node_dst
        log(f"官方插件已备份到本地：{copied} 个")
        self._refresh_plugins()
        return {"plugins": copied, "node": os.path.isfile(node_dst)}

    def _auto_mirror_icons(self) -> None:
        time.sleep(20)                                     # 等启动忙完再在后台慢慢做
        try:
            self.mirror_icons()
        except Exception as exc:  # noqa: BLE001
            log(f"备份图标包失败：{exc}")

    def official_ready(self) -> bool:
        """官方 Stream Dock 驱动装没装：没装时「同步官方」没有东西可以同步。"""
        root = core.SD_ROOT or ""
        if not root or not os.path.isdir(root):
            return False
        return os.path.basename(os.path.normpath(root)).lower() != "m18-no-official"

    def sync_official(self, mode: str = "new", include_plugins: bool = False,
                      include_scenes: bool = True, include_icons: bool = True) -> dict:
        """「同步官方」按钮。

        可以只挑一部分同步：include_scenes / include_icons / include_plugins。
        场景按 mode 只加新的（new）或全部覆盖重导（all）。
        """
        if include_scenes or include_icons or include_plugins:
            self._catalog(refresh=True)
        if self.plugin_host and (include_scenes or include_icons or include_plugins):
            plugins = {}
            for uid, p in self._catalog()["plugins"].items():
                man = official._plugin_manifest(p["folder"])
                if man:
                    plugins[uid] = {"folder": p["folder"], "manifest": man}
            self.plugin_host.register_plugins(plugins)
        result = {"scenes": []}
        if include_scenes:
            result = self.sync_official_scenes(only_new=(mode != "all"))
        if include_icons:
            result["iconsMirrored"] = self.mirror_icons()
        mirrored_plugins = 0
        if include_plugins:
            mirrored = self.mirror_plugins()
            mirrored_plugins = mirrored.get("plugins", 0)
            result["node"] = mirrored.get("node", False)
        active = self.active_scene
        if mode == "all" and active and active in [r["name"] for r in result["scenes"]]:
            self.apply_scene(active)          # 当前场景被重导了，重新载入
        cat = self._catalog()
        result.update({"packs": len(cat["packs"]), "icons": sum(len(p["files"]) for p in cat["packs"].values()),
                       "plugins": len(cat["plugins"]), "mirroredPlugins": mirrored_plugins})
        return result

    def sync_official_scenes(self, only_new: bool = False) -> dict:
        """把官方驱动里 M18 的全部场景（含预设）转换后写进场景列表。不会切换当前场景。"""
        plugins = official.PluginIndex(core.SD_ROOT)
        rev = self._vk_reverse()
        base = self.export_config()
        report, names = [], []
        os.makedirs(SCENE_DIR, exist_ok=True)
        existing = self.official_scene_names()
        for item in official.list_scene_sources():
            if only_new and os.path.isfile(self._scene_path(item["name"].strip()[:40])):
                names.append(item["name"].strip()[:40])
                continue
            try:
                sc = official.convert_scene(item, plugins, core.SD_ROOT, rev)
            except Exception as exc:  # noqa: BLE001
                log(f"官方场景「{item.get('name')}」转换失败：{exc}")
                continue
            multi = len(sc["pages"]) > 1
            pages = []
            for i, page in enumerate(sc["pages"]):
                images = {}
                for key, img in page["images"].items():
                    try:
                        images[str(key)] = self._store_scene_image(img)
                    except OSError as exc:
                        log(f"保存场景图标失败：{exc}")
                actions = {str(k): v for k, v in page["actions"].items()}
                for k, nav in ((16, "上一页"), (17, "主页"), (18, "下一页")):
                    actions.setdefault(str(k), {"type": "nav", "name": nav})
                pages.append({
                    "name": f"第 {i + 1} 页" if multi else sc["name"],
                    "images": images,
                    "actions": actions,
                    "styles": {str(k): v for k, v in page["styles"].items()},
                })
            name = sc["name"].strip()[:40]
            cfg = dict(base)
            cfg.update({"pages": pages, "page": 0, "officialSource": item["id"],
                        "activeScene": name})
            app_id = str(item["manifest"].get("AppIdentifier") or "").strip()
            if app_id and app_id != "*":
                cfg["apps"] = [os.path.basename(app_id).lower()]
            try:
                with open(self._scene_path(name), "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, ensure_ascii=False, indent=2)
            except OSError as exc:
                log(f"写入场景「{name}」失败：{exc}")
                continue
            names.append(name)
            report.append({"name": name, "kind": item["kind"], **sc["stats"]})
        names = sorted(set(names) | (existing if only_new else set()))
        try:
            with open(self._official_mark_path(), "w", encoding="utf-8") as fh:
                json.dump({"names": names, "time": int(time.time()), "pluginsMigrated": True},
                          fh, ensure_ascii=False)
        except OSError:
            pass
        log("官方场景同步完成：" + "，".join(
            f"{r['name']}（{r['supported']}/{r['total']} 键可执行，{r['pages']} 页）" for r in report))
        return {"scenes": report}

    def _seed_presets(self) -> None:
        """首次运行：把随包带的默认场景（音乐控制台 / 相册1）装进用户数据目录。

        只在用户还没有任何场景文件时做一次；相册图片同时拷进上传目录（场景里记的是文件名）。
        """
        try:
            preset_scenes = os.path.join(PRESET_DIR, "scenes")
            if not os.path.isdir(preset_scenes):
                return
            existing = []
            if os.path.isdir(SCENE_DIR):
                existing = [n for n in os.listdir(SCENE_DIR)
                            if n.endswith(".json") and not n.startswith(".") and n != "__custom__.json"]
            if existing:
                return
            os.makedirs(SCENE_DIR, exist_ok=True)
            album_src = os.path.join(PRESET_DIR, "album1")
            if os.path.isdir(album_src):
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                for name in sorted(os.listdir(album_src)):
                    src = os.path.join(album_src, name)
                    dst = os.path.join(UPLOAD_DIR, name)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        shutil.copy2(src, dst)
            names = []
            for name in sorted(os.listdir(preset_scenes)):
                if not name.endswith(".json"):
                    continue
                shutil.copy2(os.path.join(preset_scenes, name), os.path.join(SCENE_DIR, name))
                names.append(os.path.splitext(name)[0])
            if names:
                log(f"已载入默认场景：{' / '.join(names)}")
        except Exception as exc:  # noqa: BLE001
            log(f"载入默认场景失败：{exc}")

    def _library_seed_existing(self) -> None:
        """「相册」分类还是空的时候，把当前壁纸和待机幻灯片里的图先收进去。"""
        try:
            if self.library_items():
                return
            paths = []
            wp = self.wallpaper_file
            if wp:
                p = wp if os.path.isabs(wp) else data_path(wp)
                if p and os.path.isfile(p):
                    paths.append(p)
            items = list(self.album_items or [])
            for idx in range(min(len(items), 60)):
                p = self._album_file_for(items, idx)
                if p:
                    paths.append(p)
            if paths:
                n = self.library_collect(paths)
                if n:
                    log(f"相册分类已收进 {n} 张（当前壁纸和待机幻灯片）")
        except Exception as exc:  # noqa: BLE001
            log(f"收集现有图片失败：{exc}")

    def _migrate_official_plugins(self) -> None:
        """已同步过的官方场景里，插件控件（时钟 / 天气等）当时没接上：一次性补上插件动作。
        只补「没有动作」的键，不覆盖用户改过的内容；当前正在用的场景也同步改 config.json。"""
        mark = self._official_mark_path()
        try:
            with open(mark, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return
        if meta.get("pluginsMigrated") or not meta.get("names"):
            return
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
                live = json.load(fh)
        except (OSError, ValueError):
            live = {}
        plugins = official.PluginIndex(core.SD_ROOT)
        rev = self._vk_reverse()
        sources = {it["name"]: it for it in official.list_scene_sources()}
        added_total = 0
        for name in meta["names"]:
            item = sources.get(name)
            path = self._scene_path(name)
            if not item or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    scene = json.load(fh)
                conv = official.convert_scene(item, plugins, core.SD_ROOT, rev)
            except Exception as exc:  # noqa: BLE001
                log(f"补全官方场景插件失败（{name}）：{exc}")
                continue
            targets = [scene["pages"]]
            if live.get("activeScene") == name and isinstance(live.get("pages"), list):
                targets.append(live["pages"])
            added = 0
            for pages in targets:
                for i, page in enumerate(conv["pages"]):
                    if i >= len(pages):
                        break
                    acts = pages[i].setdefault("actions", {})
                    for key, spec in page["actions"].items():
                        if spec.get("type") == "plugin" and str(key) not in acts:
                            acts[str(key)] = dict(spec)
                            added += 1
            if added:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(scene, fh, ensure_ascii=False, indent=2)
                added_total += added
        if live.get("pages") and added_total:
            with self._save_lock:
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(live, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)
        meta["pluginsMigrated"] = True
        with open(mark, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
        log(f"官方场景插件控件补全：新增 {added_total} 个插件动作")

    def _auto_sync_official(self) -> None:
        """首次启动自动同步一次；之后用户删掉的官方场景不会再自己冒出来。"""
        if os.path.isfile(self._official_mark_path()):
            return
        try:
            self.sync_official_scenes()
        except Exception as exc:  # noqa: BLE001
            log(f"自动同步官方场景失败：{exc}")

    def _scene_usable(self, it: dict) -> bool:
        """这个场景现在切得过去吗：停用的跳过，空相册 / 空场景也跳过。"""
        if it.get("muted"):
            return False
        if it.get("sceneType") == "album":
            return bool(self._album_scene_items(it["name"]))
        path = (os.path.join(SCENE_DIR, "__custom__.json")
                if it.get("custom") else self._scene_path(it["name"]))
        data = self._scene_file_data(path)
        return data is None or self._scene_has_content(data)

    def _step_scene(self, step: int) -> bool:
        items = self.list_scenes()
        names = [it["name"] for it in items]
        cur = self.current_scene()
        usable = {it["name"] for it in items if self._scene_usable(it)}
        idx = names.index(cur) if cur in names else -1
        target = None
        for hop in range(1, len(names) + 1):
            cand = names[(idx + step * hop) % len(names)]
            if cand in usable and cand != cur:
                target = cand
                break
        if not target:
            log("没有可切换的场景（其他场景都停用了或是空的）")
            return False
        if cur != target:
            self._scene_history.append(cur)
            del self._scene_history[:-20]
        ok = self.apply_scene(target)
        self.broadcast({"type": "scene", "name": target, "ok": ok})
        return ok

    def next_scene(self) -> bool:
        """官方「场景切换」键：按场景列表顺序切到下一个。"""
        return self._step_scene(1)

    def swap_keys(self, a: int, b: int) -> bool:
        top = KEY_COUNT + 3
        if a == b or not (1 <= a <= top) or not (1 <= b <= top):
            return False
        for store in (self.key_overrides, self.key_actions, self.key_styles, self.key_gestures):
            va, vb = store.get(a), store.get(b)
            if vb is None:
                store.pop(a, None)
            else:
                store[a] = vb
            if va is None:
                store.pop(b, None)
            else:
                store[b] = va
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def copy_key(self, key: int) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):      # 16/17/18 是三个导航键，也要能复制
            return False
        self.clipboard = {
            "image": copy.deepcopy(self.key_overrides.get(key)),
            "action": copy.deepcopy(self.key_actions.get(key)),
            "style": copy.deepcopy(self.key_styles.get(key)),
            "gestures": copy.deepcopy(self.key_gestures.get(key)),
        }
        return any(self.clipboard.values())

    def paste_key(self, key: int) -> bool:
        if not self.clipboard or not (1 <= key <= KEY_COUNT + 3):
            return False
        for name, store in (("image", self.key_overrides),
                            ("action", self.key_actions),
                            ("style", self.key_styles),
                            ("gestures", self.key_gestures)):
            val = self.clipboard.get(name)
            if val:
                store[key] = copy.deepcopy(val)
                if name == "action" and isinstance(store[key], dict) and store[key].get("type") == "plugin":
                    store[key]["context"] = os.urandom(16).hex()
            else:
                store.pop(key, None)
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    def clear_key_all(self, key: int) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):
            return False
        self.key_overrides.pop(key, None)
        self.key_actions.pop(key, None)
        self.key_styles.pop(key, None)
        self.key_gestures.pop(key, None)
        if self._drop_orphan_folders():
            self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        return True

    LONG_PRESS_SEC = 0.5
    DOUBLE_GAP_SEC = 0.3

    def _gesture(self, key: int, state: int, g: dict) -> None:
        """设了长按 / 双击的键：长按 ≥0.5 秒触发长按；两次点击间隔 <0.3 秒触发双击；否则是短按。"""
        st = self._press.setdefault(key, {})
        run = lambda act: threading.Thread(target=self._run_action, args=(key, act), daemon=True).start()  # noqa: E731

        def short() -> None:
            # 相册场景里的导航键，短按就是 上页 / 主场景 / 下页（别去跑那条 nav 动作）
            if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
                threading.Thread(target=self._album_nav_step, args=(key,), daemon=True).start()
                return
            run(None)
        if state == 1:
            st["down"] = time.time()
            st["long_fired"] = False
            if g.get("long"):
                down = st["down"]

                def fire_long():
                    if st.get("down") == down and not st.get("released"):
                        st["long_fired"] = True
                        log(f"按键 {key} → 长按")
                        run(g["long"])
                st["released"] = False
                st["long_timer"] = threading.Timer(self.LONG_PRESS_SEC, fire_long)
                st["long_timer"].start()
            return
        st["released"] = True
        down = st.get("down")
        if down:
            held = (time.time() - down) * 1000
            log(f"按键 {key} 松开，按住 {held:.0f}ms（长按阈值 {self.LONG_PRESS_SEC * 1000:.0f}ms）")
        if st.get("long_timer"):
            st["long_timer"].cancel()
        if st.get("long_fired"):
            return
        if g.get("double"):
            pending = st.get("single_timer")
            if pending and pending.is_alive():
                pending.cancel()
                st["single_timer"] = None
                log(f"按键 {key} → 双击")
                run(g["double"])
                return
            st["single_timer"] = threading.Timer(self.DOUBLE_GAP_SEC, short)
            st["single_timer"].start()
        else:
            short()

    def _run_action(self, key: int, act: dict | None = None) -> None:
        act = act if act is not None else self.key_actions.get(key)
        if not act:
            return
        kind = act.get("type") or "launch"

        if kind == "plugin":
            # 插件动作被设成短按 / 长按 / 双击时，这里补一对按下+松开，插件照常收到 keyDown / keyUp
            self._plugin_key(key, act, 1)
            self._plugin_key(key, act, 0)
            log(f"按键 {key} → 插件 {act.get('plugin')}")
            return

        if kind == "console":
            msg = self.console_action(str(act.get("op") or ""))
            log(f"按键 {key} → 控制台：{msg}")
            return

        if kind == "folder":
            ok = self.open_folder(str(act.get("page") or ""))
            log(f"按键 {key} → 打开文件夹「{act.get('name')}」" + ("" if ok else "（子页不存在）"))
            return

        if kind == "nav":
            name = act.get("name") or ""
            if name in ("上一页", "下一页", "主页", "返回上级"):
                self.add_nav_pages(name)
            elif name == "下一场景":
                self.next_scene()
            elif name == "返回上一场景":
                self.back_scene()
            elif name.startswith("跳到第"):
                digits = "".join(ch for ch in name if ch.isdigit())
                mains = self._main_pages()
                if digits and 0 < int(digits) <= len(mains):
                    self.goto_page(mains[int(digits) - 1])
            else:
                self.broadcast({"type": "nav", "key": key, "name": name})
            log(f"按键 {key} → 导航：{name}")
            return

        if kind == "macro":
            steps = act.get("steps") or []
            threading.Thread(target=self._run_macro, args=(key, steps), daemon=True).start()
            return

        if kind == "media" and act.get("action") in ("brightup", "brightdown"):
            step = 10 if act.get("action") == "brightup" else -10
            self.screen_brightness = max(0, min(100, int(self.screen_brightness) + step))
            self.device.set_screen_brightness(self.screen_brightness)
            self.broadcast({"type": "status", "screenBrightness": self.screen_brightness})
            log(f"按键 {key} → 屏幕亮度 {self.screen_brightness}")
            return

        if kind == "media":
            action = act.get("action", "")
            vk = MEDIA_LABELS.get(action, ("", ""))[0]
            ok = bool(vk) and send_hotkey(vk)
            log(f"按键 {key} → 媒体键 {MEDIA_LABELS.get(action, ('', action))[1]} {'✓' if ok else '✗'}")
            return

        if kind == "hotkey":
            combo = act.get("keys", "")
            ok = send_hotkey(combo)
            log(f"按键 {key} → 热键 {combo} {'✓' if ok else '✗ 组合键无法识别'}")
            return

        if kind == "toggle":
            keys = act.get("keys") or []
            if not keys:
                return
            st = int(act.get("state") or 0) % len(keys)
            ok = send_hotkey(keys[st])
            act["state"] = (st + 1) % len(keys)
            log(f"按键 {key} → 双状态热键 {keys[st]}（切到状态 {act['state'] + 1}）{'✓' if ok else '✗'}")
            self._save_config()
            if self.device.connected and 1 <= key <= KEY_COUNT and not self.in_standby:
                self.device.send_image(key, self._render_key(key))
                self.device.refresh()
            self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
            return

        if kind == "text":
            ok = type_text(act.get("text", ""), bool(act.get("enter")))
            log(f"按键 {key} → 输入{'密码' if act.get('secret') else '文本'} {'✓' if ok else '✗'}")
            return

        if kind == "mouse":
            ok = mouse_action(act.get("button", "click"), act.get("pos", ""))
            log(f"按键 {key} → {MOUSE_LABELS.get(act.get('button', ''), '鼠标')} {'✓' if ok else '✗'}")
            return

        if kind == "window":
            ok = window_action(act.get("op", ""))
            log(f"按键 {key} → {WINDOW_LABELS.get(act.get('op', ''), '窗口操作')} {'✓' if ok else '✗'}")
            return

        if kind == "close":
            ok = close_target(act.get("target", ""))
            log(f"按键 {key} → 关闭 {act.get('target') or '前台窗口'} {'✓' if ok else '✗'}")
            return

        if kind == "scene":
            name = act.get("scene", "")
            self._scene_history.append(self.current_scene())
            del self._scene_history[:-20]
            ok = self.apply_scene(name)
            self.broadcast({"type": "scene", "name": name, "ok": ok})
            log(f"按键 {key} → 切换场景 {name} {'✓' if ok else '✗'}")
            return

        if kind == "sound":
            path = act.get("path", "")
            if path and os.path.isfile(path):
                self.sound.play(path)
                log(f"按键 {key} → 播放音效 {os.path.basename(path)}")
            else:
                self.sound.stop()
                log(f"按键 {key} → 停止音效")
            return

        if kind == "url":
            url = act.get("url", "")
            if not url:
                return
            try:
                os.startfile(url)
                log(f"按键 {key} → 网页 {url}")
            except Exception as exc:  # noqa: BLE001
                log(f"打开网页失败：{exc}")
            return

        path = act.get("path") or ""
        if not os.path.exists(path):
            log(f"动作路径不存在：{path}")
            return
        try:
            os.startfile(path)  # exe / lnk / 文件夹 / 文档
            log(f"按键 {key} → {os.path.basename(path)}")
        except Exception as exc:  # noqa: BLE001
            log(f"执行失败 {path}: {exc}")

    @staticmethod
    def _parse_macro(raw: str) -> list:
        steps = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            op = parts[0].lower()
            val = parts[1].strip() if len(parts) > 1 else ""
            if op in ("delay", "wait"):
                try:
                    steps.append({"op": "delay", "sec": float(val)})
                except ValueError:
                    pass
            elif op in ("hotkey", "key"):
                if val:
                    steps.append({"op": "hotkey", "keys": val})
            elif op == "url":
                if val:
                    steps.append({"op": "url", "url": val})
            elif op in ("text", "type"):
                if val:
                    steps.append({"op": "text", "text": val})
            elif op in ("launch", "open", "run"):
                path = data_path(val)
                if val and os.path.exists(path):
                    steps.append({"op": "launch", "path": path})
        return steps

    def _build_action(self, spec: dict) -> dict | None:
        """界面提交的动作 → 存储格式；内容不合法返回 None。"""
        kind = spec.get("type") or "launch"
        if kind == "nav":
            name = str(spec.get("name") or "")
            return {"type": "nav", "name": name} if name else None
        if kind == "hotkey":
            keys = str(spec.get("keys") or "").strip()
            return {"type": "hotkey", "keys": keys} if keys else None
        if kind == "toggle":
            keys = [k.strip() for k in str(spec.get("keys") or "").split("|") if k.strip()]
            return {"type": "toggle", "keys": keys[:2], "state": 0} if len(keys) >= 2 else None
        if kind == "media":
            action = str(spec.get("action") or "")
            return {"type": "media", "action": action} if action in MEDIA_LABELS else None
        if kind == "macro":
            raw = str(spec.get("macro") or "")
            steps = self._parse_macro(raw)
            return {"type": "macro", "steps": steps, "macro": raw} if steps else None
        if kind == "url":
            url = str(spec.get("url") or "").strip()
            return {"type": "url", "url": url} if url else None
        if kind == "text":
            text = str(spec.get("text") or "")
            return {"type": "text", "text": text, "enter": bool(spec.get("enter")),
                    "secret": bool(spec.get("secret"))} if text else None
        if kind == "mouse":
            button = str(spec.get("button") or "click")
            return {"type": "mouse", "button": button, "pos": str(spec.get("pos") or "").strip()} \
                if button in MOUSE_LABELS else None
        if kind == "close":
            return {"type": "close", "target": str(spec.get("target") or "").strip()}
        if kind == "window":
            op = str(spec.get("op") or "")
            return {"type": "window", "op": op} if op in WINDOW_LABELS else None
        if kind == "scene":
            name = str(spec.get("scene") or "").strip()
            return {"type": "scene", "scene": name} if name else None
        if kind == "sound":
            path = str(spec.get("path") or "").strip()
            if path and not os.path.isfile(path):
                return None
            return {"type": "sound", "path": path}
        if kind == "console":
            op = str(spec.get("op") or "")
            return {"type": "console", "op": op} if op in CONSOLE_LABELS else None
        raw = str(spec.get("path") or "")
        path = data_path(raw)
        if not raw or not os.path.exists(path):
            return None
        return {"type": "launch", "path": path}

    def reset_nav_key(self, key: int) -> bool:
        """把 16/17/18 还原成默认：只保留短按（上一页 / 主页 / 下一页）。"""
        if key not in self.ALBUM_NAV:
            return False
        spec = {"type": "nav", "name": self.ALBUM_NAV[key]}
        self.key_gestures.pop(key, None)
        self.key_actions[key] = dict(spec)
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            self._album_nav_write(self.album_scene_name, key, spec, "")
            for g in ("long", "double"):
                self._album_nav_write(self.album_scene_name, key, None, g)
        self._save_config()
        if self.device.connected and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        log(f"按键 {key} 已还原默认：{spec['name']}")
        return True

    def set_key_action(self, key: int, spec: dict) -> bool:
        if not (1 <= key <= KEY_COUNT + 3):
            return False
        if spec.get("type") == "folder":
            gesture = spec.get("gesture") if spec.get("gesture") in ("long", "double") else ""
            cur = (self.key_gestures.get(key) or {}).get(gesture) if gesture else self.key_actions.get(key)
            name = str(spec.get("name") or "").strip()[:20] or "文件夹"
            if isinstance(cur, dict) and cur.get("type") == "folder" and self._page_index(cur.get("page", "")) >= 0:
                cur["name"] = name                      # 已经是文件夹：改名
                self.pages[self._page_index(cur["page"])]["name"] = name
                self._save_config()
                return True
            ok = self.create_folder(key, name, gesture).get("ok", False)
            if ok and self._drop_orphan_folders():
                self._bind_page()
            return ok
        action = self._build_action(spec)
        if action is None:
            return False
        gesture = spec.get("gesture")
        if gesture in ("long", "double"):
            self.key_gestures.setdefault(key, {})[gesture] = action
        else:
            self.key_actions[key] = action
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            # 相册场景的导航键存在场景文件里，切场景回来还在
            self._album_nav_write(self.album_scene_name, key, action,
                                  gesture if gesture in ("long", "double") else "")
        if self._drop_orphan_folders():           # 原来是文件夹、被换成别的动作：子页一并回收
            self._bind_page()
        self._save_config()
        if self.device.connected and 1 <= key <= KEY_COUNT and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True

    def clear_key_gesture(self, key: int, gesture: str) -> bool:
        g = self.key_gestures.get(key) or {}
        if gesture not in g:
            return False
        g.pop(gesture, None)
        if not g:
            self.key_gestures.pop(key, None)
        if self.album_active() and self.album_scene_name and key in self.ALBUM_NAV:
            self._album_nav_write(self.album_scene_name, key, None, gesture)
        if self._drop_orphan_folders():
            self._bind_page()
        self._save_config()
        return True

    def back_scene(self) -> bool:
        while self._scene_history:
            name = self._scene_history.pop()
            if name != self.current_scene():
                ok = self.apply_scene(name)
                self.broadcast({"type": "scene", "name": name, "ok": ok})
                return ok
        return False

    # -- 前台程序自动切换场景 ---------------------------------------------- #
    def _scene_links(self) -> dict:
        """exe 名（小写）→ 场景名。5 秒缓存。"""
        cache = getattr(self, "_links_cache", None)
        if cache and time.time() - cache[0] < 5:
            return cache[1]
        links = {}
        for item in self.list_scenes():
            if item.get("muted"):
                continue
            for app in item.get("apps") or []:
                links.setdefault(app.lower(), item["name"])
        self._links_cache = (time.time(), links)
        return links

    def set_scene_apps(self, name: str, apps) -> bool:
        if name == self.custom_scene_name:
            return False
        path = self._scene_path(name)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        cleaned = []
        for a in apps or []:
            a = os.path.basename(str(a).strip().strip('"')).lower()
            if a and not a.endswith(".exe"):
                a += ".exe"
            if a and a not in cleaned:
                cleaned.append(a)
        data["apps"] = cleaned
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self._links_cache = None
        log(f"场景「{name}」关联程序：{', '.join(cleaned) or '（无）'}")
        return True

    def _app_watch_loop(self) -> None:
        """前台切到关联了场景的程序 → 自动切过去；离开 → 切回原来的场景。"""
        while True:
            time.sleep(0.8)
            try:
                self._app_watch_tick(*foreground_app())
            except Exception as exc:  # noqa: BLE001
                log(f"前台程序监测异常：{exc}")
                time.sleep(5)

    def _app_watch_tick(self, exe: str, title: str) -> None:
        if not exe or "M18 控制台" in title:
            return            # 正在用控制台窗口本身，不来回切
        if exe in self.recent_apps:
            self.recent_apps.remove(exe)
        self.recent_apps.appendleft(exe)
        links = self._scene_links()
        target = links.get(exe)
        active = self._canonical_scene(self.active_scene or self.custom_scene_name)
        if target:
            if active != target:
                if not self._auto_scene:
                    self._auto_return = active
                self._auto_scene = target
                self._auto_apply(target, f"前台程序 {exe}")
        elif self._auto_scene and active == self._auto_scene:
            back = self._auto_return or self.custom_scene_name
            self._auto_scene = None
            self._auto_apply(back, "离开关联程序")

    def _auto_apply(self, name: str, why: str) -> None:
        self._auto_applying = True
        try:
            ok = self.apply_scene(name)
        finally:
            self._auto_applying = False
        log(f"自动切换场景 → {name}（{why}）{'✓' if ok else '✗'}")
        self.broadcast({"type": "scene", "name": name, "ok": ok})

    def clear_key_action(self, key: int, gesture: str = "") -> bool:
        if gesture in ("long", "double"):
            return self.clear_key_gesture(key, gesture)
        if key not in self.key_actions:
            return False
        self.key_actions.pop(key, None)
        if self._drop_orphan_folders():
            self._bind_page()
        self._save_config()
        return True

    def key_raw_path(self, key: int) -> str | None:
        """返回原始上传文件路径（动图用它做预览，能真正动起来）。"""
        item = self.key_overrides.get(key)
        if not item:
            return None
        path = item.get("file") or ""
        return path if path and os.path.isfile(path) else None

    def key_thumb(self, key: int) -> bytes | None:
        item = self.key_overrides.get(key)
        if not item:
            return None
        jpeg = item["jpeg"] if item["type"] == "image" else item["frames"][item["idx"]][0]
        return jpeg

    @staticmethod
    def _detect_official() -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq StreamDock.exe", "/NH"],
                capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return "StreamDock.exe" in out
        except Exception:  # noqa: BLE001
            return False
