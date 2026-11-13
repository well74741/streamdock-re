"""Read-only probe of the connected Stream Dock M18 via the official device SDK transport.

Does NOT write images, lighting, or config. Only: enumerate -> open -> read firmware -> close.
"""
import sys, os, json

SDK_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "refs", "devicesdk", "Python-SDK", "src")
sys.path.insert(0, SDK_SRC)

from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI
from StreamDock.ProductIDs import g_products

tr = LibUSBHIDAPI()
print("SDK transport loaded OK")

# 1) enumerate every VID/PID pair the SDK knows about
found = []
for vid, pid, cls in g_products:
    devs = tr.enumerate_devices(vendor_id=vid, product_id=pid)
    for d in devs:
        found.append((vid, pid, cls, d))

print(f"known pairs: {len(g_products)}, devices found: {len(found)}")
for vid, pid, cls, d in found:
    print(f"  VID_{vid:04X} PID_{pid:04X} class={getattr(cls, '__name__', cls)} "
          f"serial={d.get('serial_number')!r} path={d.get('path')}")

if not found:
    raise SystemExit("no device matched")

vid, pid, cls, info = found[0]
ok = tr.open(bytes(info["path"], "utf-8"))
print("open() ->", ok)
if not ok:
    print("last error:", tr.get_last_error())
    raise SystemExit(1)

try:
    tr.set_report_size(513, 1025, 0)
    fw = tr.get_firmware_version()
    print("firmware:", repr(fw))
finally:
    tr.close()
    print("closed")
