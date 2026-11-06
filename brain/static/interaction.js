class ChatInteraction {
  static voiceResultsDecision(text, results, options = {}) {
    // SpeechRecognitionEvent.results contains the full unsubmitted transcript,
    // including earlier final results; resultIndex only identifies changes.
    for (const result of Array.from(results)) {
      const alternatives = Array.from(result);
      if (!alternatives.length) continue;
      const decision = this.voiceDecision(alternatives[0].transcript, alternatives, options);
      if (decision.delay === null && decision.reviewRequired) return decision;
    }
    return this.voiceDecision(text, [], options);
  }
  static isInterruption(text) {
    return /^(?:嗯[，,\s]*)?(?:等等(?:[，,\s]|$)|等一等|等一下|等会儿|先等一下|停一下|停下|先别说|先暂停|打断一下|不是这个意思|我不是这个意思|不对[，,\s]|不对$)/.test(text.trim());
  }
  static nameHints(text, names = []) {
    const distance = (a,b) => {
      let row = Array.from({length:b.length+1},(_,i)=>i);
      for (let i=0;i<a.length;i++) { const next=[i+1];
        for (let j=0;j<b.length;j++) next.push(Math.min(next[j]+1,row[j+1]+1,row[j]+(a[i]===b[j]?0:1)));
        row=next;
      } return row[b.length];
    };
    const found=[];
    for (const name of names.slice(0,20)) {
      if (name.length<3 || name.length>12 || text.includes(name)) continue;
      for (let i=0;i<=text.length-name.length;i++) {
        const heard=text.slice(i,i+name.length);
        if (distance(heard,name)===1) {found.push({heard,name});break;}
      }
    }
    return found;
  }
  static voiceDecision(text, alternatives = [], options = {}) {
    const clean = text.trim();
    const first = alternatives[0];
    const uncertain = first && first.confidence > 0 && first.confidence < 0.65;
    const critical = value => (value.match(/\d+(?:\.\d+)?|[零一二三四五六七八九十百千万两]+|不|没|别|叫.{1,8}|名字.{1,8}/g) || []).join('|');
    const conflict = alternatives.slice(1).find(item => item.transcript.trim() !== clean
      && critical(item.transcript) !== critical(first.transcript));
    if (uncertain || conflict) return {delay:null, reviewRequired:true, listeningCue:'wait', hint: conflict
      ? `请核对关键内容，另一个候选是“${conflict.transcript.trim()}”；可在输入框只改错的部分。`
      : '这段识别把握较低，请核对后确认发送。'};
    const names = this.nameHints(clean, options.names || []);
    if (names.length) return {delay:null, reviewRequired:true, listeningCue:'wait', hint:`名字可能是“${names[0].name}”，当前识别为“${names[0].heard}”，请核对后发送。`, names};
    if (/(我想想|让我想|等我想|等一下|稍等|怎么说呢)[啊呀呢吧，。…\s]*$/.test(clean))
      return {delay:null, listeningCue:'wait', hint:'我等你继续说；说完也可以点击确认发送。'};
    if (/(?:我说完了|就这些|可以回答了|请回答吧)[。！!\s]*$/.test(clean))
      return {delay:650, listeningCue:'complete', hint:'0.65秒后发送；继续说话会取消发送。'};
    const unfinished = /(但是|然后|因为|所以|还有|比如|比如说|如果|那个|就是|我觉得|我想说|我想要|我打算|一方面)[，。…\s]*$/.test(clean);
    const question = /[？?]$|[吗呢]？?$/.test(clean);
    const base = unfinished ? 4500 : question ? 1100 : 2000;
    const pace = options.pace === 'patient' ? 1.5 : options.pace === 'quick' ? .75 : 1;
    // Never rush an unfinished clause, even in quick mode.
    const delay = Math.round(base * (unfinished ? Math.max(1,pace) : pace));
    const listeningCue = unfinished ? 'wait' : question ? 'question' : 'neutral';
    return {delay, listeningCue, hint:`${delay / 1000}秒后发送；继续说话或编辑会取消本次倒计时。`};
  }
  constructor(clock = () => Date.now()) {
    this.clock = clock;
    this.phase = 'idle';
    this.since = clock();
    this.turn = 0;
    this.error = '';
  }
  set(phase, turn = this.turn, error = '') {
    if (turn < this.turn) return;
    this.turn = turn;
    if (phase !== this.phase) this.since = this.clock();
    this.phase = phase;
    this.error = error;
  }
  label() {
    const labels = {idle:'可以和我说话', listening:'正在听你说', recognizing:'正在识别',
      review:'检查一下识别内容，可以修改后发送', thinking:'正在想', synthesizing:'正在准备语音',
      ready:'正在加载语音', speaking:'正在说', paused:'已暂停 · 继续后从原位置播放', error:'暂时遇到问题'};
    let text = this.error || labels[this.phase] || labels.idle;
    const elapsed = Math.floor((this.clock() - this.since) / 1000);
    if (['thinking','synthesizing','ready','recognizing'].includes(this.phase) && elapsed >= 8)
      text += ` · 已等待 ${elapsed} 秒，可停止或重试`;
    return text;
  }
}
if (typeof module !== 'undefined') module.exports = ChatInteraction;
else window.ChatInteraction = ChatInteraction;
