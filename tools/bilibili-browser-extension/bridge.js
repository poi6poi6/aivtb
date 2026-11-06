(function(root) {
  'use strict';
  function install(win, runtime, common) {
    if (win !== win.top || !common.local(win.location.href)) return;
    const pending = new Set();
    const send = message => {
      try { return Promise.resolve(runtime.sendMessage({channel:common.channel, ...message})); }
      catch(error) { return Promise.reject(error); }
    };
    win.addEventListener('message', event => {
      const data = event.data;
      if (event.source !== win || event.origin !== win.location.origin || !data
          || data.source !== 'ai-vtuber' || data.type !== 'bilibili-browser-request'
          || !common.validId(data.id) || !['connect','search','read_video','cancel'].includes(data.action)) return;
      if (data.action === 'cancel') {
        pending.delete(data.id);
        send({id:data.id, action:'cancel'}).catch(() => {});
        return;
      }
      if (pending.has(data.id)) return;
      pending.add(data.id);
      // Explicit fields prevent a backend token or arbitrary page data from
      // accidentally crossing the extension boundary.
      send({id:data.id, action:data.action, query:data.query, url:data.url, kind:data.kind})
        .catch(() => common.error('extension_unavailable')).then(result => {
          if (!pending.delete(data.id)) return;
          win.postMessage({source:'ai-vtuber-extension', type:'bilibili-browser-response',
            id:data.id, ...result}, win.location.origin);
        });
    });
    win.addEventListener('pagehide', () => {
      for (const id of pending) send({action:'cancel', id}).catch(() => {});
      pending.clear();
    });
  }
  if (typeof module !== 'undefined') module.exports = {install};
  else install(root.window, chrome.runtime, root.VtuberBrowserCommon);
})(globalThis);
