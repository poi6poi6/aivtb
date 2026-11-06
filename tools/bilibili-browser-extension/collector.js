(function(root) {
  'use strict';
  const common=root.VtuberBrowserCommon || require('./common.js');
  const text=node => (node?.textContent || '').replace(/\s+/g,' ').trim();
  const countText=value => String(value || '').normalize('NFKC')
    .replace(/[萬億總觀數閱彈讚贊時長幣評論點擊歡瀏覽]/g,char=>({萬:'万',億:'亿',總:'总',觀:'观',數:'数',閱:'阅',彈:'弹',讚:'赞',贊:'赞',時:'时',長:'长',幣:'币',評:'评',論:'论',點:'点',擊:'击',歡:'欢',瀏:'浏',覽:'览'}[char]));
  // Known rounded play-triangle glyph in a public MIT-licensed markup sample:
  // https://greasyfork.org/zh-CN/scripts/501860-bilibili/code
  // This is compatibility evidence, not a capture of the user's current DOM.
  // Never match the outer rectangle shared by play and danmaku icons.
  const inlinePlayTriangle='M14.7138 10.96875C15.50765 11.4271 15.50765 12.573 14.71375 13.0313L11.5362 14.8659C10.74235 15.3242 9.75 14.7513 9.75001 13.8346L9.75001 10.1655C9.75001 9.24881 10.74235 8.67587 11.5362 9.13422L14.7138 10.96875z';
  const pathFingerprint=value=>String(value || '').replace(/[\s,]+/g,'');
  function visible(node,win) {
    if (!node || node.hidden || !node.getClientRects().length) return false;
    const style=win.getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
  }
  const first=(node,selectors) => node.querySelector(selectors);
  function coverUrl(value) {
    try {
      const url=new URL(value,'https://www.bilibili.com');
      return url.protocol === 'https:' && /(^|\.)hdslb\.com$/.test(url.hostname) ? url.href : '';
    } catch { return ''; }
  }
  function playNumber(value) {
    const clean=countText(value).trim().replace(/^(?:(?:总)?(?:播放|观看|浏览)(?:次数|量|数)?|views?|plays?)\s*[:：]?\s*/i,'')
      .replace(/\s*(?:(?:次)?(?:播放|观看|浏览)|次|views?|plays?)$/i,'').trim();
    return /^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\s*[万亿]?$/.test(clean) ? clean.slice(0,64) : '';
  }
  function statMeaning(stat) {
    // Attribute values here identify the statistic only; numeric data-* values
    // are never used as counts. Counts come from visible text/accessible labels.
    const semanticAttributes=['title','aria-label','class','id','data-icon','data-type','data-stat',
      'data-stat-type','data-testid','data-name','data-e2e','name','href','xlink:href'];
    const nodes=[stat,...stat.querySelectorAll('svg, use, svg title, [title], [aria-label], [data-icon], [data-type], [data-stat], [data-stat-type], [data-testid], [data-name], [data-e2e]')];
    const signals=countText(nodes.slice(0,32).map(node=>semanticAttributes.map(name=>node.getAttribute(name)||'').join(' ')
      +(node.tagName?.toLowerCase()==='title'?' '+text(node):'')).join(' '));
    const rejected=/弹幕|评论|点赞|赞|喜欢|收藏|投币|时长|danmaku|danmu|comment|duration|favorite|like|coin|watch.later/i.test(signals);
    const positive=/播放|观看|浏览|(?:^|[\s#:_-])(?:plays?|playcount|playback|views?|viewcount|video.play)(?=$|[\s#:_-])/i.test(signals);
    return {rejected,positive};
  }
  function knownInlinePlay(stat,win) {
    if(!stat.matches('.bili-video-card__stats--item'))return false;
    const count=stat.querySelector('.bili-video-card__stats--text');
    if(!visible(count,win) || !playNumber(text(count)))return false;
    for(const icon of stat.querySelectorAll('svg.bili-video-card__stats--icon')) {
      if(!visible(icon,win) || (icon.getAttribute('viewBox') || '').trim().replace(/[\s,]+/g,' ')!=='0 0 24 24')continue;
      const paths=icon.querySelectorAll('path');
      if(paths.length>=1 && paths.length<=2 && [...paths].some(path=>visible(path,win) && pathFingerprint(path.getAttribute('d'))===pathFingerprint(inlinePlayTriangle)))return true;
    }
    return false;
  }
  const statisticSelector='.bili-video-card__stats--item, .bili-video-card__stats-item, .video-card__stats--item, .so-icon.watch-num, .play-count, .view-count, .video-views, .video-info-detail-list .view, .view-text, .bili-video-card__stats [data-stat], .bili-video-card__stats [data-type], .bili-video-card__stats [aria-label]';
  const countSelector='.bili-video-card__stats--text, .bili-video-card__stats-text, .video-card__stats--text';
  function playText(node,win) {
    const candidates=node.querySelectorAll(statisticSelector);
    for (const stat of candidates) {
      if (!visible(stat,win)) continue;
      const meaning=statMeaning(stat);
      if (meaning.rejected) continue;
      const explicit=stat.matches('.so-icon.watch-num, .play-count, .view-count, .video-views, .video-info-detail-list .view, .view-text');
      if (explicit || meaning.positive || knownInlinePlay(stat,win)) {
        const countNode=stat.querySelector(countSelector);
        for(const raw of [stat.getAttribute('title'),stat.getAttribute('aria-label'),
          countNode?.getAttribute('title'),countNode?.getAttribute('aria-label'),text(countNode),text(stat)]) {
          const value=playNumber(raw);
          if(value)return value;
        }
      }
    }
    return '';
  }
  function diagnosticNode(node,win) {
    if(!node)return null;
    const classes=(node.getAttribute('class')||'').split(/\s+/).filter(Boolean).slice(0,20);
    const style=win.getComputedStyle(node);
    return {tag:node.tagName?.toLowerCase(),classes,other_classes:classes.filter(value=>!common.diagnosticClass(value)).length,
      ns:node.namespaceURI==='http://www.w3.org/2000/svg'?'svg':node.namespaceURI==='http://www.w3.org/1999/xhtml'?'html':'unknown',
      visible:visible(node,win),rects:node.getClientRects().length,
      hidden:node.hidden===true?'true':node.hidden===false?'false':typeof node.hidden==='undefined'?'undefined':typeof node.hidden==='object'?'object':'other',
      display:style.display,visibility:style.visibility};
  }
  function pathDiagnostic(path,win) {
    const raw=path.getAttribute('d')||'',data=pathFingerprint(raw.slice(0,4096));
    // A non-security fingerprint identifies repeated glyphs without exporting d.
    let hash=2166136261;
    for(let i=0;i<data.length;i++)hash=Math.imul(hash^data.charCodeAt(i),16777619);
    return {visible:visible(path,win),rects:path.getClientRects().length,length:raw.length,
      hash:(hash>>>0).toString(16).padStart(8,'0'),known:data===pathFingerprint(inlinePlayTriangle)};
  }
  function diagnosticCard(node,win,index) {
    const candidates=node.querySelectorAll(statisticSelector);
    const card={index,candidates:candidates.length,stats:[]};
    for(const stat of [...candidates].slice(0,3)) {
      const count=stat.querySelector(countSelector),meaning=statMeaning(stat);
      const rawValues=[['stat_title',stat.getAttribute('title')],['stat_aria',stat.getAttribute('aria-label')],
        ['count_title',count?.getAttribute('title')],['count_aria',count?.getAttribute('aria-label')],
        ['count_text',text(count)],['stat_text',text(stat)]];
      const values=rawValues.filter(([,raw])=>typeof raw==='string'&&raw.length).map(([source,raw])=>{
        const safe=common.statisticText(raw);
        return {source,text:safe,invalid:!safe,parsed:!!playNumber(raw)};
      });
      const icons=[...stat.querySelectorAll('svg')].slice(0,1).map(icon=>({node:diagnosticNode(icon,win),
        view_box:(icon.getAttribute('viewBox')||'').trim().split(/[\s,]+/).map(Number),
        use:[...icon.querySelectorAll('use')].slice(0,2).map(ref=>ref.getAttribute('href')||ref.getAttribute('xlink:href')||''),
        path_count:icon.querySelectorAll('path').length,
        paths:[...icon.querySelectorAll('path')].slice(0,2).map(path=>pathDiagnostic(path,win))}));
      card.stats.push({node:diagnosticNode(stat,win),...meaning,count:diagnosticNode(count,win),values,icons});
    }
    if(!candidates.length)card.regions=[...node.querySelectorAll('.bili-video-card__stats, .video-card__stats')].slice(0,1)
      .map(region=>({node:diagnosticNode(region,win),children:[...region.children].slice(0,3)
        .map(child=>({node:diagnosticNode(child,win),text:common.statisticText(text(child))}))}));
    return card;
  }
  function pageError(doc,win) {
    for (const node of doc.querySelectorAll('.geetest_panel, .geetest_holder, .bili-captcha, [class*="captcha"], .risk-control')) {
      if (visible(node,win)) return 'verification_required';
    }
    const panels=[...doc.querySelectorAll('.error-container, .error-wrap, .error-panel, .login-panel, .bili-mini-login, .bili-mini-content-wp')]
      .filter(node => visible(node,win)).map(text).join(' ');
    const hasCards=[...doc.querySelectorAll('.bili-video-card, .video-item, .video-list-item')].some(node => visible(node,win));
    const videoTitle=first(doc,'h1.video-title, .video-title');
    const hasVideo=visible(videoTitle,win) && !!text(videoTitle)
      && /\/video\/BV[A-Za-z0-9]{10}\/?$/.test(new URL(win.location.href).pathname);
    // Do not mistake a video's title containing "security verification" for
    // a site challenge. Error overlays remain detectable over existing cards.
    const body=panels || (!hasCards && !hasVideo ? (doc.body?.innerText || '').slice(0,20000) : '');
    if (/请完成验证|安全验证|人机验证|请通过验证|访问过于频繁/.test(body)) return 'verification_required';
    if (/请先登录|登录后才能|登录后(?:即可|才可|方可)(?:查看|搜索|访问)/.test(body)) return 'login_required';
    if (/访问被拒绝|页面访问受限|无法访问此页面|请求被拦截|服务不可用/.test(body)) return 'page_unavailable';
    return '';
  }
  function readPage(doc,win,kind) {
    const code=pageError(doc,win);
    if (code) return common.error(code);
    const results=[],seen=new Set(),missingCards=[];
    const add=(bvid,title,node,author,duration,cover) => {
      if (!/^BV[A-Za-z0-9]{10}$/.test(bvid) || !title || seen.has(bvid)) return;
      seen.add(bvid);const play=playText(node,win);
      if(!play&&kind==='search'&&missingCards.length<2)missingCards.push({node,index:results.length});
      results.push({bvid,title:title.slice(0,250),author:author.slice(0,100),
        duration:duration.slice(0,30),cover:coverUrl(cover),play_text:play});
    };
    if (kind === 'video') {
      const bvid=new URL(win.location.href).pathname.match(/\/video\/(BV[A-Za-z0-9]{10})/)?.[1];
      const heading=first(doc,'h1.video-title, h1[title], .video-title');
      if (visible(heading,win)) add(bvid,text(heading),doc,text(first(doc,'.up-name')),
        text(first(doc,'.bpx-player-ctrl-time-duration, .bilibili-player-video-time-total')),
        first(doc,'video[poster]')?.getAttribute('poster') || '');
    } else {
      for (const card of doc.querySelectorAll('.bili-video-card, .video-item, .video-list-item')) {
        if (!visible(card,win)) continue;
        const link=first(card,'a[href*="/video/BV"]');
        let bvid='';
        try {
          const url=new URL(link?.getAttribute('href'),'https://www.bilibili.com');
          if (url.hostname === 'www.bilibili.com') bvid=url.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})\/?$/)?.[1] || '';
        } catch { /* Invalid/advertisement links are not video results. */ }
        const heading=first(card,'.bili-video-card__info--tit, .bili-video-card__info--title, a.title, h3');
        const title=visible(heading,win) ? text(heading) || heading.getAttribute('title') || '' : '';
        const image=first(card,'img');
        add(bvid,title,card,text(first(card,'.bili-video-card__info--author, .up-name, .up-name__text')),
          text(first(card,'.bili-video-card__stats__duration, .duration')),image?.currentSrc || image?.getAttribute('src') || '');
        if (results.length >= 30) break;
      }
    }
    const data={page_url:win.location.href,results};
    if(missingCards.length) {
      try {
        data.stats_diagnostic=common.cleanStatsDiagnostic({version:1,page_visibility:doc.visibilityState,
          cards:missingCards.map(({node,index})=>diagnosticCard(node,win,index))});
      } catch { /* Optional diagnostics must never break a usable page result. */ }
    }
    return {ok:true,data,empty:[...doc.querySelectorAll('.search-empty, .search-nodata, .no-data')].some(node => visible(node,win))};
  }
  function mediaGuard(doc,win) {
    let enabled=false;
    const hush=node => {
      if (!enabled || !node || !['VIDEO','AUDIO'].includes(node.tagName)) return;
      if (!node.muted) node.muted=true;
      if (!node.defaultMuted) node.defaultMuted=true;
      if (!node.paused) node.pause();
      node.autoplay=false;
    };
    const scan=() => { if(enabled) for(const media of doc.querySelectorAll('video, audio')) hush(media); };
    // Listeners are registered at document_start; ownership is confirmed by
    // the worker before touching media. The owned tab was already muted before
    // navigation, so the short async ownership check cannot produce sound.
    for (const name of ['play','playing','loadedmetadata','volumechange']) doc.addEventListener(name,event => hush(event.target),true);
    const observer=new win.MutationObserver(scan);
    observer.observe(doc,{childList:true,subtree:true});
    return {enable(){enabled=true;scan();},stop(){enabled=false;observer.disconnect();}};
  }
  function collect(doc,win,kind,done,{setTimer=setTimeout,clearTimer=clearTimeout,now=Date.now}={}) {
    let stopped=false,timer=null,signature='',stableSince=0,cardSignature='',cardsStableSince=0,firstRowsAt=null;
    const started=now();
    const stop=() => {stopped=true;clearTimer(timer);};
    const finish=result => {stop();done(result);};
    const check=() => {
      if(stopped)return;
      const result=readPage(doc,win,kind);
      if(!result.ok){finish(result);return;}
      const next=JSON.stringify(result.data.results);
      if(next !== signature){signature=next;stableSince=now();}
      const cards=JSON.stringify(result.data.results.map(item=>[item.bvid,item.title]));
      if(cards !== cardSignature){cardSignature=cards;cardsStableSince=now();}
      const hasRows=result.data.results.length>0;
      if(hasRows && firstRowsAt===null)firstRowsAt=now();
      const allCounts=hasRows && result.data.results.every(item=>item.play_text!=='');
      // Cards can paint before their statistics. Allow missing counts after a
      // four-second grace from the first rows, while still requiring a full
      // second of row stability. Missing values remain explicitly unknown.
      if((hasRows && now()-cardsStableSince>=1000 && now()-stableSince>=1000
          && (allCounts || now()-firstRowsAt>=4000))
          || (result.empty && !hasRows && now()-stableSince>=1000)) {finish(result);return;}
      if(now()-started >= 24000){finish(common.error('timeout'));return;}
      timer=setTimer(check,250);
    };
    check();
    return stop;
  }
  function install(doc,win,runtime) {
    if(win !== win.top)return;
    const jobId=common.jobId(win.location.href);
    if(!jobId)return;
    const guard=mediaGuard(doc,win);
    let stop=null,cancelled=false;
    runtime.onMessage.addListener(message => {
      if(message?.channel === common.channel && message.action === 'cancel' && message.jobId === jobId) {
        cancelled=true;stop?.(); // Keep the query tab muted after cancellation.
      }
    });
    runtime.sendMessage({channel:common.channel,action:'collector-hello',jobId}).then(response => {
      if(!response?.owned){guard.stop();return;}
      guard.enable();
      if(!response.collect || cancelled)return;
      stop=collect(doc,win,response.collect.kind,result => {
        if(!cancelled) runtime.sendMessage({channel:common.channel,action:'collector-result',jobId,result}).catch(() => {});
      });
    }).catch(() => guard.stop());
    win.addEventListener('pagehide',() => {cancelled=true;stop?.();guard.stop();});
  }
  const api={visible,playNumber,statMeaning,knownInlinePlay,playText,pageError,readPage,mediaGuard,collect,install};
  if(typeof module !== 'undefined')module.exports=api;
  else install(document,window,chrome.runtime);
})(globalThis);
