"""灯效库、多页灯效场景、相册/灯效按键层、HTTP/MCP集成：临时数据和假设备，不加载core或启动真实SDK。"""
from __future__ import annotations
import ast
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from test_effects import FX, FakeApp, ROOT, jpeg
from test_fixed_gap import load_selected_class


def module_ns(filename, ns):
    """执行模块（去掉 from core / __future__ 导入），常量与类都进 ns。"""
    path = ROOT / 'app' / filename
    tree = ast.parse(path.read_text(encoding='utf-8'))
    body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and n.module in ('core', '__future__'))]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), ns)
    return ns


def classes(folder):
    ns = {"json": json, "os": os, "tempfile": tempfile, "SCENE_DIR": str(folder), "DATA_DIR": str(folder / 'data'),
          "UI_DIR": str(ROOT / 'app/ui'), "log": lambda *_: None}
    module_ns('app_effect_scenes.py', ns)
    effects = ns['EffectScenesMixin']
    keys = module_ns('app_scene_keys.py', dict(ns))['SceneKeysMixin']
    ns.update({"time": time, "threading": threading, "LEGACY_ALBUM_NAMES": (),
               "base64": base64, "zipfile": zipfile, "io": io, "hashlib": __import__('hashlib'),
               "CUSTOM_PREFIX": 'com.m18.custom.', "CUSTOM_PLUGIN_DIR": str(folder/'plugins'),
               "UPLOAD_DIR": str(folder/'uploads'), "ICON_DIR": str(folder/'icons')})
    methods = {'_scene_path', '_scene_file_data', '_canonical_scene', '_scene_has_content', 'apply_scene',
               'create_scene', 'list_scenes', 'current_scene', '_snapshot_custom_scene', 'rename_scene',
               'delete_scene', 'export_scene', 'import_scene_package', '_fresh_contexts', '_scene_data'}
    scenes = load_selected_class('app_scenes.py', 'ScenesMixin', methods, ns)

    class App(effects, keys, scenes, FakeApp):
        SCENE_KEYS = ('pages', 'page')
        SCENE_PKG_VERSION = 1
        def __init__(self):
            super().__init__()
            self._save_lock = threading.RLock()
            self.custom_scene_name = '自定义'
            self.active_scene = self.custom_scene_name
            self.scene_rev = 0
            self.in_standby = False
            self.preview_mode = 'wallpaper'
            self.album_scene_name = ''
            self._album_scene = {}
            self._album_return = None
            self._auto_applying = False
            self._applying_scene = False
            self.input_muted = False
            self.key_actions = {1: {'type': 'launch', 'path': '禁止执行'}}
            self.key_gestures = {}
            self.key_previews = {}
            self._album_generation = 0
            self.config_saves = 0
            self._effect_return = None
            self.gestures, self.runs = [], []
            self._write('__custom__', {'pages':[{'images':{'1':'x.png'}}], 'page':0})
        def _write(self, name, data):
            (folder/(name+'.json')).write_text(json.dumps(data), encoding='utf-8')
        def _read(self, name):
            return json.loads((folder/(name+'.json')).read_text(encoding='utf-8'))
        def _save_config(self): self.config_saves += 1
        def export_config(self): return {'pages':[{'images':{'1':'x.png'}}], 'page':0}
        def _apply_scene_file(self, _path): self.restores += 1; return True
        def official_scene_names(self): return set()
        def _scene_flags(self): return {}
        def _scene_order(self): return []
        def album_active(self): return self.in_standby and self.preview_mode == 'album'
        def is_album_scene(self, name): return (self._scene_file_data(self._scene_path(name)) or {}).get('sceneType')=='album'
        def _play_album_scene(self, name):
            self.active_scene=name; self.album_scene_name=name; self.preview_mode='album'; self.in_standby=True
            self._layer_reload(); return True
        def _cancel_album_push(self): self._album_generation += 1
        def _push_album(self): return True
        def _refresh_plugins(self): pass
        def reveal_in_explorer(self, path): self.revealed = path; return os.path.isfile(path)
        def _gesture(self, key, state, g): self.gestures.append((key, state, dict(g)))
        def _run_action(self, key, act=None): self.runs.append((key, act if act is not None else self._key_action(key)))
        def _plugin_key(self, *a): pass
        def snapshot(self):
            return {'keyCount':15, 'device':{'connected':True}, 'activeScene': self.active_scene,
                    'effect':self.effect_state(), 'keyImages':{}, 'keyActions':{}, 'keyActionTypes':{}}
    return App


SIMPLE = 'M18FX.on("frame",()=>{});'


class EffectSceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='m18-effect-scene-')
        self.folder = Path(self.temp.name)
        self.app = classes(self.folder)()
        self.app._upgrade_effect_templates()     # 首次：建灯效库并放入打击垫示例
    def tearDown(self):
        self.app.close_effects()
        self.temp.cleanup()
    def create(self):
        r=self.app.create_scene('', 'effect'); self.assertTrue(r['ok']); return r['name']

    # -- 灯效库 ----------------------------------------------------------------
    def test_library_seeded_once_and_crud(self):
        self.assertEqual([it['name'] for it in self.app.list_effects()], ['打击垫'])
        self.assertIn('M18FX', self.app.get_effect('打击垫')['code'])
        self.assertTrue(self.app.save_effect('新灯效', SIMPLE, '说明')['ok'])
        self.assertFalse(self.app.save_effect('新灯效', SIMPLE)['ok'])            # 同名需明确 original
        self.assertTrue(self.app.save_effect('新灯效', SIMPLE + '//2', original='新灯效')['ok'])
        self.assertFalse(self.app.save_effect('大代码', 'x' * 131073)['ok'])
        self.assertTrue(self.app.delete_effect('打击垫')['ok'])
        self.app._upgrade_effect_templates()                                   # 删掉的示例不会再补回
        self.assertEqual([it['name'] for it in self.app.list_effects()], ['新灯效'])
        self.assertTrue(self.app.reorder_effects(['新灯效'])['ok'])

    def test_migrates_old_scene_code_into_library_and_upgrades_untouched_templates(self):
        old = (ROOT/'tests/fixtures/effect-pad-v1.js').read_text(encoding='utf-8')
        v4 = (ROOT/'tests/fixtures/effect-pad-v4.js').read_text(encoding='utf-8')
        self.app._write('旧打击垫', {'sceneType':'effect', 'effect':{'code':old, 'description':'保留说明'},
                                  'apps':['保留关联'], 'pages':[], 'page':0})
        self.app._write('自己修改', {'sceneType':'effect', 'effect':{'code':old+'\n// 自己修改'}, 'pages':[], 'page':0})
        self.app._write('星空版', {'sceneType':'effect', 'effect':{'code':v4.replace(chr(10), chr(13)+chr(10))},
                                'pages':[], 'page':0})
        self.assertEqual(self.app._upgrade_effect_templates(), 2)              # v1、v4 未编辑 → 升级
        data = self.app._read('旧打击垫')
        self.assertEqual(data['effect'], {'items':['旧打击垫'], 'index':0, 'description':'保留说明'})
        self.assertEqual(data['apps'], ['保留关联'])
        self.assertEqual(self.app.get_effect('旧打击垫')['code'], self.app.default_effect_code())
        self.assertEqual(self.app.get_effect('自己修改')['code'], old+'\n// 自己修改')
        self.assertEqual(self.app.get_effect('星空版')['code'], self.app.default_effect_code())
        self.assertEqual(self.app._upgrade_effect_templates(), 0)

    def test_preset_scene_with_embedded_library_installs_effects(self):
        self.app._write('随包灯效', {'sceneType':'effect', 'pages':[], 'page':0,
                                  'effect':{'items':['星河','打击垫'], 'index':1, 'description':'预设',
                                            'library':{'星河':{'code':SIMPLE+'//星河','description':'随包'},
                                                       '打击垫':{'code':self.app.get_effect('打击垫')['code']}}}})
        self.assertGreaterEqual(self.app._migrate_effect_library(), 1)
        data = self.app._read('随包灯效')
        self.assertNotIn('library', data['effect'])
        self.assertEqual(data['effect']['items'], ['星河','打击垫'])          # 同名同代码复用，不另起「打击垫 2」
        self.assertEqual(self.app.get_effect('星河')['code'], SIMPLE+'//星河')
        self.assertEqual(self.app.get_effect_scene('随包灯效')['index'], 1)

    # -- 多页灯效场景 ------------------------------------------------------------
    def test_create_scene_has_pad_page_and_key_layer_defaults(self):
        one=self.create(); two=self.create()
        self.assertEqual((one,two),('灯效 1','灯效 2'))
        self.assertFalse(self.app.effect_active())
        info=self.app.get_effect_scene(one)
        self.assertEqual([it['name'] for it in info['items']], ['打击垫'])
        page=self.app._read(one)['pages'][0]
        self.assertEqual(page['gestures']['1'], {'long': {'type':'nav','name':'下一场景'}})
        self.assertEqual(page['gestures']['16'], {'long': {'type':'nav','name':'上一灯效页'}})
        self.assertEqual(page['gestures']['18'], {'long': {'type':'nav','name':'下一灯效页'}})
        self.assertEqual(page.get('actions'), {})                              # 短按 / 双击都空
        rows=self.app.list_scenes()
        self.assertEqual(sum(r['sceneType']=='effect' for r in rows),2)
        self.assertFalse(self.app.create_scene('未知','wrong')['ok'])

    def test_pages_add_move_remove_goto_and_long_press_step(self):
        self.app.save_effect('甲', SIMPLE); self.app.save_effect('乙', SIMPLE + '//乙')
        scene=self.create()
        self.app.effect_scene_edit(scene, 'add', name='甲')
        self.app.effect_scene_edit(scene, 'add', name='乙', index=0)
        names=lambda: [it['name'] for it in self.app.get_effect_scene(scene)['items']]
        self.assertEqual(names(), ['乙','打击垫','甲'])
        self.assertFalse(self.app.effect_scene_edit(scene, 'add', name='不存在')['ok'])
        self.assertTrue(self.app.apply_scene(scene)); self.assertTrue(self.app.effect_active())
        self.assertEqual(self.app._effect_lib_running, '乙')                   # 加入时停在新加的页
        gen=self.app.effect_state()['revision']
        self.app.effect_scene_edit(scene, 'move', **{'from':0, 'to':2})        # 当前页跟着走，不重启
        self.assertEqual(names(), ['打击垫','甲','乙'])
        self.assertEqual(self.app.get_effect_scene(scene)['index'], 2)
        self.assertEqual(self.app.effect_state()['revision'], gen)
        self.assertTrue(self.app.effect_page_step(1))                          # ▶ 长按：循环到第一页
        self.assertEqual(self.app._effect_lib_running, '打击垫')
        self.assertTrue(self.app.effect_page_step(-1))
        self.assertEqual(self.app._effect_lib_running, '乙')
        self.app.effect_scene_edit(scene, 'goto', index=1)
        self.assertEqual(self.app._effect_lib_running, '甲')
        self.app.effect_scene_edit(scene, 'remove', index=1)
        self.assertEqual(names(), ['打击垫','乙'])
        self.assertIn(self.app._effect_lib_running, ('打击垫','乙'))
        self.assertTrue(self.app.save_effect('乙二', SIMPLE, original='乙')['ok'])   # 改名同步引用
        self.assertEqual(names(), ['打击垫','乙二'])
        self.assertTrue(self.app.delete_effect('乙二')['ok'])                       # 删除移除引用
        self.assertEqual(names(), ['打击垫'])

    def test_empty_scene_runs_blank_placeholder(self):
        scene=self.create(); self.app.effect_scene_edit(scene, 'remove', index=0)
        self.assertTrue(self.app.apply_scene(scene))
        self.assertEqual(self.app._effect_lib_running, '')
        self.assertTrue(self.app.effect_active())

    def test_trial_run_returns_to_previous_scene(self):
        self.app.save_effect('试', SIMPLE)
        self.assertTrue(self.app.run_effect('试')['trial'])
        self.assertTrue(self.app.effect_active()); self.assertEqual(self.app.active_scene, '自定义')
        self.assertEqual(self.app._layer_scene_name(), '')                      # 试运行没有按键层
        self.assertTrue(self.app.stop_effect_scene()['ok'])
        self.assertFalse(self.app.effect_active()); self.assertEqual(self.app.active_scene, '自定义')
        self.assertFalse(self.app.run_effect('不存在')['ok'])

    def test_hot_reload_stop_restores_actions_and_scene(self):
        name=self.create(); actions=copy.deepcopy(self.app.key_actions)
        self.assertTrue(self.app.apply_scene(name)); self.assertTrue(self.app.effect_active())
        generation=self.app.effect_state()['revision']
        self.assertTrue(self.app.save_effect('打击垫', SIMPLE, original='打击垫')['ok'])
        self.assertGreater(self.app.effect_state()['revision'],generation)
        self.assertEqual(self.app.key_actions,actions)
        self.assertTrue(self.app.stop_effect_scene()['ok']); self.assertFalse(self.app.effect_active())
        self.assertEqual(self.app.active_scene,'自定义'); self.assertEqual(self.app.key_actions,actions)

    def test_snapshot_does_not_replace_effect_definition(self):
        name=self.create(); self.app.apply_scene(name)
        path=self.folder/(name+'.json'); before=path.read_bytes()
        self.app._snapshot_custom_scene(); self.assertEqual(path.read_bytes(),before)

    def test_failure_returns_metadata_and_reconnect_does_not_restart_bad_code(self):
        name=self.create(); self.app.apply_scene(name)
        self.app.stop_effect('timeout',restore=True)
        self.assertEqual(self.app.active_scene,'自定义'); self.assertFalse(self.app.effect_active())
        self.app._restore_display(); self.assertFalse(self.app.effect_active())

    def test_rename_active_and_collision_protection(self):
        name=self.create(); other=self.create(); self.app.apply_scene(name)
        self.assertFalse(self.app.rename_scene(name,other))
        self.assertTrue(self.app.rename_scene(name,'改名'))
        self.assertEqual(self.app.active_scene,'改名')
        self.assertEqual(self.app._layer()['scene'], '改名')

    def test_delete_active_restores_and_keeps_other_scenes(self):
        name=self.create(); other=self.create(); self.app.apply_scene(name)
        self.assertTrue(self.app.delete_scene(name)); self.assertFalse(self.app.effect_active())
        self.assertEqual(self.app.active_scene,'自定义'); self.assertTrue(self.app.is_effect_scene(other))

    def test_export_import_carries_library_code_without_executing(self):
        self.app.save_effect('带走', SIMPLE + '//带走')
        name=self.create(); self.app.effect_scene_edit(name, 'add', name='带走')
        package=self.app.export_scene(name); self.assertTrue(package['ok'])
        self.app.delete_effect('带走')
        r=self.app.import_scene_package(package['data']); self.assertTrue(r['ok'])
        self.assertEqual([it['name'] for it in self.app.get_effect_scene(r['name'])['items']], ['打击垫','带走'])
        self.assertEqual(self.app.get_effect('带走')['code'], SIMPLE + '//带走')
        self.assertFalse(self.app.effect_active())

    def test_reveal_guide_writes_fixed_doc_and_example(self):
        r=self.app.reveal_effect_guide()
        self.assertTrue(r['ok'])
        doc=Path(r['path']).read_text(encoding='utf-8')
        self.assertTrue(doc.startswith('> 给智能体'))
        self.assertIn('M18FX', doc)
        self.assertTrue((Path(r['folder'])/'示例-打击垫.js').is_file())
        self.assertEqual(self.app.revealed, r['path'])

    # -- 相册 / 灯效场景的按键层 -------------------------------------------------
    def test_album_layer_defaults_edit_and_isolation(self):
        self.app._write('相册', {'sceneType':'album','album':{'items':['a.png']},
                               'pages':[{'actions':{'16':{'type':'nav','name':'上一页'},
                                                    '17':{'type':'hotkey','keys':'ctrl+a'}}}], 'page':0})
        self.app.apply_scene('相册')
        layer=self.app._layer()
        self.assertEqual(layer['actions'][3], {'type':'nav','name':'下一场景'})    # 旧相册补上屏幕键默认
        self.assertEqual(layer['actions'][17], {'type':'hotkey','keys':'ctrl+a'})  # 已有设置不覆盖
        self.assertEqual(layer['actions'][18], {'type':'nav','name':'下一页'})
        self.assertTrue(self.app._layer_set(3, {'type':'hotkey','keys':'f5'}))
        self.assertTrue(self.app._layer_set(3, {'type':'hotkey','keys':'f6'}, 'long'))
        self.assertTrue(self.app._layer_set(5, None))                              # 清掉后不再补回
        self.assertFalse(self.app._layer_set(4, {'type':'folder'}))
        self.app._layer_reload()
        page=self.app._read('相册')['pages'][0]
        self.assertEqual(page['actions']['3'], {'type':'hotkey','keys':'f5'})
        self.assertEqual(page['gestures']['3'], {'long':{'type':'hotkey','keys':'f6'}})
        self.assertNotIn('5', page['actions'])
        self.assertNotIn(5, self.app._layer()['actions'])
        self.assertEqual(self.app.key_actions, {1: {'type':'launch','path':'禁止执行'}})  # 底层不动
        self.app._layer_input(3, 1)                                                # 有长按 → 手势判定
        self.assertEqual(self.app.gestures[-1][0], 3)
        self.app._layer_input(7, 1)                                                # 只有短按 → 立即执行
        self.assertEqual(self.app.runs[-1], (7, {'type':'nav','name':'下一场景'}))
        self.app._layer_input(7, 0)
        self.assertEqual(len(self.app.runs), 1)

    def test_effect_layer_short_press_empty_long_press_next(self):
        name=self.create(); self.app.apply_scene(name)
        self.assertEqual(self.app._layer_snapshot()['kind'], 'effect')
        self.app._layer_input(2, 1)
        self.assertEqual(self.app.gestures[-1], (2, 1, {'long':{'type':'nav','name':'下一场景'}}))
        self.app._layer_input(17, 1)                                               # 17 默认什么都不做
        self.assertEqual(self.app.runs, [])
        self.assertEqual(len(self.app.gestures), 1)
        self.app.stop_effect_scene()
        self.assertIsNone(self.app._layer())


class EffectHttpMcpTests(EffectSceneTests):
    def setUp(self):
        super().setUp()
        source=ROOT/'app/server.py'; tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        ns={'BaseHTTPRequestHandler':BaseHTTPRequestHandler, 'APP':self.app,'os':os,'json':json,
            'time':time,'threading':threading,'urllib':urllib,'queue':queue,'re':re,
            'UI_DIR':str(ROOT/'app/ui'),'MAX_EFFECT_BODY':FX.MAX_EFFECT_BODY,'effect_asset':FX.effect_asset,
            'PLUGIN_UI_FILES':{'/plugin-host.html','/pi-frame.html'}, 'SHUTTING_DOWN':False,
            'EDITION':'测试版','VERSION':'test','DATA_DIR':str(self.folder),'core':__import__('types').SimpleNamespace(KEY_GAP=27)}
        for name in ('Handler','PluginHandler'):
            node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==name)
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
        self.main=ThreadingHTTPServer(('127.0.0.1',0),ns['Handler'])
        self.host=ThreadingHTTPServer(('127.0.0.1',0),ns['PluginHandler'])
        ns['PORT']=self.main.server_port;ns['PLUGIN_PORT']=self.host.server_port
        self.base=f'http://127.0.0.1:{self.main.server_port}';self.origin=f'http://127.0.0.1:{self.host.server_port}'
        self.app._effect_origin=self.origin
        self.threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (self.main,self.host)]
        for t in self.threads:t.start()
    def tearDown(self):
        for server in (self.main,self.host): server.shutdown();server.server_close()
        for thread in self.threads:thread.join(timeout=2)
        super().tearDown()
    def post(self,path,body):
        req=urllib.request.Request(self.base+path,json.dumps(body).encode(),{'Content-Type':'application/json','Origin':self.base})
        return json.load(urllib.request.urlopen(req,timeout=5))
    def test_http_library_pages_run_input_and_host_authorization(self):
        saved=self.post('/api/effects',{'action':'save','name':'HTTP灯效','code':SIMPLE})
        self.assertTrue(saved['ok'])
        self.assertIn('HTTP灯效',[it['name'] for it in self.post('/api/effects',{'action':'list'})['items']])
        scene=self.post('/api/scenes',{'create':'HTTP场景','kind':'effect'})
        self.assertTrue(scene['ok'])
        added=self.post('/api/effects',{'action':'pageAdd','scene':'HTTP场景','name':'HTTP灯效'})
        self.assertEqual([it['name'] for it in added['items']],['打击垫','HTTP灯效'])
        moved=self.post('/api/effects',{'action':'pageMove','scene':'HTTP场景','from':1,'to':0})
        self.assertEqual([it['name'] for it in moved['items']],['HTTP灯效','打击垫'])
        self.assertTrue(self.post('/api/scenes',{'apply':'HTTP场景'})['ok'])
        self.assertTrue(self.post('/api/effects/input',{'key':18,'down':True})['ok'])
        self.assertFalse(self.post('/api/effects/input',{'key':18,'down':1})['ok'])
        with self.assertRaises(urllib.error.HTTPError) as bad: urllib.request.urlopen(self.origin+'/effects/session')
        self.assertEqual(bad.exception.code,403)
        req=urllib.request.Request(self.origin+'/effects/poll',headers={'Authorization':'Bearer '+self.app._effect_token})
        data=json.load(urllib.request.urlopen(req)); self.assertEqual(data['events'][-1]['key'],18)
        req=urllib.request.Request(self.origin+'/effects/effect-worker.js')
        with urllib.request.urlopen(req) as r:self.assertIn("connect-src 'none'",r.headers['Content-Security-Policy'])
        for path in ('/effect-host.html','/effect-worker.js','/effect-runtime.js'):
            with self.assertRaises(urllib.error.HTTPError) as bad: urllib.request.urlopen(self.base+path)
            self.assertEqual(bad.exception.code,404)
        with self.assertRaises(urllib.error.HTTPError) as bad: urllib.request.urlopen(self.base+'/api/effects/thumb/'+urllib.parse.quote('HTTP灯效'))
        self.assertEqual(bad.exception.code,404)                                   # 还没截过缩略图
        self.assertTrue(self.post('/api/effects',{'action':'stop'})['ok'])
        self.assertTrue(self.post('/api/effects',{'action':'delete','name':'HTTP灯效'})['ok'])
    def test_mcp_stdio_full_effect_crud_and_input(self):
        calls=[('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'test','version':'1'}}),
          ('tools/list',{}),('tools/call',{'name':'m18_effect_api','arguments':{}}),
          ('tools/call',{'name':'m18_save_effect','arguments':{'name':'MCP灯效','code':SIMPLE}}),
          ('tools/call',{'name':'m18_run_effect','arguments':{'name':'MCP灯效'}}),
          ('tools/call',{'name':'m18_effect_input','arguments':{'key':17,'down':True}}),
          ('tools/call',{'name':'m18_get_effect','arguments':{'name':'MCP灯效'}}),
          ('tools/call',{'name':'m18_effect_logs','arguments':{}}),
          ('tools/call',{'name':'m18_stop_effect','arguments':{}}),
          ('tools/call',{'name':'m18_effect_scene','arguments':{'scene':'灯效 1','op':'get'}}),
          ('tools/call',{'name':'m18_delete_effect','arguments':{'name':'MCP灯效'}})]
        self.create()
        messages='\n'.join(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':args}) for i,(method,args) in enumerate(calls,1))+'\n'
        result=subprocess.run([sys.executable,'-B',str(ROOT/'app/mcp_server.py')],input=messages,text=True,encoding='utf-8',
                              capture_output=True,timeout=20,env=dict(os.environ,M18_URL=self.base))
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies),len(calls))
        self.assertFalse(any('error' in r or r.get('result',{}).get('isError') for r in replies),replies)
        tools={t['name']:t for t in replies[1]['result']['tools']}
        self.assertEqual(tools['m18_effect_input']['inputSchema']['properties']['key']['maximum'],18)
        self.assertIn('m18_effect_scene', tools)
        self.assertIn('M18FX',replies[2]['result']['content'][0]['text'])
        self.assertIn('打击垫', replies[9]['result']['content'][0]['text'])
        self.assertFalse(self.app.effect_active()); self.assertFalse(self.app.get_effect('MCP灯效')['ok'])


if __name__=='__main__':unittest.main()
