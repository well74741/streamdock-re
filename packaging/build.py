"""一键构建安装包：python packaging/build.py

产物：dist/release/M18Console-Setup-<版本>.exe

步骤：
1. PyInstaller 把 app/server.py 打成文件夹版程序（dist/M18Console/，含 Python 运行时、界面、默认壁纸）
2. 把官方 WebSocket SDK（ws-sdk/）放到程序旁边
3. 整个程序文件夹压成 payload.zip
4. PyInstaller 把 packaging/installer.py + payload.zip 打成单个 Setup.exe
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
SEP = ";"                          # PyInstaller --add-data 在 Windows 上用分号

sys.path.insert(0, APP)
from version import VERSION  # noqa: E402


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd[:6]), "…" if len(cmd) > 6 else "")
    subprocess.run(cmd, check=True, cwd=ROOT)


def pyinstaller(*args: str) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--workpath", os.path.join(BUILD, "work"), "--specpath", BUILD, *args])


def main() -> int:
    sdk = os.path.join(ROOT, "ws-sdk", "StreamDockWebsocketSDK.exe")
    if not os.path.isfile(sdk):
        print("✗ 缺少 ws-sdk/StreamDockWebsocketSDK.exe（官方 WebSocket SDK），先放进来再构建")
        return 1
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)

    # 1) 程序本体（文件夹版，启动快、不会每次解压到临时目录）
    pyinstaller(
        "--name", "M18Console", "--windowed", "--onedir",
        "--icon", os.path.join(APP, "icon.ico"),
        "--distpath", DIST,
        "--paths", APP,
        "--add-data", f"{os.path.join(APP, 'ui')}{SEP}ui",
        "--add-data", f"{os.path.join(APP, 'assets', 'wallpaper.jpg')}{SEP}assets",
        "--add-data", f"{os.path.join(APP, 'icon.ico')}{SEP}.",
        "--add-data", f"{os.path.join(APP, 'icon.png')}{SEP}.",
        "--add-data", f"{os.path.join(APP, 'config.example.json')}{SEP}.",
        "--exclude-module", "tkinter",
        "--exclude-module", "numpy",          # Pillow 的可选依赖，控制台用不到（省 26 MB）
        os.path.join(APP, "server.py"),
    )
    app_dir = os.path.join(DIST, "M18Console")

    # 2) 官方 WebSocket SDK 放在 exe 旁边（server.py 里 APP_DIR/ws-sdk）
    shutil.copytree(os.path.join(ROOT, "ws-sdk"), os.path.join(app_dir, "ws-sdk"))

    # 2b) 卸载程序：同一个 installer.py，但不带安装内容，单独打一个小 exe 放进程序目录
    pyinstaller(
        "--name", "uninstall", "--windowed", "--onefile",
        "--icon", os.path.join(APP, "icon.ico"),
        "--distpath", os.path.join(BUILD, "uninstaller"),
        "--paths", APP, "--hidden-import", "version", "--exclude-module", "numpy",
        "--add-data", f"{os.path.join(APP, 'icon.ico')}{SEP}.",
        os.path.join(HERE, "installer.py"),
    )
    shutil.copyfile(os.path.join(BUILD, "uninstaller", "uninstall.exe"), os.path.join(app_dir, "uninstall.exe"))

    # 3) 程序文件夹 → payload.zip
    payload = os.path.join(BUILD, "payload.zip")
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(app_dir):
            for f in files:
                full = os.path.join(root, f)
                zf.write(full, os.path.relpath(full, app_dir))
    print(f"payload.zip：{os.path.getsize(payload) / 1048576:.1f} MB")

    # 4) 安装程序（单文件）
    release = os.path.join(DIST, "release")
    pyinstaller(
        "--name", f"M18Console-Setup-{VERSION}", "--windowed", "--onefile",
        "--icon", os.path.join(APP, "icon.ico"),
        "--distpath", release,
        "--paths", APP,
        "--hidden-import", "version", "--exclude-module", "numpy",
        "--add-data", f"{payload}{SEP}.",
        "--add-data", f"{os.path.join(APP, 'icon.ico')}{SEP}.",
        os.path.join(HERE, "installer.py"),
    )
    setup = os.path.join(release, f"M18Console-Setup-{VERSION}.exe")
    print(f"\n✓ 安装包：{setup}（{os.path.getsize(setup) / 1048576:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
