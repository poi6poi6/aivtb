'use strict';
const notice = document.querySelector('#notice');
async function refresh() {
  try {
    const response = await fetch('/minecraft-status', {cache:'no-store'});
    if (!response.ok) throw new Error('无法读取状态，请确认本机主服务正在运行');
    const s = await response.json();
    const p = s.position;
    const c = s.campaign;
    document.querySelector('#campaign-progress').textContent = c ? [c.completed ? '已确认击败末影龙' : `当前阶段：${c.phaseName}`, c.blocked ? `卡点：${c.blocked}` : '', `已记录 ${Object.keys(c.waypoints || {}).length} 个地点 · ${c.deaths.length} 次死亡`, ...(c.recent || []).slice(-3).map(r=>`${r.ok ? '✓' : '未完成'} ${r.text}`)].filter(Boolean).join(' · ') : '连接游戏后读取本世界进度。';
    document.querySelector('#facts').textContent = [s.activity || '未连接', s.connected ? `血量 ${s.health}/20 · 饥饿 ${s.food}/20` : s.networkConnected ? (s.dead ? '服务器已连接 · 角色已死亡' : '服务器已连接 · 等待出生') : '尚未连接服务器', p ? `坐标 ${p.x.toFixed(1)}, ${p.y.toFixed(1)}, ${p.z.toFixed(1)}` : '', s.error || '', `背包：${(s.inventory || []).map(i=>`${i.name} × ${i.count}`).join('、') || '空'}`].filter(Boolean).join('\n');
    for (const b of document.querySelectorAll('[data-action]')) {
      const a = b.dataset.action;
      b.disabled = a === 'connect' ? s.connected || s.networkConnected || s.connecting : a === 'respawn' ? !s.dead || !s.networkConnected || s.respawning || s.busy : !['stop','disconnect'].includes(a) && (!s.connected || s.busy);
    }
    const list = document.querySelector('#history');
    list.replaceChildren(...(s.history || []).slice().reverse().map(t => {const li=document.createElement('li');li.textContent=t;return li;}));
  } catch (e) {notice.textContent=e.message;}
}
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click', async()=>{
  button.disabled=true;
  try {
    const r=await fetch('/minecraft-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:button.dataset.action,target:document.querySelector('#target').value.trim()})});
    const data=await r.json();
    if(!r.ok) throw new Error(data.error || '指令未提交');
    notice.textContent=data.message;
  }catch(e){notice.textContent=e.message;}
  finally{button.disabled=false;await refresh();}
}));
async function poll(){await refresh();setTimeout(poll,2000);}poll();
