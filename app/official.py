"""读取官方 Stream Dock 驱动的本地数据：场景（.sdProfile / 预设包）、图标包、插件。

只读，不修改官方目录。

关键格式（实测还原）：
- 场景按键图标 .mbg 是加密过的 GIF：
      "mbg89a" + (GIF[0:100] 每字节 +1) + GIF[100:600] + KEY64 + GIF[600:] + KEY64
  KEY64 是固定的 64 字节 ASCII 串，文件末尾和第 600 字节处各插一份。
- 场景坐标 "列,行"：列 0..4 从左到右，行 0..2 从下到上（与本机键号 1=左下角一致）。
- 预设场景 *.streamDockProfile 是 zip，内容与已安装的 .sdProfile 目录结构相同。
"""
from __future__ import annotations

import glob
import io
import json
import os
import zipfile

SD_DATA = os.path.join(os.environ.get("APPDATA", ""), "HotSpot", "StreamDock")
DEVICE_PREFIX = "M18"          # 只收 M18 的场景（同目录下还有 293S 等别的设备）
GRID_COLS, GRID_ROWS = 5, 3

# 多媒体动作的 actionIdx（顺序取自官方程序内置的图标表）
MEDIA_BY_IDX = ["prev", "playpause", "next", "stop", "mute", "volup", "voldown"]
MEDIA_IMAGES = {
    "prev": "btn_media_prev_track", "playpause": "btn_media_play_pause",
    "next": "btn_media_next_track", "stop": "btn_media_stop", "mute": "btn_media_mute",
    "volup": "btn_media_volume_up", "voldown": "btn_media_volume_down",
}
BRIGHT_BY_IDX = ["brightup", "brightdown"]
BRIGHT_IMAGES = {"brightup": "btn_keybrightness_increase", "brightdown": "btn_keybrightness_decrease"}

IMAGE_EXTS = (".png", ".gif", ".jpg", ".jpeg", ".webp", ".bmp", ".mbg")


# --------------------------------------------------------------------------- #
# .mbg 解密
# --------------------------------------------------------------------------- #
def decode_mbg(data: bytes) -> bytes | None:
    """官方加密图标 → 原始 GIF 字节；不是 mbg 或解不开时返回 None。"""
    if not data.startswith(b"mbg8") or len(data) < 6 + 100 + 64:
        return None
    key = data[-64:]
    body = data[6:-64]
    if body[600:664] == key:
        body = body[:600] + body[664:]
    gif = bytes((b - 1) & 0xFF for b in body[:100]) + body[100:]
    return gif if gif.startswith(b"GIF8") else None


def image_bytes(name: str, data: bytes) -> tuple[str, bytes] | None:
    """统一成可直接给 Pillow 的字节，返回 (扩展名, 字节)。"""
    if name.lower().endswith(".mbg") or data.startswith(b"mbg8"):
        gif = decode_mbg(data)
        return (".gif", gif) if gif else None
    if data.startswith(b"\x89PNG"):
        return ".png", data
    if data.startswith(b"GIF8"):
        return ".gif", data
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", data
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", data
    if data[:2] == b"BM":
        return ".bmp", data
    return None


# --------------------------------------------------------------------------- #
# 场景来源：已安装的目录 / 预设 zip，统一成「按相对路径读文件」
# --------------------------------------------------------------------------- #
class _DirSource:
    def __init__(self, root: str):
        self.root = root

    def read(self, rel: str) -> bytes | None:
        path = os.path.normpath(os.path.join(self.root, rel))
        if not path.startswith(os.path.normpath(self.root)) or not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def where(self, rel: str) -> str | None:
        path = os.path.normpath(os.path.join(self.root, rel))
        return path if os.path.isfile(path) else None

    def children(self) -> list[str]:
        return sorted(os.path.basename(p) for p in
                      glob.glob(os.path.join(self.root, "profiles", "*.sdProfile")))


class _ZipSource:
    def __init__(self, path: str):
        self.path = path
        self.zf = zipfile.ZipFile(path)
        self.names = {n.replace("\\", "/"): n for n in self.zf.namelist()}

    def read(self, rel: str) -> bytes | None:
        name = self.names.get(rel.replace("\\", "/"))
        return self.zf.read(name) if name else None

    def where(self, rel: str) -> str | None:
        """压缩包里的文件没有独立路径，定位到预设包本身。"""
        return self.path if rel.replace("\\", "/") in self.names else None

    def children(self) -> list[str]:
        out = set()
        for n in self.names:
            parts = n.split("/")
            if len(parts) >= 3 and parts[0] == "profiles" and parts[1].endswith(".sdProfile"):
                out.add(parts[1])
        return sorted(out)


class _SubSource:
    """场景里的子页面（profiles/<id>.sdProfile/…）。"""

    def __init__(self, parent, prefix: str):
        self.parent, self.prefix = parent, prefix.rstrip("/") + "/"

    def read(self, rel: str) -> bytes | None:
        return self.parent.read(self.prefix + rel)

    def where(self, rel: str) -> str | None:
        return self.parent.where(self.prefix + rel)


def _load_json(raw: bytes | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_scene_sources() -> list[dict]:
    """所有 M18 场景：已安装的优先，预设包里同名的只在没装时补上。"""
    found: dict[str, dict] = {}
    for folder in sorted(glob.glob(os.path.join(SD_DATA, "profiles", "*.sdProfile"))):
        src = _DirSource(folder)
        man = _load_json(src.read("manifest.json"))
        if not man or not str(man.get("DeviceUUID", "")).startswith(DEVICE_PREFIX):
            continue
        name = str(man.get("Name") or "").strip() or os.path.basename(folder)[:8]
        found.setdefault(name, {"name": name, "id": os.path.basename(folder),
                                "kind": "installed", "source": src, "manifest": man})
    pattern = os.path.join(SD_DATA, "defaultData", "defaultProfiles", f"{DEVICE_PREFIX}*", "zh",
                           "*.streamDockProfile")
    for path in sorted(glob.glob(pattern)):
        try:
            src = _ZipSource(path)
        except (OSError, zipfile.BadZipFile):
            continue
        man = _load_json(src.read("manifest.json"))
        if not man:
            continue
        name = str(man.get("Name") or "").strip()
        if not name or name in found:
            continue
        found[name] = {"name": name, "id": os.path.basename(path), "kind": "preset",
                       "source": src, "manifest": man}
    return list(found.values())


# --------------------------------------------------------------------------- #
# 插件索引：动作 UUID → 插件目录（找默认图标 / 插件自带图片）
# --------------------------------------------------------------------------- #
def plugin_dirs(install_root: str) -> list[str]:
    roots = [os.path.join(SD_DATA, "plugins"), os.path.join(install_root, "plugins"),
             os.path.join(install_root, "defaultData", "defaultPlugins")]
    out = []
    for root in roots:
        out.extend(sorted(glob.glob(os.path.join(root, "*.sdPlugin"))))
    return out


def _plugin_manifest(folder: str) -> dict | None:
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _plugin_name(folder: str, man: dict) -> str:
    """优先用插件自带的简体中文名。"""
    for fname in ("zh_CN.json", "zh-CN.json", "zh.json"):
        loc = None
        try:
            with open(os.path.join(folder, fname), encoding="utf-8-sig") as fh:
                loc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(loc, dict) and loc.get("Name"):
            return str(loc["Name"])
    return str(man.get("Name") or os.path.basename(folder))


class PluginIndex:
    def __init__(self, install_root: str):
        self.install_root = install_root
        self.by_action: dict[str, tuple[str, dict]] = {}
        self.plugins: list[dict] = []
        seen = set()
        for folder in plugin_dirs(install_root):
            man = _plugin_manifest(folder)
            if not man:
                continue
            uid = str(man.get("UUID") or os.path.basename(folder)[:-len(".sdPlugin")])
            for act in man.get("Actions") or []:
                if isinstance(act, dict) and act.get("UUID"):
                    self.by_action.setdefault(str(act["UUID"]), (folder, act))
            if uid in seen:
                continue
            seen.add(uid)
            self.plugins.append({"folder": folder, "uuid": uid, "manifest": man})

    def folder_for(self, action_uuid: str) -> str | None:
        hit = self.by_action.get(action_uuid)
        if hit:
            return hit[0]
        # 动作 UUID 一般是「插件 UUID.动作名」
        for folder in plugin_dirs(self.install_root):
            base = os.path.basename(folder)[:-len(".sdPlugin")]
            if action_uuid.startswith(base + "."):
                return folder
        return None

    def default_image(self, action_uuid: str) -> str | None:
        hit = self.by_action.get(action_uuid)
        if not hit:
            return None
        folder, act = hit
        for cand in [(act.get("States") or [{}])[0].get("Image"), act.get("Icon")]:
            path = find_file(folder, cand)
            if path:
                return path
        return None


def find_file(folder: str | None, rel: str | None) -> str | None:
    """官方路径常省略扩展名（Images/btn_xxx），按常见扩展名和 @2x 依次找。"""
    if not folder or not rel:
        return None
    base = os.path.normpath(os.path.join(folder, rel.replace("/", os.sep)))
    if not base.startswith(os.path.normpath(folder)):
        return None
    cands = [base] if os.path.splitext(base)[1].lower() in IMAGE_EXTS else []
    stem = os.path.splitext(base)[0] if cands else base
    for ext in (".png", ".gif", ".jpg", ".jpeg", ".webp"):
        cands += [stem + ext, stem + "@2x" + ext, stem + ext.upper()]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


# --------------------------------------------------------------------------- #
# 场景转换
# --------------------------------------------------------------------------- #
def key_of(pos: str) -> int | None:
    try:
        col, row = (int(x) for x in str(pos).split(","))
    except ValueError:
        return None
    if not (0 <= col < GRID_COLS and 0 <= row < GRID_ROWS):
        return None
    return row * GRID_COLS + col + 1


def hotkey_combo(hk: dict, vk_names: dict) -> str:
    vk = int(hk.get("VKeyCode") or 0)
    if vk <= 0:
        return ""
    parts = []
    if hk.get("KeyCtrl") or hk.get("RKeyCtrl"):
        parts.append("ctrl")
    if hk.get("KeyShift") or hk.get("RKeyShift"):
        parts.append("shift")
    if hk.get("KeyOption") or hk.get("RKeyOption"):
        parts.append("alt")
    if hk.get("KeyCmd") or hk.get("RKeyCmd"):
        parts.append("win")
    if 65 <= vk <= 90:
        parts.append(chr(vk).lower())
    elif 48 <= vk <= 57:
        parts.append(chr(vk))
    elif 0x70 <= vk <= 0x87:
        parts.append(f"f{vk - 0x6F}")
    else:
        name = vk_names.get(vk)
        if not name:
            return ""
        parts.append(name)
    return "+".join(parts)


def _clean(s) -> str:
    return str(s or "").strip()


def convert_action(act: dict, vk_names: dict) -> tuple[dict | None, list]:
    """官方动作 → (本控制台动作, 宏步骤列表)。不支持的返回 (None, [])。"""
    uuid = str(act.get("UUID") or "")
    st = act.get("Settings") or {}
    tail = uuid.rsplit(".", 1)[-1]
    if uuid.endswith("system.website"):
        url = _clean(st.get("path"))
        return ({"type": "url", "url": url}, [{"op": "url", "url": url}]) if url else (None, [])
    if uuid.endswith("system.open"):
        path = _clean(st.get("path")).replace("/", "\\")
        return ({"type": "launch", "path": path}, [{"op": "launch", "path": path}]) if path else (None, [])
    if uuid.endswith("system.hotkey"):
        combos = [c for c in (hotkey_combo(h, vk_names) for h in st.get("Hotkeys") or []
                              if isinstance(h, dict)) if c]
        if not combos:
            return None, []
        steps = [{"op": "hotkey", "keys": c} for c in combos]
        if len(combos) == 1:
            return {"type": "hotkey", "keys": combos[0]}, steps
        return {"type": "macro", "steps": steps,
                "macro": "\n".join(f"hotkey {c}" for c in combos)}, steps
    if uuid.endswith("system.multimedia"):
        idx = int(st.get("actionIdx") or 0)
        if 0 <= idx < len(MEDIA_BY_IDX):
            return {"type": "media", "action": MEDIA_BY_IDX[idx]}, []
        return None, []
    if uuid.endswith("device.brightness"):
        idx = int(st.get("actionIdx") or 0)
        if 0 <= idx < len(BRIGHT_BY_IDX):
            return {"type": "media", "action": BRIGHT_BY_IDX[idx]}, []
        return None, []
    if uuid.endswith("multiactions.routine") or tail == "routine":
        steps = []
        for sub in act.get("Actions") or []:
            if not isinstance(sub, dict):
                continue
            sub_uuid = str(sub.get("UUID") or "")
            if "delay" in sub_uuid.lower():
                ms = (sub.get("Settings") or {}).get("delay") or (sub.get("Settings") or {}).get("time") or 0
                try:
                    steps.append({"op": "delay", "sec": max(0.0, float(ms) / 1000.0)})
                except (TypeError, ValueError):
                    pass
                continue
            _, sub_steps = convert_action(sub, vk_names)
            steps.extend(sub_steps)
        if not steps:
            return None, []
        lines = []
        for s in steps:
            if s["op"] == "delay":
                lines.append(f"delay {s['sec']:g}")
            elif s["op"] == "hotkey":
                lines.append(f"hotkey {s['keys']}")
            elif s["op"] == "url":
                lines.append(f"url {s['url']}")
            elif s["op"] == "launch":
                lines.append(f"launch {s['path']}")
        return {"type": "macro", "steps": steps, "macro": "\n".join(lines)}, steps
    if uuid.endswith("profile.rotate"):
        return {"type": "nav", "name": "下一场景"}, []
    if uuid.endswith("page.next") or uuid.endswith("page.indicator") or uuid.endswith("page.change"):
        return {"type": "nav", "name": "下一页"}, []
    if uuid.endswith("page.previous"):
        return {"type": "nav", "name": "上一页"}, []
    if uuid.endswith("page.goto"):
        try:
            n = int(st.get("page") or st.get("pageIndex") or 1)
        except (TypeError, ValueError):
            n = 1
        return {"type": "nav", "name": f"跳到第 {max(1, n)} 页"}, []
    if uuid.endswith("profile.backtoparent"):
        return {"type": "nav", "name": "主页"}, []
    return None, []


def _state(act: dict) -> dict:
    states = act.get("States") or [{}]
    idx = act.get("State", 0)
    if not isinstance(idx, int) or not (0 <= idx < len(states)):
        idx = 0
    st = states[idx]
    return st if isinstance(st, dict) else {}


def _font_size(value) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 14
    # 官方字号按 72px 键面设计，本机键面 64px，再略放大保证可读
    return max(9, min(24, int(round(v * 1.35))))


def _color(value) -> str:
    v = _clean(value)
    if v.startswith("#") and len(v) in (4, 7):
        return v
    if v.startswith("rgb"):
        try:
            nums = [int(float(x)) for x in v[v.index("(") + 1:v.index(")")].split(",")[:3]]
            return "#%02x%02x%02x" % tuple(max(0, min(255, n)) for n in nums)
        except ValueError:
            pass
    return "#ffffff"


def _src(res, where):
    return (res[0], res[1], where) if res else None


def resolve_image(act: dict, page_src, plugins: PluginIndex,
                  install_root: str) -> tuple[str, bytes, str | None] | None:
    """按键图标：场景自带 → 插件自带图片 → 插件默认图标。返回 (扩展名, 字节, 来源路径)。"""
    uuid = str(act.get("UUID") or "")
    st = _state(act)
    img = _clean(st.get("Image"))
    if img:
        if "/" not in img and "\\" not in img:
            raw = page_src.read("Images/" + img)
            if raw:
                return _src(image_bytes(img, raw), page_src.where("Images/" + img))
        raw = page_src.read(img)
        if raw:
            return _src(image_bytes(img, raw), page_src.where(img))
        folder = plugins.folder_for(uuid)
        path = find_file(folder, img)
        if not path:
            # Images/btn_switchProfile 这类内置图：在所有官方插件目录里找
            for f in plugin_dirs(install_root):
                path = find_file(f, img)
                if path:
                    break
        if path:
            with open(path, "rb") as fh:
                return _src(image_bytes(path, fh.read()), path)
    # 没有自定义图：按动作类型取官方默认图标
    settings = act.get("Settings") or {}
    folder = plugins.folder_for(uuid)
    special = None
    if uuid.endswith("system.multimedia"):
        idx = int(settings.get("actionIdx") or 0)
        if 0 <= idx < len(MEDIA_BY_IDX):
            special = "Images/" + MEDIA_IMAGES[MEDIA_BY_IDX[idx]]
    elif uuid.endswith("device.brightness"):
        idx = int(settings.get("actionIdx") or 0)
        if 0 <= idx < len(BRIGHT_BY_IDX):
            special = "Images/" + BRIGHT_IMAGES[BRIGHT_BY_IDX[idx]]
    path = find_file(folder, special) if special else None
    path = path or plugins.default_image(uuid)
    if path:
        with open(path, "rb") as fh:
            return _src(image_bytes(path, fh.read()), path)
    return None


def convert_page(manifest: dict, page_src, plugins: PluginIndex, install_root: str,
                 vk_names: dict) -> dict:
    """一页官方场景 → {actions, styles, images:{key:(ext, bytes)}, stats}。"""
    actions, styles, images = {}, {}, {}
    total = supported = 0
    for pos, act in (manifest.get("Actions") or {}).items():
        key = key_of(pos)
        if key is None or not isinstance(act, dict):
            continue
        total += 1
        spec, _ = convert_action(act, vk_names)
        if spec:
            actions[key] = spec
            supported += 1
        try:
            img = resolve_image(act, page_src, plugins, install_root)
        except OSError:
            img = None
        if img:
            images[key] = img
        st = _state(act)
        title = _clean(st.get("Title"))
        show = st.get("ShowTitle", True) is not False
        if title and show:
            align = str(st.get("TitleAlignment") or "bottom").lower()
            styles[key] = {
                "text": title[:12],
                "color": _color(st.get("TitleColor")),
                "bg": "#000000", "opacity": 100,
                "size": _font_size(st.get("FontSize") or 10),
                "pos": {"top": "top", "middle": "center"}.get(align, "bottom"),
            }
        elif not img:
            # 既没图也没标题：用动作名兜底，免得整键一片黑
            name = _clean(act.get("Name")) or uuid.rsplit(".", 1)[-1]
            styles[key] = {"text": name[:12], "color": "#e8eefc", "bg": "#12161f",
                           "opacity": 100, "size": 13, "pos": "center"}
    return {"actions": actions, "styles": styles, "images": images,
            "stats": {"total": total, "supported": supported}}


def convert_scene(item: dict, plugins: PluginIndex, install_root: str, vk_names: dict) -> dict:
    """整个场景（含子页面）。返回 {name, pages:[…], stats}。"""
    src, man = item["source"], item["manifest"]
    pages = [convert_page(man, src, plugins, install_root, vk_names)]
    root_id = item["id"]
    listed = [p for p in ((man.get("Pages") or {}).get("Pages") or []) if p != root_id]
    extra = [c for c in src.children() if c not in listed]
    for child in listed + extra:
        sub = _SubSource(src, f"profiles/{child}")
        cman = _load_json(sub.read("manifest.json"))
        if not cman or not cman.get("Actions"):
            continue
        pages.append(convert_page(cman, sub, plugins, install_root, vk_names))
    stats = {"total": sum(p["stats"]["total"] for p in pages),
             "supported": sum(p["stats"]["supported"] for p in pages),
             "pages": len(pages)}
    return {"name": item["name"], "pages": pages, "stats": stats}


# --------------------------------------------------------------------------- #
# 图标包 / 插件清单
# --------------------------------------------------------------------------- #
def icon_pack_dirs(install_root: str) -> list[str]:
    roots = [os.path.join(SD_DATA, "icons"), os.path.join(install_root, "icons")]
    out = []
    for root in roots:
        out.extend(sorted(glob.glob(os.path.join(root, "*.sdIconPack"))))
    return out


def icon_pack_info(folder: str) -> dict:
    man = _plugin_manifest(folder) or {}
    pack_id = os.path.basename(folder)[:-len(".sdIconPack")]
    icon_dir = os.path.join(folder, "icons")
    if not os.path.isdir(icon_dir):
        icon_dir = folder
    files = [p for p in sorted(glob.glob(os.path.join(icon_dir, "*")))
             if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
    name = str(man.get("Name") or pack_id)
    for fname in ("zh_CN.json", "zh.json"):
        try:
            with open(os.path.join(folder, fname), encoding="utf-8-sig") as fh:
                loc = json.load(fh)
            if isinstance(loc, dict) and loc.get("Name"):
                name = str(loc["Name"])
                break
        except (OSError, ValueError):
            continue
    return {"id": pack_id, "name": name, "author": str(man.get("Author") or ""),
            "version": str(man.get("Version") or ""), "folder": folder, "files": files}


def _plugin_locale(folder: str) -> dict:
    for fname in ("zh_CN.json", "zh-CN.json", "zh.json"):
        try:
            with open(os.path.join(folder, fname), encoding="utf-8-sig") as fh:
                loc = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(loc, dict):
            return loc
    return {}


def plugin_list(install_root: str) -> list[dict]:
    """官方插件：内置 + 用户下载。downloaded=True 表示在用户目录（商店下载）。"""
    user_root = os.path.normpath(os.path.join(SD_DATA, "plugins"))
    out, seen = [], set()
    for folder in plugin_dirs(install_root):
        man = _plugin_manifest(folder)
        if not man:
            continue
        uid = str(man.get("UUID") or os.path.basename(folder)[:-len(".sdPlugin")])
        if uid in seen:
            continue
        seen.add(uid)
        loc = _plugin_locale(folder)
        icon = find_file(folder, man.get("Icon")) or find_file(folder, man.get("CategoryIcon"))
        actions = []
        for act in man.get("Actions") or []:
            if not isinstance(act, dict):
                continue
            aid = str(act.get("UUID") or "")
            aloc = loc.get(aid) if isinstance(loc.get(aid), dict) else {}
            state0 = (act.get("States") or [{}])[0] if act.get("States") else {}
            a_icon = (find_file(folder, state0.get("Image") if isinstance(state0, dict) else None)
                      or find_file(folder, act.get("Icon")))
            actions.append({
                "uuid": aid,
                "name": str(aloc.get("Name") or act.get("Name") or aid.rsplit(".", 1)[-1]),
                "tooltip": str(aloc.get("Tooltip") or act.get("Tooltip") or ""),
                "icon": a_icon,
            })
        out.append({
            "id": uid,
            "name": str(loc.get("Name") or _plugin_name(folder, man)),
            "author": str(man.get("Author") or ""),
            "version": str(man.get("Version") or ""),
            "description": str(loc.get("Description") or man.get("Description") or ""),
            "actions": actions,
            "downloaded": os.path.normpath(os.path.dirname(folder)) == user_root,
            "folder": folder,
            "icon": icon or next((a["icon"] for a in actions if a["icon"]), None),
        })
    out.sort(key=lambda p: (not p["downloaded"], p["name"].lower()))
    return out


def read_image(path: str) -> tuple[str, bytes] | None:
    try:
        with open(path, "rb") as fh:
            return image_bytes(path, fh.read())
    except OSError:
        return None


def to_pillow(data: bytes):
    from PIL import Image  # 延迟导入，方便单测
    return Image.open(io.BytesIO(data))
