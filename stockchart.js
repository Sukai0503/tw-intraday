/* 共用個股彈窗：分時 / 日K（lightweight-charts，十字線可拖曳）。用法：StockChart.open('2330') */
(function(){
const SRC=(document.currentScript&&document.currentScript.src)||location.href;
const BASE=SRC.slice(0,SRC.lastIndexOf('/')+1);
const API='https://api.github.com/repos/Sukai0503/tw-intraday/contents/';
const UP='#ff4d5e',DN='#16c79a',AVG='#ff9f43';
let LWp=null,charts=[],cur=null,view='intra',rowProvider=null,groupName=null;
const cache={};
function loadLW(){if(window.LightweightCharts)return Promise.resolve();return LWp||(LWp=new Promise((res,rej)=>{const s=document.createElement('script');s.src=BASE+'vendor/lightweight-charts.standalone.production.js';s.onload=res;s.onerror=rej;document.head.appendChild(s)}))}
async function getJSON(name,maxAge,apiFirst){const c=cache[name];if(c&&Date.now()-c.t<maxAge)return c.v;let v=null;
 if(apiFirst){try{const r=await fetch(API+name+'?ref=data',{cache:'no-store',headers:{Accept:'application/vnd.github.raw'}});if(r.ok)v=await r.json()}catch(e){}}
 if(!v){try{const r=await fetch(BASE+name+(apiFirst?'?t='+Date.now():''),{cache:apiFirst?'no-store':'no-cache'});if(r.ok)v=await r.json()}catch(e){}}
 if(v)cache[name]={t:Date.now(),v};return v}
async function getRow(code){if(rowProvider){const r=rowProvider(code);if(r&&r.row)return r}
 const d=await getJSON('data.json',110000,true);if(!d||!d.heat)return null;const r=d.heat.stocks.find(x=>x[0]===code);return r?{row:r,date:d.date,groups:d.heat.groups,asof:d.heat.asof}:{row:null,date:d.date}}
const css=`.sc-m{position:fixed;inset:0;background:#000b;z-index:1000;display:none;align-items:center;justify-content:center;padding:8px;font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif}
.sc-p{background:#0d1a2e;color:#e8eaed;border:1px solid #17c8d8;border-radius:12px;padding:12px;width:min(680px,100%);max-height:94vh;overflow:auto;box-shadow:0 8px 30px #000c}
.sc-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}.sc-x{color:#8b929c;font-size:22px;text-decoration:none;line-height:1;cursor:pointer;background:none;border:0}
.sc-nm{font-size:14px;color:#9fb3c8}.sc-nm b{color:#fff;font-size:16px}.sc-big{font-size:19px;font-weight:700;margin:2px 0}.sc-big small{font-size:13px;color:#8b929c;font-weight:400;margin-left:6px}
.sc-up{color:${UP}}.sc-dn{color:${DN}}.sc-note{font-size:11.5px;color:#8b929c;margin:2px 0 6px}
.sc-tabs{display:flex;gap:6px;margin:6px 0}.sc-tabs button{flex:1;background:#232733;color:#8b929c;border:0;border-radius:8px;padding:7px 0;font-size:14px;cursor:pointer}.sc-tabs button.on{background:#3a4152;color:#fff}
.sc-info{font-size:12px;color:#cfd3d8;min-height:34px;line-height:1.5;font-variant-numeric:tabular-nums}.sc-info span{margin-right:8px;white-space:nowrap}.sc-info i{font-style:normal;color:#8b929c}
.sc-ch{width:100%;height:330px;touch-action:pan-y}.sc-msg{padding:40px 0;text-align:center;color:#8b929c}.sc-ld{background:#ffd400;color:#222;font-size:10px;border-radius:3px;padding:0 4px;margin-left:4px}`;
function ensure(){if(document.getElementById('sc-m'))return;const st=document.createElement('style');st.textContent=css;document.head.appendChild(st);
 const m=document.createElement('div');m.id='sc-m';m.className='sc-m';m.innerHTML=`<div class="sc-p"><div class="sc-top"><div id="sc-h"></div><button class="sc-x" id="sc-x">✕</button></div><div class="sc-tabs" id="sc-tabs"><button data-v="intra">分時</button><button data-v="day">日K</button></div><div class="sc-note" id="sc-note"></div><div class="sc-info" id="sc-info"></div><div class="sc-ch" id="sc-ch"></div></div>`;
 document.body.appendChild(m);m.addEventListener('click',e=>{if(e.target===m)close()});document.getElementById('sc-x').onclick=close;
 document.getElementById('sc-tabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;view=b.dataset.v;draw()};
 addEventListener('keydown',e=>{if(e.key==='Escape')close()});let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{if(m.style.display==='flex')draw()},250)})}
function close(){const m=document.getElementById('sc-m');if(m)m.style.display='none';destroy()}
function destroy(){charts.forEach(c=>{try{c.remove()}catch(e){}});charts=[]}
const f2=v=>v==null||isNaN(v)?'-':(Math.abs(v)>=1000?v.toLocaleString('en-US',{maximumFractionDigits:2}):(+v).toFixed(2));
const fv=v=>v==null?'-':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1000?(v/1000).toFixed(2)+'K':String(Math.round(v));
const sg=v=>(v>0?'+':'')+v;
function ts(date,hm){const [h,m]=hm.split(':').map(Number);return Date.UTC(+date.slice(0,4),+date.slice(4,6)-1,+date.slice(6,8),h,m)/1000}
function hmOf(t){const d=new Date(t*1000);return String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')}
function inSession(date){const n=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Taipei'})),m=n.getHours()*60+n.getMinutes();const ds=n.getFullYear()+String(n.getMonth()+1).padStart(2,'0')+String(n.getDate()).padStart(2,'0');return n.getDay()>0&&n.getDay()<6&&m>=540&&m<810&&date===ds}
function baseOpts(el,timeFmt){const L=window.LightweightCharts;return{width:el.clientWidth,height:el.clientHeight,layout:{background:{type:'solid',color:'#0d1a2e'},textColor:'#9aa4b2',fontSize:11},
 grid:{vertLines:{color:'#1c2638'},horzLines:{color:'#1c2638'}},rightPriceScale:{borderColor:'#2a3447'},timeScale:{borderColor:'#2a3447'},
 crosshair:{mode:L.CrosshairMode.Magnet,vertLine:{style:L.LineStyle.Dashed,color:'#9aa4b2',labelBackgroundColor:'#3a4152'},horzLine:{style:L.LineStyle.Dashed,color:'#9aa4b2',labelBackgroundColor:'#3a4152'}},
 localization:timeFmt?{locale:'zh-TW',timeFormatter:timeFmt}:{locale:'zh-TW'}}}
// 手指拖曳＝移動十字線（不捲動圖）
function touchCrosshair(el,chart,series,valueAt,onMove){let on=false;
 const mv=e=>{const t=e.touches[0];if(!t)return;const r=el.getBoundingClientRect(),x=t.clientX-r.left;const time=chart.timeScale().coordinateToTime(x);if(time==null)return;const v=valueAt(time);if(v!=null){try{chart.setCrosshairPosition(v,time,series)}catch(err){}onMove&&onMove(time)}};
 el.addEventListener('touchstart',e=>{on=true;mv(e)},{passive:true});
 el.addEventListener('touchmove',e=>{if(!on)return;const t=e.touches[0];mv(e)},{passive:true});
 el.addEventListener('touchend',()=>{on=false},{passive:true});el.addEventListener('touchcancel',()=>{on=false},{passive:true})}
async function open(code,opts){ensure();cur=String(code).match(/\d{4,6}/)?String(code).match(/\d{4,6}/)[0]:String(code);view=(opts&&opts.view)||'intra';
 document.getElementById('sc-m').style.display='flex';await draw()}
async function draw(){const code=cur;destroy();const h=document.getElementById('sc-h'),note=document.getElementById('sc-note'),info=document.getElementById('sc-info'),el=document.getElementById('sc-ch');
 [...document.querySelectorAll('#sc-tabs button')].forEach(b=>b.classList.toggle('on',b.dataset.v===view));
 el.innerHTML='<div class="sc-msg">載入中…</div>';info.innerHTML='';
 let lwOk=true;try{await loadLW()}catch(e){lwOk=false}
 const R=await getRow(code);if(cur!==code)return;const s=R&&R.row;const date=(R&&R.date)||'';
 const live=inSession(date);
 if(s){const up=s[9]>0,dn=s[9]<0,c=up?'sc-up':dn?'sc-dn':'';const grp=R.groups?R.groups[s[2]]:'';
  h.innerHTML=`<div class="sc-nm"><b>${s[1]}</b> ${s[0]}${grp?'・'+grp:''}${s[13]?'<span class="sc-ld">龍頭</span>':''}</div><div class="sc-big ${c}">${live?'成交':'收盤'} ${f2(s[8])} ${up?'▲':dn?'▼':''} ${sg(+s[9].toFixed(2))} (${sg(s[4].toFixed(2))}%)<small>${date.slice(0,4)}/${date.slice(4,6)}/${date.slice(6)}${live&&R.asof?' '+R.asof:''}</small></div>`}
 else h.innerHTML=`<div class="sc-nm"><b>${code}</b></div>`;
 if(!lwOk){el.innerHTML='<div class="sc-msg">圖表元件載入失敗</div>';return}
 if(view==='intra')return drawIntra(code,s,date,el,info,note);return drawDay(code,s,date,el,info,note)}
async function drawIntra(code,s,date,el,info,note){const L=window.LightweightCharts;
 note.textContent='分時：盤中約每 2 分鐘一點（早段可能為 5 分鐘回補）；均價線＝成交量加權均價（估算）；拖曳圖表看各時點';
 const I=await getJSON('intraday.json',110000,true);if(cur!==code||view!=='intra')return;
 if(!I||!I.p||!I.p[code]){el.innerHTML='<div class="sc-msg">此檔暫無分時資料</div>';return}
 const y=I.y[code]||(s?s[8]-s[9]:null);const T=I.t.slice(),P=I.p[code].slice(),V=(I.v&&I.v[code]||[]).slice();
 const pts=[],vol=[],avg=[];let cpv=0,cv=0,pv=0,pp=null;
 for(let i=0;i<T.length;i++){const p=P[i];if(p==null)continue;const t=ts(I.date,T[i]);const v=V.length?Math.max(0,(V[i]||0)-pv):0;pv=V.length?(V[i]||pv):0;
  cpv+=p*v;cv+=v;pts.push({time:t,value:p});avg.push({time:t,value:cv>0?cpv/cv:p});vol.push({time:t,value:v,color:(pp==null||p>=pp)?UP+'99':DN+'99'});pp=p}
 if(!pts.length){el.innerHTML='<div class="sc-msg">此檔暫無分時資料</div>';return}
 const ws=[];const t0=ts(I.date,'09:00'),tE=ts(I.date,'13:30');if(pts[0].time>t0)ws.push({time:t0});const full=[...ws,...pts];for(let t=pts.at(-1).time+120;t<=tE;t+=120)full.push({time:t});if(full.at(-1).time<tE)full.push({time:tE});
 el.innerHTML='';const ch=L.createChart(el,{...baseOpts(el,t=>hmOf(t)),handleScroll:false,handleScale:false,
  timeScale:{borderColor:'#2a3447',timeVisible:true,secondsVisible:false,fixLeftEdge:true,fixRightEdge:true,tickMarkFormatter:t=>hmOf(t)}});charts.push(ch);
 const last=pts.at(-1).value;
 const base=ch.addBaselineSeries({baseValue:{type:'price',price:y},topLineColor:UP,topFillColor1:'rgba(255,77,94,0.30)',topFillColor2:'rgba(255,77,94,0.04)',bottomLineColor:DN,bottomFillColor1:'rgba(22,199,154,0.04)',bottomFillColor2:'rgba(22,199,154,0.30)',lineWidth:2,
  priceLineVisible:true,priceLineColor:last>=y?UP:DN,priceLineStyle:L.LineStyle.Dotted,lastValueVisible:true,
  autoscaleInfoProvider:orig=>{const r=orig();if(!r||!y)return r;const dev=Math.max(Math.abs(r.priceRange.maxValue-y),Math.abs(r.priceRange.minValue-y),y*0.006)*1.05;return{priceRange:{minValue:y-dev,maxValue:y+dev},margins:r.margins}}});
 base.setData(full);
 base.createPriceLine({price:y,color:'#9aa4b2',lineWidth:1,lineStyle:L.LineStyle.Dotted,axisLabelVisible:true,title:'平盤 '+f2(y)});
 const av=ch.addLineSeries({color:AVG,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});av.setData(avg);
 const vs=ch.addHistogramSeries({priceScaleId:'vol',priceFormat:{type:'volume'},priceLineVisible:false,lastValueVisible:false});vs.setData(vol);
 ch.priceScale('vol').applyOptions({scaleMargins:{top:0.8,bottom:0}});ch.priceScale('right').applyOptions({scaleMargins:{top:0.06,bottom:0.24}});
 ch.timeScale().fitContent();
 const map=new Map(pts.map((p,i)=>[p.time,{p:p.value,a:avg[i].value,v:vol[i].value}]));
 const show=t=>{const d=map.get(t)||map.get(pts.at(-1).time);const tt=map.has(t)?t:pts.at(-1).time;const chg=d.p-y,pc=y?chg/y*100:0,c=chg>0?'sc-up':chg<0?'sc-dn':'';
  info.innerHTML=`<span><i>時間</i> ${hmOf(tt)}</span><span class="${c}"><i>價</i> ${f2(d.p)}</span><span class="${c}">${chg>0?'▲':chg<0?'▼':''}${f2(Math.abs(chg))} (${sg(pc.toFixed(2))}%)</span><span style="color:${AVG}"><i>均價</i> ${f2(d.a)}</span><span><i>量</i> ${fv(d.v)} 張</span>`};
 show(null);ch.subscribeCrosshairMove(p=>show(p&&p.time));
 touchCrosshair(el,ch,base,t=>{const d=map.get(t);return d?d.p:null},show)}
async function drawDay(code,s,date,el,info,note){const L=window.LightweightCharts;
 note.textContent='日K 近120日（Yahoo 日線，異常高低價已校正；今日為即時）；拖曳圖表看各日';
 const O=await getJSON('hist/'+code+'.json',3600000,false);if(cur!==code||view!=='day')return;
 if(!O){el.innerHTML='<div class="sc-msg">此檔暫無日K資料</div>';return}
 const d=O.d.slice(),o=O.o.slice(),hh=O.h.slice(),l=O.l.slice(),c=O.c.slice(),v=O.v.slice();
 if(s&&date&&date>d.at(-1)){d.push(date);o.push(s[14]||s[8]);hh.push(s[10]);l.push(s[11]);c.push(s[8]);v.push(s[15]||0)}
 const iso=x=>x.slice(0,4)+'-'+x.slice(4,6)+'-'+x.slice(6);
 const ma=n=>c.map((_,i)=>i<n-1?null:c.slice(i-n+1,i+1).reduce((a,b)=>a+b,0)/n),m5=ma(5),m20=ma(20),m60=ma(60);
 el.innerHTML='';const ch=L.createChart(el,{...baseOpts(el),localization:{locale:'zh-TW',dateFormat:"dd MMM 'yy"},
  handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:false,vertTouchDrag:false},handleScale:{mouseWheel:true,pinch:true,axisPressedMouseMove:false},
  leftPriceScale:{visible:true,borderColor:'#2a3447',scaleMargins:{top:0.8,bottom:0}},timeScale:{borderColor:'#2a3447',fixLeftEdge:true,fixRightEdge:true,rightOffset:2}});charts.push(ch);
 const cs=ch.addCandlestickSeries({upColor:UP,downColor:DN,wickUpColor:UP,wickDownColor:DN,borderVisible:false,priceLineVisible:true,priceLineStyle:L.LineStyle.Dotted,lastValueVisible:true});
 const bars=d.map((x,i)=>({time:iso(x),open:o[i],high:hh[i],low:l[i],close:c[i]}));cs.setData(bars);
 const line=(arr,col)=>{const s2=ch.addLineSeries({color:col,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});s2.setData(arr.map((x,i)=>x==null?{time:iso(d[i])}:{time:iso(d[i]),value:x}));return s2};
 line(m5,'#f6c344');line(m20,'#ff9f43');line(m60,'#b48cff');
 const vs=ch.addHistogramSeries({priceScaleId:'left',priceFormat:{type:'custom',formatter:fv,minMove:1},priceLineVisible:false,lastValueVisible:false});
 vs.setData(d.map((x,i)=>({time:iso(x),value:v[i],color:(c[i]>=o[i]?UP:DN)+'99'})));
 ch.priceScale('right').applyOptions({scaleMargins:{top:0.06,bottom:0.25}});ch.priceScale('left').applyOptions({scaleMargins:{top:0.8,bottom:0},visible:true,borderColor:'#2a3447'});ch.timeScale().fitContent();
 const idx=new Map(d.map((x,i)=>[iso(x),i]));
 const key=t=>typeof t==='string'?t:t&&t.year?`${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`:null;
 const show=t=>{let i=idx.get(key(t));if(i==null)i=d.length-1;const ch1=i>0?c[i]-c[i-1]:0,cl=ch1>0?'sc-up':ch1<0?'sc-dn':'';
  info.innerHTML=`<span><i>${d[i].slice(4,6)}/${d[i].slice(6)}</i></span><span><i>開</i> ${f2(o[i])}</span><span class="sc-up"><i>高</i> ${f2(hh[i])}</span><span class="sc-dn"><i>低</i> ${f2(l[i])}</span><span class="${cl}"><i>收</i> ${f2(c[i])}${i>0?' ('+sg((ch1/c[i-1]*100).toFixed(2))+'%)':''}</span><span><i>量</i> ${fv(v[i])} 張</span><br><span style="color:#f6c344">MA5 ${f2(m5[i])}</span><span style="color:#ff9f43">MA20 ${f2(m20[i])}</span><span style="color:#b48cff">MA60 ${f2(m60[i])}</span>`};
 show(null);ch.subscribeCrosshairMove(p=>show(p&&p.time));
 touchCrosshair(el,ch,cs,t=>{const i=idx.get(key(t));return i!=null?c[i]:null},show)}
window.StockChart={open,close,setRowProvider:f=>{rowProvider=f},base:BASE};
})();
/* 通用時間序列圖（十字線＋浮動提示，可拖曳）。StockChart.ts(idOrEl,cfg) 直接掛載；StockChart.tsHTML(cfg) 回傳佔位 HTML 並稍後掛載 */
(function(){
const SC=window.StockChart,REG={};let uid=0;
function loadLW(){if(window.LightweightCharts)return Promise.resolve();return new Promise((res,rej)=>{const s=document.createElement('script');s.src=SC.base+'vendor/lightweight-charts.standalone.production.js';s.onload=res;s.onerror=rej;document.head.appendChild(s)})}
let lwP=null;const lw=()=>lwP||(lwP=loadLW());
const css=`.tsw{position:relative;width:100%;touch-action:pan-y}.tsw .tt{position:absolute;top:6px;z-index:5;pointer-events:none;background:#0d1a2ee6;border:1px solid #17c8d899;border-radius:8px;padding:5px 8px;font-size:11.5px;line-height:1.45;color:#e8eaed;white-space:nowrap;display:none;font-variant-numeric:tabular-nums}.tsw .tt b{color:#9fb3c8;font-weight:600}.tsw .tt i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}`;
let cssDone=false;function addCss(){if(cssDone)return;cssDone=true;const s=document.createElement('style');s.textContent=css;document.head.appendChild(s)}
const yNow=new Date().getFullYear(),mNow=new Date().getMonth()+1;
function tkey(k,kind){k=String(k);if(kind==='hm'){const[h,m]=k.split(':').map(Number);return Date.UTC(2000,0,3,h,m)/1000}
 if(kind==='month')return k.slice(0,7)+'-01';
 if(/^\d{8}$/.test(k))return k.slice(0,4)+'-'+k.slice(4,6)+'-'+k.slice(6);
 if(/^\d{4}-\d{2}-\d{2}/.test(k))return k.slice(0,10);
 const m=k.match(/^(\d{1,2})\/(\d{1,2})$/);if(m){let y=yNow;if(+m[1]>mNow+1)y--;return`${y}-${m[1].padStart(2,'0')}-${m[2].padStart(2,'0')}`}return k}
function label(t,kind){if(kind==='hm'){const d=new Date(t*1000);return String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0')}
 const s=typeof t==='string'?t:`${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
 if(kind==='month')return s.slice(0,4)+'/'+(+s.slice(5,7))+'月';return s.slice(0,4)+'/'+s.slice(5,7)+'/'+s.slice(8,10)}
const keyOf=t=>typeof t==='number'?t:typeof t==='string'?t:`${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
async function mount(el,cfg){addCss();await lw();const L=window.LightweightCharts;if(typeof el==='string')el=document.getElementById(el);if(!el)return;
 const fmt=cfg.fmt||(v=>v==null?'-':(+v).toLocaleString('en-US',{maximumFractionDigits:cfg.dec??2}));const kind=cfg.kind||'day';const H=cfg.height||200;
 let R=REG[el.id];
 if(!R||!el.contains(R.box)){if(R)try{R.chart.remove()}catch(e){}
  el.innerHTML='';const box=document.createElement('div');box.className='tsw';box.style.height=H+'px';const tt=document.createElement('div');tt.className='tt';box.appendChild(tt);el.appendChild(box);
  const pan=!!cfg.pan;
  const chart=L.createChart(box,{width:box.clientWidth||320,height:H,layout:{background:{type:'solid',color:'transparent'},textColor:'#8b929c',fontSize:10},
   grid:{vertLines:{color:'#1f2633'},horzLines:{color:'#1f2633'}},rightPriceScale:{borderColor:'#2a3447',scaleMargins:{top:.08,bottom:.06}},
   timeScale:{borderColor:'#2a3447',timeVisible:kind==='hm',secondsVisible:false,fixLeftEdge:true,fixRightEdge:true,rightOffset:0,
    tickMarkFormatter:kind==='hm'?(t=>label(t,'hm')):kind==='month'?(t=>{const s=keyOf(t);return(+s.slice(5,7))+'月'}):undefined},
   crosshair:{mode:L.CrosshairMode.Normal,vertLine:{style:L.LineStyle.Dashed,color:'#9aa4b2',labelBackgroundColor:'#3a4152'},horzLine:{style:L.LineStyle.Dashed,color:'#9aa4b2',labelBackgroundColor:'#3a4152'}},
   localization:{locale:'zh-TW',dateFormat:'yyyy/MM/dd',...(kind==='hm'?{timeFormatter:t=>label(t,'hm')}:{})},
   handleScroll:pan?{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:false,vertTouchDrag:false}:false,handleScale:pan?{mouseWheel:true,pinch:true,axisPressedMouseMove:{time:true,price:false}}:false});
  R=REG[el.id]={chart,box,tt,series:[]};
  new ResizeObserver(()=>{chart.applyOptions({width:box.clientWidth})}).observe(box);
  // 拖曳＝移動十字線
  let on=false;const mv=e=>{const t=e.touches[0];if(!t)return;const r=box.getBoundingClientRect();const time=chart.timeScale().coordinateToTime(t.clientX-r.left);if(time==null||!R.series[0])return;const v=R.vals[0].get(keyOf(time));if(v!=null){try{chart.setCrosshairPosition(v,time,R.series[0].s)}catch(err){}}show(time,t.clientX-r.left)};
  box.addEventListener('touchstart',e=>{on=true;mv(e)},{passive:true});box.addEventListener('touchmove',e=>{if(on)mv(e)},{passive:true});box.addEventListener('touchend',()=>{on=false;setTimeout(()=>{R.tt.style.display='none';chart.clearCrosshairPosition&&chart.clearCrosshairPosition()},1500)},{passive:true});
  function show(time,x){if(time==null){R.tt.style.display='none';return}const k=keyOf(time);let h=`<b>${label(time,kind)}</b>`;
   R.cfg.series.forEach((s,i)=>{const v=R.vals[i].get(k);if(v===undefined)return;h+=`<br><i style="background:${s.color}"></i>${s.name} ${v==null?'-':(s.fmt||fmt)(v)}${s.unit??cfg.unit??''}`});
   R.tt.innerHTML=h;R.tt.style.display='block';const w=R.tt.offsetWidth,bw=box.clientWidth;R.tt.style.left=(x>bw/2?Math.max(4,x-w-12):Math.min(bw-w-4,x+12))+'px'}
  R.show=show;chart.subscribeCrosshairMove(p=>{if(!p||!p.time||!p.point){if(!on)R.tt.style.display='none';return}show(p.time,p.point.x)});
 }
 R.cfg=cfg;const chart=R.chart;
 // series (re)build
 R.series.forEach(x=>{try{chart.removeSeries(x.s)}catch(e){}});R.series=[];R.vals=[];
 cfg.series.forEach((s,i)=>{let ser;const common={priceLineVisible:false,lastValueVisible:s.last!==false,title:'',priceFormat:{type:'custom',formatter:s.fmt||fmt,minMove:0.01}};
  if(s.type==='hist')ser=chart.addHistogramSeries({...common,color:s.color,priceScaleId:s.scale||'right'});
  else if(s.type==='area')ser=chart.addAreaSeries({...common,lineColor:s.color,topColor:s.color+'55',bottomColor:s.color+'05',lineWidth:s.width||2,crosshairMarkerRadius:4});
  else ser=chart.addLineSeries({...common,color:s.color,lineWidth:s.width||2,lineStyle:s.dashed?L.LineStyle.Dashed:L.LineStyle.Solid,crosshairMarkerRadius:4,pointMarkersVisible:!!s.points});
  const data=[];const vm=new Map();
  for(const p of s.data){const t=tkey(p[0],kind);const v=p[1];vm.set(keyOf(t),v);if(v==null)data.push({time:t});else{const o={time:t,value:v};if(s.colorFn)o.color=s.colorFn(v,p);data.push(o)}}
  data.sort((a,b)=>a.time<b.time?-1:a.time>b.time?1:0);
  if(cfg.pad&&kind==='hm'){const end=tkey(cfg.pad[1],'hm'),st=tkey(cfg.pad[0],'hm'),have=new Map(data.map(d=>[d.time,d])),g=[],fT=data.length?data[0].time:1e18,lT=data.length?data.at(-1).time:0;let prev=null;for(let t=st;t<=end;t+=60){const d=have.get(t);if(d&&d.value!=null)prev=d;g.push(d||(prev&&t>fT&&t<lT?{time:t,value:prev.value,...(prev.color?{color:prev.color}:{})}:{time:t}))}for(const d of data)if(d.time<st||d.time>end||(d.time-st)%60)g.push(d);g.sort((x,y)=>x.time-y.time);data.length=0;data.push(...g)}
  ser.setData(data);if(s.scale&&s.scale!=='right')chart.priceScale(s.scale).applyOptions({scaleMargins:s.margins||{top:.7,bottom:0},visible:false});
  R.series.push({s:ser});R.vals.push(vm)});
 if(cfg.series[0]&&cfg.hlines)cfg.hlines.forEach(h=>R.series[0].s.createPriceLine({price:h.v,color:h.color||'#6b7280',lineWidth:1,lineStyle:h.solid?L.LineStyle.Solid:L.LineStyle.Dashed,axisLabelVisible:h.axis!==false,title:h.title||''}));
 if(cfg.range&&R.series[0])R.series[0].s.applyOptions({autoscaleInfoProvider:()=>({priceRange:{minValue:cfg.range[0],maxValue:cfg.range[1]}})});
 if(cfg.minRange&&R.series[0])R.series[0].s.applyOptions({autoscaleInfoProvider:o=>{const r=o();if(!r)return r;return{priceRange:{minValue:Math.min(r.priceRange.minValue,cfg.minRange[0]),maxValue:Math.max(r.priceRange.maxValue,cfg.minRange[1])}}}});
 if(cfg.visible&&cfg.pan){const n=cfg.series[0].data.length;chart.timeScale().setVisibleLogicalRange({from:Math.max(0,n-cfg.visible),to:n-1})}else chart.timeScale().fitContent()}
SC.ts=(el,cfg)=>mount(el,cfg);
SC.tsHTML=cfg=>{const id='tsc'+(++uid);let tries=0;const go=()=>{const el=document.getElementById(id);if(el)mount(el,cfg);else if(++tries<40)setTimeout(go,50)};setTimeout(go,0);return`<div id="${id}" style="min-height:${cfg.height||200}px"></div>`};
})();
