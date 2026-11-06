'use strict';
(async()=>{
 const params=new URLSearchParams(location.search),status=document.querySelector('#status');
 try{
  if(!params.get('show')||!params.get('card'))throw Error('请选择一张历史栏目卡片');
  const response=await fetch('/shows-drawing-data/'+encodeURIComponent(params.get('show'))+'/'+encodeURIComponent(params.get('card')),{cache:'no-store',signal:AbortSignal.timeout(8000)});
  const result=await response.json();if(!response.ok)throw Error(result.error||'历史作品不可用');
  const canvas=document.querySelector('#paper'),player=new DrawingPlayer(canvas,result.document);
  player.seek(player.duration);canvas.hidden=false;
  document.querySelector('#title').textContent=result.name||result.document.title;
  status.textContent=`${result.theme} · ${new Date(result.closed*1000).toLocaleString('zh-CN')}收播时确认的${result.snapshot.stage==='sketch'?'草稿':'上色版本'}。此页只展示当时的作品，后续修订不会替换这幅画。`;
 }catch(error){status.textContent=error.message;document.querySelector('#title').textContent='历史作品暂不可用';}
})();
