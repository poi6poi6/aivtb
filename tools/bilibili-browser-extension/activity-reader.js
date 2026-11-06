/* Read-only video evidence adapter. No cookie API or credential export.
 * Endpoint/signing contract checked against dsh-bili-whale's subtitles.mjs
 * and comments.mjs. This implementation keeps our browser/session ownership.
 */
(function(root) {
  'use strict';
  const API='https://api.bilibili.com';
  const PATHS=new Set(['/x/web-interface/view','/x/web-interface/nav','/x/player/wbi/v2','/x/v2/reply']);
  const SUBTITLE_HOSTS=new Set(['aisubtitle.hdslb.com','subtitle.hdslb.com','i0.hdslb.com','i1.hdslb.com','i2.hdslb.com']);
  const MIXIN=[46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52];
  const clean=value=>typeof value==='string'?value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g,'').trim():'';
  const positive=value=>Number.isSafeInteger(value)&&value>0;
  function subtitleURL(value) {
    if(typeof value!=='string'||value.length>4096)return '';
    try {
      const u=new URL(value.startsWith('//')?'https:'+value:value);
      if(u.protocol!=='https:'||u.port||u.username||u.password||u.hash||!SUBTITLE_HOSTS.has(u.hostname)
        ||!/^\/bfs\/(?:subtitle|ai_subtitle)\/[A-Za-z0-9._/-]+$/.test(u.pathname))return '';
      return u.href;
    } catch {return '';}
  }
  // WBI uses MD5 as a request checksum, never as a password/security primitive.
  function md5(text) {
    const data=new TextEncoder().encode(text),size=Math.ceil((data.length+9)/64)*64;
    const padded=new Uint8Array(size);padded.set(data);padded[data.length]=128;
    const view=new DataView(padded.buffer);view.setUint32(size-8,(data.length*8)>>>0,true);
    view.setUint32(size-4,Math.floor(data.length/0x20000000),true);
    const shifts=[[7,12,17,22],[5,9,14,20],[4,11,16,23],[6,10,15,21]];
    let h=[0x67452301,0xefcdab89,0x98badcfe,0x10325476];
    for(let offset=0;offset<size;offset+=64) {
      let [a,b,c,d]=h;
      for(let i=0;i<64;i++) {
        const stage=i>>4;
        const f=stage===0?(b&c)|(~b&d):stage===1?(d&b)|(~d&c):stage===2?b^c^d:c^(b|~d);
        const index=stage===0?i:stage===1?(5*i+1)%16:stage===2?(3*i+5)%16:(7*i)%16;
        const sum=(a+f+Math.floor(Math.abs(Math.sin(i+1))*0x100000000)+view.getUint32(offset+index*4,true))|0;
        const shift=shifts[stage][i%4],rotated=(sum<<shift)|(sum>>>(32-shift));
        [a,b,c,d]=[d,(b+rotated)|0,b,c];
      }
      h=h.map((value,i)=>(value+[a,b,c,d][i])|0);
    }
    return h.map(value=>[0,8,16,24].map(shift=>((value>>>shift)&255).toString(16).padStart(2,'0')).join('')).join('');
  }
  function signedQuery(params,images,now) {
    const key=value=>{
      if(typeof value!=='string')throw 'unavailable';
      const match=/\/([a-fA-F0-9]{32})\.[a-zA-Z0-9]+(?:\?.*)?$/.exec(value);
      if(!match)throw 'unavailable';return match[1];
    };
    const raw=key(images?.img_url)+key(images?.sub_url),secret=MIXIN.map(i=>raw[i]).join('').slice(0,32);
    const values={...params,wts:Math.round(now/1000)};
    const query=Object.keys(values).sort().map(k=>`${encodeURIComponent(k)}=${encodeURIComponent(String(values[k]).replace(/[!'()*]/g,''))}`).join('&');
    return `${query}&w_rid=${md5(query+secret)}`;
  }
  function transcript(body) {
    if(!Array.isArray(body)||!body.length)return null;
    const lines=[];let size=0,truncated=false;
    for(const cue of body) {
      const text=clean(cue?.content);
      if(!text||!Number.isFinite(cue.from)||!Number.isFinite(cue.to)||cue.from<0||cue.to<cue.from)return null;
      const room=12000-size-(lines.length?1:0);
      if(room<=0||lines.length>=2000){truncated=true;break;}
      const selected=text.slice(0,room);lines.push(selected);size+=selected.length+(lines.length>1?1:0);
      if(selected.length<text.length){truncated=true;break;}
    }
    return lines.length?{subtitles:lines,subtitle_truncated:truncated,
      evidence_kind:truncated?'subtitle_excerpt':'full_subtitles'}:null;
  }
  function comments(data) {
    const list=Array.isArray(data?.replies)?data.replies:[],out=[],seen=new Set();
    for(const item of list) {
      const text=clean(item?.content?.message),id=String(item?.rpid_str||item?.rpid||'');
      if(!text||!/^\d{1,30}$/.test(id)||seen.has(id))continue;
      seen.add(id);out.push({id,text:text.slice(0,300),author:clean(item?.member?.uname).slice(0,80),
        likes:Number.isSafeInteger(item.like)&&item.like>=0?item.like:0});
      if(out.length===10)break;
    }
    return out;
  }
  class Reader {
    constructor({fetcher=(...args)=>root.fetch(...args),now=Date.now,timeout=12000}={}) {
      this.fetcher=fetcher;this.now=now;this.timeout=Math.max(1,Math.min(timeout,20000));
    }
    async json(url,{signal,subtitle=false}={}) {
      const target=new URL(url);
      if(subtitle?!subtitleURL(url):target.origin!==API||!PATHS.has(target.pathname)||target.username||target.password||target.hash)throw 'unavailable';
      if(signal?.aborted)throw 'cancelled';
      const response=await this.fetcher(url,{method:'GET',credentials:subtitle?'omit':'include',redirect:'error',
        cache:'no-store',referrerPolicy:'no-referrer',headers:{Accept:'application/json'},signal});
      if(signal?.aborted)throw 'cancelled';
      if(response.status===412||response.status===429)throw 'blocked';
      if(!response.ok||response.redirected||(response.url&&response.url!==target.href))throw 'unavailable';
      if(!/^(?:application\/(?:json|octet-stream)|text\/(?:plain|json))(?:;|$)/i.test(response.headers.get('content-type')||''))throw 'unavailable';
      const cap=1024*1024,length=response.headers.get('content-length');
      if(length!==null&&(!/^\d+$/.test(length)||Number(length)>cap))throw 'unavailable';
      if(!response.body?.getReader)throw 'unavailable';
      const stream=response.body.getReader(),chunks=[];let total=0,done=false;
      try {
        while(true) {
          const next=await stream.read();if(signal?.aborted)throw 'cancelled';
          if(next.done){done=true;break;}
          total+=next.value.byteLength;if(total>cap)throw 'unavailable';chunks.push(next.value);
        }
      } finally {if(!done)await stream.cancel().catch(()=>{});stream.releaseLock();}
      const bytes=new Uint8Array(total);let offset=0;
      for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
      let value;try{value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}catch{throw 'unavailable';}
      if(!value||typeof value!=='object'||Array.isArray(value))throw 'unavailable';
      if(!subtitle) {
        if(value.code===-101)throw 'login_required';
        if([-352,-412,-509,-403,412,429].includes(value.code))throw 'blocked';
        if(value.code!==0)throw 'unavailable';
      }
      return value;
    }
    async readVideo(url,{signal}={}) {
      const parsed=typeof url==='string'&&/^https:\/\/www\.bilibili\.com\/video\/(BV[A-Za-z0-9]{10})(?:\?p=([1-9][0-9]{0,4}))?$/.exec(url);
      if(!parsed||Number(parsed[2]||1)>10000)throw 'invalid_request';
      if(signal?.aborted)throw 'cancelled';
      const part=Number(parsed[2]||1),result={url,bvid:parsed[1],video_part:part,title:'',part_title:'',description:'',
        subtitles:[],evidence_kind:'page_metadata',subtitle_status:'unavailable',subtitle_truncated:false,subtitle_language:''};
      const controller=new AbortController();
      let timer,cancel;
      const stop=new Promise(resolve=>{
        cancel=()=>{controller.abort();resolve(null);};
        signal?.addEventListener('abort',cancel,{once:true});
        timer=root.setTimeout(cancel,this.timeout);
      });
      const get=path=>this.json(API+path,{signal:controller.signal});
      const read=async()=>{
        try {
          const info=(await get('/x/web-interface/view?bvid='+parsed[1])).data;
          if(controller.signal.aborted)return;
          if(info?.bvid!==parsed[1])return;
          result.title=clean(info.title).replace(/\s+/g,' ').slice(0,300);
          result.description=clean(info.desc).slice(0,5000);
          // The requested part must be present explicitly in the API's page
          // list. Never borrow info.cid (usually P1) for a different part.
          const selected=Array.isArray(info.pages)&&info.pages.find(item=>item?.page===part&&positive(item.cid));
          if(!selected){result.subtitle_status='part_unavailable';return;}
          result.video_cid=selected.cid;
          result.part_title=clean(selected.part).replace(/\s+/g,' ').slice(0,300);
          const nav=await get('/x/web-interface/nav');
          const query=signedQuery({bvid:parsed[1],cid:selected.cid},nav.data?.wbi_img,this.now());
          const player=await get('/x/player/wbi/v2?'+query);
          const tracks=player.data?.subtitle?.subtitles;
          if(!Array.isArray(tracks))throw 'unavailable';
          const candidates=tracks.filter(track=>subtitleURL(track?.subtitle_url));
          const track=candidates.find(t=>t.lan==='zh-CN')||candidates.find(t=>t.lan==='ai-zh')||candidates.find(t=>/^zh/.test(t.lan))||candidates[0];
          if(!track){result.subtitle_status=tracks.length?'unavailable':'none';return;}
          const raw=await this.json(subtitleURL(track.subtitle_url),{signal:controller.signal,subtitle:true});
          if(controller.signal.aborted)return;
          const text=transcript(raw.body);if(!text)throw 'unavailable';
          Object.assign(result,text,{subtitle_status:'ready',subtitle_language:clean(track.lan).slice(0,30)});
        } catch(error) {
          if(controller.signal.aborted)return;
          result.subtitle_status=['blocked','login_required'].includes(error)?error:'unavailable';
        }
      };
      try {
        await Promise.race([read(),stop]);
        if(signal?.aborted)throw 'cancelled';
        return structuredClone(result);
      } finally {root.clearTimeout(timer);signal?.removeEventListener('abort',cancel);controller.abort();}
    }
    async enrich(baseline,{signal}={}) {
      const parsed=typeof baseline?.url==='string'&&/^https:\/\/www\.bilibili\.com\/video\/(BV[A-Za-z0-9]{10})$/.exec(baseline.url);
      if(!parsed)throw 'invalid_request';
      if(signal?.aborted)throw 'cancelled';
      const result={...baseline,comments:[],comments_status:'unavailable',subtitle_status:'unavailable',
        subtitle_truncated:false,video_part:1};
      const controller=new AbortController(),cancel=()=>controller.abort();
      signal?.addEventListener('abort',cancel,{once:true});
      const timer=root.setTimeout(cancel,this.timeout);
      const get=(path)=>this.json(API+path,{signal:controller.signal});
      const stopped=error=>{if(signal?.aborted)throw 'cancelled';return error==='blocked'||controller.signal.aborted;};
      try {
        let info;
        try {
          info=(await get('/x/web-interface/view?bvid='+parsed[1])).data;
          if(info?.bvid!==parsed[1]||!positive(info.aid)||!positive(info.cid))return result;
          result.title=clean(info.title).slice(0,300)||result.title;
          result.description=clean(info.desc).slice(0,5000)||result.description;
          result.video_part_count=Array.isArray(info.pages)?info.pages.length:1;
        } catch(error){stopped(error);return result;}
        try {
          const nav=await get('/x/web-interface/nav');
          const query=signedQuery({bvid:parsed[1],cid:info.cid},nav.data?.wbi_img,this.now());
          const player=await get('/x/player/wbi/v2?'+query);
          const tracks=player.data?.subtitle?.subtitles;
          if(!Array.isArray(tracks))throw 'unavailable';
          const candidates=tracks.filter(t=>subtitleURL(t?.subtitle_url));
          const selected=candidates.find(t=>t.lan==='zh-CN')||candidates.find(t=>t.lan==='ai-zh')||candidates.find(t=>/^zh/.test(t.lan))||candidates[0];
          result.subtitle_status=tracks.length?'unavailable':'none';
          if(selected) {
            const raw=await this.json(subtitleURL(selected.subtitle_url),{signal:controller.signal,subtitle:true});
            const text=transcript(raw.body);if(!text)throw 'unavailable';
            Object.assign(result,text,{subtitle_status:'ready',subtitle_language:clean(selected.lan).slice(0,30)});
          }
        } catch(error){if(stopped(error))return result;}
        try {
          const response=await get(`/x/v2/reply?type=1&oid=${info.aid}&sort=2&ps=10&pn=1`);
          result.comments=comments(response.data);result.comments_status='ready';
        } catch(error){stopped(error);}
        return result;
      } finally {root.clearTimeout(timer);signal?.removeEventListener('abort',cancel);controller.abort();}
    }
  }
  const api={Reader,subtitleURL,signedQuery,md5,transcript,comments};
  if(typeof module!=='undefined')module.exports=api;else root.VtuberBiliReader=api;
})(globalThis);
