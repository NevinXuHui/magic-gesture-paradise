<script setup>
import {ref,computed,onMounted,onBeforeUnmount} from 'vue'
import GameSprite from './components/GameSprite.vue'
import {StillRps,rpsScores} from './lib/rps.js'
import {MotionTarget} from './lib/target.js'
import {GameRound,FistShake,ShakeStopGate,LABELS} from './lib/game.js'

const base=import.meta.env.BASE_URL
const query=new URLSearchParams(location.search)
const previewScene=['waiting','shaking','revealing','win','lose','draw'].includes(query.get('preview'))?query.get('preview'):null
const useServerCamera=query.get('camera')==='server'||query.get('server')==='1'
const video=ref(null),preview=ref(null),debug=ref(query.get('debug')!=='0'||useServerCamera)
const ready=ref(false),loading=ref(false),message=ref('正在准备摄像头…'),error=ref('')
const settings=ref({holdMs:250,motionSpeed:1.8,maxDrift:.22,amplitude:.22})
const engine=new GameRound(),target=new MotionTarget(),still=new StillRps(),shake=new FistShake(),shakeStop=new ShakeStopGate()
const round=ref(engine.snapshot()),diagnostic=ref({hands:0,index:-1,ms:0,fps:0,gesture:'—',phase:'等待手部',match:0})
const motionHint=ref('waiting')
const recording=ref(false),recordedFrames=ref(0),recordedSeconds=ref(0)
let recordingData=null
let stream,worker,session=0,raf=0,timer=0,watchdog=0,busy=false,lastDispatch=0,lastVideoTime=-1,lastResult=0,clockId=0,fpsSince=0,fpsCount=0,frameWidth=0,frameHeight=0
const outcomeText={win:['你赢了！','耶！你是出拳小高手'],lose:['你输了','没关系，再来挑战小汪吧！'],draw:['平局','再来一局吧！']}
const visibleHands=computed(()=>['revealing','result'].includes(round.value.phase))
const title=computed(()=>!ready.value?message.value:round.value.phase==='result'?outcomeText[round.value.outcome][0]:round.value.phase==='revealing'?'亮出你的超能力！':round.value.phase==='shaking'?'摇一摇！':'摇摇拳头，来一局！')
const subtitle=computed(()=>!ready.value?'请稍等，马上就好':round.value.phase==='result'?outcomeText[round.value.outcome][1]:round.value.phase==='revealing'?'看看谁更厉害':round.value.phase==='shaking'?'选好手势，停稳亮出来！':'握拳上下摇一摇，小汪陪你玩')
const animatePhase=computed(()=>ready.value?round.value.phase:'waiting')
const phaseNames={moving:'手在移动',settling:'等待停稳',recognized:'已确认',unclear:'手型不明确',no_hand:'等待主手',multiple:'检测到多手'}
function resetInteraction(){target.reset();still.reset();shake.reset();shakeStop.reset();engine.reset();round.value=engine.snapshot();motionHint.value='waiting'}
function logEvent(type,detail=''){if(recording.value)recordingData.lines.push(`E|${Math.round(performance.now()-recordingData.startedPerf)}|${type}|${detail}`)}
function settingsChanged(){logEvent('settings_changed',`hold=${settings.value.holdMs},speed=${settings.value.motionSpeed},drift=${settings.value.maxDrift},amplitude=${settings.value.amplitude}`);resetInteraction()}
function finishRecording(reason='manual'){
  if(!recording.value)return
  logEvent('recording_ended',`reason=${reason}`);recording.value=false
  recordingData.lines.push(`# ended=${new Date().toISOString()} duration_ms=${Math.round(performance.now()-recordingData.startedPerf)} reason=${reason}`)
}
function stop(){finishRecording('camera_stopped');session++;ready.value=false;loading.value=false;busy=false;cancelAnimationFrame(raf);clearTimeout(timer);clearTimeout(watchdog);worker?.terminate();worker=null;stream?.getTracks().forEach(t=>t.stop());stream=null;if(video.value){video.value.srcObject=null;video.value.src=''}resetInteraction()}
function fail(text){stop();error.value=text;message.value='摄像头需要帮个忙'}
function closeCamera(){stop();error.value='摄像头已关闭。按 R 或“重连相机”可再次开启。';message.value='摄像头已关闭'}
function beginRecording(){
  if(!ready.value||recording.value)return
  recordedFrames.value=0;recordedSeconds.value=0
  const startedAt=new Date().toISOString(),camera=stream?.getVideoTracks()[0]?.getSettings()||{}
  recordingData={startedAt,startedPerf:performance.now(),lines:[
    '# RPS_GESTURE_LOG v1',
    `# started=${startedAt}`,
    `# settings hold_ms=${settings.value.holdMs} motion_speed=${settings.value.motionSpeed} max_drift=${settings.value.maxDrift} shake_amplitude=${settings.value.amplitude}`,
    `# camera width=${camera.width||''} height=${camera.height||''} fps=${camera.frameRate||''} aspect=${camera.aspectRatio||''}`,
    `# browser=${navigator.userAgent.replaceAll('|','/')}`,
    '# F columns: t_ms|infer_ms|hands|selected_index|track_id|target_changed|phase_before|phase_after|motion_hint|shake_trigger|motion_moving|motion_stopped|speed|drift|range|recognized|match|fist_candidates|rps_scores(fist,peace,palm)|model_categories|tracks(id,index,x,y,size,vx,vy,speed,moving)|image_landmarks(x,y,z;.../...hands)|world_landmarks(x,y,z;.../...hands)'
  ]}
  recording.value=true;logEvent('recording_started',`phase=${engine.phase},selected=${target.selected??-1}`)
}
function endRecording(){finishRecording('manual')}
function exportRecording(){
  if(recording.value||!recordingData||recordedFrames.value===0)return
  const stamp=recordingData.startedAt.replace(/[:.]/g,'-')
  const url=URL.createObjectURL(new Blob([recordingData.lines.join('\n')+'\n'],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`rps-gesture-trace-${stamp}.log`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
const compact=n=>Number.isFinite(n)?Math.round(n*10000)/10000:''
const pointLine=points=>points?.map(p=>`${compact(p.x)},${compact(p.y)},${compact(p.z)}`).join(';')||''
const handsPointLine=hands=>hands?.map(pointLine).join('/')||''
function recordFrame({timestamp,ms,result,index,handScores,fistCandidates,before,after,trigger,shakeMotion,recognition}){
  if(!recording.value)return
  const track=target.tracks.find(t=>t.id===target.selected)
  const tracks=target.tracks.filter(t=>t.index>=0).map(t=>[t.id,t.index,compact(t.x),compact(t.y),compact(t.size),compact(t.vx),compact(t.vy),compact(t.speed),t.moving].join(',')).join(';')
  const scores=handScores.map(s=>`${compact(s.fist)},${compact(s.peace)},${compact(s.palm)}`).join('/')
  const categories=(result.gestures[index]||[]).map(c=>`${c.categoryName}:${compact(c.score)}`).join(',')
  const motion=shakeMotion||{}
  const fields=[
    Math.round(performance.now()-recordingData.startedPerf),compact(ms),result.landmarks.length,index,target.selected??-1,target.changed?1:0,
    before,after,motionHint.value,trigger?1:0,motion.moving?1:0,motion.stopped?1:0,compact(motion.speed),compact(motion.drift),compact(motion.range),
    recognition?.id||'',compact(recognition?.matchScore),fistCandidates.map(Boolean).map(Number).join(','),scores,categories,tracks,
    handsPointLine(result.landmarks),handsPointLine(result.worldLandmarks)
  ]
  recordingData.lines.push(`F|${fields.join('|')}`)
  recordedFrames.value++
  recordedSeconds.value=(performance.now()-recordingData.startedPerf)/1000
  if(recordedFrames.value>=4500)finishRecording('frame_limit')
}
async function start(){
  stop();const token=session;error.value='';loading.value=true;message.value='正在连接摄像头…';lastVideoTime=-1;lastDispatch=0;frameWidth=0;frameHeight=0
  try{
    if(useServerCamera){
      // 服务端模式只检查后端状态；实际帧由 capture() 按推理节奏请求。
      message.value='正在连接服务端摄像头…'
      const statusResp=await fetch('/api/status',{cache:'no-store'})
      if(!statusResp.ok)throw Error('服务端摄像头 API 未响应，请确认 server.py 已启动。')
      const status=await statusResp.json()
      if(!status.ready)throw Error(status.error||'服务端摄像头未就绪')
      if(token!==session)return
    }else{
      // 浏览器本地摄像头模式
      if(!isSecureContext||!navigator.mediaDevices?.getUserMedia)throw Error('请从本机 localhost 或 HTTPS 打开网页，或使用 ?camera=server 参数。')
      const acquired=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:640},height:{ideal:480},frameRate:{ideal:30,max:30}},audio:false})
      if(token!==session){acquired.getTracks().forEach(t=>t.stop());return}
      stream=acquired;video.value.srcObject=stream
      const track=stream.getVideoTracks()[0]
      track.addEventListener('ended',()=>{if(token===session)fail('摄像头已断开。接好后按 R 重新连接。')})
      track.addEventListener('mute',()=>{if(token===session){resetInteraction();message.value='摄像头暂时没有画面'}})
      await video.value.play();if(token!==session)return
    }

    message.value='小汪正在准备…';worker=new Worker(`${import.meta.env.BASE_URL}inference-worker.js`)
    timer=setTimeout(()=>{if(token===session)fail('模型加载超时，请检查本地 models / vendor 文件。按 R 重试。')},60000)
    worker.onerror=()=>{if(token===session)fail('识别引擎未能启动。请用 Chrome 并按 R 重试。')}
    worker.onmessage=({data})=>{
      if(token!==session)return
      if(data.type==='ready'){
        clearTimeout(timer);ready.value=true;loading.value=false;message.value='摄像头已就绪';lastResult=performance.now();fpsSince=lastResult;fpsCount=0;raf=requestAnimationFrame(capture)
      }else if(data.type==='error')fail(`识别失败：${data.message}`)
      else if(data.type==='result'){clearTimeout(watchdog);busy=false;receive(data)}
    }
    worker.postMessage({type:'init'})
  }catch(e){if(token!==session)return;fail(({NotAllowedError:'请由大人在浏览器地址栏允许摄像头，然后按 R 重试。',NotFoundError:'没有找到摄像头，连接摄像头后按 R 重试。',NotReadableError:'摄像头正被占用，请关闭其他摄像头页面后按 R 重试。'})[e.name]||e.message)}
}
async function capture(now){
  if(!ready.value)return
  raf=requestAnimationFrame(capture)
  const muted=useServerCamera?false:stream?.getVideoTracks()[0]?.muted
  if(document.hidden||muted||busy||now-lastDispatch<66)return
  if(!useServerCamera){
    // 本地模式：检查 video 状态
    if(video.value.readyState<2||video.value.currentTime===lastVideoTime)return
    lastVideoTime=video.value.currentTime
  }
  busy=true;lastDispatch=now;const token=session
  try{
    let bitmap
    if(useServerCamera){
      const response=await fetch('/api/frame?t='+Date.now(),{cache:'no-store',signal:AbortSignal.timeout(5000)})
      if(!response.ok)throw Error(await response.text())
      bitmap=await createImageBitmap(await response.blob())
    }else{
      bitmap=await createImageBitmap(video.value)
    }
    if(token!==session){bitmap.close();return}
    drawFrame(bitmap)
    worker.postMessage({type:'frame',bitmap,timestamp:now},[bitmap])
    watchdog=setTimeout(()=>{if(token===session)fail('识别超时，请按 R 重新连接。')},12000)
  }catch(e){if(token===session)fail(`摄像头画面读取失败：${e.message}`)}
}
function receive({result,ms,timestamp}){
  const muted=useServerCamera?false:stream?.getVideoTracks()[0]?.muted
  if(document.hidden||muted)return
  lastResult=performance.now();message.value='摄像头已就绪';fpsCount++
  const elapsed=lastResult-fpsSince
  const aspect=frameWidth&&frameHeight?frameWidth/frameHeight:video.value.videoWidth/video.value.videoHeight
  const handScores=result.landmarks.map((hand,i)=>{
    const corrected=hand.map(p=>({x:p.x*aspect,y:p.y,z:p.z*aspect}))
    return rpsScores(result.gestures[i]||[],result.worldLandmarks?.[i],corrected).scores
  })
  const fistCandidates=handScores.map(score=>score.fist>.18&&score.fist>=score.peace&&score.fist>=score.palm)
  const index=target.update(result.landmarks,timestamp,aspect,{locked:engine.phase!=='waiting',eligible:fistCandidates,verticalOnly:engine.phase==='waiting'})
  if(target.changed){logEvent('target_changed',`track=${target.selected},index=${index}`);still.reset();shake.reset();shakeStop.reset();if(engine.phase==='shaking')engine.reset()}
  const p=result.landmarks[index],world=result.worldLandmarks?.[index],categories=result.gestures[index]||[]
  let recognition=null,trigger=false,shakeMotion=null
  const before=engine.phase
  if(before==='waiting'){
    const scores=handScores[index]||{fist:0,peace:0,palm:0}
    const isFist=scores.fist>.30&&scores.fist>scores.peace&&scores.fist>scores.palm
    // MotionTarget has already observed consecutive vertical motion from a
    // fist-like hand.  Starting here avoids asking the child to perform a
    // second complete shake after the hand is visibly locked.
    trigger=target.changed||shake.update({points:p,isFist,now:timestamp,amplitude:settings.value.amplitude})
  }else if(before==='shaking'){
    shakeMotion=shakeStop.update({points:p,now:timestamp,aspect,motionSpeed:settings.value.motionSpeed,maxDrift:settings.value.maxDrift})
    if(shakeMotion.stopped){
      motionHint.value='recognizing'
      recognition=still.update({landmarks:p,worldLandmarks:world,categories,now:timestamp,handCount:p?1:0,aspect,...settings.value})
    }else{
      motionHint.value=shakeMotion.moving?'moving':'ending'
      still.reset()
    }
  }else if(before==='result'){
    const scores=handScores[index]||{fist:0,peace:0,palm:0}
    const isFist=scores.fist>.30&&scores.fist>scores.peace&&scores.fist>scores.palm
    trigger=shake.update({points:p,isFist,now:timestamp,amplitude:settings.value.amplitude})
  }
  round.value=engine.update({now:timestamp,shake:trigger,recognized:recognition,handPresent:!!p})
  if(before!==round.value.phase)logEvent('phase_changed',`${before}->${round.value.phase}`)
  if(before==='waiting'&&round.value.phase==='shaking'){still.reset();shake.reset();shakeStop.reset();motionHint.value='moving'}
  if(before!=='waiting'&&round.value.phase==='waiting'){target.reset();still.reset();shake.reset();shakeStop.reset();motionHint.value='waiting'}
  const gamePhase=shakeMotion?.moving?'继续摇拳中':shakeMotion&&!shakeMotion.stopped?'等待摇拳结束':recognition?phaseNames[recognition.phase]:null
  diagnostic.value={...diagnostic.value,scores:handScores[index]||handScores[0],motion:shakeMotion,hands:result.landmarks.length,index,ms,gesture:recognition?.id?LABELS[recognition.id]||'—':trigger?'摇拳已触发':'—',phase:gamePhase||(p?'主手已锁定，等待摇拳':'上下摇拳来锁定主手'),match:recognition?.matchScore||0,raw:categories,round:round.value}
  if(elapsed>=1000){diagnostic.value.fps=fpsCount*1000/elapsed;fpsSince=lastResult;fpsCount=0}
  recordFrame({timestamp,ms,result,index,handScores,fistCandidates,before,after:round.value.phase,trigger,shakeMotion,recognition})
  draw(result.landmarks,index)
}
const edges=[[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],[10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]]
function draw(hands,selected){
  if(!debug.value||!preview.value)return
  const canvas=preview.value
  if(!canvas.width||!canvas.height)return // 尺寸无效
  const ctx=canvas.getContext('2d')
  ctx.save()
  ctx.translate(canvas.width,0)
  ctx.scale(-1,1)
  hands.forEach((p,index)=>{ctx.lineWidth=index===selected?3:1;ctx.strokeStyle=index===selected?'#f8ef5f':'#80cfff';ctx.fillStyle=ctx.strokeStyle
    edges.forEach(([a,b])=>{ctx.beginPath();ctx.moveTo(p[a].x*canvas.width,p[a].y*canvas.height);ctx.lineTo(p[b].x*canvas.width,p[b].y*canvas.height);ctx.stroke()})
    p.forEach(v=>{ctx.beginPath();ctx.arc(v.x*canvas.width,v.y*canvas.height,3,0,Math.PI*2);ctx.fill()})
  })
  ctx.restore()
}
function drawFrame(source){
  const width=source.width||source.videoWidth
  const height=source.height||source.videoHeight
  if(!width||!height)return
  frameWidth=width;frameHeight=height
  if(!debug.value||!preview.value)return
  const canvas=preview.value
  canvas.width=width;canvas.height=height
  const ctx=canvas.getContext('2d')
  ctx.save();ctx.translate(width,0);ctx.scale(-1,1)
  ctx.drawImage(source,0,0,width,height)
  ctx.restore()
}
function clockTick(){
  if(!ready.value||document.hidden)return
  const now=performance.now()
  if(now-lastResult>1400){resetInteraction();message.value='等待摄像头恢复画面';diagnostic.value={...diagnostic.value,fps:0};fpsCount=0;fpsSince=now;lastResult=now;return}
  const before=engine.phase;round.value=engine.update({now:performance.now(),handPresent:false})
  if(before!=='waiting'&&round.value.phase==='waiting'){target.reset();still.reset();shake.reset();shakeStop.reset();motionHint.value='waiting'}
}
function keys(e){
  if(e.target.closest?.('input,textarea,[role="slider"]')||e.metaKey||e.ctrlKey||e.altKey)return
  if(e.key.toLowerCase()==='d')debug.value=!debug.value
  if(e.key.toLowerCase()==='r')start()
  if(e.key.toLowerCase()==='f'){if(document.fullscreenElement)document.exitFullscreen?.().catch(()=>{});else document.documentElement.requestFullscreen?.().catch(()=>{})}
}
function visibility(){resetInteraction();if(!document.hidden){lastResult=performance.now();fpsSince=lastResult;fpsCount=0}}
let mcpCleanup
onMounted(()=>{
  // Explicit visual QA mode never starts a camera or records a real round.
  if(previewScene){
    ready.value=true;debug.value=false
    const hands={win:{user:'palm',computer:'fist'},lose:{user:'peace',computer:'fist'},draw:{user:'fist',computer:'fist'}}[previewScene]||{user:null,computer:null}
    round.value={phase:['win','lose','draw'].includes(previewScene)?'result':previewScene,...hands,outcome:previewScene,rounds:0};return
  }
  window.addEventListener('keydown',keys);document.addEventListener('visibilitychange',visibility)
  clockId=setInterval(clockTick,80);start()
  const context=document.modelContext
  if(context?.registerTool){const life=new AbortController();mcpCleanup=()=>life.abort();try{Promise.resolve(context.registerTool({name:'get_rps_game_status',description:'Read camera readiness and the visible round state without exposing the computer hand before reveal.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:()=>({ready:ready.value,...engine.snapshot()})},{signal:life.signal})).catch(()=>{})}catch{}}
})
onBeforeUnmount(()=>{stop();clearInterval(clockId);mcpCleanup?.();window.removeEventListener('keydown',keys);document.removeEventListener('visibilitychange',visibility)})
</script>

<template>
  <div class="viewport">
    <img v-if="useServerCamera" class="capture-video" ref="video" aria-hidden="true">
    <video v-else class="capture-video" ref="video" autoplay muted playsinline aria-hidden="true"></video>
    <main class="game" :class="[animatePhase,round.phase==='result'?round.outcome:'']">
      <div v-if="round.phase==='result'&&round.outcome!=='draw'" class="sunburst"></div>
      <div class="sky-decor" aria-hidden="true"><span v-for="n in 6" :key="n" class="paw" :style="{'--n':n}">🐾</span><i class="cloud cloud-one"></i><i class="cloud cloud-two"></i><span class="sky-star star-one">✦</span><span class="sky-star star-two">✦</span></div>
      <section class="status" aria-live="polite"><h1 :key="title"><span>{{title}}</span></h1><p v-if="round.phase==='waiting'">握拳上下摇一摇</p></section>
      <div class="arena">
        <section class="player computer" :class="{champion:round.phase==='result'&&round.outcome==='lose'}"><div class="player-label"><span class="avatar"><GameSprite kind="dog" label="机器狗"/></span>机器狗</div><div class="hand-orbit"><div class="orbit-ring"></div><div class="hand-sprite"><GameSprite :kind="visibleHands?round.computer:'fist'" :label="visibleHands?LABELS[round.computer]:'等待出拳'"/></div><span v-if="!visibleHands&&round.phase!=='shaking'" class="question">?</span></div><div class="hand-label">{{visibleHands?LABELS[round.computer]:round.phase==='shaking'?'摇拳中':'等待'}}</div></section>
        <div class="versus" aria-hidden="true">VS</div>
        <section class="player human" :class="{champion:round.phase==='result'&&round.outcome==='win'}"><div class="player-label"><span class="you-avatar"><GameSprite kind="boy" label="你"/></span>你</div><div class="hand-orbit"><div class="orbit-ring"></div><div class="hand-sprite"><GameSprite :kind="visibleHands?round.user:'fist'" :label="visibleHands?LABELS[round.user]:'等待出拳'"/></div><span v-if="!visibleHands&&round.phase!=='shaking'" class="question">?</span></div><div class="hand-label">{{visibleHands?LABELS[round.user]:round.phase==='shaking'?'停稳出拳':'等待摇拳'}}</div></section>
      </div>
      <div v-if="round.phase==='result'" :key="round.rounds" class="result-effects" role="status">
        <template v-if="round.outcome!=='draw'">
          <div class="celebration-burst" aria-hidden="true">
            <i v-for="n in 32" :key="n" class="burst-piece" :style="{'--angle':`${n*11.25}deg`,'--distance':`${205+(n%5)*24}px`,'--delay':`${(n%8)*.025}s`}"></i>
          </div>
          <div class="victory-badge"><div class="crown"><GameSprite kind="crown" label="结算皇冠"/></div><strong>{{round.outcome==='win'?'WIN!':'LOSE'}}</strong><div class="ribbon"><span>{{round.outcome==='win'?'你赢了！':'你输了'}}</span></div></div>
        </template>
        <template v-else><i v-for="n in 12" :key="n" class="tie-star" :style="{'--i':n}">✦</i><div class="tie-badge"><div class="tie-sparks">✦ ˙ ✦</div><strong>平局</strong><span>再来一局吧！</span></div></template>
      </div>
      <div v-if="!ready" class="setup-overlay"><img :src="`${base}art/dog_mascot.webp`" alt="小汪"/><h2>{{message}}</h2><p>{{error||'第一次使用时，请允许浏览器访问摄像头'}}</p><small v-if="error">请大人帮忙 · 按 R 重试</small><div v-else class="loading-dots">● ● ●</div></div>
    </main>
    <span v-if="previewScene" class="debug-open">动画预览 · 不启用摄像头</span>
    <button v-else-if="!debug" class="debug-open" @click="debug=true">摄像头调试 · D</button>
    <aside class="debug-panel" :style="{display: debug ? 'block' : 'none'}"><div class="debug-heading"><strong>摄像头调试</strong><button @click="debug=false" aria-label="隐藏调试窗口">×</button></div><canvas ref="preview" aria-label="镜像摄像头和手部关节"></canvas><div class="debug-metrics">{{diagnostic.ms.toFixed(0)}} ms · {{diagnostic.fps.toFixed(1)}} FPS · {{diagnostic.hands}} 只手</div><p>{{diagnostic.phase}} · {{diagnostic.gesture}}</p><small>黄色：本轮主手 · 蓝色：其他检测手</small><div class="score-grid" v-if="diagnostic.scores"><span v-for="(value,id) in diagnostic.scores" :key="id">{{LABELS[id]}}<b>{{Math.round(value*100)}}%</b></span></div><small v-if="diagnostic.motion">掌部速度 {{diagnostic.motion.speed?.toFixed(2) || '—'}} · 位移 {{diagnostic.motion.range?.toFixed(2) || '—'}}</small><label>停稳时间 <b>{{settings.holdMs}} ms</b></label><el-slider v-model="settings.holdMs" :min="200" :max="800" :step="50" @change="settingsChanged" aria-label="停稳时间"/><label>移动速度阈值 <b>{{settings.motionSpeed.toFixed(1)}}</b></label><el-slider v-model="settings.motionSpeed" :min=".8" :max="3.5" :step=".1" @change="settingsChanged" aria-label="移动速度阈值"/><label>累计位移阈值 <b>{{settings.maxDrift.toFixed(2)}}</b></label><el-slider v-model="settings.maxDrift" :min=".08" :max=".4" :step=".01" @change="settingsChanged" aria-label="累计位移阈值"/><label>摇拳幅度 <b>{{settings.amplitude.toFixed(2)}} 掌长</b></label><el-slider v-model="settings.amplitude" :min=".12" :max=".5" :step=".02" @change="settingsChanged" aria-label="摇拳幅度"/><div class="record-status" :class="{active:recording}"><i></i><span>{{recording?`记录中 · ${recordedFrames} 帧 · ${recordedSeconds.toFixed(1)} 秒`:recordedFrames?`已结束 · ${recordedFrames} 帧 · ${recordedSeconds.toFixed(1)} 秒`:'尚未记录'}}</span></div><div class="record-actions"><el-button size="small" type="primary" @click="beginRecording" :disabled="!ready||recording">开始记录</el-button><el-button size="small" @click="endRecording" :disabled="!recording">结束记录</el-button><el-button size="small" @click="exportRecording" :disabled="recording||!recordedFrames">导出日志</el-button></div><div class="debug-actions"><el-button size="small" @click="start" :loading="loading">重连相机</el-button><el-button size="small" @click="closeCamera">关闭相机</el-button></div><small>日志最长记录 5 分钟 · D 显示/隐藏 · F 全屏 · R 重连</small></aside>
  </div>
</template>
