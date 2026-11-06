'use strict'
const { goals } = require('mineflayer-pathfinder')
const { setTimeout: delay } = require('node:timers/promises')
const { Vec3 } = require('vec3')
const { FOOD, RAW, MINES, total, point, intersectRays } = require('./campaign')
const DIRECTIONS = [new Vec3(1, 0, 0), new Vec3(-1, 0, 0), new Vec3(0, 0, 1), new Vec3(0, 0, -1), new Vec3(0, 1, 0), new Vec3(0, -1, 0)]
const HAZARDS = new Set(['lava', 'fire', 'soul_fire', 'magma_block', 'cactus', 'powder_snow'])
const PREY = ['cow', 'pig', 'sheep', 'chicken']
const HOSTILE = ['zombie', 'skeleton', 'spider', 'creeper', 'blaze', 'wither_skeleton', 'hoglin', 'piglin_brute', 'silverfish']
const place = p => new Vec3(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z))

class Skills {
  constructor (bot, journal, check, emit) {
    this.bot = bot; this.journal = journal; this.check = check; this.emit = emit
    this.hitDragon = false; this.dead = new Set()
    bot.on('entityDead', e => this.dead.add(e.id))
  }
  items () { return this.bot.inventory.slots.filter(Boolean) }
  count (name) { return this.items().filter(i => i.name === name).reduce((n, i) => n + i.count, 0) }
  item (name) { return this.items().find(i => i.name === name) }
  block (names, distance = 48) { return this.bot.findBlock({ matching: b => names.includes(b.name), maxDistance: distance }) }
  snapshot () {
    const b = this.bot
    const entities = Object.values(b.entities)
    const frames = b.findBlocks({ matching: b.registry.blocksByName.end_portal_frame.id, maxDistance: 48, count: 12 })
      .map(p => b.blockAt(p)).filter(Boolean)
    return { connected: !!b.entity && b.health > 0, position: b.entity?.position.clone(), dimension: b.game.dimension,
      gameMode: b.game.gameMode, difficulty: b.game.difficulty, health: b.health, food: b.food,
      items: this.items().map(i => ({ name: i.name, count: i.count })),
      equipped: [5, 6, 7, 8].every(slot => /^(iron|diamond|netherite)_/.test(b.inventory.slots[slot]?.name || '')) && b.inventory.slots[45]?.name === 'shield',
      threat: entities.find(e => HOSTILE.includes(e.name) && e.position.distanceTo(b.entity.position) < 5),
      netherPortal: !!this.block(['nether_portal'], 24), endPortal: !!this.block(['end_portal'], 48),
      endFrames: frames.length > 0,
      endEyesNeeded: frames.length === 12 ? frames.filter(frame => !frame.getProperties().eye).length : null }
  }
  async pause (ms) { await delay(ms); this.check() }
  async go (p, range = 2) {
    this.check()
    await this.bot.pathfinder.goto(new goals.GoalNear(Math.floor(p.x), Math.floor(p.y), Math.floor(p.z), range))
    this.check()
  }
  safe (p) {
    const below = this.bot.blockAt(place(p).offset(0, -1, 0))
    return below?.boundingBox === 'block' && !HAZARDS.has(below.name) &&
      DIRECTIONS.every(d => { const b = this.bot.blockAt(place(p).plus(d)); return b && !HAZARDS.has(b.name) })
  }
  async toolFor (block) {
    const tools = this.items().filter(i => !block.harvestTools || block.harvestTools[i.type])
    if (block.harvestTools && !tools.length) throw new Error(`缺少能采集 ${block.name} 的镐，拒绝无掉落挖掘`)
    tools.sort((a, b) => block.digTime(a.type, false, false, false) - block.digTime(b.type, false, false, false))
    if (tools[0]) { await this.bot.equip(tools[0], 'hand'); this.check() }
  }
  async dig (block) {
    this.check()
    if (this.bot.health <= 6) throw new Error('血量过低，停止挖掘')
    if (block.position.equals(place(this.bot.entity.position).offset(0, -1, 0))) throw new Error('拒绝挖掉脚下承重方块')
    if (DIRECTIONS.some(d => { const n = this.bot.blockAt(block.position.plus(d)); return !n || HAZARDS.has(n.name) || ['sand', 'gravel'].includes(n.name) && d.y === 1 })) throw new Error('挖掘位置邻接危险方块或未加载区域')
    await this.toolFor(block)
    if (!this.bot.canDigBlock(block)) throw new Error('方块不在可挖范围')
    await this.bot.dig(block); this.check()
    if (this.bot.blockAt(block.position)?.type === block.type) throw new Error('服务器未确认方块被挖掉')
  }
  async mine (names, output) {
    const foot = place(this.bot.entity.position).offset(0, -1, 0)
    const target = this.bot.findBlock({ maxDistance: 48, matching: b => names.includes(b.name) && !b.position.equals(foot) &&
      DIRECTIONS.some(d => this.bot.blockAt(b.position.plus(d))?.boundingBox === 'empty') })
    if (!target) throw new Error(`附近未找到 ${names.join('/')}，需要探索`)
    const before = this.count(output)
    await this.go(target.position)
    const current = this.bot.blockAt(target.position)
    if (!current || !names.includes(current.name)) throw new Error('目标方块已改变')
    await this.dig(current)
    await this.go(current.position, 1)
    for (let i = 0; i < 20 && this.count(output) <= before; i++) await this.pause(100)
    const gained = this.count(output) - before
    if (gained <= 0 && output === 'flint' && this.count('gravel')) return '已确认挖掉砂砾，本次未获得燧石，需要继续采集'
    if (gained <= 0) throw new Error(`已挖掘，但未收到 ${output}；不计采集成功`)
    return `背包实际增加 ${gained} 个 ${output}`
  }
  async put (name, p) {
    this.check(); p = place(p)
    if (this.bot.blockAt(p)?.name === name) return
    if (!['air', 'cave_air', 'void_air'].includes(this.bot.blockAt(p)?.name)) throw new Error('放置位置被占用')
    if (!this.item(name)) throw new Error(`背包没有 ${name}`)
    await this.go(p, 3)
    if (p.equals(place(this.bot.entity.position)) || p.equals(place(this.bot.entity.position).offset(0, 1, 0))) throw new Error('不能在身体位置放方块')
    const dir = DIRECTIONS.find(d => this.bot.blockAt(p.minus(d))?.boundingBox === 'block')
    if (!dir) throw new Error('没有相邻支撑面')
    await this.bot.equip(this.item(name), 'hand'); this.check()
    await this.bot.placeBlock(this.bot.blockAt(p.minus(dir)), dir); this.check()
    if (this.bot.blockAt(p)?.name !== name) throw new Error(`未确认 ${name} 放置成功`)
  }
  async station (name) {
    const existing = this.block([name], 24)
    if (existing) { await this.go(existing.position); return existing }
    if (!this.item(name)) throw new Error(`缺少 ${name}`)
    const origin = place(this.bot.entity.position)
    for (const d of DIRECTIONS.slice(0, 4)) {
      const p = origin.plus(d.scaled(2))
      if (!this.safe(p) || this.bot.blockAt(p)?.name !== 'air') continue
      await this.put(name, p)
      return this.bot.blockAt(p)
    }
    throw new Error('附近没有合适的工作站放置位置')
  }
  // Resolve just ONE missing material/action. Replan after each server receipt.
  resolve (name, amount = 1, seen = []) {
    if (this.count(name) >= amount) return { action: 'satisfied', target: name }
    if (seen.includes(name) || seen.length > 12) throw new Error(`配方依赖循环：${name}`)
    const trail = [...seen, name]
    if (name.endsWith('_log')) return { action: 'mine', names: [name], target: name }
    if (MINES[name]) {
      const spec = MINES[name]
      if (spec.tool && !this.hasTool(spec.tool)) return this.resolve(spec.tool, 1, trail)
      return { action: 'mine', names: spec.blocks, target: name }
    }
    const raw = Object.keys(RAW).find(n => RAW[n] === name)
    if (raw) {
      if (!this.count(raw)) return this.resolve(raw, 1, trail)
      if (!this.count('furnace') && !this.block(['furnace'], 24)) return this.resolve('furnace', 1, trail)
      if (!this.count('coal') && !this.count('charcoal')) return this.resolve('coal', 1, trail)
      return { action: 'smelt', target: raw }
    }
    const mobs = { beef: 'cow', porkchop: 'pig', mutton: 'sheep', chicken: 'chicken', feather: 'chicken', string: 'spider', blaze_rod: 'blaze', ender_pearl: 'enderman' }
    if (mobs[name]) return { action: 'hunt', target: mobs[name] }
    const type = this.bot.registry.itemsByName[name]?.id
    if (type === undefined) throw new Error(`未知物品 ${name}`)
    const recipes = this.bot.recipesAll(type, null, true).filter(r => r.delta.every(d => d.count >= 0 || !trail.includes(this.bot.registry.items[d.id]?.name)))
    const cost = r => r.delta.filter(d => d.count < 0).reduce((n, d) => {
      const ingredient = this.bot.registry.items[d.id].name
      const missing = Math.max(0, -d.count - this.count(ingredient))
      if (!missing) return n
      const log = ingredient.replace(/_planks$/, '_log')
      const treePenalty = ingredient.endsWith('_planks') || ingredient.endsWith('_log')
        ? (this.count(log) || this.block([log], 32) ? 0 : 50) : 0
      const unsupported = ['blackstone', 'cobbled_deepslate', 'iron_block', 'diamond_block', 'coal_block'].includes(ingredient) ? 100 : 0
      return n + missing + treePenalty + unsupported
    }, 0)
    recipes.sort((a, b) => cost(a) - cost(b))
    const recipe = recipes[0]
    if (!recipe) throw new Error(`没有可执行的获取路径：${name}`)
    if (recipe.requiresTable && !this.block(['crafting_table'], 24) && !this.count('crafting_table')) return this.resolve('crafting_table', 1, trail)
    for (const d of recipe.delta.filter(d => d.count < 0)) {
      const ingredient = this.bot.registry.items[d.id].name
      if (this.count(ingredient) < -d.count) return this.resolve(ingredient, -d.count, trail)
    }
    return { action: 'make', target: name, recipe }
  }
  hasTool (name) {
    const rank = ['wooden', 'stone', 'iron', 'diamond', 'netherite']
    const [tier, kind] = name.split('_')
    return rank.slice(rank.indexOf(tier)).some(t => this.count(`${t}_${kind}`))
  }
  async make (name, recipe) {
    const before = this.count(name)
    const table = recipe.requiresTable ? await this.station('crafting_table') : null
    this.check(); await this.bot.craft(recipe, 1, table); this.check()
    const gained = this.count(name) - before
    if (gained <= 0) throw new Error('合成后背包没有增加')
    return `实际合成 ${name} × ${gained}`
  }
  async smelt (raw) {
    const output = RAW[raw]
    if (!output) throw new Error('不支持此熔炼材料')
    const block = await this.station('furnace')
    const furnace = await this.bot.openFurnace(block)
    const before = this.count(output)
    try {
      this.check()
      if (furnace.inputItem() && furnace.inputItem().name !== raw) throw new Error('熔炉有其他输入，请先整理')
      if (furnace.outputItem() && furnace.outputItem().name !== output) throw new Error('熔炉有其他产物，请先整理')
      if (!furnace.inputItem() && !furnace.outputItem()) {
        const item = this.item(raw)
        if (!item) throw new Error('缺少熔炼材料')
        await furnace.putInput(item.type, null, Math.min(4, item.count)); this.check()
      }
      if (!furnace.fuelItem() && !(furnace.fuel > 0)) {
        const fuel = this.item('coal') || this.item('charcoal')
        if (!fuel) throw new Error('缺少燃料')
        await furnace.putFuel(fuel.type, null, 1); this.check()
      }
      for (let i = 0; i < 110; i++) {
        this.check()
        if (furnace.outputItem()) { await furnace.takeOutput(); this.check() }
        if (!furnace.inputItem() && this.count(output) > before) break
        await this.pause(500)
      }
      if (this.count(output) <= before) throw new Error('熔炼超时，未收到产物')
      return `熔炉实际取出 ${output} × ${this.count(output) - before}`
    } finally { await furnace.close() }
  }
  async eat () {
    const food = FOOD.map(n => this.item(n)).find(Boolean)
    if (!food) throw new Error('背包没有可用的熟食或安全食物')
    const before = this.bot.food
    await this.bot.equip(food, 'hand'); this.check()
    await this.bot.consume(); this.check()
    if (this.bot.food <= before && before < 20) throw new Error('进食后没有观察到饥饿值恢复')
    return `进食后饥饿值 ${this.bot.food}/20`
  }
  async equip () {
    for (const [suffix, slot] of [['helmet', 'head'], ['chestplate', 'torso'], ['leggings', 'legs'], ['boots', 'feet']]) {
      const item = ['netherite', 'diamond', 'iron'].map(n => this.item(`${n}_${suffix}`)).find(Boolean)
      if (item) { await this.bot.equip(item, slot); this.check() }
    }
    if (this.item('shield')) { await this.bot.equip(this.item('shield'), 'off-hand'); this.check() }
    if (!this.snapshot().equipped) throw new Error('没有确认铁甲与盾牌全部装备')
    return '已确认护甲槽和副手盾牌'
  }
  async fight (entity, ranged = false) {
    const b = this.bot
    if (!entity || entity.type === 'player') throw new Error('没有可攻击的怪物目标')
    const isDragon = entity.name === 'ender_dragon'
    const isCrystal = entity.name === 'end_crystal'
    let fired = 0
    for (let i = 0; i < 24; i++) {
      this.check()
      if (this.dead.has(entity.id)) return `已收到 ${entity.name} 的死亡事件`
      const live = b.entities[entity.id]
      if (!live) {
        if (isCrystal && fired) return '水晶已离开实体列表；需重新观察确认，未作为末影龙通关证据'
        throw new Error('目标离开视野，未确认击败')
      }
      if (b.health <= 8) throw new Error('战斗中血量偏低，停止攻击')
      const distance = live.position.distanceTo(b.entity.position)
      if (live.name === 'creeper' && distance < 5) throw new Error('苦力怕过近，停止接近并撤退')
      if (ranged || distance > 5 && (isDragon || isCrystal || live.name === 'blaze')) {
        if (!this.item('bow') || !this.item('arrow')) throw new Error('远程目标需要弓箭')
        if (isCrystal && distance < 10) throw new Error('水晶爆炸距离过近')
        if (distance > 48) { await this.go(live.position.offset(0, -Math.max(0, live.position.y - b.entity.position.y), 0), 20); continue }
        const arrows = this.count('arrow')
        await b.equip(this.item('bow'), 'hand'); this.check()
        const aim = live.position.offset(0, Math.max(0.5, live.height / 2) + 0.006 * distance * distance, 0)
        await b.lookAt(aim, true); this.check()
        b.activateItem(); await this.pause(1200); b.deactivateItem()
        await this.pause(400)
        if (this.count('arrow') >= arrows) throw new Error('射击后箭矢未减少')
        fired++
        if (isDragon) this.hitDragon = true
      } else {
        if (distance > 3) { await this.go(live.position, 2); continue }
        const weapon = ['netherite_sword', 'diamond_sword', 'iron_sword', 'stone_sword', 'wooden_sword'].map(n => this.item(n)).find(Boolean)
        if (weapon) { await b.equip(weapon, 'hand'); this.check() }
        await b.lookAt(live.position.offset(0, Math.min(live.height * 0.6, 1.4), 0), true); this.check()
        b.attack(live)
        if (isDragon) this.hitDragon = true
        await this.pause(700)
      }
    }
    if (isDragon) return `已执行本轮攻击，尚未收到末影龙死亡回执`
    throw new Error('本轮战斗未确认击败目标')
  }
  async hunt (name) {
    const mob = this.bot.nearestEntity(e => e.name === name)
    if (!mob) throw new Error(`附近没有 ${name}，需要探索`)
    const result = await this.fight(mob)
    await this.go(mob.position, 1); await this.pause(1200)
    return `${result}；背包重新扫描后再判断掉落物是否足够`
  }
  async food () {
    const raw = ['beef', 'porkchop', 'mutton', 'chicken'].find(n => this.count(n))
    if (raw) return this.execute(this.resolve(RAW[raw], this.count(RAW[raw]) + 1))
    const prey = this.bot.nearestEntity(e => PREY.includes(e.name))
    if (!prey) throw new Error('附近没有食物来源，需要探索')
    return this.hunt(prey.name)
  }
  async explore (target = '') {
    const b = this.bot; const origin = place(b.entity.position)
    const key = `${b.game.dimension}:${Math.floor(origin.x / 16)}:${Math.floor(origin.z / 16)}`
    const visited = this.journal.data.visited
    visited.push(key); if (visited.length > 300) visited.shift()
    const choices = DIRECTIONS.slice(0, 4).map(d => origin.plus(d.scaled(12)))
    choices.sort((a, c) => Number(visited.includes(`${b.game.dimension}:${Math.floor(a.x / 16)}:${Math.floor(a.z / 16)}`)) - Number(visited.includes(`${b.game.dimension}:${Math.floor(c.x / 16)}:${Math.floor(c.z / 16)}`)))
    for (const p of choices) {
      for (let dy = 2; dy >= -2; dy--) {
        const at = p.offset(0, dy, 0)
        if (!this.safe(at) || b.blockAt(at)?.boundingBox !== 'empty' || b.blockAt(at.offset(0, 1, 0))?.boundingBox !== 'empty') continue
        try { await this.go(at, 1); this.journal.save(); return `已探索至 ${at.x}, ${at.y}, ${at.z}，重新搜索 ${target}` } catch (e) { this.check() }
      }
    }
    throw new Error('周围没有可安全到达的探索路径')
  }
  async stairs (targetY) {
    const b = this.bot; const origin = place(b.entity.position)
    const dy = origin.y > targetY ? -1 : 1
    if (origin.y === targetY) return this.explore('矿物')
    const directions = DIRECTIONS.slice(0, 4)
    const index = Math.abs(Math.floor(origin.y / 8)) % 4
    for (let i = 0; i < 4; i++) {
      const next = origin.plus(directions[(index + i) % 4]).offset(0, dy, 0)
      if (!this.safe(next)) continue
      for (let h = 2; h >= 0; h--) {
        const block = b.blockAt(next.offset(0, h, 0))
        if (!block) throw new Error('阶梯位置未加载')
        if (block.boundingBox === 'block') await this.dig(block)
      }
      await this.go(next, 0)
      return `已沿阶梯移动到 Y=${Math.floor(b.entity.position.y)}`
    }
    throw new Error('阶梯周围有危险或没有支撑，已停止向下挖掘')
  }
  async water () {
    if (!this.count('bucket')) return this.execute(this.resolve('bucket'))
    const water = this.block(['water'], 32)
    if (!water || water.getProperties().level !== 0) throw new Error('附近没有可装桶的水源')
    await this.go(water.position, 3)
    await this.bot.equip(this.item('bucket'), 'hand'); this.check()
    await this.bot.lookAt(water.position.offset(0.5, 0.5, 0.5), true); this.check()
    this.bot.activateItem(); await this.pause(700); this.bot.deactivateItem()
    if (!this.count('water_bucket')) throw new Error('装水后未收到水桶')
    return '背包已确认装满水的桶'
  }
  async buildPortal () {
    if (this.bot.game.dimension !== 'overworld') throw new Error('仅在主世界建造入口')
    let base = this.journal.data.waypoints.portalBuild
    if (!base) {
      const origin = place(this.bot.entity.position).offset(2, 0, 2)
      for (let x = 0; x < 4; x++) for (let y = 0; y < 5; y++) {
        const p = origin.offset(x, y, 0)
        if (this.bot.blockAt(p)?.boundingBox !== 'empty') throw new Error('需要一块宽 4、高 5 的空地建传送门')
        if (y === 0 && !this.safe(p)) throw new Error('传送门地基不安全')
      }
      base = { ...origin, dimension: 'overworld' }; this.journal.data.waypoints.portalBuild = base; this.journal.save()
    }
    const p = place(base)
    for (let y = 0; y < 5; y++) for (let x = 0; x < 4; x++) {
      if (y === 0 || y === 4 || x === 0 || x === 3) await this.put('obsidian', p.offset(x, y, 0))
    }
    await this.go(p.offset(1, 0, 2), 1)
    await this.bot.equip(this.item('flint_and_steel'), 'hand'); this.check()
    await this.bot.activateBlock(this.bot.blockAt(p.offset(1, 0, 0)), new Vec3(0, 1, 0)); this.check()
    await this.pause(1000)
    if (!this.block(['nether_portal'], 12)) throw new Error('框架已搭建，未确认激活')
    this.journal.data.waypoints.overworldPortal = { ...p, dimension: 'overworld' }; this.journal.save()
    return '已确认下界传送门方块生成'
  }
  async enter (kind) {
    const portal = this.block([kind], 48)
    if (!portal) throw new Error('附近没有已激活的传送门')
    const before = this.bot.game.dimension
    try { await this.go(portal.position, 0) } catch (error) {
      this.check()
      // Teleporting can invalidate pathfinder's goal before goto resolves.
      await this.pause(500)
      if (this.bot.game.dimension === before) throw error
    }
    for (let i = 0; i < 40 && this.bot.game.dimension === before; i++) await this.pause(250)
    if (this.bot.game.dimension === before) throw new Error('进入传送门后维度未改变')
    return `已确认维度切换：${before} → ${this.bot.game.dimension}`
  }
  async returnPortal () {
    const mark = this.journal.data.waypoints.netherReturn
    if (!mark || mark.dimension !== this.bot.game.dimension) throw new Error('没有当前维度的返程门记录')
    await this.go(mark); return this.enter('nether_portal')
  }
  async eye () {
    if (!this.item('ender_eye')) return this.execute(this.resolve('ender_eye'))
    const b = this.bot; const origin = b.entity.position.clone(); const previous = new Set(Object.keys(b.entities))
    await b.equip(this.item('ender_eye'), 'hand'); this.check()
    await b.look(0, 0, true); this.check(); b.activateItem()
    let entity; let first
    try {
      for (let i = 0; i < 30; i++) {
        await this.pause(100)
        entity = Object.values(b.entities).find(e => e.name === 'eye_of_ender' && !previous.has(String(e.id)) && e.position.distanceTo(origin) < 20)
        if (entity) { first = entity.position.clone(); break }
      }
      if (!entity) throw new Error('没有观察到投出的末影之眼实体')
      await this.pause(700)
      const delta = entity.position.minus(first); const length = Math.hypot(delta.x, delta.z)
      if (length < 0.3) throw new Error('末影之眼轨迹不足以判断方向')
      const ray = { origin: { x: origin.x, z: origin.z }, direction: { x: delta.x / length, z: delta.z / length } }
      const rays = this.journal.data.eyeRays
      const estimate = rays.length ? intersectRays(rays[rays.length - 1], ray) : null
      rays.push(ray); if (rays.length > 10) rays.shift()
      if (estimate) this.journal.data.waypoints.strongholdEstimate = { ...estimate, y: 32, dimension: 'overworld', estimated: true }
      this.journal.save()
      // Travel a short loaded segment, alternating a lateral offset for triangulation.
      await this.go(origin.offset(ray.direction.x * 24 + (rays.length % 2 ? -ray.direction.z * 12 : 0), 0, ray.direction.z * 24 + (rays.length % 2 ? ray.direction.x * 12 : 0)), 2)
      return estimate ? '已根据两次真实轨迹估算要塞位置，尚未确认传送门' : '已记录末影之眼轨迹并向指示方向前进'
    } finally { b.deactivateItem() }
  }
  async stronghold () {
    const estimate = this.journal.data.waypoints.strongholdEstimate
    if (estimate) {
      const p = this.bot.entity.position
      const distance = Math.hypot(p.x - estimate.x, p.z - estimate.z)
      if (distance > 20) {
        const scale = Math.min(24 / distance, 1)
        await this.go(p.offset((estimate.x - p.x) * scale, 0, (estimate.z - p.z) * scale), 2)
        return '正在接近要塞估算位置'
      }
      if (p.y > 32) return this.stairs(32)
      return this.explore('末地传送门框架')
    }
    return this.eye()
  }
  async bridgeStep (target, up = false) {
    const b = this.bot; const current = place(b.entity.position)
    if (!this.safe(current) || !b.entity.onGround) throw new Error('当前位置不能安全搭建路线')
    const delta = place(target).minus(current)
    const direction = Math.abs(delta.x) > Math.abs(delta.z) ? new Vec3(Math.sign(delta.x), 0, 0) : new Vec3(0, 0, Math.sign(delta.z))
    if (!direction.x && !direction.z) throw new Error('需要横向空间才能搭阶梯')
    const next = current.plus(direction).offset(0, up ? 1 : 0, 0)
    if (!this.count('cobblestone')) throw new Error('搭建路线的圆石不足')
    for (let h = -1; h >= -2 && up; h--) {
      const at = next.offset(0, h, 0)
      if (HAZARDS.has(b.blockAt(at)?.name) || !b.blockAt(at)) throw new Error('搭建位置危险或未加载')
    }
    for (let h = 0; h < 2; h++) if (b.blockAt(next.offset(0, h, 0))?.boundingBox !== 'empty') throw new Error('路线被方块挡住，拒绝盲目搭建')
    if (up && b.blockAt(next.offset(0, -2, 0))?.boundingBox === 'empty') await this.put('cobblestone', next.offset(0, -2, 0))
    if (b.blockAt(next.offset(0, -1, 0))?.boundingBox === 'empty') await this.put('cobblestone', next.offset(0, -1, 0))
    if (!this.safe(next)) throw new Error('新路线支撑未通过检查')
    await this.go(next, 0)
    return `已搭建并走到路线 ${next.x}, ${next.y}, ${next.z}`
  }
  async openEnd () {
    const frames = this.bot.findBlocks({ matching: this.bot.registry.blocksByName.end_portal_frame.id, maxDistance: 48, count: 12 })
    if (frames.length < 12) throw new Error('尚未观察到完整的 12 个传送门框架')
    for (const p of frames) {
      const block = this.bot.blockAt(p)
      if (block.getProperties().eye) continue
      if (!this.item('ender_eye')) throw new Error('末影之眼不足')
      await this.go(p, 3); await this.bot.equip(this.item('ender_eye'), 'hand'); this.check()
      await this.bot.activateBlock(block); this.check(); await this.pause(300)
      if (!this.bot.blockAt(p).getProperties().eye) throw new Error('框架没有确认镶入末影之眼')
    }
    if (!this.block(['end_portal'], 48)) throw new Error('没有检测到末地传送门激活')
    this.journal.data.waypoints.endPortal = { ...frames[0], dimension: 'overworld' }; this.journal.save()
    return '已确认末地传送门激活'
  }
  async dragon () {
    const b = this.bot
    const origin = place(b.entity.position)
    if (Math.hypot(origin.x, origin.z) > 65) {
      // Join the main island using confirmed blocks, never walking across an unverified void.
      return this.bridgeStep(new Vec3(0, 65, 0), origin.y < 65)
    }
    const crystal = b.nearestEntity(e => e.name === 'end_crystal')
    if (crystal) {
      // Caged crystals require removal of visible iron bars before shooting.
      const bars = b.findBlock({ point: crystal.position, matching: x => x.name === 'iron_bars', maxDistance: 3 })
      if (bars) {
        if (bars.position.distanceTo(b.entity.position) > 4) {
          const top = Math.ceil(crystal.position.y + 3)
          let perch = this.journal.data.waypoints.endPerch
          if (!perch || perch.crystal !== crystal.id) {
            const dx = origin.x - crystal.position.x; const dz = origin.z - crystal.position.z
            const len = Math.hypot(dx, dz) || 1
            perch = { x: Math.floor(crystal.position.x + dx / len * 12), z: Math.floor(crystal.position.z + dz / len * 12), y: top, crystal: crystal.id, dimension: 'the_end' }
            this.journal.data.waypoints.endPerch = perch; this.journal.save()
          }
          if (origin.y < top) {
            // A compact rising staircase; each tread has verified support before moving.
            const offsets = [new Vec3(1, 0, 0), new Vec3(0, 0, 1), new Vec3(-1, 0, 0), new Vec3(0, 0, -1)]
            return this.bridgeStep(origin.plus(offsets[((origin.y % 4) + 4) % 4]), true)
          }
          return this.bridgeStep(new Vec3(bars.position.x, origin.y, bars.position.z))
        }
        await this.dig(bars); return '已拆除一块水晶保护铁栏'
      }
      if (crystal.position.distanceTo(b.entity.position) < 10) {
        const away = origin.minus(crystal.position)
        const scale = 16 / (Math.hypot(away.x, away.z) || 1)
        return this.bridgeStep(origin.offset(away.x * scale, 0, away.z * scale))
      }
      return this.fight(crystal, true)
    }
    const dragon = b.nearestEntity(e => e.name === 'ender_dragon')
    if (!dragon) throw new Error('未观察到末影龙；不能据此判断通关')
    return this.fight(dragon, dragon.position.distanceTo(b.entity.position) > 5)
  }
  async recover () {
    if (this.bot.food < 18) return this.eat()
    const threat = this.snapshot().threat
    if (threat) {
      const away = this.bot.entity.position.minus(threat.position)
      const length = Math.hypot(away.x, away.z) || 1
      await this.go(this.bot.entity.position.offset(away.x / length * 8, 0, away.z / length * 8), 1)
    }
    const before = this.bot.health
    for (let i = 0; i < 30 && this.bot.health <= 12; i++) await this.pause(1000)
    if (this.bot.health <= before) throw new Error('等待后血量没有恢复，需要人工检查安全环境')
    return `血量已恢复至 ${this.bot.health}/20`
  }
  async execute (task) {
    this.check()
    switch (task.action) {
      case 'ensure': return this.execute(this.resolve(task.target))
      case 'satisfied': return `背包已有 ${task.target}`
      case 'mine': return this.mine(task.names, task.target)
      case 'make': return this.make(task.target, task.recipe)
      case 'smelt': return this.smelt(task.target)
      case 'eat': return this.eat()
      case 'food': return this.food()
      case 'equip': return this.equip()
      case 'hunt': return this.hunt(task.target)
      case 'explore': return this.explore(task.target)
      case 'stairs': return this.stairs(task.y)
      case 'water': return this.water()
      case 'build_portal': return this.buildPortal()
      case 'enter_nether': return this.enter('nether_portal')
      case 'enter_end': return this.enter('end_portal')
      case 'open_end': return this.openEnd()
      case 'return_portal': return this.returnPortal()
      case 'stronghold': return this.stronghold()
      case 'dragon': return this.dragon()
      case 'recover': return this.recover()
      case 'recover_items': {
        const death = this.journal.data.deaths[this.journal.data.deaths.length - 1]
        if (!death || death.dimension !== this.bot.game.dimension) throw new Error('没有可评估的同维度死亡位置')
        death.attempted = true; this.journal.save()
        const before = this.items().reduce((n, i) => n + i.count, 0)
        await this.go(death, 1); await this.pause(1500)
        const gained = this.items().reduce((n, i) => n + i.count, 0) - before
        death.recovered = gained > 0; this.journal.save()
        return gained > 0 ? `死亡位置附近实际拾取 ${gained} 件物品；不保证全部装备已找回` : '已到死亡位置，未确认拾取到掉落物，接下来重新准备装备'
      }
      case 'defend': {
        if (task.target === 'creeper') return this.recover()
        return this.hunt(task.target)
      }
      case 'mark_portal': {
        const portal = this.block(['nether_portal'], 24)
        if (!portal) throw new Error('没有看到返程传送门')
        this.journal.data.waypoints.netherReturn = { ...portal.position, dimension: this.bot.game.dimension }
        this.journal.save(); return '已记录实际返程门坐标'
      }
      case 'home':
        if (!this.safe(this.bot.entity.position)) throw new Error('当前位置不适合记录据点')
        this.journal.data.waypoints.home = point(this.snapshot()); this.journal.save()
        return '已记录据点坐标；尚未设置重生床或建造房屋'
      case 'home_return': {
        const home = this.journal.data.waypoints.home
        if (!home || home.dimension !== this.bot.game.dimension) throw new Error('没有同维度据点坐标')
        await this.go(home, 2); return '已返回记录的据点附近'
      }
      default: throw new Error(`未实现的技能：${task.action}`)
    }
  }
}
module.exports = { Skills, HAZARDS, HOSTILE }
