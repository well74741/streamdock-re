"""完整 App 灯效集成冒烟：临时数据、假设备、真实HTTP和Edge，不启动SDK。

    python -B tools/smoke_effect_app.py
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='m18-app-effect-') as folder:
        os.environ['M18_DATA_DIR']=folder
        sys.path.insert(0,str(ROOT/'app'))
        import server
        # 只替换真实硬件入口，不替换场景/HTTP/屏幕/灯珠/Worker逻辑。
        class Device:
            connected=False
            gif_native=True
            gif_running=False
            session=1
            error='离线假设备'
            info={}
            stats={}
            firmware=product=path=''
            image_rotation=0
            parallel_writes=True         # 按并行写屏路径走（每键一条写图连接）
            def __init__(self,*_a,**_k):self.calls=[]
            def send_image_batch(self,images,colors=None,cancelled=None):
                self.calls.extend(('image',k) for k in images);return True
            def writers_idle(self,keys=None):return True
            def wait_writers_idle(self,timeout=1.0):self.calls.append(('waitIdle',));return True
            def connect(self):return False
            def check_present(self):return True
            def send_image(self,key,data):self.calls.append(('image',key));return True
            def set_leds(self,colors):self.calls.append(('leds',len(colors)));return True
            def set_key_gif(self,key,path):self.calls.append(('gif',key));return True
            def clear_key_gif(self,key):return True
            def stop_gif_loop(self):self.gif_running=False;return True
            def start_gif_loop(self):self.gif_running=True;return True
            def set_screen_brightness(self,_v):return True
            def set_led_brightness(self,_v):return True
            def refresh(self):return True
            def clear_queue(self):return True
            def clear_keys(self):return True
            def disconnect(self):pass
        server.DeviceWS=Device
        server.start_loop=lambda *_a,**_k:None
        for name in ('_auto_sync_official','_warm_gif_cache','index_official_sources','_auto_mirror_icons',
                     '_migrate_official_plugins','_seed_presets'):
            setattr(server.App,name,lambda self:None)
        server.App._detect_official=staticmethod(lambda:False)
        app=server.App()
        server.APP=app
        http=server.QuietServer(('127.0.0.1',0),server.Handler);http.daemon_threads=True
        server.PORT=http.server_address[1]
        # 测试自行持有专用端口服务，以便测试结束只关闭自己的实例。
        plugin=server.QuietServer(('127.0.0.1',0),server.PluginHandler);plugin.daemon_threads=True
        server.PLUGIN_PORT=plugin.server_address[1]
        threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (http,plugin)]
        for thread in threads:thread.start()
        base=f'http://127.0.0.1:{server.PORT}'
        browser=None
        try:
            app.start_effects(f'http://127.0.0.1:{server.PLUGIN_PORT}')
            app.device.connected=True
            made=app.create_scene('离线打击垫','effect');assert made['ok']
            assert app.apply_scene(made['name'])
            for _ in range(120):
                if app.effect_state()['status']=='running':break
                time.sleep(.05)
            assert app.effect_state()['status']=='running',app.effect_logs()
            before=app.effect_preview()
            app._on_input(18,1);app._on_input(18,0)
            time.sleep(.35)
            assert app.effect_preview()!=before,'导航按键应改变马赛克波纹画面'
            assert any(c[0]=='leds' for c in app.device.calls)
            assert app.effect_state()['parallel']
            assert len({c[1] for c in app.device.calls if c[0]=='image'})==15
            from test_gif_clock import Browser
            browser=Browser();browser.call('Page.navigate',{'url':base+'/?noevents'})
            for _ in range(120):
                if browser.js("typeof state!=='undefined' && state?.effect?.active && document.body.classList.contains('effect-scene')"):break
                time.sleep(.05)
            assert browser.js("state.effect.active && document.querySelector('#editor-body').hidden")
            assert browser.js("document.querySelector('#fx-count').textContent==='1'")
            assert browser.js("!document.querySelector('#wallpaper-gap') && !document.querySelector('#as-gap')")
            browser.js("document.querySelector('#ring-stage').scrollIntoView({block:'center'});1")
            time.sleep(.4)
            for _ in range(100):
                if browser.js("document.querySelector('#fx-board-preview').complete && document.querySelector('#fx-board-preview').naturalWidth===500"):
                    break
                time.sleep(.05)
            assert browser.js("document.querySelector('#fx-board-preview').naturalWidth===500"), browser.js("({src:document.querySelector('#fx-board-preview').src,complete:document.querySelector('#fx-board-preview').complete,effect:state.effect})")
            browser.call('Page.captureScreenshot',{'format':'png'})
            assert browser.js("document.querySelector('#fx-status').textContent.includes('并行写屏')")
            shots=os.environ.get('M18_SMOKE_SHOTS')
            # 灯效库网格 + 页面编辑区（未选键时）
            assert browser.js("document.querySelector('#fx-list').classList.contains('lib-album') && document.querySelectorAll('#fx-list .fx-cell').length===1")
            assert browser.js("!document.querySelector('#effect-pages').hidden && document.querySelector('#ep-count').textContent==='1 页'")
            # 加第二页：SSE 推送后页面编辑区自动刷新
            assert app.save_effect('第二页', app.default_effect_code().replace('v5', 'v5-第二页'), '冒烟')['ok']
            assert app.effect_scene_edit(made['name'], 'add', name='第二页')['ok']
            for _ in range(80):
                if browser.js("document.querySelector('#ep-count').textContent==='2 页' && document.querySelectorAll('#ep-strip .fx-page').length===2"):break
                time.sleep(.05)
            assert browser.js("document.querySelector('#ep-count').textContent==='2 页'"), browser.js("document.querySelector('#ep-count').textContent")
            assert app._effect_lib_running=='第二页'
            # ◀ 长按 = 上一灯效页（短按仍交给灯效互动）
            app._on_input(16,1);time.sleep(.65);app._on_input(16,0);time.sleep(.2)
            assert app._effect_lib_running=='打击垫',app._effect_lib_running
            # 选中按键：换成按键编辑（只能设动作），按键动作写进灯效场景自己的按键层
            browser.js("selectKey(3);1");time.sleep(.2)
            assert browser.js("!document.querySelector('#editor-body').hidden && document.querySelector('#effect-pages').hidden")
            assert browser.js("state.keyLayer && state.keyLayer.kind==='effect' && document.querySelector('#sel-label').textContent.includes('只能设动作')")
            assert browser.js("document.body.classList.contains('key-layer')")
            assert app.set_key_action(3, {'type':'nav','name':'返回上一场景','gesture':'double'})
            assert app._layer()['gestures'][3]=={'long':{'type':'nav','name':'下一场景'},'double':{'type':'nav','name':'返回上一场景'}}
            assert 3 not in app.key_gestures,'底层普通场景的按键不能被改'
            assert not app.set_key_image(3, __file__),'灯效场景的按键不能放图片'
            browser.js("selectKey(null);1");time.sleep(.2)
            assert browser.js("!document.querySelector('#effect-pages').hidden")
            # 打开技术文档：只验证文件写出（冒烟里不真的打开资源管理器）
            guide=app.export_effect_guide()
            assert os.path.isfile(guide) and open(guide,encoding='utf-8').read().startswith('> 给智能体')
            shot=browser.call('Page.captureScreenshot',{'format':'png'})
            if shots:
                # 人工检查用：整页截图（页面编辑区 + 左侧灯效库）
                browser.js("document.querySelector('#effect-pages').scrollIntoView({block:'center'});1");time.sleep(.3)
                shot=browser.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False})
                Path(shots,'effect-pages.png').write_bytes(base64.b64decode(shot['data']))
            time.sleep(.3)
            app.stop_effect_scene();assert not app.effect_active()
            assert ('waitIdle',) in app.device.calls,'退出前应等写图连接写完'
            assert app.active_scene==app.custom_scene_name
            print('PASS：完整App临时数据启动、灯效后台宿主、音频状态、导航波纹、15键并行写屏、24LED假发送、完整UI首次灯效预览、灯效库网格/页面编辑区/加页/长按切页/按键层编辑、退出等写完再恢复。')
        finally:
            if browser:browser.close()
            app.close_effects()
            for s in (http,plugin):s.shutdown();s.server_close()
            for t in threads:t.join(timeout=3)
            if app._save_timer:app._save_timer.cancel()


if __name__=='__main__':main()
