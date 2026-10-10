import test from 'node:test'
import assert from 'node:assert/strict'
import {StillRps,updateStoppedRecognition} from '../src/lib/rps.js'
import {ShakeStopGate,GameRound} from '../src/lib/game.js'

function pose(y=0){
  const p=Array.from({length:21},()=>({x:.5,y:.95+y,z:0}))
  ;[5,9,13,17].forEach((b,j)=>[.65,.48,.57,.68].forEach((v,k)=>p[b+k]={x:.38+j*.12,y:v+y,z:0}))
  return p
}

test('stable samples accumulated before stop confirmation are available immediately',()=>{
  const still=new StillRps()
  for(const now of [0,125,250,375]){
    assert.equal(updateStoppedRecognition(still,{stopped:false},
      {landmarks:pose(),now,aspect:16/9}),null)
  }
  const result=updateStoppedRecognition(still,{stopped:true},
    {landmarks:pose(),now:500,aspect:16/9})
  assert.equal(result.stable,true)
  assert.equal(result.id,'fist')
})

test('continuous shaking cannot commit a hand at 8fps',()=>{
  const still=new StillRps(),stop=new ShakeStopGate()
  for(let frame=0;frame<32;frame++){
    const now=frame*125,landmarks=pose(frame%2?.15:-.15)
    const motion=stop.update({points:landmarks,now,aspect:16/9})
    assert.equal(updateStoppedRecognition(still,motion,{landmarks,now,aspect:16/9}),null)
  }
})

test('a stopped but still changing hand is not accepted',()=>{
  const still=new StillRps()
  for(let frame=0;frame<8;frame++){
    const result=updateStoppedRecognition(still,{stopped:true},
      {landmarks:pose(frame%2?.3:-.3),now:frame*125,aspect:16/9})
    assert.notEqual(result?.stable,true)
  }
})

test('confirmed hands show the outcome after 120ms',()=>{
  const game=new GameRound({choose:()=> 'peace'})
  game.update({now:0,shake:true})
  assert.equal(game.update({now:1000,recognized:{stable:true,id:'fist'}}).phase,'revealing')
  assert.equal(game.update({now:1119}).phase,'revealing')
  assert.equal(game.update({now:1120}).phase,'result')
})

test('clear stationary pose confirms within 750ms at 8fps after shaking ends',()=>{
 const still=new StillRps(),stop=new ShakeStopGate()
 let answer
 for(let frame=0;frame<12;frame++){
  const now=frame*125,landmarks=pose(frame%2?.15:-.15)
  updateStoppedRecognition(still,stop.update({points:landmarks,now,aspect:16/9}),{landmarks,now,aspect:16/9})
 }
 for(let frame=12;frame<=18;frame++){
  const now=frame*125,landmarks=pose(.15)
  answer=updateStoppedRecognition(still,stop.update({points:landmarks,now,aspect:16/9}),{landmarks,now,aspect:16/9})
  if(answer?.stable)break
 }
 assert.equal(answer?.stable,true)
 assert.equal(answer.id,'fist')
})
