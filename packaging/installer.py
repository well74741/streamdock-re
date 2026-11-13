"""Stream Dock M18 控制台 —— 安装 / 卸载程序。

由 packaging/build.py 用 PyInstaller 打成单个 Setup.exe，程序本体以 payload.zip 内嵌。

- 安装到当前用户目录（默认 %LOCALAPPDATA%\\Programs\\M18 Console），不需要管理员权限
- 开始菜单 + 桌面快捷方式；「应用和功能」里登记卸载项
- 卸载：安装目录里的 uninstall.exe（就是本程序，带 --uninstall）
- 命令行：--silent [--dir 目录] [--no-shortcut] [--no-launch]   静默安装（测试 / 批量部署用）
          --uninstall [--silent] [--purge]                     卸载；--purge 同时删除用户数据
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg
import zipfile

APP_NAME = "Stream Dock M18 控制台"
VERSION = "1.0.0"
try:                                   # 构建时会把 app/version.py 一起打进来
    from version import APP_NAME, VERSION  # type: ignore  # noqa: F811
except Exception:  # noqa: BLE001
    pass

EXE_NAME = "M18Console.exe"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\M18Console"
DEFAULT_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", "M18 Console")
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "M18Console")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def shell_folder(name: str) -> str:
    """桌面 / 开始菜单的真实路径（用户可能把桌面挪到了 D 盘或 OneDrive）。"""
    ps = f"[Environment]::GetFolderPath('{name}')"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    if name == "Desktop":
        return os.path.join(os.path.expanduser("~"), "Desktop")
    return os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")


def make_shortcut(lnk: str, target: str, workdir: str, icon: str) -> None:
    def q(v):
        return v.replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}');"
          f"$s.TargetPath='{q(target)}';$s.WorkingDirectory='{q(workdir)}';"
          f"$s.IconLocation='{q(icon)},0';$s.Description='{q(APP_NAME)}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                   timeout=30, creationflags=NO_WINDOW)


def shortcut_paths() -> list[str]:
    return [
        os.path.join(shell_folder("Programs"), f"{APP_NAME}.lnk"),
        os.path.join(shell_folder("Desktop"), f"{APP_NAME}.lnk"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                     f"{APP_NAME}.lnk"),        # 控制台里开过「开机自启」
    ]


def stop_running() -> None:
    """正在运行的控制台先退出（会顺带关掉插件进程），否则文件被占用没法覆盖 / 删除。"""
    for port in range(8642, 8654):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.6) as r:
                if b"m18-console" not in r.read():
                    continue
            origin = f"http://127.0.0.1:{port}"
            req = urllib.request.Request(f"{origin}/api/quit", b"{}",
                                         {"Content-Type": "application/json", "Origin": origin})
            urllib.request.urlopen(req, timeout=3).read()
            time.sleep(2.5)
        except Exception:  # noqa: BLE001
            continue


def dir_size_kb(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // 1024


# --------------------------------------------------------------------------- #
# 安装
# --------------------------------------------------------------------------- #
def install(target: str, desktop: bool, progress=lambda pct, msg: None) -> str:
    payload = resource("payload.zip")
    if not os.path.isfile(payload):
        raise FileNotFoundError("安装包损坏：找不到 payload.zip")
    progress(3, "正在关闭运行中的控制台…")
    stop_running()
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(payload) as zf:
        names = zf.namelist()
        for i, name in enumerate(names):
            dest = os.path.normpath(os.path.join(target, name))
            if not dest.startswith(os.path.normpath(target)):
                continue
            if name.endswith("/"):
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                for attempt in range(5):
                    try:
                        with zf.open(name) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(1)
            if i % 25 == 0:
                progress(5 + int(80 * i / max(1, len(names))), f"正在复制文件…（{i}/{len(names)}）")
    progress(87, "正在创建快捷方式…")
    exe = os.path.join(target, EXE_NAME)
    uninstaller = os.path.join(target, "uninstall.exe")      # 随程序一起打包的独立卸载程序
    make_shortcut(shortcut_paths()[0], exe, target, exe)
    if desktop:
        make_shortcut(shortcut_paths()[1], exe, target, exe)
    progress(94, "正在登记卸载信息…")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
        for name, value in (("DisplayName", APP_NAME), ("DisplayVersion", VERSION),
                            ("Publisher", "M18 Console"), ("DisplayIcon", f'"{exe}",0'),
                            ("InstallLocation", target),
                            ("UninstallString", f'"{uninstaller}" --uninstall'),
                            ("QuietUninstallString", f'"{uninstaller}" --uninstall --silent')):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, dir_size_kb(target))
    progress(100, "安装完成")
    return exe


def installed_dir() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            return winreg.QueryValueEx(k, "InstallLocation")[0]
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 卸载
# --------------------------------------------------------------------------- #
def uninstall(purge: bool) -> None:
    target = installed_dir() or os.path.dirname(os.path.abspath(sys.executable))
    stop_running()
    for lnk in shortcut_paths():
        try:
            os.remove(lnk)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
    except OSError:
        pass
    if purge:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    # uninstall.exe 本身在安装目录里、正在运行：交给一个延迟执行的 cmd 在我们退出后删目录
    if target and os.path.isfile(os.path.join(target, EXE_NAME)):
        subprocess.Popen(f'cmd /c ping 127.0.0.1 -n 4 >nul & rmdir /s /q "{target}"',
                         creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True, cwd=tempfile.gettempdir())


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
def gui_install() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} {VERSION} 安装")
    root.resizable(False, False)
    try:
        root.iconbitmap(resource("icon.ico"))
    except Exception:  # noqa: BLE001
        pass
    frm = ttk.Frame(root, padding=18)
    frm.grid()
    ttk.Label(frm, text=APP_NAME, font=("Microsoft YaHei", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(frm, text=f"版本 {VERSION} · 自研 Stream Dock M18 驱动控制台",
              foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))
    ttk.Label(frm, text="安装位置：").grid(row=2, column=0, sticky="w")
    dir_var = tk.StringVar(value=installed_dir() or DEFAULT_DIR)
    ttk.Entry(frm, textvariable=dir_var, width=52).grid(row=2, column=1, sticky="we")

    def browse():
        d = filedialog.askdirectory(initialdir=os.path.dirname(dir_var.get()))
        if d:
            dir_var.set(os.path.join(os.path.normpath(d), "M18 Console"))
    ttk.Button(frm, text="浏览…", command=browse).grid(row=2, column=2, padx=(6, 0))
    desk_var = tk.BooleanVar(value=True)
    run_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="创建桌面快捷方式", variable=desk_var).grid(row=3, column=1, sticky="w", pady=(10, 0))
    ttk.Checkbutton(frm, text="安装完成后启动", variable=run_var).grid(row=4, column=1, sticky="w")
    note = ("说明：需要 Windows 10/11 和 Microsoft Edge（系统自带）。装了官方 Stream Dock 软件时，\n"
            "还能导入官方场景、图标包和插件；两者同时运行时控制台会自动让位。")
    ttk.Label(frm, text=note, foreground="#666").grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 8))
    bar = ttk.Progressbar(frm, length=460, maximum=100)
    bar.grid(row=6, column=0, columnspan=3, sticky="we")
    status = ttk.Label(frm, text="")
    status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 8))
    btn = ttk.Button(frm, text="安装")
    btn.grid(row=8, column=2, sticky="e")

    def progress(pct, msg):
        root.after(0, lambda: (bar.configure(value=pct), status.configure(text=msg)))

    def work():
        try:
            exe = install(dir_var.get().strip() or DEFAULT_DIR, desk_var.get(), progress)
        except Exception as exc:  # noqa: BLE001
            root.after(0, lambda: (messagebox.showerror("安装失败", str(exc)), btn.configure(state="normal")))
            return

        def done():
            if run_var.get():
                subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
            messagebox.showinfo("安装完成", f"{APP_NAME} 已安装。\n开始菜单和桌面上都有快捷方式。")
            root.destroy()
        root.after(0, done)

    def start():
        btn.configure(state="disabled")
        threading.Thread(target=work, daemon=True).start()
    btn.configure(command=start)
    root.mainloop()


def gui_uninstall(silent: bool, purge: bool) -> None:
    if silent:
        uninstall(purge)
        return
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno("卸载", f"确定要卸载 {APP_NAME} 吗？"):
        return
    purge = messagebox.askyesno(
        "卸载", "是否同时删除你的配置、场景、自制插件等数据？\n\n"
                f"（位于 {DATA_DIR}；选「否」会保留，重新安装后还能继续用）")
    uninstall(purge)
    messagebox.showinfo("卸载", f"{APP_NAME} 已卸载。")


def main() -> int:
    args = sys.argv[1:]
    # 卸载程序（uninstall.exe）不带安装内容：直接双击也是卸载
    if "--uninstall" in args or not os.path.isfile(resource("payload.zip")):
        gui_uninstall("--silent" in args, "--purge" in args)
        return 0
    if "--silent" in args:
        target = args[args.index("--dir") + 1] if "--dir" in args else DEFAULT_DIR
        exe = install(target, "--no-shortcut" not in args)
        if "--no-launch" not in args:
            subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True)
        print(json.dumps({"ok": True, "exe": exe}, ensure_ascii=False))
        return 0
    gui_install()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
