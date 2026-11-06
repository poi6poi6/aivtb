(function(root) {
  'use strict';
  const CHANNEL='ai-vtuber-bilibili-activity-internal';
  const SOURCE='ai-vtuber-bilibili-activity';
  const RESPONSE='ai-vtuber-bilibili-activity-extension';
  const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const UID=/^[1-9][0-9]{0,19}$/;
  const ACTIONS=new Set(['account','search','read','publish','verify','interact','verify_interaction']);
  const ERROR_MESSAGES={invalid_request:'活动请求格式不正确',busy:'另一个浏览活动正在执行',
    unavailable:'页面或扩展暂不可用',timeout:'页面读取超时',login_required:'请先在浏览器登录 B 站',
    verification_required:'请自行处理 B 站页面验证',account_mismatch:'当前 B 站账号与预期账号不一致',
    unsupported_page:'当前页面结构无法安全识别',draft_conflict:'草稿标识已用于另一段正文',
    ledger_full:'发布记录已满，请人工检查扩展记录',cancelled:'活动页面已关闭',
    unsafe_composer:'动态编辑器已有内容或附加选项，请人工检查',existing_text:'当前动态列表已有相同正文，请人工核验，未再次发布'};
  function error(code) {const safe=Object.hasOwn(ERROR_MESSAGES,code)?code:'unavailable';return {ok:false,error:{code:safe,message:ERROR_MESSAGES[safe]}};}
  function url(value) {try{return new URL(value);}catch{return null;}}
  function local(value) {
    const u=url(value);return !!u&&u.protocol==='http:'&&['localhost','127.0.0.1'].includes(u.hostname)
      &&!u.username&&!u.password&&['/','/bilibili-activity','/bilibili-activity/'].includes(u.pathname);
  }
  function videoURL(value) {
    const u=url(value);if(!u||u.protocol!=='https:'||u.hostname!=='www.bilibili.com'||u.port||u.username||u.password)return '';
    const match=/^\/video\/(BV[0-9A-Za-z]{10})\/?$/.exec(u.pathname);return match?`https://www.bilibili.com/video/${match[1]}`:'';
  }
  function dynamicURL(value) {
    const u=url(value);if(!u||u.protocol!=='https:'||u.port||u.username||u.password)return null;
    const match=(u.hostname==='www.bilibili.com'?/^\/opus\/([1-9][0-9]{0,29})\/?$/:
      u.hostname==='t.bilibili.com'?/^\/([1-9][0-9]{0,29})\/?$/:/^$/).exec(u.pathname);
    return match?{dynamic_id:match[1],url:`https://www.bilibili.com/opus/${match[1]}`} : null;
  }
  function profileUID(value) {
    const u=url(value);if(!u||u.protocol!=='https:'||u.hostname!=='space.bilibili.com'||u.port||u.username||u.password)return '';
    const match=/^\/([1-9][0-9]{0,19})(?:\/|$)/.exec(u.pathname);return match?match[1]:'';
  }
  function normalize(value) {return String(value??'').replace(/[\u200b\ufeff]/g,'').replace(/\s+/gu,' ').trim();}
  function validate(message) {
    if(!message||typeof message.request_id!=='string'||!UUID.test(message.request_id)||!ACTIONS.has(message.action))return null;
    const p=message.payload;if(!p||typeof p!=='object'||Array.isArray(p))return null;
    let payload={};
    if(message.action==='search') {
      if(typeof p.query!=='string'||!p.query.trim()||p.query.length>100)return null;payload={query:p.query.trim()};
    } else if(message.action==='read') {
      const target=typeof p.url==='string'&&videoURL(p.url);if(!target)return null;payload={url:target};
    } else if(['interact','verify_interaction'].includes(message.action)) {
      const target=typeof p.url==='string'&&videoURL(p.url);
      if(!target||typeof p.expected_uid!=='string'||!UID.test(p.expected_uid)
        ||typeof p.interaction_id!=='string'||!UUID.test(p.interaction_id)
        ||!['like','comment'].includes(p.action)||typeof p.text!=='string'
        ||(p.action==='like'?p.text!=='':!p.text.trim()||p.text.length>500))return null;
      payload={url:target,expected_uid:p.expected_uid,interaction_id:p.interaction_id.toLowerCase(),action:p.action,text:p.text};
      if(message.action==='verify_interaction'&&p.comment_id!==undefined) {
        if(typeof p.comment_id!=='string'||! /^[1-9][0-9]{0,29}$/.test(p.comment_id))return null;
        payload.comment_id=p.comment_id;
      }
    } else if(['publish','verify'].includes(message.action)) {
      if(typeof p.expected_uid!=='string'||!UID.test(p.expected_uid)||typeof p.text!=='string'
        ||!p.text.trim()||p.text.length>2000||typeof p.draft_id!=='string'||!UUID.test(p.draft_id))return null;
      payload={expected_uid:p.expected_uid,text:p.text.trim(),draft_id:p.draft_id.toLowerCase()};
      if(message.action==='verify'&&p.url) {const target=dynamicURL(p.url);if(!target)return null;payload.url=target.url;}
    }
    return {request_id:message.request_id.toLowerCase(),action:message.action,payload};
  }
  function visible(node) {
    if(!node||node.hidden||!node.getClientRects?.().length)return false;
    const style=node.ownerDocument?.defaultView?.getComputedStyle?.(node);
    return !style||!(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse');
  }
  const nodes=(doc,selector)=>Array.from(doc.querySelectorAll(selector)).filter(visible);
  const first=(doc,selector)=>nodes(doc,selector)[0];
  function pageBlock(doc) {
    if(nodes(doc,'.geetest_panel,.geetest_holder,.bili-risk-control,.security-check').length)return 'verification_required';
    if(nodes(doc,'.bili-mini-login-wp,.bili-mini-mask,.login-scan-box').length)return 'login_required';
    return '';
  }
  function account(doc) {
    // Only the signed-in member widget/header is trusted; uploader/profile links
    // elsewhere on a page must never select the account used for publishing.
    const anchors=nodes(doc,'.bili-dyn-my-info__stats a,.header-avatar-wrap a.header-entry-mini,.header-avatar-wrap a.header-entry');
    const ids=new Set(anchors.map(a=>profileUID(a.href||a.getAttribute('href'))).filter(Boolean));
    if(ids.size!==1)return null;
    return {uid:[...ids][0],name:normalize(first(doc,'.bili-dyn-my-info__header .info__name,.avatar-panel-popover .nickname-item')?.innerText).slice(0,100)};
  }
  function search(doc) {
    const items=[],seen=new Set();
    for(const card of nodes(doc,'.bili-video-card,.video-item,.video-list-item')) {
      const links=nodes(card,'a[href]');const link=links.find(a=>videoURL(a.href||a.getAttribute('href')));
      const target=link&&videoURL(link.href||link.getAttribute('href'));if(!target||seen.has(target))continue;
      const heading=first(card,'.bili-video-card__info--tit,.title,h3');
      const title=normalize(heading?.getAttribute('title')||heading?.innerText||link.getAttribute('title')||link.innerText).slice(0,300);
      if(!title)continue;seen.add(target);
      items.push({url:target,title,author:normalize(first(card,'.bili-video-card__info--author,.up-name,.up-name__text')?.innerText).slice(0,100)});
      if(items.length===20)break;
    }
    return items;
  }
  function read(doc,location) {
    const target=videoURL(location);if(!target)return null;
    const title=normalize(first(doc,'h1.video-title,h1[title],.video-title')?.innerText);if(!title)return null;
    // A semantic meta description is explicitly metadata, never a claim that
    // the video has been watched. Captions are limited to currently shown cues.
    const description=String(doc.querySelector('meta[name="description"]')?.getAttribute('content')||'').slice(0,5000);
    const subtitles=[];
    for(const media of nodes(doc,'video'))for(const track of Array.from(media.textTracks||[])) {
      if(track.mode!=='showing')continue;
      for(const cue of Array.from(track.activeCues||[])){const text=normalize(cue.text);if(text&&!subtitles.includes(text))subtitles.push(text.slice(0,1000));}
    }
    return {url:target,title:title.slice(0,300),description,subtitles:subtitles.slice(0,30),
      evidence_kind:subtitles.length?'visible_subtitles':'page_metadata'};
  }
  function bodyText(node) {
    // Rich-text URL labels may display “网页链接”. Reconstruct only visible
    // anchors, not hidden application state or private framework properties.
    function walk(n) {
      if(n.nodeType===3)return n.nodeValue||'';
      if(n.nodeType===1&&!visible(n))return '';
      if(n.tagName==='BR')return '\n';
      if(n.tagName==='A') {
        const raw=n.href||n.getAttribute('href'),target=videoURL(raw);
        if(target&&/^(?:网页链接|视频链接|https?:\/\/)/.test(normalize(n.innerText)))return target;
      }
      if(n.childNodes?.length){const value=Array.from(n.childNodes).map(walk).join('');return ['P','DIV','LI'].includes(n.tagName)?value+'\n':value;}
      return n.innerText||n.textContent||'';
    }
    return normalize(walk(node));
  }
  function candidate(doc,location,payload) {
    const u=url(location),who=account(doc);
    if(!who||who.uid!==payload.expected_uid||u?.hostname!=='space.bilibili.com'
      ||u.pathname!==`/${payload.expected_uid}/dynamic`)return null;
    const found=[];
    for(const card of nodes(doc,'.bili-dyn-list__item')) {
      // Reposts and nested originals cannot be evidence of this text-only post.
      if(nodes(card,'.bili-dyn-content__forw__desc').length)continue;
      const bodies=nodes(card,'.bili-dyn-content__orig__desc');
      const times=nodes(card,'.bili-dyn-time');
      if(bodies.length===1&&times.length===1&&bodyText(bodies[0])===normalize(payload.text))found.push({card,time:times[0]});
    }
    if(found.length!==1)return null;
    if(payload._since&&!recentTime(found[0].time,payload._since,payload._now))return null;
    return found[0];
  }
  function recentTime(node,since,now=Date.now()) {
    const value=normalize(node.innerText);let age;
    if(/^(?:刚刚|刚刚发布|几秒前)$/.test(value))age=0;
    else {const match=/^(\d+)\s*(秒|分钟|小时|天)前$/.exec(value);if(!match)return false;
      age=Number(match[1])*({秒:1000,分钟:60000,小时:3600000,天:86400000}[match[2]]);}
    return Number.isFinite(since)&&since<=now&&age<=now-since+60000;
  }
  function timeline(doc,location,payload) {
    const u=url(location),who=account(doc);
    if(!who||who.uid!==payload.expected_uid||u?.hostname!=='space.bilibili.com'||u.pathname!==`/${payload.expected_uid}/dynamic`)return null;
    const lists=nodes(doc,'.bili-dyn-list');if(lists.length!==1||nodes(lists[0],'.bili-dyn-list-loading').length)return null;
    const cards=nodes(lists[0],'.bili-dyn-list__item');
    if(!cards.length&&!nodes(lists[0],'.bili-dyn-list-empty').length)return null;
    const texts=cards.map(card=>nodes(card,'.bili-dyn-content__orig__desc').map(bodyText).join('\n'));
    return {scanned:true,existing:texts.includes(normalize(payload.text)),signature:JSON.stringify(texts)};
  }
  function author(doc) {
    const links=nodes(doc,'.opus-module-author a[href],.bili-dyn-title a[href]');
    const ids=new Set(links.map(n=>profileUID(n.href||n.getAttribute('href'))).filter(Boolean));
    if(ids.size>1)return {uid:'',node:null};
    const names=nodes(doc,'.opus-module-author__name,.bili-dyn-title');
    return {uid:ids.size===1?[...ids][0]:'',node:names.length===1?names[0]:null};
  }
  function detailMatches(doc,text) {
    const bodies=nodes(doc,'.opus-module-content,.bili-dyn-content__orig__desc');
    return bodies.length===1&&bodyText(bodies[0])===normalize(text);
  }
  function composer(doc,payload,{filled=false}={}) {
    const who=account(doc);if(!who)throw 'login_required';if(who.uid!==payload.expected_uid)throw 'account_mismatch';
    const roots=nodes(doc,'.bili-dyn-publishing');if(roots.length!==1)throw 'unsupported_page';
    const area=roots[0],editors=nodes(area,'.bili-dyn-publishing__input [contenteditable="true"]');
    const launchers=nodes(area,'.bili-dyn-publishing__action.launcher');
    if(editors.length!==1||launchers.length!==1||normalize(launchers[0].innerText)!=='发布')throw 'unsupported_page';
    const attachments=nodes(area,'.bili-dyn-publishing__attachment,.bili-dyn-publishing__origin,.bili-dyn-publishing__cmorder');
    if(attachments.some(n=>normalize(n.innerText)||n.querySelector('img,video')))throw 'unsafe_composer';
    const current=bodyText(editors[0]);
    if(filled?current!==normalize(payload.text):!!current)throw 'unsafe_composer';
    const button=launchers[0];
    if(filled&&(button.classList?.contains('disabled')||button.getAttribute('aria-disabled')==='true'||button.disabled))throw 'unsafe_composer';
    return {editor:editors[0],button};
  }
  function proof(payload,actualURL,detailOK) {
    const target=dynamicURL(actualURL),requested=payload.url&&dynamicURL(payload.url);
    if(!detailOK||!target||(requested&&requested.dynamic_id!==target.dynamic_id))return {status:'unknown'};
    return {status:'published',uid:payload.expected_uid,text:payload.text,...target};
  }
  const api={CHANNEL,SOURCE,RESPONSE,UUID,UID,ACTIONS,error,local,videoURL,dynamicURL,profileUID,normalize,
    validate,visible,nodes,pageBlock,account,search,read,bodyText,candidate,recentTime,timeline,author,detailMatches,composer,proof};
  if(typeof module!=='undefined')module.exports=api;else root.VtuberBilibiliActivity=api;
})(globalThis);
