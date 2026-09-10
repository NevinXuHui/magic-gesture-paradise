import test from 'node:test'
import assert from 'node:assert/strict'
import { classify, Stabilizer, summarizeTimings, catalog, ruleGesture, isWorldPeace } from '../src/lib/gestures.js'

test('RPS maps to user labels; rock is never confused with fist',()=>{
  for(const [categoryName,id] of [['Closed_Fist','fist'],['Open_Palm','palm'],['Victory','peace']]) assert.equal(classify({categoryName,score:.9},[]).id,id)
  assert.equal(classify({categoryName:'ILoveYou',score:.99},[]).id,'no_gesture')
  assert.equal(classify({categoryName:'Thumb_Up',score:.99},[]).id,'no_gesture')
  assert.equal(classify({categoryName:'Thumb_Up',score:.99},[],{mode:'all'}).id,'like')
})
test('uncertain, missing and unknown classes never become RPS',()=>{
  assert.equal(classify({categoryName:'Closed_Fist',score:.64},[]).id,'no_gesture')
  assert.equal(classify(null,[]).id,'no_gesture')
  assert.equal(classify({categoryName:'None',score:1},[]).id,'no_gesture')
  assert.equal(ruleGesture([]),null)
  assert.equal(ruleGesture(Array(21).fill({x:0,y:0,z:0})),null)
})
test('stable output needs both elapsed time and at least three frames',()=>{
  const s=new Stabilizer(),fist={id:'fist',score:.9,source:'model'}
  assert.equal(s.update(fist,0).stable,false)
  assert.equal(s.update(fist,300).stable,false)
  assert.equal(s.update(fist,370).stable,true)
})
test('gesture change and hand loss immediately invalidate previous result',()=>{
  const s=new Stabilizer(), fist={id:'fist',score:.9,source:'model'}, palm={...fist,id:'palm'}
  s.update(fist,0);s.update(fist,100);assert.equal(s.update(fist,300).id,'fist')
  assert.equal(s.update(palm,350).id,'no_gesture')
  s.update(palm,500);assert.equal(s.update(palm,650).id,'palm')
  assert.equal(s.update({id:'no_gesture'},670).id,'no_gesture')
  assert.equal(s.update(palm,700).stable,false)
})
test('jitter never accumulates hold time across different gestures',()=>{
  const s=new Stabilizer()
  for(let t=0;t<2000;t+=100)assert.equal(s.update({id:t%200?'peace':'fist',score:.9,source:'model'},t).stable,false)
})
test('timing report uses mean and nearest-rank p95 without modifying input',()=>{
  const samples=Array.from({length:100},(_,i)=>100-i)
  assert.deepEqual(summarizeTimings(samples),{count:100,mean:50.5,p95:95})
  assert.equal(samples[0],100);assert.equal(summarizeTimings([]),null)
})
test('all requested categories are accounted for with explicit capability levels',()=>{
  assert.equal(catalog.length,22);assert.equal(new Set(catalog.map(g=>g.id)).size,22)
  assert.equal(catalog.filter(g=>g.status==='pending').length,8)
  assert.equal(catalog.find(g=>g.id==='two').status,'alias')
  assert.equal(catalog.find(g=>g.id==='stop').status,'alias')
})

function fixture(extended,thumb=false,ok=false) {
  const p=Array.from({length:21},()=>({x:.5,y:.95,z:0}))
  ;[5,9,13,17].forEach((base,j)=>{
    const x=.38+j*.12
    const ys=extended[j]?[.65,.48,.36,.25]:[.65,.48,.57,.68]
    ys.forEach((y,k)=>p[base+k]={x,y,z:0})
  })
  const t=thumb?[[.38,.82],[.29,.73],[.2,.64],[.11,.55]]:[[.36,.82],[.35,.70],[.38,.67],[.42,.68]]
  t.forEach(([x,y],j)=>p[j+1]={x,y,z:0})
  if(ok)p[4]={...p[8]}
  return p
}
test('experimental rules detect their defined shapes and tolerate in-plane rotation',()=>{
  for(const [id,pose] of [
    ['four',fixture([true,true,true,true])],
    ['three',fixture([true,true,true,false])],
    ['rock',fixture([true,false,false,true])],
    ['call',fixture([false,false,false,true],true)],
    ['ok',fixture([false,true,true,true],false,true)],
  ]) {
    assert.equal(ruleGesture(pose),id)
    const rotated=pose.map(p=>({x:1-p.y,y:p.x,z:p.z}))
    assert.equal(ruleGesture(rotated),id)
    const result=classify({categoryName:'None',score:.9},rotated,{mode:'all'})
    assert.equal(result.id,id);assert.equal(result.score,null);assert.equal(result.source,'rule')
    assert.equal(classify(null,rotated,{mode:'rps'}).id,'no_gesture')
  }
})

const worldPose=(flags)=>fixture(flags).map(p=>({x:(p.x-.5)*.18,y:(p.y-.7)*.18,z:0}))
test('3D peace survives camera-facing tilt, scale and mirrored hands',()=>{
  for(const tilt of [0,45,80,90,130,180])for(const mirror of [-1,1])for(const scale of [.7,1,1.5]){
    const t=tilt*Math.PI/180
    const world=worldPose([true,true,false,false]).map(p=>({x:p.x*mirror*scale,y:p.y*Math.cos(t)*scale,z:p.y*Math.sin(t)*scale}))
    assert.ok(isWorldPeace(world),`tilt=${tilt}, mirror=${mirror}, scale=${scale}`)
    for(const mode of ['rps','all']) {
      const result=classify({categoryName:'Victory',score:.3},[],{worldLandmarks:world,mode})
      assert.equal(result.id,'peace');assert.equal(result.method,'peace_3d');assert.equal(result.score,null)
    }
  }
})
test('3D fallback rejects other finger patterns and damaged world landmarks',()=>{
  for(const flags of [[false,false,false,false],[true,false,false,false],[true,true,true,true],[true,true,true,false],[true,false,false,true]])assert.equal(isWorldPeace(worldPose(flags)),false)
  const good=worldPose([true,true,false,false])
  assert.equal(isWorldPeace(good.map(p=>({...p,z:NaN}))),false)
  assert.equal(isWorldPeace(good.map(p=>({x:p.x,y:p.y}))),false)
  assert.equal(isWorldPeace(Array(21).fill({x:0,y:0,z:0})),false)
  const damaged=structuredClone(good);damaged[7]=damaged[6]
  assert.equal(isWorldPeace(damaged),false)
  assert.equal(classify(null,[],{worldLandmarks:good,robustPeace:false}).id,'no_gesture')
  for(const categoryName of ['Closed_Fist','Open_Palm','Pointing_Up','ILoveYou'])assert.notEqual(classify({categoryName,score:.95},[],{worldLandmarks:good}).method,'peace_3d')
})
test('brief classifier dropout pauses evidence without returning a stale stable result',()=>{
  const s=new Stabilizer(),peace={id:'peace',score:null,source:'rule'}, context={handPresent:true,tolerateDropout:true}
  s.update(peace,0,250,context);s.update(peace,70,250,context)
  assert.equal(s.update({id:'no_gesture'},140,250,context).stable,false)
  assert.equal(s.update(peace,210,250,context).stable,false)
  s.update(peace,280,250,context);s.update(peace,350,250,context)
  assert.equal(s.update(peace,420,250,context).stable,true)
  assert.equal(s.update({id:'no_gesture'},490,250,context).id,'no_gesture')
  assert.equal(s.update({id:'no_gesture'},500,250,{...context,handPresent:false}).stable,false)
  assert.equal(s.update(peace,530,250,context).stable,false)
})
test('long dropouts and mostly unknown evidence cannot keep a gesture alive',()=>{
  const s=new Stabilizer(),p={id:'peace',score:.9,source:'model'},c={handPresent:true,tolerateDropout:true}
  s.update(p,0,250,c);s.update(p,100,250,c);s.update(p,300,250,c)
  s.update({id:'no_gesture'},370,250,c)
  assert.equal(s.update(p,600,250,c).stable,false)
  s.reset()
  for(let t=0;t<3000;t+=50){const r=t%150===0?p:{id:'no_gesture'};assert.equal(s.update(r,t,250,c).stable,false)}
})
