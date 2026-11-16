"""App 的一部分：按键画面渲染、待机画面、Dock 同步预览、原生动图。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import glob
import hashlib
import io
import json
import os
import sys
import threading
import time
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence
import official

from core import (  # noqa: F401
    CROP_CACHE,
    GIF_CACHE,
    KEY_COUNT,
    KEY_SIZE,
    LIB_THUMB_DIR,
    PLUGIN_DATA,
    SCENE_DIR,
    UPLOAD_DIR,
    _touch,
    asset_path,
    log,
    parse_color,
    sweep_dir,
)


class RenderMixin:

    # -- 按键画面 ---------------------------------------------------------- #
    # -- 待机 -------------------------------------------------------------- #
    def _draw_text(self, img, style: dict):
        """把标题文字画到图像上（颜色 / 透明度 / 字号 / 上下位置）。"""
        text = (style.get("text") or "").strip()
        if not text:
            return img
        fg = parse_color(style.get("color") or "#e8eefc") or (232, 238, 252)
        opacity = max(0, min(100, int(style.get("opacity", 100))))
        size = max(8, min(40, int(style.get("size", 20))))
        pos = style.get("pos") or "center"
        font = None
        for path in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\segoeuib.ttf"):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:  # noqa: BLE001
                continue
        if font is None:
            font = ImageFont.load_default()
        overlay = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        shadow = Image.new("RGBA", (KEY_SIZE, KEY_SIZE), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        lines = [text]
        if "\n" in text:
            lines = [ln for ln in text.split("\n") if ln.strip()][:3] or [""]
        elif od.textlength(text, font=font) > KEY_SIZE - 6 and len(text) > 3:
            mid = (len(text) + 1) // 2
            lines = [text[:mid], text[mid:]]
        line_h = size + 3
        total = line_h * len(lines)
        if pos == "top":
            y = 3
        elif pos == "bottom":
            y = KEY_SIZE - total - 3
        else:
            y = (KEY_SIZE - total) / 2
        alpha = int(255 * opacity / 100)
        for line in lines:
            w = od.textlength(line, font=font)
            x = (KEY_SIZE - w) / 2
            sd.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0, int(alpha * 0.55)))
            od.text((x, y), line, font=font, fill=(*fg, alpha))
            y += line_h
        img = Image.alpha_composite(img.convert("RGBA"), shadow)
        return Image.alpha_composite(img, overlay).convert("RGB")

    @staticmethod
    def _draw_flash(img, kind: str):
        """插件 showOk / showAlert：在键面上盖一个对勾 / 感叹号。"""
        img = img.convert("RGB")
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle([0, 0, KEY_SIZE, KEY_SIZE], fill=(0, 0, 0, 120))
        if kind == "ok":
            d.ellipse([16, 16, 48, 48], fill=(40, 180, 90, 255))
            d.line([(24, 32), (30, 39), (41, 25)], fill=(255, 255, 255, 255), width=4)
        else:
            d.polygon([(32, 13), (52, 48), (12, 48)], fill=(230, 160, 30, 255))
            d.rectangle([30, 24, 34, 38], fill=(255, 255, 255, 255))
            d.rectangle([30, 41, 34, 45], fill=(255, 255, 255, 255))
        return img

    def _styled_frame(self, key: int, jpeg: bytes) -> bytes:
        """动图帧也要叠标题，保证控制台和屏幕一致。"""
        style = self.key_styles.get(key) or {}
        if not (style.get("text") or "").strip():
            return jpeg
        ck = (key, hash(jpeg), style.get("text"), style.get("color"),
              style.get("opacity"), style.get("size"), style.get("pos"))
        cache = getattr(self, "_frame_text_cache", None)
        if cache is None:
            cache = self._frame_text_cache = {}
        if ck in cache:
            return cache[ck]
        try:
            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception:  # noqa: BLE001
            return jpeg
        img = self._draw_text(img, style)
        if self.selected_key == key:
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, KEY_SIZE - 1, KEY_SIZE - 1], outline=(86, 148, 255))
            d.rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], outline=(140, 185, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        data = buf.getvalue()
        if len(cache) > 800:
            cache.clear()
        cache[ck] = data
        return data

    def _number_tile(self, key: int) -> bytes:
        """按键画面：自定义图 / 底色 + 叠加文字（颜色、透明度、字号、位置）。"""
        style = self.key_styles.get(key) or {}
        text = (style.get("text") or "").strip()
        bg = parse_color(style.get("bg") or "#000000") or (0, 0, 0)
        fg = parse_color(style.get("color") or "#e8eefc") or (232, 238, 252)
        opacity = max(0, min(100, int(style.get("opacity", 100))))
        size = max(8, min(40, int(style.get("size", 20))))
        pos = style.get("pos") or "center"
        act0 = self.key_actions.get(key)
        if isinstance(act0, dict) and act0.get("type") == "toggle" and "|" in text:
            parts = text.split("|")
            text = parts[int(act0.get("state") or 0) % len(parts)].strip()
        base = self._key_jpeg(key)
        vis = self._plugin_vis(key)
        flash = ""
        if vis:
            if vis.get("title") is not None:
                text = str(vis["title"]).strip()
            if vis.get("flash") and vis["flash"][1] > time.time():
                flash = vis["flash"][0]
        ck = (key, text, bg, fg, opacity, size, pos, hash(base) if base else 0, flash)
        if ck in self._num_cache:
            return self._num_cache[ck]

        if base:
            try:
                img = Image.open(io.BytesIO(base)).convert("RGB")
                img = ImageOps.fit(img, (KEY_SIZE, KEY_SIZE), Image.LANCZOS)
            except Exception:  # noqa: BLE001
                img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), bg)
        else:
            img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), bg)
            d0 = ImageDraw.Draw(img)
            d0.rounded_rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], radius=8,
                                 outline=(max(bg[0], 30), max(bg[1], 36), max(bg[2], 48)), width=1)

        img = self._draw_text(img, dict(style, text=text or ("" if base or vis else str(key))))
        if flash:
            img = self._draw_flash(img, flash)

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        if len(self._num_cache) > 400:          # 时钟 / 监控类插件每帧一张新图：超量就整体清掉重建
            self._num_cache.clear()
        self._num_cache[ck] = data
        return data

    def set_key_style(self, key: int, spec: dict) -> bool:
        if not (1 <= key <= KEY_COUNT):
            return False
        self._anim_pause_until = time.time() + 1.0
        style = self.key_styles.setdefault(key, {})
        if "text" in spec:
            style["text"] = str(spec["text"])[:12]
        if "color" in spec and parse_color(str(spec["color"])):
            style["color"] = str(spec["color"])
        if "bg" in spec and parse_color(str(spec["bg"])):
            style["bg"] = str(spec["bg"])
        for field, lo, hi in (("opacity", 0, 100), ("size", 8, 40)):
            if field in spec:
                try:
                    style[field] = max(lo, min(hi, int(spec[field])))
                except (TypeError, ValueError):
                    pass
        if "pos" in spec and str(spec["pos"]) in ("top", "center", "bottom"):
            style["pos"] = str(spec["pos"])
        # 全默认就当没设置过
        if (not (style.get("text") or "").strip()
                and (style.get("color") or "#e8eefc") == "#e8eefc"
                and (style.get("bg") or "#000000") == "#000000"
                and int(style.get("opacity", 100)) == 100
                and int(style.get("size", 20)) == 20
                and (style.get("pos") or "center") == "center"):
            self.key_styles.pop(key, None)
        self._save_config()
        if self.device.connected:
            self._gif_update_key(key)          # 标题变了，动图要重新叠字
            self.device.send_image(key, self._number_tile(key))
            self.device.refresh()
        return True

    def _render_key(self, key: int) -> bytes:
        """按键最终画面：自定义图/底色 + 叠加文字，选中时再叠内边缘高亮。"""
        base = self._number_tile(key)
        if self.selected_key != key:
            return base
        cached = getattr(self, "_sel_cache", None)
        if cached and cached[0] == key and cached[1] == base:
            return cached[2]
        try:
            img = Image.open(io.BytesIO(base)).convert("RGB")
        except Exception:  # noqa: BLE001
            return base
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, KEY_SIZE - 1, KEY_SIZE - 1], outline=(86, 148, 255))
        d.rectangle([1, 1, KEY_SIZE - 2, KEY_SIZE - 2], outline=(140, 185, 255))
        d.rectangle([2, 2, KEY_SIZE - 3, KEY_SIZE - 3], outline=(52, 96, 170))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        data = buf.getvalue()
        self._sel_cache = (key, base, data)
        return data

    def select_key(self, key: int) -> bool:
        """控制台选中某个键时，让设备上对应的屏幕亮一圈内边缘。"""
        if not (0 <= key <= KEY_COUNT + 3):
            return False
        old = self.selected_key
        if old == key:
            return True
        self.selected_key = key
        self._anim_pause_until = time.time() + 1.2
        if self.device.connected and not self.in_standby:
            for k in (old, key):
                # SDK 正在原生播放的动图键不重画，免得闪一下静态帧
                if k and 1 <= k <= KEY_COUNT and k not in self._gif_active:
                    self.device.send_image(k, self._render_key(k))
            self.device.refresh()
        return True

    # -- Dock 同步预览 ------------------------------------------------------ #
    def _preview_source(self, src: str) -> str | None:
        """界面传来的预览来源 → 本机图片文件。只认这几种，不接受任意路径。"""
        src = src or ""
        if src.startswith("pack:"):
            pack_id, _, fname = src[5:].partition("/")
            return self.icon_pack_file(pack_id, fname)
        if src.startswith("lib:"):
            return self.library_path(src[4:])
        if src.startswith("plugin:"):
            plugin_id, _, idx = src[7:].rpartition("/")
            try:
                return self.plugin_icon_path(plugin_id, int(idx))
            except ValueError:
                return None
        if src.startswith("asset:"):
            return asset_path(src[6:])
        if src.startswith("program:"):
            return self.program_icon(src[8:])
        return None

    def set_key_preview(self, key: int, src: str) -> bool:
        """把一张还没保存的图临时显示在设备的某个键上（只取第一帧，不写配置）。"""
        if not (1 <= key <= KEY_COUNT):
            return False
        path = self._preview_source(src)
        if not path or not os.path.isfile(path):
            return False
        try:
            img = official.read_image(path)
            raw = img[1] if img else open(path, "rb").read()
            with Image.open(io.BytesIO(raw)) as im:
                rgb = ImageOps.fit(self._flat_rgb(im), (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, "JPEG", quality=90)
        except Exception as exc:  # noqa: BLE001
            log(f"预览图读取失败 {path}: {exc}")
            return False
        others = [k for k in self.key_previews if k != key]
        self.key_previews = {key: buf.getvalue()}       # 同一时间只预览一个键
        self._repaint_keys([key, *others])
        return True

    def clear_key_preview(self, key: int = 0) -> bool:
        keys = list(self.key_previews) if not key else [k for k in self.key_previews if k == key]
        if not keys:
            return False
        for k in keys:
            self.key_previews.pop(k, None)
        self._repaint_keys(keys)
        return True

    def _repaint_keys(self, keys) -> None:
        if not (self.device.connected and not (self.in_standby or self.writes_paused or self.quiet
                                               or self.lighting_only or self.yielded or self.screen_off)):
            return
        self._anim_pause_until = time.time() + 0.6
        for k in keys:
            if not (1 <= k <= KEY_COUNT):
                continue
            if k in self.key_previews:
                # 预览盖住原生动图：先停掉这个键的动图，否则 SDK 会马上把下一帧画回去
                with self._gif_lock:
                    if self._gif_active.pop(k, None):
                        self.device.clear_key_gif(k)
            else:
                self._gif_update_key(k)
            if k not in self._gif_active:
                self.device.send_image(k, self._render_key(k))
        self.device.refresh()

    def _plugin_vis(self, key: int) -> dict | None:
        spec = self.key_actions.get(key)
        if isinstance(spec, dict) and spec.get("type") == "plugin":
            return self.plugin_visual.get(spec.get("context") or "")
        return None

    def _key_jpeg(self, key: int) -> bytes | None:
        pv = self.key_previews.get(key)
        if pv:
            return pv
        vis = self._plugin_vis(key)
        if vis and vis.get("jpeg"):
            return vis["jpeg"]
        item = self.key_overrides.get(key)
        if not item:
            return None
        if item.get("type") == "image":
            return item["jpeg"]
        frames = item.get("frames") or []
        return frames[item["idx"]][0] if frames else None

    def _clear_screen(self) -> None:
        """M18 上清屏容易把设备送进异常状态，这里保留开关但默认不做。"""
        if not getattr(self, "clear_before_paint", False):
            return
        try:
            self.device.clear_keys()
        except Exception:  # noqa: BLE001
            pass

    def apply_keys(self) -> bool:
        """正常状态：每个键显示自己的图（没有就用编号占位）。"""
        if not self.device.connected or self.writes_paused or self.quiet or self.lighting_only or self.yielded:
            return False
        if self.screen_off:
            if not getattr(self, "_blanked", False):
                self.blank_screen()     # 息屏：15 个键刷黑 + 背光 0
            return False
        self._blanked = False
        self._gif_stop_all()
        # 先发亮度命令唤醒面板，否则设备睡过去后写进去的图不会显示
        try:
            self.device.set_screen_brightness(self.screen_brightness)
            time.sleep(0.06)
        except Exception:  # noqa: BLE001
            pass
        self.in_standby = False
        ok = True
        self._pushing = True
        try:
            self.device.clear_queue()
            self._clear_screen()
            for key in range(1, KEY_COUNT + 1):
                jpeg = self._render_key(key)
                if not self.device.send_image(key, jpeg):
                    ok = False
            self.device.refresh()
        finally:
            self._pushing = False
        self._gif_start_all()
        self._last_paint = time.time()
        self._last_activity = time.time()   # 刚画完键面就重置待机计时，避免立刻又进待机
        caller = sys._getframe(1).f_code.co_name
        log(f"重画按键画面（自定义 {len(self.key_overrides)} 个，原生动图 {len(self._gif_active)} 个）来自 {caller}"
            + ("" if ok else "  ⚠ 有命令没发出去"))
        return ok

    # -- 动图：交给 SDK 原生播放 ------------------------------------------- #
    def _gif_allowed(self) -> bool:
        d = self.device
        # 相册播放（in_standby）也允许：整屏只有相册这一组 GIF，没有按键动图和它抢
        standby_ok = self.in_standby and self.preview_mode == "album"
        return bool(getattr(d, "gif_native", False) and d.connected
                    and not (self.writes_paused or self.quiet or self.lighting_only or self.yielded
                             or self.screen_off or (self.in_standby and not standby_ok)))

    def _gif_sync_session(self) -> None:
        """设备重连后 SDK 里的 GIF 已被清空，本地记录也要作废。"""
        session = getattr(self.device, "session", 0)
        if session != self._gif_session:
            self._gif_session = session
            self._gif_active = {}

    def _gif_path_for(self, key: int, item: dict | None, style: dict | None = None) -> str | None:
        """把动图渲染成 64×64 GIF（叠好标题），按内容缓存，交给 SDK 直接播。"""
        if not item or item.get("type") != "anim":
            return None
        src = item.get("file") or ""
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            return None
        if style is None:
            style = self.key_styles.get(key) or {}
        text = {k: style.get(k) for k in ("text", "color", "opacity", "size", "pos")} \
            if (style.get("text") or "").strip() else {}
        sig = hashlib.sha1(json.dumps([os.path.abspath(src), mtime, text], sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()[:20]
        out = os.path.join(GIF_CACHE, f"{sig}.gif")
        if os.path.isfile(out):
            _touch(out)
            return out
        frames, durations = [], []
        try:
            with Image.open(src) as im:
                for i, frame in enumerate(ImageSequence.Iterator(im)):
                    if i >= 240:
                        break
                    rgb = ImageOps.fit(self._flat_rgb(frame), (KEY_SIZE, KEY_SIZE),
                                       Image.Resampling.LANCZOS)
                    if text:
                        rgb = self._draw_text(rgb, text)
                    frames.append(rgb)
                    durations.append(max(20, int(frame.info.get("duration", 100) or 100)))
            if len(frames) < 2:
                return None
            os.makedirs(GIF_CACHE, exist_ok=True)
            tmp = out + ".tmp"
            frames[0].save(tmp, "GIF", save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, disposal=1, optimize=False)
            os.replace(tmp, out)
            return out
        except Exception as exc:  # noqa: BLE001
            log(f"动图转换失败 {src}: {exc}")
            return None

    def _warm_gif_cache(self) -> None:
        """后台把所有场景里的动图预先转好，第一次切场景 / 翻页也不用等渲染。"""
        time.sleep(3)
        made = 0
        for f in glob.glob(os.path.join(SCENE_DIR, "*.json")):
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    cfg = json.load(fh)
            except (OSError, ValueError):
                continue
            for page in cfg.get("pages") or []:
                styles = page.get("styles") or {}
                for key, name in (page.get("images") or {}).items():
                    path = os.path.join(UPLOAD_DIR, os.path.basename(str(name)))
                    if not path.lower().endswith(".gif") or not os.path.isfile(path):
                        continue
                    try:
                        with Image.open(path) as im:
                            if getattr(im, "n_frames", 1) < 2:
                                continue
                    except Exception:  # noqa: BLE001
                        continue
                    before = len(os.listdir(GIF_CACHE)) if os.path.isdir(GIF_CACHE) else 0
                    self._gif_path_for(int(key), {"type": "anim", "file": path},
                                       styles.get(str(key)) or {})
                    made += (len(os.listdir(GIF_CACHE)) if os.path.isdir(GIF_CACHE) else 0) - before
        if made:
            log(f"动图预渲染完成：新增 {made} 个")

    def _gif_want(self, key: int) -> str | None:
        """这个键应该由 SDK 播哪个 GIF：插件发来的动图 > 插件静态图（不播）> 按键自己的动图。"""
        vis = self._plugin_vis(key)
        if vis and (vis.get("gif") or vis.get("jpeg")):
            return vis.get("gif")
        return self._gif_path_for(key, self.key_overrides.get(key))

    def _gif_stop_all(self) -> None:
        """整屏换画面（待机 / 壁纸 / 息屏 / 重画）前先停掉 SDK 的动图，否则会盖掉新画面。"""
        with self._gif_lock:
            self._gif_sync_session()
            if not self._gif_active:
                return
            self.device.stop_gif_loop()
            for key in list(self._gif_active):
                self.device.clear_key_gif(key)
            self._gif_active = {}

    def _gif_start_all(self) -> None:
        with self._gif_lock:
            self._gif_sync_session()
            if not self._gif_allowed():
                return
            want = {}
            for key in range(1, KEY_COUNT + 1):
                path = None if key in self.key_previews else self._gif_want(key)
                if path:
                    want[key] = path
            for key in [k for k in self._gif_active if k not in want]:
                self.device.clear_key_gif(key)
            for key, path in want.items():
                if self._gif_active.get(key) != path:
                    self.device.set_key_gif(key, path)
            self._gif_active = want
            if want:
                self.device.start_gif_loop()
            else:
                self.device.stop_gif_loop()
            keys = sorted(want.keys())
        if keys:
            self.broadcast({"type": "gif", "t": time.time(), "keys": keys})

    def _gif_resync(self) -> None:
        """让所有正在播的动图从同一时刻重新开始，否则新加进来的那张会一直错位。

        只 stop/start 循环不够：每张动图是各自加载、各自计时的，停一下再开
        并不会把它们拉回同一个起点。这里整组清掉重发，再一起开始播。
        """
        with self._gif_lock:
            if not self._gif_active:
                return
            items = dict(self._gif_active)
            self.device.stop_gif_loop()
            for key in items:
                self.device.clear_key_gif(key)
            for key, path in items.items():
                self.device.set_key_gif(key, path)
            self._gif_active = dict(items)
            self.device.start_gif_loop()
            keys = sorted(items.keys())
        self.broadcast({"type": "gif", "t": time.time(), "keys": keys})

    def _gif_update_key(self, key: int) -> None:
        """单个键换图 / 改标题：只动这一个键。要在写静态画面之前调用。"""
        with self._gif_lock:
            self._gif_sync_session()
            want = self._gif_want(key) if self._gif_allowed() and key not in self.key_previews else None
            old = self._gif_active.get(key)
            if old == want:
                return
            if old:
                self.device.clear_key_gif(key)
                self._gif_active.pop(key, None)
            if want:
                self.device.set_key_gif(key, want)
                self._gif_active[key] = want
                self.device.start_gif_loop()
            elif not self._gif_active:
                self.device.stop_gif_loop()
        # 新加的动图要和大家对齐：整组停一下再一起开始
        if want:
            self._gif_resync()

    def apply_standby(self) -> bool:
        """待机：整屏铺壁纸（壁纸亮度在渲染层调暗，屏幕亮度保持正常）。"""
        if self.preview_mode == "album":
            self.preview_mode = "wallpaper"
        self.in_standby = True
        return self._push_canvas("待机图")

    def _cache_sweep_loop(self) -> None:
        """缓存只增不减：动图缓存、旋转副本、相册缩略图、裁图临时目录、插件日志，定期清扫。"""
        time.sleep(60)
        while True:
            try:
                in_use = set(self._gif_active.values())
                for vis in list(self.plugin_visual.values()):
                    if vis.get("gif"):
                        in_use.add(vis["gif"])
                report = {
                    "动图缓存": sweep_dir(GIF_CACHE, 30, 300, keep=in_use),
                    "相册缩略图": sweep_dir(LIB_THUMB_DIR, 60, 200),
                    "裁图临时文件": sweep_dir(CROP_CACHE, 1, recursive=True),
                    "插件旧日志": sweep_dir(os.path.join(PLUGIN_DATA, "logs"), 30),
                }
                freed = {k: v for k, v in report.items() if v[0]}
                if freed:
                    log("缓存清扫：" + "，".join(f"{k} {n} 个（{b / 1048576:.1f} MB）" for k, (n, b) in freed.items()))
            except Exception as exc:  # noqa: BLE001
                log(f"缓存清扫出错：{exc}")
            time.sleep(86400)

    def _diag_loop(self) -> None:
        last = dict(self.device.stats)
        while True:
            time.sleep(20)
            cur = dict(self.device.stats)
            delta = {k: cur[k] - last[k] for k in cur}
            last = cur
            if self.device.connected and any(delta.values()):
                log(f"读取统计 {delta}")

    def _standby_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if self.album_active():
                    if self.device.connected and time.time() - self._last_paint > 25:
                        self._push_album()       # 定期补一帧，避免设备自己睡过去
                elif (self.autoblank_sec > 0 and not self.screen_off and not self.in_standby
                        and self.device.connected and not self.quiet and not self.yielded
                        and time.time() - self._last_activity > self.autoblank_sec):
                    # 自动息屏：空闲够久就把 15 个键刷黑 + 背光 0（按键通道还在，按任意键唤醒）
                    self.screen_off = True
                    self.blank_screen()
                    self.broadcast({"type": "status", "screenOff": True})
                    log(f"自动息屏：空闲 {self.autoblank_sec} 秒，屏幕已关闭（按任意键唤醒）")
                elif (self.wallpaper_enabled and self.standby_sec > 0
                        and not self.in_standby and not self.screen_off
                        and self.device.connected and not self.quiet
                        and time.time() - self._last_activity > self.standby_sec):
                    if self.standby_source == "album" and self.album_items:
                        self._album_return = None          # 待机进的相册，按键回到原来的按键画面
                        self.album_play()
                    else:
                        self.apply_standby()
                elif (self.in_standby and self.device.connected
                      and time.time() - self._last_paint > 25):
                    # 长时间待机时定期补一帧，避免设备自己睡过去显示开机图
                    self.apply_standby()
            except Exception:  # noqa: BLE001
                pass

    def _black_jpeg(self) -> bytes:
        """一张全黑的 64×64，用来把 15 个键刷成黑屏。"""
        if self._black_frame is None:
            img = Image.new("RGB", (KEY_SIZE, KEY_SIZE), (0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=72)
            self._black_frame = buf.getvalue()
        return self._black_frame

    def blank_screen(self) -> bool:
        """息屏：15 个键全刷成黑 + 背光 0 + 停掉动图。

        不用 transport 的硬件 sleep —— 设备一睡就不再上报按键，三个导航键按下去也叫不醒。
        刷黑在观感上就是关机，按键通道还活着，按一下就回按键画面。
        """
        dev = self.device
        if dev is None or not getattr(dev, "connected", False):
            return False
        try:
            self._gif_stop_all()
        except Exception:  # noqa: BLE001
            pass
        ok = True
        try:
            black = self._black_jpeg()
            for key in range(1, KEY_COUNT + 1):
                if not dev.send_image(key, black):
                    ok = False
            dev.refresh()
            self._frame_hash = None
        except Exception:  # noqa: BLE001
            ok = False
        try:
            dev.set_screen_brightness(0)
        except Exception:  # noqa: BLE001
            pass
        self._blanked = ok
        if ok:
            self.broadcast({"type": "status", "screenOff": True})
            log("已刷黑屏：15 个键全黑、背光 0（按键通道还在，按任意键唤醒）")
        return ok

    def sleep_panel(self) -> bool:
        """息屏（导航键「屏幕开关」的关屏动作）。"""
        return self.blank_screen()

    def wake_panel(self) -> bool:
        dev = self.device
        if dev is None or not getattr(dev, "connected", False):
            return False
        self._blanked = False
        try:
            dev.wakeup()
        except Exception:  # noqa: BLE001
            pass
        try:
            return bool(dev.set_screen_brightness(self.screen_brightness))
        except Exception:  # noqa: BLE001
            return False

    CONSOLE_LIGHT_MODES = ("solid", "rainbow", "breathe", "chase", "gradient", "ripple")

    def console_action(self, op: str) -> str:
        """按键上的「控制台」动作：屏幕开关 / 屏蔽按键 / 灯光开关 / 灯光效果切换。"""
        op = (op or "").strip()
        if op == "screen":
            if self.screen_off:
                self.screen_off = False
                self.wake_panel()
                self.apply_keys()
                self.broadcast({"type": "status", "screenOff": False})
                return "屏幕已打开"
            self.screen_off = True
            threading.Thread(target=self.sleep_panel, daemon=True).start()
            self.broadcast({"type": "status", "screenOff": True})
            return "屏幕已关闭"
        if op == "mute":
            self.input_muted = not self.input_muted
            self.broadcast({"type": "status", "inputMuted": self.input_muted})
            return "按键已屏蔽（只有这个键还能用）" if self.input_muted else "按键已恢复"
        if op == "light":
            mode = self.lighting.get("mode") or "chase"
            if mode == "off":
                back = self._light_back or "chase"
                self._light_back = ""
                self.set_lighting({"mode": back})
                return f"灯光已打开（{back}）"
            self._light_back = mode
            self.set_lighting({"mode": "off"})
            return "灯光已关闭"
        if op == "lightfx":
            modes = list(self.CONSOLE_LIGHT_MODES)
            cur = self.lighting.get("mode") or "chase"
            nxt = modes[(modes.index(cur) + 1) % len(modes)] if cur in modes else modes[0]
            self.set_lighting({"mode": nxt})
            return f"灯光效果：{nxt}"
        return "未知的控制台动作"

    def _wake_from_standby(self) -> None:
        """按键唤醒：先叫醒设备，再整屏重画。"""
        # 打开第二个句柄会有扰动，只有确实很久没画过、而且最近没叫醒过才用
        now = time.time()
        if not self.screen_off and now - self._last_paint > 600 and now - self._last_wake > 60:
            if self.wake_panel():
                self._last_wake = now
        try:
            if not self.screen_off:      # 息屏时别把背光调回去，否则黑屏会泛白
                self.device.set_screen_brightness(self.screen_brightness)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
        self.apply_keys()
