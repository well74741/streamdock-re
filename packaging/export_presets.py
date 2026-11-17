"""打包时把控制台窗口里当前的场景（含相册 / 灯效场景和它们用到的资源）导出成绿色版的默认场景。

build.py 在打包前调用 export_presets()：通过运行中控制台的 HTTP 接口逐个拿 .m18scene 包，解压成
  presets/scenes/<场景名>.json   —— 文件名就是场景名，新用户看到的场景和这里的窗口一致
  presets/scenes/.scene-order.json —— 窗口里的场景顺序
  presets/album1/m18-<图>          —— 场景用到的图片（按键图、相册图），首次运行拷进上传目录
灯效场景用到的灯效代码内嵌在场景的 effect.library 里，首次运行写进灯效库。
不带：「自定义」草稿场景、图标库（只带场景实际用到的那几张图）、自制 / 第三方插件代码
（官方内置插件由 build.py 的 builtin-plugins 单独随包）。
"""
import base64
import io
import json
import os
import sys
import urllib.request
import zipfile

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _safe(name: str) -> str:
    """和 app_scenes._scene_path 同一套文件名规则：导出的文件装回去就是同名场景。"""
    return "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (name or ""))[:40].strip()


def export_presets(base: str = "http://127.0.0.1:8642") -> int:
    """把控制台窗口里除「自定义」外的全部场景导出成 presets（先清空旧预设）。返回场景数。"""
    def post(path, body=None):
        r = urllib.request.Request(base + path, json.dumps(body or {}).encode(),
                                   {"Content-Type": "application/json", "Origin": base})
        return json.load(urllib.request.urlopen(r, timeout=60))

    items = post("/api/scenes")["items"]
    names = [i["name"] for i in items if not i.get("custom") and not i.get("builtin")]
    packages = []
    for n in names:
        r = post("/api/scenes/export", {"name": n})
        if not r.get("ok"):
            raise RuntimeError(f"导出场景「{n}」失败：{r.get('error') or r}")
        packages.append((n, r["data"]))
    scenes_dir = os.path.join(APP, "presets", "scenes")
    album_dir = os.path.join(APP, "presets", "album1")
    os.makedirs(scenes_dir, exist_ok=True)
    os.makedirs(album_dir, exist_ok=True)
    # 预设完全以窗口为准：旧的场景文件和图片先清掉（都在 Git 里，可随时找回）
    for d in (scenes_dir, album_dir):
        for f in os.listdir(d):
            path = os.path.join(d, f)
            if os.path.isfile(path):
                os.remove(path)
    order, total_bytes = [], 0
    for name, data in packages:
        zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(data)))
        meta = json.loads(zf.read("scene.json").decode("utf-8"))
        scene = meta["scene"]
        scene.setdefault("sceneType", "normal")
        # 图片：改名 m18-<原名>，场景里的引用同步改（装进上传目录时不和用户已有文件撞名）
        ren = {}
        for info in zf.infolist():
            if not info.filename.startswith("images/") or info.is_dir():
                continue
            b = os.path.basename(info.filename)
            if not b or os.path.splitext(b)[1].lower() not in IMAGE_EXTS:
                continue
            new = b if b.startswith("m18-") else "m18-" + b
            blob = zf.read(info)
            with open(os.path.join(album_dir, new), "wb") as fh:
                fh.write(blob)
            total_bytes += len(blob)
            ren[b] = new
        for p in scene.get("pages") or []:
            p["images"] = {k: ren.get(os.path.basename(str(v)), os.path.basename(str(v)))
                           for k, v in (p.get("images") or {}).items() if v}
        if scene.get("sceneType") == "album":
            alb = scene.get("album") or {}
            alb["items"] = [ren.get(os.path.basename(str(n)), os.path.basename(str(n))) for n in (alb.get("items") or [])]
        safe = _safe(name)
        with open(os.path.join(scenes_dir, safe + ".json"), "w", encoding="utf-8") as fh:
            json.dump(scene, fh, ensure_ascii=False, indent=1)
        order.append(name)
        extra = ""
        if scene.get("sceneType") == "effect":
            extra = f"，{len((scene.get('effect') or {}).get('items') or [])} 页灯效"
        print(f"  预设场景：{name} → {safe}.json（{len(ren)} 张图{extra}）")
    with open(os.path.join(scenes_dir, ".scene-order.json"), "w", encoding="utf-8") as fh:
        json.dump(order, fh, ensure_ascii=False)
    print(f"  场景资源合计 {total_bytes / 1048576:.1f} MB")
    return len(order)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"导出 {export_presets()} 个场景到 app/presets/")
