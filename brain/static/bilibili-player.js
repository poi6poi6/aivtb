// Shared by the console and standalone page. Only Bilibili's official player
// receives the selected BV; media URLs are never extracted or downloaded.
class BilibiliDeck {
  static get minAutoPlayCount() { return 200000; }

  constructor({ beforePlay = () => {}, beforeRequest = null, narrate = null, onCancel = null, narrationTimeoutMs = 180000, reviewTimeoutMs = 240000,
    onChange = null, reviewAvailable = () => false, prefix = 'bilibili-', documentRef = document, browserSearch = null } = {}) {
    this.document = documentRef;
    this.browserSearch = browserSearch || (typeof BilibiliBrowserSearch !== 'undefined' ? new BilibiliBrowserSearch() : null);
    this.resultId = null;
    this.directTarget = null;
    this.verifiedResults = new Map();
    this.beforePlay = beforePlay;
    this.beforeRequest = beforeRequest;
    this.narrate = narrate;
    this.onCancel = onCancel;
    this.reviewAvailable = reviewAvailable;
    this.reviewController = null;
    this.reviewTarget = null;
    this.reviewEvidence = null;
    this.reviewState = 'idle';
    this.narrationTimeoutMs = Number.isFinite(narrationTimeoutMs) && narrationTimeoutMs > 0 ? narrationTimeoutMs : 180000;
    this.reviewTimeoutMs = Number.isFinite(reviewTimeoutMs) && reviewTimeoutMs > 0 ? reviewTimeoutMs : 240000;
    this.onChange = onChange;
    this.revision = 0;
    this.selection = 0;
    this.controller = null;
    this.selectionController = null;
    this.narrationController = null;
    this.continueButton = null;
    this.flowQuery = '';
    this.frame = null;
    this.phase = 'idle';
    this.elements = {};
    for (const name of ['panel', 'query', 'form', 'status', 'results', 'player-area', 'player', 'title', 'original', 'stop', 'search', 'close', 'player-status', 'external-search', 'browser-connect', 'browser-status']) {
      this.elements[name] = documentRef.getElementById(prefix + name);
    }
    for (const name of ['panel', 'query', 'form', 'status', 'results', 'player-area', 'player', 'title', 'original', 'stop']) {
      if (!this.elements[name]) throw new Error('Missing Bilibili player element: ' + prefix + name);
    }
    this.resultTools = documentRef.createElement('div');
    this.resultTools.className = 'bilibili-result-tools';
    this.resultTools.hidden = true;
    const searchLine = documentRef.createElement('p');
    this.searchPageLink = documentRef.createElement('a');
    this.searchPageLink.textContent = '查看 B站原始搜索页';
    this.searchPageLink.target = '_blank';
    this.searchPageLink.rel = 'noopener noreferrer';
    const searchHint = documentRef.createElement('span');
    searchHint.textContent = '（在新标签打开搜索原页，可查看封面旁的播放量。）';
    searchLine.append(this.searchPageLink, searchHint);
    this.statisticsDetails = documentRef.createElement('details');
    this.statisticsDetails.hidden = true;
    const summary = documentRef.createElement('summary');
    summary.textContent = '播放量读取诊断';
    const diagnosticHint = documentRef.createElement('p');
    diagnosticHint.textContent = '可复制这些信息用于排查播放量读取问题；不会改变选歌规则。';
    this.statisticsText = documentRef.createElement('textarea');
    this.statisticsText.readOnly = true;
    this.statisticsText.rows = 10;
    this.statisticsText.cols = 60;
    this.statisticsText.ariaLabel = '播放量读取诊断，可选择并复制';
    this.statisticsDetails.append(summary, diagnosticHint, this.statisticsText);
    this.resultTools.append(searchLine, this.statisticsDetails);
    // Keep the recovery tools beside the status, before the result cards.
    if (this.elements.status.insertAdjacentElement) this.elements.status.insertAdjacentElement('afterend', this.resultTools);
    else this.elements.panel.append(this.resultTools);
    this.reviewArea = documentRef.createElement('section');
    this.reviewArea.className = 'bilibili-review';
    this.reviewArea.hidden = true;
    const reviewHeading = documentRef.createElement('h3');
    reviewHeading.textContent = '听她聊聊这个视频';
    this.reviewStatus = documentRef.createElement('p');
    this.reviewStatus.role = 'status';
    const reviewHint = documentRef.createElement('p');
    reviewHint.className = 'bilibili-review-hint';
    reviewHint.textContent = '先读取所选分P的字幕。看完后点“听她评价”，会停止本页视频声音，再由她说感想；不会自动判断视频已播完。';
    this.reviewRead = documentRef.createElement('button');
    this.reviewRead.type = 'button'; this.reviewRead.textContent = '读取字幕';
    this.reviewRead.onclick = () => this.readForReview();
    this.reviewSpeak = documentRef.createElement('button');
    this.reviewSpeak.type = 'button'; this.reviewSpeak.textContent = '听她评价';
    this.reviewSpeak.onclick = () => this.review();
    this.reviewArea.append(reviewHeading, this.reviewStatus, reviewHint, this.reviewRead, this.reviewSpeak);
    this.elements['player-area'].append(this.reviewArea);
    this.elements.form.onsubmit = event => { event.preventDefault(); this.request(this.elements.query.value); };
    this.elements.query.oninput = () => this.updateExternalSearch(this.elements.query.value);
    this.elements.stop.onclick = () => this.stop();
    if (this.elements.close) this.elements.close.onclick = () => { this.stop(); this.elements.panel.hidden = true; };
    this.elements.stop.disabled = true;
    if (this.elements['browser-connect']) this.elements['browser-connect'].onclick = async () => {
      const button = this.elements['browser-connect'];
      button.disabled = true;
      this.browserStatus('正在连接浏览器助手…');
      try {
        if (!this.browserSearch) throw BilibiliDeck.searchError('请先安装浏览器助手，再刷新页面连接。', 'browser_required');
        await this.browserSearch.connect();
        this.browserStatus(this.browserConnectedStatus());
      } catch (error) { this.browserStatus((error.message || '浏览器助手连接失败，请按安装说明检查。') + BilibiliDeck.diagnosticText(error)); }
      finally { button.disabled = false; }
    };
  }

  browserConnectedStatus() {
    const version = this.browserSearch?.extensionVersion;
    return typeof version === 'string' && /^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(version)
      ? '浏览器助手已连接 · 版本 ' + version
      : '浏览器助手已连接，但这是旧版助手。请更新并重新加载扩展，再刷新控制台并重连。';
  }

  browserStatus(message) { if (this.elements['browser-status']) this.elements['browser-status'].textContent = message; }

  reviewEnabled() { return !!this.narrate && this.reviewAvailable() === true; }

  refreshReview() {
    const selected = !!this.reviewTarget && this.phase === 'opened';
    this.reviewArea.hidden = !selected || !this.narrate;
    const enabled = selected && this.reviewEnabled();
    this.reviewRead.disabled = !enabled || ['reading','speaking'].includes(this.reviewState);
    this.reviewSpeak.disabled = !enabled || this.reviewState !== 'ready' || !this.reviewEvidence;
    this.reviewRead.textContent = this.reviewState === 'reading' ? '正在读取字幕…' : this.reviewEvidence ? '重新读取字幕' : '读取字幕';
    this.reviewSpeak.textContent = this.reviewState === 'speaking' ? '正在听她评价…' : this.reviewState === 'completed' ? '评价已播完' : '听她评价';
    if (selected && !this.reviewEnabled()) this.reviewStatus.textContent = '当前主服务尚未启用字幕评价，请更新主服务后刷新控制台。';
  }

  resetReview() {
    this.reviewController?.abort();
    this.reviewController = null;
    this.reviewTarget = null;
    this.reviewEvidence = null;
    this.reviewState = 'idle';
    this.reviewStatus.textContent = '';
    this.reviewArea.hidden = true;
    this.reviewRead.disabled = true;
    this.reviewSpeak.disabled = true;
  }

  async readForReview() {
    const target = this.reviewTarget;
    if (!target || this.phase !== 'opened' || !this.reviewEnabled() || ['reading','speaking'].includes(this.reviewState)) return false;
    this.reviewController?.abort();
    const controller = this.reviewController = new AbortController();
    this.reviewEvidence = null;
    this.reviewState = 'reading';
    this.reviewStatus.textContent = '正在读取 P' + target.page + ' 的可用字幕，视频可继续播放…';
    this.refreshReview();
    const current = () => this.reviewTarget === target && this.reviewController === controller && !controller.signal.aborted;
    try {
      if (typeof this.browserSearch?.readVideo !== 'function') throw new Error('请更新浏览器助手及本机页面后，再读取字幕。');
      const result = await this.waitFor(() => this.browserSearch.readVideo(target.query, {signal:controller.signal}), controller.signal);
      if (!current()) return false;
      if (!result || result.bvid !== target.item.bvid || result.part !== target.page ||
          typeof result.evidence_id !== 'string' || !result.evidence_id || !Number.isFinite(result.expires_at)) {
        throw new Error('未能确认字幕属于当前视频和分P，请重新读取。');
      }
      if (result.subtitle_status !== 'ready' || !['full_subtitles','subtitle_excerpt'].includes(result.evidence_kind)) {
        this.reviewState = 'unavailable';
        this.reviewStatus.textContent = (typeof result.subtitle_reason === 'string' && result.subtitle_reason.trim()
          ? result.subtitle_reason.slice(0,300) : '这个分P暂时没有可读取的字幕。') + ' 她还不能据此评价视频内容。';
        return false;
      }
      this.reviewEvidence = result;
      this.reviewState = 'ready';
      const partial = result.subtitle_truncated === true || result.evidence_kind === 'subtitle_excerpt';
      this.reviewStatus.textContent = '已读取 P' + target.page + (partial ? ' 的部分字幕' : ' 的可用字幕')
        + (typeof result.subtitle_language === 'string' && result.subtitle_language ? ' · ' + result.subtitle_language.slice(0,40) : '')
        + '。看完后可以听她' + (partial ? '就这部分内容' : '结合字幕') + '评价；未读取视频画面和声音。';
      return true;
    } catch (error) {
      if (!current()) return false;
      this.reviewState = 'failed';
      this.reviewStatus.textContent = typeof error?.message === 'string' && error.message.trim()
        ? error.message.slice(0,300) : '字幕读取未完成，请确认浏览器助手已连接后重试。';
      return false;
    } finally { if (current()) this.refreshReview(); }
  }

  async review() {
    const target = this.reviewTarget, evidence = this.reviewEvidence;
    if (!target || !evidence || this.reviewState !== 'ready' || this.phase !== 'opened' || !this.reviewEnabled()) return false;
    if (evidence.expires_at <= Date.now() / 1000) {
      this.reviewState = 'expired';
      this.reviewStatus.textContent = '这份字幕快照已过期，请重新读取后再听她评价。';
      this.refreshReview();
      return false;
    }
    const controller = this.selectionController;
    if (!controller || controller.signal.aborted) return false;
    // Cross-origin embeds expose no dependable pause/ended receipt. Removing
    // this exact frame stops its audio; it does not claim the video was watched.
    this.detachFrame();
    this.reviewState = 'speaking';
    this.reviewStatus.textContent = '本页视频已停止，正在请她根据字幕评价…';
    this.elements.status.textContent = '本页视频已停止，等待助手评价；需要继续看时可重新播放或打开 B站原页。';
    if (this.elements['player-status']) this.elements['player-status'].textContent = this.elements.status.textContent;
    this.refreshReview();
    const completed = await this.say('review', controller.signal, target.item, undefined, undefined,
      {query:target.query, evidence_id:evidence.evidence_id});
    if (this.reviewTarget !== target || controller.signal.aborted || this.selectionController !== controller) return false;
    this.reviewState = completed ? 'completed' : 'ready';
    this.reviewStatus.textContent = completed ? '这次字幕评价已播完。可以继续聊天，或重新选择视频。'
      : this.narrationFailure || '这次评价没有完整播出；字幕仍保留，可再点一次重试。';
    this.refreshReview();
    return completed;
  }

  get active() { return ['ack', 'querying', 'preparing', 'narrating', 'results', 'awaiting-continue', 'opened'].includes(this.phase); }

  // Abort releases our waiter even if an integration callback ignores its signal.
  // Its eventual resolution is still consumed, but can never resume this flow.
  async waitFor(callback, signal) {
    if (signal.aborted) return false;
    let abort;
    const cancelled = new Promise(resolve => {
      abort = () => resolve(false);
      signal.addEventListener('abort', abort, {once:true});
    });
    try { return await Promise.race([Promise.resolve().then(() => signal.aborted ? false : callback()), cancelled]); }
    finally { signal.removeEventListener('abort', abort); }
  }

  async say(stage, parentSignal, item, error, selection, review = null) {
    if (!this.narrate) return true;
    this.narrationController?.abort();
    const controller = new AbortController();
    this.narrationController = controller;
    this.narrationFailure = '';
    const abort = () => controller.abort();
    if (parentSignal.aborted) controller.abort();
    else parentSignal.addEventListener('abort', abort, {once:true});
    const timer = setTimeout(abort, stage === 'review' ? this.reviewTimeoutMs : this.narrationTimeoutMs);
    try {
      const completed = await this.waitFor(() => this.narrate({stage, query:review?.query || this.flowQuery, item, error, signal:controller.signal,
        ...(stage === 'review' && review ? {evidence_id:review.evidence_id} : {}),
        ...(['found','results'].includes(stage) ? {result_id:this.resultId} : {}),
        ...(selection === 'highest_play' ? {selection} : {})}), controller.signal);
      if (completed !== true) controller.abort();
      return completed === true && !controller.signal.aborted;
    } catch (failure) {
      if (['voice_resource_unavailable','direct_narration_unavailable','review_narration_unavailable'].includes(failure?.code) && typeof failure.message === 'string'
          && this.narrationController === controller && !parentSignal.aborted) {
        this.narrationFailure = failure.message.slice(0,500);
        this.elements.status.textContent = this.narrationFailure;
      }
      controller.abort();
      return false;
    } finally {
      clearTimeout(timer);
      parentSignal.removeEventListener('abort', abort);
      if (this.narrationController === controller) this.narrationController = null;
    }
  }

  offerContinue(message, label, revision, selection, callback, container = this.elements.player) {
    this.setPhase('awaiting-continue');
    this.elements.status.textContent = this.narrationFailure
      ? this.narrationFailure + ' 当前步骤已暂停；处理后重新点歌，或手动继续。' : message;
    const button = this.document.createElement('button');
    button.type = 'button'; button.className = 'bilibili-continue'; button.textContent = label;
    this.continueButton = button;
    button.onclick = () => {
      if (revision !== this.revision || selection !== this.selection || this.continueButton !== button) return false;
      this.continueButton = null;
      button.disabled = true;
      container.replaceChildren();
      return callback();
    };
    container.append(button);
  }

  static parse(query) {
    const text = String(query ?? '').trim();
    if (!text) throw new Error('请输入歌名、歌手、BV 号或 B 站视频链接。');
    if (/^BV[0-9A-Za-z]{10}$/i.test(text)) {
      const bvid = 'BV' + text.slice(2);
      return { query: bvid, bvid, page: 1 };
    }
    const reference = /^(?:(?:www|m)\.)?bilibili\.com(?:[/:?#]|$)/i.test(text)
      ? 'https://' + text : text.startsWith('//') ? 'https:' + text : text;
    if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(reference)) {
      let url;
      try { url = new URL(reference); } catch (_) { throw new Error('请输入完整的 B 站官方视频链接。'); }
      const match = url.pathname.match(/^\/video\/(BV[0-9A-Za-z]{10})\/?$/i);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.port ||
          !['www.bilibili.com', 'bilibili.com', 'm.bilibili.com'].includes(url.hostname)) {
        throw new Error('仅支持 B 站官方 /video/BV 视频链接，或直接输入 BV 号。');
      }
      if (/^\/video\/av\d+\/?$/i.test(url.pathname)) throw new Error('暂不支持 AV 视频链接，请提供对应的 BV 号或 BV 链接。');
      if (!match) throw new Error('仅支持 B 站官方 /video/BV 视频链接，或直接输入 BV 号。');
      const pages = url.searchParams.getAll('p');
      if (pages.length > 1 || (pages.length && (!/^[1-9]\d{0,4}$/.test(pages[0]) || Number(pages[0]) > 10000))) {
        throw new Error('视频分 P 参数无效，请使用正整数 p。');
      }
      const bvid = 'BV' + match[1].slice(2);
      const page = pages.length ? Number(pages[0]) : 1;
      return { query: page > 1 ? BilibiliDeck.videoURL(bvid, page) : bvid, bvid, page };
    }
    if (/^BV[0-9A-Za-z]*$/i.test(text)) throw new Error('BV 号格式不正确，应为 BV 开头的 12 位编号。');
    return { query: text.slice(0, 100), bvid: null, page: 1 };
  }

  static videoURL(bvid, page = 1) {
    return 'https://www.bilibili.com/video/' + bvid + (page > 1 ? '?p=' + page : '');
  }

  static searchURL(query) {
    return 'https://search.bilibili.com/all?keyword=' + encodeURIComponent(String(query).trim()) + '&order=click';
  }

  static statisticsDiagnostic(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || value.version !== 1 || !Array.isArray(value.cards)) return '';
    try {
      const json = JSON.stringify(value);
      if (json.length > 6144) return '';
      let bytes = 0;
      for (const char of json) {
        const code = char.codePointAt(0);
        bytes += code <= 0x7f ? 1 : code <= 0x7ff ? 2 : code <= 0xffff ? 3 : 4;
        if (bytes > 6144) return '';
      }
      return JSON.stringify(value, null, 2);
    } catch (_) { return ''; }
  }

  static diagnosticLabel(stage) {
    const labels = {handshake:'连接浏览器助手',request_registration:'登记查询',helper_response:'等待助手响应',save_result:'确认搜索结果',
      session_load:'读取助手会话',connect_save:'保存助手连接',job_prepare:'准备浏览器查询',request_dispatch:'分发助手请求',random_id:'创建查询标识',timer_setup:'设置查询计时',tab_save:'保存助手标签页',tab_create:'创建B站标签页',tab_mute:'设置标签页静音',
      tab_reset:'准备B站标签页',tab_navigate:'打开B站页面',page_check:'检查B站页面',read_page:'读取B站页面',result_shape:'检查页面结果'};
    return Object.hasOwn(labels,stage) ? labels[stage] : '';
  }

  static diagnosticReason(value) {
    const labels = {chrome_api_error:'浏览器操作失败',storage_error:'助手设置未能保存',tab_closed:'标签页已关闭',
      tab_busy:'标签页仍在处理上一项操作',permission_denied:'浏览器未授予权限',page_changed:'页面已切换',
      invalid_result:'页面结果无法确认',blocked_page:'页面访问受限',unexpected_error:'助手操作未完成',window_unavailable:'没有可用的浏览器窗口',deadline:'等待页面结果超时',secure_random_unavailable:'浏览器安全随机功能不可用',timer_unavailable:'浏览器计时功能不可用'};
    return Object.hasOwn(labels,value) ? labels[value] : '';
  }

  static diagnosticText(error) {
    const stage = BilibiliDeck.diagnosticLabel(error.stage) || BilibiliDeck.diagnosticLabel(error.diagnostic_code);
    const reason = BilibiliDeck.diagnosticReason(error.diagnostic_reason);
    return stage ? '（阶段：' + stage + (reason ? '；' + reason : '') + '）' : '';
  }

  static searchErrorCode(code) {
    const allowed = ['network', 'timeout', 'rate_limited', 'upstream_rejected', 'invalid_response', 'service', 'browser_required', 'verification_required', 'login_required', 'page_unavailable', 'browser_disconnected'];
    return allowed.includes(code) ? code : 'service';
  }

  static searchError(message, code = 'service') {
    return Object.assign(new Error(message), {code:BilibiliDeck.searchErrorCode(code)});
  }

  static result(item) {
    if (!item || !/^BV[0-9A-Za-z]{10}$/.test(item.bvid || '')) return null;
    let cover = '';
    try {
      const raw = String(item.cover || '');
      const url = new URL(raw.startsWith('//') ? 'https:' + raw : raw);
      if (['http:', 'https:'].includes(url.protocol) && url.hostname.endsWith('.hdslb.com') && !url.username && !url.password && !url.port) {
        url.protocol = 'https:';
        cover = url.href;
      }
    } catch (_) { /* An absent or invalid thumbnail does not invalidate a video. */ }
    const playCount = Number.isSafeInteger(item.play_count) && item.play_count >= 0 ? item.play_count : null;
    return { bvid: item.bvid, title: String(item.title || item.bvid).slice(0, 250), author: String(item.author || '').slice(0, 100),
      duration: String(item.duration || '').slice(0, 20), cover, play_count:playCount,
      play_text:typeof item.play_text === 'string' ? item.play_text.slice(0,80) : '',
      play_count_exact:item.play_count_exact === true,
      play_count_lower:Number.isSafeInteger(item.play_count_lower) && item.play_count_lower >= 0 ? item.play_count_lower : null,
      play_count_upper:Number.isSafeInteger(item.play_count_upper) && item.play_count_upper >= 0 ? item.play_count_upper : null };
  }

  setPhase(phase) {
    this.phase = phase;
    this.elements.stop.disabled = !this.active;
    this.refreshReview();
    if (this.onChange) {
      try { this.onChange({ active: this.active, phase }); }
      catch (error) { console.warn('Bilibili state observer failed', error); }
    }
  }

  updateExternalSearch(query) {
    if (this.elements['external-search']) this.elements['external-search'].href = BilibiliDeck.searchURL(query);
  }

  showSearchTools(query, results, diagnostic) {
    this.searchPageLink.href = BilibiliDeck.searchURL(query);
    this.resultTools.hidden = false;
    const text = results.some(item => item.play_count === null) ? BilibiliDeck.statisticsDiagnostic(diagnostic) : '';
    this.statisticsText.value = text;
    this.statisticsDetails.hidden = !text;
    this.statisticsDetails.open = false;
  }

  detachFrame() {
    if (this.frame) { this.frame.onload = null; this.frame.onerror = null; }
    this.frame = null;
    this.continueButton = null;
    this.elements.player.replaceChildren();
  }

  cancel() {
    const hadFlow = !!this.controller;
    this.revision += 1;
    this.selection += 1;
    if (this.controller) this.controller.abort();
    this.selectionController?.abort();
    this.narrationController?.abort();
    this.controller = null;
    this.selectionController = null;
    this.narrationController = null;
    this.flowQuery = '';
    this.narrationFailure = '';
    this.resultId = null;
    this.directTarget = null;
    this.verifiedResults.clear();
    this.resetReview();
    this.detachFrame();
    this.resultTools.hidden = true;
    this.searchPageLink.removeAttribute('href');
    this.statisticsDetails.hidden = true;
    this.statisticsDetails.open = false;
    this.statisticsText.value = '';
    this.elements.results.replaceChildren();
    this.elements['player-area'].hidden = true;
    this.elements.title.textContent = '';
    this.elements.original.removeAttribute('href');
    if (this.elements['player-status']) this.elements['player-status'].textContent = '';
    if (this.elements.search) this.elements.search.disabled = false;
    if (hadFlow && this.onCancel) {
      try { Promise.resolve(this.onCancel()).catch(error => console.warn('Bilibili cancellation observer failed', error)); }
      catch (error) { console.warn('Bilibili cancellation observer failed', error); }
    }
  }

  stop(message = '播放器已停止，可以继续聊天或重新选择视频。') {
    this.cancel();
    this.setPhase('idle');
    this.elements.status.textContent = message;
  }

  showOriginal(bvid, page, title = bvid) {
    this.elements['player-area'].hidden = false;
    this.elements.title.textContent = title;
    this.elements.original.href = BilibiliDeck.videoURL(bvid, page);
  }

  async request(query, {skipAck = false} = {}) {
    this.cancel();
    this.elements.panel.hidden = false;
    let parsed;
    try { parsed = BilibiliDeck.parse(query); }
    catch (error) { this.setPhase('error'); this.elements.status.textContent = error.message; return false; }
    const revision = this.revision;
    this.elements.query.value = String(query).trim();
    this.updateExternalSearch(parsed.query);
    const controller = new AbortController();
    this.controller = controller;
    this.flowQuery = parsed.query;
    if (parsed.bvid) {
      this.directTarget = {bvid:parsed.bvid,page:parsed.page,
        item:BilibiliDeck.result({bvid:parsed.bvid,title:'指定视频 · ' + parsed.bvid})};
      this.browserStatus('指定 BV 视频直接使用官方播放器；按歌名搜索时才需要浏览器助手。');
    }
    if (this.narrate || this.beforeRequest) this.setPhase('ack');
    if (this.elements.search) this.elements.search.disabled = true;
    try {
      let ready = true;
      if (this.beforeRequest) {
        try { ready = await this.waitFor(() => this.beforeRequest({query:parsed.query, signal:controller.signal}), controller.signal) !== false; }
        catch (_) { ready = false; }
        if (revision !== this.revision || controller.signal.aborted) return false;
      }
      if (ready && this.narrate && !skipAck) {
        this.elements.status.textContent = parsed.bvid ? '等助手回应后，准备播放指定视频…' : '等助手回应后，开始查询 B 站…';
        ready = await this.say('ack', controller.signal);
        if (revision !== this.revision || controller.signal.aborted) return false;
      }
      if (!ready) {
        this.offerContinue(parsed.bvid ? '角色回应未完成，尚未打开指定视频；可以手动继续准备播放。'
          : '角色语音未完成或暂不可用，尚未开始查询；可以手动继续。若刚更新功能，请重启主程序后刷新页面。',
          parsed.bvid ? '跳过回应，继续准备播放' : '跳过回应，继续查询', revision, this.selection,
          () => this.request(query, {skipAck:true}), this.elements.results);
        return false;
      }
      if (parsed.bvid) return await this.select(this.directTarget.item, revision, parsed.page);
      this.setPhase('querying');
      this.elements.status.textContent = '正在查询 B 站实际搜索结果…';
      if (!this.browserSearch) throw BilibiliDeck.searchError('未连接浏览器助手。请先安装助手，再点击“连接浏览器助手”。', 'browser_required');
      const data = await this.browserSearch.search(parsed.query, {signal:controller.signal});
      if (revision !== this.revision || controller.signal.aborted) return false;
      if (!data || !Array.isArray(data.results) || typeof data.result_id !== 'string' || !data.result_id || data.query !== parsed.query) {
        throw BilibiliDeck.searchError('浏览器查询未返回有效结果，请重新连接助手后重试。', 'invalid_response');
      }
      this.resultId = data.result_id;
      this.browserStatus(this.browserConnectedStatus());
      const seen = new Set();
      const results = data.results.map(BilibiliDeck.result).filter(item => item && !seen.has(item.bvid) && seen.add(item.bvid));
      this.verifiedResults = new Map(results.map(item => [item.bvid,item]));
      if (!results.length) throw Object.assign(new Error('没有找到匹配的视频，请换个关键词或前往 B 站搜索。'), {code:'empty'});
      const choices = results.slice(0, 12);
      this.renderResults(choices, revision);
      this.showSearchTools(parsed.query, results, data.stats_diagnostic);
      const automatic = choices.find(item => item.bvid === data.auto_bvid);
      if (automatic && data.selection_reason === 'highest_play') {
        return await this.select(automatic, revision, 1, {selection:'highest_play'});
      }
      const selection = this.selection;
      this.setPhase(this.narrate ? 'narrating' : 'results');
      const reason = ['below_play_threshold','uncertain_play_count','missing_play_count'].includes(data.selection_reason)
        ? data.selection_reason : 'uncertain_play_count';
      // Count the full verified snapshot, including rows beyond the twelve
      // visible cards. Missing statistics are not missing search results.
      const known = results.filter(item => item.play_count !== null).length;
      const missing = results.length - known;
      const total = '已找到 ' + results.length + ' 个实际结果'
        + (choices.length < results.length ? '，当前展示前 ' + choices.length + ' 个。' : '。');
      const guidance = '可先手动选择：点击下方结果卡片的“播放这个版本”。';
      let explanation;
      if (known === 0) {
        const version = this.browserSearch?.extensionVersion;
        const parts = typeof version === 'string' && /^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(version)
          ? version.split('.').map(Number) : null;
        const recent = parts && (parts[0] > 1 || (parts[0] === 1 && (parts[1] > 0 || (parts[1] === 0 && parts[2] >= 3))));
        explanation = '但未读到任何结果的播放量，所以没自动播放。' + guidance
          + ' 可打开下方“查看 B站原始搜索页”，核对封面旁的播放量。'
          + (recent ? ' 助手已是 ' + version + '，当前页面统计仍未读到。'
            : ' 如需更新助手，可按安装说明更新至 1.0.4 或更新版本，重新加载扩展，再刷新页面并重连；更新不保证补齐当前页面统计。')
          + (!this.statisticsDetails.hidden ? ' 也可展开“播放量读取诊断”，复制信息协助排查。' : '');
      } else if (missing > 0) {
        explanation = '其中 ' + known + ' 个已读到播放量，' + missing + ' 个缺少播放量，暂时无法确认最高版本，所以没自动播放。' + guidance;
      } else if (reason === 'below_play_threshold') {
        explanation = '这些结果的播放量已读到，但未满足自动播放条件，未自动播放。' + guidance;
      } else if (reason === 'uncertain_play_count') {
        explanation = '页面播放量是近似值，暂时无法确认哪个版本最高，未自动播放。' + guidance;
      } else {
        explanation = '暂时无法确认合适的自动播放版本，未自动播放。' + guidance;
      }
      this.elements.status.textContent = total + explanation;
      if (this.narrate) {
        await this.say('results', controller.signal, undefined, reason);
        if (revision !== this.revision || selection !== this.selection || controller.signal.aborted) return false;
        this.setPhase('results');
      }
      return true;
    } catch (error) {
      if (revision !== this.revision || controller.signal.aborted) return false;
      const selection = this.selection;
      this.setPhase(this.narrate ? 'narrating' : 'error');
      this.elements.status.textContent = error instanceof TypeError
        ? '无法连接查询服务，请确认项目已启动、网络连接正常后重试。'
        : (error.message || 'B 站查询失败，请稍后重试。');
      const diagnostic = BilibiliDeck.diagnosticText(error);
      this.elements.status.textContent += diagnostic;
      if (!diagnostic && this.browserSearch?.connected === true && this.browserSearch.extensionVersion === null) {
        this.elements.status.textContent += ' 旧版助手未提供失败步骤，请更新并重新加载扩展，再刷新控制台并重连。';
      }
      if (['browser_required','browser_disconnected','verification_required','login_required','page_unavailable'].includes(error.code)) this.browserStatus(this.elements.status.textContent);
      if (parsed.bvid) this.showOriginal(parsed.bvid, parsed.page);
      if (this.narrate) {
        const code = error.code === 'empty' ? 'empty'
          : BilibiliDeck.searchErrorCode(error.code || (error instanceof TypeError ? 'network' : 'service'));
        await this.say('error', controller.signal, undefined, code);
        if (revision !== this.revision || selection !== this.selection || controller.signal.aborted) return false;
        this.setPhase('error');
      }
      return false;
    } finally {
      if (revision === this.revision) {
        if (this.elements.search) this.elements.search.disabled = false;
      }
    }
  }

  renderResults(results, revision, page = 1) {
    for (const item of results) {
      const card = this.document.createElement('article');
      if (item.cover) {
        const img = this.document.createElement('img');
        img.src = item.cover; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; card.append(img);
      }
      const title = this.document.createElement('h2');
      title.textContent = item.title;
      const details = this.document.createElement('p');
      const plays = item.play_text ? '播放量 ' + item.play_text : item.play_count === null ? '播放量暂不可用'
        : '播放量 ' + (item.play_count_exact ? '' : '约 ') + item.play_count.toLocaleString('zh-CN');
      details.textContent = [item.author, item.duration, plays, item.bvid].filter(Boolean).join(' · ');
      const play = this.document.createElement('button');
      play.type = 'button'; play.textContent = '播放这个版本';
      play.onclick = () => this.select(item, revision, page);
      const original = this.document.createElement('a');
      original.textContent = 'B 站原页'; original.href = BilibiliDeck.videoURL(item.bvid, page);
      original.target = '_blank'; original.rel = 'noopener noreferrer';
      card.append(title, details, play, original);
      this.elements.results.append(card);
    }
  }

  async select(item, revision = this.revision, page = 1, {skipNarration = false, selection:selectionReason} = {}) {
    const direct = !!this.directTarget && this.directTarget.bvid === item?.bvid && this.directTarget.page === page;
    if (this.directTarget && !direct) return false;
    item = direct ? this.directTarget.item : this.verifiedResults.get(item?.bvid);
    if (!item || revision !== this.revision || !this.controller || this.controller.signal.aborted || !Number.isInteger(page) || page < 1 || page > 10000) return false;
    const selection = ++this.selection;
    this.resetReview();
    this.selectionController?.abort();
    this.narrationController?.abort();
    const controller = new AbortController();
    this.selectionController = controller;
    this.detachFrame();
    this.setPhase('preparing');
    this.showOriginal(item.bvid, page, item.title);
    this.elements.status.textContent = '正在打开所选视频的官方播放器…';
    try {
      const allowed = await this.waitFor(() => this.beforePlay(item), controller.signal);
      if (revision !== this.revision || selection !== this.selection || controller.signal.aborted) return false;
      if (allowed === false) { this.stop('播放已取消。'); return false; }
      if (this.narrate && !skipNarration) {
        this.setPhase('narrating');
        this.elements.status.textContent = direct ? '等助手说完，再打开你指定的官方视频…' : '已确认所选视频，等助手介绍完再打开播放器…';
        const completed = await this.say(direct ? 'direct' : 'found', controller.signal, item, undefined, selectionReason);
        if (revision !== this.revision || selection !== this.selection || controller.signal.aborted) return false;
        if (!completed) {
          this.offerContinue('角色语音未完成或暂不可用，尚未打开视频；可以手动跳过介绍。若刚更新功能，请重启主程序后刷新页面。', '语音没播完，点击继续播放', revision, selection,
            () => this.select(item, revision, page, {skipNarration:true}));
          return false;
        }
      }
      const frame = this.document.createElement('iframe');
      frame.title = item.title;
      frame.allow = 'autoplay; fullscreen; encrypted-media; picture-in-picture';
      frame.allowFullscreen = true;
      frame.src = 'https://player.bilibili.com/player.html?' + new URLSearchParams({ bvid: item.bvid, p: String(page), autoplay: '1', muted: '0', danmaku: '0' });
      const notify = text => {
        if (revision !== this.revision || selection !== this.selection || this.frame !== frame) return;
        this.elements.status.textContent = text;
        if (this.elements['player-status']) this.elements['player-status'].textContent = text;
      };
      const opened = '官方播放器已打开，已请求自动播放；若浏览器限制，请点击播放器中的播放按钮。';
      frame.onload = () => notify(opened);
      frame.onerror = () => notify('官方播放器加载失败，请重试或使用 B 站原页链接。');
      this.frame = frame;
      this.elements.player.append(frame);
      this.reviewTarget = {item, page, query:page > 1 ? BilibiliDeck.videoURL(item.bvid,page) : item.bvid};
      this.setPhase('opened');
      notify(opened);
      if (this.reviewEnabled()) void this.readForReview();
      this.elements['player-area'].scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
      return true;
    } catch (error) {
      if (revision !== this.revision || selection !== this.selection) return false;
      this.setPhase('error');
      this.elements.status.textContent = '播放准备失败，请重试或使用 B 站原页链接。';
      return false;
    }
  }
}

if (typeof module !== 'undefined') module.exports = BilibiliDeck;
if (typeof window !== 'undefined') window.BilibiliDeck = BilibiliDeck;
