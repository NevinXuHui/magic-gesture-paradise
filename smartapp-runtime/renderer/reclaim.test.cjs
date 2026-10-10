const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')

test('display reclaim ends the old stream before starting a new reader', () => {
  const source = fs.readFileSync(`${__dirname}/screen-renderer.js`, 'utf8')
  const code = source.slice(source.indexOf('function scheduleDisplayReclaim()'), source.indexOf('async function verifyDisplayOwnership()'))
  const calls = [], timers = []
  const ctx = vm.createContext({
    stopping:false, claiming:false, readySent:true, restartingDisplay:false, reclaimTimer:null,
    encoder:{kill:signal=>calls.push(signal)},
    setTimeout:fn=>{timers.push(fn);return timers.length},
    ensureEncoder:()=>calls.push('new-stream'), claimDisplay:()=>calls.push('new-reader'),
  })
  vm.runInContext(code, ctx)
  ctx.scheduleDisplayReclaim()
  ctx.scheduleDisplayReclaim()
  assert.deepEqual(calls, ['SIGTERM'])
  assert.equal(timers.length, 1)
  ctx.encoder = null
  timers.shift()()
  assert.deepEqual(calls, ['SIGTERM', 'new-stream', 'new-reader'])
  assert.equal(ctx.restartingDisplay, false)
})
