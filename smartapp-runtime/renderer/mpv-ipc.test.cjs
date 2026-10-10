const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const { EventEmitter } = require('node:events')

function requestWithMessages(messages) {
  const source = fs.readFileSync(`${__dirname}/screen-renderer.js`, 'utf8')
  const code = source.slice(source.indexOf('function mpvRequest('), source.indexOf('async function waitForMpvPath('))
  const socket = new EventEmitter()
  socket.setEncoding = () => {}
  socket.destroy = () => {}
  socket.write = line => {
    const request = JSON.parse(line)
    for (const chunk of messages(request.request_id)) socket.emit('data', chunk)
  }
  const ctx = vm.createContext({
    net: { createConnection: () => {
      setImmediate(() => socket.emit('connect'))
      return socket
    } },
    MPV_SOCKET: '/tmp/mpv-test', mpvRequestId: 0, setTimeout, clearTimeout,
  })
  vm.runInContext(code, ctx)
  return ctx.mpvRequest(['get_property', 'path'])
}

test('MPV events before a response cannot masquerade as an empty path', async () => {
  const result = await requestWithMessages(id => [
    '{"event":"video-reconfig"}\n',
    JSON.stringify({request_id:id, error:'success', data:'/run/smartapp-renderer/video.nut'})+'\n',
  ])
  assert.equal(result.data, '/run/smartapp-renderer/video.nut')
})

test('MPV matches the request ID across combined and fragmented messages', async () => {
  const result = await requestWithMessages(id => [
    JSON.stringify({request_id:999, error:'property unavailable'})+'\n{"event":"idle"}\n',
    JSON.stringify({request_id:id, error:'success', data:'expected'}).slice(0, 12),
    JSON.stringify({request_id:id, error:'success', data:'expected'}).slice(12)+'\n',
  ])
  assert.equal(result.data, 'expected')
})

test('MPV still rejects errors in the matching response', async () => {
  await assert.rejects(requestWithMessages(id => [
    JSON.stringify({request_id:id, error:'property unavailable'})+'\n',
  ]), /property unavailable/)
})
