(function (root) {
  'use strict';
  const workerNames = {bilibili:'直播弹幕',mock_danmaku:'模拟弹幕',mock_game:'模拟游戏',game_agent:'游戏代理',game_status:'游戏状态',event_ingest:'事件处理',maintenance:'播放与统计维护',speech:'语音处理',audio_retention:'音频整理',rvc:'翻唱任务',suno:'原创歌曲任务'};
  const states = {running:'运行中',retrying:'等待恢复',failed:'已停止，需重启服务',stopped:'已停止'};
  function healthLabel(data) { return data.ready ? (data.warnings?.length ? '主流程正常，部分扩展需要检查' : '后台运行正常') : '部分功能尚未就绪，请检查下方状态'; }
  function duration(metric) { return metric?.samples ? `${Number(metric.p95).toFixed(2)} 秒` : '未采样'; }
  function size(bytes) { return `${(Number(bytes || 0) / 1048576).toFixed(1)} MiB`; }
  function retentionLabel(s) { return `${s.total_files || 0} 段 · ${size(s.total_bytes)} · ${s.enabled ? `保留 ${s.max_age_days} 天，容量上限 ${size(s.max_bytes)}` : '自动清理已关闭'} · 已收藏 ${s.pinned_count || 0} 段`; }
  const preparationLabels = {ready:'已准备',empty:'没有可用记录',disabled:'未启用',timeout:'超时，本轮已跳过',unavailable:'暂不可用'};
  const outcomeLabels = {generating:'正在准备回复',pending:'等待播放回执',queued:'等待完整播放',delivered:'全部段落已确认播放',failed:'未完整交付',text_only:'仅文字回复',aborted:'已中断',cancelled:'已取消'};
  const rejectionLabels = {inactive:'已过期或已被更新',different_identity:'不属于当前身份',unrelated:'与当前话题无关',low_score:'相关评分不足',quota:'超出本轮条数限制'};
  const memoryTypeLabels = {core:'核心记忆',stable_fact:'稳定事实',temporary_state:'临时状态',plan:'近期计划',episode:'共同经历',commitment:'待办承诺'};
  function recallDetail(memory) {
    const parts=[String(memory.reason || '与本轮相关')];
    if (Number.isFinite(memory.score)) parts.push(`排序分 ${memory.score.toFixed(2)}`);
    if (Number.isFinite(memory.freshness)) parts.push(`新鲜度 ${Math.round(Math.max(0,Math.min(1,memory.freshness))*100)}%`);
    return parts.join(' · ');
  }
  function contextLabel(data) { return data?.turn_id != null ? `最近处理的第 ${data.turn_id} 轮 · ${data.plan?.mode==='structured'?'分要点回应':'直接回应'}` : '尚未处理对话。'; }
  function deliveryLabel(data) { return data?.turn_id != null ? `第 ${data.turn_id} 轮回执：${outcomeLabels[data.outcome] || '等待确认'} · 完成 ${Number(data.completed_segments)||0}/${Number(data.expected_segments)||0} 段。` : '尚无播放审计回执。'; }
  if (typeof module !== 'undefined' && module.exports) module.exports = {healthLabel, duration, size, contextLabel, deliveryLabel};
  if (typeof document === 'undefined') return;
  const el = id => document.getElementById(id);
  workerNames.broadcast_recovery = '自动接话';
  workerNames.voice_readiness = '语音服务';
  let preparingStandby = false;
  const node = (tag, text, className) => { const n=document.createElement(tag); n.textContent=text; if(className)n.className=className; return n; };
  async function request(url, options) {
    const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(8000), ...options});
    if (!response.ok) {
      const data = await response.json().catch(()=>({}));
      throw new Error(response.status === 403 ? '此页面仅供本机主播使用' : data.error || '服务暂不可用，请稍后重试');
    }
    return response.json();
  }
  function row(label, value, detail) {
    const r=node('div',''), left=node('span',label), right=node('span',value);
    if(detail)right.append(node('small',detail)); r.append(left,right); return r;
  }
  async function refreshHealth() {
    try {
      const data=await request('/runtime-status');
      const recovery=data.broadcast_recovery || {};
      el('recovery-status').textContent=(recovery.message || '直播恢复功能尚未就绪') + (recovery.automatic_paused ? ` 剩余约 ${Math.ceil(recovery.retry_in_sec)} 秒。` : '');
      el('standby-status').textContent=recovery.preparing ? '正在用当前音色生成提示音…' : recovery.prepare_error || (recovery.standby_ready ? '当前音色提示音已准备。' : '尚未准备提示音，故障时使用画面提示。');
      el('prepare-standby').disabled=preparingStandby || !!recovery.preparing || !recovery.message;
      el('health').textContent=healthLabel(data); el('health').className=data.ready?'ok':'warn';
      const capabilityNames={voice:'语音服务',qq:'QQ 通道',bilibili:'B站浏览器连接'};
      const capabilityStates={checking:'正在检查',loading:'正在加载',ready:'已就绪',unavailable:'尚未就绪',stopped:'已停止'};
      el('capabilities').replaceChildren(row('文字核心', (data.core_ready ?? data.ready) ? '已就绪' : '尚未就绪', '事件处理、回复调度和持久化状态'), ...Object.entries(data.capabilities || {}).map(([name,c])=>
        row(capabilityNames[name] || name, capabilityStates[c.state] || '尚未确认', String(c.detail || ''))));
      const warningText=(data.warnings || []).map(name=>workerNames[name] || name).join('、');
      el('health-detail').textContent=`更新于 ${new Date().toLocaleTimeString()} · 可恢复任务最多自动重试 3 次；持续失败请检查后重启主服务。${warningText ? ` 需要检查：${warningText}。` : ''}${data.audio_retention?.last_error ? ` ${data.audio_retention.last_error}` : ''}`;
      el('workers').replaceChildren(...Object.entries(data.workers || {}).map(([name,w])=>row(workerNames[name] || name, states[w.state] || w.state, [w.restarts ? `已恢复尝试 ${w.restarts} 次` : '', w.last_error || ''].filter(Boolean).join(' · '))));
      el('stores').replaceChildren(...Object.entries(data.persistence || {}).map(([name,s])=>row(name, s.last_error ? '保存失败，正在重试' : s.pending ? '等待保存' : '已保存', s.last_error ? String(s.last_error) : `本次写入 ${s.writes || 0} 次`)));
      el('tasks-note').textContent=data.tasks_connected === false ? '后台任务服务尚未连接，绘画等委托暂不可用。' : '';
      const c=data.connections || {}; el('connections').textContent=`已连接 ${c.active || 0} 个页面 · 待发送 ${c.queued || 0} 条 · 已断开慢连接 ${c.slow_disconnected || 0} 次`;
      const latency=data.metrics?.latency || {}, labels={audio_start:'首音',llm_ttft:'模型首字',context_prepare:'上下文准备',memory_recall:'长期记忆检索',cognition_recall:'认知记录检索',audit_prepare:'审计准备',tts_synthesis:'语音合成',event_loop_lag:'事件循环延迟'};
      el('latency').replaceChildren(...Object.entries(labels).map(([key,label])=>{ const card=node('div','');card.append(node('span',label),node('strong',duration(latency[key])));return card;}));
      const context=data.reply_context || {}, recall=context.recall || {};
      el('context-status').textContent=contextLabel(context);
      el('context-sources').replaceChildren(...Object.entries({memory_recall:'长期记忆',cognition_recall:'认知记录'}).map(([key,label])=>{
        const source=context.sources?.[key];
        return row(label,source ? preparationLabels[source.status] || '未确认' : '未查询',source?`${(Number(source.seconds)||0).toFixed(3)} 秒`:'');
      }));
      el('reply-plan').textContent=(context.plan?.guidance || []).slice(0,4).map(String).join('；');
      const selected=(recall.selected || []).slice(0,4);
      el('recalled-memories').replaceChildren(...selected.map(memory=>row(`记忆 #${memory.id}${memoryTypeLabels[memory.retention_type] ? ' · '+memoryTypeLabels[memory.retention_type] : ''}`,String(memory.summary || '').slice(0,180),recallDetail(memory))));
      const rejected=Object.entries(recall.rejected || {}).filter(([,count])=>Number(count)>0);
      el('recall-rejections').textContent=(recall.selected?.length?'':'本轮没有可展示的选中记忆。') + (rejected.length?' 未采用原因：'+rejected.map(([reason,count])=>`${rejectionLabels[reason] || '其他筛选'} ${Number(count)} 条`).join('；'):'')
        + (selected.some(memory=>Number.isFinite(memory.score)||Number.isFinite(memory.freshness)) ? ' 评分与新鲜度仅作排序参考，不代表事实真假或置信度。' : '');
      el('delivery-audit').textContent=deliveryLabel(context.delivery);
    } catch(error) { el('health').textContent=error.message; el('health').className='warn'; el('health-detail').textContent='连接中断，页面将在稍后重新检查。'; }
    finally { root.setTimeout(refreshHealth, 5000); }
  }
  async function refreshAudio() {
    el('refresh-audio').disabled=true;
    try {
      const data=await request('/audio-library');
      if(!data.available){el('retention').textContent='音频管理尚未启动。';el('audio-list').replaceChildren();return;}
      const s=data.status || {};
      el('retention').textContent=retentionLabel(s);
      const rows=(data.items || []).map(item=>{
        const line=node('div','','audio-row'), info=node('div','');
        info.append(node('strong',item.name),node('small',`${new Date(item.modified_at*1000).toLocaleString()} · ${size(item.bytes)}${item.protected?' · 正在使用':''}`));
        const player=document.createElement('audio');player.controls=true;player.preload='none';player.src=`/audio/${encodeURIComponent(item.name)}`;
        const button=node('button',item.pinned?'取消收藏':'收藏');button.type='button';button.setAttribute('aria-pressed',String(item.pinned));
        button.onclick=async()=>{button.disabled=true;try{await request('/audio-library-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pin',name:item.name,pinned:!item.pinned})});item.pinned=!item.pinned;s.pinned_count=Math.max(0,(s.pinned_count || 0)+(item.pinned?1:-1));el('retention').textContent=retentionLabel(s);button.textContent=item.pinned?'取消收藏':'收藏';button.setAttribute('aria-pressed',String(item.pinned));el('audio-notice').textContent=item.pinned?'已收藏，自动清理会保留这段语音。':'已取消收藏，将按保留策略整理。';}catch(error){el('audio-notice').textContent=error.message;}finally{button.disabled=false;}};
        line.append(info,player,button);return line;
      });
      el('audio-list').replaceChildren(...rows);
      el('audio-notice').textContent=s.last_error ? `音频整理暂不可用：${s.last_error}` : rows.length ? '显示最近 100 段生成语音。' : '还没有可管理的生成语音。';
    } catch(error) { el('audio-notice').textContent=error.message; }
    finally {el('refresh-audio').disabled=false;}
  }
  el('refresh-audio').onclick=refreshAudio;
  el('prepare-standby').onclick=async()=>{
    preparingStandby=true; el('prepare-standby').disabled=true;
    try { await request('/broadcast-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'prepare_standby'})});el('recovery-notice').textContent='已开始准备；完成后会显示结果。'; }
    catch(error){el('recovery-notice').textContent=error.message;}
    finally{preparingStandby=false;}
  };
  refreshHealth();refreshAudio();
})(typeof window !== 'undefined' ? window : globalThis);
