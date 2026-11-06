(function (root) {
  'use strict';
  const BASE = '/api/qq';
  const PHASES = {offline: '未连接', starting: '正在启动', binding: '等待扫码', connecting: '正在连接', connected: '已连接', error: '连接异常'};
  const ACTIONS = ['bind', 'connect', 'disconnect', 'pair', 'unpair', 'reset', 'reply_mode'];
  function safeBindingURL(value) {
    if (typeof value !== 'string') return null;
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'q.qq.com'
        && url.pathname === '/qqbot/openclaw/connect.html' && !url.username && !url.password
        && !url.port && !url.hash ? url.href : null;
    } catch (_) { return null; }
  }
  function timestamp(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value * 1000;
    if (typeof value === 'string' && value) return Date.parse(value);
    return NaN;
  }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : 0; }

  class QQPage {
    constructor({documentRef = root.document, windowRef = root, fetcher = root.fetch.bind(root), now = () => Date.now()} = {}) {
      this.document = documentRef;
      this.window = windowRef;
      this.fetcher = fetcher;
      this.now = now;
      this.state = null;
      this.online = false;
      this.pending = new Set();
      this.version = 0;
      this.reading = false;
      this.stopped = false;
      this.ownerDirty = false;
      this.readFailureNotice = false;
      this.connectionIntent = '';
      this.controllers = new Set();
      for (const action of ACTIONS.filter(action => !['pair', 'reply_mode'].includes(action))) {
        this.el(action).onclick = () => this.action(action);
      }
      for (const mode of ['text', 'voice']) {
        this.el(`reply-${mode}`).onclick = () => this.action('reply_mode', mode);
      }
      this.el('pair-form').onsubmit = event => { event.preventDefault(); return this.action('pair'); };
      this.el('owner-name').oninput = () => { this.ownerDirty = true; };
      this.el('refresh').onclick = () => this.refresh();
      this.onHide = () => this.stop();
      this.onShow = event => { if (event.persisted) this.start(); };
      this.window.addEventListener?.('pagehide', this.onHide);
      this.window.addEventListener?.('pageshow', this.onShow);
    }
    el(id) { return this.document.getElementById(id); }
    message(text, error = false) {
      this.el('notice').textContent = text;
      this.el('notice').dataset.error = String(error);
    }
    async request(path, body) {
      const controller = new AbortController();
      this.controllers.add(controller);
      const timeout = setTimeout(() => controller.abort(), 25000);
      try {
        const response = await this.fetcher(path, {method: body ? 'POST' : 'GET', cache: 'no-store',
          credentials: 'same-origin', headers: body ? {'Content-Type': 'application/json'} : {},
          ...(body ? {body: JSON.stringify(body)} : {}), signal: controller.signal});
        let data;
        try { data = await response.json(); } catch (_) { throw new Error('无法读取本机服务的回复，请刷新状态。'); }
        if (!response.ok) throw new Error(typeof data?.error === 'string' ? data.error : '本机服务暂时无法处理，请稍后重试。');
        if (!data || typeof data !== 'object' || Array.isArray(data) || typeof data.phase !== 'string') {
          throw new Error('连接状态不完整，请刷新状态。');
        }
        return data;
      } finally {
        clearTimeout(timeout);
        this.controllers.delete(controller);
      }
    }
    render() {
      const state = this.state || {};
      const available = state.available === true && this.online;
      const connected = state.connected === true;
      const ownerBound = state.owner_bound === true;
      const groupEnabled = state.group_enabled === true;
      const transitioning = ['starting', 'binding', 'connecting'].includes(state.phase);
      const waiting = this.pending.size > 0;
      const needsPairing = this.online && connected && !ownerBound;
      this.el('phase').textContent = !this.online ? '状态待确认'
        : connected ? (ownerBound ? '已连接 · 可聊天' : groupEnabled ? '已连接 · 群聊可用' : '已连接 · 待配对') : (PHASES[state.phase] || '状态待确认');
      this.el('phase').dataset.connected = String(this.online && connected && (ownerBound || groupEnabled));
      this.el('phase').dataset.pairing = String(needsPairing);
      this.el('pair-next').hidden = !needsPairing;
      this.el('pair-next').textContent = groupEnabled ? '私聊还需第二步配对：让她认出你 ↓' : '继续完成第二步：让她认出你 ↓';
      this.el('connection').textContent = this.online ? '已连接本机管理服务' : '本机状态暂不可用，正在等待重新连接…';
      this.el('status-detail').textContent = !available ? 'QQ 服务尚未就绪，请先确认主服务已启动。'
        : !connected ? (transitioning ? '正在连接 QQ，请按下方提示完成。' : '连接后，就能在 QQ 里继续你们的对话。')
        : !ownerBound ? (groupEnabled ? '群聊已可 @ 她，私聊还需身份配对。请在第二步生成配对码，再用你的 QQ 私聊发送给机器人。'
          : '还差身份配对，普通私聊暂时不会回复。请在第二步生成配对码，再用你的 QQ 私聊发送给机器人。')
        : state.busy ? '她正在读你的消息、准备回复。'
        : groupEnabled ? '她已准备好，可以私聊，或在 QQ 群里 @ 她说话。' : '她已准备好，在 QQ 私聊里发来一句话吧。';
      this.el('group-panel').hidden = !groupEnabled;
      this.el('group-status').textContent = this.online && connected
        ? '机器人已连接，把她加入 QQ 群，发文字时 @ 她即可。' : '连接后，把机器人加入 QQ 群，发文字时 @ 她即可。';
      this.el('group-count').textContent = `当前活跃 ${count(state.group_session_count)} 段群聊对话。`;
      this.el('reply-scope').hidden = !groupEnabled;
      this.el('service-error').textContent = typeof state.error === 'string' ? state.error : '';
      this.el('service-error').hidden = !state.error;
      this.el('owner-status').textContent = ownerBound ? `${state.owner_name || '已配对'}${state.peer_hint ? ` · ${state.peer_hint}` : ''}` : '尚未配对';
      const activity = timestamp(state.last_activity);
      this.el('last-activity').textContent = Number.isFinite(activity) && activity > 0 ? new Date(activity).toLocaleString('zh-CN') : '尚未处理消息';
      this.el('counts').textContent = `已处理 ${count(state.counts?.received)} · 回复 ${count(state.counts?.replied)}`;
      this.el('bind').disabled = !available || connected || transitioning || waiting;
      this.el('bind').textContent = state.account_bound ? '重新扫码绑定' : '开始扫码绑定';
      this.el('connect').disabled = !available || !state.account_bound || connected || transitioning || waiting;
      // Keep disconnect available while connecting or preparing a reply.
      this.el('disconnect').disabled = !this.online || this.pending.has('disconnect') || (!connected && !transitioning && !waiting);
      this.el('pair').disabled = !available || !connected || ownerBound || waiting;
      this.el('owner-name').disabled = !available || !connected || ownerBound || waiting;
      this.el('unpair').disabled = !this.online || !ownerBound || waiting || state.busy === true;
      this.el('reset').disabled = !this.online || !ownerBound || waiting || state.busy === true;
      const replyMode = state.reply_mode === 'voice' ? 'voice' : 'text';
      const modeDisabled = !this.online || waiting || state.busy === true;
      this.el('reply-text').disabled = modeDisabled;
      this.el('reply-voice').disabled = modeDisabled || state.voice_available !== true;
      this.el('reply-text').setAttribute('aria-pressed', String(replyMode === 'text'));
      this.el('reply-voice').setAttribute('aria-pressed', String(replyMode === 'voice'));
      this.el('reply-hint').textContent = !this.online ? '连接本机管理服务后，可以调整回复方式。'
        : state.busy === true ? '她正在准备回复，完成后就可以切换。'
        : state.voice_available !== true ? '语音服务尚未就绪，暂时可用文字回复。'
        : `当前使用${replyMode === 'voice' ? '语音' : '文字'}回复。`;
      this.el('refresh').disabled = this.reading || waiting;
      if (ownerBound || !this.ownerDirty) this.el('owner-name').value = typeof state.owner_name === 'string' ? state.owner_name : '';
      this.el('account-status').textContent = state.account_bound ? '机器人已绑定。下次启动主服务后，点击“重新连接”即可。' : '尚未绑定 QQ 机器人。';
      const binding = this.online && state.phase === 'binding' ? safeBindingURL(state.qr_url) : null;
      this.el('binding-panel').hidden = !binding;
      if (binding) this.el('binding-link').href = binding;
      else this.el('binding-link').removeAttribute('href');
      const expires = timestamp(state.pair_expires_at);
      const code = this.online && connected && !ownerBound && Number.isFinite(expires) && expires > this.now()
        && typeof state.pair_code === 'string' ? state.pair_code : '';
      this.el('pair-panel').hidden = !code;
      this.el('pair-code').textContent = code;
      this.el('pair-expiry').textContent = code ? `有效至 ${new Date(expires).toLocaleTimeString('zh-CN')}；请勿把配对码交给他人。` : '';
      this.el('pair-status').textContent = ownerBound ? '身份已确认，她会沿用这个称呼对应的长期记忆。'
        : !connected ? '连接机器人后，就可以生成配对码。'
        : code ? '等待你在 QQ 私聊发送配对码…'
        : state.pair_code ? '配对码已过期，请重新生成。' : '机器人已连接，填写称呼后生成配对码。';
    }
    async refresh() {
      if (this.stopped || this.reading || this.pending.size) return;
      this.reading = true;
      const version = this.version;
      this.el('refresh').disabled = true;
      try {
        const state = await this.request(BASE);
        if (this.stopped || version !== this.version) return;
        this.state = state;
        this.online = true;
        if (this.readFailureNotice) {
          this.message('');
          this.readFailureNotice = false;
        }
        this.connectionProgress();
      } catch (error) {
        if (this.stopped || version !== this.version) return;
        this.online = false;
        this.message(error.name === 'AbortError' ? '读取状态超时，将自动重试。' : error.message, true);
        this.readFailureNotice = true;
      } finally {
        this.reading = false;
        if (!this.stopped) this.render();
      }
    }
    connectionProgress() {
      if (!this.online || !this.connectionIntent) return;
      const state = this.state;
      if (state.error || state.phase === 'error') {
        this.message(state.error || 'QQ 连接未完成，请查看当前状态。', true);
        this.connectionIntent = '';
      } else if (state.connected === true && state.phase === 'connected') {
        this.message(state.group_enabled === true
          ? (state.owner_bound ? 'QQ 已连接，可以私聊或在群里 @ 她。' : 'QQ 已连接，群聊可 @ 她；私聊请继续确认你的身份。')
          : state.owner_bound ? 'QQ 已连接，可以继续私聊。' : 'QQ 机器人已连接，请继续确认你的身份。');
        this.connectionIntent = '';
      } else if (state.phase === 'offline') {
        this.message('QQ 连接已停止。');
        this.connectionIntent = '';
      } else if (this.connectionIntent === 'bind' && state.phase === 'binding' && safeBindingURL(state.qr_url)) {
        this.message('绑定页面已准备好，请打开下方腾讯页面完成扫码。');
      } else {
        this.message(this.connectionIntent === 'bind' && ['starting', 'binding'].includes(state.phase)
          ? '正在准备腾讯绑定页面，请稍候…' : '正在连接 QQ，请稍候…');
      }
    }
    async action(action, replyMode) {
      if (this.stopped || !ACTIONS.includes(action)) return;
      if (action === 'reply_mode' && !['text', 'voice'].includes(replyMode)) return;
      if (this.el(action === 'reply_mode' ? `reply-${replyMode}` : action).disabled) return;
      const body = {action};
      if (action === 'reply_mode') {
        if ((this.state?.reply_mode === 'voice' ? 'voice' : 'text') === replyMode) return;
        body.reply_mode = replyMode;
      }
      if (action === 'pair') {
        body.owner_name = this.el('owner-name').value.trim();
        if (!body.owner_name || body.owner_name.length > 40) {
          this.message('请填写 1 至 40 字的聊天称呼，并与主页面保持一致。', true);
          this.el('owner-name').focus();
          return;
        }
      }
      const version = ++this.version;
      this.pending.add(action);
      this.readFailureNotice = false;
      this.connectionIntent = ['bind', 'connect'].includes(action) ? action : '';
      this.message(action === 'disconnect' ? '正在断开连接…' : '正在处理…');
      this.render();
      try {
        const state = await this.request(BASE + '/action', body);
        if (this.stopped || version !== this.version) return;
        this.state = state;
        this.online = true;
        const messages = {disconnect: '已断开 QQ 连接。', pair: '配对码已生成，请在 QQ 私聊中发送。',
          unpair: '已解除 QQ 身份绑定。', reset: '已开始新私聊，长期记忆仍然保留。'};
        if (this.connectionIntent) this.connectionProgress();
        else if (action === 'reply_mode') {
          const confirmed = state.reply_mode === replyMode;
          this.message(state.error || (confirmed ? `已切换为${replyMode === 'voice' ? '语音' : '文字'}回复，下条消息生效。`
            : '回复方式尚未确认，请刷新状态。'), Boolean(state.error) || !confirmed);
        }
        else this.message(state.error || messages[action], Boolean(state.error));
      } catch (error) {
        if (this.stopped || version !== this.version) return;
        this.online = false;
        this.connectionIntent = '';
        this.message(error.name === 'AbortError' ? '操作结果尚未确认，请刷新状态。' : error.message, true);
      } finally {
        this.pending.delete(action);
        if (!this.stopped) this.render();
      }
    }
    start() {
      this.stopped = false;
      clearInterval(this.timer);
      void this.refresh();
      this.timer = setInterval(() => { this.render(); void this.refresh(); }, 3000);
    }
    stop() {
      this.stopped = true;
      ++this.version;
      clearInterval(this.timer);
      for (const controller of this.controllers) controller.abort();
      this.online = false;
      this.render();
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {QQPage, safeBindingURL};
  else if (root.document?.getElementById('qq-page')) {
    const page = new QQPage();
    page.start();
  }
})(typeof window === 'undefined' ? globalThis : window);
