<script setup>
import { StillRps } from './lib/rps.js'
import { ref, computed, onBeforeUnmount } from 'vue'
import { catalog, byId, rps, classify, Stabilizer, summarizeTimings } from './lib/gestures.js'

const video=ref(null), canvas=ref(null), running=ref(false), loading=ref(false), status=ref('摄像头未开启'), error=ref('')
const stillRps=new StillRps()
const phaseText={moving:'手在移动，暂不识别',settling:'请停稳片刻',no_hand:'等待手进入画面',multiple:'请只展示一只手',unclear:'手型差异较大，请调整手势',recognized:'已识别'}
const robustPeace=ref(true)
const mode=ref('rps'), threshold=ref(0.65), holdMs=ref(250), motionSpeed=ref(1.8), maxDrift=ref(.22), mirror=ref(true), skeleton=ref(true), device=ref(''), devices=ref([])
const tab=ref('recognize'), raw=ref(null), output=ref(null), history=ref([]), handCount=ref(0), primaryHand=ref(-1), latency=ref(null), fps=ref(null), timings=ref([]), frameCount=ref(0), outputJson=ref('{}')
const state=computed(()=>output.value?.stable ? byId[output.value.id] : byId.no_gesture)
const stats=computed(()=>summarizeTimings(timings.value))
const shown=computed(()=>mode.value==='rps'?catalog.filter(g=>rps.includes(g.id)):catalog)
const statusLabels={model:'模型识别',rule:'实验规则',pending:'待接入',alias:'同形归并',system:'系统状态'}
let stream=null, worker=null, raf=0, timer=null, watchdog=null, busy=false, session=0, lastVideoTime=-1, lastDispatch=0, lastResultAt=0, fpsStart=0, fpsFrames=0, previous='no_gesture', stabilizer=new Stabilizer(), handLock=null
function clearResult() {
  stabilizer.reset(); stillRps.reset(); handLock=null; primaryHand.value=-1; output.value=null; raw.value=null; handCount.value=0; previous='no_gesture'
  const payload={timestamp:new Date().toISOString(),gesture:'no_gesture',stable:false,confidence:0,source:'system',inference_ms:null,hands:[]}
  outputJson.value=JSON.stringify(payload,null,2)
  window.dispatchEvent(new CustomEvent('gesture-result',{detail:payload}))
  canvas.value?.getContext('2d')?.clearRect(0,0,canvas.value.width,canvas.value.height)
}
function resetRecognition() { clearResult() }
function resetRpsStability() { motionSpeed.value=1.8; maxDrift.value=.22; holdMs.value=250; resetRecognition() }
function stop() {
  session++; running.value=false; loading.value=false; busy=false
  cancelAnimationFrame(raf); clearTimeout(timer); clearTimeout(watchdog)
  worker?.terminate(); worker=null
  stream?.getTracks().forEach(t=>t.stop()); stream=null
  if(video.value) video.value.srcObject=null
  clearResult(); latency.value=null; fps.value=null; status.value='摄像头已关闭'
}
const messages={NotAllowedError:'摄像头权限被拒绝。请在浏览器地址栏允许摄像头访问，再重新开启。',NotFoundError:'没有找到摄像头。请连接 USB 摄像头后重试。',NotReadableError:'摄像头无法读取，可能正被其他应用占用。请关闭占用应用后重试。',OverconstrainedError:'所选摄像头不可用。请选择默认摄像头后重试。'}
function fail(message) { stop(); error.value=message; status.value='启动或识别失败' }
async function start() {
  stop(); const token=session; error.value=''; loading.value=true; status.value='等待摄像头授权'
  timings.value=[]; frameCount.value=0; fpsFrames=0; lastVideoTime=-1; lastDispatch=0; history.value=[]
  try {
    if(!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('请使用 http://localhost 或 HTTPS 打开网页，浏览器才允许读取摄像头。')
    const acquired=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:device.value?{exact:device.value}:undefined,width:{ideal:640},height:{ideal:480},frameRate:{ideal:30,max:30}}})
    if(token!==session) {acquired.getTracks().forEach(t=>t.stop());return}
    stream=acquired; video.value.srcObject=stream
    stream.getVideoTracks()[0].addEventListener('ended',()=>{if(token===session)fail('摄像头已断开，请重新连接后开启。')})
    stream.getVideoTracks()[0].addEventListener('mute',()=>{if(token===session){clearResult();status.value='摄像头暂停供帧'}})
    stream.getVideoTracks()[0].addEventListener('unmute',()=>{if(token===session&&running.value)status.value='实时识别中'})
    await video.value.play(); if(token!==session)return
    try { devices.value=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput') } catch { /* Camera is still usable if enumeration is denied. */ }
    if(token!==session)return
    status.value='正在加载本地识别模型'
    // Vite public files are served unchanged; use the deployment base for subdirectories.
    worker=new Worker(`${import.meta.env.BASE_URL}inference-worker.js`)
    timer=setTimeout(()=>{if(token===session)fail('模型加载超时。请确认 models 与 vendor 文件完整，或尝试 Chrome / Chromium。')},60000)
    worker.onerror=()=>{if(token===session)fail('识别引擎启动失败。请检查模型文件和浏览器对 WebAssembly 的支持。')}
    worker.onmessage=({data})=>{
      if(token!==session)return
      if(data.type==='ready') {clearTimeout(timer);running.value=true;loading.value=false;status.value='实时识别中';fpsStart=performance.now();lastResultAt=fpsStart;raf=requestAnimationFrame(tick)}
      else if(data.type==='error') fail(`识别失败：${data.message}`)
      else if(data.type==='result') {clearTimeout(watchdog);busy=false;receive(data)}
    }
    worker.postMessage({type:'init'})
  } catch(e) { if(token===session)fail(messages[e.name]||e.message||'摄像头启动失败') }
}
async function tick(now) {
  if(!running.value)return
  raf=requestAnimationFrame(tick)
  if(now-lastResultAt>1200&&output.value){clearResult();fps.value=0;latency.value=null;status.value='等待新的视频帧'}
  if(document.hidden||busy||video.value?.readyState<2||video.value.currentTime===lastVideoTime||now-lastDispatch<66)return
  busy=true;lastVideoTime=video.value.currentTime;lastDispatch=now;const token=session
  try {
    const bitmap=await createImageBitmap(video.value)
    if(token!==session) {bitmap.close();return}
    worker.postMessage({type:'frame',bitmap,timestamp:now},[bitmap])
    watchdog=setTimeout(()=>{if(token===session)fail('识别响应超时，请重新开启摄像头。')},15000)
  } catch(e) {if(token===session)fail(`读取视频帧失败：${e.message}`)}
}
function receive({result,ms,timestamp}) {
  if(document.hidden||stream?.getVideoTracks()[0]?.muted)return
  lastResultAt=performance.now();status.value='实时识别中'
  latency.value=ms;frameCount.value++;timings.value=[...timings.value.slice(-299),ms]
  fpsFrames++;const now=performance.now();if(now-fpsStart>=1000){fps.value=fpsFrames*1000/(now-fpsStart);fpsStart=now;fpsFrames=0}
  handCount.value=result.landmarks.length
  primaryHand.value=pickPrimaryHand(result.landmarks)
  result.gestures=result.gestures.map(list=>[...list].sort((a,b)=>b.score-a.score))
  const detected=result.landmarks.map((p,i)=>classify(result.gestures[i]?.[0],p,{mode:mode.value,threshold:threshold.value,worldLandmarks:result.worldLandmarks?.[i],robustPeace:robustPeace.value}))
  // For RPS, two simultaneous hands have no single unambiguous answer.
  const current=mode.value==='rps'&&primaryHand.value>=0?detected[primaryHand.value]:detected.length===1?detected[0]:{id:'no_gesture',score:0,source:'system'}
  raw.value=current
  output.value=mode.value==='rps'?stillRps.update({landmarks:result.landmarks[primaryHand.value],worldLandmarks:result.worldLandmarks?.[primaryHand.value],categories:result.gestures[primaryHand.value],now:timestamp,holdMs:holdMs.value,motionSpeed:motionSpeed.value,maxDrift:maxDrift.value,aspect:video.value.videoWidth/video.value.videoHeight,handCount:primaryHand.value>=0?1:0}):stabilizer.update(current,now,holdMs.value,{handPresent:detected.length===1,tolerateDropout:robustPeace.value})
  const payload={timestamp:new Date().toISOString(),gesture:output.value.id,stable:output.value.stable,confidence:output.value.score,source:output.value.source,method:output.value.method||null,phase:output.value.phase||null,match_score:output.value.matchScore??null,candidates:output.value.candidates||[],inference_ms:Math.round(ms*10)/10,hands:result.landmarks.map((p,i)=>({gesture:detected[i].id,confidence:detected[i].score,source:detected[i].source,handedness:result.handedness[i]?.[0]?.categoryName,method:detected[i].method||null,raw_model:result.gestures[i]?.[0]||null,world_landmarks:result.worldLandmarks?.[i]||[],landmarks:p}))}
  outputJson.value=JSON.stringify(payload,null,2)
  window.dispatchEvent(new CustomEvent('gesture-result',{detail:payload}))
  if(output.value.stable&&output.value.id!==previous){history.value=[{id:output.value.id,time:new Date().toLocaleTimeString('zh-CN'),score:output.value.score,source:output.value.source,matchScore:output.value.matchScore},...history.value].slice(0,8);previous=output.value.id}
  if(!output.value.stable)previous='no_gesture'
  draw(result.landmarks)
}
function pickPrimaryHand(hands) {
  if(!hands.length)return -1
  const aspect=video.value.videoWidth/video.value.videoHeight
  const descriptors=hands.map((p,index)=>{
    const wrist=p[0], middle=p[9]
    const size=Math.hypot((wrist.x-middle.x)*aspect,wrist.y-middle.y,(wrist.z-middle.z)*aspect)
    return {index,x:wrist.x*aspect,y:wrist.y,z:wrist.z*aspect,size}
  }).filter(v=>Number.isFinite(v.size)&&v.size>.025)
  if(!descriptors.length)return -1
  let selected
  if(handLock){
    selected=descriptors.map(v=>({...v,match:Math.hypot(v.x-handLock.x,v.y-handLock.y,.35*(v.z-handLock.z))/(Math.max(v.size,handLock.size)||1)})).sort((a,b)=>a.match-b.match)[0]
    if(selected.match>.85)selected=null
  }
  selected??=descriptors.sort((a,b)=>b.size-a.size)[0]
  handLock=selected
  return selected.index
}
const edges=[[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]]
function draw(hands) {
  const c=canvas.value;if(!c)return;c.width=video.value.videoWidth;c.height=video.value.videoHeight
  const ctx=c.getContext('2d');ctx.clearRect(0,0,c.width,c.height);if(!skeleton.value)return
  hands.forEach((points,index)=>{if(mode.value==='rps'&&index!==primaryHand.value)return;ctx.strokeStyle='#c1f56d';ctx.fillStyle='#fff';ctx.lineWidth=2.5
    edges.forEach(([a,b])=>{ctx.beginPath();ctx.moveTo(points[a].x*c.width,points[a].y*c.height);ctx.lineTo(points[b].x*c.width,points[b].y*c.height);ctx.stroke()})
    points.forEach(p=>{ctx.beginPath();ctx.arc(p.x*c.width,p.y*c.height,3.5,0,Math.PI*2);ctx.fill()})
  })
}
function download() {
  const report={created_at:new Date().toISOString(),userAgent:navigator.userAgent,mode:mode.value,threshold:threshold.value,hold_ms:holdMs.value,motion_speed_threshold:motionSpeed.value,max_drift_threshold:maxDrift.value,robust_peace:robustPeace.value,engine:'MediaPipe 0.10.32 / WASM CPU',camera:stream?.getVideoTracks()[0]?.getSettings()||null,sample_window:'last 300 inference calls; includes warm-up if in window',timing:stats.value,target_ms:73,target_met:stats.value?stats.value.p95<=73:null,last_result:JSON.parse(outputJson.value)}
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='gesture-benchmark.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
function visibility(){if(document.hidden){clearResult();status.value=running.value?'后台暂停采样':status.value}else if(running.value){clearResult();fpsStart=performance.now();fpsFrames=0;status.value='实时识别中'}}
document.addEventListener('visibilitychange',visibility)
onBeforeUnmount(()=>{stop();document.removeEventListener('visibilitychange',visibility)})
</script>

<template>
  <div class="app-shell">
    <header class="topbar"><a class="brand" href="./"><span class="brand-mark">G<span>↗</span></span><span>Gesture<span class="brand-light">Lab</span><small>手势识别实验室</small></span></a><div class="header-right"><span class="local-dot"></span> 本机处理 <span class="version">V 0.3.4</span></div></header>
    <main>
      <div class="heading"><div><div class="eyebrow">VISION / REALTIME</div><h1>让手势，被看见<span>。</span></h1><p>开启摄像头，用剪刀、石头、布测试实时识别。</p></div><div class="mode-control"><span>识别范围</span><el-radio-group v-model="mode" @change="resetRecognition"><el-radio-button value="rps">剪刀石头布</el-radio-button><el-radio-button value="all">全部手势</el-radio-button></el-radio-group></div></div>
      <el-alert v-if="error" class="error-alert" :title="error" type="error" show-icon :closable="false" />
      <div class="workspace">
        <section class="camera-panel">
          <div class="panel-toolbar"><span><i :class="['status-dot',{active:running}]"></i>{{ status }}</span><span class="mono">{{running?'LIVE':'STANDBY'}} <span class="toolbar-divider">/</span> CAMERA 01</span></div>
          <div class="camera-stage">
            <video ref="video" autoplay playsinline muted :class="{mirrored:mirror}" :style="{opacity:running?1:0}" aria-label="实时摄像头画面"></video>
            <canvas ref="canvas" :class="{mirrored:mirror}" aria-label="手部关键点叠加"></canvas>
            <div v-if="!running" class="camera-empty"><div class="viewfinder"><span>⌁</span></div><h2>{{loading?'准备识别中':'把你的手势带到镜头前'}}</h2><p>{{loading?status:'画面仅在当前设备处理，不会上传'}}</p><el-button v-if="!loading" type="primary" size="large" @click="start"><span class="button-icon">◉</span> 开启摄像头</el-button><el-button v-else size="large" @click="stop">取消启动</el-button><div class="empty-hands">✌️ <span>·</span> ✊ <span>·</span> 🖐️</div></div>
            <template v-if="running"><span class="camera-corner top-left"></span><span class="camera-corner bottom-right"></span><div class="live-result"><span class="status-dot active"></span>{{output?.stable?`${state.label} / ${state.id}`:mode==='rps'&&output?.phase?phaseText[output.phase]:handCount>1?'请只展示一只手':handCount?'正在确认手势…':'等待手进入画面'}}</div><span class="camera-resolution">{{video?.videoWidth}} × {{video?.videoHeight}}</span></template>
          </div>
          <div class="camera-footer"><div class="switches"><label><el-switch v-model="skeleton" aria-label="显示关键点" />关键点</label><label><el-switch v-model="mirror" aria-label="镜像画面" />镜像</label></div><el-button v-if="running" plain @click="stop">关闭摄像头</el-button><span v-else class="subtle">建议手距镜头 30–80 cm</span></div>
        </section>
        <aside class="result-panel"><div class="section-label">识别结果 <span>OUTPUT</span></div><div class="result-main" aria-live="polite"><div :class="['result-icon',{recognized:output?.stable}]">{{output?.stable?state.icon:'∅'}}</div><h2>{{output?.stable?state.label:mode==='rps'&&output?.phase?phaseText[output.phase]:'等待识别'}}</h2><span class="result-code">{{output?.stable?state.id:'no_gesture'}}</span><p>{{output?.stable?(output.source==='rps'?'停稳后综合匹配 · 非模型概率':output.source==='rule'?(output.method==='peace_3d'?'三维剪刀增强 · 关节角度匹配':'实验规则匹配 · 请验证实际效果'):'已连续稳定识别'):running?(mode==='rps'&&output?.phase?phaseText[output.phase]:handCount>1?'检测到双手，请保留一只手':raw?.id!=='no_gesture'&&raw?'请保持手势片刻':'请完整展示一只手'):'开启摄像头后显示结果'}}</p></div><div class="confidence"><div><span>{{mode==='rps'?'三选一匹配度':output?.source==='rule'?'规则匹配':'模型置信度'}}</span><strong>{{output?.stable&&(output.matchScore??output.score)!=null?`${((output.matchScore??output.score)*100).toFixed(0)}%`:'—'}}</strong></div><el-progress :percentage="output?.stable&&(output.matchScore??output.score)!=null?Math.round((output.matchScore??output.score)*100):0" :show-text="false" :stroke-width="5" color="#b9ed71" /></div><div class="metrics"><div><strong>{{latency===null?'—':latency.toFixed(0)}}<small>ms</small></strong><span>单帧推理</span></div><div><strong>{{fps===null?'—':fps.toFixed(1)}}<small>FPS</small></strong><span>识别帧率</span></div><div><strong>{{running?handCount:'—'}}<small>/ 2</small></strong><span>检测手数</span></div></div><div class="target-note"><span>参考目标 ≤ 73 ms</span><span>{{stats?`P95 ${stats.p95.toFixed(0)} ms`:'等待实测'}}</span></div></aside>
      </div>
      <div class="lower-grid"><section class="details-panel"><el-tabs v-model="tab"><el-tab-pane label="手势图鉴" name="recognize"><div class="catalog-intro"><span>{{mode==='rps'?'先从这三个手势开始':'22 个类别 · 6 个模型类别 / 5 个实验规则 / 2 个同形归并 / 8 个待接入 / 1 个系统状态'}}</span><small>{{mode==='rps'?'保持手势约 0.3 秒':'实验规则不代表已通过准确率验收'}}</small></div><div :class="['gesture-grid',{expanded:mode==='all'}]"><div v-for="g in shown" :key="g.id" :class="['gesture-card',{selected:output?.stable&&output.id===g.id,pending:g.status==='pending'}]"><div class="gesture-card-top"><span class="gesture-symbol">{{g.icon}}</span><span class="gesture-kind">{{statusLabels[g.status]}}</span></div><h3>{{g.label}}<code>{{g.id}}</code></h3><p>{{g.hint}}</p></div></div></el-tab-pane><el-tab-pane label="识别记录" name="history"><div v-if="!history.length" class="empty-history">稳定识别的手势会出现在这里。本次会话最多保留 8 条。</div><div v-for="(item,i) in history" :key="i" class="history-row"><span>{{byId[item.id].icon}}　{{byId[item.id].label}}</span><code>{{item.id}}</code><span>{{item.source==='rps'?`匹配 ${(item.matchScore*100).toFixed(0)}%`:item.source==='rule'?'规则匹配':`${(item.score*100).toFixed(0)}%`}}</span><time>{{item.time}}</time></div></el-tab-pane><el-tab-pane label="结果 JSON" name="json"><p class="json-hint">每帧结果包含手势、置信度、关键点和耗时，可用于后续游戏接入。</p><pre>{{outputJson}}</pre></el-tab-pane></el-tabs></section>
      <aside class="settings-panel"><div class="section-label">识别设置 <span>SETTINGS</span></div><div v-if="mode==='all'" class="robust-setting"><label><el-switch v-model="robustPeace" aria-label="剪刀侧向增强" @change="resetRecognition" /> 剪刀侧向增强</label><p>用三维关节补充识别，容忍短暂分类丢帧。手指完全遮挡时仍可能失败。</p></div><label class="field-title">摄像头</label><el-select v-model="device" :disabled="running||loading" placeholder="默认摄像头" aria-label="摄像头"><el-option label="默认摄像头" value=""/><el-option v-for="(d,i) in devices" :key="d.deviceId" :label="d.label||`摄像头 ${i+1}`" :value="d.deviceId"/></el-select><template v-if="mode==='rps'"><div class="slider-heading"><label>移动速度阈值</label><strong>{{motionSpeed.toFixed(1)}} 掌长/秒</strong></div><el-slider v-model="motionSpeed" :min="0.8" :max="3.5" :step="0.1" :show-tooltip="false" aria-label="移动速度阈值" @change="resetRecognition"/><div class="slider-heading"><label>累计位移阈值</label><strong>{{maxDrift.toFixed(2)}} 掌长</strong></div><el-slider v-model="maxDrift" :min="0.08" :max="0.40" :step="0.01" :show-tooltip="false" aria-label="累计位移阈值" @change="resetRecognition"/><div class="stability-actions"><span>数值越小越严格</span><el-button text @click="resetRpsStability">恢复推荐值</el-button></div></template><div v-if="mode==='all'" class="slider-heading"><label>模型置信度阈值</label><strong>{{threshold.toFixed(2)}}</strong></div><el-slider v-if="mode==='all'" v-model="threshold" :min="0.4" :max="0.95" :step="0.05" :show-tooltip="false" aria-label="模型置信度阈值" @change="resetRecognition"/><div class="slider-heading"><label>{{mode==='rps'?'出手停稳时间':'稳定保持时间'}}</label><strong>{{holdMs}} ms</strong></div><el-slider v-model="holdMs" :min="150" :max="800" :step="50" :show-tooltip="false" aria-label="稳定保持时间" @change="resetRecognition"/><p class="settings-note">{{mode==='rps'?'数值越小越容易判定为移动。若动手时仍出结果，先降低“移动速度阈值”和“累计位移阈值”。':'提高阈值减少误识别；延长保持时间减少结果跳变。实验规则不使用模型阈值。'}}</p><el-button class="export-button" :disabled="!stats" @click="download">↓ 导出性能报告</el-button></aside></div>
      <footer><span><span class="local-dot"></span> 视频不上传 · 模型本地加载</span><span>Vue 3 + Element Plus <i>/</i> WASM CPU <i>/</i> RK3588 待实机验证</span></footer>
    </main>
  </div>
</template>
