import test from 'node:test'
import assert from 'node:assert/strict'
import {gameResultData} from '../src/lib/report.js'

test('game result uses the external hand names and three-field schema',()=>{
  assert.deepEqual(gameResultData({user:'fist',computer:'peace',outcome:'win',rounds:1}),
    {user:'rock',robot:'scissors',result:'win'})
  assert.deepEqual(gameResultData({user:'peace',computer:'palm',outcome:'lose'}),
    {user:'scissors',robot:'paper',result:'lose'})
  assert.deepEqual(gameResultData({user:'palm',computer:'palm',outcome:'draw'}),
    {user:'paper',robot:'paper',result:'draw'})
  assert.equal(gameResultData({user:'other',computer:'peace',outcome:'win'}),null)
  assert.equal(gameResultData({user:'__proto__',computer:'peace',outcome:'win'}),null)
})
