(function(root) {
  'use strict';
  const MAX_IMAGE=5*1024*1024;
  function safeMeme(message) {
    return !!message && message.type==='companion_meme' && typeof message.id==='string'
      && /^[a-z][a-z0-9_-]{1,63}$/.test(message.id)
      && message.url==='/api/companion/memes/'+message.id;
  }
  class CompanionChat {
    constructor({document,fetcher}) {
      this.document=document;this.fetcher=fetcher;this.allowed=false;this.pending=false;this.selected=null;this.seen=new Set();
      this.el('chat-image').onclick=()=>this.selected?this.upload():this.el('chat-image-file').click();
      this.el('chat-image-file').onchange=()=>{this.selected=this.el('chat-image-file').files?.[0]||null;if(this.selected)this.upload();};
      this.el('chat-image-cancel').onclick=()=>this.clear();
    }
    el(id){return this.document.getElementById(id);}
    setAccess(allowed) {
      this.allowed=allowed===true;this.el('chat-image').hidden=!this.allowed;
      this.el('companion-link').hidden=!this.allowed;
      if(!this.allowed){this.controller?.abort();this.clear();}
    }
    clear(){this.selected=null;this.el('chat-image-file').value='';this.el('chat-image-cancel').hidden=true;this.el('chat-image').textContent='发图片';}
    note(text){this.el('chat-image-notice').textContent=text;}
    async upload() {
      if(!this.allowed||this.pending)return;
      const file=this.selected;
      if(!file||!['image/png','image/jpeg','image/gif','image/webp'].includes(file.type)||!file.size||file.size>MAX_IMAGE){this.note('请选择不超过 5 MiB 的 PNG、JPEG、GIF 或 WebP 图片。');this.clear();return;}
      const text=this.el('text').value,user=this.el('companion-user').value||'你';
      if(text.trim().length>1000){this.note('图片问题最多 1000 字，请缩短后重试。');this.el('chat-image').textContent='重试图片';this.el('chat-image-cancel').hidden=false;return;}
      this.pending=true;this.el('chat-image').disabled=true;this.el('chat-image-cancel').hidden=true;this.note('正在通过本机 Codex 套餐交给 GPT Astra 查看图片…');
      const controller=new AbortController();this.controller=controller;
      const timeout=setTimeout(()=>controller.abort(),150000);
      try {
        const query=new URLSearchParams({text:text.trim(),user:user.trim()});
        const response=await this.fetcher('/api/companion/image?'+query,{method:'POST',credentials:'same-origin',headers:{'Content-Type':file.type},body:file,signal:controller.signal});
        let data;try{data=await response.json();}catch(_){throw Error(response.status===403?'图片功能只供本机主控使用。':'图片处理结果无法确认，内容已保留。');}
        if(!response.ok||data?.ok!==true)throw Error(typeof data?.error==='string'?data.error:'图片未发送成功，请稍后重试。');
        if(!this.allowed)return;
        if(this.el('text').value===text)this.el('text').value='';
        this.clear();this.note('图片已交给她，请在聊天区等候回复。');
      }catch(error){if(this.allowed){this.note(error.name==='AbortError'?'这次图片处理未确认，文字和图片已保留。重试前请先看看是否已收到回复。':error.message);this.el('chat-image').textContent='重试图片';this.el('chat-image-cancel').hidden=false;}}
      finally {clearTimeout(timeout);this.pending=false;this.el('chat-image').disabled=false;this.controller=null;}
    }
    renderMeme(message) {
      if(!this.allowed||!safeMeme(message))return null;
      const key=String(message.turn_id)+':'+message.id;
      if(this.seen.has(key))return null;
      this.seen.add(key);if(this.seen.size>256)this.seen.delete(this.seen.values().next().value);
      const row=this.document.createElement('div');row.className='row reply companion-meme';
      const image=this.document.createElement('img');image.src=message.url;image.alt=String(message.caption||'她发来的表情').slice(0,180);image.loading='lazy';
      image.style.maxWidth='240px';image.style.maxHeight='240px';image.style.objectFit='contain';image.style.borderRadius='10px';
      const caption=this.document.createElement('p');caption.textContent=String(message.caption||'').slice(0,180);
      row.appendChild(image);row.appendChild(caption);this.el('events').appendChild(row);this.el('chat-empty').hidden=true;
      return row;
    }
  }
  const api={CompanionChat,safeMeme};if(typeof module!=='undefined'&&module.exports)module.exports=api;root.CompanionChat=api;
  if(root.document?.getElementById('chat-image'))root.companionChat=new CompanionChat({document:root.document,fetcher:root.fetch.bind(root)});
})(typeof window!=='undefined'?window:globalThis);
