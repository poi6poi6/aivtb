(function(root) {
  'use strict';
  const STATUS = {pending:'等待合适时机',claimed:'正在准备',delivered:'已确认送达',cancelled:'已取消',failed:'未完成',unknown:'送达状态待确认',expired:'已过期'};
  const MAX_IMAGE = 5 * 1024 * 1024;
  function imageFile(file) {
    if (!file || !['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)) throw Error('请选择 PNG、JPEG、GIF 或 WebP 图片。');
    if (!file.size || file.size > MAX_IMAGE) throw Error('图片为空或超过 5 MiB，请缩小后重试。');
    return file;
  }
  function memeURL(id) { return typeof id==='string' && /^[a-z][a-z0-9_-]{1,63}$/.test(id) ? '/api/companion/memes/'+id : null; }
  function hongKongInput(timestamp) { return Number.isFinite(timestamp) && timestamp>0 ? new Date(timestamp*1000+8*3600000).toISOString().slice(0,16) : ''; }
  function hongKongEpoch(value) { const ms=Date.parse(value+':00+08:00'); if(!Number.isFinite(ms))throw Error('请选择有效的日程时间。'); return ms/1000; }
  function date(timestamp) { return Number.isFinite(timestamp)&&timestamp>0 ? new Date(timestamp*1000).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong',hour12:false}) : '未记录'; }
  function tags(text) { return String(text || '').split(/[,，、\s]+/).map(s=>s.trim()).filter(Boolean); }
  class CompanionPage {
    constructor({document,fetcher,window}) {
      this.document=document;this.fetcher=fetcher;this.window=window;this.dirty=new Set();this.drafts=new Map();this.pending=new Set();this.controllers=new Set();this.stopped=false;this.refreshVersion=0;
      for(const id of ['settings-form','vision-form','calendar-form','followup-form','meme-form']) {
        this.el(id).addEventListener('input',()=>this.dirty.add(id));
        this.el(id).addEventListener('change',()=>this.dirty.add(id));
      }
      this.el('settings-form').onsubmit=e=>{e.preventDefault();return this.save('settings-form',{action:'settings',settings:{memes_enabled:this.el('memes-enabled').checked,auto_memes:this.el('auto-memes').checked,journal_enabled:this.el('journal-enabled').checked}});};
      this.el('vision-form').onsubmit=e=>{e.preventDefault();return this.save('vision-form',{action:'settings',settings:{vision:{enabled:this.el('vision-enabled').checked}}});};
      this.el('followup-form').onsubmit=e=>{e.preventDefault();return this.save('followup-form',{action:'followup_settings',settings:{enabled:this.el('followup-enabled').checked,quiet_start_hour:Number(this.el('quiet-start').value),quiet_end_hour:Number(this.el('quiet-end').value),min_idle_sec:Number(this.el('min-idle').value),min_gap_sec:Number(this.el('min-gap').value),daily_budget:Number(this.el('daily-budget').value)}});};
      this.el('calendar-form').onsubmit=e=>{e.preventDefault();try{return this.save('calendar-form',{action:'calendar_create',title:this.el('calendar-title-input').value.trim(),due_at:hongKongEpoch(this.el('calendar-due').value),user:this.el('calendar-user').value.trim(),respect_quiet_hours:this.el('calendar-quiet').checked},()=>{this.el('calendar-title-input').value='';this.el('calendar-due').value='';});}catch(error){this.notice(error.message,true);}};
      this.el('meme-form').onsubmit=e=>{e.preventDefault();return this.upload();};
      this.el('refresh').onclick=()=>this.refresh();
      window.addEventListener?.('pagehide',()=>{this.stopped=true;for(const controller of this.controllers)controller.abort();});
    }
    el(id){return this.document.getElementById(id);}
    node(tag,text,cls){const n=this.document.createElement(tag);n.textContent=text||'';if(cls)n.className=cls;return n;}
    notice(text,error=false){this.el('notice').textContent=String(text||'');this.el('notice').dataset.error=String(error);}
    async request(path,options={}) {
      const controller=new AbortController();this.controllers.add(controller);
      const timer=setTimeout(()=>controller.abort(),35000);
      try {
        const response=await this.fetcher(path,{cache:'no-store',credentials:'same-origin',...options,signal:controller.signal});
        let data;try{data=await response.json();}catch(_){throw Error(response.status===403?'此页面只供本机主控使用。':'本机服务暂不可用，请稍后重试。');}
        if(!response.ok)throw Error(typeof data?.error==='string'?data.error:'操作未完成，请稍后重试。');
        return data;
      } finally {clearTimeout(timer);this.controllers.delete(controller);}
    }
    busy(id,on){this.pending[on?'add':'delete'](id);this.el(id)?.querySelectorAll?.('button,input,select,textarea').forEach(control=>control.disabled=on);}
    async save(id,payload,onSuccess) {
      if(this.pending.has(id)||this.stopped)return;
      this.busy(id,true);
      try {
        await this.request('/api/companion/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        this.dirty.delete(id);this.drafts.delete(id);onSuccess?.();this.notice('已保存。');await this.refresh();
      }catch(error){this.notice(error.name==='AbortError'?'请求未确认，输入已保留。请先刷新状态再决定是否重试。':error.message,true);}
      finally{this.busy(id,false);}
    }
    async upload() {
      if(this.pending.has('meme-form'))return;
      try {
        const file=imageFile(this.el('meme-file').files?.[0]);
        const query=new URLSearchParams({caption:this.el('meme-caption').value.trim(),tags:this.el('meme-tags').value.trim(),filename:file.name});
        this.busy('meme-form',true);
        await this.request('/api/companion/memes?'+query,{method:'POST',headers:{'Content-Type':file.type},body:file});
        this.el('meme-file').value='';this.el('meme-caption').value='';this.el('meme-tags').value='';this.dirty.delete('meme-form');this.notice('图片已加入图库。');await this.refresh();
      }catch(error){this.notice(error.name==='AbortError'?'上传未确认，图片和说明已保留。请先刷新图库。':error.message,true);}
      finally{this.busy('meme-form',false);}
    }
    async refresh() {
      if(this.stopped)return;this.el('refresh').disabled=true;const version=++this.refreshVersion;
      try {const state=await this.request('/api/companion');if(version!==this.refreshVersion||this.stopped)return;if(!state||typeof state!=='object'||!state.settings)throw Error('状态不完整，请稍后刷新。');this.render(state);this.el('connection').textContent='已连接本机主控';}
      catch(error){if(version!==this.refreshVersion||this.stopped)return;this.el('connection').textContent='本机状态暂不可用';this.notice(error.name==='AbortError'?'读取状态超时，未更改表单内容。':error.message,true);}
      finally{if(version===this.refreshVersion&&!this.stopped)this.el('refresh').disabled=false;}
    }
    fill(form,values) {if(this.dirty.has(form))return;for(const [id,value] of Object.entries(values)){const el=this.el(id);if(typeof value==='boolean')el.checked=value;else el.value=value??'';}}
    render(state) {
      const settings=state.settings||{},vision=settings.vision||{},f=state.followups?.settings||{},mood=state.mood||{};
      this.fill('settings-form',{'memes-enabled':!!settings.memes_enabled,'auto-memes':!!settings.auto_memes,'journal-enabled':!!settings.journal_enabled});
      this.fill('vision-form',{'vision-enabled':!!vision.enabled});
      this.fill('followup-form',{'followup-enabled':!!f.enabled,'quiet-start':f.quiet_start_hour??23,'quiet-end':f.quiet_end_hour??8,'min-idle':f.min_idle_sec??90,'min-gap':f.min_gap_sec??300,'daily-budget':f.daily_budget??8});
      this.el('vision-status').textContent=state.vision?.message||'图片理解状态暂不可用';
      this.el('vision-backend').textContent='本机 Codex 套餐 · '+(state.vision?.model||'gpt-6-astra');
      this.el('mood-label').textContent=mood.enabled===false?'尚未启用':mood.label||'尚未记录';
      for(const field of ['valence','arousal'])this.el('mood-'+field).textContent=Number.isFinite(mood[field])?Math.round(Math.max(0,Math.min(1,mood[field]))*100)+'%':'—';
      this.el('mood-detail').textContent='最后变化：'+date(mood.last_changed_at)+(mood.half_life_sec?' · 约每 '+(mood.half_life_sec/3600)+' 小时减半偏离基调的幅度':'');
      this.renderCalendar(Array.isArray(state.calendar)?state.calendar:[]);
      this.renderJournal(Array.isArray(state.journal)?state.journal:[]);
      this.renderMemes(Array.isArray(state.memes)?state.memes:[]);
      if(state.error||state.followups?.error)this.notice(state.error||state.followups.error,true);
    }
    field(label,value,type='text') {const wrap=this.node('label',label),input=this.document.createElement(type==='textarea'?'textarea':'input');if(type!=='textarea')input.type=type;input.value=value||'';wrap.appendChild(input);return [wrap,input];}
    button(label,fn,cls){const b=this.node('button',label,cls);b.type='button';b.onclick=fn;return b;}
    renderCalendar(items) {
      const list=this.el('calendar-list');list.replaceChildren();
      if(!items.length){list.appendChild(this.node('p','还没有约定。也可以在聊天里说“十分钟后提醒我喝水”。','empty'));return;}
      for(const item of items.slice(0,100)) {
        const id='calendar:'+item.id,draft=this.drafts.get(id)||{},row=this.node('div','','entry');row.id=id;
        row.appendChild(this.node('p',`${STATUS[item.status]||'状态待确认'} · ${item.user||'你'} · ${date(item.due_at)}（香港时间）`,'meta'));
        const [titleWrap,title]=this.field('约定内容',draft.title??item.title);title.maxLength=180;
        const [dueWrap,due]=this.field('改期为（香港时间 UTC+8）',draft.due??hongKongInput(item.due_at),'datetime-local');
        const remember=()=>this.drafts.set(id,{title:title.value,due:due.value});title.oninput=due.oninput=remember;
        row.appendChild(titleWrap);row.appendChild(dueWrap);
        row.appendChild(this.button('保存 / 重新安排',()=>{try{return this.save(id,{action:'calendar_update',id:item.id,title:title.value.trim(),due_at:hongKongEpoch(due.value)});}catch(error){this.notice(error.message,true);}}));
        if(['pending','claimed'].includes(item.status))row.appendChild(this.button('取消约定',()=>this.save(id,{action:'calendar_cancel',id:item.id}),'danger'));
        list.appendChild(row);
      }
    }
    renderJournal(items) {
      const list=this.el('journal-list');list.replaceChildren();
      if(!items.length){list.appendChild(this.node('p','还没有私人日记。完成对话后，有价值的互动会留在这里。','empty'));return;}
      for(const item of items.slice(0,100)) {
        const id='journal:'+item.id,row=this.node('div','','entry');row.id=id;
        row.appendChild(this.node('p',[item.day||item.date||date(item.created_at),item.user||'',item.title||'共同经历'].filter(Boolean).join(' · '),'meta'));
        const [wrap,input]=this.field('修订日记（最多1200字）',this.drafts.get(id)?.text??item.text??item.summary??item.body??'','textarea');input.maxLength=1200;
        input.oninput=()=>this.drafts.set(id,{text:input.value});row.appendChild(wrap);
        row.appendChild(this.button('保存修改',()=>this.save(id,{action:'journal_edit',id:item.id,text:input.value.trim()})));
        row.appendChild(this.button('删除这条日记',()=>this.save(id,{action:'journal_delete',id:item.id}),'danger'));list.appendChild(row);
      }
    }
    renderMemes(items) {
      const list=this.el('meme-list');list.replaceChildren();
      if(!items.length){list.appendChild(this.node('p','图库还没有图片。先加入一张她的表情吧。','empty'));return;}
      for(const item of items.slice(0,200)) {
        const url=memeURL(item.id);if(!url)continue;
        const id='meme:'+item.id,draft=this.drafts.get(id)||{},row=this.node('div','','entry');row.id=id;
        if(item.enabled!==false){const image=this.document.createElement('img');image.src=url;image.alt=String(item.caption||'图库表情');image.loading='lazy';row.appendChild(image);}
        else row.appendChild(this.node('p','已停用','meta'));
        const [captionWrap,caption]=this.field('说明',draft.caption??item.caption);caption.maxLength=120;
        const [tagsWrap,tagInput]=this.field('标签',draft.tags??(item.tags||[]).join('，'));tagInput.maxLength=400;
        const remember=()=>this.drafts.set(id,{caption:caption.value,tags:tagInput.value});caption.oninput=tagInput.oninput=remember;
        row.appendChild(captionWrap);row.appendChild(tagsWrap);
        row.appendChild(this.button('保存说明',()=>this.save(id,{action:'meme_update',id:item.id,caption:caption.value.trim(),tags:tags(tagInput.value)})));
        row.appendChild(item.enabled===false?this.button('重新启用',()=>this.save(id,{action:'meme_update',id:item.id,caption:caption.value.trim(),tags:tags(tagInput.value),enabled:true})):this.button('停用',()=>this.save(id,{action:'meme_remove',id:item.id}),'danger'));list.appendChild(row);
      }
    }
  }
  const api={CompanionPage,imageFile,memeURL,hongKongInput,hongKongEpoch};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  root.CompanionSettings=api;
  if(root.document?.getElementById('settings-form')) {const page=new CompanionPage({document:root.document,fetcher:root.fetch.bind(root),window:root});root.companionPage=page;page.refresh();}
})(typeof window!=='undefined'?window:globalThis);
