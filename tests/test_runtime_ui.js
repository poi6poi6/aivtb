const assert = require('node:assert/strict');
const {healthLabel, duration, size} = require('../brain/static/runtime.js');
assert.match(healthLabel({ready:false}), /尚未就绪/);
assert.match(healthLabel({ready:true,warnings:['music']}), /扩展需要检查/);
assert.equal(healthLabel({ready:true,warnings:[]}), '后台运行正常');
assert.equal(duration({samples:0,p95:0}), '未采样');
assert.equal(duration({samples:2,p95:1.234}), '1.23 秒');
assert.equal(size(1048576), '1.0 MiB');
console.log('Runtime UI status formatting checks passed');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag=tag;this.children=[];this.textContent='';this.attributes={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  setAttribute(key,value) { this.attributes[key]=value; }
}
(async()=>{
  const html=fs.readFileSync(require.resolve('../brain/static/runtime.html'),'utf8');
  const nodes=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match=>[match[1],new Element('div')]));
  const timers=[], actions=[];
  let offline=false;
  const data={ready:true,warnings:[],workers:{speech:{state:'running',restarts:0}},persistence:{memory:{writes:4}},connections:{active:2},metrics:{latency:{audio_start:{samples:3,p95:1.8}}}};
  const context={document:{getElementById:id=>nodes[id],createElement:tag=>new Element(tag)},
    window:{setTimeout:callback=>timers.push(callback)},AbortSignal:{timeout:()=>undefined},
    fetch:async(url,options)=>{
      if(offline)throw Error('fixture offline');
      if(url==='/runtime-status')return {ok:true,json:async()=>data};
      if(url==='/audio-library')return {ok:true,json:async()=>({available:true,status:{enabled:true,total_files:1,total_bytes:10,max_age_days:7,max_bytes:1048576,pinned_count:0},items:[{name:'1800000000_1_index.wav',bytes:10,modified_at:1800000000,pinned:false,protected:true}]})};
      actions.push(JSON.parse(options.body));return {ok:true,json:async()=>({pinned:true})};
    }};
  vm.runInNewContext(fs.readFileSync(require.resolve('../brain/static/runtime.js'),'utf8'),context);
  await new Promise(setImmediate);
  assert.equal(nodes.health.textContent,'后台运行正常');
  assert.equal(nodes['audio-list'].children.length,1);
  assert.equal(nodes['context-status'].textContent,'尚未处理对话。');
  data.core_ready=true;
  data.capabilities={voice:{ready:false,state:'loading',detail:'语音模型正在加载'},qq:{ready:false,state:'unavailable'}};
  data.reply_context={turn_id:7,plan:{mode:'structured',guidance:['先回应问题','只引用明确事实']},
    sources:{memory_recall:{status:'ready',seconds:.015},cognition_recall:{status:'timeout',seconds:.75}},
    recall:{selected:[{id:2,summary:'<img src=x onerror=alert(1)>',reason:'相关主题',
      retention_type:'stable_fact',score:1.237,freshness:.84},
      {id:3,summary:'旧版记录',reason:'明确询问历史'}],rejected:{unrelated:3,different_identity:2}},
    delivery:{turn_id:7,outcome:'queued',expected_segments:2,completed_segments:1}};
  await timers.shift()();
  assert.equal(nodes.capabilities.children[0].children[0].textContent,'文字核心');
  assert.equal(nodes.capabilities.children[0].children[1].textContent,'已就绪');
  assert.equal(nodes.capabilities.children[1].children[1].textContent,'正在加载');
  assert.equal(nodes.capabilities.children[2].children[1].textContent,'尚未就绪');
  assert.match(nodes['context-status'].textContent,/分要点回应/);
  assert.equal(nodes['recalled-memories'].children[0].children[1].textContent,'<img src=x onerror=alert(1)>');
  assert.match(nodes['recalled-memories'].children[0].children[0].textContent,/稳定事实/);
  assert.equal(nodes['recalled-memories'].children[0].children[1].children[0].textContent,'相关主题 · 排序分 1.24 · 新鲜度 84%');
  assert.equal(nodes['recalled-memories'].children[1].children[1].children[0].textContent,'明确询问历史','legacy diagnostics still render without invented scores');
  assert.match(nodes['recall-rejections'].textContent,/与当前话题无关 3 条/);
  assert.match(nodes['recall-rejections'].textContent,/不代表事实真假或置信度/);
  assert.match(nodes['delivery-audit'].textContent,/等待完整播放/);
  data.reply_context.recall.selected=[{id:4,summary:'即将过期的计划',reason:'本轮询问计划',retention_type:'plan',score:0,freshness:0}];
  await timers.shift()();
  assert.match(nodes['recalled-memories'].children[0].children[0].textContent,/近期计划/);
  assert.equal(nodes['recalled-memories'].children[0].children[1].children[0].textContent,'本轮询问计划 · 排序分 0.00 · 新鲜度 0%');
  data.reply_context.recall={selected:[],rejected:{}};
  data.reply_context.delivery={turn_id:7,outcome:'failed',expected_segments:2,completed_segments:1};
  await timers.shift()();
  assert.equal(nodes['recalled-memories'].children.length,0);
  assert.match(nodes['delivery-audit'].textContent,/未完整交付/);
  const audioRow=nodes['audio-list'].children[0];
  assert.match(audioRow.children[1].src,/^\/audio\/1800000000_1_index.wav$/);
  await audioRow.children[2].onclick();
  assert.deepEqual(actions,[{action:'pin',name:'1800000000_1_index.wav',pinned:true}]);
  assert.equal(audioRow.children[2].textContent,'取消收藏');
  assert.match(nodes.retention.textContent,/已收藏 1 段/);
  data.broadcast_recovery={message:'自动接话暂缓',automatic_paused:true,retry_in_sec:12.1,preparing:false,standby_ready:false};
  await timers.shift()();
  assert.match(nodes['recovery-status'].textContent,/13 秒/);
  assert.match(nodes['standby-status'].textContent,/画面提示/);
  await nodes['prepare-standby'].onclick();
  assert.deepEqual(actions[1],{action:'prepare_standby'});
  assert.match(nodes['recovery-notice'].textContent,/已开始/);
  data.broadcast_recovery={message:'可用',preparing:true};
  await timers.shift()();
  assert.equal(nodes['prepare-standby'].disabled,true);
  assert.match(nodes['standby-status'].textContent,/正在/);
  offline=true;
  await timers.shift()();
  assert.equal(nodes.health.className,'warn');
  assert.match(nodes['health-detail'].textContent,/连接中断/);
  console.log('PASS: runtime page rendering, explicit audio pin, counts and disconnected health');
})().catch(error=>{console.error(error);process.exitCode=1;});
