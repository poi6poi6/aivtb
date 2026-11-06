/* Shared validation only. No credentials, page scripts, or network APIs. */
(function(root) {
  'use strict';
  const channel = 'ai-vtuber-extension-internal';
  const messages = {
    invalid_request: '查询请求格式不正确。', not_connected: '请先连接浏览器扩展。',
    cancelled: '查询已取消。', timeout: '页面查询超时，请查看扩展打开的 B 站标签页。',
    verification_required: 'B 站页面需要验证，请在浏览器自行处理后重新查询。',
    login_required: 'B 站页面要求登录，请在浏览器自行登录后重新查询。',
    page_unavailable: 'B 站页面暂时不可用，请查看浏览器标签页。',
    extension_unavailable: '浏览器扩展连接已断开，请刷新控制台后重新连接。',
  };
  const stages=new Set(['session_load','connect_save','job_prepare','tab_create','tab_save','tab_mute',
    'tab_reset','tab_navigate','page_check','read_page','result_shape','request_dispatch','random_id','timer_setup']);
  const reasons=new Set(['chrome_api_error','storage_error','tab_closed','tab_busy','permission_denied',
    'window_unavailable','page_changed','invalid_result','blocked_page','unexpected_error','deadline',
    'secure_random_unavailable','timer_unavailable']);
  function error(code,diagnostic={}) {
    const detail={code,message:messages[code] || messages.page_unavailable};
    if(stages.has(diagnostic.stage))detail.stage=diagnostic.stage;
    if(reasons.has(diagnostic.reason))detail.reason=diagnostic.reason;
    return {ok:false,error:detail};
  }
  function errorReason(cause,fallback='chrome_api_error') {
    // Match only fixed API error categories. Raw messages may contain URLs or
    // page text and must never be returned to the web page or stored.
    const message=typeof cause?.message === 'string' ? cause.message : '';
    if(/No tab with id|Invalid tab ID|tab (?:was |has been )?closed/i.test(message))return 'tab_closed';
    if(/Tabs cannot be edited right now|dragging a tab/i.test(message))return 'tab_busy';
    if(/permission|not allowed|access denied/i.test(message))return 'permission_denied';
    if(/No current window|No window with id|window (?:was |has been )?closed/i.test(message))return 'window_unavailable';
    return reasons.has(fallback) ? fallback : 'unexpected_error';
  }
  function local(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === 'http:' && ['localhost','127.0.0.1'].includes(parsed.hostname)
        && ['/', '/bilibili', '/bilibili/'].includes(parsed.pathname) && !parsed.username && !parsed.password;
    } catch { return false; }
  }
  const validId = id => typeof id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(id);
  function target(request) {
    const query = typeof request.query === 'string' ? request.query.trim() : '';
    if (query.length > 300) throw new Error('invalid_request');
    if (!request.url) {
      if (!query) throw new Error('invalid_request');
      const url = new URL('https://search.bilibili.com/all');
      url.searchParams.set('keyword', query); url.searchParams.set('order', 'click');
      return {url:url.href, kind:'search'};
    }
    const url = new URL(request.url);
    if (url.protocol !== 'https:' || url.port || url.username || url.password) throw new Error('invalid_request');
    if (url.hostname === 'search.bilibili.com' && /^\/(?:all|video)\/?$/.test(url.pathname)) {
      const keyword = url.searchParams.get('keyword')?.trim();
      if (!keyword || keyword.length > 300 || (query && keyword !== query)
          || url.searchParams.getAll('keyword').length !== 1 || url.searchParams.getAll('order').length !== 1
          || url.searchParams.get('order') !== 'click') throw new Error('invalid_request');
      const canonical = new URL('https://search.bilibili.com' + url.pathname.replace(/\/$/, ''));
      canonical.searchParams.set('keyword', keyword); canonical.searchParams.set('order', 'click');
      return {url:canonical.href, kind:'search'};
    }
    const match = url.pathname.match(/^\/video\/(BV[A-Za-z0-9]{10})\/?$/);
    if (url.hostname === 'www.bilibili.com' && match) {
      const part = url.searchParams.get('p');
      if (url.searchParams.getAll('p').length > 1 || (part !== null && (!/^[1-9]\d{0,4}$/.test(part) || Number(part)>10000))) throw new Error('invalid_request');
      return {url:`https://www.bilibili.com/video/${match[1]}` + (part ? `?p=${part}` : ''), kind:'video'};
    }
    throw new Error('invalid_request');
  }
  function jobId(url) {
    try { return new URL(url).hash.match(/^#ai-vtuber-job=([a-f0-9-]{32,36})$/)?.[1] || ''; }
    catch { return ''; }
  }
  function samePage(actual,expected,id) {
    try {
      const left=new URL(actual),right=new URL(expected);
      if(left.hash !== '#ai-vtuber-job='+id)return false;
      left.hash='';right.hash='';
      left.pathname=left.pathname.replace(/\/$/,'');right.pathname=right.pathname.replace(/\/$/,'');
      if(left.hostname === 'search.bilibili.com' && right.hostname === left.hostname
          && ['/all','/video'].includes(left.pathname) && ['/all','/video'].includes(right.pathname)) {
        left.pathname='/all';right.pathname='/all';
      }
      const tracking=new Set(['spm_id_from','from_source','search_source','vd_source','from_spmid','seid','source_from','from']);
      for(const key of [...left.searchParams.keys()]) if(tracking.has(key))left.searchParams.delete(key);
      left.searchParams.sort();right.searchParams.sort();
      return left.href === right.href;
    } catch { return false; }
  }
  // Public statistic diagnostics are optional and never contain page prose,
  // arbitrary attributes, URLs, path data, or browser/session information.
  const diagnosticClasses=new Set(['bili-video-card__stats','bili-video-card__stats--item',
    'bili-video-card__stats-item','bili-video-card__stats--text','bili-video-card__stats-text',
    'bili-video-card__stats--icon','bili-video-card__stats__duration','video-card__stats',
    'video-card__stats--item','video-card__stats--text','so-icon','watch-num','play-count',
    'view-count','video-views','view','view-text','icon-play-count','icon-play','icon-danmaku']);
  const diagnosticSources=new Set(['stat_title','stat_aria','count_title','count_aria','count_text','stat_text']);
  const diagnosticRefs=new Set(['#widget-play-count','#widget-danmaku','#icon-play','#icon-danmaku']);
  const diagnosticClass=value=>typeof value==='string'&&value.length<=64&&(diagnosticClasses.has(value)
    || /^(?:bili-video-card__stats|video-card__stats|icon)[-_a-z0-9]{0,48}$/i.test(value));
  const choose=(value,allowed,fallback='unknown')=>allowed.includes(value)?value:fallback;
  const bounded=(value,max)=>Number.isSafeInteger(value)&&value>=0?Math.min(value,max):0;
  function statisticText(value) {
    if(typeof value!=='string'||value.length>128)return '';
    const clean=value.normalize('NFKC').replace(/\s+/g,' ').trim();
    // Entire-field vocabulary, not extraction of digits from arbitrary text.
    // Mixed metrics remain visible to diagnosis without becoming a play count.
    return clean.length<=64 && /^(?:(?:[0-9.,:：\s/+~\-]|万|亿|萬|億|次|总|總|播放|观看|觀看|浏览|瀏覽|弹幕|彈幕|评论|評論|点赞|點讚|讚|赞|收藏|投币|投幣|时长|時長|数量|數量|次数|次數|量|数|數|views?|plays?|[wWkKmMbB]))+$/.test(clean)?clean:'';
  }
  function cleanDiagnosticNode(value) {
    value=value&&typeof value==='object'?value:{};
    return {tag:choose(value.tag,['span','div','i','b','em','strong','svg','path','use','p']),
      classes:Array.isArray(value.classes)?[...new Set(value.classes.filter(diagnosticClass))].slice(0,3):[],
      ns:choose(value.ns,['html','svg']),
      other_classes:bounded(value.other_classes,20),visible:value.visible===true,rects:bounded(value.rects,4),
      hidden:choose(value.hidden,['undefined','false','true','object','other']),
      display:choose(value.display,['none','contents','block','inline','inline-block','flex','inline-flex','grid','inline-grid']),
      visibility:choose(value.visibility,['visible','hidden','collapse'])};
  }
  function cleanStatsDiagnostic(value) {
    if(value?.version!==1||!Array.isArray(value.cards))return undefined;
    const output={version:1,page_visibility:choose(value.page_visibility,['visible','hidden']),cards:[]};
    const fits=()=>new TextEncoder().encode(JSON.stringify(output)).length<=6144;
    for(const source of value.cards.slice(0,2)) {
      if(!source||typeof source!=='object')continue;
      const card={index:bounded(source.index,29),candidates:bounded(source.candidates,99),stats:[]};
      output.cards.push(card);
      for(const item of (Array.isArray(source.stats)?source.stats:[]).slice(0,3)) {
        if(!item||typeof item!=='object')continue;
        const stat={node:cleanDiagnosticNode(item.node),positive:item.positive===true,rejected:item.rejected===true,
          count:item.count?cleanDiagnosticNode(item.count):null,values:[],icons:[]};
        for(const raw of (Array.isArray(item.values)?item.values:[]).slice(0,6)) {
          if(!diagnosticSources.has(raw?.source))continue;
          const safe=statisticText(raw.text);
          stat.values.push({source:raw.source,text:safe,invalid:raw.invalid===true||(!safe&&!!raw.text),parsed:raw.parsed===true&&!!safe});
        }
        for(const icon of (Array.isArray(item.icons)?item.icons:[]).slice(0,1)) {
          const view=Array.isArray(icon?.view_box)&&icon.view_box.length===4&&icon.view_box.every(n=>Number.isFinite(n)&&Math.abs(n)<=10000)?icon.view_box:[];
          stat.icons.push({node:cleanDiagnosticNode(icon?.node),view_box:view,path_count:bounded(icon?.path_count,99),
            use:(Array.isArray(icon?.use)?icon.use:[]).slice(0,2).map(ref=>diagnosticRefs.has(ref)?ref:'unknown'),
            paths:(Array.isArray(icon?.paths)?icon.paths:[]).slice(0,2).map(path=>({visible:path?.visible===true,
              rects:bounded(path?.rects,4),length:bounded(path?.length,4096),known:path?.known===true,
              hash:typeof path?.hash==='string'&&/^[a-f0-9]{8}$/.test(path.hash)?path.hash:''}))});
        }
        card.stats.push(stat);
        if(!fits()){card.stats.pop();break;}
      }
      // A selector miss still gets a small outline of a named stats region.
      if(card.candidates===0&&Array.isArray(source.regions)) {
        card.regions=[];
        for(const region of source.regions.slice(0,1)) {
          card.regions.push({node:cleanDiagnosticNode(region?.node),children:(Array.isArray(region?.children)?region.children:[]).slice(0,3)
            .map(child=>({node:cleanDiagnosticNode(child?.node),text:statisticText(child?.text)}))});
          if(!fits())card.regions.pop();
        }
      }
      if(!fits()){output.cards.pop();break;}
    }
    return output.cards.length?output:undefined;
  }
  const api = {channel, error, errorReason, stages, reasons, local, validId, target, jobId, samePage,
    statisticText,diagnosticClass,cleanStatsDiagnostic};
  root.VtuberBrowserCommon = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
