(function(root) {
  'use strict';
  const a=root.VtuberBilibiliActivity||(typeof require==='function'&&require('./activity.js'));
  const unknown=()=>({ok:true,result:{status:'unknown'}});
  async function fingerprint(payload) {
    const bytes=new TextEncoder().encode(JSON.stringify([payload.expected_uid,payload.text]));
    return Array.from(new Uint8Array(await root.crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
  }
  class ActivityWorker {
    constructor(api,{randomId=()=>root.crypto.randomUUID(),hash=fingerprint,now=Date.now,
      setTimer=(fn,ms)=>root.setTimeout(fn,ms),clearTimer=id=>root.clearTimeout(id),reader=null}={}) {
      this.api=api;this.randomId=randomId;this.hash=hash;this.now=now;this.setTimer=setTimer;this.clearTimer=clearTimer;
      this.active=null;this.ledger={};this.owned=null;this.reader=reader;
      this.ready=api.storage.local.get('activityPublishLedger').then(saved=>{this.ledger=saved.activityPublishLedger||{};});
      api.runtime.onMessage.addListener((message,sender,reply)=>{
        if(message?.channel!==a.CHANNEL)return false;
        this.handle(message,sender).then(reply).catch(()=>reply(a.error('unavailable')));return true;
      });
      api.tabs.onRemoved.addListener(id=>{
        const job=this.active;
        if(job&&(id===job.owner.tab||id===job.tab))this.finish(job,job.attempted?unknown():a.error('cancelled'));
      });
      api.tabs.onCreated.addListener(tab=>{
        const job=this.active;if(!job?.navigation||tab.openerTabId!==job.tab)return;
        // Only children created during the explicitly armed read-only time
        // click are considered. No existing user tabs are queried or reused.
        job.navigation.children.add(tab.id);this.navigationUpdate(job,tab.id,tab.pendingUrl||tab.url);
      });
      api.tabs.onUpdated.addListener((id,change,tab)=>{
        const job=this.active;if(job?.navigation)this.navigationUpdate(job,id,change.url||tab?.url);
      });
    }
    current(job) {return this.active===job&&!job.finished&&this.now()<job.deadline;}
    requireCurrent(job) {if(!this.current(job))throw 'cancelled';}
    async handle(message,sender) {
      if(message.kind==='hello'||message.kind==='permit')return this.collectorMessage(message,sender);
      if(sender.frameId!==0||!sender.documentId||!a.local(sender.url)||!Number.isInteger(sender.tab?.id))return a.error('invalid_request');
      if(message.kind==='cancel') {
        const job=this.active;
        if(job&&job.owner.tab===sender.tab.id&&job.owner.document===sender.documentId&&job.command.request_id===message.request_id)
          this.finish(job,job.attempted?unknown():a.error('cancelled'));
        return {ok:true};
      }
      const command=message.kind==='request'&&a.validate(message);if(!command)return a.error('invalid_request');
      await this.ready;
      if(this.active)return a.error('busy');
      let done;const result=new Promise(resolve=>{done=resolve;});
      const job={command,owner:{tab:sender.tab.id,document:sender.documentId,url:sender.url},
        token:this.randomId(),tab:null,primary:null,document:null,deadline:this.now()+50000,done,finished:false,attempted:false,children:new Set()};
      this.active=job;
      try {job.timer=this.setTimer(()=>this.finish(job,job.attempted||command.action==='verify'?unknown():a.error('timeout')),50000);}
      catch {this.active=null;return a.error('unavailable');}
      this.execute(job).then(value=>this.finish(job,value)).catch(code=>this.finish(job,job.attempted?unknown():a.error(code)));
      return result;
    }
    finish(job,result) {
      if(job.finished)return;job.finished=true;try{this.clearTimer(job.timer);}catch{}
      if(this.active===job)this.active=null;
      job.readController?.abort();
      job.waitReady?.reject('cancelled');job.navigation?.reject('cancelled');
      if(job.tab!==null)Promise.resolve().then(()=>this.api.tabs.sendMessage(job.tab,{channel:a.CHANNEL,kind:'stop',token:job.token})).catch(()=>{});
      if(job.primary!==null)this.owned={id:job.primary,token:job.token};
      // The main activity tab is kept for manual inspection. Only verified
      // read-navigation child tabs created by this operation are closed.
      for(const id of job.children)Promise.resolve().then(()=>this.api.tabs.remove(id)).catch(()=>{});
      job.done(result);
    }
    async collectorMessage(message,sender) {
      const job=this.active;
      if(!job||!this.current(job)||message.token!==job.token||sender.frameId!==0||sender.tab?.id!==job.tab||!sender.documentId)return {owned:false};
      const tab=await this.api.tabs.get(job.tab);if(!this.current(job)||tab.url!==sender.url||sender.url!==job.target)return {owned:false};
      if(message.kind==='permit')return {allowed:!!job.attempted&&job.document===sender.documentId&&job.commitSent};
      job.document=sender.documentId;job.waitReady?.resolve();job.waitReady=null;
      return {owned:true};
    }
    async open(job,target,{tabId=null}={}) {
      this.requireCurrent(job);job.document=null;
      if(tabId!==null)job.tab=tabId;
      if(job.tab===null) {
        if(this.owned) {
          const previous=await this.api.tabs.get(this.owned.id).catch(()=>null);this.requireCurrent(job);
          if(previous?.url?.endsWith(`#ai-vtuber-activity=${this.owned.token}`))job.tab=previous.id;
        }
        if(job.tab===null) {
          const tab=await this.api.tabs.create({url:'about:blank',active:false});job.tab=tab.id;
          if(!this.current(job)){Promise.resolve(this.api.tabs.remove(tab.id)).catch(()=>{});throw 'cancelled';}
        }
        job.primary=job.tab;this.requireCurrent(job);
      }
      await this.api.tabs.update(job.tab,{muted:true});this.requireCurrent(job);
      // Changing only a hash does not inject a new content script. Reset our
      // own tab first, including a newly opened detail tab whose initial page
      // intentionally had no ownership token.
      await this.api.tabs.update(job.tab,{url:'about:blank'});this.requireCurrent(job);
      job.target=`${target}#ai-vtuber-activity=${job.token}`;
      const ready=new Promise((resolve,reject)=>{job.waitReady={resolve,reject};});
      // Attach a rejection handler immediately: cancellation can happen while
      // tabs.update itself is still pending.
      ready.catch(()=>{});
      await this.api.tabs.update(job.tab,{url:job.target});this.requireCurrent(job);await ready;this.requireCurrent(job);
    }
    async run(job,operation,payload={}) {
      this.requireCurrent(job);
      const value=await this.api.tabs.sendMessage(job.tab,{channel:a.CHANNEL,kind:'run',token:job.token,
        operation,payload,deadline:job.deadline},{documentId:job.document});
      this.requireCurrent(job);
      if(!value?.ok)throw value?.error?.code||'unsupported_page';return value.result;
    }
    async rememberAttempt(job) {
      const p=job.command.payload;
      if(Object.keys(this.ledger).length>=512)throw 'ledger_full';
      // Persist BEFORE dispatching the only operation that may click publish.
      // Plaintext drafts and cookies are deliberately absent from this ledger.
      this.ledger[p.draft_id]={request_id:job.command.request_id,fingerprint:job.fingerprint,status:'attempted',attempted_at:this.now()};
      await this.api.storage.local.set({activityPublishLedger:this.ledger});
      this.requireCurrent(job);job.attempted=true;
    }
    navigationUpdate(job,id,target) {
      const nav=job.navigation;if(!nav||!this.current(job)||!(id===job.tab||nav.children.has(id)))return;
      const parsed=nav.kind==='author'?(a.profileUID(target)?{uid:a.profileUID(target),url:`https://space.bilibili.com/${a.profileUID(target)}`} :null):a.dynamicURL(target);
      if(!parsed)return;
      if(id!==job.tab)job.children.add(id);
      nav.resolve({id,...parsed});job.navigation=null;
    }
    async follow(job,kind,operation,payload) {
      const navigation=new Promise((resolve,reject)=>{job.navigation={resolve,reject,kind,children:new Set()};});
      navigation.catch(()=>{});
      let opened;try{opened=await this.run(job,operation,payload);}catch{opened=null;}
      if(opened&&opened.clicked!==true){job.navigation=null;return null;}
      const destination=await navigation;this.requireCurrent(job);return destination;
    }
    async verify(job) {
      const p=job.command.payload;
      const existing=this.ledger[p.draft_id];
      const fingerprint=await this.hash(p);this.requireCurrent(job);
      if(existing&&existing.fingerprint!==fingerprint)throw 'draft_conflict';
      const attemptedAt=Number.isFinite(existing?.attempted_at)&&existing.attempted_at>0?existing.attempted_at:null;
      // After reinstalling/clearing extension storage, an arbitrary old post
      // with the same text cannot prove that this draft was just published.
      if(!attemptedAt&&!p.url)return {status:'unknown'};
      const evidence={...p,...(attemptedAt?{_since:attemptedAt}:{})};
      await this.open(job,`https://space.bilibili.com/${p.expected_uid}/dynamic`);
      const found=await this.run(job,'candidate',evidence);if(!found?.unique)return {status:'unknown'};
      // The content script rechecks identity and uniqueness immediately before
      // clicking this specific card's time. This click only opens a detail page.
      const destination=await this.follow(job,'dynamic','open-candidate',evidence);if(!destination)return {status:'unknown'};
      if(p.url&&a.dynamicURL(p.url).dynamic_id!==a.dynamicURL(destination.url).dynamic_id)return {status:'unknown'};
      await this.open(job,destination.url,{tabId:destination.id});
      const detail=await this.run(job,'detail',p);
      if(detail?.matches!==true)return {status:'unknown'};
      let authorUID=detail.author_uid;
      if(!authorUID&&detail.can_open_author)authorUID=(await this.follow(job,'author','open-author',p))?.uid;
      return a.proof(p,destination.url,authorUID===p.expected_uid);
    }
    async execute(job) {
      const {action,payload:p}=job.command;
      if(action==='interact'||action==='verify_interaction') {
        const interaction=root.VtuberBilibiliInteraction||(typeof require==='function'&&require('./activity-interaction.js'));
        return interaction.execute(this,job,a);
      }
      if(action==='search') {
        await this.open(job,`https://search.bilibili.com/all?keyword=${encodeURIComponent(p.query)}`);
        return {ok:true,result:await this.run(job,'search')};
      }
      if(action==='read') {
        await this.open(job,p.url);const baseline=await this.run(job,'read');
        if(!this.reader)return {ok:true,result:baseline};
        job.readController=new AbortController();
        const result=await this.reader.enrich(baseline,{signal:job.readController.signal});
        this.requireCurrent(job);return {ok:true,result};
      }
      if(action==='verify')return {ok:true,result:await this.verify(job)};
      if(action==='publish') {
        job.fingerprint=await this.hash(p);this.requireCurrent(job);
        const old=this.ledger[p.draft_id];
        const sameRequest=Object.entries(this.ledger).find(([,entry])=>entry.request_id===job.command.request_id);
        if(sameRequest&&sameRequest[0]!==p.draft_id)throw 'draft_conflict';
        if(old) {
          if(old.fingerprint!==job.fingerprint)throw 'draft_conflict';
          return old.status==='published'?{ok:true,result:{status:'published',uid:p.expected_uid,text:p.text,dynamic_id:old.dynamic_id,url:old.url}}:unknown();
        }
        await this.open(job,`https://space.bilibili.com/${p.expected_uid}/dynamic`);
        const baseline=await this.run(job,'preflight',p);
        if(!baseline?.scanned)throw 'unsupported_page';if(baseline.existing)throw 'existing_text';
      }
      await this.open(job,'https://t.bilibili.com/');
      if(action==='account')return {ok:true,result:await this.run(job,'account')};
      await this.run(job,'prepare',p);await this.rememberAttempt(job);this.requireCurrent(job);
      job.commitSent=true;
      await this.run(job,'commit',p);
      const verified=await this.verify(job);
      if(verified.status==='published') {
        const entry=this.ledger[p.draft_id];Object.assign(entry,{status:'published',dynamic_id:verified.dynamic_id,url:verified.url});
        await this.api.storage.local.set({activityPublishLedger:this.ledger});
      }
      return {ok:true,result:verified};
    }
  }
  if(typeof module!=='undefined')module.exports={ActivityWorker,fingerprint};
  else new ActivityWorker(chrome,{reader:new root.VtuberBiliReader.Reader()});
})(globalThis);
