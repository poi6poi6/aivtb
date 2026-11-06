'use strict'
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const vm = require('node:vm')
const fs = require('node:fs')
const path = require('node:path')

function harness () {
  const input = new EventEmitter(), messages = [], pending = [], clients = []
  let requests = 0
  const context = {
    __dirname, console: { log () {}, error () {} },
    process: { stdin: new EventEmitter(), stdout: { write: s => messages.push(JSON.parse(s)) } },
    setImmediate: f => pending.push(f), setInterval: () => 1, clearInterval () {},
    setTimeout: () => ({ unref () {} }), clearTimeout () {},
    require (name) {
      if (name === 'mineflayer') return { createBot () {
        const bot = new EventEmitter()
        Object.assign(bot, { health: undefined, isAlive: true, food: 20, players: {},
          entity: { position: { x: 0, y: 80, z: 0 } }, game: { dimension: 'overworld' },
          inventory: { items: () => [] }, pathfinder: { setGoal () {}, setMovements () {} },
          loadPlugin () {}, stopDigging () {}, clearControlStates () {}, deactivateItem () {},
          respawn () { requests++ }, quit () { bot.emit('end') } })
        clients.push(bot); return bot
      } }
      if (name === 'mineflayer-pathfinder') return { Movements: class {}, goals: {} }
      if (name === 'node:readline') return { createInterface: () => input }
      if (name === 'prismarine-viewer') return { mineflayer: b => { b.viewer = { close () {} } } }
      if (name === './campaign') return { PHASES: {}, Journal: class {
        constructor () { this.data = { deaths: [] } }
        death () { this.data.deaths.push({ time: Date.now() }); this.data.blocked = '角色死亡' }
        save () {}
      } }
      if (name === './skills') return { Skills: class { snapshot () { return {} } } }
      return require(name)
    }
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'bot.js'), 'utf8'), context)
  return {
    command: action => input.emit('line', JSON.stringify({ action })),
    flush () { while (pending.length) pending.shift()() },
    state: () => messages.filter(m => m.type === 'status').at(-1),
    clients, requests: () => requests
  }
}

test('dead login remains connected and can respawn without an initial spawn event', () => {
  const h = harness(); h.command('connect'); const b = h.clients[0]
  b.emit('login'); b.health = 0; b.isAlive = false; b.emit('death')
  assert.equal(h.state().networkConnected, true)
  assert.equal(h.state().dead, true)
  h.command('respawn'); h.command('respawn')
  assert.equal(h.requests(), 1)
  b.health = 20; b.isAlive = true; b.emit('health'); h.flush()
  assert.equal(h.state().connected, true)
  assert.equal(h.state().dead, false)
  assert.equal(h.state().respawning, false)
  assert.equal(h.state().error, '')
})

test('early spawn waits for health; death and recovery clear stale UI state', () => {
  const h = harness(); h.command('connect'); const b = h.clients[0]
  b.emit('login'); b.emit('spawn'); b.health = 20; b.emit('health'); h.flush()
  assert.equal(h.state().connected, true)
  b.health = 0; b.isAlive = false; b.emit('death'); b.emit('death')
  assert.equal(h.state().campaign.deaths.length, 1)
  h.command('respawn'); b.health = 20; b.isAlive = true; b.emit('spawn'); h.flush()
  assert.equal(h.state().campaign.blocked, '')
  assert.match(h.state().activity, /重生/)
  h.command('disconnect')
  assert.equal(h.state().networkConnected, false)
  assert.equal(h.state().connecting, false)
  h.command('connect'); b.emit('health'); h.flush()
  assert.equal(h.state().connected, false)
})
