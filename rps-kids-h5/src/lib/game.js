export const HANDS=['fist','peace','palm']
export const LABELS={fist:'石头',peace:'剪刀',palm:'布'}
export function winner(user,computer){
  if(!HANDS.includes(user)||!HANDS.includes(computer))throw new Error('Invalid hand')
  if(user===computer)return 'draw'
  return {fist:'peace',peace:'palm',palm:'fist'}[user]===computer?'win':'lose'
}
export function randomHand(random=()=>crypto.getRandomValues(new Uint32Array(1))[0]){
  let n
  do {n=random()} while(n===0xffffffff)
  return HANDS[n%3]
}

// A deliberate fist must move vertically, then reverse by a meaningful
// fraction of a palm length. A stationary fist never starts another round.
export class FistShake {
  constructor(){this.reset()}
  reset(){this.anchor=null;this.extreme=null;this.direction=0;this.time=null;this.since=null;this.lastFist=null}
  update({points,isFist,now,amplitude=.22}){
    if(!points||points.length!==21){this.reset();return false}
    if(!isFist){if(this.lastFist===null||now-this.lastFist>180)this.reset();return false}
    const y=[0,5,9,13,17].reduce((s,i)=>s+points[i].y,0)/5
    const x=[0,5,9,13,17].reduce((s,i)=>s+points[i].x,0)/5
    const size=Math.hypot(points[0].x-points[9].x,points[0].y-points[9].y)
    if(!Number.isFinite(size)||size<.025){this.reset();return false}
    const p={x,y,size}
    if(this.time===null||now-this.time>350||now-this.since>1600){this.reset();this.anchor=p;this.extreme=p;this.since=now}
    this.time=now;this.lastFist=now
    const threshold=amplitude*size
    if(!this.direction){
      const dy=y-this.anchor.y
      if(Math.abs(dy)>threshold&&Math.abs(dy)>Math.abs(x-this.anchor.x)*.7){this.direction=Math.sign(dy);this.extreme=p}
      return false
    }
    if((y-this.extreme.y)*this.direction>0)this.extreme=p
    if((this.extreme.y-y)*this.direction>threshold&&now-this.since>=180){this.reset();return true}
    return false
  }
}

// Once a round has started, keep watching the palm itself.  The classifier is
// deliberately not fed until motion has genuinely ended, so a slow continuous
// shake cannot be mistaken for a steady rock.
export class ShakeStopGate {
  constructor(){this.reset()}
  reset(){this.last=null;this.anchor=null;this.time=0;this.stoppedSince=null;this.history=[]}
  update({points,now,aspect=4/3,motionSpeed=1.8,maxDrift=.22,quietMs=70,motionWindowMs=240,motionRange=.10}={}){
    if(!points||points.length!==21){this.reset();return {moving:true,stopped:false}}
    const raw=points.map(p=>({x:p.x*aspect,y:p.y,z:p.z*aspect}))
    if(!raw.every(p=>[p.x,p.y,p.z].every(Number.isFinite))){this.reset();return {moving:true,stopped:false}}
    const smoothed=this.last?raw.map((p,i)=>({x:.45*p.x+.55*this.last[i].x,y:.45*p.y+.55*this.last[i].y,z:.45*p.z+.55*this.last[i].z})):raw
    const size=Math.hypot(smoothed[0].x-smoothed[9].x,smoothed[0].y-smoothed[9].y,smoothed[0].z-smoothed[9].z)
    if(size<.025){this.reset();return {moving:true,stopped:false}}
    const palm=[0,5,9,13,17].reduce((v,i)=>({x:v.x+smoothed[i].x/5,y:v.y+smoothed[i].y/5}),{x:0,y:0})
    this.history.push({...palm,now});this.history=this.history.filter(p=>now-p.now<=motionWindowMs)
    const xs=this.history.map(p=>p.x),ys=this.history.map(p=>p.y)
    const range=this.history.length>1?Math.hypot(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys))/size:0
    const delta=p=>Math.sqrt(smoothed.reduce((s,v,i)=>s+(v.x-p[i].x)**2+(v.y-p[i].y)**2+(.35*(v.z-p[i].z))**2,0)/21)/size
    const dt=(now-this.time)/1000
    if(!this.last||dt<=0||dt>.5){this.last=smoothed;this.anchor=smoothed;this.time=now;this.stoppedSince=null;return {moving:true,stopped:false,range}}
    const speed=delta(this.last)/dt,drift=delta(this.anchor)
    this.last=smoothed;this.time=now
    if(speed>motionSpeed||drift>maxDrift||range>motionRange){this.anchor=smoothed;this.stoppedSince=null;return {moving:true,stopped:false,speed,drift,range}}
    if(this.stoppedSince===null)this.stoppedSince=now
    return {moving:false,stopped:now-this.stoppedSince>=quietMs,speed,drift,range}
  }
}

export class GameRound {
  constructor({choose=randomHand,revealMs=420,resultMs=2200,minShakeMs=850,timeoutMs=10000}={}){
    Object.assign(this,{choose,revealMs,resultMs,minShakeMs,timeoutMs});this.rounds=0;this.reset()
  }
  reset(){this.phase='waiting';this.computer=null;this.user=null;this.outcome=null;this.since=0;this.lastHand=null}
  update({now,shake=false,recognized=null,handPresent=true}={}){
    if(this.phase==='revealing'&&now-this.since>=this.revealMs){this.phase='result';this.since=now}
    else if(this.phase==='result'&&now-this.since>=this.resultMs){this.reset();return this.snapshot()}
    if(this.phase==='waiting'&&shake){
      this.phase='shaking';this.computer=this.choose();this.since=now;this.lastHand=now
    } else if(this.phase==='shaking'){
      if(handPresent)this.lastHand=now
      if(now-this.since>this.timeoutMs||now-this.lastHand>1200){this.reset();return this.snapshot()}
      if(now-this.since>=this.minShakeMs&&recognized?.stable&&HANDS.includes(recognized.id)){
        this.user=recognized.id;this.outcome=winner(this.user,this.computer);this.phase='revealing';this.since=now;this.rounds++
      }
    }
    return this.snapshot()
  }
  snapshot(){const reveal=['revealing','result'].includes(this.phase);return {phase:this.phase,user:reveal?this.user:null,computer:reveal?this.computer:null,outcome:reveal?this.outcome:null,rounds:this.rounds}}
}
