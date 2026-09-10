import test from 'node:test'
import assert from 'node:assert/strict'
import {MotionTarget} from '../src/lib/target.js'
const hand=x=>Array.from({length:21},(_,i)=>({x:x+(i%4)*.01,y:.5+(i===9?.1:0),z:0}))
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
