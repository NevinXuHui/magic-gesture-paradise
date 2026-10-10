import test from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'

test('capture discards an expired frame and accepts the next fresh frame',async()=>{
  const source=readFileSync(new URL('../src/App.vue',import.meta.url),'utf8')
  const capture=source.slice(source.indexOf('async function capture(now)'),source.indexOf('function receive('))
  const frames=[{sequence:1,ageMs:1600},{sequence:2,ageMs:100,width:640,height:360}]
  const failures=[],received=[]
  const context=vm.createContext({
    recognizerReady:true,document:{hidden:false},stream:null,busy:false,
    lastDispatch:0,useServerCamera:true,session:1,debug:{value:false},recording:{value:false},
    lastSequence:-1,ready:{value:true},loading:{value:false},
    AbortController,setTimeout,clearTimeout,performance,
    requestAnimationFrame:()=>1,cameraApi:p=>p,
    fetch:async()=>({ok:true,json:async()=>frames.shift()}),
    fail:message=>failures.push(message),receive:data=>received.push(data),
    resetInteraction:()=>{},message:{value:''},
  })
  vm.runInContext(capture,context)
  await context.capture(2000)
  assert.equal(failures.length,0)
  assert.equal(received.length,0)
  assert.equal(context.busy,false)
  await context.capture(2200)
  assert.equal(received.length,1)
  assert.equal(received[0].sequence,2)
})
