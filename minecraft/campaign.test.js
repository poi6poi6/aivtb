'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const { Vec3 } = require('vec3')
const { Journal, choose, intersectRays } = require('./campaign')
const { Skills } = require('./skills')
const registry = require('minecraft-data')('1.20.1')
const Recipe = require('prismarine-recipe')(registry).Recipe

function fresh (t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'avatar-campaign-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return new Journal(path.join(dir, 'campaign.json'), 'world-one')
}
function state (items = {}) {
  return { connected: true, health: 20, food: 20, gameMode: 'survival', difficulty: 'normal', dimension: 'overworld',
    position: { x: 0, y: 65, z: 0 }, equipped: true, items: Object.entries(items).map(([name, count]) => ({ name, count })) }
}
function geared (extra = {}) {
  return state({ cooked_beef: 16, stone_pickaxe: 1, iron_pickaxe: 1, iron_sword: 1, shield: 1,
    iron_helmet: 1, iron_chestplate: 1, iron_leggings: 1, iron_boots: 1, bow: 1, arrow: 48,
    cobblestone: 128, diamond_pickaxe: 1, flint_and_steel: 1, water_bucket: 1, ...extra })
}
function fake (items = {}) {
  const bot = new EventEmitter()
  bot.registry = registry
  bot.entity = { position: new Vec3(0, 65, 0), onGround: true }
  bot.health = 20; bot.food = 12
  bot.inventory = { slots: Object.entries(items).map(([name, count]) => ({ name, count, type: registry.itemsByName[name].id })) }
  bot.findBlock = () => null
  bot.recipesAll = (id, metadata, table) => Recipe.find(id, metadata).filter(r => table || !r.requiresTable)
  bot.blockAt = () => null
  return bot
}
test('survival and hunger preempt progression; resources are re-evaluated after death', t => {
  const journal = fresh(t)
  journal.data.waypoints.home = { x: 0, y: 65, z: 0 }
  journal.data.phase = 4
  assert.equal(choose({ ...geared(), food: 8 }, journal).action, 'eat')
  assert.equal(choose({ ...geared(), health: 4 }, journal).action, 'recover')
  assert.equal(choose(state({ cooked_beef: 8 }), journal).target, 'stone_pickaxe')
  assert.equal(choose({ ...geared(), gameMode: 'creative' }, journal).action, 'wait')
  assert.equal(choose({ ...geared(), difficulty: 'peaceful' }, journal).action, 'wait')
})
test('goal quantities do not degrade into satisfied-one-item loops', t => {
  const journal = fresh(t); journal.data.waypoints.home = {}
  assert.deepEqual(choose(geared({ arrow: 4 }), journal), { action: 'ensure', target: 'arrow', amount: 48 })
  assert.deepEqual(choose(geared({ cobblestone: 2 }), journal), { action: 'ensure', target: 'cobblestone', amount: 128 })
  assert.deepEqual(choose(geared({ obsidian: 2 }), journal), { action: 'ensure', target: 'obsidian', amount: 14 })
  assert.deepEqual(choose(geared({ blaze_rod: 7, ender_eye: 2 }), journal), { action: 'ensure', target: 'ender_eye', amount: 12 })
})
test('nether return and end dimension determine the next action', t => {
  const journal = fresh(t)
  assert.equal(choose({ ...geared(), dimension: 'the_nether' }, journal).action, 'mark_portal')
  journal.data.waypoints.netherReturn = {}
  assert.equal(choose({ ...geared(), dimension: 'the_nether' }, journal).target, 'blaze')
  assert.equal(choose({ ...geared({ blaze_rod: 7, ender_pearl: 14 }), dimension: 'the_nether' }, journal).action, 'return_portal')
  assert.equal(choose({ ...geared(), dimension: 'the_end' }, journal).action, 'dragon')
})
test('eye use preserves the expedition and an activated portal needs no eyes', t => {
  const journal = fresh(t); journal.data.waypoints.home = {}
  const supplies = { ender_eye: 12, blaze_powder: 2, ender_pearl: 2 }
  assert.equal(choose(geared(supplies), journal).action, 'stronghold')
  journal.save()
  const resumed = new Journal(journal.file, 'world-one')
  assert.equal(choose({ ...geared({ ...supplies, ender_eye: 11 }), netherPortal: true }, resumed).action, 'stronghold')
  assert.equal(choose({ ...geared({ ender_eye: 0 }), endPortal: true, netherPortal: true }, resumed).action, 'enter_end')
  assert.deepEqual(choose({ ...geared({ ender_eye: 2, blaze_powder: 2, ender_pearl: 2 }),
    endFrames: true, endEyesNeeded: 4 }, resumed), { action: 'ensure', target: 'ender_eye', amount: 4 })
  assert.equal(choose({ ...geared({ ender_eye: 4 }), endFrames: true, endEyesNeeded: 4 }, resumed).action, 'open_end')
  assert.equal(choose({ ...geared({ ender_eye: 0 }), netherPortal: true }, resumed).action, 'enter_nether')
})
test('observed partially filled frames report only missing eyes', t => {
  const bot = fake()
  bot.entities = {}; bot.game = { dimension: 'overworld', gameMode: 'survival', difficulty: 'normal' }
  bot.findBlocks = () => Array.from({ length: 12 }, (_, x) => new Vec3(x, 32, 0))
  bot.blockAt = p => ({ getProperties: () => ({ eye: p.x < 8 }) })
  const snapshot = new Skills(bot, fresh(t), () => {}, () => {}).snapshot()
  assert.equal(snapshot.endFrames, true)
  assert.equal(snapshot.endEyesNeeded, 4)
})
test('victory requires dragon death in survival End with combat participation', t => {
  const journal = fresh(t)
  assert.equal(journal.dragonDead({ name: 'cow', id: 1 }, geared(), true), false)
  assert.equal(journal.dragonDead({ name: 'ender_dragon', id: 1 }, geared(), true), false)
  const end = { ...geared(), dimension: 'the_end' }
  assert.equal(journal.dragonDead({ name: 'ender_dragon', id: 1 }, end, false), false)
  assert.equal(journal.dragonDead({ name: 'ender_dragon', id: 1 }, { ...end, gameMode: 'creative' }, true), false)
  assert.equal(journal.data.completed, false)
  assert.equal(journal.dragonDead({ name: 'ender_dragon', id: 1 }, end, true), true)
  assert.equal(new Journal(journal.file, 'world-one').data.completed, true)
  assert.equal(new Journal(journal.file, 'different-world').data.completed, false)
})
test('failed operations and death locations survive restart', t => {
  const journal = fresh(t)
  for (let n = 0; n < 3; n++) journal.receipt({ action: 'mine', target: 'diamond' }, false, 'not reachable', {})
  journal.death(geared())
  const loaded = new Journal(journal.file, 'world-one')
  assert.equal(loaded.data.attempts['mine:diamond'], 3)
  assert.equal(loaded.data.deaths[0].dimension, 'overworld')
  assert.equal(choose(geared(), loaded).action, 'recover_items')
  loaded.data.deaths[0].time = Date.now() - 300000
  assert.notEqual(choose(geared(), loaded).action, 'recover_items')
  loaded.receipt({ action: 'mine', target: 'diamond' }, true, 'got one', {})
  assert.equal(loaded.data.attempts['mine:diamond'], 0)
})
test('eye triangulation rejects parallel or backward rays', () => {
  const a = { origin: { x: 0, z: 0 }, direction: { x: 1, z: 0 } }
  assert.equal(intersectRays(a, { origin: { x: 1, z: 1 }, direction: { x: 1, z: 0 } }), null)
  assert.equal(intersectRays(a, { origin: { x: 5, z: 5 }, direction: { x: 0, z: 1 } }), null)
  assert.deepEqual(intersectRays(a, { origin: { x: 5, z: 5 }, direction: { x: 0, z: -1 } }), { x: 5, z: 0 })
})
test('actual 1.20.1 recipes resolve materials and table prerequisites', t => {
  const journal = fresh(t)
  let skills = new Skills(fake({ birch_log: 1 }), journal, () => {}, () => {})
  assert.equal(skills.resolve('crafting_table').target, 'birch_planks')
  skills = new Skills(fake({ birch_planks: 4 }), journal, () => {}, () => {})
  assert.equal(skills.resolve('crafting_table').action, 'make')
  skills = new Skills(fake({ iron_ingot: 3, stick: 2, crafting_table: 1 }), journal, () => {}, () => {})
  assert.equal(skills.resolve('iron_pickaxe').action, 'make')
  skills = new Skills(fake({ iron_pickaxe: 1, cobblestone: 2 }), journal, () => {}, () => {})
  assert.equal(skills.resolve('cobblestone', 128).action, 'mine')
  assert.throws(() => skills.resolve('not_real'), /未知物品/)
})
test('no success receipt for unconfirmed crafting or eating', async t => {
  const bot = fake({ cooked_beef: 1 }); bot.craft = async () => {}; bot.equip = async () => {}; bot.consume = async () => {}
  const skills = new Skills(bot, fresh(t), () => {}, () => {})
  await assert.rejects(skills.make('stick', { requiresTable: false }), /没有增加/)
  await assert.rejects(skills.eat(), /没有观察到/)
})
test('cancellation after equipping prevents later actions', async t => {
  const bot = fake({ cooked_beef: 1 }); let cancelled = false; let consumed = false
  bot.equip = async () => { cancelled = true }; bot.consume = async () => { consumed = true }
  const skills = new Skills(bot, fresh(t), () => { if (cancelled) throw new Error('cancelled') }, () => {})
  await assert.rejects(skills.eat(), /cancelled/)
  assert.equal(consumed, false)
})
test('digging underneath player and bridging over unloaded blocks are rejected', async t => {
  const bot = fake(); const skills = new Skills(bot, fresh(t), () => {}, () => {})
  await assert.rejects(skills.dig({ position: new Vec3(0, 64, 0) }), /脚下/)
  await assert.rejects(skills.bridgeStep(new Vec3(1, 65, 0)), /不能安全/)
})
test('furnace is closed on cancellation and never treats pending input as produced', async t => {
  const bot = fake({ raw_iron: 2 }); let closed = false
  const furnace = { close: async () => { closed = true } }
  bot.openFurnace = async () => furnace
  const skills = new Skills(bot, fresh(t), () => { throw new Error('cancelled') }, () => {})
  skills.station = async () => ({})
  await assert.rejects(skills.smelt('raw_iron'), /cancelled/)
  assert.equal(closed, true)
})
