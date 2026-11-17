"""实际浏览器灯效编辑/点击测试，后台是离线假设备，绝不连接真实SDK。"""
import base64
from http.server import ThreadingHTTPServer
import io
from pathlib import Path
import re
import sys
import threading
import time
import urllib.parse

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_effect_scenes import EffectHttpMcpTests, jpeg
from test_gif_clock import Browser


OUTLINE_NODES = "[...document.querySelectorAll('.ring-board .cell, #ring .dot, #ring .ring-btn')]"
GEOMETRY = f"""(()=>{{
  const wrap = document.querySelector('#ring-wrap').getBoundingClientRect();
  return {OUTLINE_NODES}.map(el=>{{
    const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return [el.className, el.dataset.key, el.style.left, el.style.top,
      ...[r.x-wrap.x,r.y-wrap.y,r.width,r.height].map(v=>Math.round(v*100)/100),
      s.transform,s.margin,s.borderTopWidth,s.pointerEvents];
  }});
}})()"""


def assert_outlines(browser, normal_geometry):
    """真实截图与命中检测：边框在图上方，四向摆放不改变原有几何。"""
    for rotation in (0, 90, 180, 270):
        browser.js(f"state.rotation={rotation};applyRotation();1")
        time.sleep(.3)
        browser.js("document.querySelector('#ring-stage').scrollIntoView({block:'center'});1")
        time.sleep(.1)
        assert browser.js(GEOMETRY) == normal_geometry[rotation], f'{rotation}°几何/命中区域变化'
        result = browser.js(f"""(()=>{{
          const nodes={OUTLINE_NODES}, image=document.querySelector('#fx-board-preview');
          const style=getComputedStyle(image);
          const items=nodes.map(el=>{{
            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
            const face=getComputedStyle(el,'::before');
            return {{kind:el.classList.contains('cell')?'screen':el.classList.contains('dot')?'led':'nav',
              key:Number(el.dataset.key), x:r.x,y:r.y,w:r.width,h:r.height,
              visibility:s.visibility,opacity:s.opacity,border:s.borderTopColor,
              borderWidth:parseFloat(s.borderTopWidth),borderStyle:s.borderTopStyle,
              background:s.backgroundColor,image:s.backgroundImage,color:s.color,symbol:face.content,
              hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.closest('.cell,.dot,.ring-btn')===el,
              childrenHidden:[...el.children].every(child=>getComputedStyle(child).visibility==='hidden')}};
          }});
          return {{items, preview:{{loaded:image.complete&&image.naturalWidth===500&&!image.hidden,
            pointer:style.pointerEvents,z:Number(style.zIndex)}},
            boardZ:Number(getComputedStyle(document.querySelector('.ring-board')).zIndex),
            ringZ:Number(getComputedStyle(document.querySelector('#ring')).zIndex)}};
        }})()""")
        assert result['preview']['loaded'], '整板预览必须真实加载，不能只检查空层'
        assert result['preview']['pointer'] == 'none'
        assert result['boardZ'] > result['preview']['z'] and result['ringZ'] > result['preview']['z'], result
        assert [sum(it['kind'] == kind for it in result['items']) for kind in ('screen','led','nav')] == [15,24,3]
        shot = browser.screenshot()
        for item in result['items']:
            assert item['visibility'] == 'visible' and float(item['opacity']) > 0, item
            assert item['borderWidth'] >= 1 and item['borderStyle'] == 'solid' and item['hit'], item
            assert item['border'] not in ('transparent','rgba(0, 0, 0, 0)'), item
            # 不是只有计算样式：42个结构的上边缘在真实合成截图中均须有亮轮廓。
            x, y = round(item['x']+item['w']/2), round(item['y'])
            edge = [shot.getpixel((px,py)) for px in range(x-3,x+4) for py in range(y-2,y+3)
                    if 0 <= px < shot.width and 0 <= py < shot.height]
            assert edge and max(max(pixel) for pixel in edge) > 110, (rotation,item,'边缘被画面覆盖')
            if item['kind'] == 'screen':
                assert item['background'] == 'rgba(0, 0, 0, 0)' and item['image'] == 'none' and item['childrenHidden'], item
                center = shot.getpixel((x,round(item['y']+item['h']/2)))
                assert max(abs(a-b) for a,b in zip(center,(20,30,48))) <= 5, (rotation,item,center,'动画被遮挡')
            elif item['kind'] == 'led':
                assert item['background'] == 'rgb(0, 0, 0)', item
                assert shot.getpixel((x,round(item['y']+item['h']/2))) == (0,0,0), item
            else:
                assert item['symbol'] in ('"◀"','"⌂"','"▶"') and item['color'] == 'rgb(237, 245, 255)', item
    browser.js("state.rotation=0;applyRotation();1")
    time.sleep(.3)


def main():
    case = EffectHttpMcpTests('test_http_save_apply_input_and_host_authorization')
    case.setUp()
    case.main.shutdown(); case.main.server_close(); case.threads[0].join(timeout=2)
    original = case.main.RequestHandlerClass
    # 同一真实Handler，仅app.js去掉boot，测试自己决定初始状态，避免启动额外线程/插件。
    class Handler(original):
        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                pass  # 测试关闭浏览器时主动断开空闲HTTP连接
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path == '/app.js':
                code = (ROOT / 'app/ui/app.js').read_text(encoding='utf-8-sig')
                data = re.sub(r'\bboot\(\);\s*$', '', code).encode('utf-8')
                self.send_response(200); self.send_header('Content-Type','text/javascript')
                self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                return
            super().do_GET()
    case.main = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    # 原类方法的globals就是路由命名空间；保持同源访问规则。
    original.do_GET.__globals__['PORT']=case.main.server_port
    case.base=f'http://127.0.0.1:{case.main.server_port}'
    case.threads[0]=threading.Thread(target=case.main.serve_forever,daemon=True);case.threads[0].start()
    browser=Browser()
    try:
        browser.call('Page.navigate',{'url':case.base+'/'})
        for _ in range(100):
            if browser.js("typeof M18EffectUI === 'object' && typeof buildBoard === 'function'"): break
            time.sleep(.05)
        assert browser.js("""(()=>{
          buildBoard(); buildRing(); bindRingPainting(); M18EffectUI.init();
          state={activeScene:'自定义',effect:{},album:{items:[],active:false}};
          sceneItems=[]; selectedScene=null; selectedKey=null;
          window.m18ReloadScenes=async()=>{
            sceneItems=(await post('/api/scenes',{})).items; M18EffectUI.renderLibrary(sceneItems);renderEditor();
          };
          refreshState=async()=>{
            state.effect=await post('/api/effects',{action:'status'});
            state.activeScene=state.effect.active?state.effect.name:'自定义';
            M18EffectUI.renderState(state.effect);renderEditor();
          };
          const sections=[...document.querySelectorAll('.side-sec')].map(e=>e.dataset.sec);
          return sections.indexOf('effects')>sections.indexOf('library') && sections.indexOf('effects')<sections.indexOf('custom');
        })()""")
        assert browser.js("!document.querySelector('#fx-pad-side')")
        normal_geometry = {}
        for rotation in (0,90,180,270):
            browser.js(f"state.rotation={rotation};applyRotation();1")
            time.sleep(.3)
            normal_geometry[rotation] = browser.js(GEOMETRY)
        browser.js("state.rotation=0;applyRotation();1")
        assert browser.js("""(async()=>{
          for(let k=1;k<=18;k++) draftsCount(k);
          if(Object.keys(draftsAll).length) return false;
          draftsAll[1]={}; draftsFor(2,true).press=viewToDraft(gestureView(2,'press'));
          const ok=await pendingDraftsPrompt();
          return ok && !document.querySelector('.confirm-modal') && !Object.keys(draftsAll).length;
        })()"""), '只点选/空草稿/相同内容不能弹保存提示'
        browser.js("""state.keyActions={4:'打开'};state.keyActionTypes={4:'launch'};state.keyActionValues={4:'old.exe'};
          draftsFor(4,true).press={...viewToDraft(gestureView(4,'press')),value:'new.exe'};
          window.draftChoice=null;pendingDraftsPrompt().then(v=>window.draftChoice=v);1""")
        assert browser.js("!!document.querySelector('.confirm-modal')"), '真实未保存改动仍须提示'
        browser.js("document.querySelector('.confirm-modal [data-v=\"\"]').click();delete draftsAll[4];1")
        assert browser.js("window.draftChoice===false")
        browser.js("document.querySelector('#fx-new').click();1")
        for _ in range(100):
            if browser.js("document.querySelector('#fx-code').value.includes('M18FX')"):break
            time.sleep(.05)
        assert browser.js("document.querySelector('#fx-name').value")== '灯效 1'
        browser.js("document.querySelector('#fx-save').click();1")
        for _ in range(100):
            if case.app.is_effect_scene('灯效 1'):break
            time.sleep(.05)
        assert case.app.is_effect_scene('灯效 1')
        assert browser.js("document.querySelector('#fx-code').value")==case.app.get_effect_scene('灯效 1')['code']
        browser.js("document.querySelector('#fx-apply').click();1")
        for _ in range(100):
            if case.app.effect_active() and browser.js("document.body.classList.contains('effect-scene')"):break
            time.sleep(.05)
        assert case.app.effect_active()
        assert browser.js("document.querySelector('#editor-body').hidden")
        # 纯色假预览没有轮廓：所有结构必须由UI独立叠加，而不是混入设备画面。
        case.app.receive_effect_frame(case.app._effect_token, {'seq':1,'colors':[[0,0,0]]*24,
            'preview':jpeg(color=(20,30,48),size=(500,370))})
        browser.js("ledBrightness=100;paintDots(Array.from({length:24},()=>[0,0,0]));refreshState()")
        for _ in range(100):
            if browser.js("document.querySelector('#fx-board-preview').complete && document.querySelector('#fx-board-preview').naturalWidth===500"):break
            time.sleep(.05)
        assert_outlines(browser, normal_geometry)
        before_custom=browser.js('JSON.stringify(localCustom)')
        led_pos=browser.js("(()=>{const r=document.querySelector('#ring .dot').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
        browser.call('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,'x':led_pos[0],'y':led_pos[1]})
        browser.call('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,'x':led_pos[0],'y':led_pos[1]})
        assert browser.js('JSON.stringify(localCustom)')==before_custom, '灯效预览点击灯珠不能意外修改全局灯色'
        # 灯珠通过现有paintDots更新，边框不强制覆盖活跃颜色/发光。
        assert browser.js("""(async()=>{
          const src=document.querySelector('#fx-board-preview').src;
          paintDots(Array.from({length:24},()=>[18,164,232]));
          await new Promise(resolve=>setTimeout(resolve,150)); // 等现有80ms灯色过渡结束
          return [...document.querySelectorAll('#ring .dot')].every(el=>
            getComputedStyle(el).backgroundColor==='rgb(18, 164, 232)' && el.style.boxShadow!=='none')
            && document.querySelector('#fx-board-preview').src===src;
        })()"""), '活跃灯珠须保留实时颜色，不能写进效果画布'
        shot=browser.screenshot()
        for pos in browser.js("[...document.querySelectorAll('#ring .dot')].map(el=>{const r=el.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})"):
            assert shot.getpixel(tuple(round(v) for v in pos)) == (18,164,232), '灯珠实时颜色被预览覆盖'
        browser.js("paintDots(Array.from({length:24},()=>[0,0,0]));1")
        next_preview=base64.b64decode(jpeg(color=(56,20,42),size=(500,370)).split(',')[1])
        case.app.receive_effect_frame(case.app._effect_token, {'seq':2,'colors':[[0,0,0]]*24,
            'preview':'data:image/jpeg;base64,'+base64.b64encode(next_preview).decode('ascii')})
        browser.js("refreshState()")
        for _ in range(100):
            if browser.js("document.querySelector('#fx-board-preview').complete && document.querySelector('#fx-board-preview').src.endsWith('-2')"):break
            time.sleep(.05)
        shot=browser.screenshot()
        for pos in browser.js("[...document.querySelectorAll('.ring-board .cell')].map(el=>{const r=el.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})"):
            color=shot.getpixel(tuple(round(v) for v in pos))
            assert max(abs(a-b) for a,b in zip(color,(56,20,42))) <= 5, ('新动画帧被轮廓层遮挡',color)
        assert case.app.effect_preview()==next_preview, 'UI不能污染设备预览画面'
        # 15屏和3导航键全部真实点击，下/上进入灯效，不能选中/交换/执行底层动作。
        before=case.app.restores
        for key in range(1,19):
            selector=f".cell[data-key='{key}']" if key<=15 else f".ring-btn[data-key='{key}']"
            pos=browser.js(f"(()=>{{const r=document.querySelector({selector!r}).getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}})()")
            browser.call('Input.dispatchMouseEvent',{'type':'mousePressed','button':'left','clickCount':1,'x':pos[0],'y':pos[1]})
            browser.call('Input.dispatchMouseEvent',{'type':'mouseReleased','button':'left','clickCount':1,'x':pos[0],'y':pos[1]})
            time.sleep(.1)
        expected=[(key,down) for key in range(1,19) for down in (True,False)]
        for _ in range(100):
            if len(case.app._effect_events)>=len(expected):break
            time.sleep(.05)
        events=list(case.app._effect_events)
        assert [(e['key'],e['down']) for e in events]==expected,events
        assert case.app.restores==before
        assert browser.js("selectedKey===null && !swapSource"), '灯效点击不能进入普通按键编辑/交换'
        assert browser.js("pendingDraftsPrompt()") is True
        browser.js("window.savedEffectCode=document.querySelector('#fx-code').value;1")
        browser.js("document.querySelector('#fx-code').value+='\\n//保留草稿';document.querySelector('#fx-code').dispatchEvent(new Event('input',{bubbles:true}));1")
        # 刷新场景目录不能吃掉未保存代码。
        browser.js("m18ReloadScenes()")
        assert browser.js("document.querySelector('#fx-code').value.endsWith('//保留草稿')")
        browser.js("M18EffectUI.guard();1")
        assert browser.js("!!document.querySelector('.confirm-modal')")
        browser.js("document.querySelector('.confirm-modal [data-act=\"no\"]').click();1")
        browser.js("document.querySelector('#fx-code').value=window.savedEffectCode;document.querySelector('#fx-code').dispatchEvent(new Event('input',{bubbles:true}));1")
        assert browser.js("pendingDraftsPrompt()") is True, '改回原代码也不应提示'
        assert browser.js("!document.querySelector('.confirm-modal')")
        browser.js("document.querySelector('#fx-stop').click();1")
        for _ in range(100):
            if not case.app.effect_active() and case.app.active_scene=='自定义':break
            time.sleep(.05)
        assert not case.app.effect_active()
        assert case.app.active_scene=='自定义'
        browser.js("refreshState()")
        assert browser.js("""(()=>{
          const image=document.querySelector('#fx-board-preview');
          return image.hidden&&!image.hasAttribute('src')
            && getComputedStyle(document.querySelector('.ring-board')).zIndex==='auto'
            && getComputedStyle(document.querySelector('#ring')).zIndex==='auto'
            && getComputedStyle(document.querySelector('#ring .dot .idx')).visibility==='visible'
            && getComputedStyle(document.querySelector('.ring-btn')).color==='rgb(20, 69, 127)';
        })()"""), '退出灯效后必须恢复普通预览样式'
        # 新建弹窗真的包含第三种类型和默认名。
        browser.js("newSceneDialog();document.querySelector('[data-kind=effect]').click();1")
        assert browser.js("document.querySelector('#scene-new-name').value")== '灯效 2'
        assert browser.js("document.querySelector('#scene-new-hint').textContent.includes('屏幕')")
        browser.js("document.querySelector('.confirm-modal [data-act=no]').click();1")
        print('PASS：真实Edge临时假HTTP；15屏+24灯珠+3导航轮廓/叠层/截图/命中，四向旋转几何不变；黑色空闲与实时灯色、动画无遮挡；18键下/上36事件且无底层动作；编辑/保存/应用/退出、草稿与普通样式恢复。')
    finally:
        browser.close();case.tearDown()


if __name__=='__main__':main()
