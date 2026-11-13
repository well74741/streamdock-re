#!/usr/bin/env python3
"""
m18_light.py — Stream Dock M18 灯带控制器 (M0 原型)

直接通过 Mirabox 官方设备 SDK 的 transport.dll 控制 M18 的 24 颗 RGB 灯珠，
不依赖官方 StreamDock 软件（也不占用它的界面）。

用法:
    python m18_light.py --list
    python m18_light.py --effect solid --color 00aaff
    python m18_light.py --effect rainbow --fps 25
    python m18_light.py --effect breathe --color ff3b30
    python m18_light.py --effect chase --color 00ff88 --speed 6
    python m18_light.py --effect gradient --from ff0000 --to 0000ff
    python m18_light.py --effect off

注意:
  * 与官方 StreamDock 软件同时运行时，双方都会写灯光，会互相抢（本工具退出时会 reset）。
  * 仅操作 LED，不写按键图片、不写开机 Logo、不改配置。
"""
from __future__ import annotations

import argparse
import colorsys
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_SRC = os.path.join(HERE, "refs", "devicesdk", "Python-SDK", "src")
sys.path.insert(0, SDK_SRC)

from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI  # noqa: E402

LED_COUNT = 24
# M18 / M18E
CANDIDATES = [(0x6603, 0x1009), (0x6603, 0x1012), (0x5548, 0x1000)]


def find_device(tr: LibUSBHIDAPI):
    for vid, pid in CANDIDATES:
        for d in tr.enumerate_devices(vendor_id=vid, product_id=pid):
            return vid, pid, d
    return None


def hex_rgb(s: str):
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"颜色需为 6 位十六进制, 收到 {s!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def hsv(h: float, s: float, v: float):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return [int(r * 255), int(g * 255), int(b * 255)]


def frame(effect: str, t: float, args):
    n = LED_COUNT
    if effect == "off":
        return [[0, 0, 0]] * n

    if effect == "solid":
        return [list(args.color)] * n

    if effect == "rainbow":
        return [hsv(t * 0.12 + i / n, 1.0, 1.0) for i in range(n)]

    if effect == "breathe":
        v = 0.15 + 0.85 * (0.5 + 0.5 * __import__("math").sin(t * 2.0 * args.speed))
        return [[int(c * v) for c in args.color]] * n

    if effect == "chase":
        out = []
        for i in range(n):
            d = (i - t * args.speed * 4) % n
            k = max(0.0, 1.0 - min(d, n - d) / 2.5)
            out.append([int(c * k) for c in args.color])
        return out

    if effect == "gradient":
        out = []
        for i in range(n):
            k = i / (n - 1)
            out.append([int(args.frm[j] + (args.to[j] - args.frm[j]) * k) for j in range(3)])
        return out

    raise ValueError(effect)


def main() -> int:
    p = argparse.ArgumentParser(description="Stream Dock M18 light strip controller")
    p.add_argument("--list", action="store_true", help="只列出检测到的设备")
    p.add_argument("--effect", default="rainbow",
                   choices=["solid", "rainbow", "breathe", "chase", "gradient", "off"])
    p.add_argument("--color", type=hex_rgb, default=(0, 160, 255))
    p.add_argument("--from", dest="frm", type=hex_rgb, default=(255, 0, 0))
    p.add_argument("--to", dest="to", type=hex_rgb, default=(0, 0, 255))
    p.add_argument("--brightness", type=int, default=100, help="0-255")
    p.add_argument("--fps", type=float, default=20.0)
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--duration", type=float, default=0.0, help="秒, 0 表示一直运行")
    p.add_argument("--no-reset", action="store_true", help="退出时保留最后状态")
    args = p.parse_args()

    tr = LibUSBHIDAPI()
    found = find_device(tr)
    if not found:
        print("未找到 M18 设备（VID 6603 / PID 1009 或 1012）", file=sys.stderr)
        return 2
    vid, pid, info = found
    print(f"设备: VID_{vid:04X} PID_{pid:04X} serial={info.get('serial_number')}")
    if args.list:
        return 0

    if not tr.open(bytes(info["path"], "utf-8")):
        print(f"打开失败: {tr.get_last_error()}", file=sys.stderr)
        return 3
    tr.set_report_size(513, 1025, 0)
    print("固件:", tr.get_firmware_version())

    try:
        tr.set_led_brightness(max(0, min(255, args.brightness)))
        t0 = time.time()
        tick = 1.0 / max(1.0, args.fps)
        nxt = time.time()
        while True:
            t = time.time() - t0
            if args.duration and t > args.duration:
                break
            tr.set_single_led_color(frame(args.effect, t, args))
            nxt += tick
            sleep = nxt - time.time()
            if sleep > 0:
                time.sleep(sleep)
            else:
                nxt = time.time()
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        if not args.no_reset:
            tr.reset_led_color()
        tr.close()
        print("已关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
