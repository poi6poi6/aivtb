/* Host console: configuration/status, explicit drafts and isolated test scenarios. */
(function(root) {
  'use strict';
  const scenarios = {
    chat: {source:'companion',kind:'chat',user:'本地测试员',text:'你好，介绍一下你自己吧。'},
    comfort: {source:'companion',kind:'chat',user:'本地测试员',text:'今天有点累，想找个人聊一会儿。'},
    gift: {source:'danmaku',kind:'gift',user:'本地测试员',text:'送出一颗小星星（测试事件）'},
    interrupt: {text:'停止当前回答与播放，保留已经显示的文字。'},
    slow: {text:'模拟等待较久：观察等待提示，再返回离线脚本回复与提示音。'},
    reply_error: {text:'模拟回复失败：显示可恢复的错误，不请求真实模型。'},
    tts_error: {text:'模拟语音失败：保留文字回复，观察错误反馈。'},
  };
  function playerLabel(data) {
    if (typeof data.audio_owner !== 'boolean') return '归属尚未确认';
    if (data.audio_owner) return '当前页面播放';
    return data.audio_target === 'obs' ? 'OBS 直播页播放' : data.audio_target === 'display' ? '展示页播放' : '另一个控制台播放';
  }
  function voiceLabel(data) {
    if (data.local_test) return '测试提示音 · 非角色语音';
    return data.tts === 'mock' ? '文字模式 · 无语音' : data.tts === 'fish_speech' ? 'Fish Speech' : data.tts || '尚未连接';
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {scenarios, playerLabel, voiceLabel};
  let controller;
  const api = root.ConsoleUI = {
    init(callbacks) { controller = create(callbacks); },
    message(data) { controller?.message(data); },
    disconnected(reason) { controller?.disconnected(reason); },
    audioBlocked() { controller?.audioBlocked(); },
    playing(item) { controller?.playing(item); },
    finished(item, reason) { controller?.finished(item, reason); },
  };
  function create(callbacks) {
    const el = id => document.getElementById(id);
    if (!el('console-summary')) return null;
    let connected = false, hello = {}, playback = {}, owner = {}, blocked = false, blockVersion = 0, pending = null, timer;
    const display = new URLSearchParams(location.search).get('display') === '1';
    const local = () => hello.local_test === true;
    const restricted = () => display || !!hello.public_test;
    const set = (id,text) => { if(el(id))el(id).textContent=text; };
    function notice(text) {
      set('console-notice',text);
      if(el('console-notice'))el('console-notice').hidden=!text || restricted();
    }
    function result(text) { set('test-result',text); }
    function complete(text, run = pending) {
      if (run !== pending) return;
      clearTimeout(timer);pending=null;result(text);controls();
    }
    function controls() {
      const scenario=el('test-scenario').value, busy=!!playback.current_id || Number(playback.queued)>0;
      el('test-run').disabled = !connected || restricted() || (!!pending && scenario !== 'interrupt')
        || (!local() && !scenarios[scenario]?.source) || (local() && busy && scenario !== 'interrupt');
      el('test-run').textContent = local() ? scenario === 'interrupt' ? '停止本轮测试' : '运行离线场景' : '填入测试草稿';
      el('console-reconnect').disabled=connected;
      el('console-reconnect').hidden=connected || restricted();
      el('console-enable-audio').disabled=!connected || restricted() || owner.audio_owner !== true || hello.tts === 'mock';
      el('console-enable-audio').hidden=!connected || restricted() || hello.tts === 'mock';
      el('console-enable-audio').textContent=blocked?'点击恢复本页声音':'启用本页声音';
    }
    function preview() {
      const scenario=scenarios[el('test-scenario').value];
      el('test-preview').value=scenario?.text || '';
      el('test-preview').readOnly=!scenario?.source;
      controls();
    }
    function mode() {
      for (const id of ['console-nav','console-summary','local-test-panel','console-local-banner'])
        if(el(id))el(id).hidden=restricted() || (id==='console-local-banner'&&!local());
      document.documentElement.classList.toggle('local-test-console',local());
      set('console-mode', local()?'离线测试':'当前服务');
      set('console-local-banner','离线测试 · 使用脚本回复和短提示音；测试数据独立，不连接直播间，不代表真实模型或角色音色。');
      set('test-mode-note',local()
        ? '可直接运行下面的场景。声音是短提示音；麦克风、真实模型和角色音色请到正式服务的开播演练中检查。'
        : '这里先填入草稿，不会自动发送。需要无模型、无直播连接的测试时，双击项目里的 启动.cmd，选择“本地离线测试”，独立测试页会自动打开。真实声音检查请使用开播演练。');
      for (const link of document.querySelectorAll('[data-console-service="live"]')) {
        link.setAttribute('aria-disabled',String(local()));
        link.tabIndex=local()?-1:0;
        link.title=local()?'此功能需要正式服务；离线测试不连接它。':'';
      }
      if(local())for(const id of ['voice-input','continuous-label','open-songs','mock-label','mock-game-label','director-control','memory-panel'])
        if(el(id))el(id).hidden=true;
      if(el('test-open-rehearsal'))el('test-open-rehearsal').hidden=local() || restricted();
      set('console-model',local()?'离线脚本回复':hello.llm || '尚未连接');
      set('console-voice',voiceLabel(hello));
      const updateNotice = el('console-update-notice');
      if(updateNotice) {
        const outdated = !restricted() && !local() && Number(hello.music_protocol || 0) < 6;
        updateNotice.hidden = !outdated;
        updateNotice.textContent = outdated
          ? '点歌更新尚未加载：当前连接的是旧版主服务。请关闭旧的主服务启动窗口，再用“启动.cmd”启动正式服务，然后按 Ctrl+F5 刷新。仅刷新网页不会更新主服务。'
          : '';
      }
    }
    function arm(scenario, text) {
      clearTimeout(timer);
      const run=pending={scenario,text,id:null,awaitingReceipt:!scenarios[scenario]?.source,
        buffered:[],audioIds:new Set(),playingIds:new Set()};
      timer=setTimeout(()=>{if(pending===run)complete('本轮尚未确认完成。检查连接、暂停状态和播放器；不会自动重发。',run);},30000);
      result('已提交本轮场景，等待服务回执…');
      controls();
      return run;
    }
    async function run() {
      const name=el('test-scenario').value, scenario=scenarios[name];
      if(el('test-run').disabled || !scenario)return;
      const text=el('test-preview').value.trim();
      if(scenario.source && (!text || text.length>500)) {result('请填写 1–500 个字的测试消息。');return;}
      if(!local()) {
        const accepted=callbacks.draft({...scenario,text});
        result(accepted ? (scenario.source==='danmaku'?'已填入高级事件测试，尚未发送。':'已填入聊天输入框，尚未发送。') : '请先处理未发送的输入或结束收音，再填入测试草稿。');
        return;
      }
      if(name==='interrupt') {
        if(callbacks.send({type:'conversation_interrupt'}))complete('已提交停止请求，正在释放本轮测试。');
        else complete('连接已断开，停止请求尚未确认。');
        return;
      }
      const run=arm(name,text);
      try {
        if(scenario.source) {
          if(!callbacks.send({type:'inject',...scenario,text}))throw Error('连接已断开，测试未发送。');
        } else {
          const response=await fetch('/local-test-scenario',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario:name}),signal:AbortSignal.timeout(8000)});
          if(!response.ok)throw Error('测试请求未确认，请检查连接后再试。');
          const data=await response.json();
          if(pending!==run)return;
          if(data.accepted!==true || !Number.isInteger(data.turn_id) || data.turn_id<1)
            throw Error('测试回执缺少事件编号，请先核对对话区；不会自动重发。');
          run.id=data.turn_id;
          run.awaitingReceipt=false;
          const buffered=run.buffered.splice(0);
          for(const item of buffered)track(item);
        }
      } catch(error) {
        complete(error.name==='TimeoutError'?'提交回执超时；请先核对对话区，本轮不会自动重发。'
          : ['TypeError','SyntaxError'].includes(error.name)?'测试请求未确认，请检查连接；已显示的场景不会自动重发。':error.message,run);
      }
    }
    function track(data) {
      if(!pending)return;
      const run=pending;
      if(run.awaitingReceipt) {
        // POST and WebSocket have no arrival ordering. Keep bounded receipts
        // until the accepted HTTP turn ID identifies this particular run.
        if(run.buffered.length<64)run.buffered.push(data);
        return;
      }
      const scenario=scenarios[run.scenario];
      if(data.type==='event' && run.id===null && scenario?.source
          && data.local_test===true && data.text===run.text && data.source===scenario.source
          && data.kind===scenario.kind && data.user===scenario.user)run.id=data.id;
      const id=data.turn_id ?? data.id;
      if(run.id===null || id!==run.id)return;
      if(data.type==='thinking')result('已收到测试事件 · 正在生成脚本回复…');
      if(data.type==='reply')result('文字已返回 · 等待短提示音与播放回执…');
      if(data.type==='audio') {
        run.audioIds.add(data.id);
        result('提示音已派发 · 等待播放页开始。');
      }
      if(data.type==='reply_error' || data.type==='tts_error')complete('已收到'+(data.type==='reply_error'?'回复':'语音')+'失败反馈。可修改场景或重新测试。',run);
      if(data.type==='reply_cancelled')complete('本轮测试已停止，可以重新选择场景。',run);
      if(data.type==='console_playing' && run.audioIds.has(data.audio_id)) {
        run.playingIds.add(data.audio_id);
        result('提示音已开始播放 · 等待完整播放结束。');
      }
      if(data.type==='console_finished') {
        const heard=run.audioIds.has(data.audio_id) && run.playingIds.has(data.audio_id);
        complete(data.reason==='ended' && heard
          ? '播放页已回报结束。请确认听到了短提示音；真实音色请另做开播演练。'
          : '提示音未确认完整播放（'+(data.reason==='ended'?'未收到开始回执':({'error':'播放错误',stop:'已停止',skip:'已跳过',clear:'已清空'}[data.reason]||'已中断'))+'），可以重试。',run);
      }
    }
    function message(data) {
      if(data.type==='hello') {
        connected=true;hello=data;owner=data;playback=data.playback||{};blocked=false;
        mode();set('console-connection','服务已连接');set('console-player',playerLabel(owner));
        notice(local()?'离线环境已就绪，可运行测试场景。':hello.tts==='mock'?'当前只显示文字；真实语音请检查运行状态。':'声音只交给一个播放页；可在右侧查看归属。');
      } else if(data.type==='audio_owner') {
        owner={...owner,...data};set('console-player',playerLabel(owner));
        if(!data.audio_owner) {
          blocked=false;
          notice(playerLabel(owner)+'，本页继续显示文字与状态。');
          if(pending)result('声音已交给其他播放页，请在那里确认提示音；本页不会代报完成。');
        }
        else notice('声音已回到当前页面。');
      } else if(data.type==='playback')playback=data;
      if(['event','thinking','reply','audio','reply_error','tts_error','reply_cancelled'].includes(data.type))track(data);
      controls();
    }
    for(const link of document.querySelectorAll('[data-console-target]'))link.addEventListener('click',()=>{
      const target=el(link.dataset.consoleTarget);if(!target)return;
      if(target.tagName==='DETAILS')target.open=true;
      for(const other of document.querySelectorAll('[data-console-target]'))other.removeAttribute('aria-current');
      link.setAttribute('aria-current','location');
      target.scrollIntoView?.({block:'start',behavior:'smooth'});
    });
    for(const link of document.querySelectorAll('[data-console-service="live"]'))link.addEventListener('click',event=>{
      if(local()){event.preventDefault();notice('此入口需要正式服务。离线测试仅检查界面、事件和提示音播放。');}
    });
    el('test-scenario').onchange=preview;
    el('test-run').onclick=run;
    el('console-reconnect').onclick=()=>{notice('正在重新连接；不会重复发送之前的消息。');callbacks.reconnect();};
    el('console-enable-audio').onclick=async()=>{
      if(el('console-enable-audio').disabled)return;
      const version=blockVersion;
      try {
        await callbacks.enableAudio();
        if(version===blockVersion) {
          blocked=false;notice('已尝试启用本页声音；下一段播放时请确认听感。');
        }
      }
      catch(_){blocked=true;notice('浏览器尚未允许播放，请检查此页面的声音权限后重试。');}
      controls();
    };
    if(el('test-copy-command'))el('test-copy-command').onclick=async()=>{
      try {await navigator.clipboard.writeText('.\\启动.cmd -Action local-test');result('已复制启动命令。在项目目录的 PowerShell 中运行即可。');}
      catch(_){result('在项目目录运行：.\\启动.cmd -Action local-test；也可双击 启动.cmd，选择“本地离线测试”。');}
    };
    preview();
    return {message,
      disconnected(reason){connected=false;owner={};set('console-connection','服务连接中断');set('console-player','归属尚未确认');notice(reason || '正在自动重连，未发送的输入会保留。');if(pending)complete('连接中断，本轮未确认完成；不会自动重发。');controls();},
      audioBlocked(){blocked=true;blockVersion++;notice('浏览器拦截了声音，请点击“恢复本页声音”；无需重新生成回复。');controls();},
      playing(item){blocked=false;controls();if(item)track({type:'console_playing',id:item.turn_id??item.id,audio_id:item.id});},
      finished(item,reason){if(item)track({type:'console_finished',id:item.turn_id??item.id,audio_id:item.id,reason});},
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
