"""App 的一部分：官方图标包 / 插件目录、删除进回收站、裁图工具。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import ctypes
import glob
import io
import json
import os
import shutil
import threading
import time
import uuid
from PIL import Image
import official

import core
from core import (  # noqa: F401
    CROP_CACHE,
    GRID_COLS,
    GRID_ROWS,
    ICON_DIR,
    KEY_COUNT,
    KEY_SIZE,
    MIRROR_DIR,
    MY_ICON_DIR,
    UPLOAD_DIR,
    asset_path,
    log,
)


class CatalogMixin:

    # -- 官方资源库 -------------------------------------------------------- #
    # -- 官方图标包 / 插件（直接读官方目录，不复制） ------------------------ #
    _catalog_lock = threading.Lock()

    def _catalog(self, refresh: bool = False) -> dict:
        with self._catalog_lock:          # 图标包和插件两个请求常同时到，只扫一遍
            return self._catalog_locked(refresh)

    def _catalog_locked(self, refresh: bool) -> dict:
        cat = getattr(self, "_catalog_cache", None)
        if cat is None or refresh:
            packs = {}
            for folder in official.icon_pack_dirs(core.SD_ROOT):
                info = official.icon_pack_info(folder)
                if info["files"] and info["id"] not in packs:
                    packs[info["id"]] = info
            for folder in sorted(glob.glob(os.path.join(MY_ICON_DIR, "*.sdIconPack"))):
                info = official.icon_pack_info(folder)      # 「我的图标」：裁图工具存的
                if info["files"] and info["id"] not in packs:
                    info["mine"] = True
                    packs[info["id"]] = info
            plugins = {p["id"]: p for p in official.plugin_list(core.SD_ROOT)}
            cat = self._catalog_cache = {"packs": packs, "plugins": plugins}
            log(f"官方资源：图标包 {len(packs)} 个（{sum(len(p['files']) for p in packs.values())} 个图标），"
                f"插件 {len(plugins)} 个")
        return cat

    def icon_packs(self, refresh: bool = False) -> list:
        def _rev(p: dict) -> int:
            """包目录的修改时间：同名重裁后让前端不会拿浏览器旧缓存显示。"""
            try:
                return int(os.path.getmtime(p.get("folder") or ""))
            except (OSError, TypeError):
                return 0

        packs = [{
            "id": p["id"], "name": p["name"], "author": p["author"],
            "count": len(p["files"]), "mine": bool(p.get("mine")),
            "crop": bool(p.get("mine")), "rev": _rev(p),
            "files": [os.path.basename(f) for f in p["files"]],
        } for p in self._catalog(refresh)["packs"].values()]
        packs.sort(key=lambda p: (0 if p["mine"] else 1, p["name"].lower()))
        names = [p["name"] for p in packs]
        for p in packs:            # 同名图标包（官方有两个 "Icons for Designer"）加上来源后缀
            if names.count(p["name"]) > 1:
                p["name"] += f" · {p['id'].rsplit('.', 1)[-1]}"
        local = self.list_icons()
        if local:
            packs.append({"id": "local", "name": "本地图标", "author": "", "count": len(local),
                          "files": local})
        return packs

    def icon_pack_file(self, pack_id: str, fname: str) -> str | None:
        fname = os.path.basename(fname or "")
        if pack_id == "local":
            path = os.path.join(ICON_DIR, fname)
            return path if fname and os.path.isfile(path) else None
        pack = self._catalog()["packs"].get(pack_id)
        if not pack:
            return None
        for f in pack["files"]:
            if os.path.basename(f) == fname:
                return f
        return None

    def plugin_catalog(self, refresh: bool = False) -> list:
        out = []
        for p in self._catalog(refresh)["plugins"].values():
            out.append({
                "id": p["id"], "name": p["name"], "author": p["author"],
                "version": p["version"], "description": p["description"],
                "downloaded": p["downloaded"], "hasIcon": bool(p["icon"]), "custom": p.get("custom", False),
                "builtin": p.get("builtin", False), "hotkeyPack": p.get("hotkeyPack", False),
                "mirrored": os.path.normpath(p["folder"]).startswith(os.path.normpath(MIRROR_DIR)),
                "actions": [{"uuid": a.get("uuid", ""), "name": a["name"], "tooltip": a["tooltip"],
                             "hasIcon": bool(a["icon"]),
                             "supported": a.get("supported", True)}
                            for a in p["actions"]],
            })
        return out

    def plugin_icon_path(self, plugin_id: str, idx: int) -> str | None:
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return None
        if idx < 0:
            return p["icon"]
        acts = p["actions"]
        return acts[idx]["icon"] if 0 <= idx < len(acts) else None

    # -- 删除图标 / 插件（进回收站，可恢复） ---------------------------------- #
    @staticmethod
    def _recycle(path: str) -> bool:
        """把文件或目录移进 Windows 回收站；成功返回 True。"""
        path = os.path.abspath(path or "")
        if not path or not os.path.exists(path):
            return False

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p),
                        ("wFunc", ctypes.c_uint),
                        ("pFrom", ctypes.c_wchar_p),
                        ("pTo", ctypes.c_wchar_p),
                        ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", ctypes.c_int),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", ctypes.c_wchar_p)]

        op = SHFILEOPSTRUCTW()
        op.wFunc = 3                                    # FO_DELETE
        op.pFrom = path + "\0"                          # 文件名列表以双 0 结尾
        op.fFlags = 0x40 | 0x10 | 0x4 | 0x400           # 允许撤销 / 不确认 / 静默 / 不弹错误框
        try:
            shell32 = ctypes.windll.shell32
            shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
            shell32.SHFileOperationW.restype = ctypes.c_int
            rc = shell32.SHFileOperationW(ctypes.byref(op))
        except Exception as exc:  # noqa: BLE001
            log(f"调回收站失败：{exc}")
            return False
        return rc == 0 and not op.fAnyOperationsAborted

    def delete_icon(self, pack_id: str, fname: str) -> tuple[bool, str]:
        """删掉图标包里的一个图标（pack_id = local 时删本地图标）。"""
        path = self.icon_pack_file(pack_id, fname)
        if not path:
            return False, "找不到这个图标"
        if not self._recycle(path):
            return False, f"删除失败（可能被占用）：{os.path.basename(path)}"
        self._catalog_cache = None
        log(f"已删除图标：{path}")
        return True, "已删除这个图标"

    def delete_icon_pack(self, pack_id: str) -> tuple[bool, str]:
        if pack_id == "local":
            return False, "本地图标是单个文件，请在图标上右键逐个删除"
        pack = self._catalog()["packs"].get(pack_id)
        if not pack:
            return False, "找不到这个图标包"
        folder = pack["folder"]
        if not self._recycle(folder):
            return False, f"删除失败（可能被占用）：{folder}"
        self._catalog_cache = None
        log(f"已删除图标包：{folder}")
        return True, f"已删除图标包「{pack['name']}」"

    def reveal_icon_pack(self, pack_id: str, fname: str = "") -> bool:
        """在资源管理器里打开图标包；给了文件名就选中那个图标。"""
        target = self.icon_pack_file(pack_id, fname) if fname else ""
        if not target:
            if pack_id == "local":
                target = ICON_DIR if os.path.isdir(ICON_DIR) else os.path.dirname(ICON_DIR)
            else:
                pack = self._catalog()["packs"].get(pack_id)
                if not pack:
                    return False
                target = pack["folder"]
        return self.reveal_in_explorer(target)

    # -- 裁图工具：一张大图裁成 15 张，正好对应 15 个屏幕键 ---------------- #
    def _crop_dir(self, token: str) -> str:
        return os.path.join(CROP_CACHE, "".join(c for c in (token or "") if c.isalnum())[:32])

    def _crop_canvas(self, src: str, fit: str, gap: int) -> tuple[list, list[int], bool]:
        """返回 (15 张图块（按 1..15 排列）、每帧时长、是否动图)。"""
        fit = fit if fit in ("cover", "contain", "fill") else "cover"
        g = max(0, min(64, int(gap)))
        w = GRID_COLS * KEY_SIZE + (GRID_COLS - 1) * g
        h = GRID_ROWS * KEY_SIZE + (GRID_ROWS - 1) * g
        tiles: list = [[] for _ in range(KEY_COUNT)]
        durations: list[int] = []
        with Image.open(src) as im:
            try:
                frames_total = int(getattr(im, "n_frames", 1) or 1)
            except Exception:  # noqa: BLE001
                frames_total = 1
            animated = frames_total > 1
            for i in range(frames_total if animated else 1):
                if animated:
                    im.seek(i)
                    durations.append(int(im.info.get("duration") or 100))
                frame = im.convert("RGBA")
                canvas = self._fit_canvas(frame, w, h, zoom_offset=False, fit=fit)
                for key in range(1, KEY_COUNT + 1):
                    col = (key - 1) % GRID_COLS
                    row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
                    box = (col * (KEY_SIZE + g), row_top * (KEY_SIZE + g),
                           col * (KEY_SIZE + g) + KEY_SIZE, row_top * (KEY_SIZE + g) + KEY_SIZE)
                    tiles[key - 1].append(canvas.crop(box).convert("RGB"))
        return tiles, durations, animated

    @staticmethod
    def _crop_visual_index(key: int) -> int:
        """键号 → 图标库里的文件名序号（按屏幕从上到下、从左到右数）。
        控制台第一行是 11–15、第二行 6–10、第三行 1–5，
        所以 11 号键存成 01，1 号键存成 11，图标库展开时才是屏幕上的样子。"""
        col = (key - 1) % GRID_COLS
        row_from_bottom = (key - 1) // GRID_COLS
        row_from_top = GRID_ROWS - 1 - row_from_bottom
        return row_from_top * GRID_COLS + col + 1

    def crop_preview(self, file: str, fit: str = "cover", gap: int = 27, name: str = "") -> dict:
        src = asset_path(file)
        if not src:
            return {"ok": False, "reason": "找不到这张图片，重新拖一次"}
        token = uuid.uuid4().hex
        out_dir = self._crop_dir(token)
        try:
            os.makedirs(out_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(out_dir, "src" + (os.path.splitext(src)[1].lower() or ".png")))
            tiles, durations, animated = self._crop_canvas(src, fit, gap)
            ext = ".gif" if animated else ".png"
            for idx, frames in enumerate(tiles, start=1):
                # 文件名按屏幕上看到的顺序补零：图标库按文件名排序时和 15 个键一一对应
                dst = os.path.join(out_dir, f"{self._crop_visual_index(idx):02d}{ext}")
                if animated:
                    frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:],
                                   duration=durations or 100, loop=0, optimize=False)
                else:
                    frames[0].save(dst, "PNG")
        except Exception as exc:  # noqa: BLE001
            log(f"裁图失败：{exc}")
            return {"ok": False, "reason": f"这张图片裁不了：{exc}"}
        shown = "".join(c for c in (name or "").strip() if c not in '\\/:*?"<>|').strip()
        name = (shown[:60] or os.path.splitext(os.path.basename(src))[0])
        return {"ok": True, "token": token, "name": name, "animated": animated,
                "frames": len(durations) if animated else 1,
                "fit": fit if fit in ("cover", "contain", "fill") else "cover",
                "gap": max(0, min(64, int(gap))),
                "src": f"/api/iconcrop/src?token={token}",
                "tiles": [f"/api/iconcrop/tile?token={token}&n={self._crop_visual_index(i):02d}{ext}"
                          for i in range(1, KEY_COUNT + 1)]}

    def crop_src(self, token: str) -> tuple[bytes, str] | None:
        folder = self._crop_dir(token)
        for path in sorted(glob.glob(os.path.join(folder, "src.*"))):
            ext = os.path.splitext(path)[1].lower()
            ctype = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".webp": "image/webp", ".bmp": "image/bmp"}.get(
                         ext, "application/octet-stream")
            try:
                with open(path, "rb") as fh:
                    return fh.read(), ctype
            except OSError:
                return None
        return None

    def crop_tile(self, token: str, name: str) -> tuple[bytes, str] | None:
        folder = self._crop_dir(token)
        safe = os.path.basename(name)
        if not safe or not folder:
            return None
        path = os.path.join(folder, safe)
        if not os.path.isfile(path):
            return None
        ctype = "image/gif" if safe.lower().endswith(".gif") else "image/png"
        try:
            with open(path, "rb") as fh:
                return fh.read(), ctype
        except OSError:
            return None

    def crop_save(self, token: str, name: str = "") -> dict:
        folder = self._crop_dir(token)
        files = sorted(glob.glob(os.path.join(folder, "*"))) if folder else []
        if not files:
            return {"ok": False, "reason": "预览已经过期，重新裁一次"}
        pack_name = "".join(c for c in (name or "").strip() if c not in '\\/:*?"<>|').strip() or "我的图标"
        pack_name = pack_name[:40]
        os.makedirs(MY_ICON_DIR, exist_ok=True)
        base, n = pack_name, 2
        while os.path.exists(os.path.join(MY_ICON_DIR, f"{base}.sdIconPack")):
            base = f"{pack_name} {n}"
            n += 1
        pack_dir = os.path.join(MY_ICON_DIR, f"{base}.sdIconPack")
        icons = os.path.join(pack_dir, "icons")
        try:
            os.makedirs(icons, exist_ok=True)
            for path in files:
                fname = os.path.basename(path)
                if fname.startswith("src."):
                    continue   # 原图只是给编辑区左侧预览用的，不进图标包
                shutil.copy2(path, os.path.join(icons, fname))
            with open(os.path.join(pack_dir, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"Name": base, "Author": "我", "Version": "1.0.0",
                           "Description": "裁图工具生成的 15 键图标"}, fh, ensure_ascii=False, indent=2)
            with open(os.path.join(pack_dir, "zh_CN.json"), "w", encoding="utf-8") as fh:
                json.dump({"Name": base}, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            log(f"保存图标包失败：{exc}")
            return {"ok": False, "reason": f"保存失败：{exc}"}
        shutil.rmtree(folder, ignore_errors=True)
        self._catalog_cache = None
        log(f"已保存图标包：{base}（15 个图标）")
        info = official.icon_pack_info(pack_dir)
        return {"ok": True, "pack": {"id": info["id"], "name": info["name"], "mine": True,
                                     "count": len(info["files"]),
                                     "files": [os.path.basename(f) for f in info["files"]]}}

    def crop_cancel(self, token: str) -> bool:
        shutil.rmtree(self._crop_dir(token), ignore_errors=True)
        return True

    def _drop_plugin_bindings(self, plugin_id: str) -> int:
        """把绑在已删插件上的按键清空，返回清掉的个数。"""
        removed = 0
        for page in self.pages:
            acts = page.get("actions") or {}
            for k in list(acts):
                spec = acts.get(k)
                if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("plugin") == plugin_id:
                    del acts[k]
                    removed += 1
            gest = page.get("gestures") or {}
            for k in list(gest):
                per = gest.get(k)
                if not isinstance(per, dict):
                    continue
                for g in list(per):
                    spec = per.get(g)
                    if isinstance(spec, dict) and spec.get("type") == "plugin" and spec.get("plugin") == plugin_id:
                        del per[g]
                        removed += 1
                if not per:
                    gest.pop(k, None)
        if removed:
            self._save_config()
        return removed

    def delete_plugin(self, plugin_id: str) -> tuple[bool, str]:
        """删除整个官方插件目录（进回收站）。"""
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return False, "找不到这个插件"
        folder, name = p["folder"], p["name"]
        if p.get("custom"):
            return False, "自制插件请到左侧「自制插件」里删除"
        if self.plugin_host:
            self.plugin_host.forget(plugin_id)
        if not self._recycle(folder):
            return False, f"删除失败（可能被插件占着）：{folder}"
        self._catalog_cache = None
        self._refresh_plugins()
        cleared = self._drop_plugin_bindings(plugin_id)
        if cleared:
            self.apply_keys()
        log(f"已删除插件：{name}（{folder}）" + (f"，清空 {cleared} 个按键绑定" if cleared else ""))
        return True, f"已删除插件「{name}」" + (f"，{cleared} 个按键已清空" if cleared else "")

    def delete_plugin_action(self, plugin_id: str, uuid: str) -> tuple[bool, str]:
        """从插件的 manifest.json 里删掉一个动作（原清单备份为 manifest.json.m18bak）。"""
        p = self._catalog()["plugins"].get(plugin_id)
        if not p:
            return False, "找不到这个插件"
        if p.get("builtin"):
            return False, "官方内置插件的动作是程序自带功能，删不掉"
        folder = p["folder"]
        mpath = os.path.join(folder, "manifest.json")
        try:
            with open(mpath, encoding="utf-8-sig") as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            return False, "读不到这个插件的 manifest.json"
        acts = [a for a in (man.get("Actions") or []) if isinstance(a, dict)]
        keep = [a for a in acts if str(a.get("UUID")) != uuid]
        if len(keep) == len(acts):
            return False, "找不到这个动作"
        gone = next((a for a in acts if str(a.get("UUID")) == uuid), {})
        aname = str(gone.get("Name") or uuid.rsplit(".", 1)[-1])
        try:
            shutil.copyfile(mpath, mpath + ".m18bak")
            man["Actions"] = keep
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(man, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            return False, f"写插件清单失败：{exc}"
        for lname in ("zh_CN.json", "zh-CN.json", "en.json", "en_US.json"):
            lpath = os.path.join(folder, lname)
            try:
                with open(lpath, encoding="utf-8-sig") as fh:
                    loc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(loc, dict) and uuid in loc:
                aname = str((loc.get(uuid) or {}).get("Name") or aname)
                loc.pop(uuid, None)
                try:
                    with open(lpath, "w", encoding="utf-8") as fh:
                        json.dump(loc, fh, ensure_ascii=False, indent=2)
                except OSError:
                    pass
        if self.plugin_host:
            self.plugin_host.reload(plugin_id)
        self._catalog_cache = None
        self._refresh_plugins()
        log(f"已删除插件动作：{plugin_id}/{uuid}（原清单备份 manifest.json.m18bak）")
        return True, f"已删除动作「{aname}」"

    def list_icons(self) -> list:
        try:
            files = sorted(os.listdir(ICON_DIR))
        except OSError:
            return []
        exts = (".png", ".gif", ".jpg", ".jpeg", ".webp")
        return [f for f in files if f.lower().endswith(exts)]

    def set_key_icon(self, key: int, name: str) -> bool:
        name = name or ""
        src = None
        if name.startswith("pack:"):
            pack_id, _, fname = name[5:].partition("/")
            src = self.icon_pack_file(pack_id, fname)
        elif name.startswith("lib:"):
            src = self.library_path(name[4:])
        elif name.startswith("plugin:"):
            plugin_id, _, idx = name[7:].rpartition("/")
            try:
                src = self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                src = None
        if name.startswith(("pack:", "plugin:", "lib:")):
            img = official.read_image(src) if src else None
            if not img or not (1 <= key <= KEY_COUNT):
                return False
            # 复制一份进 uploads：官方删了图标包，按键图也不会丢
            stored = self._store_scene_image(img, src)
            self._anim_pause_until = time.time() + 1.0
            return self.set_key_image(key, os.path.join(UPLOAD_DIR, stored))
        safe = os.path.basename(name)
        path = os.path.join(ICON_DIR, safe)
        if not safe or not os.path.isfile(path):
            return False
        if safe.lower().endswith(".gif"):
            return self.set_key_image(key, path)
        try:
            icon = Image.open(path).convert("RGBA")
        except Exception:  # noqa: BLE001
            return False
        bg = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 255))
        bg.alpha_composite(icon)
        buf = io.BytesIO()
        bg.convert("RGB").save(buf, "JPEG", quality=94)
        self._anim_pause_until = time.time() + 1.0
        self.key_overrides[key] = {"type": "image", "file": path, "jpeg": buf.getvalue()}
        self._save_config()
        if self.device.connected:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True
