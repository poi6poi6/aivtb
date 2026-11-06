/* Page navigation plus an owned, read-only video evidence adapter. */
if (typeof importScripts === 'function') importScripts('common.js', 'activity.js', 'activity-reader.js', 'activity-interaction.js', 'activity-background.js');
(function(root) {
  'use strict';
  const common = root.VtuberBrowserCommon || require('./common.js');
  const Reader = (root.VtuberBiliReader || require('./activity-reader.js')).Reader;
  function secureRandomId() {
    const cryptoApi=root.crypto;
    if(typeof cryptoApi?.randomUUID === 'function') {
      try {
        const id=cryptoApi.randomUUID();
        if(/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(id))return id.toLowerCase();
      } catch { /* Fall back only to another cryptographically secure source. */ }
    }
    if(typeof cryptoApi?.getRandomValues === 'function') {
      try {
        const bytes=new Uint8Array(16);cryptoApi.getRandomValues(bytes);
        bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
        const hex=Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');
        return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
      } catch { /* No Math.random/time-based IDs: fail closed below. */ }
    }
    throw {stage:'random_id',reason:'secure_random_unavailable'};
  }
  class BrowserQueries {
    constructor(api, {randomId=secureRandomId,
      setTimer=(fn,delay)=>root.setTimeout(fn,delay),clearTimer=id=>root.clearTimeout(id),reader=null}={}) {
      this.api=api; this.randomId=randomId; this.setTimer=setTimer; this.clearTimer=clearTimer;
      this.connected={}; this.owned=null; this.active=null;
      this.reader=reader;this.reads=new Map();
      this.openQueue=Promise.resolve();
      this.ready=this.step('session_load',()=>api.storage.session.get(['connected','owned']),'storage_error').then(saved => {
        this.connected=saved.connected || {}; this.owned=saved.owned || null;
      });
      api.runtime.onMessage.addListener((message,sender,reply) => {
        if (message?.channel !== common.channel) return false;
        this.handle(message,sender).then(reply).catch(error => reply(this.failure(error)));
        return true;
      });
      api.tabs.onRemoved.addListener(tabId => {
        this.cancelRead(tabId);
        delete this.connected[tabId];
        if (this.active?.owner === tabId || this.owned?.id === tabId) this.finish(common.error('cancelled'));
        if (this.owned?.id === tabId) this.owned=null;
        this.save().catch(() => {});
      });
    }
    tagged(cause,stage,fallback='chrome_api_error') {
      return {stage,reason:common.errorReason(cause,fallback)};
    }
    async step(stage,action,fallback='chrome_api_error') {
      try { return await action(); }
      catch(cause) { throw this.tagged(cause,stage,fallback); }
    }
    failure(error) {
      return common.error('page_unavailable',{
        stage:common.stages.has(error?.stage)?error.stage:'request_dispatch',
        reason:common.reasons.has(error?.reason)?error.reason:'unexpected_error',
      });
    }
    save(stage='tab_save') {
      return this.step(stage,()=>this.api.storage.session.set({connected:this.connected, owned:this.owned}),'storage_error');
    }
    current(job) { return this.active === job; }
    cancelRead(owner,id=null) {
      const job=this.reads.get(owner);
      if(job&&(!id||job.id===id)) {this.reads.delete(owner);job.controller.abort();}
    }
    async readVideo(message,owner,identity) {
      if(typeof message.url!=='string'||!/^https:\/\/www\.bilibili\.com\/video\/BV[A-Za-z0-9]{10}(?:\?p=[1-9][0-9]{0,4})?$/.test(message.url)
        ||Number(new URL(message.url).searchParams.get('p')||1)>10000)return common.error('invalid_request');
      this.cancelRead(owner);
      const job={id:message.id,controller:new AbortController()};this.reads.set(owner,job);
      try {
        this.reader ||= new Reader();
        const data=await this.reader.readVideo(message.url,{signal:job.controller.signal});
        const permission=this.connected[owner];
        if(this.reads.get(owner)!==job||job.controller.signal.aborted||permission?.origin!==identity.origin
          ||permission?.documentId!==identity.documentId)return common.error('cancelled');
        return {ok:true,data};
      } catch {return common.error(job.controller.signal.aborted?'cancelled':'page_unavailable');}
      finally {if(this.reads.get(owner)===job)this.reads.delete(owner);}
    }
    stopCollector(job) {
      if (!job || !this.owned)return;
      try {
        Promise.resolve(this.api.tabs.sendMessage(this.owned.id,
          {channel:common.channel, action:'cancel', jobId:job.jobId})).catch(() => {});
      } catch { /* Cleanup is best effort, including an invalidated context. */ }
    }
    finish(result) {
      const job=this.active;
      if (!job) return;
      this.active=null;
      // Resolving the operation must not depend on a still-existing tab or a
      // working extension context. Late callbacks also see active=null.
      job.resolve(result);
      try { this.clearTimer(job.timer); } catch { /* The active guard still prevents late effects. */ }
      try { this.stopCollector(job); } catch { /* Cleanup cannot invalidate success/cancellation. */ }
    }
    async handle(message,sender) {
      await this.ready;
      const tabId=sender.tab?.id;
      if (!Number.isInteger(tabId) || sender.frameId !== 0) return common.error('invalid_request');
      if (message.action === 'collector-hello' || message.action === 'collector-result') {
        const owned=this.owned;
        if (!owned || owned.id !== tabId || message.jobId !== owned.jobId
            || common.jobId(sender.url) !== owned.jobId) return {owned:false};
        const rejectPage=reason=>{
          if(this.active?.jobId === owned.jobId)this.finish(common.error('page_unavailable',{stage:'page_check',reason}));
          return {owned:false};
        };
        if(!common.samePage(sender.url,owned.url,owned.jobId))return rejectPage('page_changed');
        let live;
        try { live=await this.api.tabs.get(tabId); }
        catch(error) { return rejectPage(common.errorReason(error)); }
        if(this.owned !== owned)return {owned:false};
        if(!common.samePage(live.url,owned.url,owned.jobId))return rejectPage('page_changed');
        const job=this.active;
        if (message.action === 'collector-hello') return {owned:true,
          collect:job?.jobId === owned.jobId ? {jobId:job.jobId, kind:job.target.kind} : null};
        if (job?.jobId === message.jobId) this.finish(this.cleanResult(message.result,job,sender.url));
        return {owned:true};
      }
      if (!common.local(sender.url) || !common.validId(message.id)) return common.error('invalid_request');
      const identity={origin:new URL(sender.url).origin, documentId:sender.documentId || ''};
      if (message.action === 'connect') {
        if(this.connected[tabId]?.documentId!==identity.documentId)this.cancelRead(tabId);
        this.connected[tabId]=identity; await this.save('connect_save');
        return {ok:true, data:{connected:true, version:1,extension_version:this.api.runtime.getManifest().version,
          capabilities:['video_subtitles_v1']}};
      }
      const permission=this.connected[tabId];
      if (!permission || permission.origin !== identity.origin || permission.documentId !== identity.documentId)
        return common.error('not_connected');
      if (message.action === 'cancel') {
        this.cancelRead(tabId,message.id);
        if (this.active?.owner === tabId && this.active.id === message.id) this.finish(common.error('cancelled'));
        return {ok:true, data:{cancelled:true}};
      }
      if(message.action==='read_video')return this.readVideo(message,tabId,identity);
      if (message.action !== 'search') return common.error('invalid_request');
      let target;
      try { target=common.target(message); } catch { return common.error('invalid_request'); }
      let jobId;
      try { jobId=this.randomId(); }
      catch(error) { throw {stage:'random_id',reason:'secure_random_unavailable'}; }
      this.finish(common.error('cancelled'));
      return new Promise(resolve => {
        const job={owner:tabId, id:message.id, jobId, target, resolve,stage:'timer_setup'};
        this.active=job;
        try {
          job.timer=this.setTimer(() => { if(this.current(job)) this.finish(common.error('timeout',{stage:job.stage,reason:'deadline'})); },25000);
        } catch {
          this.finish(common.error('page_unavailable',{stage:'timer_setup',reason:'timer_unavailable'}));
          return;
        }
        this.openQueue=this.openQueue.then(() => this.open(job))
          .catch(error => { if(this.current(job)) this.finish(this.failure(error)); });
      });
    }
    cleanResult(result,job,actualUrl) {
      if (!result?.ok) {
        const code=result?.error?.code;
        return common.error(['verification_required','login_required','page_unavailable','timeout'].includes(code)
          ? code : 'page_unavailable',{stage:'read_page',reason:code==='timeout'?'deadline':'blocked_page'});
      }
      if (!Array.isArray(result.data?.results)) return common.error('page_unavailable',{stage:'result_shape',reason:'invalid_result'});
      if(!common.samePage(result.data.page_url,job.target.url,job.jobId))return common.error('page_unavailable',{stage:'page_check',reason:'page_changed'});
      // The public page URL never includes the private extension correlation ID.
      const page=new URL(actualUrl);page.hash='';
      const data={page_url:page.href,results:result.data.results.slice(0,30).map(item => ({
        bvid:String(item.bvid || '').slice(0,12), title:String(item.title || '').slice(0,250),
        author:String(item.author || '').slice(0,100), duration:String(item.duration || '').slice(0,30),
        cover:String(item.cover || '').slice(0,1000), play_text:String(item.play_text || '').slice(0,64),
      })).filter(item => /^BV[A-Za-z0-9]{10}$/.test(item.bvid) && item.title)};
      if(data.results.some(item=>!item.play_text)) {
        const diagnostic=common.cleanStatsDiagnostic(result.data.stats_diagnostic);
        if(diagnostic)data.stats_diagnostic=diagnostic;
      }
      return {ok:true,data};
    }
    async open(job) {
      if (!this.current(job)) return;
      let tab;
      if (this.owned) {
        try {
          const existing=await this.api.tabs.get(this.owned.id);
          // A user who navigates this tab elsewhere has taken it back.
          if (common.jobId(existing.url) === this.owned.jobId || existing.url === 'about:blank') tab=existing;
        } catch { /* The user may have closed the previous query tab. */ }
      }
      if (!this.current(job)) return;
      if (!tab) {
        job.stage='tab_create';
        tab=await this.step(job.stage,()=>this.api.tabs.create({url:'about:blank',active:false}));
      }
      this.owned={id:tab.id,jobId:job.jobId,url:job.target.url};
      job.stage='tab_save';
      await this.save();
      if (!this.current(job)) return;
      // Muting precedes navigation, including autoplay in nested players.
      job.stage='tab_mute';
      await this.step(job.stage,()=>this.api.tabs.update(tab.id,{muted:true}));
      if (!this.current(job)) return;
      // The same keyword may be requested again. A fresh document avoids a
      // hash-only navigation leaving the previous collector/SPA cards alive.
      job.stage='tab_reset';
      await this.step(job.stage,()=>this.api.tabs.update(tab.id,{url:'about:blank'}));
      if (!this.current(job)) return;
      job.stage='tab_navigate';
      await this.step(job.stage,()=>this.api.tabs.update(tab.id,{url:job.target.url+'#ai-vtuber-job='+job.jobId}));
      if(this.current(job))job.stage='read_page';
    }
  }
  if (typeof module !== 'undefined') module.exports={BrowserQueries,secureRandomId};
  else new BrowserQueries(chrome);
})(globalThis);
