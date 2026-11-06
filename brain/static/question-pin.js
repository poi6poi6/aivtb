(function(){
  'use strict';
  function attach(row,event){
    if(!event.question_pin_token||event.kind!=='chat'||row.querySelector('.question-pin'))return;
    if(document.documentElement.classList.contains('broadcast-display')||document.querySelector('.friend-test'))return;
    const button=document.createElement('button');button.type='button';button.className='question-pin';button.textContent='固定待答';button.style.marginLeft='8px';button.style.fontSize='12px';
    const result=document.createElement('span');result.setAttribute('role','status');result.style.fontSize='12px';
    button.onclick=async()=>{button.disabled=true;row.questionPinPending=true;result.textContent='';try{const response=await fetch('/questions-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pin',token:event.question_pin_token}),signal:AbortSignal.timeout(8000)});const payload=await response.json();if(!response.ok)throw Error(payload.error||'未能固定');button.textContent='已固定';const link=document.createElement('a');link.href='/questions';link.target='_blank';link.rel='noopener';link.textContent=payload.state==='withdrawn'?' 查看已撤回卡片':' 打开待答卡';result.replaceChildren(link);}catch(error){result.textContent=error.name==='TimeoutError'?' 请求未确认，可重试核对，不会重复保存':' '+error.message;button.disabled=false;}finally{row.questionPinPending=false;}};
    row.append(button,result);
  }
  window.QuestionPin=Object.freeze({attach});
})();
