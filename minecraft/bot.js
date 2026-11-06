'use strict'
// NDJSON over parent-owned stdin/stdout. No network control endpoint.
const mineflayer = require('mineflayer')
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder')
const readline = require('node:readline')
const { setTimeout: delay } = require('node:timers/promises')
const fs = require('node:fs')
const path = require('node:path')
const { Journal, choose, PHASES } = require('./campaign')
const { Skills } = require('./skills')
const send = data => process.stdout.write(JSON.stringify(data) + '\n')
// Keep dependency logging out of the protocol stream.
console.log = (...args) => console.error(...args)
let bot, connected = false, generation = 0, busy = false, automatic = false
let activity = '未连接', lastError = '', timer
let journal, skills
let deathRecovery, resumeCampaignEpoch = null, activeAction = ''
let networkConnected = false, dead = false, respawning = false
const inventory = () => bot?.inventory?.items().map(i => ({ name: i.name, count: i.count })) || []
const count = predicate => inventory().filter(i => predicate(i.name)).reduce((n, i) => n + i.count, 0)
function status () {
  send({ type: 'status', connected, networkConnected, connecting: !!bot && !networkConnected,
    dead, respawning, busy, automatic, activity, error: lastError,
    health: networkConnected ? bot?.health : null, food: networkConnected ? bot?.food : null,
    position: connected ? bot.entity?.position : null, inventory: inventory(),
    players: connected ? Object.keys(bot.players).filter(n => n !== bot.username) : [],
    viewerUrl: connected && bot?.viewer ? 'http://127.0.0.1:3007/' : null,
    campaign: journal ? { ...journal.data, phaseName: PHASES[journal.data.phase] } : null })
}
function stop (reason = '已暂停') {
  clearTimeout(deathRecovery); deathRecovery = null; resumeCampaignEpoch = null
  generation++; automatic = false
  if (bot) {
    bot.pathfinder?.setGoal(null)
    bot.stopDigging()
    bot.clearControlStates()
    bot.deactivateItem()
    if (bot.currentWindow) bot.closeWindow(bot.currentWindow).catch(() => {})
  }
  activity = reason; status()
}
function check (ticket) {
  if (!connected || ticket !== generation) throw new Error('操作已取消')
}
async function go (goal, ticket) {
  check(ticket)
  await bot.pathfinder.goto(goal)
  check(ticket)
}
async function wood (ticket) {
  check(ticket)
  const candidates = bot.findBlocks({ matching: b => b.name.endsWith('_log'), maxDistance: 24, count: 48 })
  if (!candidates.length) throw new Error('附近 24 格内没有找到可见原木，请把她带到树林再试')
  let block
  for (const position of candidates) {
    check(ticket)
    const candidate = bot.blockAt(position)
    if (!candidate || position.y <= bot.entity.position.y - 1 &&
      Math.abs(position.x + 0.5 - bot.entity.position.x) < 0.8 &&
      Math.abs(position.z + 0.5 - bot.entity.position.z) < 0.8) continue
    const goal = new goals.GoalNear(position.x, position.y, position.z, 2)
    const route = bot.pathfinder.getPathTo(bot.pathfinder.movements, goal, 150)
    if (route.status === 'success') { block = candidate; break }
    await delay(0)
  }
  if (!block) throw new Error('附近原木暂时没有找到安全可达路线，请带她靠近树干再试')
  const before = count(n => n.endsWith('_log'))
  await go(new goals.GoalNear(block.position.x, block.position.y, block.position.z, 2), ticket)
  const target = bot.blockAt(block.position)
  if (!target?.name.endsWith('_log') || !bot.canDigBlock(target)) throw new Error('原木不可触及')
  await bot.dig(target)
  check(ticket)
  await go(new goals.GoalNear(block.position.x, block.position.y, block.position.z, 1), ticket)
  // Inventory packets may arrive after the block disappears.
  for (let i = 0; i < 15 && count(n => n.endsWith('_log')) <= before; i++) {
    await delay(100); check(ticket)
  }
  const gained = count(n => n.endsWith('_log')) - before
  if (gained <= 0) throw new Error('原木已挖掉，但背包尚未收到掉落物，不计为采集成功')
  return `背包实际增加了 ${gained} 个原木`
}
async function craft (ticket, name) {
  check(ticket)
  const item = bot.registry.itemsByName[name]
  const recipe = item && bot.recipesFor(item.id, null, 1, null)[0]
  if (!recipe) throw new Error(`背包材料不足，不能合成 ${name}`)
  const before = count(n => n === name)
  await bot.craft(recipe, 1, null)
  check(ticket)
  const gained = count(n => n === name) - before
  if (gained <= 0) throw new Error('合成后未检测到背包增加')
  return `背包实际增加了 ${gained} 个 ${name}`
}
async function perform (action, target, ticket) {
  check(ticket)
  if (skills && ['eat', 'equip', 'explore', 'home_return', 'water', 'recover'].includes(action)) {
    skills.check = () => check(ticket)
    return skills.execute({ action })
  }
  if (action === 'observe') {
    const p = bot.entity.position
    return `当前位置 ${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)}；血量 ${bot.health}/20，饥饿值 ${bot.food}/20；背包：${inventory().map(i => `${i.name}×${i.count}`).join('、') || '空'}`
  }
  if (action === 'wood') return wood(ticket)
  if (action === 'craft') return craft(ticket, 'crafting_table')
  if (action === 'planks') {
    const log = inventory().find(i => i.name.endsWith('_log'))
    if (!log) throw new Error('背包没有原木')
    return craft(ticket, log.name.replace(/_log$/, '_planks'))
  }
  if (action === 'follow') {
    const entity = bot.players[target]?.entity
    if (!entity) throw new Error('目标玩家不在视野内')
    await go(new goals.GoalFollow(entity, 3), ticket)
    return `已靠近玩家 ${target}；本次跟随已结束`
  }
  throw new Error('未知操作')
}
async function campaign (ticket) {
  if (!skills || !journal) throw new Error('通关模块尚未就绪')
  skills.check = () => check(ticket)
  const started = Date.now()
  for (let step = 0; step < 400 && Date.now() - started < 30 * 60 * 1000; step++) {
    check(ticket)
    const snapshot = skills.snapshot()
    const task = choose(snapshot, journal)
    if (task.action === 'done' || task.action === 'wait') {
      activity = task.reason; journal.save(); status(); return
    }
    let resolved = task
    if (task.action === 'ensure') resolved = skills.resolve(task.target, task.amount || 1)
    const key = `${resolved.action}:${resolved.target || ''}`
    const previousFailures = journal.data.attempts[key] || 0
    if (previousFailures >= 3) throw new Error(`${key} 连续失败 3 次，已保存卡点，请处理环境后点击重试通关`)
    activity = `${PHASES[journal.data.phase]} · ${resolved.action} ${resolved.target || ''}`
    journal.data.lastSnapshot = { ...snapshot, threat: snapshot.threat ? { name: snapshot.threat.name } : null }
    journal.save(); status()
    let timedOut = false
    const stepTimeout = setTimeout(() => { timedOut = true; stop('单步操作超时，已保存进度并暂停') }, 90000)
    try {
      const result = await skills.execute(resolved)
      check(ticket)
      journal.receipt({ action: resolved.action, target: resolved.target, key }, true, result, { position: pointForSave(), items: inventory() })
      send({ type: 'event', text: result })
    } catch (error) {
      if (ticket !== generation && !timedOut) {
        journal.data.blocked = '本轮已暂停，未将取消计为技能失败'; journal.save(); throw error
      }
      journal.receipt({ action: resolved.action, target: resolved.target, key }, false, error.message, { position: pointForSave(), items: inventory() })
      if (timedOut || ticket !== generation) throw error
      send({ type: 'event', text: `通关步骤未完成：${error.message}；正在评估替代路线` })
      clearTimeout(stepTimeout)
      // Retry using a bounded alternative, instead of repeating the same failing action.
      const recoveryTimeout = setTimeout(() => stop('替代路线超时，已暂停'), 45000)
      try {
        let result
        if (bot.health <= 8) result = await skills.recover()
        else if (resolved.action === 'mine' && ['diamond', 'raw_iron'].includes(resolved.target)) {
          result = await skills.stairs(resolved.target === 'diamond' ? -54 : 16)
        } else result = await skills.explore(resolved.target || task.action)
        check(ticket)
        journal.receipt({ action: 'recovery', key: `recovery:${key}` }, true, result, { position: pointForSave(), items: inventory() })
        send({ type: 'event', text: result })
      } finally { clearTimeout(recoveryTimeout) }
    } finally { clearTimeout(stepTimeout) }
    await delay(500)
  }
  activity = '本轮达到 30 分钟或 400 步上限，进度已保存，等待继续'
  journal.save(); status()
}
function pointForSave () { return bot?.entity ? { ...bot.entity.position, dimension: bot.game.dimension } : null }
async function run (action, target = '') {
  if (busy) throw new Error('上一项操作尚未退出，请稍后再试')
  const ticket = generation
  busy = true; lastError = ''; activity = action; activeAction = action; automatic = ['auto', 'campaign', 'campaign_retry'].includes(action); status()
  // Every task is bounded, including automatic starter progression.
  const timeout = setTimeout(() => stop('操作超时，已暂停'), action.startsWith('campaign') ? 31 * 60 * 1000 : 60000)
  try {
    if (action === 'campaign' || action === 'campaign_retry') {
      if (action === 'campaign_retry' && journal) { journal.data.attempts = {}; journal.data.blocked = ''; journal.save() }
      await campaign(ticket)
    } else if (action === 'auto') {
      // Initial autonomous goal: collect enough wood and make a crafting table.
      for (let step = 0; step < 12; step++) {
        check(ticket)
        if (count(n => n === 'crafting_table') > 0) {
          send({ type: 'event', text: '背包已有工作台，开局目标完成，等待下一项任务' }); break
        }
        const next = count(n => n.endsWith('_planks')) >= 4 ? 'craft'
          : count(n => n.endsWith('_log')) > 0 ? 'planks' : 'wood'
        activity = `自主开局：${next}`; status()
        const result = await perform(next, '', ticket)
        check(ticket)
        send({ type: 'event', text: result })
        await delay(500)
      }
    } else {
      const result = await perform(action, target, ticket)
      check(ticket); send({ type: 'event', text: result })
    }
    if (ticket === generation && !action.startsWith('campaign')) activity = '本次任务结束'
  } catch (error) {
    if (ticket === generation) {
      lastError = error.message; activity = '操作未完成'
      send({ type: 'event', text: `操作未完成：${lastError}` })
      stop('操作未完成，已暂停')
    }
  } finally {
    clearTimeout(timeout); busy = false; automatic = false; status()
  }
}
function connect () {
  if (bot) throw new Error('已有连接，请先断开')
  connected = networkConnected = dead = respawning = false
  skills = journal = undefined; lastError = ''; activity = '正在连接'
  bot = mineflayer.createBot({ host: '127.0.0.1', port: 25565,
    username: 'Avatar', auth: 'offline', version: '1.20.1', respawn: false })
  const session = bot
  let initialized = false
  bot.loadPlugin(pathfinder)
  bot.on('login', () => {
    if (bot !== session) return
    networkConnected = true; activity = '已连接，等待角色状态'; status()
  })
  function enteredWorld () {
    if (bot !== session || !(session.health > 0)) return
    const restored = dead || respawning
    networkConnected = true; dead = respawning = false; lastError = ''
    connected = true
    if (!initialized) {
    initialized = true
    try {
      // Pinned viewer forwards this object to http.Server.listen, binding only loopback.
      require('prismarine-viewer').mineflayer(bot, {
        firstPerson: true, viewDistance: 4, port: { host: '127.0.0.1', port: 3007 }
      })
    } catch (error) {
      send({ type: 'event', text: `第一视角未启动：${error.message}` })
    }
    const movements = new Movements(bot)
    movements.canDig = false // Only the explicit wood skill may dig.
    movements.allow1by1towers = false; movements.allowParkour = false
    movements.allowFreeMotion = false; movements.maxDropDown = 1
    movements.scafoldingBlocks = []
    bot.pathfinder.setMovements(movements)
    bot.pathfinder.thinkTimeout = 5000
    const folder = path.resolve(__dirname, '../data/minecraft')
    const worldPath = path.join(folder, 'server/avatar-world')
    const world = fs.existsSync(worldPath) ? `avatar-world:${fs.statSync(worldPath).birthtimeMs}` : 'local-world-unverified'
    try {
      journal = new Journal(path.join(folder, 'campaign.json'), world)
      skills = new Skills(bot, journal, () => { if (!connected) throw new Error('连接已断开') }, send)
    } catch (error) { lastError = `通关存档无法读取：${error.message}`; journal = undefined; skills = undefined }
    activity = '已进入世界，等待任务'; status()
    send({ type: 'event', text: '助手已实际进入 Minecraft 世界，当前尚未开始自主任务' })
    }
    if (restored) {
      activity = '角色已重生，等待任务'
      if (journal && journal.data.blocked?.startsWith('角色死亡')) {
        journal.data.blocked = ''; journal.save()
      }
      send({ type: 'event', text: '服务器已确认角色恢复存活，可继续操作' })
    }
    status()
    resumeAfterSpawn()
  }
  bot.on('entityDead', entity => {
    if (skills && journal && journal.dragonDead(entity, skills.snapshot(), skills.hitDragon)) {
      send({ type: 'event', text: '已收到末地末影龙死亡事件，且本角色参与了战斗；通关目标已完成并保存' })
      stop('击败末影龙的目标已完成')
    }
  })
  bot.on('death', () => {
    if (bot !== session || dead) return
    const continueCampaign = activeAction.startsWith('campaign') && automatic
    if (skills) journal.death(skills.snapshot())
    connected = false; dead = true; respawning = false
    stop('角色已死亡，连接仍在，可点击重生')
    send({ type: 'event', kind: 'death', text: skills
      ? 'Minecraft 角色实际死亡，本轮操作已停止，死亡位置已记录'
      : '登录后服务器确认角色处于死亡状态，连接正常，可点击重生' })
    if (continueCampaign && journal && journal.data.deaths.filter(d => Date.now() - d.time < 600000).length < 3) {
      const epoch = generation
      deathRecovery = setTimeout(() => {
        if (epoch !== generation || !bot || bot.health > 0) return
        resumeCampaignEpoch = epoch
        requestRespawn()
      }, 3000)
    }
  })
  bot.on('spawn', () => {
    // Mineflayer may emit its initial spawn before assigning the health packet.
    setImmediate(enteredWorld)
  })
  function resumeAfterSpawn () {
    if (resumeCampaignEpoch === generation) {
      const epoch = generation; resumeCampaignEpoch = null
      ;(async () => {
        for (let i = 0; i < 100; i++) {
          if (epoch !== generation || !connected) return
          if (!busy) { await run('campaign'); return }
          await delay(500)
        }
        activity = '上轮动作仍在退出，请手动继续'; status()
      })().catch(e => { lastError = e.message; status() })
    }
  }
  bot.on('health', () => {
    if (bot !== session) return
    // Health is authoritative even when reconnecting to a dead player save.
    if (session.health > 0 && (!connected || dead || !initialized)) setImmediate(enteredWorld)
    if (connected && busy && !automatic && bot.health > 0 && bot.health <= 4) stop('血量过低，已暂停')
  })
  bot.on('kicked', reason => { lastError = JSON.stringify(reason).slice(0, 300); status() })
  bot.on('error', error => { lastError = error.message; status() })
  bot.on('end', () => {
    session.viewer?.close()
    if (bot !== session) return
    connected = networkConnected = dead = respawning = false
    stop('连接已断开'); bot = undefined; skills = undefined; status()
  })
}
function requestRespawn () {
  if (!bot || !networkConnected) throw new Error('请先连接 Minecraft，再重生')
  if (bot.health > 0) throw new Error('角色仍然存活，无需重生')
  if (!dead || bot.isAlive) throw new Error('尚未收到服务器死亡确认，请稍后再试')
  if (respawning) return
  respawning = true; lastError = ''
  bot.respawn(); activity = '已请求重生，等待服务器确认'; status()
  const session = bot
  setTimeout(() => {
    if (bot === session && respawning) {
      respawning = false; lastError = '服务器尚未确认重生，可以重试'; status()
    }
  }, 10000).unref()
}
readline.createInterface({ input: process.stdin }).on('line', line => {
  try {
    const msg = JSON.parse(line)
    if (msg.action === 'connect') connect()
    else if (msg.action === 'respawn') {
      requestRespawn()
    }
    else if (msg.action === 'stop') stop()
    else if (msg.action === 'disconnect') { stop('正在断开'); bot?.quit() }
    else run(msg.action, String(msg.target || '')).catch(e => { lastError = e.message; status() })
  } catch (error) { lastError = error.message; status() }
})
process.stdin.on('end', () => { stop(); bot?.quit(); clearInterval(timer); setTimeout(() => process.exit(0), 500) })
timer = setInterval(status, 2000)
status()
