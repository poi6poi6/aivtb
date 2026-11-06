// The helper reads visible Bilibili pages in the user's browser. The local
// server validates those rows and owns the short-lived selection capability.
class BilibiliBrowserSearch {
  constructor({windowRef = window, fetcher = (...args) => fetch(...args),
    connectTimeoutMs = 2500, searchTimeoutMs = 35000, requestTimeoutMs = 30000} = {}) {
    this.window = windowRef;
    this.fetcher = fetcher;
    this.origin = windowRef.location.origin;
    this.connectTimeoutMs = connectTimeoutMs;
    this.searchTimeoutMs = searchTimeoutMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.connected = false;
    this.extensionVersion = null;
    this.capabilities = [];
    this.pending = new Map();
    this.sequence = 0;
    this.flow = null;
    this.readFlow = null;
    this.receive = event => {
      const message = event.data;
      if (event.source !== this.window || event.origin !== this.origin ||
          message?.source !== 'ai-vtuber-extension' || message.type !== 'bilibili-browser-response') return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      if (message.ok === true) pending.resolve(message.data);
      else pending.reject(BilibiliBrowserSearch.error(message.error?.code, message.error?.message,
        message.error?.stage, message.error?.diagnostic_code, message.error?.reason));
    };
    windowRef.addEventListener('message', this.receive);
    windowRef.addEventListener('pagehide', () => {this.cancel();this.cancelRead();});
  }

  static diagnosticStage(...values) {
    const allowed = ['handshake','request_registration','helper_response','save_result',
      'session_load','connect_save','job_prepare','request_dispatch','random_id','timer_setup','tab_save','tab_create','tab_mute','tab_reset','tab_navigate',
      'page_check','read_page','result_shape'];
    return values.find(value => allowed.includes(value)) || null;
  }

  static diagnosticReason(value) {
    const allowed = ['chrome_api_error','storage_error','tab_closed','tab_busy','permission_denied',
      'page_changed','invalid_result','blocked_page','unexpected_error','window_unavailable','deadline','secure_random_unavailable','timer_unavailable'];
    return allowed.includes(value) ? value : null;
  }

  static error(code, message, stage, diagnosticCode, reason) {
    if (['not_connected','extension_unavailable'].includes(code)) code = 'browser_disconnected';
    const messages = {
      browser_required:'未连接浏览器助手。请先安装助手，再点击“连接浏览器助手”。',
      browser_disconnected:'浏览器助手已断开，请重新连接后再试。',
      verification_required:'B站页面需要验证，请在打开的 B站标签页完成验证后重试。',
      login_required:'B站页面需要登录，请在打开的 B站标签页登录后重试。',
      page_unavailable:'未能读取 B站页面，请检查打开的标签页后重试。',
      timeout:'等待浏览器助手超时，请检查 B站标签页后重试。',
      network:'无法连接本地主服务，请检查连接后重试。',
      invalid_response:'浏览器查询返回的数据无效，请重新查询。',
      extension_upgrade_required:'读取点播字幕需要浏览器助手 1.3.0，请重新加载新版助手再连接。',
    };
    const safeCode = Object.hasOwn(messages, code) ? code : 'page_unavailable';
    return Object.assign(new Error(typeof message === 'string' && message.trim()
      ? message.slice(0, 300) : messages[safeCode]), {code:safeCode,
        stage:BilibiliBrowserSearch.diagnosticStage(stage, diagnosticCode),
        diagnostic_reason:BilibiliBrowserSearch.diagnosticReason(reason)});
  }

  post(id, action, fields = {}) {
    this.window.postMessage({source:'ai-vtuber', type:'bilibili-browser-request', id, action, ...fields}, this.origin);
  }

  exchange(id, action, fields, signal, timeout) {
    return new Promise((resolve, reject) => {
      let timer;
      const finish = (callback, value) => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
        this.pending.delete(id);
        callback(value);
      };
      const abort = () => {
        this.post(id, 'cancel');
        finish(reject, Object.assign(new Error('Cancelled'), {name:'AbortError'}));
      };
      if (signal?.aborted) { abort(); return; }
      this.pending.set(id, {resolve:value => finish(resolve, value), reject:error => finish(reject, error)});
      signal?.addEventListener('abort', abort, {once:true});
      timer = setTimeout(() => {
        this.post(id, 'cancel');
        const code = action === 'connect' ? (this.connected ? 'browser_disconnected' : 'browser_required') : 'timeout';
        this.connected = false;
        finish(reject, BilibiliBrowserSearch.error(code, undefined, action === 'connect' ? 'handshake' : 'helper_response'));
      }, timeout);
      try { this.post(id, action, fields); }
      catch (_) { finish(reject, BilibiliBrowserSearch.error('browser_disconnected', undefined, 'handshake')); }
    });
  }

  async connect({signal} = {}) {
    const id = 'bili-connect-' + Date.now().toString(36) + '-' + (++this.sequence);
    try {
      const data = await this.exchange(id, 'connect', {}, signal, this.connectTimeoutMs);
      if (data?.connected !== true || data.version !== 1) throw BilibiliBrowserSearch.error('browser_required', undefined, 'handshake');
      this.connected = true;
      this.extensionVersion = typeof data.extension_version === 'string' && /^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(data.extension_version)
        ? data.extension_version : null;
      this.capabilities = Array.isArray(data.capabilities) ? data.capabilities.filter(value=>value==='video_subtitles_v1') : [];
      return true;
    } catch (error) { this.connected = false; throw error; }
  }

  async json(path, body, flow) {
    const stage = path.endsWith('/request') ? 'request_registration' : 'save_result';
    const controller = new AbortController();
    let timer, expired = false;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        expired = true;
        controller.abort();
        reject(BilibiliBrowserSearch.error('timeout', '本地主服务响应超时，请重试。', stage));
      }, this.requestTimeoutMs);
    });
    const work = async () => {
      let response, data;
      try {
        response = await this.fetcher(path, {method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), cache:'no-store', signal:controller.signal});
      } catch (_) { throw BilibiliBrowserSearch.error(expired ? 'timeout' : 'network', undefined, stage); }
      try { data = await response.json(); }
      catch (_) { throw BilibiliBrowserSearch.error('invalid_response', undefined, stage); }
      // Preserve cleanup even when a transport ignores abort and resolves after
      // our deadline or the user has already switched to a different song.
      if (flow && data && typeof data === 'object') {
        if (typeof data.request_id === 'string') flow.requestId = data.request_id;
        if (typeof data.result_id === 'string') flow.resultId = data.result_id;
        if (flow.kind==='read' && typeof data.evidence_id === 'string') flow.resultId = data.evidence_id;
        if (expired || (flow.kind==='read'?this.readFlow:this.flow) !== flow || flow.controller.signal.aborted) this.revoke(flow);
      }
      if (!response.ok) throw BilibiliBrowserSearch.error(data?.error_code, data?.error, stage);
      if (!data || typeof data !== 'object') throw BilibiliBrowserSearch.error('invalid_response', undefined, stage);
      return data;
    };
    try { return await Promise.race([work(), deadline]); }
    finally { clearTimeout(timer); }
  }

  revoke(flow) {
    const body = {};
    if (flow.requestId) body.request_id = flow.requestId;
    if (flow.resultId) body[flow.kind==='read'?'evidence_id':'result_id'] = flow.resultId;
    if (!Object.keys(body).length) return;
    // Do not reuse the aborted search signal. A late POST can mint a capability
    // after cancellation, so every newly received token gets its own revocation.
    try {
      Promise.resolve(this.fetcher(flow.kind==='read'?'/bilibili-video/cancel':'/bilibili-browser/cancel', {method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), keepalive:true})).catch(() => {});
    } catch (_) { /* The server also expires all capabilities automatically. */ }
  }

  cancel() {
    const flow = this.flow;
    if (!flow) return;
    this.flow = null;
    flow.controller.abort();
    flow.signal?.removeEventListener('abort', flow.abort);
    this.post(flow.id, 'cancel');
    this.revoke(flow);
  }

  cancelRead() {
    const flow=this.readFlow;
    if(!flow)return;
    this.readFlow=null;
    flow.controller.abort();
    flow.signal?.removeEventListener('abort',flow.abort);
    this.post(flow.id,'cancel');
    this.revoke(flow);
  }

  async readVideo(query,{signal}={}) {
    this.cancelRead();
    const flow={kind:'read',id:'bili-read-'+Date.now().toString(36)+'-'+(++this.sequence),
      controller:new AbortController(),signal,requestId:null,resultId:null};
    this.readFlow=flow;
    flow.abort=()=>{if(this.readFlow===flow)this.cancelRead();};
    if(signal?.aborted)this.cancelRead();else signal?.addEventListener('abort',flow.abort,{once:true});
    const active=()=>{
      if(this.readFlow!==flow||flow.controller.signal.aborted){
        this.revoke(flow);throw Object.assign(new Error('Cancelled'),{name:'AbortError'});
      }
    };
    let abort;
    const cancelled=new Promise((_,reject)=>{
      abort=()=>reject(Object.assign(new Error('Cancelled'),{name:'AbortError'}));
      if(flow.controller.signal.aborted)abort();else flow.controller.signal.addEventListener('abort',abort,{once:true});
    });
    const work=async()=>{
      active();
      await this.connect({signal:flow.controller.signal});
      active();
      if(!this.capabilities.includes('video_subtitles_v1'))throw BilibiliBrowserSearch.error('extension_upgrade_required');
      const request=await this.json('/bilibili-video/request',{query},flow);
      active();
      if(typeof request.request_id!=='string'||typeof request.url!=='string')throw BilibiliBrowserSearch.error('invalid_response');
      flow.requestId=request.request_id;
      const evidence=await this.exchange(flow.id,'read_video',{url:request.url},flow.controller.signal,this.searchTimeoutMs);
      active();
      const receipt=await this.json('/bilibili-video/result',{request_id:flow.requestId,evidence},flow);
      active();
      if(typeof receipt.evidence_id!=='string'||receipt.url!==request.url||receipt.query!==query
        ||!['full_subtitles','subtitle_excerpt','page_metadata'].includes(receipt.evidence_kind))throw BilibiliBrowserSearch.error('invalid_response');
      flow.resultId=receipt.evidence_id;
      return receipt;
    };
    try{return await Promise.race([work(),cancelled]);}
    catch(error){if(this.readFlow===flow)this.cancelRead();throw error;}
    finally{flow.controller.signal.removeEventListener('abort',abort);}
  }

  async search(query, {signal} = {}) {
    this.cancel();
    const flow = {id:'bili-search-' + Date.now().toString(36) + '-' + (++this.sequence),
      controller:new AbortController(), signal, requestId:null, resultId:null};
    this.flow = flow;
    flow.abort = () => { if (this.flow === flow) this.cancel(); };
    if (signal?.aborted) this.cancel();
    else signal?.addEventListener('abort', flow.abort, {once:true});
    const active = () => {
      if (this.flow !== flow || flow.controller.signal.aborted) {
        this.revoke(flow);
        throw Object.assign(new Error('Cancelled'), {name:'AbortError'});
      }
    };
    let abort;
    const cancelled = new Promise((_, reject) => {
      abort = () => reject(Object.assign(new Error('Cancelled'), {name:'AbortError'}));
      if (flow.controller.signal.aborted) abort();
      else flow.controller.signal.addEventListener('abort', abort, {once:true});
    });
    const work = async () => {
      active();
      // A fresh handshake detects removed/reloaded extensions before registering
      // an operation; connected is a UI hint, not perpetual authorization.
      await this.connect({signal:flow.controller.signal});
      active();
      const request = await this.json('/bilibili-browser/request', {query}, flow);
      flow.requestId = typeof request.request_id === 'string' ? request.request_id : null;
      active();
      if (!flow.requestId || !['search','video'].includes(request.kind) || typeof request.url !== 'string') {
        throw BilibiliBrowserSearch.error('invalid_response');
      }
      const page = await this.exchange(flow.id, 'search', {query:request.query, url:request.url, kind:request.kind},
        flow.controller.signal, this.searchTimeoutMs);
      active();
      const result = await this.json('/bilibili-browser/result', {request_id:flow.requestId,
        page_url:page?.page_url, results:page?.results}, flow);
      flow.resultId = typeof result.result_id === 'string' ? result.result_id : null;
      active();
      if (!flow.resultId || result.query !== query || !Array.isArray(result.results)) throw BilibiliBrowserSearch.error('invalid_response');
      // Public statistic diagnostics belong only to this successful browser
      // request. Do not send them to the server or persist them with snapshots.
      if (page?.stats_diagnostic?.version === 1 && Array.isArray(page.stats_diagnostic.cards)) {
        try {
          const diagnostic = JSON.stringify(page.stats_diagnostic);
          if (new TextEncoder().encode(diagnostic).length <= 6144) result.stats_diagnostic = JSON.parse(diagnostic);
        } catch (_) { /* A diagnostic must never make an otherwise usable search fail. */ }
      }
      return result;
    };
    try { return await Promise.race([work(), cancelled]); }
    catch (error) {
      if (this.flow === flow) this.cancel();
      if (error.code === 'browser_disconnected' || error.code === 'browser_required') this.connected = false;
      throw error;
    } finally { flow.controller.signal.removeEventListener('abort', abort); }
  }
}
if (typeof module !== 'undefined') module.exports = BilibiliBrowserSearch;
if (typeof window !== 'undefined') window.BilibiliBrowserSearch = BilibiliBrowserSearch;
