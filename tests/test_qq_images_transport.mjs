// Official pinned SDK + fake fetch: no login, QQ network or real messages.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {QQBot} from '../tools/qq-harness/node_modules/@tencent-connect/qqbot-nodejs/dist/index.js';
import {QQTransport, createHost, validImageUrl, downloadQQImage} from '../tools/qq-harness/transport.mjs';

const png = readFileSync(new URL('../brain/static/memes/basic/happy.png', import.meta.url));
const signal = new AbortController().signal;
const url = 'https://multimedia.nt.qq.com.cn/download?fileid=fixture&rkey=private';
assert(validImageUrl(url));
for (const bad of ['http://multimedia.nt.qq.com.cn/download', 'https://127.0.0.1/a',
  'https://multimedia.nt.qq.com.cn.evil.test/a', 'https://user@multimedia.nt.qq.com.cn/a',
  'https://multimedia.nt.qq.com.cn:444/a', 'https://evil.test/a', 'file:///tmp/a.png',
  'https://multimedia.nt.qq.com.cn/a#secret']) assert(!validImageUrl(bad));
let fetches = 0;
const imageFetch = async (target, options) => {
  fetches += 1;
  assert.equal(target, url);
  assert.equal(options.redirect, 'error');
  assert(!options.headers.Authorization, 'QQ credentials never go to attachment download URLs');
  return new Response(png, {headers: {'Content-Type':'application/octet-stream'}});
};
const attachment = {content_type:'image/png', url};
assert.equal((await downloadQQImage(attachment, signal, imageFetch)).mime, 'image/png');
await assert.rejects(downloadQQImage({...attachment,url:'https://evil.test/a'},signal,imageFetch));
assert.equal(fetches, 1);
await assert.rejects(downloadQQImage({...attachment,size:6*1024*1024},signal,imageFetch));
await assert.rejects(downloadQQImage(attachment,signal,async()=>new Response(png,{headers:{'content-length':'99999999'}})));
await assert.rejects(downloadQQImage(attachment,signal,async()=>new Response('<svg onload="x"/>')));

let bridgeOptions;
const bridge = createHost('http://127.0.0.1:12345','fixture-local-token',async (target, options)=>{
  assert.equal(target,'http://127.0.0.1:12345/image/'+'A'.repeat(32));
  bridgeOptions=options;return new Response(png);
});
assert.equal((await bridge.image('A'.repeat(32),signal)).mime,'image/png');
assert.equal(bridgeOptions.headers.Authorization,'Bearer fixture-local-token');
assert.equal(bridgeOptions.redirect,'error');
await assert.rejects(bridge.image('../outside',signal));

const originalFetch=globalThis.fetch;
const requests=[], hostCalls=[], diagnostics=[];
let failUpload=false, failSend=false;
globalThis.fetch=async (target, options)=>{
  requests.push({target,options});
  if(target.endsWith('/app/getAppAccessToken')) return Response.json({access_token:'fixture-token',expires_in:7200});
  if(target.endsWith('/files')) {
    if(failUpload)throw Error('fixture upload failed');
    return Response.json({file_info:'fixture-file-info',file_uuid:'fixture-id',ttl:300});
  }
  if(failSend)throw Error('ambiguous send');
  return Response.json({id:'confirmed-send!',timestamp:'2026-10-29T00:00:00Z'});
};
const host=async (path,body)=>{
  hostCalls.push({path,body});
  return path==='/message'?{ok:true,reply:'看到了，给你一个开心表情。',receipt_id:'A'.repeat(32),reply_mode:'image'}:{ok:true};
};
host.image=async()=>({data:png,mime:'image/png'});
const bot=new QQBot({appId:'123',appSecret:'fixture-secret',baseUrl:'https://api.bot.qq.com',tokenBaseUrl:'https://api.bot.qq.com',markdownSupport:false});
const transport=new QQTransport({host,fetchImage:imageFetch,diagnostic:value=>diagnostics.push(value)});
const incoming=(id,overrides={})=>({kind:'c2c',senderId:'peer',messageId:id,content:'',attachments:[attachment],
  replyTarget:{scope:'c2c',targetId:'peer',msgId:id},...overrides});
try {
  await transport.inbound(bot,'123',incoming('one'),signal);
  assert.equal(hostCalls[0].body.text,'（用户发送了一张图片）');
  assert.deepEqual(hostCalls[0].body.attachments,[{content_type:'image/png',data_base64:png.toString('base64')}]);
  assert(!JSON.stringify(hostCalls[0]).includes('rkey'));
  const upload=JSON.parse(requests.find(r=>r.target.endsWith('/files')).options.body);
  assert.equal(upload.file_type,1);assert.equal(upload.srv_send_msg,false);
  assert.equal(upload.file_data,png.toString('base64'));
  const sends=()=>requests.filter(r=>r.target.endsWith('/messages'));
  assert.equal(sends().length,1);
  const message=JSON.parse(sends()[0].options.body);
  assert.equal(message.msg_type,7);assert.equal(message.msg_id,'one');
  assert.equal(message.content,'看到了，给你一个开心表情。');
  assert.equal(hostCalls.at(-1).body.status,'sent');
  await transport.inbound(bot,'123',incoming('one'),signal);
  assert.equal(sends().length,1,'duplicate input cannot produce another image');

  failSend=true;
  await transport.inbound(bot,'123',incoming('unknown'),signal);
  assert.equal(sends().length,2);
  assert.equal(hostCalls.at(-1).body.status,'unknown','ambiguous image send has no text fallback');
  failSend=false;failUpload=true;
  await transport.inbound(bot,'123',incoming('upload-failed'),signal);
  assert.equal(sends().length,3);
  assert.equal(JSON.parse(sends().at(-1).options.body).msg_type,0,'pre-send upload failure permits one text fallback');
  assert.equal(hostCalls.at(-1).body.status,'sent');
  failUpload=false;
  await transport.inbound(bot,'123',incoming('unavailable',{attachments:[{...attachment,url:'https://evil.test/a'}]}),signal);
  const unavailable=hostCalls.find(x=>x.path==='/message'&&x.body.message_id==='unavailable').body;
  assert.equal(unavailable.image_unavailable,true);assert(!unavailable.attachments);
  const count=fetches;
  await transport.inbound(bot,'123',{...incoming('not-mentioned'),kind:'group',rawEventType:'GROUP_MESSAGE_CREATE',
    groupOpenid:'group',replyTarget:{scope:'group',targetId:'group',msgId:'not-mentioned'}},signal);
  assert.equal(fetches,count,'unmentioned group messages do not download images');
  for(const secret of ['fixture-token','fixture-secret','rkey','fileid',png.toString('base64')]) {
    assert(!JSON.stringify(diagnostics).includes(secret));
  }
  console.log('QQ images: bounded CDN intake, private bridge assets, official SDK single image+text send and no ambiguous retry: PASS');
} finally { transport.stop();bot.stop();globalThis.fetch=originalFetch; }
