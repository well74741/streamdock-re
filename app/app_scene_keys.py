"""相册 / 灯效场景自己的一层按键动作（1–18 键），存在场景文件 pages[0] 里。

以前相册场景切进来时把三个导航键的动作临时写进普通场景的按键字典、退出再还原；
扩到 15 个屏幕键后这种做法容易把相册的动作存进原场景。现在改成独立的一层：
相册 / 灯效场景运行时，输入、编辑区、MCP 读写的都是这一层，底层普通场景的按键原样不动。
"""
from __future__ import annotations

import json
import os
import tempfile

from core import log

NEXT_SCENE = {"type": "nav", "name": "下一场景"}
LAYER_KEYS = range(1, 19)


class SceneKeysMixin:
    ALBUM_NAV = {16: "上一页", 17: "主页", 18: "下一页"}

    # -- 默认动作 ------------------------------------------------------------ #
    @classmethod
    def _layer_defaults(cls, kind: str) -> tuple[dict, dict]:
        """(短按动作, 手势) 的默认值；只在场景第一次建立按键层时写入，用户清掉的不会再补回。"""
        if kind == "album":
            # 相册：屏幕键单击切下一个场景；◀ ⌂ ▶ 是上一张 / 第一张 / 下一张
            actions = {k: dict(NEXT_SCENE) for k in range(1, 16)}
            actions.update({k: {"type": "nav", "name": n} for k, n in cls.ALBUM_NAV.items()})
            return actions, {}
        # 灯效：短按、双击都留空（交给灯效互动），长按切下一个场景；◀ ▶ 长按切换灯效页
        gestures = {k: {"long": dict(NEXT_SCENE)} for k in range(1, 16)}
        gestures[16] = {"long": {"type": "nav", "name": "上一灯效页"}}
        gestures[18] = {"long": {"type": "nav", "name": "下一灯效页"}}
        return {}, gestures

    # -- 读写场景文件 -------------------------------------------------------- #
    def _layer_kind(self, data) -> str:
        kind = data.get("sceneType") if isinstance(data, dict) else ""
        return kind if kind in ("album", "effect") else ""

    def _layer_read(self, name: str, *, ensure: bool = True) -> dict | None:
        """读场景文件里的按键层；第一次读时补上默认动作并写回（带 keyLayer 标记）。"""
        path = self._scene_path(name)
        data = self._scene_file_data(path)
        kind = self._layer_kind(data)
        if not kind:
            return None
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            pages = [{"name": name, "images": {}, "actions": {}}]
            data["pages"], data["page"] = pages, 0
        page = pages[0]
        acts = page.get("actions") if isinstance(page.get("actions"), dict) else {}
        gs = page.get("gestures") if isinstance(page.get("gestures"), dict) else {}
        if ensure and not page.get("keyLayer"):
            d_acts, d_gs = self._layer_defaults(kind)
            for k, spec in d_acts.items():
                acts.setdefault(str(k), spec)
            for k, one in d_gs.items():
                cur = gs.get(str(k)) if isinstance(gs.get(str(k)), dict) else {}
                for g, spec in one.items():
                    cur.setdefault(g, spec)
                gs[str(k)] = cur
            page["actions"], page["gestures"], page["keyLayer"] = acts, gs, 1
            self._layer_save_file(path, data)
        out_a, out_g = {}, {}
        for k, spec in acts.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            if key in LAYER_KEYS and isinstance(spec, dict) and spec.get("type") != "folder":
                out_a[key] = spec
        for k, one in gs.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            if key in LAYER_KEYS and isinstance(one, dict):
                one = {g: v for g, v in one.items() if g in ("long", "double") and isinstance(v, dict)}
                if one:
                    out_g[key] = one
        return {"scene": name, "kind": kind, "actions": out_a, "gestures": out_g}

    def _layer_save_file(self, path: str, data: dict) -> bool:
        folder = os.path.dirname(path)
        try:
            fd, tmp = tempfile.mkstemp(prefix=".keys-", suffix=".tmp", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log(f"写入场景按键失败：{exc}")
            return False

    def _layer_write(self, name: str, key: int, spec: dict | None, gesture: str = "") -> bool:
        """把一个键的动作 / 手势写回相册或灯效场景文件（短按进 actions，长按 / 双击进 gestures）。"""
        path = self._scene_path(name)
        data = self._scene_file_data(path)
        if not self._layer_kind(data):
            return False
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            pages = [{"name": name, "images": {}, "actions": {}}]
            data["pages"], data["page"] = pages, 0
        page = pages[0]
        page["keyLayer"] = 1
        if gesture in ("long", "double"):
            gs = page.get("gestures") if isinstance(page.get("gestures"), dict) else {}
            one = gs.get(str(key)) if isinstance(gs.get(str(key)), dict) else {}
            if spec:
                one[gesture] = spec
            else:
                one.pop(gesture, None)
            if one:
                gs[str(key)] = one
            else:
                gs.pop(str(key), None)
            page["gestures"] = gs
        else:
            acts = page.get("actions") if isinstance(page.get("actions"), dict) else {}
            if spec:
                acts[str(key)] = spec
            else:
                acts.pop(str(key), None)
            page["actions"] = acts
        return self._layer_save_file(path, data)

    # -- 运行时 -------------------------------------------------------------- #
    def _layer_scene_name(self) -> str:
        """当前生效的相册 / 灯效场景名；普通场景、待机幻灯片返回空串。"""
        if getattr(self, "effect_active", lambda: False)() and self.is_effect_scene(self.active_scene):
            return self.active_scene
        if self.album_active() and self.album_scene_name:
            return self.album_scene_name
        return ""

    def _layer(self) -> dict | None:
        name = self._layer_scene_name()
        if not name:
            return None
        cached = getattr(self, "_key_layer", None)
        if not cached or cached.get("scene") != name:
            cached = self._layer_read(name)
            self._key_layer = cached
        return cached

    def _layer_reload(self) -> None:
        self._key_layer = None

    def _key_action(self, key: int):
        layer = self._layer()
        return layer["actions"].get(key) if layer else self.key_actions.get(key)

    def _key_gesture(self, key: int) -> dict:
        layer = self._layer()
        return (layer["gestures"].get(key) if layer else self.key_gestures.get(key)) or {}

    def _layer_input(self, key: int, state: int) -> None:
        """相册 / 灯效场景里按键执行这一层的动作（和普通场景同一套短按 / 长按 / 双击判定）。"""
        act = self._key_action(key)
        g = self._key_gesture(key)
        if g.get("long") or g.get("double"):
            self._gesture(key, state, g)
            return
        if isinstance(act, dict) and act.get("type") == "plugin":
            self._plugin_key(key, act, state)
            return
        if state == 1 and act:
            import threading
            threading.Thread(target=self._run_action, args=(key,), daemon=True).start()

    # -- 编辑（被 set_key_action / clear_key_action / clear_key_gesture / reset_nav_key 调用） -- #
    def _layer_set(self, key: int, spec: dict | None, gesture: str = "") -> bool:
        layer = self._layer()
        if not layer or key not in LAYER_KEYS:
            return False
        if spec and spec.get("type") == "folder":
            return False                    # 相册 / 灯效场景没有子页，不能放文件夹
        if not self._layer_write(layer["scene"], key, spec, gesture):
            return False
        if gesture in ("long", "double"):
            one = dict(layer["gestures"].get(key) or {})
            if spec:
                one[gesture] = spec
            else:
                one.pop(gesture, None)
            if one:
                layer["gestures"][key] = one
            else:
                layer["gestures"].pop(key, None)
        elif spec:
            layer["actions"][key] = spec
        else:
            layer["actions"].pop(key, None)
        self.scene_rev = getattr(self, "scene_rev", 0) + 1
        return True

    def _layer_snapshot(self) -> dict | None:
        """给界面快照用：相册 / 灯效场景时按键动作取这一层。"""
        layer = self._layer()
        if not layer:
            return None
        return {"actions": dict(layer["actions"]), "gestures": dict(layer["gestures"]),
                "scene": layer["scene"], "kind": layer["kind"]}
