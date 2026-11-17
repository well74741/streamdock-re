"""App 的一部分：多页面、文件夹、灯珠空间映射。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import glob
import json
import os
import shutil
import time
from PIL import Image, ImageDraw

import core
from core import (  # noqa: F401
    ALBUM_TRANSITIONS,
    AUTOBLANK_MAX,
    AUTOBLANK_MIN,
    CONFIG_BAK_DIR,
    CONFIG_PATH,
    DEFAULT_LIGHTING,
    HERE,
    ICON_DIR,
    KEY_COUNT,
    KEY_GAP,
    LED_COUNT,
    LEGACY_ALBUM_NAMES,
    MODES,
    STANDBY_MAX,
    STANDBY_MIN,
    UPLOAD_DIR,
    load_path,
    log,
    store_path,
)


class PagesMixin:

    # -- 页面 -------------------------------------------------------------- #
    # -- 文件夹 ------------------------------------------------------------ #
    # 文件夹 = 一个不参与翻页的子页面（page["folder"] = True），按键动作 {"type":"folder","page":子页 id}。
    # 子页 11 号键（左上角）默认是「返回上级」；页面用稳定 id 关联，增删 / 调整页面不会串。
    FOLDER_BACK_KEY = 11

    @staticmethod
    def _new_page_id() -> str:
        return os.urandom(5).hex()

    def _ensure_page_ids(self) -> None:
        seen = set()
        for p in self.pages:
            if not p.get("id") or p["id"] in seen:
                p["id"] = self._new_page_id()
            seen.add(p["id"])

    def _page_index(self, pid: str) -> int:
        for i, p in enumerate(self.pages):
            if p.get("id") == pid:
                return i
        return -1

    def _main_pages(self) -> list[int]:
        """参与翻页的页面（不含文件夹子页）。"""
        return [i for i, p in enumerate(self.pages) if not p.get("folder")]

    def _folder_owner(self, pid: str):
        """文件夹子页 → (所在页序号, 按键)。"""
        for i, p in enumerate(self.pages):
            for k, spec in (p.get("actions") or {}).items():
                if isinstance(spec, dict) and spec.get("type") == "folder" and spec.get("page") == pid:
                    return i, k
        return None

    def _folder_path(self, index: int | None = None) -> list[dict]:
        """当前页的层级路径（面包屑）：[{name, index}, …]，最后一个是当前页。"""
        index = self.page if index is None else index
        chain, guard = [], 0
        while 0 <= index < len(self.pages) and guard < 20:
            p = self.pages[index]
            chain.append({"name": p.get("name", ""), "index": index, "folder": bool(p.get("folder"))})
            if not p.get("folder"):
                break
            owner = self._folder_owner(p.get("id"))
            if not owner:
                break
            index = owner[0]
            guard += 1
        return chain[::-1]

    def create_folder(self, key: int, name: str = "", gesture: str = "") -> dict:
        """把当前页的某个键设成文件夹：新建子页（11 号键 = 返回），按键标题默认是文件夹名。"""
        if not (1 <= key <= KEY_COUNT):
            return {"ok": False, "error": "只有屏幕键可以做文件夹"}
        name = (name or "").strip()[:20] or "文件夹"
        self._ensure_page_ids()
        parent = self.pages[self.page]
        sub = {"id": self._new_page_id(), "name": name, "folder": True, "images": {}, "styles": {},
               "gestures": {},
               "actions": {self.FOLDER_BACK_KEY: {"type": "nav", "name": "返回上级"},
                           16: {"type": "nav", "name": "返回上级"},
                           17: {"type": "nav", "name": "主页"},
                           18: {"type": "nav", "name": "返回上级"}}}
        back = self._load_image_source(self._glyph_icon("back"))
        if back:
            sub["images"][self.FOLDER_BACK_KEY] = back
        sub["styles"][self.FOLDER_BACK_KEY] = {"text": "返回", "size": 14, "pos": "bottom"}
        self.pages.append(sub)
        spec = {"type": "folder", "page": sub["id"], "name": name}
        if gesture in ("long", "double"):
            parent.setdefault("gestures", {}).setdefault(key, {})[gesture] = spec
        else:
            parent["actions"][key] = spec
            if key not in parent["images"]:            # 没有自己的图标：用文件夹图标 + 名字
                icon = self._load_image_source(self._glyph_icon("folder"))
                if icon:
                    parent["images"][key] = icon
                st = parent.setdefault("styles", {}).setdefault(key, {})
                if not (st.get("text") or "").strip():
                    st.update({"text": name[:12], "size": 14, "pos": "bottom"})
        self._save_config()
        if self.device.connected and not self.in_standby:
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        log(f"按键 {key} 已设为文件夹「{name}」")
        return {"ok": True, "page": sub["id"]}

    @staticmethod
    def _glyph_icon(kind: str) -> str:
        """文件夹 / 返回 图标（设备字体画不了 emoji，直接画成图片），生成一次放在上传目录。"""
        path = os.path.join(UPLOAD_DIR, f"m18_{kind}.png")
        if os.path.isfile(path):
            return path
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        im = Image.new("RGB", (144, 144), (22, 30, 44))
        d = ImageDraw.Draw(im)
        if kind == "folder":
            d.rounded_rectangle([26, 30, 72, 52], radius=7, fill=(214, 150, 40))
            d.rounded_rectangle([22, 42, 122, 102], radius=10, fill=(242, 184, 75))
            d.rounded_rectangle([22, 54, 122, 102], radius=10, fill=(250, 200, 95))
        else:
            d.line([(44, 66), (104, 66)], fill=(150, 200, 255), width=14)
            d.polygon([(26, 66), (60, 38), (60, 94)], fill=(150, 200, 255))
        im.save(path, "PNG")
        return path

    def open_folder(self, pid: str) -> bool:
        idx = self._page_index(pid)
        if idx < 0:
            return False
        return self.goto_page(idx) or idx == self.page

    def folder_back(self) -> bool:
        """返回上级：子页回到放文件夹的那一页；主页面上没有上级，不动。"""
        p = self.pages[self.page]
        if not p.get("folder"):
            return False
        owner = self._folder_owner(p.get("id"))
        target = owner[0] if owner else (self._main_pages() or [0])[0]
        return self.goto_page(target)

    def _drop_orphan_folders(self) -> int:
        """删掉没有任何按键指向的文件夹子页（连带子页里的子文件夹）。"""
        removed = 0
        while True:
            refs = set()
            for p in self.pages:
                specs = list((p.get("actions") or {}).values())
                for g in (p.get("gestures") or {}).values():
                    specs.extend((g or {}).values())
                refs.update(s.get("page") for s in specs if isinstance(s, dict) and s.get("type") == "folder")
            orphans = [i for i, p in enumerate(self.pages) if p.get("folder") and p.get("id") not in refs]
            if not orphans:
                return removed
            cur_id = self.pages[self.page].get("id")
            for i in reversed(orphans):
                self.pages.pop(i)
                removed += 1
            idx = self._page_index(cur_id)
            self.page = idx if idx >= 0 else (self._main_pages() or [0])[0]

    def _bind_page(self) -> None:
        self.key_previews = {}
        self._ensure_page_ids()
        page = self.pages[self.page]
        self.key_overrides = page["images"]
        self.key_actions = page["actions"]
        self.key_styles = page.setdefault("styles", {})
        self.key_gestures = page.setdefault("gestures", {})
        self._plugin_sync()

    def goto_page(self, index: int) -> bool:
        if not self.pages:
            return False
        index = max(0, min(len(self.pages) - 1, index))
        if index == self.page:
            return False
        self.page = index
        self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        p = self.pages[self.page]
        log(f"进入文件夹「{p.get('name')}」" if p.get("folder") else f"切换到第 {self.page + 1} 页")
        return True

    def add_page(self) -> int:
        self.pages.append({
            "id": self._new_page_id(),
            "name": f"第 {len(self._main_pages()) + 1} 页",
            "images": {}, "actions": {
                16: {"type": "nav", "name": "上一页"},
                17: {"type": "nav", "name": "主页"},
                18: {"type": "nav", "name": "下一页"},
            }, "styles": {},
        })
        self._save_config()
        return len(self.pages) - 1

    def delete_page(self, index: int) -> bool:
        index = max(0, min(len(self.pages) - 1, index))
        target = self.pages[index]
        if not target.get("folder") and len(self._main_pages()) <= 1:
            return False
        if target.get("folder"):
            # 删除文件夹子页 = 去掉指向它的按键，子页和里面的子文件夹一起回收
            owner = self._folder_owner(target.get("id"))
            if owner:
                self.pages[owner[0]]["actions"].pop(owner[1], None)
                self.page = owner[0]
        else:
            cur_id = self.pages[self.page].get("id")
            self.pages.pop(index)
            idx = self._page_index(cur_id)
            if idx < 0:                      # 删的就是当前页：落到它前面一页（没有就第一页）
                mains = self._main_pages()
                before = [i for i in mains if i < index]
                idx = before[-1] if before else mains[0]
            self.page = idx
        self._drop_orphan_folders()
        self.page = max(0, min(len(self.pages) - 1, self.page))
        self._bind_page()
        self._save_config()
        self._dirty = True
        if self.device.connected:
            self.apply_keys()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        log(f"删除第 {index + 1} 页，现在共 {len(self.pages)} 页")
        return True

    def rename_page(self, index: int, name: str) -> bool:
        name = (name or "").strip()[:20]
        if not (0 <= index < len(self.pages)) or not name:
            return False
        self.pages[index]["name"] = name
        if self.pages[index].get("folder"):        # 文件夹改名：指向它的按键名字跟着改
            owner = self._folder_owner(self.pages[index].get("id"))
            if owner:
                self.pages[owner[0]]["actions"][owner[1]]["name"] = name
        self._save_config()
        self.broadcast({"type": "page", "page": self.page, "pageCount": len(self.pages)})
        return True

    def add_nav_pages(self, name: str) -> None:
        """翻页只在主页面之间走；在文件夹里按上一页 / 下一页 = 返回上级。"""
        mains = self._main_pages()
        if name == "返回上级" or (self.pages[self.page].get("folder") and name in ("上一页", "下一页")):
            self.folder_back()
            return
        pos = mains.index(self.page) if self.page in mains else 0
        if name == "下一页":
            if pos + 1 < len(mains):
                self.goto_page(mains[pos + 1])
        elif name == "上一页":
            if pos > 0:
                self.goto_page(mains[pos - 1])
        elif name == "主页":
            self.go_home()

    def go_home(self) -> bool:
        """「主页」键：回到当前场景的第一页；相册场景里是回到第一张图片。"""
        if self.album_active():
            return self.album_goto(index=0)
        mains = self._main_pages()
        target = mains[0] if mains else 0
        if self.page == target:
            log("已经是第 1 页")
            return False
        return self.goto_page(target)

    def _read_config_file(self) -> dict:
        """读 config.json。Windows 上文件正被原子替换的瞬间会 PermissionError，
        这不是损坏，重试即可——以前这里直接回退到旧备份，刚改的设置（如待机时间）就被冲掉了。"""
        last = None
        for _ in range(20):
            try:
                with self._save_lock:
                    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
                        data = json.load(fh)
                return data if isinstance(data, dict) else {}
            except PermissionError as exc:
                last = exc
                time.sleep(0.05)
        raise last

    def _load_config(self) -> None:
        try:
            cfg = self._read_config_file()
        except (OSError, ValueError) as exc:
            example = os.path.join(HERE, "config.example.json")
            if not os.path.isfile(CONFIG_PATH) and os.path.isfile(example):
                try:
                    shutil.copyfile(example, CONFIG_PATH)
                    log("已从 config.example.json 初始化配置")
                except OSError:
                    pass
            # 配置损坏时，优先用最近的自动备份，而不是回退到默认值
            log(f"读取配置失败：{exc}，尝试最近的自动备份…")
            cfg = {}
            for name in self.list_config_backups():
                try:
                    with open(os.path.join(CONFIG_BAK_DIR, name), encoding="utf-8-sig") as fh:
                        cfg = json.load(fh)
                    log(f"已从备份恢复配置：{name}")
                    break
                except (OSError, ValueError):
                    continue
        try:
            self.wallpaper_gap = KEY_GAP     # 旧配置的 gap 只保留在磁盘，不再用于渲染
            self.wallpaper_flip = bool(cfg.get("flip", self.wallpaper_flip))
            self.led_offset = int(cfg.get("ledOffset", 20)) % (LED_COUNT - 2)
            self.rotation = int(cfg.get("rotation", 0)) % 360
            self.effect_reverse = bool(cfg.get("effectReverse", False))
            if self.rotation not in (0, 90, 180, 270):
                self.rotation = 0
            fit = str(cfg.get("fit", "cover"))
            self.wallpaper_fit = fit if fit in ("cover", "contain", "fill") else "cover"
            self.wallpaper_zoom = max(0.2, min(4.0, float(cfg.get("zoom", 1.0))))
            self.wallpaper_ox = int(cfg.get("ox", 0))
            self.wallpaper_oy = int(cfg.get("oy", 0))
            self.standby_sec = max(STANDBY_MIN, min(STANDBY_MAX, int(cfg.get("standby", 300))))
            lit = cfg.get("lighting")
            if isinstance(lit, dict):
                for k in ("mode", "color", "speed", "brightness"):
                    if lit.get(k) is not None:
                        self.lighting[k] = lit[k]
                if self.lighting["mode"] not in MODES:
                    self.lighting["mode"] = DEFAULT_LIGHTING["mode"]
                custom = lit.get("custom")
                if isinstance(custom, list) and len(custom) == LED_COUNT:
                    self.lighting["custom"] = [[int(v) for v in c[:3]] for c in custom]
            alb = cfg.get("album")
            if isinstance(alb, dict):
                self.album_items = [str(n) for n in (alb.get("items") or [])
                                    if os.path.isfile(os.path.join(UPLOAD_DIR, os.path.basename(str(n))))]
                self.album_slideshow = bool(alb.get("slideshow", True))
                self.album_interval = max(2, min(600, int(alb.get("interval", 8))))
                tr = str(alb.get("transition") or "fade")
                self.album_transition = tr if tr in ALBUM_TRANSITIONS else "fade"
                self.album_fit = "contain" if alb.get("fit") == "contain" else "cover"
            self.standby_source = "album" if cfg.get("standbySource") == "album" else "wallpaper"
            self.standby_brightness = max(0, min(100, int(cfg.get("standbyBrightness", 60))))
            self.custom_scene_name = str(cfg.get("customScene") or "自定义")[:40]
            self.active_scene = str(cfg.get("activeScene") or "")
            if self.active_scene in LEGACY_ALBUM_NAMES:   # 旧版把待机相册当成场景用
                self.active_scene = self.custom_scene_name
            self.wallpaper_enabled = bool(cfg.get("wallpaperEnabled", self.wallpaper_enabled))
            try:
                ab = int(cfg.get("autoBlank", 0) or 0)
            except (TypeError, ValueError):
                ab = 0
            self.autoblank_sec = 0 if ab <= 0 else max(AUTOBLANK_MIN, min(AUTOBLANK_MAX, ab))
            if self.autoblank_sec and self.wallpaper_enabled:
                self.wallpaper_enabled = False    # 二选一：自动息屏开着就不用待机画面
            self.keep_awake = bool(cfg.get("keepAwake", True))
            self.close_to_tray = bool(cfg.get("closeToTray", True))
            pages = cfg.get("pages")
            if isinstance(pages, list) and pages:
                self.pages = []
                for p in pages:
                    self.pages.append({
                        "id": str(p.get("id") or "") or self._new_page_id(),
                        "folder": bool(p.get("folder")),
                        "name": str(p.get("name") or "页"),
                        "images": {},
                        "actions": {},
                        "styles": {int(k): v for k, v in (p.get("styles") or {}).items()
                                   if str(k).isdigit()},
                        "gestures": {int(k): v for k, v in (p.get("gestures") or {}).items()
                                     if str(k).isdigit() and isinstance(v, dict) and v},
                    })
                for idx, p in enumerate(pages):
                    for key, name in (p.get("images") or {}).items():
                        path = os.path.join(UPLOAD_DIR, name)
                        if not os.path.isfile(path):
                            path = os.path.join(ICON_DIR, name)
                        if os.path.isfile(path):
                            data = self._load_image_source(path)
                            if data:
                                self.pages[idx]["images"][int(key)] = data
                    for key, spec in (p.get("actions") or {}).items():
                        k = int(key)
                        if isinstance(spec, dict):
                            self.pages[idx]["actions"][k] = spec
                        elif spec:
                            self.pages[idx]["actions"][k] = {"type": "launch", "path": str(spec)}
                self.page = max(0, min(len(self.pages) - 1, int(cfg.get("page", 0))))
            else:
                # 旧版单页配置迁移
                for key, spec in (cfg.get("actions") or {}).items():
                    k = int(key)
                    if isinstance(spec, dict):
                        self.pages[0]["actions"][k] = spec
                    elif spec:
                        self.pages[0]["actions"][k] = {"type": "launch", "path": str(spec)}
                for key, name in (cfg.get("keys") or {}).items():
                    path = os.path.join(UPLOAD_DIR, name)
                    if os.path.isfile(path):
                        data = self._load_image_source(path)
                        if data:
                            self.pages[0]["images"][int(key)] = data
                self.page = 0
            self.led_reverse = bool(cfg.get("ledReverse", False))
            root = str(cfg.get("officialRoot") or "")
            if root and os.path.isdir(root):
                core.SD_ROOT = root           # 拆模块后要改 core 里的那份，别的模块才看得到
                self.official_root_cfg = root
            wp = cfg.get("wallpaper")
            if wp:
                path = load_path(wp)
                if os.path.isfile(path):
                    self.wallpaper_file = path
        except (OSError, ValueError, TypeError):
            pass

    def _backup_config(self) -> None:
        """每次保存前留一份配置快照，最多保留 20 份。"""
        try:
            if not os.path.isfile(CONFIG_PATH):
                return
            now = time.time()
            if now - getattr(self, "_last_backup", 0) < 30:
                return
            self._last_backup = now
            os.makedirs(CONFIG_BAK_DIR, exist_ok=True)
            with open(CONFIG_PATH, "rb") as fh:
                data = fh.read()
            if not data.strip():
                return
            dst = os.path.join(CONFIG_BAK_DIR, f"config-{time.strftime('%Y%m%d-%H%M%S')}.json")
            if not os.path.exists(dst):
                with open(dst, "wb") as fh:
                    fh.write(data)
            files = sorted(glob.glob(os.path.join(CONFIG_BAK_DIR, "config-*.json")))
            for old in files[:-20]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    def list_config_backups(self) -> list:
        try:
            files = sorted(glob.glob(os.path.join(CONFIG_BAK_DIR, "config-*.json")), reverse=True)
            return [os.path.basename(f) for f in files[:20]]
        except Exception:  # noqa: BLE001
            return []

    def restore_config_backup(self, name: str) -> bool:
        safe = os.path.basename(name or "")
        path = os.path.join(CONFIG_BAK_DIR, safe)
        if not safe.startswith("config-") or not os.path.isfile(path):
            return False
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        return self.import_config(data)

    def _save_config(self) -> None:
        # 多个请求线程可能同时保存，共用同一个 .tmp 会互相覆盖 / os.replace 报错
        with self._save_lock:
            for attempt in range(3):
                try:
                    self._save_config_locked()
                    break
                except RuntimeError as exc:     # 别的线程正在增删按键字典：稍等重拍一次快照
                    if attempt == 2:
                        log(f"保存配置失败：{exc}")
                    time.sleep(0.05)
        self._plugin_sync()

    def _save_config_locked(self) -> None:
        self._backup_config()
        cfg = {
            "gap": KEY_GAP,
            "flip": self.wallpaper_flip,
            "ledOffset": self.led_offset,
            "ledReverse": self.led_reverse,
            "rotation": self.rotation,
            "effectReverse": self.effect_reverse,
            "fit": self.wallpaper_fit,
            "zoom": self.wallpaper_zoom,
            "ox": self.wallpaper_ox,
            "oy": self.wallpaper_oy,
            "page": self.page,
            "standby": self.standby_sec,
            "standbyBrightness": self.standby_brightness,
            "customScene": self.custom_scene_name,
            "activeScene": self.active_scene,
            "wallpaperEnabled": self.wallpaper_enabled,
            "autoBlank": self.autoblank_sec,
            "keepAwake": self.keep_awake,
            "closeToTray": self.close_to_tray,
            "lighting": {k: self.lighting[k] for k in ("mode", "color", "speed", "brightness", "custom")},
            "standbySource": self.standby_source,
            "album": {"items": self.album_items, "slideshow": self.album_slideshow,
                      "interval": self.album_interval, "transition": self.album_transition,
                      "fit": self.album_fit},
            "pages": [
                {
                    "id": p.get("id") or "",
                    **({"folder": True} if p.get("folder") else {}),
                    "name": p.get("name", "页"),
                    "images": {str(k): os.path.basename(v.get("file", "") or "")
                               for k, v in p["images"].items() if v.get("file")},
                    "actions": {str(k): v for k, v in p["actions"].items()},
                    "styles": {str(k): v for k, v in (p.get("styles") or {}).items()},
                    "gestures": {str(k): v for k, v in (p.get("gestures") or {}).items() if v},
                }
                for p in self.pages
            ],
            "wallpaper": store_path(self.wallpaper_file) if self.wallpaper_file else "",
        }
        if self.official_root_cfg:
            cfg["officialRoot"] = self.official_root_cfg
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CONFIG_PATH)   # 原子替换，避免写一半被打断导致配置损坏
            self._snapshot_custom_scene()
        except OSError as exc:
            log(f"保存配置失败：{exc}")

    # -- 灯珠空间映射 ------------------------------------------------------ #
    def _to_hw(self, colors):
        """逻辑顺序（UI 上按一圈排列）→ 设备实际灯珠顺序。

        外圈是 22 颗（与设备一致），只在这一圈里旋转；正面 2 颗固定不动。
        """
        ring = LED_COUNT - 2
        out = [None] * LED_COUNT
        for i in range(ring):
            j = (self.led_offset - i) % ring if self.led_reverse \
                else (self.led_offset + i) % ring
            out[j] = colors[i]
        out[ring] = colors[ring]
        out[ring + 1] = colors[ring + 1]
        return out
