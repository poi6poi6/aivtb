// body：mineflayer 游戏体。
// 连接 Minecraft 服务器，把游戏事件经 WebSocket 推给 brain；
// 同时接收 brain 的指令（目前只有 chat，让 bot 在游戏聊天栏说话）。
//
// 环境变量：MC_HOST(默认127.0.0.1) MC_PORT(默认25565) MC_USER(默认Avatar) WS_PORT(默认8787)
const mineflayer = require('mineflayer')
const { WebSocketServer } = require('ws')

const MC_HOST = process.env.MC_HOST || '127.0.0.1'
const MC_PORT = Number(process.env.MC_PORT || 25565)
const MC_USER = process.env.MC_USER || 'Avatar'
const WS_PORT = Number(process.env.WS_PORT || 8787)

const bot = mineflayer.createBot({ host: MC_HOST, port: MC_PORT, username: MC_USER })

let brain = null
const wss = new WebSocketServer({ port: WS_PORT })
wss.on('connection', (ws) => {
  console.log('[ws] brain 已连接')
  brain = ws
  ws.on('close', () => {
    if (brain === ws) brain = null
    console.log('[ws] brain 断开')
  })
  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw)
      if (msg.type === 'chat' && msg.text) bot.chat(String(msg.text).slice(0, 256))
    } catch {
      // 忽略无法解析的指令
    }
  })
})

function emit(kind, text) {
  console.log(`[event] ${kind}: ${text}`)
  if (brain && brain.readyState === 1) {
    brain.send(JSON.stringify({ kind, text }))
  }
}

bot.once('spawn', () => emit('find', `进入了服务器 ${MC_HOST}:${MC_PORT}，出生点安家`))
bot.on('death', () => emit('death', '死了，正在重生'))
bot.on('health', () => {
  if (bot.health > 0 && bot.health < 6) emit('ambient', `残血了，只剩 ${bot.health} 点生命`)
})
bot.on('chat', (username, message) => {
  if (username !== bot.username) emit('chat', `${username}: ${message}`)
})
bot.on('rain', () => emit('ambient', '下雨了'))
bot.on('kicked', (reason) => console.log('[kicked]', reason))
bot.on('end', () => {
  console.log('[end] 与服务器断开，5 秒后重连')
  setTimeout(() => process.exit(1), 5000) // 交由 supervisor / 手动重启，保持简单
})
bot.on('error', (err) => console.log('[error]', err.message))
