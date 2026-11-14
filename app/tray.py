"""Windows 系统托盘图标（纯 ctypes，不用第三方库）。

控制台关掉窗口以后服务仍在后台跑，托盘图标用来：
  · 双击 / 「打开控制台」→ 重新打开界面窗口
  · 「退出控制台」→ 结束服务与设备连接

只在 Windows 上有效；其它平台 start() 直接返回 False。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_TRAY = 0x8000 + 17          # WM_APP + 17
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1

MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
IMAGE_ICON = 1
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001

command_open, command_exit = 1, 2


if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32

    def _proto(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    # 不声明原型时 ctypes 一律按 32 位 int 传参，lparam 一大就 OverflowError，
    # 表现是回调里每收一条消息就抛一次 "int too long to convert"。
    _proto(user32.DefWindowProcW, ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
           wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT,
           wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.SendMessageW, ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
           wintypes.WPARAM, wintypes.LPARAM)
    _proto(user32.GetWindowLongW, wintypes.LONG, wintypes.HWND, ctypes.c_int)
    _proto(user32.SetWindowLongW, wintypes.LONG, wintypes.HWND, ctypes.c_int, wintypes.LONG)

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        # 只用到 V2 的字段；cbSize 按本结构大小填，Windows 会按版本处理
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class Tray:
    """托盘图标 + 气泡提示。start() 之后后台线程跑消息循环。"""

    def __init__(self, tooltip: str, on_open, on_exit, icon_path: str = ""):
        self.tooltip = tooltip[:120]
        self.on_open = on_open
        self.on_exit = on_exit
        self.icon_path = icon_path
        self._hwnd = None
        self._nid = None
        self._thread = None
        self._proc = None            # 必须留着引用，否则回调会被回收
        self._ready = threading.Event()
        self._title = "M18ConsoleTray"

    # -- 对外接口 ----------------------------------------------------------- #
    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        return self._ready.wait(6)

    def alive(self) -> bool:
        return bool(self._hwnd)

    def balloon(self, title: str, text: str) -> None:
        if not self._nid:
            return
        try:
            self._nid.uFlags = NIF_INFO
            self._nid.szInfoTitle = title[:60]
            self._nid.szInfo = text[:250]
            self._nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        try:
            if self._nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._hwnd:
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
        except Exception:  # noqa: BLE001
            pass

    # -- 内部 --------------------------------------------------------------- #
    def _load_icon(self):
        for path in (self.icon_path,
                     os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "icon.ico")):
            if path and os.path.isfile(path):
                h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return h
        try:
            h = shell32.ExtractIconW(kernel32.GetModuleHandleW(None), sys.executable, 0)
            return h or None
        except Exception:  # noqa: BLE001
            return None

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                code = lparam & 0xFFFF
                if code in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._call(self.on_open)
                elif code in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_COMMAND:
                cmd = wparam & 0xFFFF
                if cmd == command_open:
                    self._call(self.on_open)
                elif cmd == command_exit:
                    self._call(self.on_exit)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _call(fn) -> None:
        if not fn:
            return
        try:
            # 回调里不要干重活，扔给线程
            threading.Thread(target=fn, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    def _menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, command_open, "打开控制台")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, command_exit, "退出控制台")
        user32.SetMenuDefaultItem(hmenu, command_open, 0)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 必须先把自己设成前台窗口，否则点菜单外面菜单不会消失
        user32.SetForegroundWindow(self._hwnd)
        cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, 0x0000, 0, 0)     # WM_NULL，让菜单彻底关掉
        user32.DestroyMenu(hmenu)
        if cmd == command_open:
            self._call(self.on_open)
        elif cmd == command_exit:
            self._call(self.on_exit)

    def _run(self) -> None:
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._on_message)
            wc = WNDCLASSW()
            wc.style = CS_HREDRAW | CS_VREDRAW
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self._title
            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = kernel32.GetLastError()
                if err != 1410:                      # 1410 = 类已注册
                    self._ready.set()
                    return
            hwnd = user32.CreateWindowExW(0, self._title, self._title, 0, 0, 0, 0, 0,
                                          None, None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = hwnd
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._load_icon()
            nid.szTip = self.tooltip
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._hwnd = None
                self._ready.set()
                return
            self._nid = nid
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001
            self._ready.set()
            return
        finally:
            try:
                if self._nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:  # noqa: BLE001
                pass
            self._hwnd = None
