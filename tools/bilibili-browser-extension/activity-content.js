(function(root) {
  'use strict';
  function install(doc,win,runtime,a) {
    if(win!==win.top)return;
    const token=new URL(win.location.href).hash.match(/^#ai-vtuber-activity=([0-9a-f-]+)$/i)?.[1];
    if(!a.UUID.test(token||''))return;
    let owned=false,stopped=false,committed=false,prepared=null,ownership;
    const interaction=root.VtuberBilibiliInteraction||(typeof require==='function'&&require('./activity-interaction.js'));
    const timers=new Set();
    const sleep=ms=>new Promise(resolve=>{const id=win.setTimeout(()=>{timers.delete(id);resolve();},ms);timers.add(id);});
    const active=deadline=>owned&&!stopped&&Date.now()<deadline;
    function hush() {
      if(!owned)return;
      for(const media of doc.querySelectorAll('video,audio')) {media.muted=true;media.defaultMuted=true;media.autoplay=false;if(!media.paused)media.pause();}
    }
    for(const event of ['play','playing','loadedmetadata','volumechange'])doc.addEventListener(event,hush,true);
    const observer=new win.MutationObserver(hush);observer.observe(doc,{childList:true,subtree:true});
    async function waitFor(deadline,read,{empty=null}={}) {
      let last='',stableAt=0;
      while(active(deadline)) {
        const blocked=a.pageBlock(doc);if(blocked)throw blocked;
        const result=read();
        if(result) {
          const next=JSON.stringify(result);
          if(next!==last){last=next;stableAt=Date.now();}
          else if(Date.now()-stableAt>=750)return result;
        }
        if(empty?.())return null;
        await sleep(200);
      }
      throw 'timeout';
    }
    async function run(message) {
      const {operation,payload:p,deadline}=message;
      await ownership;
      if(!active(deadline))throw 'cancelled';
      const blocked=a.pageBlock(doc);if(blocked)throw blocked;
      const localDeadline=Math.min(deadline,Date.now()+15000);
      if(operation==='account')return waitFor(localDeadline,()=>a.account(doc));
      if(operation==='search')return waitFor(localDeadline,()=>{const items=a.search(doc);return items.length?{items}:null;},
        {empty:()=>a.nodes(doc,'.search-empty,.no-data,.search-no-result').length>0}).then(value=>value||{items:[]});
      if(operation==='read')return waitFor(localDeadline,()=>a.read(doc,win.location.href));
      if(operation==='interaction-prepare') {
        await waitFor(localDeadline,()=>a.account(doc)?{ready:true}:null);
        const result=interaction.prepare(doc,win,p,a);
        prepared=JSON.stringify(p);return result;
      }
      if(operation==='interaction-commit') {
        if(committed||prepared!==JSON.stringify(p))throw 'unsafe_composer';
        const permit=await runtime.sendMessage({channel:a.CHANNEL,kind:'permit',token});
        if(committed||!permit?.allowed||!active(deadline))throw 'cancelled';
        committed=true;interaction.click(doc,win,p,a);
        await sleep(1200);return {submitted:true};
      }
      if(operation==='interaction-proof') {
        return await waitFor(localDeadline,()=>{
          const result=interaction.receipt(doc,win.location.href,{...p,_now:Date.now()},a);
          return result.status==='completed'?result:null;
        }).catch(code=>{if(code==='timeout')return {status:'unknown'};throw code;});
      }
      if(operation==='preflight')return waitFor(localDeadline,()=>{
        const who=a.account(doc);if(who&&who.uid!==p.expected_uid)throw 'account_mismatch';
        return a.timeline(doc,win.location.href,p);
      }).then(value=>({scanned:value.scanned,existing:value.existing}));
      if(operation==='candidate') {
        const found=await waitFor(localDeadline,()=>{
          const who=a.account(doc);if(who&&who.uid!==p.expected_uid)throw 'account_mismatch';
          return a.candidate(doc,win.location.href,p)?{unique:true}:null;
        }).catch(code=>{if(code==='timeout')return null;throw code;});
        return found||{unique:false};
      }
      if(operation==='open-candidate') {
        const match=a.candidate(doc,win.location.href,p);if(!match)return {clicked:false};
        match.time.click();return {clicked:true};
      }
      if(operation==='detail') {
        const matched=await waitFor(localDeadline,()=>a.detailMatches(doc,p.text)?{matches:true}:null)
          .catch(code=>{if(code==='timeout')return null;throw code;});
        const who=a.account(doc);if(who&&who.uid!==p.expected_uid)throw 'account_mismatch';
        const author=a.author(doc);return {matches:!!matched,author_uid:author.uid,can_open_author:!!author.node};
      }
      if(operation==='open-author') {
        const author=a.author(doc);if(!a.detailMatches(doc,p.text)||!author.node)return {clicked:false};
        author.node.click();return {clicked:true};
      }
      if(operation==='prepare') {
        await waitFor(localDeadline,()=>a.account(doc)&&a.nodes(doc,'.bili-dyn-publishing').length?{ready:true}:null);
        const {editor}=a.composer(doc,p);editor.focus();
        editor.textContent=p.text;
        editor.dispatchEvent(new win.InputEvent('input',{bubbles:true,inputType:'insertText',data:p.text}));
        await sleep(300);if(!active(deadline))throw 'cancelled';a.composer(doc,p,{filled:true});
        prepared=JSON.stringify(p);return {prepared:true};
      }
      if(operation==='commit') {
        if(committed||prepared!==JSON.stringify(p))throw 'unsafe_composer';
        const permit=await runtime.sendMessage({channel:a.CHANNEL,kind:'permit',token});
        if(committed||!permit?.allowed||!active(deadline))throw 'cancelled';
        const {button}=a.composer(doc,p,{filled:true});
        // Set the in-document barrier before the click; the worker has already
        // persisted its independent barrier. No catch/retry ever clicks again.
        committed=true;button.click();await sleep(1200);
        return {submitted:true};
      }
      throw 'invalid_request';
    }
    runtime.onMessage.addListener((message,_sender,reply)=>{
      if(message?.channel!==a.CHANNEL||message.token!==token)return false;
      if(message.kind==='stop'){stopped=true;return false;}
      if(message.kind!=='run')return false;
      run(message).then(result=>reply({ok:true,result})).catch(code=>reply(a.error(code)));return true;
    });
    ownership=runtime.sendMessage({channel:a.CHANNEL,kind:'hello',token}).then(value=>{owned=value?.owned===true;hush();}).catch(()=>{});
    win.addEventListener('pagehide',()=>{stopped=true;observer.disconnect();});
  }
  if(typeof module!=='undefined')module.exports={install};else install(document,window,chrome.runtime,root.VtuberBilibiliActivity);
})(globalThis);
