(function(root) {
  'use strict';
  const CATEGORIES = {anime:'二次元与游戏',meme:'热梗与网络文化',finance:'金融与消费现象'};
  const STATES = {disabled:'自动更新已关闭。',waiting:'等待空闲时更新素材。',refreshing:'Astra 正在联网整理话题…',ready:'可用的话题素材已更新。',empty:'本轮已完成检索，暂未找到符合范围的新话题。',failed:'本次更新未完成。',stale:'旧素材已过期，等待重新更新。',blocked:'暂时不能更新，请检查套餐或稍后重试。'};
  function epoch(value) {
    if(typeof value==='number')return Number.isFinite(value)&&value>0?value:0;
    if(typeof value==='string'&&/^\d{4}-\d\d-\d\dT/.test(value)) {const ms=Date.parse(value);return Number.isFinite(ms)?ms/1000:0;}
    return 0;
  }
  function date(value) {const timestamp=epoch(value);return timestamp?new Date(timestamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',hour12:false}):'尚未记录';}
  function sourceURL(value) {
    if(typeof value!=='string'||value.length>2048)return null;
    try {
      const url=new URL(value),host=url.hostname.toLowerCase();
      if(url.protocol!=='https:'||url.username||url.password||(url.port&&url.port!=='443')
        ||!host.includes('.')||host.endsWith('.')||host.includes(':')||/^\d+(?:\.\d+)*$/.test(host)
        ||/(?:^|\.)(?:localhost|local|internal|test|invalid|example)$/.test(host))return null;
      return url.href;
    }catch(_){return null;}
  }
  class TrendingTopicsPage {
    constructor({document,fetcher,window,clock=()=>Date.now()/1000}) {
      this.document=document;this.fetcher=fetcher;this.window=window;this.clock=clock;
      this.dirty=false;this.pending=false;this.stopped=false;this.version=0;this.timer=null;this.controllers=new Set();this.state=null;this.readFailed=false;
      this.el('trends-form').addEventListener('input',()=>{this.dirty=true;});
      this.el('trends-form').addEventListener('change',()=>{this.dirty=true;});
      this.el('trends-form').onsubmit=e=>{e.preventDefault();return this.save();};
      this.el('trends-refresh').onclick=()=>this.update();
      this.el('refresh')?.addEventListener('click',()=>this.refresh());
      window.addEventListener?.('pagehide',()=>this.stop());
    }
    el(id){return this.document.getElementById(id);}
    node(tag,text,cls){const node=this.document.createElement(tag);node.textContent=String(text||'');if(cls)node.className=cls;return node;}
    notice(text,error=false){this.el('trends-notice').textContent=text;this.el('trends-notice').dataset.error=String(error);}
    stop(){this.stopped=true;this.version++;if(this.timer!==null)this.window.clearTimeout?.(this.timer);for(const c of this.controllers)c.abort();}
    schedule(){
      if(this.stopped)return;
      if(this.timer!==null)this.window.clearTimeout?.(this.timer);
      this.timer=this.window.setTimeout?.(()=>this.refresh(),this.state?.state==='refreshing'?5000:20000)??null;
    }
    controls(){
      for(const id of ['trends-save','trends-enabled','trends-interval',...Object.keys(CATEGORIES).map(key=>'trends-'+key)])this.el(id).disabled=this.pending;
      this.el('trends-refresh').disabled=this.pending||!this.state||this.state.settings.enabled!==true||this.state.state==='refreshing';
      this.el('trends-refresh').textContent=this.state?.state==='refreshing'?'正在更新素材…':'现在更新素材';
    }
    async request(path,options={}) {
      const controller=new AbortController();this.controllers.add(controller);
      const timer=setTimeout(()=>controller.abort(),15000);
      try {
        const response=await this.fetcher(path,{cache:'no-store',credentials:'same-origin',...options,signal:controller.signal});
        let value;try{value=await response.json();}catch(_){throw Error('话题服务暂不可用，请刷新本页重试。');}
        if(!response.ok)throw Error(typeof value?.error==='string'?value.error.slice(0,300):'操作未完成，请稍后重试。');
        if(!value||typeof value!=='object'||!value.settings||typeof value.settings.enabled!=='boolean'||!Array.isArray(value.settings.categories)||!Object.hasOwn(STATES,value.state))throw Error('话题状态不完整，请稍后刷新。');
        return value;
      }finally{clearTimeout(timer);this.controllers.delete(controller);}
    }
    async refresh() {
      if(this.stopped||this.pending)return;
      const version=++this.version;
      try {
        const state=await this.request('/trends-status');
        if(version!==this.version||this.stopped)return;
        this.render(state);
        if(this.readFailed)this.notice('');
        this.readFailed=false;
      }catch(error){
        if(version!==this.version||this.stopped)return;
        if(this.state)this.render(this.state,true);
        this.readFailed=true;
        this.el('trends-status').textContent='话题状态暂时无法确认。';
        this.notice(error.name==='AbortError'?'状态读取超时，设置输入已保留。':error.message,true);
      }finally{if(version===this.version&&!this.stopped){this.controls();this.schedule();}}
    }
    settings(){
      const categories=Object.keys(CATEGORIES).filter(key=>this.el('trends-'+key).checked);
      const interval=Number(this.el('trends-interval').value);
      if(!categories.length)throw Error('请至少保留一个可以聊的方向。');
      if(!Number.isInteger(interval)||interval<2||interval>24)throw Error('更新间隔请填写 2–24 的整数小时。');
      return {enabled:this.el('trends-enabled').checked,interval_hours:interval,categories};
    }
    async save(){
      if(this.pending||this.stopped)return;
      let settings;try{settings=this.settings();}catch(error){this.notice(error.message,true);return;}
      return this.mutate('/trends-settings',settings,true);
    }
    async update(){
      if(this.pending||this.stopped||!this.state?.settings.enabled||this.state.state==='refreshing')return;
      return this.mutate('/trends-refresh',{},false);
    }
    async mutate(path,payload,saved) {
      this.pending=true;const version=++this.version;this.controls();
      try {
        const state=await this.request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        if(this.stopped||version!==this.version)return;
        if(saved)this.dirty=false;
        this.render(state);
        this.notice(saved?'话题设置已保存。':state.state==='refreshing'?'已开始更新，完成后会显示新素材。':'已请求更新，状态以页面显示为准。');
      }catch(error){
        if(!this.stopped&&version===this.version)this.notice(error.name==='AbortError'?'操作结果未确认，输入已保留。请先刷新状态再决定是否重试。':error.message,true);
      }finally{this.pending=false;if(!this.stopped){this.controls();this.schedule();}}
    }
    render(state,unconfirmed=false) {
      this.state=state;
      if(!this.dirty){
        this.el('trends-enabled').checked=state.settings.enabled;
        this.el('trends-interval').value=state.settings.interval_hours??6;
        for(const key of Object.keys(CATEGORIES))this.el('trends-'+key).checked=state.settings.categories.includes(key);
      }
      this.el('trends-status').textContent=typeof state.message==='string'&&state.message.trim()?state.message.slice(0,500):STATES[state.state];
      const next=state.settings.enabled&&epoch(state.next_refresh_at)?date(state.next_refresh_at):state.settings.enabled?'等待调度':'已关闭';
      this.el('trends-times').textContent='上次成功更新：'+date(state.last_success_at)+' · 下次检查：'+next+'（香港时间）';
      const list=this.el('trends-list');list.replaceChildren();
      const fresh=state.stale!==true&&state.state!=='stale'&&epoch(state.last_success_at)>this.clock()-86400;
      const topics=fresh&&Array.isArray(state.topics)?state.topics.filter(topic=>topic&&Object.hasOwn(CATEGORIES,topic.category)&&epoch(topic.expires_at)>this.clock()&&Array.isArray(topic.sources)&&topic.sources.some(source=>sourceURL(source?.url))).slice(0,12):[];
      if(!state.settings.enabled){list.appendChild(this.node('p','话题更新已关闭。现有缓存不会被当作新话题主动带入聊天。','empty'));}
      else if(!topics.length){list.appendChild(this.node('p',state.state==='empty'?'这次已经查过，暂时没有符合范围的新素材。可以继续原来的话题，等待下一次更新。':state.stale===true||state.state==='stale'?'旧素材已过期，重新更新后再聊。':'还没有可用的新素材。更新失败或没有合适话题时，不会编造热点。','empty'));}
      else {
        if(unconfirmed||['refreshing','failed','blocked','waiting'].includes(state.state))list.appendChild(this.node('p','以下是上次读取、仍在有效期内的缓存；不是本次刚查到的结果。','hint'));
        for(const topic of topics){
          const sources=Array.isArray(topic.sources)?topic.sources.map(source=>({source,url:sourceURL(source?.url)})).filter(item=>item.url).slice(0,5):[];
          if(!sources.length)continue;
          const row=this.node('article','','entry');
          row.appendChild(this.node('h3',String(topic.title||'').slice(0,180)));
          row.appendChild(this.node('p',CATEGORIES[topic.category]+' · 资料日期：'+date(topic.published_at),'meta'));
          row.appendChild(this.node('p',String(topic.summary||'').slice(0,1200),'intro'));
          if(topic.talking_point)row.appendChild(this.node('p','可以接着聊：'+String(topic.talking_point).slice(0,600),'hint'));
          const links=this.node('div','','toolbar');
          for(const {source,url} of sources){const link=this.node('a',String(source.title||'查看来源').slice(0,160));link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.referrerPolicy='no-referrer';links.appendChild(link);}
          row.appendChild(links);list.appendChild(row);
        }
      }
      this.controls();
    }
  }
  const api={TrendingTopicsPage,sourceURL,epoch};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  root.TrendingTopicsSettings=api;
  if(root.document?.getElementById('trends-form')){const page=new TrendingTopicsPage({document:root.document,fetcher:root.fetch.bind(root),window:root});root.trendingTopicsPage=page;page.refresh();}
})(typeof window!=='undefined'?window:globalThis);
