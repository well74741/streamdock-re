"""SDK批次只用假socket/本地socketpair，不启动SDK、不接真实设备。"""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('offline_batch_sdk', ROOT/'app/wssdk.py')
SDK = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SDK)


class Wire:
    def __init__(self): self.calls=[]; self.buffer=b''
    def sendall(self, data): self.calls.append(bytes(data)); self.buffer += data
    def recv(self, size):
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data


def decoded(wire, count):
    reader = SDK.WSClient()
    reader.sock = wire
    out=[]
    for _ in range(count):
        fin, opcode, data = reader._read_frame()
        assert fin and opcode == 1
        out.append(json.loads(data))
    return out


def jpeg():
    buffer=io.BytesIO()
    Image.new('RGB',(64,64),'red').save(buffer,'JPEG')
    return buffer.getvalue()


class BatchSdkTests(unittest.TestCase):
    def test_distinct_masked_ws_messages_share_one_sendall(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        values=[{'event':'图片','payload':{'text':'中文'}},{'event':'refresh','payload':{}}]
        client.send_json_batch(values)
        self.assertEqual(len(wire.calls),1)
        self.assertEqual(decoded(wire,2),values)

    def test_limits_fail_before_any_network_write(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        for values in ([],[{}]*33,[{'data':'x'*(513*1024)}],[None]):
            with self.assertRaises(ValueError):client.send_json_batch(values)
        self.assertEqual(wire.calls,[])

    def test_device_batches_images_paired_leds_then_one_refresh(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        device=SDK.DeviceWS(lambda *_:None);device._ws=client;device.path='test-device';device.session=1
        order=(8,11,5,1,15,6,10,13,3,7,9,12,14,2,4)
        images={key:jpeg() for key in order};colors=[[10,20,30] for _ in range(24)]
        started=time.perf_counter()
        self.assertTrue(device.send_image_batch(images,colors))
        elapsed=(time.perf_counter()-started)*1000
        self.assertEqual(len(wire.calls),1)
        messages=decoded(wire,17)
        self.assertEqual([m['event'] for m in messages],['setKeyImgData']*15+['setSingleLedColor','refresh'])
        self.assertEqual([m['payload']['keyIndex'] for m in messages[:15]],list(order))
        for message in messages[:15]:
            self.assertEqual(base64.b64decode(message['payload']['imgData'].split(',',1)[1]),jpeg())
        self.assertEqual(messages[15]['payload']['colors'],colors)
        self.assertTrue(all(m['path']=='test-device' for m in messages))
        print(f'15键+配对LED+refresh：一次socket写入，编码/提交{elapsed:.2f}ms（假socket，无硬件回执）。')

    def test_clear_leds_resets_per_led_cache_and_global_color(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        device=SDK.DeviceWS(lambda *_:None);device._ws=client;device.path='test-device'
        self.assertTrue(device.clear_leds())
        self.assertEqual(len(wire.calls),1)
        messages=decoded(wire,2)
        self.assertEqual([m['event'] for m in messages],['setSingleLedColor','setLEDColor'])
        self.assertEqual(messages[0]['payload']['colors'],[[0,0,0] for _ in range(24)])
        self.assertEqual(messages[1]['payload'],{'r':0,'g':0,'b':0})
        self.assertFalse(any('Brightness' in m['event'] for m in messages))

    def test_cancelled_or_invalid_batch_does_not_send(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        device=SDK.DeviceWS(lambda *_:None);device._ws=client;device.path='test-device'
        self.assertFalse(device.send_image_batch({1:jpeg()},cancelled=lambda:True))
        self.assertFalse(device.send_image_batch({16:jpeg()}))
        self.assertFalse(device.send_image_batch({1:jpeg()},[[0,0,0]]))
        self.assertEqual(wire.calls,[])

    def parallel_device(self):
        """主连接 + 每键假写图连接；_ack(key) 模拟 SDK 对该连接 gifLoopStatus 的回执。"""
        main=Wire();client=SDK.WSClient();client.sock=main
        device=SDK.DeviceWS(lambda *_:None);device._ws=client;device.path='test-device';device.session=3
        device.gif_native=True
        wires={}
        def writer(key):
            if key not in wires:
                ws=SDK.WSClient();ws.sock=Wire();wires[key]=ws
                device._writers[key]=ws
            return wires[key]
        device._writer=writer
        def ack(key):
            with device._writer_cond:
                device._writer_pending[key]-=1
                device._writer_cond.notify_all()
        return device,main,wires,ack

    def test_parallel_batch_uses_one_writer_per_key_and_tracks_receipts(self):
        device,main,wires,ack=self.parallel_device()
        order=(8,11,5,1,15,6,10,13,3,7,9,12,14,2,4)
        self.assertTrue(device.send_image_batch({k:jpeg() for k in order}))
        self.assertEqual(main.calls,[])                                  # 主连接（按键/灯珠）不被图片占用
        self.assertEqual(sorted(wires),list(range(1,16)))
        for key,ws in wires.items():
            self.assertEqual(len(ws.sock.calls),1)
            msgs=decoded(ws.sock,3)
            self.assertEqual([m['event'] for m in msgs],['setKeyImgData','refresh','gifLoopStatus'])
            self.assertEqual(msgs[0]['payload']['keyIndex'],key)
        self.assertFalse(device.writers_idle())                          # 回执前不放行下一批
        for key in range(1,8): ack(key)
        self.assertFalse(device.writers_idle())
        for key in range(8,16): ack(key)
        self.assertTrue(device.writers_idle())
        device._writer_pending[1]=1;device._writer_since[1]=time.monotonic()-2
        self.assertTrue(device.writers_idle())                           # 1秒无回执不永久卡住

    def test_every_key_command_keeps_order_on_its_own_connection(self):
        device,main,wires,ack=self.parallel_device()
        self.assertTrue(device.clear_key_gif(4))
        self.assertTrue(device.send_image(4,jpeg()))
        self.assertTrue(device.set_key_gif(4,'C:/x/a.gif'))
        self.assertEqual(main.calls,[])
        events=[m['event'] for m in decoded(wires[4].sock,7)]
        self.assertEqual(events,['clearKeyGif','gifLoopStatus','setKeyImgData','refresh','gifLoopStatus',
                                 'setKeyGif','gifLoopStatus'])

    def test_send_image_backpressure_waits_for_receipt(self):
        device,main,wires,ack=self.parallel_device()
        device.send_image(2,jpeg());device.send_image(2,jpeg())         # 一张在写、一张排队
        threading.Timer(0.15,ack,args=(2,)).start()
        started=time.perf_counter()
        device.send_image(2,jpeg())                                      # 第三张要等回执
        self.assertGreater(time.perf_counter()-started,0.1)
        started=time.perf_counter()
        device.send_image(3,jpeg())                                      # 别的键不受影响
        self.assertLess(time.perf_counter()-started,0.05)

    def test_global_commands_wait_for_key_connections_first(self):
        device,main,wires,ack=self.parallel_device()
        device.send_image(5,jpeg())
        def finish():
            ack(5)
            device._gif_status.set()
        threading.Timer(0.1,finish).start()
        started=time.perf_counter()
        self.assertTrue(device.clear_keys())
        self.assertGreater(time.perf_counter()-started,0.08)            # 先等 5 号键写完
        self.assertEqual([m['event'] for m in decoded(main,2)],['clearAllIcon','gifLoopStatus'])

    def test_parallel_needs_sdk_receipts(self):
        wire=Wire();client=SDK.WSClient();client.sock=wire
        device=SDK.DeviceWS(lambda *_:None);device._ws=client;device.path='test-device'
        device.gif_native=False
        device._writer=lambda key:self.fail('不支持回执时不应开写图连接')
        self.assertTrue(device.send_image_batch({1:jpeg()}))
        self.assertTrue(device.send_image(2,jpeg()))
        self.assertEqual([m['event'] for m in decoded(wire,3)],['setKeyImgData','refresh','setKeyImgData'])

    @unittest.skipUnless(os.name=='nt','Windows专用socket发送超时')
    def test_send_timeout_does_not_change_receive_timeout(self):
        left,right=socket.socketpair()
        try:
            client=SDK.WSClient();client.sock=left
            before=left.getsockopt(socket.SOL_SOCKET,socket.SO_SNDTIMEO)
            client.send_json_batch([{'event':'refresh','payload':{}}])
            self.assertTrue(right.recv(4096))
            self.assertEqual(left.getsockopt(socket.SOL_SOCKET,socket.SO_SNDTIMEO),before)
            self.assertIsNone(left.gettimeout());self.assertIsNone(right.gettimeout())
        finally:
            left.close();right.close()


if __name__=='__main__':unittest.main()
