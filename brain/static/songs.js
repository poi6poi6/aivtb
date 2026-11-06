/* Plays prepared audio locally; local files are never uploaded. */
class SongDeck {
  constructor({beforePlay, beforeRequest, narrate, onCancel, onChange, narrationTimeoutMs = 180000, onBilibili, onStop} = {}) {
    this.beforePlay = beforePlay; this.onBilibili = onBilibili; this.onStop = onStop;
    this.beforeRequest = beforeRequest; this.narrate = narrate; this.onCancel = onCancel; this.onChange = onChange;
    this.narrationTimeoutMs = Number.isFinite(narrationTimeoutMs) && narrationTimeoutMs > 0 ? narrationTimeoutMs : 180000;
    this.flow = null; this.phase = 'idle'; this.continueButton = null;
    this.songs = []; this.local = []; this.queue = []; this.current = null;
    this.revision = 0; this.pending = null;
    this.loadState = 'idle'; this.loadPromise = null; this.loadRevision = 0;
    const el = id => document.getElementById(id);
    this.audio = el('song-audio'); this.panel = el('song-panel');
    this.status = el('song-status'); this.search = el('song-search');
    this.audio.hidden = true;
    if (this.narrate) {
      this.narrationActions = document.createElement('div');
      this.narrationActions.className = 'song-narration-actions';
      this.panel.append(this.narrationActions);
    }
    el('open-songs').onclick = () => { this.panel.hidden = !this.panel.hidden; };
    el('close-songs').onclick = () => { this.panel.hidden = true; };
    el('song-refresh').onclick = () => this.load({force:true});
    el('song-next').onclick = () => this.next();
    el('song-stop').onclick = () => this.stop();
    this.search.oninput = () => this.render();
    el('song-files').onchange = event => {
      for (const file of event.target.files) {
        if (!file.type.startsWith('audio/') && !/\.(mp3|wav|ogg|m4a|flac|webm)$/i.test(file.name)) continue;
        this.local.push({title:file.name.replace(/\.[^.]+$/, ''), url:URL.createObjectURL(file)});
      }
      event.target.value = ''; this.render();
    };
    this.audio.onended = () => {
      // A preceding track ending cannot cancel the newer command awaiting a
      // refreshed library. That command still owns the next selection.
      if (this.pending) { this.current = null; this.renderQueue(); }
      else this.next();
    };
    this.audio.onpause = () => { if (this.current) { this.status.textContent = `已暂停：${this.current.title}`; this.setPhase('paused'); } };
    this.audio.onplaying = () => { if (this.current) { this.status.textContent = `正在播放：${this.current.title}`; this.setPhase('playing'); } };
    this.audio.onerror = () => { if (this.current) this.status.textContent = '歌曲无法播放，可重选音频或切换下一首'; };
    window.addEventListener('beforeunload', () => this.local.forEach(song => URL.revokeObjectURL(song.url)));
    this.load();
  }
  static unwrap(text) {
    let value = String(text ?? '').normalize('NFKC').trim().replace(/^[:：]+/, '').trim()
      .replace(/[。！？!]+$/, '').trim();
    const pairs = {'《':'》', '「':'」', '『':'』', '“':'”', '‘':'’', '"':'"', "'":"'"};
    while (value.length > 1 && pairs[value[0]] === value.at(-1)) value = value.slice(1, -1).trim();
    return value;
  }
  static normalize(text) {
    return SongDeck.unwrap(text).toLowerCase().replace(/[\p{P}\s]+/gu, '');
  }
  static bilibiliReference(text) {
    if (/^BV[0-9a-z]{10}$/i.test(text)) return true;
    try {
      const url = new URL(text.includes('://') ? text : `https://${text}`);
      return /^https?:$/.test(url.protocol) && !url.username && !url.password
        && /^(?:(?:www|m)\.)?bilibili\.com$/i.test(url.hostname)
        && ['', '80', '443'].includes(url.port)
        && /^\/video\/(?:BV[0-9a-z]{10}|av\d+)\/?$/i.test(url.pathname);
    } catch (_) { return false; }
  }
  static commandPrefix(value) {
    // Only consume known greetings/vocatives and request wording at the start.
    // Looking for a command somewhere inside a sentence would execute quotes,
    // past requests and hypothetical or negated examples.
    const greeting = /^(?:早上好|上午好|中午好|下午好|晚上好|你好(?:呀|啊)?|您好|哈喽|哈啰|嗨|早安|晚安|助手)[\s,，、:：!！。]*/i;
    const permission = /^(?:你)?(?:能不能|可不可以|能否|可以)\s*/;
    const polite = /^(?:请(?:你)?|麻烦(?:你)?|帮我|给我|为我)\s*/;
    let question = false;
    for (let i=0;i<8;i++) {
      let prefix = value.match(greeting);
      if (prefix) { value=value.slice(prefix[0].length); continue; }
      prefix = value.match(permission);
      if (prefix) { question=true; value=value.slice(prefix[0].length); continue; }
      prefix = value.match(polite);
      if (prefix) { value=value.slice(prefix[0].length); continue; }
      break;
    }
    return {value, question};
  }
  static commandQuery(value, {question=false, platform=false} = {}) {
    value=value.trim().replace(/^[:：,，]+/, '').trim().replace(/[。!！?？]+$/, '').trim();
    if (platform) value=value.replace(/\s*(?:并|然后)(?:播放|打开)$/, '').trim();
    const quotes={'"':'"', "'":"'", '“':'”', '‘':'’', '《':'》', '「':'」', '『':'』'};
    // A quoted title owns its punctuation and final 吧. Courtesy outside the
    // closing quote can be removed without silently changing the song title.
    if (value && quotes[value[0]]) {
      const close=value.lastIndexOf(quotes[value[0]]);
      if (close>0 && /^[\s,，、:：!！。]*(?:(?:吧|呀|啊|好吗|好不好|可以吗|行吗|谢谢|拜托了|吗))?$/.test(value.slice(close+1))) {
        value=value.slice(1,close).trim();
        while(value.length>=2 && quotes[value[0]]===value.at(-1)) value=value.slice(1,-1).trim();
        return value;
      }
    }
    value=value.replace(/[\s,，]+(?:吧|呀|啊|好吗|好不好|可以吗|行吗|谢谢|拜托了|吗)$/, '').trim();
    if (question) {
      const suffix=value.match(/(?:可以吗|好吗|好不好|行吗|吗)$/);
      if (suffix && suffix.index>0) value=value.slice(0,suffix.index).trim();
    }
    return value;
  }
  static queryIsDiscussion(value) {
    const quotes={'"':'"', "'":"'", '“':'”', '‘':'’', '《':'》', '「':'」', '『':'』'};
    if (value && quotes[value[0]]) {
      const close=value.lastIndexOf(quotes[value[0]]);
      if (close>0) {
        const tail=value.slice(close+1).trim().replace(/[。!！?？]+$/, '').trim();
        if (/^[\s,，、:：!！。]*(?:(?:吧|呀|啊|好吗|好不好|可以吗|行吗|谢谢|拜托了|吗))?$/.test(tail)) return false;
        value=tail;
      }
    }
    return /(?:是什么意思|什么意思|是什么|会怎么样|会怎样|怎么样|怎么用|如何使用|怎么操作|的意思|的功能|功能介绍|这个功能|了吗|了没|过吗)[。!！?？]*$/.test(value)
      || /^(?:功能|这个功能|你(?:说|讲|聊)|关于)/.test(value)
      || /(?:^|[,，;；]|但是|不过|然后|但)\s*(?:现在|先|暂时|还是|请)?\s*(?:(?:不要|别|不用|不想)(?:再)?(?:播放|放歌|点歌|听歌|放个|放一首|放首|放一下|放(?=$|[了吧]))|取消(?:播放|点歌)|(?:稍后|等会)(?:再)?(?:播放|放歌|点歌))/.test(value);
  }
  static platformCommandQuery(value, {question=false} = {}) {
    value=value.trim().replace(/^[:：,，]+/, '').trim().replace(/[。!！?？]+$/, '').trim();
    // Exactly one extra spoken play request can follow a platform command.
    // Quoted titles never start this branch and remain verbatim title text.
    const request=value.match(/^(?:(?:请(?:你)?|麻烦(?:你)?|帮我|给我|为我)\s*){0,3}(?:放个|放一首|放首|放一下|播放|来一首|来首)\s*[:：,，]?\s*(.*)$/);
    if (!request) {
      const guarded=value.replace(/^(?:(?:请(?:你)?|麻烦(?:你)?|帮我|给我|为我)\s*){1,3}/, '');
      return SongDeck.queryIsDiscussion(guarded) ? null : SongDeck.commandQuery(value, {question,platform:true});
    }
    const title=request[1].trim();
    if (SongDeck.queryIsDiscussion(title)) return null;
    let query=SongDeck.commandQuery(title, {question,platform:true});
    const quoted=/^["'“‘《「『]/.test(title);
    const separatedCourtesy=/[\s,，]+(?:吧|呀|啊|好吗|好不好|可以吗|行吗|谢谢|拜托了|吗)$/.test(title);
    if (!quoted && !separatedCourtesy && !SongDeck.bilibiliReference(query)
        && Array.from(query).length>2 && query.endsWith('吧')) query=query.slice(0,-1).trimEnd();
    return query;
  }
  static parseCommand(text) {
    if (typeof text !== 'string') return null;
    const raw=text.normalize('NFKC').trim().replace(/[。!！?？]+$/, '').trim();
    let value=SongDeck.unwrap(raw);
    if (SongDeck.bilibiliReference(value)) return {mode:'bilibili', query:value};
    if (raw.startsWith('《') && raw.endsWith('》')) return null;
    const prefix=SongDeck.commandPrefix(value);
    value=prefix.value;
    const {question}=prefix;
    if (SongDeck.bilibiliReference(value)) return {mode:'bilibili', query:value};
    const control=SongDeck.commandQuery(value, {question});
    if (/^(?:停止播放|停止音乐|停止唱歌|停止歌曲|停止点歌|停歌|取消点歌|取消播放|别放了)$/.test(control)) return {mode:'stop', query:''};
    if (/^(?:下一首|切歌)$/.test(control)) return {mode:'next', query:''};
    if (/^(?:唱一首歌|唱首歌|打开歌单|查看歌单|本地歌单|点个歌)$/.test(control)) return {mode:'open', query:''};
    const play='(?:(?:我)?想(?:要)?(?:听(?:一首|一下)?|点(?:一)?首(?:歌)?|点歌)|点一首歌|点一首|点首歌|点首|点歌|播放|放一下|放一首|放首|放歌|来一首|来首|听一首|听一下)';
    const platform='(?:(?:在|去)\\s*)?(?:B\\s*站|哔哩哔哩|bilibili)(?:上|里)?(?:的)?\\s*';
    const action='(?:'+play+'|搜索|搜一下|搜|找|打开|点开|听)';
    const desire=value.match(new RegExp('^(?:我)?想(?:要)?(?='+platform+')', 'i'));
    if (desire) value=value.slice(desire[0].length);
    const bili=value.match(new RegExp('^'+platform+action+'\\s*[:：]?\\s*(.*)$', 'i'));
    if (bili) {
      const query=SongDeck.platformCommandQuery(bili[1], {question});
      return query===null ? null : {mode:'bilibili', query};
    }
    const request=value.match(new RegExp('^'+play+'\\s*[:：]?\\s*(.*)$', 'i'));
    if (!request) return null;
    const rawQuery=request[1].trim().replace(/^[:：,，]+/, '').trim();
    if (SongDeck.queryIsDiscussion(rawQuery)) return null;
    const remote=rawQuery.match(new RegExp('^'+platform+'('+action+')?\\s*[:：]?\\s*(.*)$', 'i'));
    if (remote) {
      const candidate=/^(?:放个|放一首|放首|放一下|播放|来一首|来首)$/.test(remote[1] || '')
        ? remote[1]+' '+remote[2] : remote[2];
      const query=SongDeck.platformCommandQuery(candidate, {question});
      return query===null ? null : {mode:'bilibili', query};
    }
    const query=SongDeck.commandQuery(rawQuery, {question});
    if (!query) return {mode:'open', query:''};
    return {mode:SongDeck.bilibiliReference(query) ? 'bilibili' : 'local', query};
  }
  get active() { return this.current !== null || this.pending !== null; }
  setPhase(phase) {
    this.phase = phase;
    this.renderQueue();
    try { this.onChange?.({active:this.active, phase}); }
    catch (error) { console.warn('Song state observer failed', error); }
  }
  currentFlow(flow) { return this.flow === flow && flow.revision === this.revision && !flow.controller.signal.aborted; }
  cancelFlow() {
    const previous = this.flow;
    ++this.revision;
    this.flow = null; this.pending = null; this.continueButton = null;
    previous?.controller.abort();
    this.narrationActions?.replaceChildren();
    if (previous && this.onCancel) {
      try { Promise.resolve(this.onCancel()).catch(error => console.warn('Song cancellation observer failed', error)); }
      catch (error) { console.warn('Song cancellation observer failed', error); }
    }
  }
  detachAudio() {
    // Clear current before pause/load can synchronously dispatch old media events.
    this.current = null;
    this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load(); this.audio.hidden = true;
  }
  async waitFor(callback, signal) {
    if (signal.aborted) return false;
    let abort;
    const cancelled = new Promise(resolve => {
      abort = () => resolve(false); signal.addEventListener('abort', abort, {once:true});
    });
    try { return await Promise.race([Promise.resolve().then(() => signal.aborted ? false : callback()), cancelled]); }
    finally { signal.removeEventListener('abort', abort); }
  }
  async say(flow, stage, item, error) {
    if (!this.currentFlow(flow)) return false;
    flow.narrationFailure = '';
    const controller = new AbortController();
    const abort = () => controller.abort();
    flow.controller.signal.addEventListener('abort', abort, {once:true});
    const timer = setTimeout(abort, this.narrationTimeoutMs);
    try {
      const completed = await this.waitFor(() => this.narrate({stage, query:flow.query, item, error, signal:controller.signal}), controller.signal);
      if (completed !== true) controller.abort();
      return completed === true && !controller.signal.aborted && this.currentFlow(flow);
    } catch (failure) {
      if (failure?.code === 'voice_resource_unavailable' && typeof failure.message === 'string' && this.currentFlow(flow)) {
        flow.narrationFailure = failure.message.slice(0,500);
        this.status.textContent = flow.narrationFailure;
      }
      controller.abort(); return false;
    }
    finally { clearTimeout(timer); flow.controller.signal.removeEventListener('abort', abort); }
  }
  offerContinue(flow, stage) {
    if (!this.currentFlow(flow)) return;
    this.status.textContent = flow.narrationFailure
      ? flow.narrationFailure + ' 当前步骤已暂停；处理后重新点歌，或手动继续。'
      : stage === 'ack'
      ? '角色语音未完成或暂不可用，尚未查找歌曲；可以手动继续。若刚更新功能，请重启主程序后刷新页面。'
      : '角色语音未完成或暂不可用，歌曲尚未播放；可以手动跳过介绍。若刚更新功能，请重启主程序后刷新页面。';
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = stage === 'ack' ? '跳过回应，继续查找' : '语音没播完，点击继续播放';
    button.onclick = () => {
      if (!this.currentFlow(flow) || this.continueButton !== button) return false;
      button.disabled = true;
      return this.startNarrated(flow.query, {song:flow.song, ack:false, skipFound:stage === 'found'});
    };
    this.continueButton = button; this.narrationActions.replaceChildren(button);
    this.setPhase('awaiting-continue');
  }
  startNarrated(query, {song = null, ack = false, skipFound = false} = {}) {
    this.cancelFlow();
    this.detachAudio();
    const flow = {revision:this.revision, controller:new AbortController(), query, song};
    this.flow = flow; this.pending = flow; this.panel.hidden = false;
    this.setPhase(ack ? 'ack' : 'preparing');
    const work = this.runNarrated(flow, {ack, skipFound});
    this.pendingWork = work;
    return work;
  }
  matchingSongs(query) {
    const normalized = SongDeck.normalize(query), all = [...this.songs, ...this.local];
    const exact = all.filter(song => SongDeck.normalize(song.title) === normalized);
    const matches = exact.length ? exact : all.filter(song => SongDeck.normalize(song.title).includes(normalized));
    return [...new Map(matches.filter(song => this.validSong(song, true)).map(song => [song.url, song])).values()];
  }
  async runNarrated(flow, {ack, skipFound}) {
    try {
      if (this.beforeRequest) {
        const ready = await this.waitFor(() => this.beforeRequest({query:flow.query, signal:flow.controller.signal}), flow.controller.signal);
        if (!this.currentFlow(flow)) return false;
        if (ready === false) { this.offerContinue(flow, 'ack'); return false; }
      }
      if (ack) {
        this.status.textContent = '等助手回应后，查找这首歌…';
        const ready = await this.say(flow, 'ack');
        if (!this.currentFlow(flow)) return false;
        if (!ready) { this.offerContinue(flow, 'ack'); return false; }
      }
      if (!flow.song) {
        this.setPhase('querying'); this.status.textContent = `正在查找：${flow.query}`;
        const loaded = this.loadState === 'ready' || await this.waitFor(() => this.waitForLibrary(flow.revision), flow.controller.signal);
        if (!this.currentFlow(flow)) return false;
        const matches = this.matchingSongs(flow.query);
        if (matches.length !== 1) {
          this.search.value = flow.query; this.render();
          this.status.textContent = matches.length ? '找到多首歌曲，请选择想听的版本'
            : !loaded ? '曲库加载失败，请刷新重试；已有本地音频中也未找到这首歌' : '曲库里还没有这首歌，请先添加音频';
          this.setPhase('narrating');
          await this.say(flow, matches.length ? 'results' : 'error', undefined, matches.length ? undefined : loaded ? 'empty' : 'library');
          if (!this.currentFlow(flow)) return false;
          if (!matches.length) this.pending = null;
          this.setPhase(matches.length ? 'results' : 'error');
          return false;
        }
        flow.song = matches[0];
      }
      if (!this.validSong(flow.song, true)) throw new Error('invalid song');
      this.setPhase('preparing');
      if (this.beforePlay) {
        const allowed = await this.waitFor(() => this.beforePlay(flow.song), flow.controller.signal);
        if (!this.currentFlow(flow)) return false;
        if (allowed === false) { this.offerContinue(flow, 'found'); return false; }
      }
      if (!skipFound) {
        this.setPhase('narrating'); this.status.textContent = `已找到《${flow.song.title}》，等助手介绍完再播放…`;
        const ready = await this.say(flow, 'found', flow.song);
        if (!this.currentFlow(flow)) return false;
        if (!ready) { this.offerContinue(flow, 'found'); return false; }
      }
      if (!this.currentFlow(flow) || !this.validSong(flow.song, true)) return false;
      return await this.playAudio(flow.song, flow.revision);
    } catch (_) {
      if (!this.currentFlow(flow)) return false;
      this.pending = null; this.status.textContent = '点歌暂时失败，请刷新曲库或重新选择音频';
      this.setPhase('error'); return false;
    }
  }
  validSong(song, allowLocal = false) {
    return song && typeof song.title === 'string' && song.title.trim() && typeof song.url === 'string'
      && (/^\/songs\/[a-f0-9]{24}$/.test(song.url)
        || (allowLocal && this.local.some(item => item.url === song.url) && song.url.startsWith('blob:')));
  }
  request(song) { return this.enqueue(song, false); }
  enqueue(song, allowLocal = true) {
    if (!this.validSong(song, allowLocal)) return false;
    if (this.queue.length >= 10) { this.status.textContent = '队列最多容纳10首，请先移除或播放一首'; return false; }
    if (this.narrate) {
      if (this.current?.url === song.url || this.pending?.song?.url === song.url || this.queue.some(item => item.url === song.url)) return false;
      this.queue.push(song); this.renderQueue();
      // Adding behind a playing or already selected song never interrupts it.
      if (this.current || this.pending?.song) return true;
      const selected = this.queue.shift();
      this.startNarrated(selected.title, {song:selected});
      return true;
    }
    // A newer concrete selection supersedes an older command still loading.
    ++this.revision; this.pending = null;
    if (this.current?.url === song.url || this.queue.some(item => item.url === song.url)) { this.renderQueue(); return false; }
    this.queue.push(song); this.renderQueue();
    if (!this.current) this.next();
    return true;
  }
  load({force = false} = {}) {
    if (this.loadPromise && !force) return this.loadPromise;
    if (this.loadState === 'ready' && !force) return Promise.resolve(true);
    const loadRevision = ++this.loadRevision, statusRevision = this.revision;
    this.loadState = 'loading'; this.render();
    const promise = (async () => {
      try {
        const response = await fetch('/songs', {cache:'no-store'});
        if (!response.ok) throw new Error('Library unavailable');
        const songs = await response.json();
        if (!Array.isArray(songs)) throw new Error('Invalid library');
        if (loadRevision !== this.loadRevision) return false;
        this.songs = songs.filter(song => this.validSong(song));
        this.loadState = 'ready'; this.render();
        return true;
      } catch (_) {
        if (loadRevision === this.loadRevision) {
          this.loadState = 'error'; this.render();
          if (statusRevision === this.revision && !this.current && !this.pending)
            this.status.textContent = '曲库加载失败，请点击刷新曲库重试；本地音频仍可播放';
        }
        return false;
      } finally {
        if (loadRevision === this.loadRevision) this.loadPromise = null;
      }
    })();
    this.loadPromise = promise;
    return promise;
  }
  async waitForLibrary(revision) {
    while (revision === this.revision) {
      if (this.loadState === 'ready') return true;
      const promise = this.loadPromise || this.load();
      const loaded = await promise;
      if (revision !== this.revision) return false;
      // A refresh can replace the request while this command is waiting.
      if (this.loadPromise && this.loadPromise !== promise) continue;
      if (this.loadState === 'ready') return true;
      if (!loaded) return false;
    }
    return false;
  }
  render() {
    const list = document.getElementById('song-list'); list.replaceChildren();
    const query = SongDeck.normalize(this.search.value);
    const songs = [...this.songs, ...this.local].filter(song => SongDeck.normalize(song.title).includes(query));
    if (!songs.length) {
      const hint = document.createElement('p');
      hint.textContent = this.loadState === 'loading' ? '正在加载曲库…'
        : this.loadState === 'error' ? '曲库加载失败，请刷新重试，也可以选择本地音频。'
        : query ? '没有找到这首歌，可以选择本地音频添加。' : '曲库还没有歌曲，选择本地音频即可开始。';
      list.append(hint);
    }
    songs.forEach(song => {
      const row = document.createElement('div'); row.className = 'song-toolbar';
      const title = document.createElement('span'); title.textContent = song.title;
      const play = document.createElement('button'); play.textContent = '播放'; play.onclick = () => this.play(song);
      const add = document.createElement('button'); add.textContent = '加入队列'; add.onclick = () => this.enqueue(song);
      row.append(title, play, add); list.append(row);
    });
    this.renderQueue();
  }
  renderQueue() {
    const list = document.getElementById('song-queue'); list.replaceChildren();
    this.queue.forEach((song, index) => {
      const li = document.createElement('li'); li.textContent = song.title + ' ';
      const remove = document.createElement('button'); remove.textContent = '移除';
      remove.onclick = () => { this.queue.splice(index, 1); this.renderQueue(); };
      li.append(remove); list.append(li);
    });
    document.getElementById('song-next').disabled = !this.queue.length;
    document.getElementById('song-stop').disabled = !this.active && !this.queue.length;
  }
  async play(song) {
    if (!this.validSong(song, true)) { this.status.textContent = '歌曲地址无效，请刷新曲库或重新选择本地音频'; return false; }
    if (this.narrate) return this.startNarrated(song.title, {song});
    const revision = ++this.revision;
    return this.playAudio(song, revision, true);
  }
  async playAudio(song, revision, prepare = false) {
    if (revision !== this.revision) return false;
    this.pending = null; this.current = song; this.panel.hidden = false;
    this.status.textContent = `正在加载：${song.title}`; this.setPhase('playing');
    try {
      if (prepare) this.beforePlay?.();
      if (revision !== this.revision) return false;
      this.audio.src = song.url;
      this.audio.hidden = false;
      await this.audio.play();
      if (revision === this.revision) this.status.textContent = `正在播放：${song.title}`;
      return revision === this.revision;
    } catch (error) {
      if (revision === this.revision) this.status.textContent = error?.name === 'NotAllowedError'
        ? '浏览器阻止了自动播放，请点击下方播放器的播放按钮继续'
        : '未能播放这首歌，请点击播放器重试；仍失败可换一首或检查音频格式';
      return false;
    }
  }
  next() {
    if (this.narrate) {
      const song = this.queue.shift();
      if (song) return this.startNarrated(song.title, {song});
      this.stop('播放结束，可以继续聊天了');
      return false;
    }
    ++this.revision; this.pending = null;
    const song = this.queue.shift();
    if (song) this.play(song); else this.stop('播放结束，可以继续聊天了');
    this.renderQueue();
  }
  stop(notice = '歌曲已停止，可以继续聊天了') {
    const hadWork = this.active || this.queue.length;
    // Always invalidate pending commands, including while current is still null.
    this.cancelFlow(); this.current = null; this.queue = [];
    this.audio.hidden = true;
    if (hadWork) {
      this.audio.pause(); this.audio.removeAttribute('src'); this.audio.load();
      this.status.textContent = notice;
    }
    this.setPhase('idle');
  }
  async resolveLocal(query, revision) {
    const loaded = this.loadState === 'ready' || await this.waitForLibrary(revision);
    if (revision !== this.revision) return;
    this.pending = null;
    const normalized = SongDeck.normalize(query);
    const all = [...this.songs, ...this.local];
    const exact = all.filter(song => SongDeck.normalize(song.title) === normalized);
    const matches = exact.length ? exact : all.filter(song => SongDeck.normalize(song.title).includes(normalized));
    // Refreshes may repeat an identical URL; that is one recording, not ambiguity.
    const unique = [...new Map(matches.map(song => [song.url, song])).values()];
    if (unique.length === 1) { this.play(unique[0]); return; }
    this.search.value = query; this.render();
    this.status.textContent = unique.length ? '找到多首歌曲，请选择想听的版本'
      : !loaded ? '曲库加载失败，请刷新重试；已有本地音频中也未找到这首歌'
      : '曲库里还没有这首歌，请先添加音频';
  }
  command(text) {
    const parsed = SongDeck.parseCommand(text);
    if (!parsed) return false;
    if (parsed.mode === 'stop') {
      this.stop();
      try {
        const result = this.onStop?.(), revision = this.revision;
        Promise.resolve(result).catch(() => {
          if (revision === this.revision) this.status.textContent = '本地歌曲已停止，请检查B站播放器的停止状态';
        });
      }
      catch (_) { this.status.textContent = '本地歌曲已停止，请检查B站播放器的停止状态'; }
      return true;
    }
    if (parsed.mode === 'next') { this.next(); return true; }
    if (parsed.mode === 'open') { this.panel.hidden = false; this.status.textContent = '请选择曲库歌曲，或输入“点歌 歌名”'; return true; }
    if (parsed.mode === 'bilibili') {
      // The Bilibili player owns its own cancellation. Do not leave a SongDeck
      // pending token that its before-play cleanup could accidentally cancel.
      this.stop();
      if (typeof this.onBilibili !== 'function') {
        this.panel.hidden = false;
        this.status.textContent = '当前页面未启用B站播放，请从曲库或本地音频选择';
        return true;
      }
      this.panel.hidden = true;
      this.status.textContent = '已切换到B站点歌，请查看B站面板。';
      const revision = this.revision;
      const failed = () => {
        if (revision === this.revision) this.status.textContent = 'B站请求未完成，请查看B站面板提示。';
      };
      try {
        // A resolved false is a handled Bilibili outcome, not an ongoing local
        // operation. Never reopen this panel or overwrite a newer local song.
        Promise.resolve(this.onBilibili(parsed.query)).then(result => {
          if (result === false) failed();
        }, failed);
      } catch (_) { failed(); }
      return true;
    }
    this.panel.hidden = false;
    const query = SongDeck.normalize(parsed.query);
    if (this.narrate) {
      if (!query) { this.status.textContent = '请在点歌后填写歌名'; return true; }
      this.startNarrated(parsed.query, {ack:true});
      return true;
    }
    ++this.revision; this.pending = null;
    if (!query) { this.status.textContent = '请在点歌后填写歌名'; this.renderQueue(); return true; }
    const revision = this.revision;
    this.pending = {revision, query:parsed.query};
    this.status.textContent = this.loadState === 'ready' ? `正在查找：${parsed.query}` : `曲库正在加载，稍后播放：${parsed.query}`;
    this.renderQueue();
    this.resolveLocal(parsed.query, revision).catch(() => {
      if (revision !== this.revision) return;
      this.pending = null; this.status.textContent = '点歌暂时失败，请刷新曲库重试'; this.renderQueue();
    });
    return true;
  }
}
