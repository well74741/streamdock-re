"""一键构建安装包：python packaging/build.py（或双击项目根目录的「打包.bat」）

    python packaging/build.py            # 检查环境 → 缺什么引导装什么 → 打包
    python packaging/build.py --yes      # 缺的依赖不问，直接装
    python packaging/build.py --check    # 只检查环境，不打包

产物：dist/release/M18Console-Setup-<版本>.exe

步骤：
1. PyInstaller 把 app/server.py 打成文件夹版程序（dist/M18Console/，含 Python 运行时、界面、默认壁纸）
2. 把官方 WebSocket SDK（ws-sdk/）放到程序旁边
3. 整个程序文件夹压成 payload.zip
4. PyInstaller 把 packaging/installer.py + payload.zip 打成单个 Setup.exe
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
SEP = ";"                          # PyInstaller --add-data 在 Windows 上用分号

sys.path.insert(0, APP)
from version import VERSION  # noqa: E402

PIP_PACKAGES = {"PIL": "pillow", "PyInstaller": "pyinstaller"}     # 模块名 → pip 包名
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"            # 官方源下载失败时换国内镜像
SDK_EXE = os.path.join(ROOT, "ws-sdk", "StreamDockWebsocketSDK.exe")
SDK_PAGE = "https://sdk.key123.vip/"


# --------------------------------------------------------------------------- #
# 环境检查：缺什么就引导装什么
# --------------------------------------------------------------------------- #
def ask(question: str, auto_yes: bool) -> bool:
    if auto_yes:
        print(f"{question} → 是（--yes）")
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes", "是")
    except EOFError:
        return False


def missing_modules() -> list[str]:
    out = []
    for mod in PIP_PACKAGES:
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            out.append(mod)
    return out


def pip_install(packages: list[str]) -> bool:
    py = [sys.executable, "-m", "pip"]
    if subprocess.run(py + ["--version"], capture_output=True).returncode != 0:
        print("  · 没有 pip，先用 ensurepip 装上…")
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"])
    for extra in ([], ["-i", PIP_MIRROR]):
        label = "国内镜像" if extra else "官方源"
        print(f"  · 正在安装 {' '.join(packages)}（{label}）…")
        if subprocess.run(py + ["install", "--upgrade", *extra, *packages]).returncode == 0:
            return True
        print(f"  ✗ 从{label}安装失败")
    return False


def console_in_dist() -> list[tuple[str, str]]:
    """从 dist 目录里运行着的控制台（打包要先删 dist，文件被占用会失败）。返回 [(端口, 程序路径)]。"""
    found = []
    for port in range(8642, 8654):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as r:
                info = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        pid = info.get("pid")
        if not pid:
            continue
        try:
            path = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {int(pid)}).Path"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            path = ""
        if path and os.path.normcase(path).startswith(os.path.normcase(DIST + os.sep)):
            found.append((str(port), path))
    return found


def preflight(auto_yes: bool) -> bool:
    print(f"== 检查打包环境（M18 控制台 {VERSION}）==")
    ok = True

    # 1) Python 版本
    if sys.version_info < (3, 9):
        print(f"✗ Python {sys.version.split()[0]} 太旧，需要 3.9 或更高：https://www.python.org/downloads/")
        return False
    print(f"✓ Python {sys.version.split()[0]}（{sys.executable}）")

    # 2) tkinter：安装程序的界面要用，pip 装不了，只能重装 Python 时勾选
    try:
        import tkinter  # noqa: F401
        print("✓ tkinter")
    except Exception:  # noqa: BLE001
        print("✗ 缺少 tkinter（安装程序界面要用）。重新运行 Python 安装程序 → Modify，勾选「tcl/tk and IDLE」")
        ok = False

    # 3) Pillow / PyInstaller：可以自动装
    miss = missing_modules()
    if miss:
        pkgs = [PIP_PACKAGES[m] for m in miss]
        print(f"✗ 缺少 Python 依赖：{', '.join(pkgs)}")
        if ask(f"  现在自动安装 {' '.join(pkgs)}？", auto_yes) and pip_install(pkgs):
            still = missing_modules()
            if still:
                print(f"✗ 装完仍然导入失败：{', '.join(still)}（可能装到了别的 Python 上）")
                ok = False
            else:
                print("✓ 依赖已安装")
        else:
            print(f"  手动安装：\"{sys.executable}\" -m pip install {' '.join(pkgs)}")
            ok = False
    else:
        import PIL
        import PyInstaller
        print(f"✓ Pillow {PIL.__version__}，PyInstaller {PyInstaller.__version__}")

    # 4) 官方 WebSocket SDK（不在仓库里，也不随官方驱动安装，需要从 SDK 网站下载）
    if os.path.isfile(SDK_EXE):
        print("✓ 官方 WebSocket SDK（ws-sdk\\StreamDockWebsocketSDK.exe）")
    else:
        print("✗ 缺少 ws-sdk\\StreamDockWebsocketSDK.exe（官方 WebSocket SDK）")
        print(f"  从 {SDK_PAGE} 下载 Windows 版 WebSocket SDK，把压缩包里的文件解压到：")
        print(f"  {os.path.join(ROOT, 'ws-sdk')}\\（解压后这个目录里应直接有 StreamDockWebsocketSDK.exe）")
        if ask("  现在打开 SDK 下载页？", False):
            webbrowser.open(SDK_PAGE)
        ok = False

    # 5) dist 里的程序正在运行 → 文件被占用，删不掉旧产物
    running = console_in_dist()
    if running:
        print(f"✗ dist 里的控制台正在运行（{running[0][1]}），打包前要先退出")
        if ask("  现在退出它？", auto_yes):
            for port, _ in running:
                origin = f"http://127.0.0.1:{port}"
                req = urllib.request.Request(f"{origin}/api/quit", b"{}",
                                             {"Content-Type": "application/json", "Origin": origin})
                try:
                    urllib.request.urlopen(req, timeout=3).read()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(3)
            print("✓ 已退出")
        else:
            ok = False
    if ok:
        print("== 环境就绪 ==\n")
    else:
        print("\n== 环境还没准备好，按上面的提示处理后重新运行 ==")
    return ok


def clean_dir(path: str) -> bool:
    """删除旧产物；被占用时给出明确提示（以前 ignore_errors 会静默失败，后面 PyInstaller 才报一堆错）。"""
    if not os.path.exists(path):
        return True
    for _ in range(3):
        try:
            shutil.rmtree(path)
            return True
        except OSError as exc:
            last = exc
            time.sleep(1.5)
    print(f"✗ 删不掉 {path}：{last}")
    print("  有程序占用了里面的文件：关掉从 dist 运行的控制台、停在该目录的终端 / 资源管理器窗口后重试")
    return False


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd[:6]), "…" if len(cmd) > 6 else "")
    subprocess.run(cmd, check=True, cwd=ROOT)


def pyinstaller(*args: str) -> None:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--workpath", os.path.join(BUILD, "work"), "--specpath", BUILD, *args])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)   # 中文 / ✓ 在 GBK 控制台里不报错；逐行输出
    parser = argparse.ArgumentParser(description="打包 M18 控制台安装程序")
    parser.add_argument("--yes", "-y", action="store_true", help="缺少的依赖直接安装，不再询问")
    parser.add_argument("--check", action="store_true", help="只检查环境，不打包")
    args = parser.parse_args()
    if not preflight(args.yes):
        return 1
    if args.check:
        return 0
    if not (clean_dir(BUILD) and clean_dir(DIST)):
        return 1
    t0 = time.time()

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

    # 2c) MCP 服务：给 Codex / DeepSeek Harness / Claude 等 AI 智能体用（带控制台的单文件 exe，走标准输入输出）
    pyinstaller(
        "--name", "m18-mcp", "--console", "--onefile",
        "--icon", os.path.join(APP, "icon.ico"),
        "--distpath", os.path.join(BUILD, "mcp"),
        "--paths", APP, "--hidden-import", "version",
        "--exclude-module", "tkinter", "--exclude-module", "numpy", "--exclude-module", "PIL",
        "--add-data", f"{os.path.join(APP, 'plugin_guide.md')}{SEP}.",
        os.path.join(APP, "mcp_server.py"),
    )
    shutil.copyfile(os.path.join(BUILD, "mcp", "m18-mcp.exe"), os.path.join(app_dir, "m18-mcp.exe"))
    for doc in (os.path.join(ROOT, "AGENTS.md"), os.path.join(APP, "plugin_guide.md")):
        shutil.copyfile(doc, os.path.join(app_dir, os.path.basename(doc)))

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
    print(f"\n✓ 安装包：{setup}（{os.path.getsize(setup) / 1048576:.1f} MB，用时 {time.time() - t0:.0f} 秒）")
    print("  发给别人的就是这一个文件；安装说明见 docs/打包与安装指南.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
