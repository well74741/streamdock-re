"""灯效库与多页灯效场景的持久化与切换；用户 JavaScript 只交给独立浏览器运行时。

- 灯效库（左侧「灯效」分类）：每个灯效一份代码，存在 数据目录/effects/<名称>.json，
  缩略图在 effects/.thumbs/，排序在 effects/.order.json。
- 灯效场景：只存「页」的顺序（引用灯效库里的名称）和当前页，外加自己的按键层（见 app_scene_keys）。
  ◀ ▶ 长按切换灯效页。旧版本「一个场景一段代码」的数据在启动时自动拆成 灯效库条目 + 一页的场景。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time

from core import DATA_DIR, SCENE_DIR, UI_DIR, log

EFFECT_LIB_DIR = os.path.join(DATA_DIR, "effects")
EFFECT_THUMB_DIR = os.path.join(EFFECT_LIB_DIR, ".thumbs")
BLANK_EFFECT = ("// 空灯效场景：从左侧「灯效」分类拖入灯效\n"
                "M18FX.on('frame', () => { M18FX.ctx.fillStyle = '#000';"
                " M18FX.ctx.fillRect(0, 0, M18FX.width, M18FX.height); M18FX.render(); });\n")
# 历代随包示例（v1 初版、v2 中央单条波形、v3 三排波形、v4 星空）；只有原封不动的才换成当前版本
OLD_TEMPLATE_HASHES = {"39e19c56b10dc01d6fd50d6e8f90ff345f1e9c3f9ac2260e90a9c9333a171724",
                       "fe25a5c981309418e784416af6c7e8a0aa84fb144468f7417f51f0c425328f76",
                       "e59d6c75567854a34dfe28e2ad49a0e9fa1b6fc5f8f442d14885c5faed408032",
                       "9e0f091d42a0a44c93644f53d589d04b7d6c6c33fa98679ffe4254320c6a0d4e"}
DEFAULT_EFFECT_NAME = "打击垫"


def _code_hash(code: str) -> str:
    return hashlib.sha256(str(code or "").replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def _write_json(path: str, data) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".effect-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class EffectScenesMixin:
    EFFECT_CODE_LIMIT = 128 * 1024

    @staticmethod
    def default_effect_code() -> str:
        try:
            with open(os.path.join(UI_DIR, "effect-pad.js"), encoding="utf-8-sig") as fh:
                return fh.read()
        except OSError:
            return "// 示例暂未加载，请打开灯效技术文档中的示例代码。\n"

    # ===================================================================== #
    # 灯效库
    # ===================================================================== #
    @staticmethod
    def _effect_clean_name(name) -> str:
        return " ".join(str(name or "").split())[:40]

    @staticmethod
    def _effect_file(name: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (name or ""))[:40].strip()
        return os.path.join(EFFECT_LIB_DIR, (safe or "_") + ".json")

    def _effect_read(self, name: str) -> dict | None:
        try:
            with open(self._effect_file(name), encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("code"), str):
            return None
        data["name"] = str(data.get("name") or name)
        return data

    def _effect_order(self) -> list:
        try:
            with open(os.path.join(EFFECT_LIB_DIR, ".order.json"), encoding="utf-8") as fh:
                order = json.load(fh)
            return [str(n) for n in order] if isinstance(order, list) else []
        except (OSError, ValueError):
            return []

    def _effect_names(self) -> list:
        """灯效库里的名称，按用户排序（没排过的按建立时间排在后面）。"""
        if not os.path.isdir(EFFECT_LIB_DIR):
            return []
        found = {}
        for fn in os.listdir(EFFECT_LIB_DIR):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            path = os.path.join(EFFECT_LIB_DIR, fn)
            data = None
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                pass
            if isinstance(data, dict) and isinstance(data.get("code"), str):
                name = str(data.get("name") or fn[:-5])
                found[name] = os.path.getmtime(path)
        order = [n for n in self._effect_order() if n in found]
        rest = sorted((n for n in found if n not in order), key=lambda n: (found[n], n))
        return order + rest

    def list_effects(self) -> list:
        used = {}
        for scene in self._effect_scene_names():
            for item in self._effect_scene_items(scene):
                used.setdefault(item, []).append(scene)
        running = getattr(self, "_effect_lib_running", "") if getattr(self, "effect_active", lambda: False)() else ""
        out = []
        for name in self._effect_names():
            data = self._effect_read(name) or {}
            thumb = os.path.join(EFFECT_THUMB_DIR, os.path.basename(self._effect_file(name))[:-5] + ".jpg")
            out.append({"name": name, "description": str(data.get("description") or ""),
                        "thumb": int(os.path.getmtime(thumb)) if os.path.isfile(thumb) else 0,
                        "scenes": used.get(name, []), "active": name == running})
        return out

    def get_effect(self, name: str) -> dict:
        name = self._effect_clean_name(name)
        data = self._effect_read(name) if name else None
        if not data:
            return {"ok": False, "error": f"灯效库里没有「{name}」"}
        return {"ok": True, "name": data["name"], "code": data["code"],
                "description": str(data.get("description") or "")}

    def effect_thumb(self, name: str) -> bytes | None:
        path = os.path.join(EFFECT_THUMB_DIR, os.path.basename(self._effect_file(self._effect_clean_name(name)))[:-5] + ".jpg")
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def save_effect(self, name: str, code: str, description: str = "", original: str = "") -> dict:
        """新建 / 更新 / 改名灯效库条目；改名时同步各灯效场景里的引用；正在运行的这一页热重载。"""
        name = self._effect_clean_name(name)
        original = self._effect_clean_name(original)
        if not name:
            return {"ok": False, "error": "灯效名称不能为空"}
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "灯效代码不能为空"}
        if len(code.encode("utf-8")) > self.EFFECT_CODE_LIMIT:
            return {"ok": False, "error": "灯效代码不能超过 128 KB"}
        with self._save_lock:
            exists = self._effect_read(name)
            if exists and original != name:
                return {"ok": False, "error": "灯效库里已有同名灯效；更新时需明确原名称"}
            if original and original != name and not self._effect_read(original):
                return {"ok": False, "error": "原灯效不存在"}
            try:
                _write_json(self._effect_file(name), {"name": name, "description": str(description or "")[:300],
                                                      "code": code})
                if original and original != name:
                    os.remove(self._effect_file(original))
                    old_thumb = os.path.join(EFFECT_THUMB_DIR, os.path.basename(self._effect_file(original))[:-5] + ".jpg")
                    if os.path.isfile(old_thumb):
                        os.replace(old_thumb, os.path.join(
                            EFFECT_THUMB_DIR, os.path.basename(self._effect_file(name))[:-5] + ".jpg"))
                    order = [name if n == original else n for n in self._effect_order()]
                    _write_json(os.path.join(EFFECT_LIB_DIR, ".order.json"), order)
            except OSError as exc:
                return {"ok": False, "error": f"保存灯效失败：{exc}"}
        if original and original != name:
            for scene in self._effect_scene_names():
                self._effect_scene_update(scene, lambda eff: eff.update(
                    items=[name if n == original else n for n in eff.get("items") or []]))
        running = getattr(self, "_effect_lib_running", "")
        active = getattr(self, "effect_active", lambda: False)() and running in (name, original)
        if active:
            self._effect_lib_running = name
            self._run_effect_code(name, code)
        self.broadcast({"type": "effectlibrary"})
        log(f"已保存灯效：{name}")
        return {"ok": True, "name": name, "active": active}

    def delete_effect(self, name: str) -> dict:
        name = self._effect_clean_name(name)
        if not self._effect_read(name):
            return {"ok": False, "error": f"灯效库里没有「{name}」"}
        try:
            os.remove(self._effect_file(name))
        except OSError as exc:
            return {"ok": False, "error": f"删除失败：{exc}"}
        thumb = os.path.join(EFFECT_THUMB_DIR, os.path.basename(self._effect_file(name))[:-5] + ".jpg")
        if os.path.isfile(thumb):
            os.remove(thumb)
        for scene in self._effect_scene_names():
            self._effect_scene_update(scene, lambda eff: eff.update(
                items=[n for n in eff.get("items") or [] if n != name]))
        if getattr(self, "effect_active", lambda: False)() and getattr(self, "_effect_lib_running", "") == name:
            if self.is_effect_scene(self.active_scene):
                self._effect_play_page(self.active_scene)
            else:
                self.stop_effect_scene()
        self.broadcast({"type": "effectlibrary"})
        log(f"已删除灯效：{name}")
        return {"ok": True}

    def reorder_effects(self, names) -> dict:
        known = self._effect_names()
        order = [n for n in (names or []) if n in known]
        order += [n for n in known if n not in order]
        _write_json(os.path.join(EFFECT_LIB_DIR, ".order.json"), order)
        self.broadcast({"type": "effectlibrary"})
        return {"ok": True, "items": order}

    def create_effect(self, name: str = "") -> dict:
        name = self._effect_clean_name(name)
        if not name:
            i = 1
            while self._effect_read(f"灯效 {i}"):
                i += 1
            name = f"灯效 {i}"
        if self._effect_read(name):
            return {"ok": False, "error": "灯效库里已有同名灯效"}
        return self.save_effect(name, self.default_effect_code(), "科幻音频打击垫：星空、音频波形和马赛克扩散")

    def _effect_unique(self, base: str, code: str) -> str:
        """导入/迁移用：同名同代码复用，同名不同代码加序号。"""
        base = self._effect_clean_name(base) or "灯效"
        name, i = base, 2
        while True:
            cur = self._effect_read(name)
            if cur is None:
                return name
            if _code_hash(cur.get("code")) == _code_hash(code):
                return name
            name = f"{base[:36]} {i}"
            i += 1

    def _effect_capture_thumb(self, name: str, generation: int) -> None:
        """运行约 2.5 秒后把整板预览存成库里的缩略图（同一灯效每次运行最多存一次）。"""
        def work():
            time.sleep(2.5)
            if getattr(self, "_effect_generation", -1) != generation or not self.effect_active():
                return
            jpeg = self.effect_preview()
            if not jpeg:
                return
            try:
                os.makedirs(EFFECT_THUMB_DIR, exist_ok=True)
                path = os.path.join(EFFECT_THUMB_DIR, os.path.basename(self._effect_file(name))[:-5] + ".jpg")
                with open(path + ".tmp", "wb") as fh:
                    fh.write(jpeg)
                os.replace(path + ".tmp", path)
                self.broadcast({"type": "effectlibrary"})
            except OSError:
                pass
        threading.Thread(target=work, daemon=True, name="m18-fx-thumb").start()

    # ===================================================================== #
    # 灯效场景（多页）
    # ===================================================================== #
    def is_effect_scene(self, name: str) -> bool:
        data = self._scene_file_data(self._scene_path(name)) if name else None
        return bool(data and data.get("sceneType") == "effect")

    def _effect_scene_names(self) -> list:
        if not os.path.isdir(SCENE_DIR):
            return []
        out = []
        for fn in os.listdir(SCENE_DIR):
            if not fn.endswith(".json") or fn.startswith((".", "__")):
                continue
            data = self._scene_file_data(os.path.join(SCENE_DIR, fn))
            if data and data.get("sceneType") == "effect":
                out.append(self._canonical_scene(fn[:-5]))
        return out

    def _effect_scene_items(self, name: str) -> list:
        data = self._scene_file_data(self._scene_path(name)) if name else None
        eff = (data or {}).get("effect") if isinstance(data, dict) else None
        return [str(n) for n in (eff or {}).get("items") or [] if str(n)] if isinstance(eff, dict) else []

    def _effect_scene_update(self, name: str, fn) -> dict | None:
        """改灯效场景的 effect 字段（items / index），原子写回；返回新的 effect。"""
        path = self._scene_path(name)
        with self._save_lock:
            data = self._scene_file_data(path)
            if not data or data.get("sceneType") != "effect":
                return None
            eff = data.get("effect") if isinstance(data.get("effect"), dict) else {}
            eff = {"items": [str(n) for n in eff.get("items") or [] if str(n)],
                   "index": int(eff.get("index") or 0), "description": str(eff.get("description") or "")[:300]}
            fn(eff)
            n = len(eff["items"])
            eff["index"] = max(0, min(n - 1, int(eff.get("index") or 0))) if n else 0
            data["effect"] = eff
            try:
                _write_json(path, data)
            except OSError as exc:
                log(f"写入灯效场景失败：{exc}")
                return None
        return eff

    def get_effect_scene(self, name: str) -> dict:
        name = self._canonical_scene(str(name or ""))
        data = self._scene_file_data(self._scene_path(name)) if name else None
        if not data or data.get("sceneType") != "effect":
            return {"ok": False, "error": "没有这个灯效场景"}
        eff = data.get("effect") if isinstance(data.get("effect"), dict) else {}
        items = [str(n) for n in eff.get("items") or [] if str(n)]
        index = max(0, min(len(items) - 1, int(eff.get("index") or 0))) if items else 0
        library = {it["name"]: it for it in self.list_effects()}
        active = self.effect_active() and self.active_scene == name
        return {"ok": True, "name": name, "sceneType": "effect",
                "description": str(eff.get("description") or ""),
                "items": [{"name": n, "missing": n not in library,
                           "description": (library.get(n) or {}).get("description", ""),
                           "thumb": (library.get(n) or {}).get("thumb", 0)} for n in items],
                "index": index, "active": active,
                "running": getattr(self, "_effect_lib_running", "") if active else ""}

    def create_effect_scene(self, name: str = "") -> dict:
        name = self._canonical_scene(str(name or "").strip()[:40])
        if not name:
            i = 1
            while os.path.isfile(self._scene_path(f"灯效 {i}")):
                i += 1
            name = f"灯效 {i}"
        if name == self.custom_scene_name or os.path.isfile(self._scene_path(name)):
            return {"ok": False, "error": "已有同名场景", "kind": "effect"}
        library = self._effect_names()
        first = DEFAULT_EFFECT_NAME if DEFAULT_EFFECT_NAME in library else (library[0] if library else "")
        acts, gs = self._layer_defaults("effect")
        data = {"sceneType": "effect",
                "effect": {"items": [first] if first else [], "index": 0, "description": ""},
                "pages": [{"name": name, "images": {}, "keyLayer": 1,
                           "actions": {str(k): v for k, v in acts.items()},
                           "gestures": {str(k): v for k, v in gs.items()}}], "page": 0}
        try:
            _write_json(self._scene_path(name), data)
        except OSError as exc:
            return {"ok": False, "error": f"新建失败：{exc}", "kind": "effect"}
        log(f"已新建灯效场景：{name}")
        return {"ok": True, "name": name, "kind": "effect", "sceneType": "effect"}

    def effect_scene_edit(self, scene: str, op: str, **kw) -> dict:
        """页面编辑区：add(name, index) / remove(index) / move(from, to) / goto(index)。"""
        scene = self._canonical_scene(str(scene or ""))
        if not self.is_effect_scene(scene):
            return {"ok": False, "error": "没有这个灯效场景"}
        if op == "add":
            name = self._effect_clean_name(kw.get("name"))
            if not self._effect_read(name):
                return {"ok": False, "error": f"灯效库里没有「{name}」"}

            def fn(eff):
                at = kw.get("index")
                at = len(eff["items"]) if not isinstance(at, int) else max(0, min(len(eff["items"]), at))
                eff["items"].insert(at, name)
                eff["index"] = at
        elif op == "remove":
            def fn(eff):
                i = int(kw.get("index", -1))
                if 0 <= i < len(eff["items"]):
                    eff["items"].pop(i)
                    if eff["index"] > i or eff["index"] >= len(eff["items"]):
                        eff["index"] = max(0, eff["index"] - 1)
        elif op == "move":
            def fn(eff):
                a, b = int(kw.get("from", -1)), int(kw.get("to", -1))
                items = eff["items"]
                if 0 <= a < len(items) and 0 <= b < len(items) and a != b:
                    cur = eff["index"]
                    items.insert(b, items.pop(a))
                    # 当前页跟着它自己走（同名灯效可以出现多次，所以按位置算）
                    if cur == a:
                        eff["index"] = b
                    elif a < cur <= b:
                        eff["index"] = cur - 1
                    elif b <= cur < a:
                        eff["index"] = cur + 1
        elif op == "goto":
            def fn(eff):
                eff["index"] = int(kw.get("index", 0))
        else:
            return {"ok": False, "error": "未知操作"}
        eff = self._effect_scene_update(scene, fn)
        if eff is None:
            return {"ok": False, "error": "写入失败"}
        if self.effect_active() and self.active_scene == scene:
            # 正在运行这个场景：当前页换了灯效（加入/删除/点选）才重启运行时；单纯排序不打断
            want = eff["items"][eff["index"]] if eff["items"] else ""
            if op == "goto" or want != getattr(self, "_effect_lib_running", None):
                self._effect_play_page(scene)
        self.scene_rev = getattr(self, "scene_rev", 0) + 1
        self.broadcast({"type": "effectscene", "scene": scene})
        return {"ok": True, **self.get_effect_scene(scene)}

    def effect_page_step(self, delta: int) -> bool:
        """◀ ▶ 长按：上一 / 下一灯效页（循环）。"""
        scene = self.active_scene
        if not (self.effect_active() and self.is_effect_scene(scene)):
            return False
        items = self._effect_scene_items(scene)
        if len(items) < 2:
            return False
        eff = self._effect_scene_update(scene, lambda e: e.update(index=(e["index"] + delta) % len(e["items"])))
        if eff is None:
            return False
        ok = self._effect_play_page(scene)
        self.broadcast({"type": "effectscene", "scene": scene})
        log(f"灯效页 {eff['index'] + 1}/{len(items)}：{eff['items'][eff['index']]}")
        return ok

    def _run_effect_code(self, label: str, code: str) -> bool:
        ok = bool(self._start_effect_scene(label, code))
        if ok:
            self._effect_capture_thumb(label, getattr(self, "_effect_generation", -1))
        return ok

    def _effect_play_page(self, scene: str) -> bool:
        """运行灯效场景的当前页；空场景放黑屏占位，按键层照常可用。"""
        info = self.get_effect_scene(scene)
        if not info["ok"]:
            return False
        items = [it for it in info["items"] if not it["missing"]]
        current = info["items"][info["index"]] if info["items"] else None
        if current is not None and current["missing"] and items:
            current = items[0]
        effect = self.get_effect(current["name"]) if current and not current["missing"] else {"ok": False}
        if effect["ok"]:
            self._effect_lib_running = effect["name"]
            return self._run_effect_code(effect["name"], effect["code"])
        self._effect_lib_running = ""
        return bool(self._start_effect_scene(scene, BLANK_EFFECT))

    def _apply_effect_scene(self, name: str) -> bool:
        if not self.is_effect_scene(name):
            return False
        self._enter_effect_mode()
        self._effect_trial = False
        self.active_scene = name
        self._layer_reload()
        self._save_config()
        return self._effect_play_page(name)

    def _enter_effect_mode(self) -> None:
        old = self.active_scene or self.custom_scene_name
        if old == self.custom_scene_name:
            self._snapshot_custom_scene()
        if not getattr(self, "effect_active", lambda: False)():
            self._effect_return = self._album_return if self.album_active() else old
        self._cancel_album_push()
        self.preview_mode = "effect"
        self.in_standby = False
        self.album_scene_name = ""
        self._album_scene = {}
        self._album_return = None
        self.key_previews.clear()
        self._last_activity = time.time()

    def run_effect(self, name: str) -> dict:
        """编辑区「试运行」：当前是灯效场景就临时放这一段（不改页列表），否则单独试运行。"""
        effect = self.get_effect(name)
        if not effect["ok"]:
            return effect
        if not (self.effect_active() and self.is_effect_scene(self.active_scene)):
            self._enter_effect_mode()
            self._effect_trial = True
        self._layer_reload()
        self._effect_lib_running = effect["name"]
        ok = self._run_effect_code(effect["name"], effect["code"])
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": ok, "quiet": True})
        return {"ok": ok, "name": effect["name"], "trial": bool(getattr(self, "_effect_trial", False))}

    def stop_effect_scene(self) -> dict:
        if not getattr(self, "effect_active", lambda: False)():
            return {"ok": True, "active": False}
        trial = bool(getattr(self, "_effect_trial", False))
        target = getattr(self, "_effect_return", None) or self.custom_scene_name
        self._stop_effect_runtime()
        self._effect_return = None
        self._effect_trial = False
        self._effect_lib_running = ""
        self._layer_reload()
        self.preview_mode = "wallpaper"
        self.in_standby = False
        if trial and not self.is_effect_scene(self.active_scene):
            target = self.active_scene or self.custom_scene_name   # 试运行结束：回到原来的场景
        elif target == self.active_scene or self.is_effect_scene(target):
            target = self.custom_scene_name
        # 即使底层普通场景是空的也应恢复可编辑画面，不留下停住的灯效。
        ok = self.apply_scene(target)
        if not ok:
            self.active_scene = self.custom_scene_name
            self._save_config()
            self.apply_keys()
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True})
        return {"ok": True, "active": False, "scene": self.current_scene()}

    def _effect_runtime_failed(self, reason: str) -> None:
        """错误/超时只恢复原场景，不让启动/重连重新执行同一段坏代码。"""
        trial = bool(getattr(self, "_effect_trial", False))
        target = getattr(self, "_effect_return", None) or self.custom_scene_name
        self._effect_return = None
        self._effect_trial = False
        self._effect_lib_running = ""
        self._layer_reload()
        self.preview_mode = "wallpaper"
        self.in_standby = False
        if trial and not self.is_effect_scene(self.active_scene):
            target = self.active_scene or self.custom_scene_name
        if self.is_effect_scene(target):
            target = self.custom_scene_name
        self.active_scene = target
        if not self.apply_scene(target):
            self.active_scene = self.custom_scene_name
            self._save_config()
            self.apply_keys()
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True, "quiet": True})
        self.broadcast({"type": "notice", "text": "灯效代码出错或无响应，已退出并恢复原场景，请查看灯效日志"})

    def _restore_display(self) -> bool:
        """启动/重连/唤醒遵循当前场景类型，不把特殊场景盖回普通按键。"""
        if self.is_effect_scene(self.active_scene):
            if getattr(self, "effect_active", lambda: False)():
                self.effect_repaint()  # 息屏后同session也需清空差分缓存重新提交
                return True
            return self._apply_effect_scene(self.active_scene)
        if getattr(self, "effect_active", lambda: False)() and getattr(self, "_effect_trial", False):
            self.effect_repaint()      # 试运行中：继续显示灯效
            return True
        if self.is_album_scene(self.active_scene):
            return self._play_album_scene(self.active_scene)
        if self.album_active():
            return self._push_album()
        return self.apply_keys()

    # ===================================================================== #
    # 技术文档：放到数据目录里一个固定文件夹，方便直接拖进其他智能体
    # ===================================================================== #
    GUIDE_DIR_NAME = "灯效开发文档"
    GUIDE_FILE_NAME = "M18FX 灯效技术文档.md"

    def export_effect_guide(self) -> str:
        """把最新的技术文档 + 打击垫示例写到 数据目录/灯效开发文档/，返回文档路径（每次覆盖成当前版本）。"""
        folder = os.path.join(DATA_DIR, self.GUIDE_DIR_NAME)
        os.makedirs(folder, exist_ok=True)
        head = ("> 给智能体：这是 Stream Dock M18 控制台的灯效开发文档。请按文档写一段 M18FX JavaScript，"
                "只返回可直接粘贴的代码（不要 HTML / Node.js / Python，不联网、不读写文件）。"
                "同目录的「示例-打击垫.js」是可运行的完整示例。\n\n")
        guide = self.effect_guide()
        path = os.path.join(folder, self.GUIDE_FILE_NAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + guide)
        with open(os.path.join(folder, "示例-打击垫.js"), "w", encoding="utf-8") as fh:
            fh.write(self.default_effect_code())
        return path

    def reveal_effect_guide(self) -> dict:
        try:
            path = self.export_effect_guide()
        except OSError as exc:
            return {"ok": False, "error": f"写出技术文档失败：{exc}"}
        ok = self.reveal_in_explorer(path)
        return {"ok": ok, "path": path, "folder": os.path.dirname(path)}

    # ===================================================================== #
    # 导出 / 导入 / 迁移
    # ===================================================================== #
    def _effect_export(self, effect: dict) -> dict:
        """导出场景包：把引用到的灯效代码一起带上。"""
        items = [str(n) for n in (effect or {}).get("items") or [] if str(n)]
        library = {}
        for n in items:
            got = self.get_effect(n)
            if got["ok"]:
                library[n] = {"code": got["code"], "description": got["description"]}
        return {"items": items, "index": int((effect or {}).get("index") or 0),
                "description": str((effect or {}).get("description") or "")[:300], "library": library}

    def _effect_import(self, effect) -> dict | None:
        """导入场景包：旧格式（一段 code）或新格式（items + library）都写进灯效库。"""
        if not isinstance(effect, dict):
            return None
        desc = str(effect.get("description") or "")[:300]
        if isinstance(effect.get("code"), str):
            code = effect["code"]
            if not code.strip() or len(code.encode("utf-8")) > self.EFFECT_CODE_LIMIT:
                return None
            name = self._effect_unique("导入的灯效", code)
            self.save_effect(name, code, desc, name if self._effect_read(name) else "")
            return {"items": [name], "index": 0, "description": desc}
        library = effect.get("library") if isinstance(effect.get("library"), dict) else {}
        renamed, items = {}, []
        for n, one in library.items():
            if not isinstance(one, dict) or not isinstance(one.get("code"), str):
                continue
            code = one["code"]
            if not code.strip() or len(code.encode("utf-8")) > self.EFFECT_CODE_LIMIT:
                return None
            target = self._effect_unique(n, code)
            if not self._effect_read(target):
                self.save_effect(target, code, str(one.get("description") or ""))
            renamed[str(n)] = target
        for n in effect.get("items") or []:
            if str(n) in renamed:
                items.append(renamed[str(n)])
            elif self._effect_read(str(n)):
                items.append(str(n))
        return {"items": items, "index": int(effect.get("index") or 0), "description": desc}

    def _migrate_effect_library(self) -> int:
        """旧数据：一个灯效场景 = 一段代码。拆成「灯效库条目 + 一页的灯效场景」，代码不丢。"""
        fresh = not os.path.isdir(EFFECT_LIB_DIR)
        moved = 0
        if os.path.isdir(SCENE_DIR):
            for fn in os.listdir(SCENE_DIR):
                if not fn.endswith(".json") or fn.startswith((".", "__")):
                    continue
                path = os.path.join(SCENE_DIR, fn)
                data = self._scene_file_data(path)
                if not data or data.get("sceneType") != "effect":
                    continue
                eff = data.get("effect") if isinstance(data.get("effect"), dict) else {}
                if isinstance(eff.get("library"), dict):
                    # 随包预设 / 拷来的场景文件：灯效代码内嵌在 library 里，写进灯效库后只留页顺序
                    renamed = {}
                    for lib_name, one in eff["library"].items():
                        if not isinstance(one, dict) or not isinstance(one.get("code"), str) or not one["code"].strip():
                            continue
                        target = self._effect_unique(lib_name, one["code"])
                        if not self._effect_read(target):
                            _write_json(self._effect_file(target), {
                                "name": target, "description": str(one.get("description") or "")[:300],
                                "code": one["code"]})
                        renamed[str(lib_name)] = target
                    items = [renamed.get(str(n), str(n)) for n in eff.get("items") or [] if str(n)]
                    data["effect"] = {"items": items, "index": int(eff.get("index") or 0),
                                      "description": str(eff.get("description") or "")[:300]}
                    try:
                        _write_json(path, data)
                        moved += 1
                    except OSError as exc:
                        log(f"导入灯效场景「{fn[:-5]}」的灯效失败：{exc}")
                    continue
                code = eff.get("code")
                if not isinstance(code, str):
                    continue
                scene = self._canonical_scene(fn[:-5])
                desc = str(eff.get("description") or "")[:300]
                items = []
                if code.strip():
                    lib = self._effect_unique(scene, code)
                    if not self._effect_read(lib):
                        _write_json(self._effect_file(lib), {"name": lib, "description": desc, "code": code})
                    items = [lib]
                data["effect"] = {"items": items, "index": 0, "description": desc}
                try:
                    _write_json(path, data)
                    moved += 1
                except OSError as exc:
                    log(f"迁移灯效场景「{scene}」失败：{exc}")
        if fresh and not self._effect_names():
            # 第一次建灯效库：放进随包的打击垫示例（用户以后删掉不会再补回）
            _write_json(self._effect_file(DEFAULT_EFFECT_NAME),
                        {"name": DEFAULT_EFFECT_NAME, "description": "科幻音频打击垫：星空、音频波形和马赛克扩散",
                         "code": self.default_effect_code()})
        os.makedirs(EFFECT_LIB_DIR, exist_ok=True)
        if moved:
            log(f"已把 {moved} 个旧灯效场景拆成灯效库 + 灯效页")
        return moved

    def _upgrade_effect_templates(self) -> int:
        """只升级原封不动的旧打击垫示例，任何手工编辑的代码都不覆盖。"""
        self._migrate_effect_library()
        code = self.default_effect_code()
        if "M18FX.on" not in code:
            return 0
        changed = 0
        for name in self._effect_names():
            data = self._effect_read(name)
            if not data or _code_hash(data["code"]) not in OLD_TEMPLATE_HASHES:
                continue
            try:
                _write_json(self._effect_file(name), {**data, "code": code})
                changed += 1
            except OSError as exc:
                log(f"旧打击垫示例升级失败，保留原代码：{exc}")
        if changed:
            log(f"已升级 {changed} 个未编辑的打击垫示例：星空闪烁背景、三排音频波形（振幅与密度收敛）")
        return changed
