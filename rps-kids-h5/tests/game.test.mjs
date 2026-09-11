import test from 'node:test'
import assert from 'node:assert/strict'
import {GameRound,FistShake,ShakeStopGate,winner,randomHand,HANDS} from '../src/lib/game.js'

test('all nine outcomes and fair random mapping',()=>{
  const outcomes=[['draw','win','lose'],['lose','draw','win'],['win','lose','draw']]
  HANDS.forEach((u,i)=>HANDS.forEach((c,j)=>assert.equal(winner(u,c),outcomes[i][j])))
  assert.throws(()=>winner('other','palm'))
  for(let i=0;i<6;i++)assert.equal(randomHand(()=>i),HANDS[i%3])
  const samples=[0xffffffff,2];assert.equal(randomHand(()=>samples.shift()),'palm')
})
const points=(y=0,x=0)=>Array.from({length:21},(_,i)=>({x:.4+x,y:.4+y+(i===9?.1:0),z:0}))
test('finger depth flicker does not restart the palm stop gate',()=>{
 const gate=new ShakeStopGate();let out
 for(let frame=0;frame<15;frame++){
  const p=points();for(const i of [7,8,11,12,15,16,19,20])p[i].z=frame%2?.12:-.12
  out=gate.update({points:p,now:frame*67})
 }
 assert.equal(out.stopped,true)
 assert.equal(gate.update({points:points(.08),now:15*67}).stopped,false)
})
test('only a vertical fist reversal starts a shake',()=>{
  const shake=new FistShake()
  for(let now=0;now<1000;now+=100)assert.equal(shake.update({points:points(),isFist:true,now}),false)
  shake.reset()
  assert.equal(shake.update({points:points(),isFist:true,now:0}),false)
  assert.equal(shake.update({points:points(-.04),isFist:true,now:100}),false)
  assert.equal(shake.update({points:points(.01),isFist:true,now:200}),true)
  for(const fist of [true,false]){
    shake.reset()
    for(let t=0;t<6;t++)assert.equal(shake.update({points:points(0,t%2*.05),isFist:fist,now:t*100}),false)
  }
  shake.reset();shake.update({points:points(),isFist:true,now:0});shake.update({points:points(-.05),isFist:true,now:100})
  shake.update({points:null,isFist:true,now:150})
  assert.equal(shake.update({points:points(.02),isFist:true,now:200}),false)
})
test('shake stop gate requires a quiet period after the last movement',()=>{
  const gate=new ShakeStopGate()
  assert.equal(gate.update({points:points(),now:0}).stopped,false)
  assert.equal(gate.update({points:points(-.03),now:70}).moving,true)
  assert.equal(gate.update({points:points(-.06),now:140}).moving,true)
  for(let now=210;now<=490;now+=70)assert.equal(gate.update({points:points(-.06),now}).stopped,false)
  assert.equal(gate.update({points:points(-.06),now:560}).stopped,true)
  assert.equal(gate.update({points:points(-.09),now:630}).moving,true)
  assert.equal(gate.update({points:points(-.09),now:840}).stopped,false)
  let stopped=false
  for(let now=910;now<=1260;now+=70)stopped=gate.update({points:points(-.09),now}).stopped
  assert.equal(stopped,true)
})
test('continuous back-and-forth shaking never passes the stop gate',()=>{
  const gate=new ShakeStopGate()
  let lastY=0
  for(let frame=0;frame<36;frame++){
    lastY=Math.sin(frame*Math.PI/3)*.04
    assert.equal(gate.update({points:points(lastY),now:frame*67}).stopped,false)
  }
  let stopped=false
  for(let now=36*67;now<=36*67+800;now+=67)stopped=gate.update({points:points(lastY),now}).stopped
  assert.equal(stopped,true)
})
test('round waits for shake, commits computer before user, reveals together and auto resets',()=>{
  let calls=0
  const game=new GameRound({choose:()=>{calls++;return 'peace'}})
  assert.equal(game.update({now:0,recognized:{stable:true,id:'fist'}}).phase,'waiting')
  assert.deepEqual(game.update({now:100,shake:true}),{phase:'shaking',user:null,computer:null,outcome:null,rounds:0})
  assert.equal(calls,1)
  assert.equal(game.update({now:200,shake:true,recognized:{stable:true,id:'palm'}}).phase,'shaking')
  assert.equal(calls,1)
  const reveal=game.update({now:1000,recognized:{stable:true,id:'fist'}})
  assert.equal(reveal.phase,'revealing');assert.equal(reveal.computer,'peace');assert.equal(reveal.user,'fist');assert.equal(reveal.outcome,'win')
  assert.equal(game.update({now:1420}).phase,'result')
 assert.equal(game.update({now:2619}).phase,'result')
 assert.equal(game.update({now:2620}).phase,'waiting')
 assert.equal(game.update({now:4000,recognized:{stable:true,id:'fist'}}).phase,'waiting')
 assert.equal(calls,1)
})
test('shake during result is queued until the shorter result wait ends',()=>{
 const game=new GameRound({choose:()=> 'peace',resultMs:1200})
 game.update({now:0,shake:true})
 game.update({now:900,recognized:{stable:true,id:'fist'}})
 assert.equal(game.phase,'revealing')
 assert.equal(game.update({now:1420}).phase,'result')
 assert.equal(game.update({now:1800,shake:true,handPresent:true}).phase,'result')
 assert.equal(game.update({now:2619,handPresent:true}).phase,'result')
 assert.equal(game.update({now:2620,handPresent:true}).phase,'shaking')
 assert.equal(game.computer,'peace')
})
test('unclear pose cannot reveal; missing hand or round timeout cancels',()=>{
  const game=new GameRound({choose:()=> 'palm'})
  game.update({now:0,shake:true})
  assert.equal(game.update({now:900,recognized:{stable:false,id:'fist'}}).phase,'shaking')
  assert.equal(game.update({now:2201,handPresent:false}).phase,'waiting')
  game.update({now:3000,shake:true})
  assert.equal(game.update({now:13001}).phase,'waiting')
  assert.equal(game.rounds,0)
})
