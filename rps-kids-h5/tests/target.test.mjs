import test from 'node:test'
import assert from 'node:assert/strict'
import {MotionTarget} from '../src/lib/target.js'
const hand=x=>Array.from({length:21},(_,i)=>({x:x+(i%4)*.01,y:.5+(i===9?.1:0),z:0}))
test('locked round keeps player when another hand starts moving',()=>{
 const t=new MotionTarget()
 t.update([hand(.1),hand(.7)],0);t.update([hand(.12),hand(.7)],100)
 assert.equal(t.update([hand(.14),hand(.7)],200),0)
 t.update([hand(.14),hand(.72)],300,16/9,{locked:true})
 assert.equal(t.update([hand(.14),hand(.74)],400,16/9,{locked:true}),0)
 assert.equal(t.changed,false)
 assert.equal(t.update([hand(.76),hand(.14)],500,16/9,{locked:true}),1)
})
test('select moving hand, retain at rest and across index reorder',()=>{
 const t=new MotionTarget()
 assert.equal(t.update([hand(.1),hand(.7)],0),-1)
 t.update([hand(.12),hand(.7)],100)
 assert.equal(t.update([hand(.14),hand(.7)],200),0)
 assert.equal(t.update([hand(.7),hand(.14)],300),1)
 assert.equal(t.update([hand(.7),hand(.14)],400),1)
 assert.equal(t.update([hand(.7)],500),-1)
 assert.equal(t.update([hand(.7),hand(.14)],600),1)
})
test('stationary false positive cannot acquire target; disappearance does not select idle hand',()=>{
 const t=new MotionTarget()
 for(let i=0;i<6;i++) assert.equal(t.update([hand(.7)],i*100),-1)
 t.update([hand(.1),hand(.7)],600);t.update([hand(.12),hand(.7)],700)
 assert.equal(t.update([hand(.14),hand(.7)],800),0)
 assert.equal(t.update([hand(.7)],1900),-1)
})
test('eligibility and vertical direction restrict acquisition to the shaking fist',()=>{
 const t=new MotionTarget(), eligible=[false,true]
 t.update([hand(.1),hand(.7)],0,16/9,{eligible,verticalOnly:true})
 t.update([hand(.13),hand(.7)],100,16/9,{eligible,verticalOnly:true})
 assert.equal(t.update([hand(.16),hand(.7)],200,16/9,{eligible,verticalOnly:true}),-1)
  const vertical=y=>Array.from({length:21},(_,i)=>({x:.7+(i%4)*.01,y:y+(i===9?.1:0),z:0}))
  t.update([hand(.19),vertical(.48)],300,16/9,{eligible,verticalOnly:true})
 assert.equal(t.update([hand(.22),vertical(.46)],400,16/9,{eligible,verticalOnly:true}),1)
 assert.equal(t.changed,true)
  assert.equal(t.update([hand(.25),vertical(.44)],500,16/9,{eligible,verticalOnly:true}),1)
 assert.equal(t.changed,false)
})
