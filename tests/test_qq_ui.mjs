import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const {QQPage, safeBindingURL} = require('../brain/static/qq.js');
const now = 1792700000000;
const response = (data, status = 200) => ({ok: status >= 200 && status < 300, json: async () => data});
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const flush = () => new Promise(setImmediate);
const initial = () => ({available: true, phase: 'offline', connected: false, account_bound: false,
  owner_bound: false, owner_name: '', error: '', counts: {received: 0, replied: 0}});
class Element {
  constructor() { this.textContent = ''; this.dataset = {}; this.disabled = false; this.hidden = false; this.value = ''; this.attributes = {}; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this[name]; }
  focus() { this.focused = true; }
  set innerHTML(value) { throw new Error('Untrusted data must never become HTML'); }
}
function fixture({state = initial(), fetch} = {}) {
  const elements = new Map();
  const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const calls = [];
  const listeners = new Map();
  const page = new QQPage({documentRef: {getElementById: el}, now: () => now,
    windowRef: {addEventListener(type, fn) { listeners.set(type, fn); }},
    fetcher: async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({url, options, body});
      return fetch ? fetch(url, body, options) : response(state);
    }});
  return {page, state, el, calls, listeners};
}

test('loading and refreshing never initiate login or pair; offline controls are disabled', async () => {
  const f = fixture();
  await f.page.refresh();
  await f.page.refresh();
  assert.equal(f.calls.length, 2);
  assert(f.calls.every(call => call.options.method === 'GET' && call.options.cache === 'no-store'));
  assert.equal(f.el('bind').disabled, false);
  for (const id of ['connect', 'disconnect', 'pair', 'unpair', 'reset']) assert.equal(f.el(id).disabled, true);
});

test('only the official QQ binding URL is exposed, no SVG or untrusted markup is rendered', async () => {
  const valid = 'https://q.qq.com/qqbot/openclaw/connect.html?task_id=fixture&source=test&_wv=2';
  assert.equal(safeBindingURL(valid), valid);
  for (const url of ['javascript:alert(1)', 'https://q.qq.com.evil.test/qqbot/openclaw/connect.html',
    'http://q.qq.com/qqbot/openclaw/connect.html', 'https://evil.test/',
    'https://secret@q.qq.com/qqbot/openclaw/connect.html', 'https://q.qq.com:444/qqbot/openclaw/connect.html',
    'https://q.qq.com/other', valid + '#fragment', null]) assert.equal(safeBindingURL(url), null);
  const f = fixture({state: {...initial(), phase: 'binding', qr_url: valid, qr_svg: '<svg onload=alert(1)>', error: '<script>x</script>'}});
  await f.page.refresh();
  assert.equal(f.el('binding-panel').hidden, false);
  assert.equal(f.el('binding-link').href, valid);
  assert.equal(f.el('service-error').textContent, '<script>x</script>');
  f.state.qr_url = 'https://evil.test/';
  await f.page.refresh();
  assert.equal(f.el('binding-panel').hidden, true);
  assert.equal(f.el('binding-link').href, undefined);
});

test('pair validates the name and preserves unsaved input across polling', async () => {
  const f = fixture({state: {...initial(), account_bound: true, connected: true, phase: 'connected'}});
  await f.page.refresh();
  await f.page.action('pair');
  assert.equal(f.calls.filter(call => call.body).length, 0);
  assert.equal(f.el('owner-name').focused, true);
  f.el('owner-name').value = '  小林  ';
  f.el('owner-name').oninput();
  await f.page.refresh();
  assert.equal(f.el('owner-name').value, '  小林  ');
  await f.el('pair-form').onsubmit({preventDefault() {}});
  assert.deepEqual(f.calls.find(call => call.body).body, {action: 'pair', owner_name: '小林'});
  const post = f.calls.find(call => call.body);
  assert.equal(post.options.credentials, 'same-origin');
  assert.equal(post.options.headers['Content-Type'], 'application/json');
});

test('connected without an owner prominently requires pairing before chat is ready', async () => {
  const f = fixture({state: {...initial(), account_bound: true, connected: true, phase: 'connected'}});
  await f.page.refresh();
  assert.equal(f.el('phase').textContent, '已连接 · 待配对');
  assert.equal(f.el('phase').dataset.connected, 'false');
  assert.equal(f.el('phase').dataset.pairing, 'true');
  assert.equal(f.el('group-panel').hidden, true);
  assert.equal(f.el('reply-scope').hidden, true);
  assert.equal(f.el('pair-next').hidden, false);
  assert.match(f.el('status-detail').textContent, /普通私聊暂时不会回复/);
  assert.equal(f.el('counts').textContent, '已处理 0 · 回复 0');
  f.state.owner_bound = true;
  await f.page.refresh();
  assert.equal(f.el('phase').textContent, '已连接 · 可聊天');
  assert.equal(f.el('phase').dataset.connected, 'true');
  assert.equal(f.el('pair-next').hidden, true);
  f.page.online = false;
  f.page.render();
  assert.equal(f.el('phase').textContent, '状态待确认');
  assert.equal(f.el('pair-next').hidden, true);
});

test('connected group chat is available before private pairing, without bypassing private controls', async () => {
  const f = fixture({state: {...initial(), account_bound: true, connected: true, phase: 'connected',
    group_enabled: true, group_session_count: 3}});
  await f.page.refresh();
  assert.equal(f.el('phase').textContent, '已连接 · 群聊可用');
  assert.equal(f.el('phase').dataset.connected, 'true');
  assert.match(f.el('status-detail').textContent, /群聊已可 @ 她，私聊还需身份配对/);
  assert.equal(f.el('group-panel').hidden, false);
  assert.match(f.el('group-status').textContent, /机器人已连接/);
  assert.equal(f.el('group-count').textContent, '当前活跃 3 段群聊对话。');
  assert.equal(f.el('reply-scope').hidden, false);
  assert.equal(f.el('pair-next').hidden, false);
  assert.match(f.el('pair-next').textContent, /私聊还需/);
  assert.equal(f.el('pair').disabled, false);
  assert.equal(f.el('unpair').disabled, true);
  assert.equal(f.el('reset').disabled, true);
  assert(f.calls.every(call => call.options.method === 'GET'));

  f.state.owner_bound = true;
  await f.page.refresh();
  assert.equal(f.el('phase').textContent, '已连接 · 可聊天');
  assert.match(f.el('status-detail').textContent, /可以私聊，或在 QQ 群里 @ 她/);
  assert.equal(f.el('pair-next').hidden, true);
  assert.equal(f.el('reset').disabled, false);
});

test('group status requires connection and treats missing or invalid session counts as zero', async () => {
  const f = fixture({state: {...initial(), group_enabled: true, group_session_count: '<script>x</script>'}});
  await f.page.refresh();
  assert.equal(f.el('phase').textContent, '未连接');
  assert.equal(f.el('phase').dataset.connected, 'false');
  assert.match(f.el('group-status').textContent, /^连接后/);
  assert.equal(f.el('group-count').textContent, '当前活跃 0 段群聊对话。');
  f.state.connected = true;
  f.state.phase = 'connected';
  delete f.state.group_session_count;
  await f.page.refresh();
  f.page.online = false;
  f.page.render();
  assert.equal(f.el('phase').textContent, '状态待确认');
  assert.match(f.el('group-status').textContent, /^连接后/);
  assert.equal(f.el('group-count').textContent, '当前活跃 0 段群聊对话。');
});

test('connection completion distinguishes group availability from private pairing', async () => {
  for (const ownerBound of [false, true]) {
    const f = fixture({fetch: (_url, body) => response({...initial(), account_bound: true,
      group_enabled: true, owner_bound: ownerBound,
      ...(body ? {connected: true, phase: 'connected'} : {})})});
    await f.page.refresh();
    await f.page.action('connect');
    assert.match(f.el('notice').textContent, ownerBound ? /可以私聊或在群里 @ 她/ : /群聊可 @ 她；私聊请继续确认你的身份/);
  }
});

test('expired or completed pairing clears the code from the DOM', async () => {
  const f = fixture({state: {...initial(), connected: true, phase: 'connected', pair_code: 'PAIR-123456', pair_expires_at: now / 1000 + 60}});
  await f.page.refresh();
  assert.equal(f.el('pair-panel').hidden, false);
  assert.equal(f.el('pair-code').textContent, 'PAIR-123456');
  f.state.pair_expires_at = now / 1000 - 1;
  await f.page.refresh();
  assert.equal(f.el('pair-panel').hidden, true);
  assert.equal(f.el('pair-code').textContent, '');
  assert.match(f.el('pair-status').textContent, /过期/);
  f.state.pair_expires_at = now / 1000 + 60;
  f.state.owner_bound = true;
  f.state.owner_name = '小林';
  await f.page.refresh();
  assert.equal(f.el('pair-panel').hidden, true);
  assert.equal(f.el('pair-code').textContent, '');
  assert.equal(f.el('pair').disabled, true);
  assert.equal(f.el('owner-name').disabled, true);
  assert.equal(f.el('unpair').disabled, false);
  assert.equal(f.el('reset').disabled, false);
  f.state.busy = true;
  await f.page.refresh();
  assert.equal(f.el('unpair').disabled, true);
  assert.equal(f.el('reset').disabled, true);
  assert.equal(f.el('disconnect').disabled, false);
});

test('background startup does not claim a binding link or connection before it exists', async () => {
  const state = initial();
  const f = fixture({fetch: (_url, body) => {
    if (body) state.phase = 'starting';
    return response({...state});
  }});
  await f.page.refresh();
  await f.page.action('bind');
  assert.match(f.el('notice').textContent, /正在准备/);
  assert.equal(f.el('binding-panel').hidden, true);
  assert(!f.el('notice').textContent.includes('已连接'));
  state.phase = 'binding';
  await f.page.refresh();
  assert.match(f.el('notice').textContent, /正在准备/);
  state.qr_url = 'https://q.qq.com/qqbot/openclaw/connect.html?task_id=test';
  await f.page.refresh();
  assert.match(f.el('notice').textContent, /页面已准备好/);
  assert.equal(f.el('binding-panel').hidden, false);
  state.phase = 'connecting';
  await f.page.refresh();
  assert.match(f.el('notice').textContent, /正在连接/);
  assert.equal(f.el('binding-panel').hidden, true);
  state.phase = 'connected';
  state.connected = true;
  await f.page.refresh();
  assert.match(f.el('notice').textContent, /已连接/);
  assert.match(f.el('notice').textContent, /确认你的身份/);
});

test('an older status read cannot restore a revoked pairing', async () => {
  const pending = deferred();
  let reads = 0;
  const bound = {...initial(), connected: true, phase: 'connected', owner_bound: true};
  const f = fixture({fetch: (_url, body) => body ? response({...bound, owner_bound: false}) : ++reads === 1 ? response(bound) : pending.promise});
  await f.page.refresh();
  const reading = f.page.refresh();
  await f.page.action('unpair');
  pending.resolve(response(bound));
  await reading;
  assert.equal(f.page.state.owner_bound, false);
  assert.equal(f.el('unpair').disabled, true);
});

test('disconnect stays available during a pending connection and its state wins', async () => {
  const pending = deferred();
  const f = fixture({fetch: (_url, body) => body?.action === 'bind' ? pending.promise : response(initial())});
  await f.page.refresh();
  const binding = f.page.action('bind');
  await flush();
  assert.equal(f.el('bind').disabled, true);
  assert.equal(f.el('disconnect').disabled, false);
  await f.page.action('disconnect');
  pending.resolve(response({...initial(), phase: 'binding'}));
  await binding;
  assert.equal(f.page.state.phase, 'offline');
  assert.match(f.el('notice').textContent, /已断开/);
  assert.deepEqual(f.calls.filter(call => call.body).map(call => call.body.action), ['bind', 'disconnect']);
});

test('failed writes never claim completion or automatically retry them', async () => {
  const f = fixture({state: initial(), fetch: (_url, body) => body ? response({error: 'QQ 操作未完成'}, 503) : response(initial())});
  await f.page.refresh();
  await f.page.action('bind');
  assert.equal(f.el('notice').dataset.error, 'true');
  assert.match(f.el('notice').textContent, /未完成/);
  assert.equal(f.el('bind').disabled, true);
  await f.page.refresh();
  assert.equal(f.calls.filter(call => call.body).length, 1);
  assert.equal(f.el('bind').disabled, false);
});

test('pagehide aborts requests and late responses cannot restore pairing details', async () => {
  const pending = deferred();
  const f = fixture({fetch: () => pending.promise});
  const reading = f.page.refresh();
  await flush();
  f.listeners.get('pagehide')();
  assert.equal(f.calls[0].options.signal.aborted, true);
  pending.resolve(response({...initial(), connected: true, phase: 'connected', pair_code: 'PAIR-123456', pair_expires_at: now / 1000 + 60}));
  await reading;
  assert.equal(f.page.state, null);
});

test('reply mode defaults to text and unavailable voice leaves text selectable', async () => {
  const f = fixture();
  await f.page.refresh();
  assert.equal(f.el('reply-text').getAttribute('aria-pressed'), 'true');
  assert.equal(f.el('reply-voice').getAttribute('aria-pressed'), 'false');
  assert.equal(f.el('reply-text').disabled, false);
  assert.equal(f.el('reply-voice').disabled, true);
  assert.match(f.el('reply-hint').textContent, /语音服务尚未就绪/);
  await f.el('reply-voice').onclick();
  await f.page.action('reply_mode', 'invalid');
  assert.equal(f.calls.filter(call => call.body).length, 0);

  f.state.reply_mode = 'voice';
  await f.page.refresh();
  assert.equal(f.el('reply-voice').getAttribute('aria-pressed'), 'true');
  assert.equal(f.el('reply-text').disabled, false);
});

test('reply mode can change before QQ connection, waits for confirmation and survives refresh', async () => {
  const pending = deferred();
  const state = {...initial(), reply_mode: 'text', voice_available: true};
  const f = fixture({fetch: (_url, body) => body ? pending.promise : response({...state})});
  await f.page.refresh();
  const changing = f.el('reply-voice').onclick();
  await flush();
  const post = f.calls.find(call => call.body);
  assert.deepEqual(post.body, {action: 'reply_mode', reply_mode: 'voice'});
  assert.equal(post.url, '/api/qq/action');
  assert.equal(post.options.headers['Content-Type'], 'application/json');
  assert.equal(f.el('reply-text').getAttribute('aria-pressed'), 'true');
  assert.equal(f.el('reply-text').disabled, true);
  assert.equal(f.el('reply-voice').disabled, true);
  assert(!f.el('notice').textContent.includes('已切换'));
  state.reply_mode = 'voice';
  pending.resolve(response({...state}));
  await changing;
  assert.equal(f.el('reply-voice').getAttribute('aria-pressed'), 'true');
  assert.equal(f.el('notice').textContent, '已切换为语音回复，下条消息生效。');
  await f.page.refresh();
  assert.equal(f.el('reply-voice').getAttribute('aria-pressed'), 'true');
  await f.el('reply-voice').onclick();
  assert.equal(f.calls.filter(call => call.body).length, 1);
});

test('text can be restored while voice is unavailable', async () => {
  const state = {...initial(), reply_mode: 'voice', voice_available: false};
  const f = fixture({fetch: (_url, body) => {
    if (body) state.reply_mode = body.reply_mode;
    return response({...state});
  }});
  await f.page.refresh();
  await f.el('reply-text').onclick();
  assert.deepEqual(f.calls.find(call => call.body).body, {action: 'reply_mode', reply_mode: 'text'});
  assert.equal(f.el('reply-text').getAttribute('aria-pressed'), 'true');
  assert.equal(f.el('notice').textContent, '已切换为文字回复，下条消息生效。');
});

test('reply mode controls are disabled while replying or disconnected from the local service', async () => {
  const f = fixture({state: {...initial(), voice_available: true, busy: true}});
  await f.page.refresh();
  for (const mode of ['text', 'voice']) assert.equal(f.el(`reply-${mode}`).disabled, true);
  assert.match(f.el('reply-hint').textContent, /完成后就可以切换/);
  await f.el('reply-voice').onclick();
  assert.equal(f.calls.filter(call => call.body).length, 0);
  f.state.busy = false;
  await f.page.refresh();
  for (const mode of ['text', 'voice']) assert.equal(f.el(`reply-${mode}`).disabled, false);
  f.page.online = false;
  f.page.render();
  for (const mode of ['text', 'voice']) assert.equal(f.el(`reply-${mode}`).disabled, true);
});

test('failed or unconfirmed reply mode writes never claim the selection was saved', async () => {
  const state = {...initial(), reply_mode: 'text', voice_available: true};
  for (const writeResponse of [response({error: '回复方式保存失败'}, 503), response(state)]) {
    const f = fixture({fetch: (_url, body) => body ? writeResponse : response(state)});
    await f.page.refresh();
    await f.el('reply-voice').onclick();
    assert.equal(f.el('reply-text').getAttribute('aria-pressed'), 'true');
    assert.equal(f.el('notice').dataset.error, 'true');
    assert(!f.el('notice').textContent.includes('已切换'));
    await f.page.refresh();
    assert.equal(f.calls.filter(call => call.body).length, 1);
  }
});

test('an older status read cannot undo a confirmed reply mode change', async () => {
  const pending = deferred();
  let reads = 0;
  const state = {...initial(), reply_mode: 'text', voice_available: true};
  const f = fixture({fetch: (_url, body) => body ? response({...state, reply_mode: body.reply_mode})
    : ++reads === 1 ? response(state) : pending.promise});
  await f.page.refresh();
  const reading = f.page.refresh();
  await f.el('reply-voice').onclick();
  pending.resolve(response(state));
  await reading;
  assert.equal(f.page.state.reply_mode, 'voice');
  assert.equal(f.el('reply-voice').getAttribute('aria-pressed'), 'true');
});

test('markup provides explicit steps, private pairing and secure external links', () => {
  const html = fs.readFileSync(new URL('../brain/static/qq.html', import.meta.url), 'utf8');
  assert.match(html, /第一步/);
  assert.match(html, /第二步/);
  assert.match(html, /id="binding-link"[^>]*rel="noopener noreferrer"/);
  assert.match(html, /id="notice"[^>]*aria-live="polite"/);
  assert.match(html, /for="owner-name"/);
  assert.match(html, /id="reply-text"[^>]*aria-pressed="true"[^>]*>文字</);
  assert.match(html, /id="reply-voice"[^>]*aria-pressed="false"[^>]*>语音</);
  assert.match(html, /role="group"[^>]*aria-labelledby="reply-title"/);
  assert.match(html, /语音生成失败或内容过长时，会改发文字/);
  assert.match(html, /<h1>QQ 聊天<\/h1>/);
  assert.match(html, /第二步 · 仅私聊需要/);
  assert.match(html, /群聊无需身份配对/);
  assert.match(html, /不会带入你的私聊或长期记忆/);
  assert.match(html, /私聊和群聊共用此设置/);
  assert.match(html, /id="reset"[^>]*>开始一段新私聊</);
  assert(!html.includes('<svg'));
  const index = fs.readFileSync(new URL('../brain/static/index.html', import.meta.url), 'utf8');
  assert.match(index, /id="qq-link"[^>]*href="\/qq"/);
  assert.match(index, /getElementById\('qq-link'\)\.hidden = true/);
});
