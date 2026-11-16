"""App 的一部分：按键图片 / 素材、旋转、相册、相册场景、图标库相册分类。

由 server.py 拆出（方法体原样搬过来），App 通过多重继承把它们合在一起。
"""
from __future__ import annotations
import hashlib
import io
import json
import random
import os
import shutil
import subprocess
import threading
import time
from PIL import Image, ImageDraw, ImageOps

from core import (  # noqa: F401
    ALBUM_TRANSITIONS,
    GIF_CACHE,
    GRID_COLS,
    GRID_ROWS,
    IMAGE_EXTS,
    KEY_COUNT,
    KEY_SIZE,
    LIBRARY_DIR,
    LIB_THUMB_DIR,
    SCENE_DIR,
    UPLOAD_DIR,
    WALLPAPER,
    _inside,
    asset_path,
    log,
)


class MediaMixin:

    # -- 按键素材 ---------------------------------------------------------- #
    @staticmethod
    def _flat_rgb(img):
        """带透明通道的图先铺黑底再转 RGB（直接 convert 会露出透明区域的杂色）。"""
        if img.mode in ("RGBA", "LA", "P") and (img.mode != "P" or "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
            bg.alpha_composite(rgba)
            return bg.convert("RGB")
        return img.convert("RGB")

    def _load_image_source(self, path: str):
        try:
            img = Image.open(path)
            try:
                n = int(getattr(img, "n_frames", 1))
            except Exception:  # noqa: BLE001
                n = 1
            if n > 1:
                frames = []
                for i in range(min(n, 80)):
                    img.seek(i)
                    frame = ImageOps.fit(self._flat_rgb(img),
                                         (KEY_SIZE, KEY_SIZE), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    frame.save(buf, "JPEG", quality=70, subsampling=2)
                    dur = int(img.info.get("duration", 100) or 100)
                    frames.append((buf.getvalue(), max(40, min(1000, dur))))
                return {"type": "anim", "frames": frames, "idx": 0, "next": 0.0, "file": path}
            static = ImageOps.fit(self._flat_rgb(img), (KEY_SIZE, KEY_SIZE),
                                  Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            static.save(buf, "JPEG", quality=90, subsampling=0)
            return {"type": "image", "jpeg": buf.getvalue(), "file": path}
        except Exception as exc:  # noqa: BLE001
            log(f"图片读取失败 {path}: {exc}")
            return None

    def set_wallpaper_file(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        self.wallpaper_file = path
        self.library_collect([path])     # 换壁纸时，这张图也收进图标库的「相册」
        self._save_config()
        return self.apply_wallpaper()

    def restore_default_wallpaper(self) -> bool:
        """从校准图等临时画面回到我自己的壁纸，并强制清一次屏去掉残留。"""
        self.preview_mode = "wallpaper"
        if os.path.isfile(WALLPAPER):
            self.wallpaper_file = WALLPAPER
        self._save_config()
        self.device.clear_keys()
        self.device.refresh()
        return self.apply_wallpaper()

    def set_key_image(self, key: int, path: str) -> bool:
        if not (1 <= key <= KEY_COUNT) or not os.path.isfile(path):
            return False
        data = self._load_image_source(path)
        if data is None:
            return False
        self.key_overrides[key] = data
        self._save_config()
        self._apply_key(key)
        self.device.refresh()
        return True

    def clear_key(self, key: int) -> bool:
        if key not in self.key_overrides:
            return False
        self.key_overrides.pop(key, None)
        self._save_config()
        # 以前这里会整屏铺壁纸（像进了待机），只重画这一个键就够了
        if self.device.connected and not self.in_standby:
            self._gif_update_key(key)
            self.device.send_image(key, self._render_key(key))
            self.device.refresh()
        return True

    def _apply_key(self, key: int) -> None:
        self._gif_update_key(key)
        if not self.key_overrides.get(key):
            return
        self.device.send_image(key, self._render_key(key))

    def _keepalive_loop(self) -> None:
        """低频巡检（30 秒）。

        注意：这里**不再**周期性发唤醒包。实测每 30 秒 call 一次 wakeup_screen，
        设备过一阵会进入「灯亮着但画面不再更新」的状态（切场景不同步）。
        唤醒只在真正需要时做一次（启动、按亮/灭、长时间没画过）。
        """
        while True:
            time.sleep(30)
            now = time.time()
            gap = now - getattr(self, "_last_tick", now)
            self._last_tick = now
            try:
                # 这一轮本该只睡 30 秒：实际过去很久，说明电脑刚休眠过。
                # 休眠后 SDK 那条连接常常变成「能发不能收」，灯还亮着但按键没反应。
                if gap > 90:
                    log(f"系统休眠了约 {int(gap / 60)} 分钟，正在恢复按键通道")
                    threading.Thread(target=self._after_resume, daemon=True).start()
                    continue
                if not self.device.connected or self.screen_off or self.yielded:
                    continue
                # 输入通道看门狗：15 分钟一个按键都没有，重订阅一次（比整条重连温和）
                if now - self._last_input_seen > 900:
                    self.device.resubscribe_input()
                    self._last_input_seen = now
                    log("输入通道 15 分钟无事件，已重新订阅按键")
                # 很久没画过（设备可能已睡过去）：叫醒一次再补画，别连发
                if now - self._last_paint > 1800 and now - self._last_wake > 600:
                    self._last_wake = now
                    self.wake_panel()
                    log("30 分钟没画过，发一次唤醒并补画")
                    self.apply_keys("keepalive")
            except Exception:  # noqa: BLE001
                pass

    def _after_resume(self) -> None:
        """电脑从休眠里醒过来之后收拾残局。

        休眠后有两个坑：
        1. SDK 连接常常变成「能发不能收」—— 灯效照写（灯还是对的），但按键读不回来，
           界面上看着「已连接」，按了却没反应；
        2. 设备自己那块屏也睡了 —— 再写图会被丢掉，看着就是「屏幕卡在醒之前那一帧」。
        所以先探活，活着就重发 read 拉回输入通道，再把背光关一下开一下强制重新初始化面板，
        然后整版重推一次、1.5 秒后再补推一次（刚醒的头几帧容易被设备丢掉）。
        探不通（半死连接）就整条断开，交给热插拔线程重连并重推。
        """
        try:
            dev = self.device
            if not dev.connected:
                return
            probe = getattr(dev, "probe_alive", None)
            if not dev.check_present() or (probe is not None and not probe()):
                log("休眠恢复：连接没有回应，重新连接设备")
                dev.mark_lost("休眠恢复后连接无回应")
                self.broadcast({"type": "status", "connected": False})
                return
            dev.resubscribe_input()
            if not self.screen_off:
                # 背光 0 → 原亮度：把设备面板从休眠里蹬一脚，否则推过去的图不刷新
                try:
                    dev.wakeup()
                    dev.set_screen_brightness(0)
                    time.sleep(0.12)
                    dev.set_screen_brightness(self.screen_brightness)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.25)
            if self.screen_off:
                self._blanked = False
                self.blank_screen()
            elif self.in_standby:
                self.apply_standby()
            else:
                self.apply_keys()
            self._gif_start_all()
            self._frame_hash = None
            self._dirty = True
            self._last_activity = time.time()
            self._last_input_seen = time.time()
            # 补推：设备刚醒时前面的图可能被吞掉，1.5 秒后再来一遍（不动「上一次画」的时间）
            time.sleep(1.5)
            if self.device.connected and not self.screen_off:
                try:
                    if self.in_standby:
                        self.apply_standby()
                    else:
                        self.apply_keys()
                    self._gif_start_all()
                except Exception:  # noqa: BLE001
                    pass
            log("休眠恢复：按键通道已重新订阅，画面和动图已重推")
            if not (getattr(dev, "firmware", "") or "").strip():
                log("设备上报的固件号是空的：这块屏可能处于异常状态（画面卡住时用设备页的「拔插设备」）")
                self.broadcast({"type": "notice",
                                "text": "设备状态看起来不太对：屏幕要是卡住不动，到设备页点「拔插设备（软件）」。",
                                "ms": 6500})
        except Exception as exc:  # noqa: BLE001
            log(f"休眠恢复处理失败：{exc}")

    def device_instance_id(self) -> str:
        """找 Dock 的 USB 复合设备实例 ID（软件拔插要用）。"""
        try:
            out = subprocess.run(["pnputil", "/enum-devices", "/connected"],
                                 capture_output=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # pnputil 的输出会跟着系统语言变，所以不认「Instance ID:」这个标签，
            # 直接在文本里找 USB\VID_6603&PID_1012\<序列号> 这串（标签是本地化的时候也稳）
            text = out.stdout.decode("mbcs", "ignore") + out.stderr.decode("mbcs", "ignore")
            import re as _re
            m = _re.search(r"USB\\VID_6603&PID_1012\\[^\\\s\"',]+", text, _re.IGNORECASE)
            if m:
                return m.group(0)
            log(f"软件拔插：枚举设备时没看到 VID_6603（输出 {len(text)} 字节）")
        except Exception as exc:  # noqa: BLE001
            log(f"软件拔插：枚举设备失败：{exc}")
        return ""

    def usb_reset(self) -> dict:
        """软件拔插：让 Windows 重新枚举这块设备，等于把 USB 线拔了再插。

        设备固件偶尔会卡在「命令收得到、画面不刷新」的状态（睡醒后最常见），
        这种情况普通命令救不回来，只有重新枚举能好。
        """
        import ctypes  # 局部导入：只有这条救援路径用得到

        inst = self.device_instance_id()
        if not inst:
            log("软件拔插：没找到 Dock 的 USB 实例 ID（设备可能已经掉了）")
            return {"ok": False, "error": "没找到设备的 USB 实例 ID"}
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            is_admin = False
        log(f"软件拔插：{inst}（管理员={is_admin}）")
        cmd = ["pnputil", "/restart-device", inst]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=40)
            text = (out.stdout + out.stderr).decode("utf-8", "ignore").strip()
        except Exception as exc:  # noqa: BLE001
            log(f"软件拔插失败：{exc}")
            return {"ok": False, "error": f"执行失败：{exc}"}
        if out.returncode != 0:
            log(f"软件拔插：pnputil 返回 {out.returncode}：{text[:300]}")
            low = text.lower()
            if "denied" in low or "拒绝" in text or "privilege" in low or "权限" in text:
                # 不是管理员：弹一次 UAC，用户点「是」之后由系统重启设备
                log("软件拔插：权限不够，弹一次 UAC 让用户确认")
                try:
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Start-Process pnputil -ArgumentList "
                                    f"'-restart-device' '{inst}' -Verb RunAs -Wait"],
                                   capture_output=True, timeout=90)
                except Exception as exc:  # noqa: BLE001
                    log(f"软件拔插：提权也没成功：{exc}")
                    return {"ok": False, "error": f"需要管理员权限才能拔插设备：{exc}"}
                text = "已通过管理员权限重启设备"
            else:
                return {"ok": False, "error": text or "重启设备失败"}
        log(f"软件拔插：{inst} 已重启，等设备重新枚举")
        # 等它重新接上（热插拔线程会自动接管并重画）
        for _ in range(24):
            time.sleep(0.5)
            if self.device.connected:
                break
        return {"ok": True, "instance": inst, "message": text, "connected": self.device.connected}

    def hard_recover(self, hard: bool = False) -> dict:
        """画面卡住（推过去的图不刷新，但灯效、按键都正常）时的重手恢复。

        按顺序：停动图 → 清掉所有键图（clearAllIcon）→ 背光 0 → 背光还原 →
        整版重推 + 动图重启。还不行就 hard=True：整条 SDK 连接断开重连
        （重连会重新枚举 HID，等于软拔插一次 USB）。
        """
        dev = self.device
        steps: list[str] = []
        if dev is None or not getattr(dev, "connected", False):
            return {"ok": False, "error": "设备没连上", "steps": steps}
        try:
            self._gif_stop_all()
            steps.append("停动图")
            dev.set_screen_brightness(0)
            steps.append("背光 0")
            time.sleep(0.2)
            dev.clear_keys()
            steps.append("清屏")
            time.sleep(0.2)
            dev.set_screen_brightness(self.screen_brightness)
            steps.append(f"背光 {self.screen_brightness}")
            time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            steps.append(f"前置出错：{exc}")
        if hard:
            try:
                dev.disconnect()
                steps.append("断开连接")
                time.sleep(1.2)
                if dev.connect():
                    steps.append("重新连接成功")
                    self._restore_all()
                else:
                    steps.append("重新连接失败")
            except Exception as exc:  # noqa: BLE001
                steps.append(f"重连出错：{exc}")
            log(f"强制恢复画面：{' / '.join(steps)}")
            return {"ok": True, "steps": steps}
        try:
            if self.screen_off:
                self._blanked = False
                self.blank_screen()
                steps.append("重新息屏")
            elif self.in_standby:
                self.apply_standby()
                steps.append("重推待机画面")
            else:
                self.apply_keys()
                self._gif_start_all()
                steps.append("重推 15 个键")
            self._frame_hash = None
            self._dirty = True
        except Exception as exc:  # noqa: BLE001
            steps.append(f"重推出错：{exc}")
        log(f"恢复画面：{' / '.join(steps)}")
        return {"ok": True, "steps": steps}

    def _animation_loop(self) -> None:
        """按键动图：主机侧逐帧推送（设备本身不循环播放）。"""
        last_refresh = 0.0
        while True:
            time.sleep(0.012)
            if (not self.device.connected or not self.key_overrides
                    or self.in_standby or self.lighting_only
                    or self.quiet or self.writes_paused or self.yielded
                    or self.screen_off
                    or time.time() < getattr(self, "_anim_pause_until", 0)):
                continue
            now = time.time()
            pushed = 0
            # SDK 原生播放的键不归这里管
            host_keys = [k for k, it in list(self.key_overrides.items())
                         if it.get("type") == "anim" and it.get("frames") and k not in self._gif_active
                         and not self._plugin_vis(k)]
            if not host_keys:
                continue
            # 能用 SDK 原生播放就别走「主机推图」：推图走的是和灯带同一条通道，
            # 18 张/秒往里塞会把灯珠的帧挤断（灯效看起来一顿一顿）。
            # 原生播放被停掉后（待机/壁纸/息屏/整屏重画都会 stop），这里负责把它恢复回来。
            if self._gif_allowed():
                now_g = time.time()
                if now_g - self._gif_retry_at > 2.0:
                    self._gif_retry_at = now_g
                    self._gif_start_all()
                if self._gif_active:
                    continue
            # SDK 写图没有回执、也没有背压：所有动图合计每秒最多 20 帧，
            # 超出就丢帧而不是排队，否则积压会拖慢之后的灯光 / 换页 / 切场景
            per_key_fps = max(2.0, 20.0 / len(host_keys))
            min_gap = 1.0 / per_key_fps
            for key in host_keys:
                item = self.key_overrides.get(key)
                if not item or now < item.get("next", 0):
                    continue
                item["idx"] = (item["idx"] + 1) % len(item["frames"])
                jpeg, dur = item["frames"][item["idx"]]
                item["next"] = now + max(min_gap, dur / 1000.0)
                self.device.send_image(key, self._styled_frame(key, jpeg))
                pushed += 1
            if pushed and now - last_refresh >= 0.05:
                self.device.refresh()
                last_refresh = now
                self._anim_pushes += pushed      # 诊断：主机侧推了多少张动图（会占通道）

    def _gap(self, gap: int | None = None) -> int:
        """键间缝隙；相册场景可以有自己的缝隙，传 gap 就按它算（None = 全局设置）。"""
        g = self.wallpaper_gap if gap is None else gap
        return max(0, min(64, int(g)))

    def _canvas_size(self, gap: int | None = None) -> tuple[int, int]:
        gap = self._gap(gap)
        cols, rows = (GRID_ROWS, GRID_COLS) if self.rotation % 180 else (GRID_COLS, GRID_ROWS)
        return (
            cols * KEY_SIZE + (cols - 1) * gap,
            rows * KEY_SIZE + (rows - 1) * gap,
        )

    def _cell_slot(self, col: int, row: int) -> tuple[int, int]:
        """把「默认朝向」的键位换算成「旋转后」画面里的位置。"""
        rot = self.rotation % 360
        if rot == 90:
            return GRID_ROWS - 1 - row, col
        if rot == 180:
            return GRID_COLS - 1 - col, GRID_ROWS - 1 - row
        if rot == 270:
            return row, GRID_COLS - 1 - col
        return col, row

    def _calibration_source(self) -> Image.Image:
        """校准图案：斜线连续、圆不扁 = 间隙补偿正确。"""
        gap = self._gap()
        w, h = self._canvas_size()
        pitch = KEY_SIZE + gap
        cols, rows = (GRID_ROWS, GRID_COLS) if self.rotation % 180 else (GRID_COLS, GRID_ROWS)
        img = Image.new("RGB", (w, h), (14, 17, 23))
        d = ImageDraw.Draw(img)
        for c in range(cols):
            for r in range(rows):
                x, y = c * pitch, r * pitch
                d.rectangle([x + 1, y + 1, x + KEY_SIZE - 2, y + KEY_SIZE - 2],
                            outline=(46, 56, 72), width=1)
        d.line([(0, 0), (w, h)], fill=(80, 200, 255), width=2)
        d.line([(w, 0), (0, h)], fill=(255, 120, 90), width=2)
        radius = min(w, h) * 0.36
        cx, cy = w / 2, h / 2
        d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                  outline=(255, 255, 255), width=2)
        d.line([(0, cy), (w, cy)], fill=(120, 255, 170), width=1)
        return img

    def _canvas(self, mode: str | None = None) -> Image.Image | None:
        """当前要显示的画面，尺寸 = 含缝隙的总画布。支持填充方式 / 缩放 / 位移。"""
        w, h = self._canvas_size()
        mode = mode or self.preview_mode
        if mode == "album":
            return self._album_canvas(self.album_index)
        if mode == "calibration":
            return self._calibration_source()
        try:
            src = Image.open(self.wallpaper_file).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            log(f"壁纸处理失败：{exc}")
            return None
        return self._fit_canvas(src, w, h)

    def _fit_canvas(self, src: Image.Image, w: int, h: int, zoom_offset: bool = True,
                    fit: str | None = None) -> Image.Image:
        """壁纸和相册共用的铺放：填充 / 完整 / 拉伸 + 缩放（壁纸还有拖动位移）+ 待机亮度。"""
        zoom = max(0.2, min(4.0, float(self.wallpaper_zoom)))
        tw, th = max(1, int(w * zoom)), max(1, int(h * zoom))
        fit = fit or self.wallpaper_fit
        if fit == "fill":
            scaled = src.resize((tw, th), Image.Resampling.LANCZOS)
        elif fit == "contain":
            base = ImageOps.pad(src, (w, h), color=(0, 0, 0),
                                method=Image.Resampling.LANCZOS)
            scaled = base.resize((tw, th), Image.Resampling.LANCZOS)
        else:
            scaled = ImageOps.fit(src, (tw, th), Image.Resampling.LANCZOS)

        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        x = (w - scaled.width) // 2 + (int(self.wallpaper_ox) if zoom_offset else 0)
        y = (h - scaled.height) // 2 + (int(self.wallpaper_oy) if zoom_offset else 0)
        canvas.paste(scaled, (x, y))
        # 待机画面单独调暗（不影响按键画面的屏幕亮度）
        dim = max(0, min(100, int(self.standby_brightness)))
        if dim < 100:
            k = dim / 100.0
            canvas = canvas.point(lambda v: int(v * k))
        return canvas

    def _canvas_key(self):
        try:
            mtime = os.path.getmtime(self.wallpaper_file or "")
        except OSError:
            mtime = 0
        return (
            self.wallpaper_file, mtime, self.wallpaper_gap, self.wallpaper_zoom,
            self.wallpaper_ox, self.wallpaper_oy, self.wallpaper_fit,
            self.wallpaper_flip, self.rotation, "calibration" if self.preview_mode == "calibration" else "wp",
            self.standby_brightness,
        )

    def canvas_png(self) -> bytes | None:
        """壁纸卡片的预览：相册播放时也显示壁纸本身。"""
        key = self._canvas_key()
        cached = getattr(self, "_canvas_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        canvas = self._canvas("calibration" if self.preview_mode == "calibration" else "wallpaper")
        if canvas is None:
            return None
        buf = io.BytesIO()
        canvas.save(buf, "PNG", compress_level=1)
        data = buf.getvalue()
        self._canvas_cache = (key, data)
        return data

    def apply_wallpaper(self) -> bool:
        self.preview_mode = "wallpaper"
        self.in_standby = True
        return self._push_canvas("壁纸")

    def apply_calibration(self) -> bool:
        self.preview_mode = "calibration"
        self._calib_until = time.time() + 60
        threading.Timer(20.0, self._end_calibration).start()
        return self._push_canvas("校准图")

    def _end_calibration(self) -> None:
        """校准图只是临时画面，到点无条件回到壁纸。"""
        if self.preview_mode == "calibration":
            self.preview_mode = "wallpaper"
            self.apply_wallpaper()

    def _push_canvas(self, note: str) -> bool:
        """把画布按当前 gap 切块，逐键写入设备。"""
        if not self.device.connected or self.writes_paused or self.quiet or self.lighting_only or self.yielded:
            return False
        if self.screen_off:
            return False
        try:
            self.device.set_screen_brightness(self.screen_brightness)
            time.sleep(0.06)
        except Exception:  # noqa: BLE001
            pass
        canvas = self._canvas()
        if canvas is None:
            return False
        self._gif_stop_all()
        gap = self._gap()
        pitch = KEY_SIZE + gap
        ok = True
        self._pushing = True          # 图片传输期间暂停灯效写入，避免数据包互相穿插
        try:
            self.device.clear_queue()
            self._clear_screen()
            for key in range(1, KEY_COUNT + 1):
                col = (key - 1) % GRID_COLS
                row_from_bottom = (key - 1) // GRID_COLS
                row_top = row_from_bottom if self.wallpaper_flip else (GRID_ROWS - 1 - row_from_bottom)
                slot_col, slot_row = self._cell_slot(col, row_top)
                left = slot_col * pitch
                top = slot_row * pitch
                tile = canvas.crop((left, top, left + KEY_SIZE, top + KEY_SIZE))
                buf = io.BytesIO()           # 方向由设备层统一转（DeviceWS.image_rotation）
                tile.save(buf, "JPEG", quality=88, subsampling=0)
                if not self.device.send_image(key, buf.getvalue()):
                    ok = False
            self.device.refresh()
        finally:
            self._pushing = False
        self._last_paint = time.time()
        w, h = canvas.size
        log(f"{note}已铺满 {KEY_COUNT} 键（间隙补偿 {gap}px，画布 {w}×{h}，旋转 {self.rotation}°）"
            + ("" if ok else "  ⚠ 有按键写入失败"))
        return ok

    # -- 旋转 ---------------------------------------------------------------- #
    def set_rotation(self, value: int) -> bool:
        rot = value % 360
        self.rotation = rot if rot in (0, 90, 180, 270) else 0
        self.device.image_rotation = self.rotation
        self._num_cache.clear()
        self._gif_active = {}
        self._save_config()
        if self.album_active():
            return self._push_album()
        if self.preview_mode == "calibration":
            return self.apply_calibration()
        if self.in_standby:
            return self.apply_standby()
        threading.Thread(target=self.apply_keys, daemon=True).start()
        return True

    # -- 相册 ------------------------------------------------------------------ #
    def album_active(self) -> bool:
        return self.in_standby and self.preview_mode == "album"

    # 相册数据源：在放相册场景时用场景自己的，否则用待机相册（全局设置）
    def _alb_items(self) -> list:
        if self.album_scene_name:
            return self._album_scene.get("items") or []
        return self.album_items

    def _alb_index(self) -> int:
        if self.album_scene_name:
            return int(self._album_scene.get("index") or 0)
        return self.album_index

    def _alb_slideshow(self) -> bool:
        if self.album_scene_name:
            return bool(self._album_scene.get("slideshow", True))
        return self.album_slideshow

    def _alb_interval(self) -> float:
        if self.album_scene_name:
            return float(self._album_scene.get("interval") or 8)
        return self.album_interval

    def _alb_transition(self) -> str:
        if self.album_scene_name:
            tr = str(self._album_scene.get("transition") or "fade")
            return tr if tr in ALBUM_TRANSITIONS else "fade"
        return self.album_transition

    def _alb_fit(self) -> str:
        if self.album_scene_name:
            fit = str(self._album_scene.get("fit") or "cover")
            return fit if fit in ("cover", "contain", "fill") else "cover"
        return self.album_fit

    def _alb_gap(self) -> int | None:
        """相册场景自己的键间缝隙；没单独设过就返回 None（跟全局设置走）。"""
        if self.album_scene_name:
            g = self._album_scene.get("gap")
            return None if g is None else self._gap(g)
        return None

    def _alb_gap_eff(self) -> int:
        g = self._alb_gap()
        return self._gap() if g is None else g

    def _alb_set_index(self, index: int) -> None:
        n = len(self._alb_items())
        if n <= 0:
            return
        index = int(index) % n
        if self.album_scene_name:
            self._album_scene["index"] = index
        else:
            self.album_index = index

    # -- 相册场景：一个场景 = 一组图片 + 过渡设置，存在场景文件里 --------------- #
    def _album_scene_data(self, name: str) -> dict | None:
        """读相册场景的 album 段；不是相册场景返回 None。"""
        if not name or name == self.custom_scene_name:
            return None
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return None
        alb = data.get("album")
        return alb if isinstance(alb, dict) else {}

    def is_album_scene(self, name: str) -> bool:
        return self._album_scene_data(name) is not None

    def _album_scene_items(self, name: str) -> list:
        alb = self._album_scene_data(name) or {}
        return [str(n) for n in (alb.get("items") or []) if str(n)]

    def _album_scene_write(self, name: str, alb: dict) -> bool:
        """把 album 段写回场景文件（顺手补齐 pages，方便导出 / 复制）。"""
        path = self._scene_path(name)
        data = self._scene_file_data(path) or {}
        if not isinstance(data.get("pages"), list):
            data["pages"] = [{"name": "相册", "images": {}, "actions": {}}]
            data.setdefault("page", 0)
        data["sceneType"] = "album"
        data["album"] = alb
        try:
            os.makedirs(SCENE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log(f"保存相册场景失败：{exc}")
            return False
        return True

    def _album_scene_edit(self, name: str, fn) -> bool:
        """改相册场景的 album 段：fn(alb) 返回 False 表示没改动。"""
        alb = self._album_scene_data(name)
        if alb is None:
            log(f"「{name}」不是相册场景，改不了图片")
            return False
        alb = dict(alb)
        if not fn(alb):
            return False
        alb["items"] = [str(n) for n in (alb.get("items") or []) if str(n)]
        if not self._album_scene_write(name, alb):
            return False
        if self.album_scene_name == name:      # 正在放这个场景，内存里也同步
            alb["index"] = int(alb.get("index") or 0)
            self._album_scene = alb
        self.broadcast({"type": "album", **self.album_state()})
        return True

    ALBUM_NAV = {16: "上一页", 17: "主页", 18: "下一页"}

    def _album_nav_page(self, name: str) -> dict | None:
        """相册场景文件里的第一页（导航键的动作/手势都存在这里，不跟当前页走）。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return None
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        if not isinstance(pages[0], dict):
            return None
        return pages[0]

    def _album_nav_write(self, name: str, key: int, spec: dict | None, gesture: str = "") -> bool:
        """把相册场景里导航键的动作写回场景文件：长按 / 双击进 gestures，短按进 actions。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return False
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            pages = [{"name": name, "images": {}, "actions": {}}]
            data["pages"] = pages
            data["page"] = 0
        page = pages[0]
        if gesture in ("long", "double"):
            gs = page.get("gestures")
            if not isinstance(gs, dict):
                gs = {}
                page["gestures"] = gs
            one = gs.get(str(key))
            if not isinstance(one, dict):
                one = {}
            if spec:
                one[gesture] = spec
            else:
                one.pop(gesture, None)
            if one:
                gs[str(key)] = one
            else:
                gs.pop(str(key), None)
            if not gs:
                page.pop("gestures", None)
        else:
            acts = page.get("actions")
            if not isinstance(acts, dict):
                acts = {}
                page["actions"] = acts
            if spec:
                acts[str(key)] = spec
            else:
                acts.pop(str(key), None)
        try:
            path = self._scene_path(name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError as exc:
            log(f"写入相册场景导航键失败：{exc}")
            return False

    def _album_nav_load(self, name: str) -> None:
        """进相册场景时，把场景文件里导航键的动作/手势装进内存，编辑区就能看到。"""
        page = self._album_nav_page(name)
        if not page:
            return
        acts = page.get("actions") if isinstance(page.get("actions"), dict) else {}
        for k in self.ALBUM_NAV:
            spec = acts.get(str(k))
            if isinstance(spec, dict):
                self.key_actions[k] = spec
        gs = page.get("gestures") if isinstance(page.get("gestures"), dict) else {}
        for k, one in gs.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            if key in self.ALBUM_NAV and isinstance(one, dict) and one:
                self.key_gestures[key] = one

    def _album_nav_step(self, key: int) -> None:
        """相册场景里导航键的短按：◀ 上一张 / ⌂ 第一张 / ▶ 下一张。"""
        if key == 16:
            self.album_goto(-1)
        elif key == 18:
            self.album_goto(1)
        elif key == 17:
            self.go_home()

    def _ensure_album_nav(self, name: str) -> None:
        """相册场景的三个导航键默认是 上页 / 主页 / 下页：补进场景文件，编辑区里能看到。"""
        data = self._scene_file_data(self._scene_path(name))
        if not data or data.get("sceneType") != "album":
            return
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            return
        acts = pages[0].get("actions")
        if not isinstance(acts, dict):
            acts = {}
            pages[0]["actions"] = acts
        changed = False
        for k, nav in self.ALBUM_NAV.items():
            if str(k) not in acts:
                acts[str(k)] = {"type": "nav", "name": nav}
                changed = True
        if not changed:
            return
        try:
            tmp = self._scene_path(name) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._scene_path(name))
        except OSError as exc:
            log(f"写入相册场景默认导航键失败：{exc}")

    def _album_file_for(self, items: list, index: int) -> str | None:
        if not items:
            return None
        name = os.path.basename(items[index % len(items)])
        path = os.path.join(UPLOAD_DIR, name)
        return path if os.path.isfile(path) else None

    def album_state(self, active: bool | None = None, scene: str = "") -> dict:
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = [str(n) for n in (alb.get("items") or []) if str(n)]
            fit = str(alb.get("fit") or "cover")
            gap = alb.get("gap")
            playing = self.album_active() and self.album_scene_name == scene
            index = self._alb_index() if playing else int(alb.get("index") or 0)
            return {"items": items, "index": index, "active": bool(playing),
                    "slideshow": bool(alb.get("slideshow", True)),
                    "interval": max(2, min(600, int(alb.get("interval") or 8))),
                    "transition": str(alb.get("transition") or "fade"),
                    "fit": fit if fit in ("cover", "contain", "fill") else "cover",
                    "gap": self._gap() if gap is None else self._gap(gap),
                    "gapOwn": gap is not None,
                    "source": "album-scene", "scene": scene}
        return {"items": list(self._alb_items()), "index": self._alb_index(),
                "active": self.album_active() if active is None else active,
                "slideshow": self._alb_slideshow(), "interval": int(self._alb_interval()),
                "transition": self._alb_transition(), "fit": self._alb_fit(),
                "gap": self._alb_gap_eff(), "gapOwn": self._alb_gap() is not None,
                "standby": {"items": len(self.album_items),
                            "index": int(self.album_index),
                            "slideshow": bool(self.album_slideshow),
                            "interval": max(2, min(600, int(self.album_interval)))},
                "source": self.standby_source, "scene": self.album_scene_name}

    def _album_file(self, index: int) -> str | None:
        return self._album_file_for(self._alb_items(), index)

    def _album_copy_in(self, files: list) -> list:
        """把拖进来的图片收进上传目录，返回文件名列表（相册场景和待机相册共用）。"""
        names = []
        self._album_from_library = set()     # 这一次有几个是从「相册」分类来的
        for f in files[:200]:
            p = asset_path(f)
            if not p or not os.path.isfile(p):
                continue
            # 允许上传目录里的图，也允许图标库「相册」分类里的图（拖过去的时候）
            if not (_inside(p, UPLOAD_DIR) or _inside(p, LIBRARY_DIR)):
                continue
            try:
                with Image.open(p) as im:
                    im.verify()
            except Exception:  # noqa: BLE001
                continue
            if _inside(p, UPLOAD_DIR):
                name = os.path.relpath(p, UPLOAD_DIR).replace("\\", "/")
                if "/" not in name:
                    names.append(name)
                    continue
            # 来自上传目录的子文件夹，或者来自「相册」分类：复制一份到上传目录根下
            dst = os.path.join(UPLOAD_DIR, f"album_{int(time.time() * 1000)}_{len(names)}{os.path.splitext(p)[1]}")
            shutil.copyfile(p, dst)
            names.append(os.path.basename(dst))
            if _inside(p, LIBRARY_DIR):
                self._album_from_library.add(os.path.basename(dst))
        return names

    # -- 图标库的「相册」分类 -------------------------------------------- #
    def library_items(self) -> list:
        """「相册」分类里的图片（按时间倒序，最新收进来的排最前）。"""
        out = []
        try:
            os.makedirs(LIBRARY_DIR, exist_ok=True)
            for fn in os.listdir(LIBRARY_DIR):
                if not fn.lower().endswith(IMAGE_EXTS):
                    continue
                fp = os.path.join(LIBRARY_DIR, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append({"name": fn, "size": st.st_size, "mtime": int(st.st_mtime)})
        except Exception:  # noqa: BLE001
            return out
        out.sort(key=lambda it: (-it["mtime"], it["name"]))
        return out

    def library_path(self, name: str) -> str | None:
        safe = os.path.basename(name or "")
        if not safe:
            return None
        fp = os.path.join(LIBRARY_DIR, safe)
        return fp if os.path.isfile(fp) else None

    def library_thumb(self, name: str, size: int = 168) -> str | None:
        """相册缩略图：侧栏只加载小图，避免十几张十几 MB 的原图把界面拖卡。"""
        src = self.library_path(name)
        if not src:
            return None
        try:
            mtime = int(os.path.getmtime(src))
        except OSError:
            mtime = 0
        stem = os.path.splitext(os.path.basename(src))[0]
        os.makedirs(LIB_THUMB_DIR, exist_ok=True)
        dst = os.path.join(LIB_THUMB_DIR, f"{stem}-{mtime}-{size}.jpg")
        if os.path.isfile(dst):
            return dst
        try:
            with Image.open(src) as im:
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((size, size), Image.LANCZOS)
                im.save(dst, "JPEG", quality=84)
            return dst
        except Exception as exc:  # noqa: BLE001
            log(f"生成相册缩略图失败：{exc}")
            return None

    def library_collect(self, paths) -> int:
        """把图片收进「相册」分类：复制一份到 LIBRARY_DIR，重名自动加序号。"""
        n = 0
        try:
            os.makedirs(LIBRARY_DIR, exist_ok=True)
        except Exception:  # noqa: BLE001
            return 0
        for src in paths or []:
            try:
                if not src or not os.path.isfile(src):
                    continue
                if not src.lower().endswith(IMAGE_EXTS):
                    continue
                if os.path.normpath(os.path.dirname(os.path.abspath(src))) == os.path.normpath(LIBRARY_DIR):
                    continue                # 已经在相册里了
                base = os.path.basename(src)
                stem, ext = os.path.splitext(base)
                dst = os.path.join(LIBRARY_DIR, base)
                try:
                    if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
                        continue            # 同一个文件已经收过了
                except OSError:
                    pass
                i = 2
                while os.path.exists(dst):
                    dst = os.path.join(LIBRARY_DIR, f"{stem} {i}{ext}")
                    i += 1
                shutil.copy2(src, dst)
                n += 1
            except Exception:  # noqa: BLE001
                continue
        if n:
            self.broadcast({"type": "library", "items": self.library_items()})
            log(f"相册收进 {n} 张图片")
        return n

    def library_add(self, files) -> dict:
        paths = []
        for f in (files or [])[:200]:
            p = asset_path(f)
            if p and os.path.isfile(p):
                paths.append(p)
        n = self.library_collect(paths)
        return {"ok": bool(n), "added": n, "items": self.library_items()}

    def pick_program(self) -> str:
        """弹一个「选择程序」窗口（服务端在本机弹，浏览器拿不到真实路径）。"""
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$o=New-Object System.Windows.Forms.Form;"
            "$o.TopMost=$true; $o.ShowInTaskbar=$false; $o.FormBorderStyle='None';"
            "$o.StartPosition='CenterScreen'; $o.Size=New-Object System.Drawing.Size(1,1);"
            "$o.Show();"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='选择要绑定的程序（.exe / 快捷方式）';"
            "$d.Filter='程序 (*.exe;*.lnk;*.bat;*.cmd)|*.exe;*.lnk;*.bat;*.cmd|所有文件 (*.*)|*.*';"
            "$d.CheckFileExists=$true;"
            "if($d.ShowDialog($o) -eq 'OK'){ $d.FileName };"
            "$o.Close()"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                                 capture_output=True, text=True, timeout=300)
            return (out.stdout or "").strip()
        except Exception as exc:  # noqa: BLE001
            log(f"打开选择程序窗口失败：{exc}")
            return ""

    def _lnk_target_bytes(self, data: bytes) -> str:
        """从 .lnk 的字节里解析目标程序路径（先按结构读，再退化成字符串扫描）。"""
        if not data or len(data) < 0x4C:
            return ""
        import struct
        cands: list[str] = []
        flags = 0
        try:
            flags = struct.unpack_from("<I", data, 0x14)[0]
            pos = 0x4C
            if flags & 0x1:                         # HasLinkTargetIDList
                idl = struct.unpack_from("<H", data, pos)[0]
                pos += 2 + idl
            if flags & 0x2 and pos + 0x1C <= len(data):   # HasLinkInfo
                _li_size, li_hdr, li_flags = struct.unpack_from("<III", data, pos)
                if li_flags & 0x1:                  # VolumeIDAndLocalBasePath
                    base_off = struct.unpack_from("<I", data, pos + 16)[0]
                    start = pos + base_off
                    end = data.find(b"\x00", start)
                    if 0 < start < len(data) and end > start:
                        try:
                            cands.append(data[start:end].decode("mbcs", "ignore"))
                        except Exception:             # noqa: BLE001
                            pass
                    if li_hdr >= 0x24 and pos + 0x20 <= len(data):
                        uni_off = struct.unpack_from("<I", data, pos + 28)[0]
                        if uni_off:
                            start = pos + uni_off
                            end = start
                            while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
                                end += 2
                            if end > start:
                                cands.append(data[start:end].decode("utf-16-le", "ignore"))
        except Exception:                             # noqa: BLE001
            pass
        # 兜底：有些安装器写的快捷方式不规范，直接扫「X:\...\xxx.exe」这种串
        import re as _re
        pat = _re.compile(r"[A-Za-z]:\\[^\x00:*?\"<>|\r\n]{1,240}?\.(?:exe|com|bat|cmd)", _re.I)
        for enc in ("utf-16-le", "mbcs"):
            try:
                blob = data.decode(enc, "ignore")
            except Exception:                         # noqa: BLE001
                continue
            cands.extend(m.group(0) for m in pat.finditer(blob))
        for c in cands:
            p = os.path.expandvars(c.strip().strip('"'))
            if p and os.path.isfile(p):
                return p
        return ""

    def _program_roots(self) -> list[str]:
        roots = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "APPDATA"):
            p = os.environ.get(env)
            if p:
                roots.append(p)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(os.path.join(local, "Programs"))
        sysroot = os.environ.get("SystemRoot")
        if sysroot:
            roots.extend([os.path.join(sysroot, "System32"), os.path.join(sysroot, "SysWOW64")])
        home = os.path.expanduser("~")
        for name in ("Desktop", "Downloads", "OneDrive\\Desktop", "OneDrive\\桌面", "桌面"):
            roots.append(os.path.join(home, name))
        out = []
        for r in roots:
            if os.path.isdir(r) and r not in out:
                out.append(r)
        return out

    def _find_program(self, name: str, size: int = 0) -> str:
        """在常见目录里按「文件名（+ 大小）」找程序，找不到再让用户选。"""
        want = os.path.basename(name or "").lower()
        if not want:
            return ""
        deadline = time.time() + 6.0
        skip = {"$recycle.bin", "system volume information", "winsxs", "node_modules",
                "__pycache__", ".git", "temp", "tmp", "cache", "cache2", "crashdumps",
                "installer", "packages"}
        hits: list[str] = []
        for root in self._program_roots():
            if len(hits) >= 8 or time.time() > deadline:
                break
            for dirpath, dirnames, filenames in os.walk(root):
                if len(hits) >= 8 or time.time() > deadline:
                    break
                dirnames[:] = [d for d in dirnames if d.lower() not in skip]
                for fn in filenames:
                    if fn.lower() != want:
                        continue
                    fp = os.path.join(dirpath, fn)
                    try:
                        if size and os.path.getsize(fp) != size:
                            continue
                    except OSError:
                        continue
                    hits.append(fp)
                    if len(hits) >= 8:
                        break
        if not hits:
            return ""
        hits.sort(key=lambda p: (0 if "\\program files" in p.lower() else 1, len(p)))
        return hits[0]

    def resolve_program(self, name: str, size: int = 0, data: bytes | None = None,
                        path: str = "") -> dict:
        """尽量把拖进来的程序 / 快捷方式解析成真实路径，省掉让用户再选一次。"""
        hint = (path or "").strip().strip('"')
        if hint and os.path.isfile(hint):
            return {"ok": True, "path": hint, "how": "drop"}
        base = os.path.basename((name or "").strip())
        is_lnk = base.lower().endswith(".lnk")
        if data and is_lnk:
            tgt = self._lnk_target_bytes(data)
            if tgt:
                return {"ok": True, "path": tgt, "how": "shortcut"}
        if is_lnk:
            for root in self._program_roots():
                cand = os.path.join(root, base)
                if not os.path.isfile(cand):
                    continue
                try:
                    with open(cand, "rb") as fh:
                        tgt = self._lnk_target_bytes(fh.read())
                except OSError:
                    tgt = ""
                if not tgt:
                    tgt = self._lnk_target(cand)      # 退回到 PowerShell 解析
                if tgt and os.path.isfile(tgt):
                    return {"ok": True, "path": tgt, "how": "shortcut"}
        if not is_lnk:
            hit = self._find_program(base, size)
            if hit:
                return {"ok": True, "path": hit, "how": "search"}
        return {"ok": False, "error": "没找到这个程序的位置"}

    def program_icon(self, path: str, size: int = 64) -> str | None:
        """取程序 / 快捷方式的图标，存成 PNG 缓存。"""
        # 网络路径（\\主机\共享）和设备路径（\\?\、\\.\）一律不碰：
        # 访问它们会让 Windows 自动把本机账号的 NTLM 凭据发给对方
        if not path or path.replace("/", "\\").startswith("\\\\") or not os.path.isfile(path):
            return None
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:  # noqa: BLE001
            return None
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            mtime = 0
        stem = "".join(ch for ch in os.path.basename(path) if ch.isalnum() or ch in ".-_")[:40]
        os.makedirs(LIB_THUMB_DIR, exist_ok=True)
        dst = os.path.join(LIB_THUMB_DIR, f"icon-{stem}-{mtime}-{size}.png")
        if os.path.isfile(dst):
            return dst

        class SHFILEINFO(ctypes.Structure):
            _fields_ = [("hIcon", ctypes.c_void_p), ("iIcon", ctypes.c_int),
                        ("dwAttributes", wintypes.DWORD),
                        ("szDisplayName", wintypes.WCHAR * 260),
                        ("szTypeName", wintypes.WCHAR * 80)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        hicon = None
        try:
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            info = SHFILEINFO()
            SHGFI_ICON, SHGFI_LARGEICON = 0x100, 0x0
            if not shell32.SHGetFileInfoW(ctypes.c_wchar_p(path), 0, ctypes.byref(info),
                                          ctypes.sizeof(info), SHGFI_ICON | SHGFI_LARGEICON):
                return None
            hicon = info.hIcon
            if not hicon:
                return None
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = size
            bmi.bmiHeader.biHeight = -size
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            hdc = user32.GetDC(None)
            memdc = gdi32.CreateCompatibleDC(hdc)
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            old = gdi32.SelectObject(memdc, hbmp)
            ctypes.memset(bits, 0, size * size * 4)
            user32.DrawIconEx(memdc, 0, 0, hicon, size, size, 0, None, 3)
            raw = ctypes.string_at(bits, size * size * 4)
            gdi32.SelectObject(memdc, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(memdc)
            user32.ReleaseDC(None, hdc)
            im = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
            if im.getchannel("A").getextrema()[1] == 0:
                im = im.convert("RGB").convert("RGBA")
            bg = Image.new("RGBA", (size, size), (0, 0, 0, 255))
            bg.alpha_composite(im)
            bg.convert("RGB").save(dst, "PNG")
            return dst
        except Exception as exc:  # noqa: BLE001
            log(f"取程序图标失败：{exc}")
            return None
        finally:
            try:
                if hicon:
                    ctypes.windll.user32.DestroyIcon(hicon)
            except Exception:  # noqa: BLE001
                pass

    def bind_program(self, key: int, path: str) -> dict:
        """把程序 / 快捷方式绑到按键上，并尽量用它的图标当按键画面。"""
        p = (path or "").strip().strip('"')
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "找不到这个文件"}
        if not self.set_key_action(key, {"type": "launch", "path": p}):
            return {"ok": False, "error": "绑定失败"}
        icon = self.program_icon(p)
        if icon:
            self.set_key_image(key, icon)
        self._save_config()
        return {"ok": True, "name": os.path.basename(p), "icon": bool(icon)}

    def library_remove(self, name: str) -> bool:
        fp = self.library_path(name)
        if not fp:
            return False
        try:
            os.remove(fp)
        except OSError:
            return False
        self.broadcast({"type": "library", "items": self.library_items()})
        return True

    def album_add(self, files: list, scene: str = "") -> bool:
        names = self._album_copy_in(files)
        if not names:
            return False
        # 加到待机相册 / 相册场景的图，也顺手收进图标库的「相册」里。
        # 从「相册」分类拖过去的不用再收一次（否则每拖一次就多一份重复图）。
        skip = getattr(self, "_album_from_library", set())
        rest = [os.path.join(UPLOAD_DIR, n) for n in names if n not in skip]
        if rest:
            self.library_collect(rest)
        if scene:
            def add(alb: dict) -> bool:
                alb["items"] = list(alb.get("items") or []) + names
                return True
            return self._album_scene_edit(scene, add)
        self.album_items.extend(names)
        self._save_config()
        return True

    def album_remove(self, index: int, scene: str = "") -> bool:
        if scene:
            def drop(alb: dict) -> bool:
                items = list(alb.get("items") or [])
                if not (0 <= index < len(items)):
                    return False
                items.pop(index)
                alb["items"] = items
                if int(alb.get("index") or 0) >= len(items):
                    alb["index"] = 0
                return True
            ok = self._album_scene_edit(scene, drop)
            if ok and not self._album_scene_items(scene) and self.album_scene_name == scene:
                threading.Thread(target=self.album_stop, daemon=True).start()
            return ok
        if not (0 <= index < len(self.album_items)):
            return False
        self.album_items.pop(index)
        if self.album_index >= len(self.album_items):
            self.album_index = 0
        if not self.album_items and self.album_active() and not self.album_scene_name:
            threading.Thread(target=self.album_stop, daemon=True).start()
        self._save_config()
        return True

    def album_move(self, src: int, dst: int, scene: str = "") -> bool:
        if scene:
            def move(alb: dict) -> bool:
                items = list(alb.get("items") or [])
                n = len(items)
                if not (0 <= src < n and 0 <= dst < n) or src == dst:
                    return False
                items.insert(dst, items.pop(src))
                alb["items"] = items
                return True
            return self._album_scene_edit(scene, move)
        n = len(self.album_items)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return False
        self.album_items.insert(dst, self.album_items.pop(src))
        self._save_config()
        return True

    def album_settings(self, body: dict, scene: str = "") -> None:
        if scene:
            def change(alb: dict) -> bool:
                if "slideshow" in body:
                    alb["slideshow"] = bool(body["slideshow"])
                if "interval" in body:
                    alb["interval"] = max(2, min(600, int(body["interval"])))
                if body.get("transition") in ALBUM_TRANSITIONS:
                    alb["transition"] = body["transition"]
                if body.get("fit") in ("cover", "contain", "fill"):
                    alb["fit"] = body["fit"]
                if "gap" in body:
                    alb["gap"] = max(0, min(64, int(body["gap"])))
                return True
            if self._album_scene_edit(scene, change):
                self._album_last = time.time()
                if self.album_active() and self.album_scene_name == scene:
                    threading.Thread(target=self._push_album, daemon=True).start()
            return
        if body.get("source") in ("wallpaper", "album"):
            self.standby_source = body["source"]
            self._save_config()
        if "slideshow" in body:
            self.album_slideshow = bool(body["slideshow"])
            self._album_last = time.time()
        if "interval" in body:
            self.album_interval = max(2, min(600, int(body["interval"])))
        if body.get("transition") in ALBUM_TRANSITIONS:
            self.album_transition = body["transition"]
        if body.get("fit") in ("cover", "contain", "fill"):
            self.album_fit = body["fit"]
            if self.album_active() and not self.album_scene_name:
                threading.Thread(target=self._push_album, daemon=True).start()
        self._save_soon(0.5)

    def _album_canvas(self, index: int) -> Image.Image | None:
        """相册图片铺满整版（含键间缝隙补偿和旋转），按内容缓存。"""
        return self._album_canvas_generic({"fit": self._alb_fit(), "gap": self._alb_gap()},
                                          self._alb_items(), index)

    def _album_frame(self, alb: dict, items: list, index: int):
        """相册某一张的画面：(帧列表 [PIL.Image], 每帧毫秒)。动图返回多帧，静态图一帧，缺失返回 ([], [])。"""
        path = self._album_file_for(items, index)
        fit = str(alb.get("fit") or "cover")
        if fit not in ("cover", "contain", "fill"):
            fit = "cover"
        gap = self._gap(alb.get("gap"))
        w, h = self._canvas_size(gap)
        if not path:
            return [Image.new("RGB", (w, h), (0, 0, 0))], [100]
        key = (path, os.path.getmtime(path), w, h, fit, self.standby_brightness, "anim")
        cache = getattr(self, "_album_cache", {})
        if key in cache:
            return cache[key]
        frames, durations = [], []
        try:
            with Image.open(path) as im:
                total = int(getattr(im, "n_frames", 1) or 1)
                if total > 1 and path.lower().endswith(".gif"):
                    for i in range(min(total, 120)):
                        im.seek(i)
                        frames.append(self._fit_canvas(self._flat_rgb(im), w, h, zoom_offset=False, fit=fit))
                        durations.append(max(40, min(1000, int(im.info.get("duration", 100) or 100))))
                else:
                    frames.append(self._fit_canvas(self._flat_rgb(im), w, h, zoom_offset=False, fit=fit))
                    durations.append(0)
        except Exception as exc:  # noqa: BLE001
            log(f"相册图片读取失败 {path}: {exc}")
            return [Image.new("RGB", (w, h), (0, 0, 0))], [100]
        cache = dict(list(cache.items())[-4:])
        cache[key] = (frames, durations)
        self._album_cache = cache
        return frames, durations

    def _album_canvas_generic(self, alb: dict, items: list, index: int) -> Image.Image | None:
        """相册某一张的「静态画面」（第一帧）；动图的多帧走 _album_frame。"""
        frames, _durs = self._album_frame(alb, items, index)
        return frames[0] if frames else None

    def _canvas_tiles(self, canvas: Image.Image, gap: int | None = None) -> dict:
        """整版画面切成 15 块（设备方向由 DeviceWS 统一转）。"""
        pitch = KEY_SIZE + self._gap(gap)
        out = {}
        for key in range(1, KEY_COUNT + 1):
            col = (key - 1) % GRID_COLS
            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
            sc, sr = self._cell_slot(col, row_top)
            out[key] = canvas.crop((sc * pitch, sr * pitch, sc * pitch + KEY_SIZE, sr * pitch + KEY_SIZE))
        return out

    def _send_tiles(self, tiles: dict, keys=None) -> None:
        for key in keys or tiles:
            buf = io.BytesIO()
            tiles[key].save(buf, "JPEG", quality=88, subsampling=0)
            self.device.send_image(key, buf.getvalue())

    def _album_can_push(self) -> bool:
        return (self.device.connected and not (self.writes_paused or self.quiet or self.yielded
                                               or self.screen_off))

    def _album_gif_start(self) -> None:
        """相册当前张是动图：切成 15 块 64×64 GIF 交给 SDK 循环播放（过渡结束后调用）。"""
        if not self._gif_allowed():
            return          # 相册播放时 in_standby=True，_gif_allowed 为 False：直接用主机推帧
        frames, durs = self._album_frame(dict(fit=self._alb_fit(), gap=self._alb_gap_eff()),
                                         self._alb_items(), self._alb_index())
        if len(frames) < 2:
            return
        gap = self._alb_gap_eff()
        # 逐键把「每一帧的这个键」打包成一个多帧 GIF，交给 SDK 播
        pitch = KEY_SIZE + gap
        ok_any = False
        for key in range(1, KEY_COUNT + 1):
            col = (key - 1) % GRID_COLS
            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
            sc, sr = self._cell_slot(col, row_top)
            kf, kd = [], []
            for f, d in zip(frames, durs):
                kf.append(f.crop((sc * pitch, sr * pitch, sc * pitch + KEY_SIZE, sr * pitch + KEY_SIZE)))
                kd.append(max(40, d))
            if len(kf) < 2:
                continue
            sig = hashlib.sha1(repr([id(f) for f in kf]).encode()).hexdigest()  # 内容不稳定，用帧序列长度+源路径
            src = self._album_file_for(self._alb_items(), self._alb_index()) or ""
            sig = hashlib.sha1(f"{src}|{self._alb_index()}|{key}|{gap}".encode()).hexdigest()[:20]
            out = os.path.join(GIF_CACHE, f"album_{sig}.gif")
            if not os.path.isfile(out):
                try:
                    kf[0].save(out, "GIF", save_all=True, append_images=kf[1:], duration=kd,
                               loop=0, disposal=1, optimize=False)
                except Exception as exc:  # noqa: BLE001
                    log(f"相册动图转换失败：{exc}")
                    return
            self.device.set_key_gif(key, out)
            self._gif_active[key] = out
            ok_any = True
        if ok_any:
            self.device.start_gif_loop()

    def _push_album(self, prev: Image.Image | None = None) -> bool:
        """把当前相册图片推到设备；prev 不为空时按设置的过渡效果切换。"""
        if not self._album_can_push():
            return False
        with self._album_lock:
            cur = self._album_canvas(self._alb_index())
            if cur is None:
                return False
            gap = self._alb_gap_eff()
            mode = self._alb_transition() if prev is not None and prev.size == cur.size else "none"
            self._pushing = True
            try:
                if mode in ("fade", "slide"):
                    # 步数压到 3、间隔拉到 0.16s：SDK 没有背压，5 步 0.07s 会把队列塞爆，
                    # 表现成「切换太慢、有的格子刷一半」。中间帧只是锦上添花，最终帧必须完整到达。
                    steps = 3
                    w = cur.width
                    for i in range(1, steps):
                        k = i / steps
                        if mode == "fade":
                            frame = Image.blend(prev, cur, k)
                        else:
                            ease = 1 - (1 - k) ** 2
                            off = int(w * ease)
                            frame = Image.new("RGB", cur.size)
                            frame.paste(prev, (-off, 0))
                            frame.paste(cur, (w - off, 0))
                        self._send_tiles(self._canvas_tiles(frame, gap))
                        self.device.refresh()
                        time.sleep(0.16)
                    # 最后一步直接发目标帧（和 mode == "none" 同路），不再被中间帧挤掉
                    self._gif_stop_all()
                    self._send_tiles(self._canvas_tiles(cur, gap))
                    self.device.refresh()
                    time.sleep(0.05)
                elif mode in ("wipe", "mosaic"):
                    tiles = self._canvas_tiles(cur, gap)
                    if mode == "wipe":       # 从左往右一列一列换（按观看方向的列）
                        groups: dict = {}
                        for key in tiles:
                            col = (key - 1) % GRID_COLS
                            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
                            groups.setdefault(self._cell_slot(col, row_top)[0], []).append(key)
                        order = [groups[c] for c in sorted(groups)]
                    else:
                        keys = list(tiles)
                        random.shuffle(keys)
                        order = [[k] for k in keys]
                    # 每组只发一次、间隔拉大：wipe 0.09 / mosaic 0.035 挤爆 SDK 队列，格子会刷一半
                    for group in order:
                        self._send_tiles(tiles, group)
                        self.device.refresh()
                        time.sleep(0.14)
                    # 补发整版：中间丢过包也能保证最终画面完整
                    self._gif_stop_all()
                    self._send_tiles(tiles)
                    self.device.refresh()
                else:
                    self._gif_stop_all()               # 先停掉上一张的动图循环
                    self._send_tiles(self._canvas_tiles(cur, gap))
                    self.device.refresh()
            finally:
                self._pushing = False
            self._last_paint = time.time()
        if mode != "none":
            time.sleep(0.05)                       # 过渡最后一段写完再起动图，避免被盖掉
        self._album_gif_start()
        return True

    def album_play(self, index: int | None = None) -> bool:
        items = self._alb_items()
        if not items:
            return False
        if not self.album_scene_name:
            # 待机幻灯片（不是相册场景）：按任意键就回到当前场景
            self._album_return = None
        if index is not None:
            self._alb_set_index(index)
        self._gif_stop_all()
        self.preview_mode = "album"
        self.in_standby = True
        self._album_last = time.time()
        try:
            if not self.screen_off:
                self.device.set_screen_brightness(self.screen_brightness)
        except Exception:  # noqa: BLE001
            pass
        threading.Thread(target=self._push_album, daemon=True).start()
        self.broadcast({"type": "album", **self.album_state()})
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True, "quiet": True})
        log(f"相册开始播放（{len(items)} 张）")
        return True

    def _play_album_scene(self, name: str) -> bool:
        """切到一个相册场景：用这个场景自己的图片和过渡设置播放。"""
        alb = self._album_scene_data(name)
        if alb is None:
            return False
        items = [str(n) for n in (alb.get("items") or []) if str(n)]
        if not items:
            # 空相册也允许「切换过去」：整屏黑 + 界面里可以继续编辑（拖图进来立即显示）
            self._ensure_album_nav(name)
            if not self.album_active():
                self._album_return = self.active_scene or self.custom_scene_name
                self._album_nav_backup = {
                    k: (self.key_actions.get(k),
                        dict(self.key_gestures.get(k) or {})) for k in self.ALBUM_NAV
                }
            self._album_nav_load(name)
            self.album_scene_name = name
            self._album_scene = {"items": [], "index": 0, "slideshow": bool(alb.get("slideshow", True)),
                                 "interval": max(2, min(600, int(alb.get("interval") or 8))),
                                 "transition": str(alb.get("transition") or "fade"),
                                 "fit": str(alb.get("fit") or "cover"), "gap": None if alb.get("gap") is None else self._gap(alb.get("gap"))}
            self._gif_stop_all()
            self.preview_mode = "album"
            self.in_standby = True
            self._album_last = time.time()
            self.active_scene = name           # 和非空路径一致：当前场景就是这个相册场景
            self._save_config()
            self._push_album()
            self.broadcast({"type": "album", **self.album_state()})
            self.broadcast({"type": "scene", "name": name, "ok": True})
            log(f"相册场景「{name}」是空的：整屏显示黑色，在编辑区里加图即可")
            return True
        if not self.album_active():
            self._album_return = self.active_scene or self.custom_scene_name
        self._ensure_album_nav(name)
        if not self.album_active():
            # 记住进相册前这三个键的动作，退出时还原
            self._album_nav_backup = {
                k: (self.key_actions.get(k),
                    dict(self.key_gestures.get(k) or {})) for k in self.ALBUM_NAV
            }
        self._album_nav_load(name)
        self.album_scene_name = name
        fit = str(alb.get("fit") or "cover")
        self._album_scene = {
            "items": items,
            "index": int(alb.get("index") or 0) % len(items),
            "slideshow": bool(alb.get("slideshow", True)),
            "interval": max(2, min(600, int(alb.get("interval") or 8))),
            "transition": str(alb.get("transition") or "fade") if str(alb.get("transition") or "") in ALBUM_TRANSITIONS else "fade",
            "fit": fit if fit in ("cover", "contain", "fill") else "cover",
            "gap": None if alb.get("gap") is None else self._gap(alb.get("gap")),
        }
        ok = self.album_play()
        if ok:
            self.active_scene = name
            self._save_config()
            log(f"已切换到相册场景：{name}（{len(items)} 张）")
        return ok

    def album_goto(self, delta: int = 1, index=None) -> bool:
        items = self._alb_items()
        if not items:
            return False
        prev = self._album_canvas(self._alb_index()) if self.album_active() else None
        if index is not None:
            self._alb_set_index(int(index))
        else:
            self._alb_set_index(self._alb_index() + delta)
        self._album_last = time.time()
        if self.album_active():
            threading.Thread(target=self._push_album, args=(prev,), daemon=True).start()
        self.broadcast({"type": "album", **self.album_state()})
        return True

    def album_stop(self) -> None:
        if self.preview_mode != "album":
            return
        scene_name = self.album_scene_name
        self.preview_mode = "wallpaper"
        self.in_standby = False
        self.album_scene_name = ""
        self._album_scene = {}
        self._last_activity = time.time()
        # 退出相册：把那三个导航键还原成进相册之前的样子，别把相册的设置带到原场景里
        backup = getattr(self, "_album_nav_backup", None)
        if backup:
            for k, (act, gs) in backup.items():
                if act is None:
                    self.key_actions.pop(k, None)
                else:
                    self.key_actions[k] = act
                if gs:
                    self.key_gestures[k] = gs
                else:
                    self.key_gestures.pop(k, None)
            self._album_nav_backup = None
        back = getattr(self, "_album_return", None)
        self._album_return = None
        self.broadcast({"type": "album", **self.album_state()})
        log(("相册场景已退出" if scene_name else "幻灯片已退出") + (f"，回到「{back}」" if back else ""))
        if back and back != (self.active_scene or self.custom_scene_name) and self.apply_scene(back):
            self.broadcast({"type": "scene", "name": back, "ok": True})
            return
        self.apply_keys()
        self.broadcast({"type": "scene", "name": self.current_scene(), "ok": True, "quiet": True})

    def _album_loop(self) -> None:
        while True:
            time.sleep(0.25)
            try:
                items = self._alb_items()
                if (self.album_active() and self._alb_slideshow() and len(items) > 1
                        and time.time() - self._album_last >= self._alb_interval()):
                    self.album_goto(1)
            except Exception as exc:  # noqa: BLE001
                log(f"幻灯片出错：{exc}")
                time.sleep(2)

    def _album_path_for(self, scene: str, index: int) -> str | None:
        if scene:
            items = self._album_scene_items(scene)
            return self._album_file_for(items, index)
        return self._album_file(index) if 0 <= index < len(self._alb_items()) else None

    def album_thumb(self, index: int, scene: str = "") -> bytes | None:
        path = self._album_path_for(scene, index)
        if not path:
            return None
        try:
            with Image.open(path) as im:
                img = ImageOps.fit(self._flat_rgb(im), (160, 96), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return None

    def _album_scene_index(self, scene: str, alb: dict) -> int:
        """正在播放这个场景时用内存里的当前张，否则用场景文件里存的。"""
        if self.album_active() and self.album_scene_name == scene:
            return self._alb_index()
        return int(alb.get("index") or 0)

    def album_preview_jpg(self, scene: str = "") -> bytes | None:
        """相册卡片里的大预览：当前图片按整版画布（观看方向）渲染。"""
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = self._album_scene_items(scene)
            if not items:
                return None
            canvas = self._album_canvas_generic(alb, items, self._album_scene_index(scene, alb))
        else:
            if not self.album_items:
                return None
            canvas = self._album_canvas(self.album_index)
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=85)
        return buf.getvalue()

    def album_canvas_gif(self, scene: str = "") -> bytes | None:
        """当前相册画面合成一张动图精灵图（和 tiles.png 同布局：15 格 5×3）。
        界面键盘预览铺它（background-position 各取各的位置），动起来和 Dock 一致。"""
        alb = (self._album_scene_data(scene) or {}) if scene else {"fit": self._alb_fit(), "gap": self._alb_gap()}
        items = self._album_scene_items(scene) if scene else self._alb_items()
        idx = self._album_scene_index(scene, alb) if scene else self._alb_index()
        frames, durs = self._album_frame(alb, items, idx)
        if not frames:
            return None
        # 排成和 tiles.png 一样的精灵图（15 格 5×3，无间隙），界面按 % 定位直接铺
        gap = self._gap(alb.get("gap"))
        W, H = GRID_COLS * KEY_SIZE, GRID_ROWS * KEY_SIZE
        sprite_frames = []
        for f in frames:
            sp = Image.new("RGB", (W, H), (0, 0, 0))
            for key, tile in self._canvas_tiles(f, gap).items():
                col = (key - 1) % GRID_COLS
                row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
                sp.paste(tile, (col * KEY_SIZE, row_top * KEY_SIZE))
            sprite_frames.append(sp)
        path = self._album_file_for(items, idx) or ""
        key = hashlib.sha1(f"{path}|{os.path.getmtime(path) if os.path.isfile(path) else 0}|{W}x{H}|{len(sprite_frames)}|{scene}".encode()).hexdigest()[:20]
        out = os.path.join(GIF_CACHE, f"albsprite_{key}.gif")
        if os.path.isfile(out):
            return open(out, "rb").read()
        os.makedirs(GIF_CACHE, exist_ok=True)
        try:
            if len(sprite_frames) > 1:
                sprite_frames[0].save(out, "GIF", save_all=True, append_images=sprite_frames[1:],
                                      duration=[max(40, d) for d in durs], loop=0, disposal=1, optimize=False)
            else:
                sprite_frames[0].save(out, "GIF", disposal=1, optimize=False)
            return open(out, "rb").read()
        except Exception as exc:  # noqa: BLE001
            log(f"相册精灵动图合成失败：{exc}")
            return None

    def album_tiles_png(self, scene: str = "") -> bytes | None:
        """相册当前图切成 15 块拼成精灵图（和设备上一致），给界面键盘预览叠在上面。"""
        if scene:
            alb = self._album_scene_data(scene) or {}
            items = self._album_scene_items(scene)
            if not items:
                return None
            canvas = self._album_canvas_generic(alb, items, self._album_scene_index(scene, alb))
            gap = self._gap(alb.get("gap"))
        else:
            canvas = self._album_canvas(self.album_index)
            gap = self._alb_gap_eff()
        if canvas is None:
            return None
        sprite = Image.new("RGB", (GRID_COLS * KEY_SIZE, GRID_ROWS * KEY_SIZE), (0, 0, 0))
        for key, tile in self._canvas_tiles(canvas, gap).items():
            col = (key - 1) % GRID_COLS
            row_top = GRID_ROWS - 1 - (key - 1) // GRID_COLS
            # 界面里键格会反向转回正，所以这里给「观看方向」的图块，不转
            sprite.paste(tile, (col * KEY_SIZE, row_top * KEY_SIZE))
        buf = io.BytesIO()
        sprite.save(buf, "PNG")
        return buf.getvalue()
