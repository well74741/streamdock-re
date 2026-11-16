"""打包时把用户当前的场景（含相册场景）导出成绿色版的默认场景。

build.py 在打包前调用 export_presets_for_build()（通过控制台的 HTTP 接口拿 .m18scene 包，
再解压成 presets/scenes/<名>.json + presets/album1/<图>，和随包带的「相册1」同一机制）。
"""
import io
import json
import os
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def export_presets(base: str = "http://127.0.0.1:8642") -> int:
    """把控制台里除「自定义 / 内置」外的所有场景导出成 presets。返回场景数。"""
    def post(path, body=None):
        r = urllib.request.Request(base + path, json.dumps(body or {}).encode(),
                                   {"Content-Type": "application/json", "Origin": base})
        return json.load(urllib.request.urlopen(r, timeout=60))

    items = post("/api/scenes")["items"]
    names = [i["name"] for i in items
             if not i.get("custom") and not i.get("builtin") and not i.get("official")]
    # 官方场景也可以带（official=True 表示本机官方驱动里有同名，别人的机器上装不装都无妨）
    names += [i["name"] for i in items if i.get("official")]
    seen, out = set(), []
    for n in names:
        if n in seen or n in ("自定义", "相册", "幻灯片"):
            continue
        seen.add(n)
        r = post("/api/scenes/export", {"name": n})
        if r.get("ok"):
            out.append((n, base64 := r["data"]))
    scenes_dir = os.path.join(APP, "presets", "scenes")
    album_dir = os.path.join(APP, "presets", "album1")
    os.makedirs(scenes_dir, exist_ok=True)
    os.makedirs(album_dir, exist_ok=True)
    # 清掉上一次导出的（带 m18- 前缀标记，别动随包带的 相册1/音乐控制台）
    for d in (scenes_dir,):
        for f in os.listdir(d):
            if f.startswith("m18-") and f.endswith(".json"):
                os.remove(os.path.join(d, f))
    for f in os.listdir(album_dir):
        if f.startswith("m18-"):
            os.remove(os.path.join(album_dir, f))
    n_ok = 0
    for name, data in out:
        import base64 as _b64
        zf = zipfile.ZipFile(io.BytesIO(_b64.b64decode(data)))
        meta = json.loads(zf.read("scene.json").decode("utf-8"))
        scene = meta["scene"]
        scene.setdefault("sceneType", "normal")
        safe = "m18-" + "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name)[:40] + ".json"
        # 图片：改名 m18-<原名>，场景里的引用同步改（避免和随包带的 preset-album1-* 撞名）
        ren = {}
        for info in zf.infolist():
            if not info.filename.startswith("images/") or info.is_dir():
                continue
            b = os.path.basename(info.filename)
            if not b:
                continue
            ext = os.path.splitext(b)[1].lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                continue
            new = "m18-" + b
            with open(os.path.join(album_dir, new), "wb") as fh:
                fh.write(zf.read(info))
            ren[b] = new
        for p in scene.get("pages") or []:
            p["images"] = {k: ren.get(os.path.basename(str(v)), os.path.basename(str(v)))
                           for k, v in (p.get("images") or {}).items() if v}
        if scene.get("sceneType") == "album":
            alb = scene.get("album") or {}
            alb["items"] = [ren.get(os.path.basename(str(n)), os.path.basename(str(n))) for n in (alb.get("items") or [])]
        with open(os.path.join(scenes_dir, safe), "w", encoding="utf-8") as fh:
            json.dump(scene, fh, ensure_ascii=False, indent=1)
        n_ok += 1
        print(f"  预设场景：{name} → {safe}（{len(ren)} 张图）")
    return n_ok


if __name__ == "__main__":
    print(f"导出 {export_presets()} 个场景到 app/presets/")
