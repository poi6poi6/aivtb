(() => {
  const edits = new Map();
  const notice = text => { document.getElementById('music-notice').textContent = text; };
  const button = (label, click) => { const b = document.createElement('button'); b.type='button'; b.textContent=label; b.onclick=click; return b; };
  async function action(data) {
    const response = await fetch('/suno-manual', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '操作失败');
  }
  function handoff(row, item) {
    const section = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent='编辑文稿 · 复制到 Suno · 导入成品'; section.append(summary);
    const fields = {}; const draft = edits.get(item.id) || item.draft || {title:item.query, style:'', lyrics:''};
    for (const [key, label] of [['title','标题'],['style','风格'],['lyrics','歌词']]) {
      const group=document.createElement('div'); const caption=document.createElement('label'); caption.textContent=label;
      const input=document.createElement('textarea'); input.value=draft[key]; input.rows=key==='lyrics'?9:2;
      input.style.cssText='display:block;width:100%;box-sizing:border-box;background:#183126;color:#eee7d4;border:1px solid #65765c;border-radius:10px;padding:10px';
      input.setAttribute('aria-label',label); caption.append(input); fields[key]=input;
      input.oninput=()=>edits.set(item.id,Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value])));
      group.append(caption,button('复制'+label,async()=>{try {await navigator.clipboard.writeText(input.value);notice(label+'已复制');} catch (_) {input.focus();input.select();notice('请按 Ctrl+C 复制已选内容');}})); section.append(group);
    }
    section.append(button('保存文稿',async()=>{
      try {await action({action:'save',id:item.id,draft:Object.fromEntries(Object.entries(fields).map(([k,v])=>[k,v.value]))}); edits.delete(item.id);notice('文稿已保存');}
      catch(error){notice(error.message);}
    }));
    const link=document.createElement('a'); link.href='https://suno.com/create';link.target='_blank';link.rel='noopener noreferrer';link.textContent='打开 Suno 网页';link.style.margin='12px';section.append(link);
    const hint=document.createElement('p');hint.textContent='在 Suno 选择你的音色，粘贴标题、风格和歌词后生成。试听并将下载文件放到项目 data/music/suno-inbox；下方手动选择对应成品。';section.append(hint);
    const select=document.createElement('select');select.setAttribute('aria-label','选择下载的Suno成品');
    const fill=async()=>{
      try {const response=await fetch('/suno-inbox',{cache:'no-store'});if(!response.ok)throw new Error();const data=await response.json();const old=select.value;select.replaceChildren();
        const empty=document.createElement('option');empty.value='';empty.textContent='选择对应音频';select.append(empty);
        data.files.forEach(file=>{const opt=document.createElement('option');opt.value=file.id;opt.textContent=`${file.name} (${(file.bytes/1048576).toFixed(1)} MB)`;select.append(opt);});
        if(data.files.some(f=>f.id===old))select.value=old;
      }catch(_){notice('下载目录读取失败，请稍后刷新');}
    };
    section.append(select,button('刷新下载列表',fill));
    const importButton=button('确认此文件并入队',async()=>{
      if(!select.value){notice('请先选择此任务对应的音频文件');return;}
      importButton.disabled=true;notice('正在验证并导入音频…');
      try {await action({action:'import',id:item.id,file:select.value});edits.delete(item.id);await window.refreshMusicRequests();notice('成品已导入并发送到播放队列');}
      catch(error){notice(error.message);}finally{importButton.disabled=false;}
    });section.append(importButton);
    let poll;
    section.ontoggle=()=>{clearInterval(poll);if(section.open){fill();poll=setInterval(()=>{if(!section.isConnected){clearInterval(poll);return;}fill();},5000);}};
    row.append(section);
  }
  window.refreshMusicRequests = async () => {
    const list = document.getElementById('music-requests');
    try {
      const response=await fetch('/music-requests',{cache:'no-store'});if(!response.ok)throw new Error();
      const data=await response.json();list.replaceChildren();
      data.items.filter(item=>item.state!=='cancelled').forEach(item=>{
        const row=document.createElement('div');row.style.cssText='border-top:1px solid #526655;padding:14px 0';
        const label=document.createElement('p');
        const states={needs_setup:'等待准备',awaiting_draft:'等待文稿',drafting:'准备文稿中',waiting_suno:'等网页生成',draft_failed:'文稿失败',waiting_source:'等原曲',processing:'制作中',ready:'已完成',failed:'失败'};
        label.textContent=`${item.mode==='cover'?'RVC翻唱':'Suno原创'} · ${item.query} · ${states[item.state]||item.state}：${item.detail}`;row.append(label);
        const owner=!document.getElementById('companion-shell').classList.contains('friend-test');
        if(owner){
          row.append(button('取消请求',()=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'music_cancel',id:item.id}));}));
          if(item.state==='ready'||item.state==='failed')row.append(button(item.state==='ready'?'播放成品':'重试',()=>{if(ws.readyState===1)ws.send(JSON.stringify({type:item.state==='ready'?'music_play':'music_retry',id:item.id}));}));
          if(item.mode==='original' && item.state!=='ready') {
            if(item.state==='draft_failed')row.append(button('重新准备文稿',async()=>{try{await action({action:'retry',id:item.id});}catch(error){notice(error.message);}}));
            handoff(row,item);
          }
        }
        list.append(row);
      });
    }catch(_){notice('音乐待办加载失败，请确认服务已启动。');}
  };
  document.getElementById('open-songs').addEventListener('click',window.refreshMusicRequests);
})();
