import test from 'node:test'
import assert from 'node:assert/strict'
import { StillRps,rpsScores } from '../src/lib/rps.js'
function pose(flags){
 const p=Array.from({length:21},()=>({x:.5,y:.95,z:0}))
 ;[5,9,13,17].forEach((b,j)=>{(flags[j]?[.65,.48,.36,.25]:[.65,.48,.57,.68]).forEach((y,k)=>p[b+k]={x:.38+j*.12,y,z:0})})
 return p
}
const world=p=>p.map(v=>({x:(v.x-.5)*.18,y:(v.y-.7)*.18,z:v.z*.18}))
const input=(p,now,extra={})=>({landmarks:p,worldLandmarks:world(p),categories:[{categoryName:'None',score:.98}],now,...extra})
const settle=(s,p,start=0,extra={})=>{let out;for(let t=start;t<=start+350;t+=70)out=s.update(input(p,t,extra));return out}
test('still poses choose all three even when model reports None',()=>{
 for(const [id,flags] of [['peace',[1,1,0,0]],['fist',[0,0,0,0]],['palm',[1,1,1,1]]]){
  const s=new StillRps(),p=pose(flags)
  assert.equal(s.update(input(p,0)).stable,false)
  const out=settle(s,p,70);assert.equal(out.id,id);assert.equal(out.score,null);assert.ok(out.matchScore>.5)
 }
})
test('motion clears result immediately and requires a new still period',()=>{
 const s=new StillRps(),p=pose([1,1,0,0]);assert.equal(settle(s,p).stable,true)
 const moved=p.map(v=>({...v,x:v.x+.1}))
 assert.equal(s.update(input(moved,420)).phase,'moving')
 assert.equal(s.update(input(moved,490)).stable,false)
 assert.equal(settle(s,moved,560).id,'peace')
})
test('finger changes with fixed wrist count as movement',()=>{
 const s=new StillRps(),p=pose([0,0,0,0]);settle(s,p)
 assert.equal(s.update(input(pose([1,1,1,1]),420)).phase,'moving')
})
test('small tracking jitter is tolerated while continuous translation is not',()=>{
 const p=pose([1,1,0,0]),s=new StillRps();let out
 for(let i=0;i<10;i++)out=s.update(input(p.map(v=>({...v,x:v.x+(i%2)*.001})),i*70))
 assert.equal(out.id,'peace')
 s.reset()
 for(let i=0;i<10;i++)assert.equal(s.update(input(p.map(v=>({...v,x:v.x+i*.04})),i*70)).stable,false)
})
test('unsupported shapes, invalid points, missing and multiple hands are rejected',()=>{
 for(const flags of [[1,0,0,0],[1,0,0,1]])assert.equal(settle(new StillRps(),pose(flags)).phase,'unclear')
 const s=new StillRps(),p=pose([1,1,0,0]);settle(s,p)
 assert.equal(s.update(input(p,420,{handCount:0})).phase,'no_hand')
 assert.equal(s.update(input(p,490,{handCount:2})).phase,'multiple')
 assert.equal(s.update(input([],560)).stable,false)
 assert.equal(s.update(input(p.map(v=>({...v,x:NaN})),630)).stable,false)
})
test('model scores remain available when geometry is unavailable',()=>{
 const modest=rpsScores([{categoryName:'Victory',score:.4},{categoryName:'Closed_Fist',score:.1}],[],[])
 assert.equal(modest.hasGeometry,false)
 assert.deepEqual(modest.scores,{fist:.1,peace:.4,palm:0})
 const tie=rpsScores([{categoryName:'Victory',score:.3},{categoryName:'Closed_Fist',score:.28}],[],[])
 assert.ok(tie.scores.peace-tie.scores.fist<.07)
 assert.equal(rpsScores([],[]).hasGeometry,false)
})
test('clear finger geometry overrides a conflicting palm model result',()=>{
 const scissors=pose([1,1,0,0]), fist=pose([0,0,0,0])
 for(const [shape,expected] of [[scissors,'peace'],[fist,'fist']]){
  const result=rpsScores([{categoryName:'Open_Palm',score:.99}],[],shape)
  const winner=Object.entries(result.scores).sort((a,b)=>b[1]-a[1])[0][0]
  assert.equal(winner,expected)
  assert.equal(result.geometrySource,'image')
 }
})
test('resuming after a frame gap restarts stillness timing',()=>{
 const s=new StillRps(),p=pose([1,1,0,0]);settle(s,p)
 assert.equal(s.update(input(p,2000)).stable,false)
})
test('natural hand tremor can settle and keep a result without restarting',()=>{
 const p=pose([1,1,0,0]),s=new StillRps()
 for(let i=0;i<25;i++){
  const jitter=p.map(v=>({...v,x:v.x+(i%2?1:-1)*.014}))
  const out=s.update(input(jitter,i*70,{worldLandmarks:world(p)}))
  if(i>=4)assert.equal(out.id,'peace')
 }
})
test('estimated depth noise does not prevent stillness',()=>{
 const p=pose([1,1,0,0]),s=new StillRps()
 for(let i=0;i<15;i++){
  const jitter=p.map((v,j)=>({...v,z:j===0?0:(i%2?1:-1)*.025}))
  const out=s.update(input(jitter,i*70,{worldLandmarks:world(p)}))
  if(i>=4)assert.equal(out.id,'peace')
 }
})
test('motion thresholds are configurable for a stricter or looser experience',()=>{
 const p=pose([1,1,0,0])
 const moved=p.map(v=>({...v,x:v.x+.10}))
 const strict=new StillRps(), loose=new StillRps()
 strict.update(input(p,0));loose.update(input(p,0))
 assert.equal(strict.update(input(moved,70,{motionSpeed:1,maxDrift:.10})).phase,'moving')
 assert.notEqual(loose.update(input(moved,70,{motionSpeed:3.5,maxDrift:.40})).phase,'moving')
})
test('one noisy frame suppresses output but does not reset a settled hand',()=>{
 const p=pose([1,1,0,0]),s=new StillRps()
 assert.equal(settle(s,p).stable,true)
 const noisy=p.map(v=>({...v,x:v.x+.08}))
 assert.equal(s.update(input(noisy,420,{motionSpeed:1.8,maxDrift:.22})).phase,'moving')
 assert.equal(s.update(input(p,490,{motionSpeed:1.8,maxDrift:.22})).id,'peace')
})
test('consistent movement clears the accumulated stillness',()=>{
 const p=pose([1,1,0,0]),s=new StillRps();settle(s,p)
 for(const [i,time] of [0,1].entries())assert.equal(s.update(input(p.map(v=>({...v,x:v.x+.08*(i+1)})),420+time*70,{motionSpeed:1.8,maxDrift:.22})).phase,'moving')
 assert.equal(s.update(input(p,630,{motionSpeed:1.8,maxDrift:.22})).stable,false)
})
