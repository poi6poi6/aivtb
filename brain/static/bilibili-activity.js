'use strict';
(() => {
  const BASE = '/api/bilibili-activity';
  const COMMANDS = new Set(['account', 'search', 'read', 'publish', 'verify', 'interact', 'verify_interaction']);
  const INTERACTION_STATES = {draft:'待执行', ready:'待执行', executing:'正在执行', unknown:'结果待核验',
    completed:'已确认完成', failed:'未完成'};
  const STATES = {draft:'待发布', ready:'待发布', publishing:'正在发布', unknown:'发布结果待核验',
    uncertain:'发布结果待核验', published:'已发布', discarded:'已丢弃', failed:'操作失败'};
  const STATUS = {idle:'待命', disabled:'自动浏览已关闭', stopped:'已停止', stopping:'正在停止',
    browsing:'正在浏览', reading:'正在阅读', drafting:'正在整理草稿', publishing:'正在发布',
    verifying:'正在核验', running:'正在活动', waiting_browser:'等待浏览器', waiting:'等待执行',
    failed:'活动遇到问题', error:'活动遇到问题', blocked:'等待处理', ready:'准备就绪', completed:'本次活动已结束'};
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const text = (value, limit=4000) => typeof value === 'string' ? value.slice(0, limit) : '';
  const errorText = (error, fallback='操作未完成，请刷新状态后再试。') =>
    text(typeof error === 'string' ? error : error?.message || error?.detail) || fallback;
  const uid = value => typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  const validUID = value => /^[1-9]\d{0,19}$/.test(uid(value));
  function safeURL(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
        (url.hostname === 'bilibili.com' || url.hostname.endsWith('.bilibili.com') || url.hostname === 'b23.tv')
        ? url.href : null;
    } catch (_) { return null; }
  }
  async function request(fetcher, path, body, timeoutMs=12000) {
    const controller = new AbortController();
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(Object.assign(new Error('本机服务响应超时。操作可能已提交，请刷新核对；发布结果不明时先核验，不要重复发布。'), {timeout:true}));
      }, timeoutMs);
    });
    const work = async () => {
      let response;
      try {
        response = await fetcher(path, {method:body === undefined ? 'GET' : 'POST',
          credentials:'same-origin', cache:'no-store', signal:controller.signal,
          ...(body === undefined ? {} : {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
      } catch (_) { throw new Error('无法连接本机服务。操作可能已提交，请刷新核对状态。'); }
      let data;
      try { data = await response.json(); }
      catch (_) { throw new Error('本机服务返回的数据无法读取，请刷新核对状态。'); }
      if (!response.ok) throw Object.assign(new Error(errorText(data?.error,
        response.status === 403 ? '此页面仅允许本机管理，请从本机服务地址打开。' : undefined)), {status:response.status});
      if (!object(data)) throw new Error('本机服务返回的数据无效。');
      return data;
    };
    try { return await Promise.race([work(), deadline]); }
    finally { clearTimeout(timer); }
  }

  class ActivityBridge {
    constructor({windowRef, fetcher, onStatus=()=>{}, commandTimeoutMs=65000, requestTimeoutMs=12000,
      intervalMs=1000} = {}) {
      this.window = windowRef;
      this.fetcher = fetcher;
      this.onStatus = onStatus;
      this.commandTimeoutMs = commandTimeoutMs;
      this.requestTimeoutMs = requestTimeoutMs;
      this.intervalMs = intervalMs;
      this.origin = windowRef.location.origin;
      this.closed = false;
      this.busy = false;
      this.pending = null;
      this.receipt = null;
      this.completed = new Map();
      this.timer = null;
      this.receive = event => {
        const message = event.data;
        if (this.closed || event.source !== this.window || event.origin !== this.origin ||
            message?.source !== 'ai-vtuber-bilibili-activity-extension' || message.type !== 'result' ||
            !this.pending || message.request_id !== this.pending.id || typeof message.ok !== 'boolean') return;
        if (message.ok && !object(message.result)) return;
        this.pending.finish({ok:message.ok, ...(message.ok ? {result:message.result}
          : {error:errorText(message.error, '浏览器操作未完成，请检查 B站标签页。')})});
      };
      this.pagehide = () => this.stop();
      windowRef.addEventListener('message', this.receive);
      windowRef.addEventListener('pagehide', this.pagehide);
    }

    exchange(command) {
      return new Promise(resolve => {
        let timer, controlTimer;
        const finish = value => {
          clearTimeout(timer);
          clearTimeout(controlTimer);
          this.pending = null;
          resolve(value);
        };
        this.pending = {id:command.id, finish};
        const checkControl = async () => {
          if (this.closed || this.pending?.id !== command.id) return;
          try {
            const status = await request(this.fetcher, BASE + '/browser',
              {action:'claim', active_id:command.id}, this.requestTimeoutMs);
            if (!this.closed && this.pending?.id === command.id &&
                Array.isArray(status.cancelled) && status.cancelled.includes(command.id)) {
              this.window.postMessage({source:'ai-vtuber-bilibili-activity', type:'cancel',
                request_id:command.id}, this.origin);
              this.onStatus('宿主已停止本次浏览器任务；发布若已提交，请核验结果。');
              finish(null);
            }
          } catch (_) {
            // A failed control read is not a cancellation decision. Keep the
            // original request and its eventual receipt, especially for publish.
          } finally {
            if (!this.closed && this.pending?.id === command.id) controlTimer = setTimeout(checkControl, this.intervalMs);
          }
        };
        controlTimer = setTimeout(checkControl, this.intervalMs);
        timer = setTimeout(() => {
          const detail = command.action === 'interact'
            ? '未收到浏览器执行结果，请检查浏览器助手与 B站标签页；互动结果可能未知，请先核验，不要重复执行。'
            : '未收到浏览器执行结果，请检查浏览器助手与 B站标签页；发布结果可能未知，请先核验，不要重复发布。';
          finish(['publish','interact'].includes(command.action) ? {ok:true, result:{status:'unknown', detail}}
            : {ok:false, error:detail});
        }, this.commandTimeoutMs);
        try {
          this.window.postMessage({source:'ai-vtuber-bilibili-activity', type:'request',
            request_id:command.id, action:command.action, payload:command.payload || {}}, this.origin);
        } catch (_) { finish({ok:false, error:'无法连接浏览器助手，请检查扩展后再试。'}); }
      });
    }

    async deliverReceipt() {
      if (!this.receipt || this.closed) return;
      try {
        const response = await request(this.fetcher, BASE + '/browser', this.receipt, this.requestTimeoutMs);
        if (response.ok === false) {
          // The broker leases a command once. A negative receipt means it is
          // already settled or expired, not permission to run the command again.
          this.receipt = null;
          this.onStatus('执行回执已处理或过期，请刷新状态；发布结果不明时先核验。');
          return;
        }
        this.receipt = null;
        this.onStatus('浏览器执行回执已送达，结果以草稿与活动状态为准。');
      } catch (error) {
        if ([409,410].includes(error.status)) {
          this.receipt = null;
          this.onStatus('本次执行回执已过期，请刷新状态；发布结果不明时先核验。');
        } else {
          this.onStatus('执行回执暂未送达，正在重传回执；不会重新执行发布。');
          throw error;
        }
      }
    }

    async step() {
      if (this.closed || this.busy) return;
      this.busy = true;
      try {
        if (this.receipt) { await this.deliverReceipt(); return; }
        const response = await request(this.fetcher, BASE + '/browser', {action:'claim'}, this.requestTimeoutMs);
        if (this.closed) return;
        if (response.ok === false) throw new Error(errorText(response.error));
        const command = response.command;
        if (command == null) return;
        if (!object(command) || typeof command.id !== 'string' || !command.id || command.id.length > 160) {
          throw new Error('收到的浏览任务无效，请刷新本机服务状态。');
        }
        if (this.completed.has(command.id)) {
          this.receipt = this.completed.get(command.id);
          await this.deliverReceipt();
          return;
        }
        let result;
        if (!COMMANDS.has(command.action) || command.payload != null && !object(command.payload)) {
          result = {ok:false, error:'此浏览任务不受支持，未执行。'};
        } else {
          this.onStatus('等待浏览器助手执行，请保持本页与 B站标签页打开。');
          result = await this.exchange(command);
        }
        if (this.closed || !result) return;
        this.receipt = {action:'result', id:command.id, ...result};
        this.completed.set(command.id, this.receipt);
        if (this.completed.size > 128) this.completed.delete(this.completed.keys().next().value);
        await this.deliverReceipt();
      } catch (error) {
        if (!this.closed && !this.receipt) this.onStatus(errorText(error, '浏览器通道暂不可用，请检查本机服务。'));
      } finally { this.busy = false; }
    }

    start() {
      if (this.started || this.closed) return;
      this.started = true;
      const tick = async () => {
        await this.step();
        if (!this.closed) this.timer = setTimeout(tick, this.intervalMs);
      };
      tick();
    }

    stop() {
      this.closed = true;
      clearTimeout(this.timer);
      this.pending?.finish(null);
      this.window.removeEventListener('message', this.receive);
      this.window.removeEventListener('pagehide', this.pagehide);
    }
  }

  class ActivityPage {
    constructor({documentRef, windowRef, fetcher} = {}) {
      this.document = documentRef;
      this.window = windowRef;
      this.fetcher = fetcher;
      this.state = null;
      this.online = false;
      this.busy = '';
      this.epoch = 0;
      this.readVersion = 0;
      this.settingsRevision = 0;
      this.dirty = false;
      this.closed = false;
      this.cards = new Map();
      this.interactionCards = new Map();
      this.signatures = new Map();
      this.el = id => this.document.getElementById(id);
      this.el('settings-form').oninput = () => {
        this.dirty = true;
        this.settingsRevision++;
        this.el('settings-hint').textContent = '有未保存的修改；当前活动仍使用已保存的设置。';
      };
      this.el('settings-form').onsubmit = event => { event.preventDefault(); return this.save(); };
      this.el('run').onclick = () => this.action('run');
      this.el('stop').onclick = () => this.action('stop');
      this.el('read-account').onclick = () => this.action('account');
      this.el('refresh').onclick = () => this.refresh();
      this.bridge = new ActivityBridge({windowRef, fetcher,
        onStatus:message => { if (!this.closed) this.el('bridge-status').textContent = message; }});
      windowRef.addEventListener('pagehide', () => this.stop());
    }

    node(tag, content, className) {
      const node = this.document.createElement(tag);
      if (content !== undefined) node.textContent = content;
      if (className) node.className = className;
      return node;
    }

    notice(message, failed=false) {
      this.el('notice').textContent = message;
      this.el('notice').dataset.error = String(failed);
    }

    savedSettings() {
      const settings = this.state?.settings || {};
      return {allow_likes:false, allow_comments:false, daily_interactions:3,
        active_start_hour:0, active_end_hour:24, ...settings, enabled:this.state?.enabled ?? settings.enabled ?? false,
        auto_publish:this.state?.auto_publish ?? settings.auto_publish ?? false};
    }

    settingsPayload() {
      const settings = {enabled:this.el('enabled').checked, auto_publish:this.el('auto-publish').checked,
        allow_likes:this.el('allow-likes').checked, allow_comments:this.el('allow-comments').checked,
        expected_uid:this.el('expected-uid').value.trim()};
      for (const [key, id, low, high, label] of [
        ['interval_minutes','interval-minutes',10,1440,'浏览间隔'], ['max_items','max-items',1,10,'每次阅读条数'],
        ['daily_posts','daily-posts',0,10,'每日发布上限'], ['daily_interactions','daily-interactions',0,10,'每日互动上限'],
        ['active_start_hour','active-start-hour',0,23,'开始时间'], ['active_end_hour','active-end-hour',0,24,'结束时间']]) {
        const raw = this.el(id).value;
        const value = Number(raw);
        if (!String(raw).trim() || !Number.isInteger(value) || value < low || value > high) {
          this.el(id).focus();
          throw new Error(`${label}须为 ${low}–${high} 的整数。`);
        }
        settings[key] = value;
      }
      if (settings.expected_uid && !validUID(settings.expected_uid)) throw new Error('B站 UID 须为正整数，请检查账号资料。');
      if (settings.auto_publish && (!validUID(settings.expected_uid) || settings.daily_posts < 1)) {
        throw new Error('开启自动发布前，请填写允许发布的账号 UID，并将每日发布上限设为至少 1。');
      }
      if ((settings.allow_likes || settings.allow_comments) && !validUID(settings.expected_uid)) {
        throw new Error('开启互动前，请填写使用的 B站账号 UID。');
      }
      if (settings.active_start_hour === settings.active_end_hour) throw new Error('活动开始和结束时间不能相同。');
      return settings;
    }

    async save() {
      let settings;
      try { settings = this.settingsPayload(); }
      catch (error) { this.notice(error.message, true); return; }
      return this.action('settings', settings, this.settingsRevision);
    }

    controls() {
      const disabled = !this.online || !!this.busy;
      this.el('save-settings').disabled = disabled;
      this.el('run').disabled = disabled || !!this.state?.running;
      this.el('read-account').disabled = disabled || !!this.state?.running;
      this.el('stop').disabled = !this.online || this.busy === 'stop';
      this.el('refresh').disabled = !!this.busy;
      const settings = this.savedSettings();
      const actual = uid(this.state?.browser?.account?.uid);
      const mismatch = actual && validUID(settings.expected_uid) && actual !== uid(settings.expected_uid);
      for (const item of this.cards.values()) {
        for (const button of item.buttons) {
          button.disabled = disabled || !!this.state?.running || button.dataset.action === 'publish' &&
            (!validUID(settings.expected_uid) || Number(settings.daily_posts) < 1 || !!mismatch);
        }
      }
      for (const item of this.interactionCards.values()) {
        const allowed = item.action === 'like' ? settings.allow_likes === true : settings.allow_comments === true;
        for (const button of item.buttons) {
          button.disabled = disabled || !!this.state?.running || button.dataset.action === 'interact' &&
            (!allowed || !validUID(settings.expected_uid) || Number(settings.daily_interactions) < 1 || !!mismatch);
        }
      }
    }

    applyState(payload) {
      const state = object(payload.state) ? payload.state : payload;
      if (!object(state.settings) || !Array.isArray(state.drafts)) return false;
      this.state = state;
      this.online = true;
      this.el('connection').textContent = '已连接本机服务 · 下方为最近一次读取的状态';
      const settings = this.savedSettings();
      if (!this.dirty) {
        this.el('enabled').checked = !!settings.enabled;
        this.el('auto-publish').checked = !!settings.auto_publish;
        this.el('allow-likes').checked = settings.allow_likes === true;
        this.el('allow-comments').checked = settings.allow_comments === true;
        for (const [id, key, fallback] of [['expected-uid','expected_uid',''], ['interval-minutes','interval_minutes',60],
          ['max-items','max_items',3], ['daily-posts','daily_posts',1], ['daily-interactions','daily_interactions',3],
          ['active-start-hour','active_start_hour',0], ['active-end-hour','active_end_hour',24]]) this.el(id).value = String(settings[key] ?? fallback);
      }
      this.el('saved-mode').textContent = `已保存：自动浏览${settings.enabled ? '开启' : '关闭'} · 自动发布${settings.auto_publish ? '开启' : '关闭'} · 点赞${settings.allow_likes === true ? '开启' : '关闭'} · 评论${settings.allow_comments === true ? '开启' : '关闭'}`;
      this.el('interaction-policy').textContent = Number(settings.daily_interactions) < 1
        ? '每日互动额度为 0，当前不能执行点赞或评论。'
        : settings.allow_likes === true || settings.allow_comments === true
          ? `已允许${[settings.allow_likes === true && '点赞', settings.allow_comments === true && '评论'].filter(Boolean).join('、')}；每轮结束可自动执行一条建议，每日合计最多 ${Number(settings.daily_interactions)} 次。`
          : '点赞与评论尚未开启。建议会保留，开启对应开关并保存后才可执行。';
      this.el('activity-status').textContent = STATUS[state.status] ||
        (/[\u4e00-\u9fff]/.test(text(state.status)) ? text(state.status,300) : state.running ? '正在活动' : '待命');
      const account = state.browser?.account || {};
      this.el('account-status').textContent = state.browser?.connected
        ? account.uid ? `最近确认：${text(account.name,100) || 'B站账号'} · UID ${uid(account.uid)}`
          : '活动页面已连接 · 尚未读取登录账号，请检查浏览器助手后读取当前账号。'
        : '活动页面尚未连接，请检查本机服务与浏览器助手后读取当前账号。';
      const expected = uid(settings.expected_uid), actual = uid(account.uid);
      this.el('account-match').textContent = !expected ? '尚未指定发布账号。'
        : !actual ? `允许发布的 UID：${expected}，尚未核对登录账号。`
        : expected === actual ? '登录 UID 与允许发布的账号一致。'
        : `账号不一致：允许 UID ${expected}，当前 UID ${actual}。请切换账号或修改设置后保存。`;
      this.el('harness-status').textContent = `${state.harness?.ready ? '创作连接可用' : '创作连接尚未就绪'}${state.harness?.detail ? ' · ' + text(state.harness.detail,500) : ''}`;
      this.el('activity-error').textContent = errorText(state.error, '');
      this.el('activity-error').hidden = !this.el('activity-error').textContent;
      this.renderDrafts(state.drafts);
      this.renderInteractions(Array.isArray(state.interactions) ? state.interactions : []);
      this.renderDiaries(Array.isArray(state.diaries) ? state.diaries : []);
      this.renderNotes(Array.isArray(state.notes) ? state.notes : []);
      this.renderHistory(Array.isArray(state.history) ? state.history : []);
      this.controls();
      return true;
    }

    async refresh() {
      if (this.closed || this.refreshing) return;
      this.refreshing = true;
      const epoch = this.epoch;
      const readVersion = ++this.readVersion;
      try {
        const payload = await request(this.fetcher, BASE);
        if (this.closed || epoch !== this.epoch || readVersion !== this.readVersion) return;
        if (!this.applyState(payload)) throw new Error('活动状态数据不完整。');
      } catch (error) {
        if (this.closed || epoch !== this.epoch || readVersion !== this.readVersion) return;
        this.online = false;
        this.el('connection').textContent = '连接中断 · 显示的状态可能已过期。' + errorText(error);
        this.controls();
      } finally { this.refreshing = false; }
    }

    async action(action, extra={}, revision=null) {
      if (this.closed || !this.online || this.busy && action !== 'stop') return;
      const epoch = ++this.epoch;
      this.busy = action;
      this.controls();
      this.notice('正在提交…');
      try {
        const timeout = ['publish','interact'].includes(action) ? 165000 : ['account','verify','verify_interaction'].includes(action) ? 85000 : 15000;
        const payload = await request(this.fetcher, BASE + '/action', {action, ...extra}, timeout);
        if (this.closed || epoch !== this.epoch) return;
        if (payload.ok === false || payload.result?.ok === false) throw new Error(errorText(payload.error || payload.result?.error));
        ++this.readVersion;
        if (action === 'settings' && revision === this.settingsRevision) this.dirty = false;
        this.applyState(payload);
        const messages = {settings:'设置已保存。', run:'浏览请求已提交，请查看活动状态。', stop:'停止请求已提交，请核对活动状态。',
          account:'账号读取请求已提交，连接信息以返回状态为准。', publish:'发布请求已提交，请核对草稿状态与动态链接。',
          verify:'核验请求已提交，请查看草稿的最新结果。', discard:'丢弃请求已提交，请查看草稿状态。',
          interact:'互动请求已提交，请核对该条建议的执行结果。', verify_interaction:'核验请求已提交，请查看互动的最新结果。'};
        this.notice(messages[action] || '请求已提交，请核对最新状态。');
        if (action === 'settings') this.el('settings-hint').textContent = this.dirty
          ? '仍有提交期间新修改的内容，保存后才会生效。' : '已保存，后续活动使用这些设置。';
      } catch (error) {
        if (epoch === this.epoch && !this.closed) this.notice(errorText(error), true);
      } finally {
        if (epoch === this.epoch && !this.closed) {
          this.busy = '';
          this.controls();
          await this.refresh();
        }
      }
    }

    sourceLink(url, label) {
      const safe = safeURL(url);
      if (!safe) return null;
      const link = this.node('a', label || safe);
      link.href = safe;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    }

    draftCard(draft, previous) {
      const card = this.node('article');
      const heading = this.node('div', undefined, 'card-heading');
      heading.append(this.node('h3', '动态草稿'), this.node('span', STATES[draft.state] || '状态待确认',
        'badge' + (['unknown','uncertain'].includes(draft.state) ? ' warning' : draft.state === 'published' ? ' success' : '')));
      card.append(heading, this.node('pre', text(draft.text,12000), 'draft-text'));
      const details = this.node('details');
      details.open = !!previous?.details.open;
      details.append(this.node('summary', '查看内容来源'));
      const sources = this.node('ul', undefined, 'sources');
      for (const url of (Array.isArray(draft.source_urls) ? draft.source_urls : []).slice(0,30)) {
        const link = this.sourceLink(url);
        if (link) { const li = this.node('li'); li.append(link); sources.append(li); }
      }
      if (!sources.children.length) sources.append(this.node('li', '暂无可打开的 B站来源链接。'));
      details.append(sources); card.append(details);
      if (draft.error) card.append(this.node('p', errorText(draft.error), 'card-error'));
      if (['unknown','uncertain'].includes(draft.state)) card.append(this.node('p', '发布可能已经发生。请先核验，暂不重复发布，也不丢弃这条待核验记录。', 'hint'));
      const result = this.sourceLink(draft.url, draft.state === 'published' ? '查看已发布动态' : '打开页面核对');
      if (result) card.append(result);
      const buttons = [], row = this.node('div', undefined, 'actions');
      const add = (label, action) => {
        const button = this.node('button', label);
        button.type = 'button'; button.dataset.action = action;
        button.onclick = () => this.action(action, {draft_id:draft.id});
        row.append(button); buttons.push(button);
      };
      if (['draft','ready'].includes(draft.state)) add('发布这条草稿', 'publish');
      if (['unknown','uncertain','published'].includes(draft.state)) add('核验发布结果', 'verify');
      if (draft.state === 'draft') add('丢弃草稿', 'discard');
      if (buttons.length) card.append(row);
      return {node:card, details, buttons};
    }

    renderDrafts(drafts) {
      const container = this.el('drafts'), retained = new Set();
      const visible = drafts.filter(draft => object(draft) && typeof draft.id === 'string' && draft.id);
      this.el('draft-count').textContent = `${visible.filter(draft => draft.state !== 'discarded').length} 条保留记录`;
      let index = 0;
      for (const draft of visible) {
        if (retained.has(draft.id)) continue;
        retained.add(draft.id);
        const signature = JSON.stringify(draft), previous = this.cards.get(draft.id);
        let item = previous;
        if (!item || item.signature !== signature) {
          item = {...this.draftCard(draft, previous), signature};
          previous?.node.remove();
          this.cards.set(draft.id, item);
        }
        if (container.children[index] !== item.node) container.insertBefore(item.node, container.children[index] || null);
        index++;
      }
      for (const [id, item] of this.cards) if (!retained.has(id)) { item.node.remove(); this.cards.delete(id); }
      for (const child of [...container.children]) if (![...this.cards.values()].some(item => item.node === child)) child.remove();
      if (!visible.length) container.append(this.node('p', '还没有草稿。可以先“立即逛一次”，查看浏览结果。', 'empty'));
    }

    date(value) {
      if (value == null || value === '') return '';
      const date = new Date(typeof value === 'number' ? value * 1000 : value);
      return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '';
    }

    interactionCard(item) {
      const card = this.node('article'), heading = this.node('div', undefined, 'card-heading');
      const knownAction = ['like','comment'].includes(item.action);
      heading.append(this.node('h3', item.action === 'like' ? '点赞建议' : item.action === 'comment' ? '评论建议' : '互动记录'),
        this.node('span', INTERACTION_STATES[item.state] || '状态待确认',
          'badge' + (item.state === 'unknown' ? ' warning' : item.state === 'completed' ? ' success' : '')));
      card.append(heading, this.node('time', this.date(item.at)));
      if (item.text) card.append(this.node('pre', text(item.text,4000), 'draft-text'));
      const link = this.sourceLink(item.url, '打开对应视频');
      if (link) card.append(link);
      if (item.error) card.append(this.node('p', errorText(item.error), 'card-error'));
      if (item.state === 'unknown') card.append(this.node('p', '互动可能已经发生。请先核验，不重复执行。', 'hint'));
      const buttons = [], row = this.node('div', undefined, 'actions');
      const add = (label, action) => {
        const button = this.node('button', label);
        button.type = 'button'; button.dataset.action = action;
        button.onclick = () => this.action(action, {interaction_id:item.id});
        buttons.push(button); row.append(button);
      };
      if (knownAction && ['draft','ready'].includes(item.state)) {
        add(item.action === 'like' ? '执行这条点赞' : '发送这条评论', 'interact');
        card.append(this.node('p', '须先开启对应互动开关并保存；设置开启后，后续浏览也可自动执行该类互动。', 'hint'));
      }
      if (knownAction && ['unknown','completed'].includes(item.state)) add('核验互动结果', 'verify_interaction');
      if (buttons.length) card.append(row);
      return {node:card, buttons, action:item.action};
    }

    renderInteractions(items) {
      const container = this.el('interactions'), retained = new Set();
      const visible = items.slice(-100).reverse().filter(item => object(item) && typeof item.id === 'string' && item.id);
      this.el('interaction-count').textContent = `${visible.length} 条记录`;
      let index = 0;
      for (const item of visible) {
        if (retained.has(item.id)) continue;
        retained.add(item.id);
        const signature = JSON.stringify(item), previous = this.interactionCards.get(item.id);
        let rendered = previous;
        if (!rendered || rendered.signature !== signature) {
          rendered = {...this.interactionCard(item), signature};
          previous?.node.remove(); this.interactionCards.set(item.id, rendered);
        }
        if (container.children[index] !== rendered.node) container.insertBefore(rendered.node, container.children[index] || null);
        index++;
      }
      for (const [id, item] of this.interactionCards) if (!retained.has(id)) { item.node.remove(); this.interactionCards.delete(id); }
      for (const child of [...container.children]) if (![...this.interactionCards.values()].some(item => item.node === child)) child.remove();
      if (!visible.length) container.append(this.node('p', '还没有互动建议。她读到值得回应的内容时，会在这里留下建议。', 'empty'));
    }

    renderDiaries(diaries) {
      const signature = JSON.stringify(diaries);
      if (this.signatures.get('diaries') === signature) return;
      this.signatures.set('diaries', signature);
      const cards = diaries.slice(-30).reverse().filter(object).map(diary => {
        const card = this.node('article');
        card.append(this.node('h3', '阅读日记'), this.node('time', this.date(diary.at)),
          this.node('pre', text(diary.text,6000), 'draft-text'));
        const sources = this.node('ul', undefined, 'sources');
        for (const url of (Array.isArray(diary.source_urls) ? diary.source_urls : []).slice(0,10)) {
          const link = this.sourceLink(url);
          if (link) { const li = this.node('li'); li.append(link); sources.append(li); }
        }
        if (sources.children.length) card.append(sources);
        return card;
      });
      this.el('diary-count').textContent = `${diaries.filter(object).length} 篇记录`;
      this.el('diaries').replaceChildren(...(cards.length ? cards : [this.node('p', '还没有阅读日记。', 'empty')]));
    }

    renderNotes(notes) {
      const signature = JSON.stringify(notes);
      if (this.signatures.get('notes') === signature) return;
      this.signatures.set('notes', signature);
      const cards = notes.slice(-30).reverse().filter(object).map(note => {
        const card = this.node('article');
        const heading = this.node('h3');
        heading.append(this.sourceLink(note.url, text(note.title,300) || '查看这条见闻') || this.node('span', text(note.title,300) || '浏览见闻'));
        card.append(heading, this.node('time', this.date(note.at)), this.node('p', text(note.summary,6000)));
        if (note.reaction) card.append(this.node('p', text(note.reaction,3000), 'note-reaction'));
        const evidenceKind = note.subtitle_truncated && note.evidence_kind === 'full_subtitles' ? 'subtitle_excerpt' : note.evidence_kind;
        card.append(this.node('p', {title:'阅读范围：页面标题', metadata:'阅读范围：页面摘要', summary:'阅读范围：页面摘要',
          page_metadata:'阅读范围：视频简介与页面资料，未观看视频',
          full_subtitles:'阅读范围：已取得的完整字幕轨，未观看视频画面',
          subtitle_excerpt:'阅读范围：字幕轨节选，未读完全部字幕',
          visible_subtitles:'阅读范围：页面当时显示的字幕，未读完全部字幕',
          text:'阅读范围：页面文字', page_text:'阅读范围：页面文字'}[evidenceKind] || '阅读范围以本条页面摘录为准。', 'hint'));
        if (note.subtitle_truncated && evidenceKind !== 'subtitle_excerpt') {
          card.append(this.node('p', '字幕达到本轮长度上限，仅保留部分内容。', 'hint'));
        }
        if (Number.isInteger(note.video_part) && Number.isInteger(note.video_part_count)
            && note.video_part >= 1 && note.video_part <= note.video_part_count && note.video_part_count <= 10000) {
          card.append(this.node('p', `分P范围：P${note.video_part} / 共 ${note.video_part_count}P；字幕仅限此分P。`, 'hint'));
        }
        return card;
      });
      this.el('notes').replaceChildren(...(cards.length ? cards : [this.node('p', '还没有浏览见闻。', 'empty')]));
    }

    renderHistory(history) {
      const signature = JSON.stringify(history);
      if (this.signatures.get('history') === signature) return;
      this.signatures.set('history', signature);
      const names = {account:'读取账号', run:'浏览', browse:'浏览', read:'阅读', draft:'准备草稿', publish:'发布',
        verify:'核验', discard:'丢弃草稿', settings:'保存设置', stop:'停止', diary:'记日记',
        interaction:'互动建议', interact:'执行互动', verify_interaction:'核验互动'};
      const rows = history.slice(-50).reverse().filter(object).map(item => {
        const li = this.node('li');
        li.append(this.node('time', this.date(item.at)), this.node('span', ` · ${names[item.action] || '活动'} · ${text(item.detail,2000)}`));
        return li;
      });
      this.el('history').replaceChildren(...(rows.length ? rows : [this.node('li', '尚无活动记录。', 'empty')]));
    }

    start() {
      this.bridge.start();
      const tick = async () => {
        await this.refresh();
        if (!this.closed) this.timer = setTimeout(tick, 2000);
      };
      tick();
    }

    stop() { this.closed = true; clearTimeout(this.timer); this.bridge.stop(); }
  }

  if (typeof module !== 'undefined') module.exports = {ActivityBridge, ActivityPage, safeURL};
  if (typeof window !== 'undefined' && typeof document !== 'undefined' && document.getElementById('bilibili-activity')) {
    new ActivityPage({documentRef:document, windowRef:window, fetcher:(...args) => fetch(...args)}).start();
  }
})();
