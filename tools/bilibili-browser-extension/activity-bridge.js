(function(root) {
  'use strict';
  function install(win,runtime,a) {
    if(win!==win.top||!a.local(win.location.href))return;
    const pending=new Set();
    win.addEventListener('message',event=>{
      const data=event.data;
      if(event.source!==win||event.origin!==win.location.origin||data?.source!==a.SOURCE)return;
      if(data.type==='cancel') {
        if(typeof data.request_id==='string'&&a.UUID.test(data.request_id)&&pending.has(data.request_id.toLowerCase()))
          Promise.resolve().then(()=>runtime.sendMessage({channel:a.CHANNEL,kind:'cancel',request_id:data.request_id.toLowerCase()})).catch(()=>{});
        return;
      }
      if(data.type!=='request')return;
      const command=a.validate(data);if(!command||pending.has(command.request_id))return;
      pending.add(command.request_id);
      Promise.resolve().then(()=>runtime.sendMessage({channel:a.CHANNEL,kind:'request',...command}))
        .catch(()=>command.action==='publish'?{ok:true,result:{status:'unknown'}}:a.error('unavailable'))
        .then(result=>{
          if(!pending.delete(command.request_id))return;
          win.postMessage({source:a.RESPONSE,type:'result',request_id:command.request_id,...result},win.location.origin);
        });
    });
    win.addEventListener('pagehide',()=>{
      for(const request_id of pending)Promise.resolve().then(()=>runtime.sendMessage({channel:a.CHANNEL,kind:'cancel',request_id})).catch(()=>{});
      pending.clear();
    });
  }
  if(typeof module!=='undefined')module.exports={install};else install(root.window,chrome.runtime,root.VtuberBilibiliActivity);
})(globalThis);
