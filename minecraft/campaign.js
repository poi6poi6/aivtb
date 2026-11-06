'use strict'
const fs = require('node:fs')
const path = require('node:path')

const PHASES = ['生存与据点', '铁器与补给', '下界准备', '下界收集', '寻找要塞', '末地挑战', '已击败末影龙']
const FOOD = ['cooked_beef', 'cooked_porkchop', 'cooked_mutton', 'cooked_chicken', 'bread', 'baked_potato', 'carrot', 'apple', 'cooked_cod', 'cooked_salmon']
const RAW = { raw_iron: 'iron_ingot', beef: 'cooked_beef', porkchop: 'cooked_porkchop', mutton: 'cooked_mutton', chicken: 'cooked_chicken', potato: 'baked_potato' }
const MINES = {
  cobblestone: { blocks: ['stone', 'cobblestone'], tool: 'wooden_pickaxe' },
  coal: { blocks: ['coal_ore', 'deepslate_coal_ore'], tool: 'wooden_pickaxe' },
  raw_iron: { blocks: ['iron_ore', 'deepslate_iron_ore'], tool: 'stone_pickaxe' },
  diamond: { blocks: ['diamond_ore', 'deepslate_diamond_ore'], tool: 'iron_pickaxe' },
  obsidian: { blocks: ['obsidian'], tool: 'diamond_pickaxe' },
  flint: { blocks: ['gravel'], tool: null }
}
const total = (s, name) => (s.items || []).filter(i => i.name === name).reduce((n, i) => n + i.count, 0)
const sum = (s, names) => names.reduce((n, name) => n + total(s, name), 0)
const point = (s) => ({ ...s.position, dimension: s.dimension })

class Journal {
  constructor (file, world) {
    this.file = file
    this.data = { version: 1, world, goal: '生存模式击败末影龙', phase: 0, completed: false,
      waypoints: {}, deaths: [], attempts: {}, recent: [], visited: [], eyeRays: [], blocked: '', lastSnapshot: null }
    if (fs.existsSync(file)) {
      const saved = JSON.parse(fs.readFileSync(file, 'utf8'))
      if (saved.version !== 1) throw new Error('通关存档版本不支持，请保留备份后迁移')
      if (saved.world === world) this.data = saved
      else fs.copyFileSync(file, `${file}.${Date.now()}.previous-world.json`)
    }
  }
  save () {
    fs.mkdirSync(path.dirname(this.file), { recursive: true })
    const temp = `${this.file}.tmp`
    fs.writeFileSync(temp, JSON.stringify(this.data, null, 2))
    fs.renameSync(temp, this.file)
  }
  receipt (task, ok, text, snapshot) {
    const key = task.key || `${task.action}:${task.target || ''}`
    this.data.attempts[key] = ok ? 0 : (this.data.attempts[key] || 0) + 1
    this.data.blocked = ok ? '' : text
    this.data.lastSnapshot = snapshot
    this.data.recent = [...this.data.recent, { time: new Date().toISOString(), task, ok, text }].slice(-50)
    this.save()
  }
  death (snapshot) {
    this.data.deaths = [...this.data.deaths, { ...point(snapshot), time: Date.now(), recovered: false }].slice(-10)
    this.data.blocked = '角色死亡，需重生并重新评估装备和路线'
    this.save()
  }
  // Entity disappearance, chat messages and a planner's claim never count as victory.
  dragonDead (entity, snapshot, participated) {
    if (entity.name !== 'ender_dragon' || snapshot.dimension !== 'the_end' || !participated || snapshot.gameMode !== 'survival') return false
    this.data.completed = true; this.data.phase = 6
    this.data.victory = { time: Date.now(), entityId: entity.id, evidence: 'entityDead', position: point(snapshot) }
    this.save(); return true
  }
}

function choose (s, journal) {
  const d = journal.data
  if (d.completed) return { action: 'done', reason: '已保存服务器末影龙死亡回执' }
  if (!s.connected) return { action: 'wait', reason: '尚未进入世界' }
  if (s.gameMode !== 'survival') return { action: 'wait', reason: '通关计划要求生存模式' }
  if (s.health <= 0) return { action: 'wait', reason: '等待重生' }
  if (s.food < 18 && sum(s, FOOD) > 0) return { action: 'eat' }
  if (s.health <= 8) return { action: 'recover', reason: '优先恢复血量' }
  if (s.food <= 6 && sum(s, FOOD) === 0) return { action: 'food' }
  if (s.threat) return { action: 'defend', target: s.threat.name }
  const death = d.deaths[d.deaths.length - 1]
  if (death && !death.attempted && death.dimension === s.dimension && Date.now() - death.time < 240000 &&
      Math.hypot(s.position.x - death.x, s.position.y - death.y, s.position.z - death.z) < 48 && s.health >= 14) return { action: 'recover_items' }
  if (!d.waypoints.home && s.dimension === 'overworld') return { action: 'home' }
  if (s.dimension === 'the_end') { d.phase = 5; return { action: 'dragon' } }
  if (s.dimension === 'the_nether') {
    d.phase = 3
    if (!d.waypoints.netherReturn) return { action: 'mark_portal' }
    if (sum(s, FOOD) < 3) return { action: 'return_portal', reason: '下界补给不足，返回主世界' }
    if (total(s, 'blaze_rod') < 7) return { action: 'hunt', target: 'blaze' }
    if (total(s, 'ender_pearl') + total(s, 'ender_eye') < 14) return { action: 'hunt', target: 'enderman' }
    return { action: 'return_portal' }
  }
  // Portal activation consumes the eyes. Observed world progress takes
  // precedence over the supply thresholds used to prepare the expedition.
  if (s.endPortal) { d.phase = 4; return { action: 'enter_end' } }
  d.phase = 0
  if (sum(s, FOOD) < 8) return { action: 'food' }
  d.phase = 1
  for (const name of ['stone_pickaxe', 'iron_pickaxe', 'iron_sword', 'shield', 'iron_helmet', 'iron_chestplate', 'iron_leggings', 'iron_boots']) {
    if (!total(s, name)) return { action: 'ensure', target: name }
  }
  if (!s.equipped) return { action: 'equip' }
  if (s.difficulty === 'peaceful') return { action: 'wait', reason: '和平难度无法正常收集通关所需的怪物掉落物，请改为普通难度' }
  if (!total(s, 'bow')) return { action: 'ensure', target: 'bow' }
  if (total(s, 'arrow') < 48) return { action: 'ensure', target: 'arrow', amount: 48 }
  if (total(s, 'cobblestone') < 128) return { action: 'ensure', target: 'cobblestone', amount: 128 }
  const locating = d.strongholdStarted || d.eyeRays?.length > 0 || d.waypoints.strongholdEstimate
  if (s.endFrames || locating || total(s, 'ender_eye') >= 12 || total(s, 'blaze_rod') + total(s, 'blaze_powder') >= 7) {
    d.phase = 4
    const needed = s.endFrames
      ? (Number.isInteger(s.endEyesNeeded) && s.endEyesNeeded >= 0 && s.endEyesNeeded <= 12 ? s.endEyesNeeded : 12)
      : locating ? (d.waypoints.strongholdEstimate ? 0 : 1) : 12
    if (total(s, 'ender_eye') < needed) {
      // Blaze materials require the Nether; after a death or exhausted supply,
      // do not ask the Overworld recipe resolver to hunt an absent blaze.
      if (total(s, 'blaze_powder') + total(s, 'blaze_rod') * 2 >= needed - total(s, 'ender_eye')) {
        return { action: 'ensure', target: 'ender_eye', amount: needed }
      }
    } else {
      if (!total(s, 'water_bucket')) return { action: 'water' }
      if (s.endFrames) return { action: 'open_end' }
      d.strongholdStarted = true
      return { action: 'stronghold' }
    }
  }
  d.phase = 2
  for (const name of ['diamond_pickaxe', 'flint_and_steel']) if (!total(s, name)) return { action: 'ensure', target: name }
  if (!total(s, 'water_bucket')) return { action: 'water' }
  if (s.netherPortal) return { action: 'enter_nether' }
  if (total(s, 'obsidian') < 14) return { action: 'ensure', target: 'obsidian', amount: 14 }
  return { action: 'build_portal' }
}

// Two observed eye trajectories; reject nearly parallel rays and intersections behind either throw.
function intersectRays (a, b) {
  const cross = (u, v) => u.x * v.z - u.z * v.x
  const den = cross(a.direction, b.direction)
  if (Math.abs(den) < 0.025) return null
  const offset = { x: b.origin.x - a.origin.x, z: b.origin.z - a.origin.z }
  const t = cross(offset, b.direction) / den
  const u = cross(offset, a.direction) / den
  if (t < 0 || u < 0 || t > 10000 || u > 10000) return null
  return { x: a.origin.x + t * a.direction.x, z: a.origin.z + t * a.direction.z }
}
module.exports = { Journal, choose, PHASES, FOOD, RAW, MINES, total, sum, point, intersectRays }
