'use strict';
const params = new URLSearchParams(location.search), job = params.get('job');
const $ = selector => document.querySelector(selector);
const clean = params.get('clean') === '1';
let player, snapshot, loadedVersion = '', anchor = 0, frame = 0, ticket = 0, applied = 0;
let busy = false, connected = false, stopped = false, timer = null, preview = null;
if (clean) document.documentElement.classList.add('clean');

function status() {
  if (!snapshot?.drawing || !player) return;
  const s = player.state(), sketch = snapshot.drawing.stage === 'sketch';
  const pending = snapshot.latest.id !== snapshot.drawing.job_id;
  $('#progress').value = s.progress;
  $('#status').textContent = !connected ? '同步连接已断开，画布已暂停；正在重连…'
    : pending ? `后续版本 · ${snapshot.latest.detail || snapshot.latest.state}`
    : s.done ? (sketch ? '草稿已画完，可修改构图，再继续上色。' : '画完了。')
    : `${sketch ? '草稿' : '成稿'} · ${s.phase} · 第 ${s.index + 1} / ${s.total} 笔 · ${snapshot.playback.playing ? '正在落笔' : '已暂停'}`;
  $('#play').textContent = snapshot.playback.playing ? '暂停' : s.done ? '再画一次' : '继续画';
  for (const id of ['#play', '#restart', '#speed']) $(id).disabled = busy || !connected;
  $('#save').disabled = false;
  $('#revision-panel').hidden = clean || pending;
  $('#finish').hidden = !sketch;
  $('#revise').disabled = busy || !connected;
  $('#finish').disabled = busy || !connected;
}

function tick(now) {
  frame = 0;
  if (!player || !snapshot?.playback || !connected) return;
  const p = snapshot.playback;
  player.seek(p.position + (p.playing ? Math.max(0, now - anchor) * p.speed : 0));
  status();
  if (p.playing && !player.state().done) frame = requestAnimationFrame(tick);
}

async function apply(state, sequence) {
  if (sequence < applied) return;
  if (snapshot?.playback?.session === state.playback?.session &&
      state.playback && state.playback.revision < snapshot.playback.revision) return;
  const version = state.drawing ? state.drawing.job_id + ':' + state.drawing.version : '';
  const receivedAt = performance.now();
  let nextPlayer = player;
  if (state.drawing && version !== loadedVersion) {
    const r = await fetch('/drawing-data/' + encodeURIComponent(state.drawing.job_id), {cache:'no-store'});
    if (!r.ok) throw Error('新版本笔画尚不可读取，保留当前画面。');
    const data = await r.json();
    if (sequence < applied) return;
    nextPlayer = new DrawingPlayer($('#paper'), data);
    $('#title').textContent = '✳ ' + data.title;
  }
  if (sequence < applied) return;
  if (snapshot?.latest.id !== state.latest.id || loadedVersion !== version) {
    preview = null; $('#confirm-change').hidden = true;
  }
  if (player && snapshot?.playback?.session === state.playback?.session &&
      snapshot.playback.revision === state.playback.revision && state.playback.playing)
    state.playback.position = Math.max(player.time, state.playback.position);
  applied = sequence; loadedVersion = version; snapshot = state;
  player = nextPlayer; anchor = receivedAt; connected = true;
  if (!player || !state.playback) { $('#status').textContent = state.latest.detail || '正在准备草稿…'; return; }
  $('#speed').value = String(state.playback.speed);
  cancelAnimationFrame(frame); frame = 0; tick(performance.now());
}

async function action(data) {
  const r = await fetch('/tasks-action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const result = await r.json();
  if (!r.ok) throw Error(result.error || '操作未确认，请同步状态后重试');
  return result;
}

async function control(operation, speed) {
  if (!snapshot?.playback || busy || !connected) return;
  busy = true; status();
  const sequence = ++ticket;
  try {
    const result = await action({action:'drawing_playback', id:job,
      ...snapshot.playback, operation, speed, version:snapshot.drawing.version});
    await apply(result, sequence);
  } catch (error) { $('#change-status').textContent = error.message; }
  finally { busy = false; status(); }
}

$('#play').onclick = () => control(snapshot.playback.playing ? 'pause' : 'play');
$('#restart').onclick = () => control('restart');
$('#speed').onchange = event => control('speed', Number(event.target.value));
$('#save').onclick = () => $('#paper').toBlob(blob => {
  if (!blob) return;
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'avatar-drawing.png'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}, 'image/png');

async function prepareChange(finish) {
  if (busy || !snapshot?.drawing) return;
  const instruction = $('#instruction').value.trim();
  if (!finish && !instruction) { $('#change-status').textContent = '先写下想修改的地方。'; return; }
  busy = true; status();
  try {
    const request = {action:finish ? 'finish_drawing' : 'amend', id:snapshot.drawing.job_id, instruction};
    const result = await action(request);
    preview = {request, token:result.token};
    $('#change-status').textContent = result.detail;
    $('#confirm-change').hidden = false;
  } catch (error) { $('#change-status').textContent = error.message; }
  finally { busy = false; status(); }
}
$('#revise').onclick = () => prepareChange(false);
$('#finish').onclick = () => prepareChange(true);
$('#instruction').oninput = () => { preview = null; $('#confirm-change').hidden = true; };
$('#confirm-change').onclick = async () => {
  if (!preview || busy) return;
  const confirmed = preview; preview = null;
  busy = true; $('#confirm-change').hidden = true; status();
  try {
    await action({...confirmed.request, token:confirmed.token});
    $('#change-status').textContent = '已提交后续版本，当前画面保留；新笔画准备好后会同步。';
    $('#instruction').value = '';
  } catch (error) { $('#change-status').textContent = error.message; }
  finally { busy = false; status(); }
};

async function poll() {
  if (stopped) return;
  try {
    const sequence = ++ticket;
    const r = await fetch('/drawing-state/' + encodeURIComponent(job), {cache:'no-store'});
    if (!r.ok) throw Error('画布同步暂不可用');
    await apply(await r.json(), sequence);
  } catch (error) {
    connected = false; cancelAnimationFrame(frame); frame = 0;
    if (player) status(); else $('#status').textContent = error.message + '，正在重试…';
  } finally { if (!stopped) timer = setTimeout(poll, 500); }
}
document.addEventListener('visibilitychange', () => { if (player && connected) { cancelAnimationFrame(frame); tick(performance.now()); } });
window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); cancelAnimationFrame(frame); });
if (!job || !/^[a-f0-9]{12}$/.test(job)) {
  document.documentElement.classList.remove('clean');
  $('#status').textContent = '请从工作室打开一项逐笔绘画任务。';
} else {
  $('#clean').href = '/drawing?job=' + encodeURIComponent(job) + '&clean=1';
  poll();
}
