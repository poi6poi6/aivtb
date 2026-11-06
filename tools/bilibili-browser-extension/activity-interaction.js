/* Owned-tab interaction adapter. Uses visible controls, never exports cookies. */
(function(root) {
  'use strict';
  function roots(doc) {
    const out=[doc]; let scanned=0;
    if(doc.shadowRoot)out.push(doc.shadowRoot);
    for(let i=0;i<out.length&&i<40;i++)for(const node of out[i].querySelectorAll('*')) {
      if(++scanned>8000)return out;
      if(node.shadowRoot&&!out.includes(node.shadowRoot))out.push(node.shadowRoot);
    }
    return out;
  }
  function all(doc,selector,a) {
    return [...new Set(roots(doc).flatMap(r=>Array.from(r.querySelectorAll(selector))))].filter(a.visible);
  }
  function identity(doc,location,p,a) {
    if(a.videoURL(location)!==p.url)throw 'invalid_request';
    const who=a.account(doc);
    if(!who)throw 'login_required';
    if(who.uid!==p.expected_uid)throw 'account_mismatch';
  }
  function likeControl(doc,a) {
    const buttons=all(doc,'.video-toolbar-left .video-like,.video-toolbar .video-like',a);
    if(buttons.length!==1)throw 'unsupported_page';
    const node=buttons[0];
    if(node.disabled||node.getAttribute('aria-disabled')==='true')throw 'unsupported_page';
    const pressed=node.getAttribute('aria-pressed');
    return {node,liked:pressed==='true'||node.classList.contains('on')||node.classList.contains('active')};
  }
  function commentControl(doc,a) {
    const boxes=all(doc,'.reply-box,bili-comment-box',a);
    if(boxes.length!==1)throw 'unsupported_page';
    const editors=all(boxes[0],'textarea.reply-box-textarea,textarea,[contenteditable="true"]',a);
    const buttons=all(boxes[0],'.reply-box-send,.send-text,button',a)
      .filter(x=>/^(发布|发送|发表评论)$/.test(a.normalize(x.innerText)));
    if(editors.length!==1||buttons.length!==1)throw 'unsupported_page';
    if(all(boxes[0],'img.upload-image,.image-preview,.reply-upload-img',a).length)throw 'unsafe_composer';
    return {editor:editors[0],button:buttons[0]};
  }
  const textOf=editor=>editor.tagName==='TEXTAREA'?editor.value:editor.textContent;
  function comments(doc,p,a) {
    const found=new Map();
    for(const card of all(doc,'.root-reply-container,.reply-item,bili-comment-thread-renderer',a)) {
      const authors=all(card,'a.user-name,a.reply-name,#user-name a,a#user-name',a);
      const identities=new Set(authors.map(x=>a.profileUID(x.href||x.getAttribute('href'))).filter(Boolean));
      if(identities.size!==1||!identities.has(p.expected_uid))continue;
      const bodies=all(card,'.reply-content,.reply-content-container,#content',a);
      if(bodies.length!==1||a.normalize(bodies[0].innerText)!==a.normalize(p.text))continue;
      let id=card.getAttribute('data-rpid')||card.getAttribute('data-id')||'';
      if(!/^[1-9][0-9]{0,29}$/.test(id)) {
        const ids=all(card,'a[href]',a).filter(x=>a.videoURL(x.href||'')===p.url)
          .map(x=>(x.href||'').match(/[?&](?:comment_on|rpid)=([1-9][0-9]{0,29})(?:&|$)/)?.[1]).filter(Boolean);
        if(new Set(ids).size!==1)continue;id=ids[0];
      }
      const times=all(card,'.reply-time,.time,#pubdate',a);
      found.set(id,{id,recent:times.some(x=>p._since&&a.recentTime(x,p._since,p._now))});
    }
    return [...found.values()];
  }
  function receipt(doc,location,p,a) {
    identity(doc,location,p,a);
    const base={status:'completed',uid:p.expected_uid,url:p.url,action:p.action};
    if(p.action==='like')return likeControl(doc,a).liked?{...base,liked:true}:{status:'unknown'};
    const matches=comments(doc,p,a).filter(x=>p.comment_id?x.id===p.comment_id:x.recent);
    return matches.length===1?{...base,text:p.text,comment_id:matches[0].id}:{status:'unknown'};
  }
  function prepare(doc,win,p,a) {
    identity(doc,win.location.href,p,a);
    if(p.action==='like')return {already:likeControl(doc,a).liked};
    if(comments(doc,p,a).length)throw 'existing_text';
    const {editor}=commentControl(doc,a);
    if(a.normalize(textOf(editor)))throw 'unsafe_composer';
    editor.focus();
    if(editor.tagName==='TEXTAREA') {
      const setter=Object.getOwnPropertyDescriptor(win.HTMLTextAreaElement?.prototype||{},'value')?.set;
      if(setter)setter.call(editor,p.text);else editor.value=p.text;
    } else editor.textContent=p.text;
    editor.dispatchEvent(new win.InputEvent('input',{bubbles:true,inputType:'insertText',data:p.text}));
    return {prepared:true};
  }
  function click(doc,win,p,a) {
    identity(doc,win.location.href,p,a);
    if(p.action==='like') {
      const state=likeControl(doc,a);if(!state.liked)state.node.click();
      return;
    }
    const {editor,button}=commentControl(doc,a);
    if(a.normalize(textOf(editor))!==a.normalize(p.text)||button.disabled
       ||button.getAttribute('aria-disabled')==='true'||button.classList.contains('disabled'))throw 'unsafe_composer';
    if(comments(doc,p,a).length)throw 'existing_text';
    button.click();
  }
  async function execute(worker,job,a) {
    const p=job.command.payload,key='interaction:'+p.interaction_id;
    const fingerprint=await worker.hash({expected_uid:p.expected_uid,text:JSON.stringify([p.action,p.url,p.text])});
    worker.requireCurrent(job);
    const old=worker.ledger[key];
    if(old&&old.fingerprint!==fingerprint)throw 'draft_conflict';
    if(Object.entries(worker.ledger).some(([k,x])=>x.request_id===job.command.request_id&&k!==key))throw 'draft_conflict';
    if(job.command.action==='interact'&&old)return {ok:true,result:{status:'unknown'}};
    if(job.command.action==='verify_interaction'&&!old&&!p.comment_id)return {ok:true,result:{status:'unknown'}};
    await worker.open(job,p.url);
    if(job.command.action==='verify_interaction')return {ok:true,result:await worker.run(job,'interaction-proof',
      {...p,_since:old?.attempted_at,_now:worker.now()})};
    const before=await worker.run(job,'interaction-prepare',p);
    if(Object.keys(worker.ledger).length>=512)throw 'ledger_full';
    // Commit barrier is durable before a potentially irreversible click.
    worker.ledger[key]={request_id:job.command.request_id,fingerprint,status:'attempted',attempted_at:worker.now()};
    await worker.api.storage.local.set({activityPublishLedger:worker.ledger});
    worker.requireCurrent(job);job.attempted=true;job.commitSent=true;
    if(!before?.already)await worker.run(job,'interaction-commit',p);
    const result=await worker.run(job,'interaction-proof',{...p,_since:worker.ledger[key].attempted_at,_now:worker.now()});
    return {ok:true,result};
  }
  const api={roots,all,identity,likeControl,commentControl,comments,receipt,prepare,click,execute};
  if(typeof module!=='undefined')module.exports=api;else root.VtuberBilibiliInteraction=api;
})(globalThis);
