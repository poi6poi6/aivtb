'use strict';
const $ = selector => document.querySelector(selector);
const clean = new URLSearchParams(location.search).get('clean') === '1';
if (clean) document.documentElement.classList.add('clean');
let current = null, reading = null, online = false, busy = false, sequence = 0;
let entries = [], entriesSignature = '', cardsSignature = '', historySignature = '';
let choicesSignature = '', lastReadingId = '', lastVisibleCards = new Set();
let serverTime = 0;
const stateLabels = {
  welcoming:'请来客入座', awaiting_confirmation:'等来客确认问题', awaiting_reply:'听听你的回应',
  closing:'收好这一轮', awaiting_host:'等主播确认文字', paused:'茶会暂停片刻',
  selecting:'等来客选牌', revealed:'牌面已揭晓', queued:'解读已排队',
  generated:'解读已生成', failed:'解读未完成', interrupted:'解读已中断', finished:'本轮已收束',
};
const stateNotes = {
  selecting:'牌序已经固定。选好牌号后，才会翻开这几张牌。',
  revealed:'这是本轮实际抽到的牌。等主播邀请助手解读。',
  queued:'解读正在排队；尚未确认已经播出。',
  generated:'解读已生成，请在角色画面确认字幕与声音是否播出。',
  failed:'这次解读没有完成。牌面已保留，可使用同一组牌重试。',
  interrupted:'这次解读已中断。牌面已保留，需由主播决定是否重试。',
  finished:'这一轮先收到这里。留下你愿意带走的一点想法。',
};
const actionMessages = {
  create:'茶会已开启，等待你请来客入座。', select:'来客已入座，请按牌桌提示继续。',
  draw:'选牌已确认，牌面已固定。', interpret:'解读请求已提交，请在角色画面确认播出。',
  finish:'这一位的解读已收起。', close:'这桌茶会已收起。',
  test_join:'测试报名已提交。', test_choose:'测试选牌已提交，请核对牌面。',
  test_reply:'回应已提交，请按牌桌提示继续。', pause:'这一轮已暂停。', resume:'已恢复这一轮，请留意牌桌提示。',
  takeover:'已转为手动推进，之后由你决定是否解读与结束。', confirm_delivered:'文字展示已确认，可以继续这一轮。',
};
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function text(value, limit=400) { return typeof value === 'string' ? value.slice(0, limit) : ''; }
function cardCount(value) { return value === 3 ? 3 : 1; }
function sessionOpen() { return current?.state === 'open'; }
function guided() { return (reading?.hosting || current?.hosting) === 'guided'; }
function activeReading() { return sessionOpen() && reading && reading.state !== 'finished'; }
function paused() { return reading?.paused === true || reading?.state === 'paused'; }
function canSelect() { return sessionOpen() && (!reading || reading.state === 'finished'); }
function canDraw() { return sessionOpen() && !paused() && reading?.state === 'selecting'; }
function canInterpret() { return activeReading() && !paused() &&
  (['failed','interrupted'].includes(reading.state) || !guided() && reading.state === 'revealed'); }
function canReply() { return activeReading() && guided() && !paused() &&
  ['awaiting_confirmation','awaiting_reply'].includes(reading.state); }

function publicStage() {
  if(!reading)return sessionOpen()?'等来客入座':current?'茶会已收起':'尚未开桌';
  if(['queued','generated'].includes(reading.state))return reading.stage==='welcome'?'请来客稍候':reading.stage==='closing'?'收好这一轮':'听她说说牌面';
  if(['failed','interrupted','awaiting_host'].includes(reading.state))return '稍等片刻';
  return stateLabels[reading.state] || '请稍候';
}
function audienceInstruction() {
  const user=text(reading?.user,30);
  if(paused())return '这一轮暂停片刻，牌面会为你留着。';
  if(reading?.state==='awaiting_confirmation')return `${user}，是她理解的这个问题吗？直接回复确认，或补充一句；也可以说“结束”。`;
  if(reading?.state==='awaiting_reply')return `${user}，你愿意回应她一句吗？直接发弹幕即可；说“结束”也可以。`;
  if(canDraw())return `${user}，发送「${cardCount(reading.card_count)===3?'抽牌 3 12 18':'抽牌 7'}」选牌，牌号 1–22${cardCount(reading.card_count)===3?'，不能重复':''}。`;
  if(reading?.state==='revealed' || reading?.state==='queued'&&reading.stage==='reading') {
    const numbers=Array.isArray(reading.positions)?reading.positions.filter(number=>Number.isInteger(number)&&number>=1&&number<=22):[];
    if(numbers.length)return `${user}，已收到你选的 ${numbers.join('、')} 号牌。等她慢慢翻开。`;
  }
  if(activeReading())return `${user}，这一轮还在为你留着。请跟着她的提示慢慢来。`;
  return sessionOpen()?'弹幕发送「塔罗 想聊的问题」报名 · 问题公开 · 每场每位观众一次':'';
}
function updateWaitClock() {
  const target=$('#wait-clock');
  target.hidden=!activeReading() || !guided();
  if(target.hidden)return;
  if(!online){target.textContent='连接恢复后再核对等待时间。';return;}
  if(paused()){target.textContent='回应计时已暂停。';return;}
  const deadline=reading.wait_deadline;
  if(typeof deadline!=='number' || !Number.isFinite(deadline) || deadline<=0) {target.hidden=true;return;}
  const seconds=Math.max(0,Math.ceil(deadline-serverTime));
  target.textContent=seconds>0?`为你留着位置 · 还可回应约 ${seconds} 秒`:'等待主持继续，请以牌桌提示为准。';
}

// Decorative geometry is authored here, never parsed from model or viewer text.
// It is not a second draw: the server owns all identities, positions and faces.
function cardArt(card=null) {
  const back=!card;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 160 220');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', 'card-art');
  const add = (tag, attributes) => {
    const shape = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, String(value));
    svg.append(shape); return shape;
  };
  add('path', {d:'M25 177V78A55 55 0 0 1 135 78V177', fill:'none', stroke:'currentColor', 'stroke-width':'.8', opacity:'.5'});
  add('path', {d:'M34 175V82A46 46 0 0 1 126 82V175', fill:'none', stroke:'currentColor', 'stroke-width':'.5', opacity:'.25'});
  add('circle', {cx:80,cy:91,r:31,fill:'none',stroke:'currentColor','stroke-width':'1'});
  if (back) {
    add('path', {d:'M80 53L92 80L119 91L92 103L80 129L68 103L41 91L68 80Z', fill:'none',stroke:'currentColor','stroke-width':'1.1'});
    add('circle', {cx:80,cy:91,r:9,fill:'none',stroke:'currentColor','stroke-width':'.8'});
  } else {
    const id=/^major-(\d{2})$/.exec(String(card.id));
    const number=id?Number(id[1]):-1;
    const stroke={fill:'none',stroke:'currentColor','stroke-width':'1.4','stroke-linecap':'round','stroke-linejoin':'round'};
    const line=d=>add('path',{d,...stroke});
    const circle=(x,y,r)=>add('circle',{cx:x,cy:y,r,...stroke});
    const star=(x,y,r=16)=>add('path',{d:`M${x} ${y-r}L${x+4} ${y-4}L${x+r} ${y}L${x+4} ${y+4}L${x} ${y+r}L${x-4} ${y+4}L${x-r} ${y}L${x-4} ${y-4}Z`,...stroke});
    // Each major has a small original emblem, not a reproduction of a deck.
    if(number===0){line('M54 109L68 80L80 101L93 74L108 110M62 119Q80 104 98 119');star(84,66,7);}
    else if(number===1){line('M63 116L96 68M61 115L65 119M94 65L99 70');star(72,76,9);circle(99,106,5);}
    else if(number===2){line('M58 115V74M102 115V74M54 74H62M98 74H106M66 112H94');add('path',{d:'M86 69A18 18 0 1 0 94 100A16 16 0 0 1 86 69Z',fill:'currentColor',opacity:'.8'});}
    else if(number===3){line('M58 91L54 74L69 83L80 68L91 83L106 74L102 91ZM62 97H98M80 100V119M80 112Q62 95 61 109Q65 121 80 116M80 112Q98 95 99 109Q95 121 80 116');}
    else if(number===4){line('M60 116V80H68V71H77V80H84V71H93V80H101V116ZM72 116V101Q80 89 88 101V116');}
    else if(number===5){line('M66 77H94L99 111H61ZM80 65V77M73 69H87M70 117H90');star(80,94,10);}
    else if(number===6){line('M80 112Q47 89 58 77Q70 64 80 82Q90 64 102 77Q113 89 80 112');star(80,64,5);}
    else if(number===7){line('M58 103L63 82H97L103 103ZM70 82L80 69L90 82M67 107V116M94 107V116');circle(65,108,5);circle(96,108,5);}
    else if(number===8){circle(80,91,25);line('M65 83L59 71L72 76M95 83L101 71L88 76M68 91H73M87 91H92M75 101L80 105L85 101M80 105V111');}
    else if(number===9){line('M65 78H95L100 112H60ZM69 78V70Q80 55 91 70V78M66 117H94');star(80,95,11);}
    else if(number===10){circle(80,91,24);circle(80,91,8);line('M80 67V83M80 99V115M56 91H72M88 91H104M63 74L74 85M86 97L97 108M63 108L74 97M86 85L97 74');}
    else if(number===11){line('M80 68V116M68 118H92M58 82H102M63 83L53 101H73ZM97 83L87 101H107Z');circle(80,73,4);}
    else if(number===12){line('M58 68H102M80 68V83M80 83L67 95L82 100M82 100V110M82 100L97 90M70 113L82 108L95 115');circle(82,118,6);}
    else if(number===13){line('M80 116V98Q60 91 63 75Q83 77 80 98Q97 85 104 93Q100 108 80 105M68 120H94');star(99,70,7);}
    else if(number===14){line('M55 75H76L73 91H58ZM85 101H107L104 118H88ZM66 94Q75 109 86 106M75 83Q87 86 94 99');}
    else if(number===15){line('M59 87Q49 78 59 70Q67 64 74 74L83 84M101 98Q112 109 102 117Q93 122 85 111L78 103M69 87L76 94M84 95L91 102M76 84L83 79M76 109L69 114');}
    else if(number===16){line('M64 119V85H95V119M70 85V75H88V85M62 119H101M81 85L73 99L89 104L79 117M103 62L89 76H102L95 85');}
    else if(number===17){star(80,91,24);star(55,71,6);star(107,112,6);circle(104,69,2);circle(55,113,2);}
    else if(number===19){circle(80,91,16);for(let i=0;i<8;i++){const angle=i*Math.PI/4;line(`M${80+Math.cos(angle)*22} ${91+Math.sin(angle)*22}L${80+Math.cos(angle)*29} ${91+Math.sin(angle)*29}`);}}
    else if(number===20){line('M62 105Q67 93 67 83Q80 63 93 83Q93 94 98 105ZM75 110Q80 122 85 110M80 65V72M55 85L49 81M105 85L111 81');}
    else if(number===21){add('ellipse',{cx:80,cy:91,rx:21,ry:29,...stroke});star(80,91,12);line('M55 75L48 70M55 107L48 113M105 75L112 70M105 107L112 113');}
    else {add('path', {d:'M91 63A29 29 0 1 0 102 113A25 25 0 0 1 91 63Z', fill:'currentColor',opacity:'.8'});star(106,82,7);}
  }
  for (const x of [48,112]) {
    const direction = x < 80 ? 1 : -1;
    add('path', {d:`M${x} 183Q${x-direction*10} 156 ${x+direction*6} 132`,fill:'none',stroke:'currentColor','stroke-width':'.9',opacity:'.75'});
    for (const y of [151,166,178]) add('path', {d:`M${x} ${y}q${direction*16} -14 ${direction*18} -4q${-direction*7} 11 ${-direction*18} 4`,fill:'currentColor',opacity:'.36'});
  }
  add('path', {d:'M65 184H95M70 190H90M80 200v-7M76 196h8',stroke:'currentColor','stroke-width':'.8',opacity:'.7'});
  for (const [x,y] of [[48,45],[112,45],[80,25],[31,115],[129,115]]) add('circle',{cx:x,cy:y,r:1.4,fill:'currentColor',opacity:'.65'});
  return svg;
}
function tarotCard(card, index, count, pendingPosition=null) {
  const figure = node('figure', undefined, 'tarot-card');
  const fallback = count === 3 ? ['眼下','阻碍','可尝试的方向'][index] : '今晚的一点提醒';
  figure.append(node('p', text(card?.label, 40) || fallback, 'card-position'));
  const face = node('div', undefined, 'card-face');
  const rawPosition=card?.position ?? pendingPosition;
  const position = Number.isInteger(rawPosition) && rawPosition >= 1 && rawPosition <= 22 ? rawPosition : null;
  if(!card&&position)face.classList.add('chosen-card');
  face.append(node('span', position ? `NO. ${String(position).padStart(2,'0')}` : card?'✧':'✧  ✧  ✧', 'card-index'));
  const art = cardArt(card);
  if (card?.orientation === 'reversed') art.classList.add('reversed');
  face.append(art, node('h3', card ? text(card.name, 60) : '待揭晓', 'card-name'));
  if (card) face.append(node('p', card.orientation === 'reversed' ? '逆位' : card.orientation === 'upright' ? '正位' : '方向待确认', 'card-orientation'));
  figure.append(face);
  if (card) {
    const keywords = Array.isArray(card.keywords) ? card.keywords.slice(0,6).map(item=>text(item,30)).filter(Boolean) : [];
    if (keywords.length) figure.append(node('p', keywords.join(' · '), 'card-keywords'));
    figure.append(node('figcaption', text(card.meaning, 600), 'card-caption'));
  }
  return figure;
}
function draftPositions() {
  return [...new Set(String($('#positions').value).trim().split(/[\s,，、]+/)
    .filter(token=>/^\d{1,2}$/.test(token)).map(Number).filter(number=>number>=1&&number<=22))];
}
function renderChoices() {
  const visible=activeReading() && guided() && !(Array.isArray(reading.positions)&&reading.positions.length)
    && ['welcoming','awaiting_confirmation','selecting','queued','generated','paused'].includes(reading.state);
  $('#card-choices').hidden=!visible;
  $('#cards').hidden=!!visible;
  if(!visible)return;
  const key=reading.id;
  const signature=JSON.stringify([key,clean]);
  if(signature!==choicesSignature) {
    choicesSignature=signature;
    $('#card-choices').replaceChildren(...Array.from({length:22},(_,index)=>{
      const number=index+1;
      const choice=node(clean?'div':'button',undefined,'card-choice');
      choice.setAttribute('data-position',String(number));
      choice.setAttribute('aria-label',`第 ${number} 号牌`);
      choice.append(node('span','✧','choice-star'),node('span',String(number),'choice-number'));
      if(!clean) {
        choice.type='button';
        choice.onclick=()=>{
          if(!online||busy||!canDraw()||reading?.id!==key)return;
          let values=draftPositions();
          const count=cardCount(reading.card_count);
          if(values.includes(number))values=values.filter(value=>value!==number);
          else if(count===1)values=[number];
          else if(values.length<count)values.push(number);
          else {$('#notice').textContent=`这一轮需要 ${count} 张牌。可以先取消一个已选牌号。`;return;}
          $('#positions').value=values.join(' ');
          renderChoices();
        };
      }
      return choice;
    }));
  }
  const selected=clean?[]:draftPositions();
  for(const choice of $('#card-choices').children) {
    const number=Number(choice.getAttribute('data-position'));
    if(selected.includes(number))choice.classList.add('selected');else choice.classList.remove('selected');
    if(!clean) {choice.disabled=!online||busy||!canDraw();choice.setAttribute('aria-pressed',String(selected.includes(number)));}
  }
}
function controls() {
  const disabled = clean || !online || busy;
  $('#create').disabled = disabled || sessionOpen();
  $('#draw').disabled = disabled || !canDraw();
  $('#positions').disabled = disabled || !canDraw();
  $('#interpret').disabled = disabled || !canInterpret();
  $('#interpret').textContent = ['failed','interrupted'].includes(reading?.state) ? (guided()?'重试这一段 · 保留原牌':'用同一组牌重试解读') : '请她解读';
  $('#interpret').hidden=guided()&&!['failed','interrupted'].includes(reading?.state);
  $('#pause').hidden=!guided()||paused();
  $('#pause').disabled=disabled||!activeReading()||paused();
  $('#resume').hidden=!guided()||!paused();
  $('#resume').disabled=disabled||!activeReading()||!paused();
  $('#takeover').hidden=!guided();
  $('#takeover').disabled=disabled||!activeReading();
  $('#finish').disabled = disabled || !sessionOpen() || !reading || reading.state === 'finished';
  $('#close').disabled = disabled || !sessionOpen();
  $('#test-form').hidden = clean || !sessionOpen() || current?.mode !== 'test';
  $('#test-join').disabled = disabled || !sessionOpen() || current?.mode !== 'test' || Number(current?.queue_count) >= 30;
  $('#test-choose-form').hidden = clean || !canDraw() || current?.mode !== 'test';
  $('#test-choose').disabled = disabled || !canDraw() || current?.mode !== 'test';
  $('#test-reply-form').hidden=clean||!activeReading()||!guided()||current?.mode!=='test';
  $('#test-reply').disabled=disabled||!canReply()||current?.mode!=='test';
  $('#text-delivery').hidden=clean||reading?.state!=='awaiting_host';
  $('#confirm-delivered').disabled=disabled||!activeReading()||reading?.state!=='awaiting_host';
  for (const button of document.querySelectorAll('.select-entry')) button.disabled = disabled || !canSelect();
  renderChoices();
  updateWaitClock();
}
function renderEntries() {
  const signature = JSON.stringify([current?.id, entries]);
  if (signature === entriesSignature) return;
  entriesSignature = signature;
  const sessionId = current?.id;
  $('#participants').replaceChildren(...(entries.length ? entries.map(entry => {
    const row = node('div',undefined,'participant');
    const detail = node('div');
    detail.append(node('strong',text(entry.user,30)), node('p',text(entry.question,160),'hint'));
    row.append(detail);
    if (entry.state === 'waiting') {
      const button = node('button','请这位入座','select-entry'); button.type='button';
      button.onclick = () => {
        if (!canSelect() || current?.id !== sessionId) return;
        return action({action:'select',session_id:sessionId,entry_id:entry.id});
      };
      row.append(button);
    } else row.append(node('span',entry.state === 'selected' ? '已入座' : '已结束','small-label'));
    return row;
  }) : [node('p','还没有来客报名。','hint')]));
}
function renderHistory(items) {
  const signature=JSON.stringify(items);
  if (signature === historySignature) return;
  historySignature=signature;
  $('#history').replaceChildren(...(items.length ? items.slice(0,20).map(item => {
    const box=node('article',undefined,'history-item');
    box.append(node('h3',text(item.user,30) || '已结束的茶会'),node('p',text(item.question,160)));
    const cards=Array.isArray(item.cards)?item.cards:[];
    if(cards.length)box.append(node('p',cards.slice(0,3).map(card=>`${text(card.name,60)} · ${card.orientation==='reversed'?'逆位':card.orientation==='upright'?'正位':'方向待确认'}`).join(' / ')));
    box.append(node('p',stateLabels[item.state] || (item.state==='closed'?'茶会已收起':'状态待确认'),'hint'));
    return box;
  }) : [node('p','还没有结束的轮次。','hint')]));
}
function render(state) {
  if (!state || typeof state !== 'object' || !Object.hasOwn(state,'current')) throw Error('牌桌数据暂不可用');
  online=true;
  serverTime=typeof state.server_time==='number'&&Number.isFinite(state.server_time)?state.server_time:Date.now()/1000;
  current=state.current;
  reading=state.reading || null;
  if (reading && current && reading.session_id && reading.session_id !== current.id) reading=null;
  $('#connection').textContent='牌桌已连接 · 显示服务器保存的这一轮';
  $('#connection').hidden=clean;
  $('#table').classList.remove('stale');
  const open=sessionOpen();
  $('#round-state').textContent=(clean||guided())?publicStage():reading ? (stateLabels[reading.state] || '状态待确认') : open ? '等来客入座' : current ? '茶会已收起' : '尚未开桌';
  $('#round-title').textContent=reading ? text(reading.question,160) || '这一轮，听听牌的提示。' : '留一盏灯，等你来坐。';
  const mode=current?.mode==='test'?'测试茶会':'观众茶会';
  const spread=(reading?.spread||current?.spread)==='three'?'三张牌 · 眼下 / 阻碍 / 可尝试的方向':'一张牌';
  $('#round-meta').textContent=current ? `${mode} · ${spread}${reading ? ' · '+text(reading.user,30) : ''}` : '牌不是命令，只是借一点微光，看看你心里已有的答案。';
  $('#audience-instruction').textContent=audienceInstruction();
  const guidedNotes={welcoming:'先听清你带来的问题，再慢慢翻牌。',awaiting_confirmation:'一句确认，或补充你更在意的部分就好。',
    selecting:'牌序已经固定。跟着自己的心意选几个编号。',revealed:'牌已经选好，等她为你翻开。',
    queued:'留一点安静，等她接着说。',generated:'跟着她的声音，慢慢看看这一轮的牌。',
    awaiting_reply:'你觉得贴近，也可以说不太像。她会听你这一句。',closing:'把愿意带走的一点想法留在这里。',
    awaiting_host:'这一段先停留片刻，等主播继续。',paused:'这一轮暂停片刻，牌面仍为你保留。',
    failed:'这一轮暂时停住了，等主播继续。',interrupted:'这一轮暂时停住了，牌面会为你保留。',
    finished:'这一轮先收到这里。愿你带走一点面对明天的勇气。'};
  $('#stage-note').textContent=reading ? (reading.state==='finished'&&text(reading.farewell,200)
    || (clean||guided()?guidedNotes:stateNotes)[reading.state] || '请稍候。')
    : open ? '不必急着给出答案。等一位来客坐下，再翻开属于这一轮的牌。' : '茶会尚未开始，不会自动抽牌或播报。';
  const count=cardCount(reading?.card_count || (reading?.spread==='three'?3:1));
  const allowed=guided()?Math.max(0,Math.min(count,Number.isInteger(reading?.revealed_count)?reading.revealed_count:0)):count;
  const revealed=reading && reading.state !== 'selecting' && Array.isArray(reading.cards) ? reading.cards.slice(0,allowed) : [];
  const selectedPositions=Array.isArray(reading?.positions)?reading.positions.slice(0,count):[];
  const signature=JSON.stringify([reading?.id,count,revealed,selectedPositions]);
  if(signature!==cardsSignature) {
    cardsSignature=signature;
    if(lastReadingId!==reading?.id) {lastReadingId=reading?.id;lastVisibleCards=new Set();}
    $('#cards').classList.remove('single');
    if(reading) {
      if(count===1)$('#cards').classList.add('single');
      $('#cards').replaceChildren(...Array.from({length:count},(_,index)=>{
        const card=revealed[index]||null;
        const figure=tarotCard(card,index,count,selectedPositions[index]);
        if(card&&!lastVisibleCards.has(index)) {figure.classList.add('newly-revealed');lastVisibleCards.add(index);}
        return figure;
      }));
    } else {
      const empty=node('div',undefined,'empty-table');
      empty.append(node('span','☾','empty-moon'),node('p','牌还没有翻开。今晚的故事，由真实的这一轮开始。'));
      $('#cards').replaceChildren(empty);
    }
  }
  if(!clean) {
    $('#host-stage').textContent=reading?(text(reading.error,160) ||
      (reading.state==='generated'?'这一段已生成，等待真实播放完成回执；尚未开始观众回应计时。':
        reading.state==='queued'?'正在准备这一段，等待真实播放。':
        reading.state==='awaiting_host'?'没有音频完成回执。确认文字已展示给观众后再继续。':
        paused()?'已暂停；继续主持将恢复等待，或从被打断的这一段重新开始。':'')):'';
    $('#display-text').textContent=reading?.state==='awaiting_host'?text(reading.display_text,4000):'';
    $('#controls-hint').textContent=!sessionOpen()?'先选择主持方式并开桌，再请一位来客入座。'
      :guided()?'由她主持：你请来客入座，她来确认问题、解读并接住一次回应。等待从实际播完后开始；随时可暂停、跳过或转为手动。'
      :'手动推进：观众入座后牌序固定，选牌后点击“请她解读”。你决定何时结束，刷新不会重抽。';
    entries=Array.isArray(state.entries)?state.entries:[];
    const count=Number.isInteger(current?.queue_count)?current.queue_count:entries.filter(entry=>entry.state==='waiting').length;
    $('#participant-count').textContent=`${count} 人等候`;
    renderEntries();
    renderHistory(Array.isArray(state.history)?state.history:[]);
  }
  controls();
}
async function refresh(force=false) {
  if(busy&&!force)return;
  const request=++sequence;
  try {
    const response=await fetch('/tarot-status'+(clean?'?clean=1':''),{cache:'no-store',signal:AbortSignal.timeout(5000)});
    const state=await response.json();
    if(!response.ok)throw Error(state.error||'牌桌暂不可用');
    if(request===sequence)render(state);
  } catch(error) {
    if(request!==sequence)return;
    online=false;
    $('#connection').hidden=false;
    $('#connection').textContent='连接中断 · 保留最后确认的牌面，当前进度待核对';
    $('#table').classList.add('stale');
    controls();
  }
}
async function action(data) {
  if(clean||busy||!online)return;
  busy=true;++sequence;controls();
  try {
    const response=await fetch('/tarot-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(12000)});
    const result=await response.json();
    if(!response.ok||result.ok!==true)throw Error(text(result.error,200)||'操作未完成，请核对牌桌。');
    $('#notice').textContent=actionMessages[data.action]||'操作已确认。';
  } catch(error) {
    $('#notice').textContent=error.name==='TimeoutError'||error.name==='AbortError'||error instanceof TypeError
      ?'请求结果尚未确认，请先核对最新牌桌，再决定下一步；不会自动重发。'
      :text(error.message,200)||'操作未完成，请核对牌桌。';
  } finally {
    await refresh(true);busy=false;controls();
  }
}
function positions(value) {
  const tokens=String(value).trim().split(/[\s,，、]+/).filter(Boolean);
  const count=cardCount(reading?.card_count || (reading?.spread==='three'?3:1));
  if(tokens.length!==count||tokens.some(token=>!/^\d{1,2}$/.test(token)))throw Error(`请填写 ${count} 个 1–22 的整数牌号。`);
  const selected=tokens.map(Number);
  if(selected.some(number=>number<1||number>22)||new Set(selected).size!==selected.length)throw Error('牌号须在 1–22 之间，且不能重复。');
  return selected;
}
function submitPositions(test=false) {
  if(!canDraw()||(test&&current?.mode!=='test'))return;
  try {
    const values=positions($(test?'#test-positions':'#positions').value);
    return action(test ? {action:'test_choose',session_id:current.id,user:$('#test-choose-user').value.trim(),positions:values}
      : {action:'draw',session_id:current.id,reading_id:reading.id,positions:values});
  }catch(error){$('#notice').textContent=text(error.message,200);}
}
if(!clean) {
  $('#create-form').onsubmit=event=>{event.preventDefault();if(sessionOpen())return;return action({action:'create',mode:$('#mode').value,spread:$('#spread').value,hosting:$('#hosting').value});};
  $('#draw-form').onsubmit=event=>{event.preventDefault();return submitPositions();};
  $('#test-choose-form').onsubmit=event=>{event.preventDefault();return submitPositions(true);};
  $('#test-form').onsubmit=event=>{event.preventDefault();if(!sessionOpen()||current.mode!=='test')return;return action({action:'test_join',session_id:current.id,user:$('#test-user').value.trim(),question:$('#test-question').value.trim()});};
  $('#test-reply-form').onsubmit=event=>{event.preventDefault();if(!canReply()||current.mode!=='test')return;return action({action:'test_reply',session_id:current.id,user:$('#test-reply-user').value.trim(),text:$('#test-reply-text').value.trim()});};
  $('#interpret').onclick=()=>canInterpret()&&action({action:'interpret',session_id:current.id,reading_id:reading.id});
  $('#pause').onclick=()=>activeReading()&&guided()&&!paused()&&action({action:'pause',session_id:current.id,reading_id:reading.id});
  $('#resume').onclick=()=>activeReading()&&guided()&&paused()&&action({action:'resume',session_id:current.id,reading_id:reading.id});
  $('#takeover').onclick=()=>activeReading()&&guided()&&action({action:'takeover',session_id:current.id,reading_id:reading.id});
  $('#confirm-delivered').onclick=()=>activeReading()&&reading.state==='awaiting_host'&&action({action:'confirm_delivered',session_id:current.id,reading_id:reading.id});
  $('#positions').oninput=()=>renderChoices();
  $('#finish').onclick=()=>sessionOpen()&&reading&&reading.state!=='finished'&&action({action:'finish',session_id:current.id,reading_id:reading.id});
  $('#close').onclick=()=>sessionOpen()&&action({action:'close',session_id:current.id});
}
controls();
async function poll(){if(!busy)await refresh();setTimeout(poll,1500);}poll();
