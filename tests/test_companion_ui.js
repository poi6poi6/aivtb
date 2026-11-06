const assert=require('node:assert/strict');
const fs=require('node:fs');
const {CompanionPage,hongKongEpoch,hongKongInput,memeURL,imageFile}=require('../brain/static/companion.js');
const {CompanionChat,safeMeme}=require('../brain/static/companion-chat.js');

class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.textContent='';this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.style={};this.listeners={};this.files=[];}
  set innerHTML(_){throw Error('Dynamic HTML is not allowed in companion controls');}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  querySelectorAll(selector){return this.children.flatMap(child=>[...(selector.split(',').includes(child.tag)?[child]:[]),...child.querySelectorAll(selector)]);}
  click(){this.clicked=true;this.onclick?.();}
}
function documentFor(file){
  const html=fs.readFileSync(require.resolve(file),'utf8');
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);assert.equal(new Set(ids).size,ids.length);
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const find=(node,id)=>node.id===id?node:node.children.map(child=>find(child,id)).find(Boolean);
  return {nodes,document:{getElementById:id=>nodes[id]||Object.values(nodes).map(node=>find(node,id)).find(Boolean),createElement:tag=>new Element(tag)}};
}
const event={preventDefault(){}};
const descend=node=>[node,...node.children.flatMap(descend)];

(async()=>{
  assert.equal(hongKongEpoch('2026-10-29T09:30'),Date.parse('2026-10-29T01:30:00Z')/1000);
  assert.equal(hongKongInput(hongKongEpoch('2026-10-29T09:30')),'2026-10-29T09:30');
  assert.throws(()=>hongKongEpoch('not-a-time'));
  assert.equal(memeURL('../private'),null);
  assert.throws(()=>imageFile({type:'text/html',size:10}));
  assert.throws(()=>imageFile({type:'image/png',size:6*1024*1024}));
  const {document,nodes}=documentFor('../brain/static/companion.html');
  const calls=[];let fail=false;
  const state={settings:{memes_enabled:true,auto_memes:false,journal_enabled:true,vision:{enabled:false}},
    vision:{message:'尚未启用',backend:'codex_local',model:'gpt-6-astra'},mood:{enabled:true,label:'沉静',valence:.55,arousal:.38,last_changed_at:1800000000,half_life_sec:7200},
    followups:{settings:{enabled:true,quiet_start_hour:23,quiet_end_hour:8,min_idle_sec:90,min_gap_sec:300,daily_budget:8}},
    calendar:[{id:'calendar1',title:'<script>不是脚本</script>',due_at:1800010000,user:'你',status:'pending'}],
    journal:[{id:'journal1',date:'2026-10-29',user:'你',summary:'<img src=x onerror=alert(1)>',owner_key:'PRIVATE_OWNER_KEY'}],
    memes:[{id:'meme-demo',caption:'<b>表情</b>',tags:['平静'],enabled:true},{id:'meme-disabled',caption:'停用的图',tags:[],enabled:false},{id:'../bad',caption:'bad',tags:[]}]};
  const fetcher=async(path,options)=>{calls.push({path,options});if(options.method==='POST')return {ok:!fail,json:async()=>fail?{error:'保存失败，输入保留'}:{ok:true}};return {ok:true,json:async()=>state};};
  const page=new CompanionPage({document,fetcher,window:{addEventListener(){}}});
  await page.refresh();
  assert.equal(nodes['mood-valence'].textContent,'55%');assert.match(nodes['mood-detail'].textContent,/2 小时/);
  assert.match(nodes['vision-backend'].textContent,/gpt-6-astra/);assert.equal(nodes['vision-model'],undefined);assert.equal(nodes['vision-key-env'],undefined);
  assert.equal(nodes['meme-list'].children.length,2,'unsafe IDs are not rendered');
  assert.equal(descend(nodes['meme-list'].children[1]).filter(n=>n.tag==='img').length,0,'disabled asset is not requested');
  assert.equal(descend(nodes['journal-list']).find(n=>n.tag==='textarea').value,'<img src=x onerror=alert(1)>');
  assert(!descend(nodes['journal-list']).some(n=>n.textContent.includes('PRIVATE_OWNER_KEY')));
  nodes['vision-enabled'].checked=true;nodes['vision-form'].listeners.input();
  await page.refresh();assert.equal(nodes['vision-enabled'].checked,true,'refresh preserves drafts');
  fail=true;await nodes['vision-form'].onsubmit(event);
  assert.equal(nodes['vision-enabled'].checked,true);assert.match(nodes.notice.textContent,/输入保留/);
  const savedVision=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
  assert.deepEqual(savedVision.settings.vision,{enabled:true},'local subscription needs only an enable switch');
  nodes['calendar-title-input'].value='明天聊画稿';nodes['calendar-due'].value='2026-10-30T10:00';nodes['calendar-user'].value='你';
  await nodes['calendar-form'].onsubmit(event);assert.equal(nodes['calendar-title-input'].value,'明天聊画稿');
  fail=false;await nodes['calendar-form'].onsubmit(event);
  const calendar=JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body);
  assert.equal(calendar.action,'calendar_create');assert.equal(calendar.due_at,hongKongEpoch('2026-10-30T10:00'));
  assert.equal(nodes['calendar-title-input'].value,'');
  const reenable=descend(nodes['meme-list'].children[1]).find(n=>n.textContent==='重新启用');await reenable.onclick();
  assert.equal(JSON.parse(calls.filter(c=>c.options.method==='POST').at(-1).options.body).enabled,true);
  const file={type:'image/png',size:40,name:'private.png'};nodes['meme-file'].files=[file];nodes['meme-caption'].value='她的表情';nodes['meme-tags'].value='开心，收到';
  await page.upload();const upload=calls.find(c=>c.path.startsWith('/api/companion/memes?'));
  assert.equal(upload.options.body,file);assert.equal(upload.options.headers['Content-Type'],'image/png');
  assert.equal(nodes['meme-caption'].value,'');
  console.log('PASS: companion settings, safe six-panel rendering, timezone, retained drafts and local uploads');

  const chatDocument=documentFor('../brain/static/index.html');const chatNodes=chatDocument.nodes;let imageFail=true;const imageCalls=[];
  const chat=new CompanionChat({document:chatDocument.document,fetcher:async(path,options)=>{imageCalls.push({path,options});return {ok:!imageFail,json:async()=>imageFail?{error:'视觉模型尚未配置'}:{ok:true}};}});
  chatNodes.text.value='看看这张图片';chatNodes['companion-user'].value='你';chat.selected=file;
  await chat.upload();assert.equal(imageCalls.length,0,'unknown or public mode cannot upload');
  chat.setAccess(true);await chat.upload();assert.equal(chatNodes.text.value,'看看这张图片');assert.equal(chat.selected,file);assert.equal(chatNodes['chat-image'].textContent,'重试图片');
  imageFail=false;await chat.upload();assert.equal(chatNodes.text.value,'');assert.equal(chat.selected,null);assert.equal(imageCalls.at(-1).options.body,file);
  assert.equal(new URL(imageCalls.at(-1).path,'http://localhost').searchParams.get('text'),'看看这张图片');
  const message={type:'companion_meme',turn_id:4,id:'meme-demo',url:'/api/companion/memes/meme-demo',caption:'<script>文字</script>'};
  const row=chat.renderMeme(message);assert(row);assert.equal(row.children[1].textContent,'<script>文字</script>');assert.equal(chat.renderMeme(message),null,'receipts are deduplicated');
  for(const url of ['https://evil.test/image.png','/api/companion/memes/../secret','/api/companion/memes/meme-demo?unsafe=1'])assert.equal(safeMeme({...message,url}),false);
  chat.setAccess(false);assert.equal(chatNodes['chat-image'].hidden,true);assert.equal(chatNodes['companion-link'].hidden,true);assert.equal(chat.renderMeme({...message,turn_id:5}),null);
  console.log('PASS: chat image input failure/retry, public-mode isolation and strict local meme rendering');
})().catch(error=>{console.error(error);process.exitCode=1;});
