"""默认预设：音频波形铺满三排屏幕，静音时为直线；LED与马赛克同色，空闲关闭。只执行仓库示例。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from test_effects import FX, ROOT


@unittest.skipUnless(shutil.which('node'), '未安装Node，示例绘图模型测试跳过')
class EffectPadTests(unittest.TestCase):
    def test_central_wave_and_led_colors(self):
        size = min(740 * .5 / (3 + 2 * .12), 1000 * .74 / (5 + 4 * .12))
        config = {'keys':[{'key':k, 'x':x, 'y':y, 'size':size, 'screen':k<=15}
                          for k,(x,y) in FX.KEY_POINTS.items()],
                  'leds':[{'index':i,'x':x,'y':y} for i,(x,y) in enumerate(FX.LED_POINTS)]}
        script = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const cfg=JSON.parse(process.argv[1]), source=fs.readFileSync(process.argv[2],'utf8');
const handlers={}, lines=[], rects=[]; let path=[], submitted;
const ctx={fillStyle:'',strokeStyle:'',lineWidth:1,
 fillRect(x,y,w,h){rects.push({x,y,w,h,color:this.fillStyle})},
 strokeRect(){},beginPath(){path=[]},moveTo(x,y){path.push([x,y])},lineTo(x,y){path.push([x,y])},
 stroke(){lines.push({color:this.strokeStyle,path:[...path]})}};
const api={ctx,width:1000,height:740,...cfg,
 audio:{available:true,status:'ok',level:.7,bands:Array(8).fill(.5)},
 on(name,fn){handlers[name]=fn},render(colors){submitted=colors},log(){}};
vm.runInNewContext(source,{M18FX:api});
function draw(time){lines.length=0;rects.length=0;handlers.frame({time,dt:.05,frame:1})}
draw(0);
assert(submitted.every(rgb=>rgb.every(v=>v===0)),'没有扩散时LED必须全黑，不能有音频/常亮底色');
const waves=lines.filter(line=>line.color==='#38bfe6');
assert.strictEqual(waves.length,1,'主音频波形只有一条');
const center=cfg.keys.find(p=>p.key===8).y, top=cfg.keys.find(p=>p.key===13).y, bottom=cfg.keys.find(p=>p.key===3).y;
const ys=waves[0].path.map(p=>p[1]);
const half=cfg.keys[0].size/2;
assert(Math.min(...ys)<top+half+4,'有声音时波形要够到顶排屏幕');
assert(Math.max(...ys)>bottom-half-4,'有声音时波形要够到底排屏幕');
assert(Math.min(...ys)>top-half&&Math.max(...ys)<bottom+half,'振幅收敛在三排屏幕之内');
const xs=waves[0].path.map(p=>p[0]);
assert(xs[0]<cfg.keys.find(p=>p.key===11).x && xs.at(-1)>cfg.keys.find(p=>p.key===15).x,'横向穿过五列');
let crossings=0;for(let i=1;i<ys.length;i++) if((ys[i-1]-center)*(ys[i]-center)<0) crossings++;
assert(crossings>=14&&crossings<=40,'波峰密度（实际过零 '+crossings+' 次）');
assert(lines.length>=4,'三股叠加 + 光晕');
const starRects=t=>{draw(t);return rects.filter(r=>/^rgba\((255,214,150|170,205,255|235,245,255),/.test(r.color)&&r.w<20&&r.h<20)};
const s1=starRects(0.3), s2=starRects(1.1);
assert(s1.length>=60,'背景应撒满星星（实际 '+s1.length+'）');
const alpha=r=>Number(r.color.match(/,([\d.]+)\)$/)[1]);
assert(s1.some(r=>alpha(r)>0.8)&&s1.some(r=>alpha(r)<0.4),'星星有明有暗');
assert(s1.map(alpha).join()!==s2.map(alpha).join(),'星星随时间闪烁');
for(const r of s1){const k=cfg.keys.filter(p=>p.screen).some(p=>Math.abs(r.x+r.w/2-p.x)<p.size/2+12&&Math.abs(r.y+r.h/2-p.y)<p.size/2+12);assert(k,'星星应落在屏幕上');}
api.audio={available:true,status:'silent',level:0,bands:Array(8).fill(0)};
for(let t=1;t<4;t+=.05) draw(t);
assert(lines.filter(l=>l.color==='#38bfe6')[0].path.every(p=>Math.abs(p[1]-center)<0.5),'静音（衰减后）是直线');
api.audio={available:true,status:'ok',level:.7,bands:Array(8).fill(.5)};
const key=cfg.keys.find(p=>p.key===18);
handlers.key({key:18,down:true,x:key.x,y:key.y,time:0});draw(.4);
assert(submitted.some(rgb=>Math.max(...rgb)>40),'按导航键应产生扩散');
const ledRects=rects.slice(-24);
for(let i=0;i<24;i++){
 const painted=ledRects[i].color.match(/\d+/g).map(Number);
 assert.deepStrictEqual(painted,Array.from(submitted[i]),'画布中灯珠颜色与提交到设备的颜色应一致');
}
draw(2.5);
assert(submitted.every(rgb=>rgb.every(v=>v===0)),'马赛克渐隐结束后LED必须关闭');
console.log('星空闪烁、三排音频波形、静音直线；空闲/结束LED关闭；按键扩散与LED提交颜色一致。');
"""
        result = subprocess.run(['node','-e',script,json.dumps(config),str(ROOT/'app/ui/effect-pad.js')],
                                cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)


if __name__=='__main__':unittest.main()
