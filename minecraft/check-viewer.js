// Live protocol smoke check; does not move or command the bot.
const { io } = require('socket.io-client')
const socket = io('http://127.0.0.1:3007', { reconnection: false })
const received = { version: null, chunks: 0, firstPerson: false }
const timeout = setTimeout(() => finish(new Error('Viewer data timeout')), 15000)
function finish (error) {
  clearTimeout(timeout)
  socket.disconnect()
  console.log(JSON.stringify({ ...received, error: error?.message }))
  process.exit(error ? 1 : 0)
}
function check () {
  if (received.version && received.chunks && received.firstPerson) finish()
}
socket.on('version', v => { received.version = v; check() })
socket.on('loadChunk', c => { if (c.chunk) received.chunks++; check() })
socket.on('position', p => { received.firstPerson = Number.isFinite(p.pitch) && Number.isFinite(p.yaw) && Number.isFinite(p.pos?.x); check() })
socket.on('connect_error', finish)
