// Sparse semantic cues on the real audio clock. Positions are estimated from
// text/pronunciation and punctuation weights, NOT word-level forced alignment.
class CompanionSpeechCues {
  constructor() {
    this.last=null;
    this.lastMedia=null;
    this.turnId=null;
    this.segmentIndex=null;
    this.serial=0;
    this.anonymousTurn=0;
    this.retired=new Set();
    this.segmentActive=false;
    this.played=false;
    this.candidates=[];
    this.schedule=[];
    this.consumed=new Set();
    this.duration=null;
    this.emitted=0;
    this.lastCueMedia=-Infinity;
    this.focus='';
    this.focusRemaining=0;
    this.questionReached=false;
    this.allowQuestionHold=true;
    this.holdRemaining=0;
  }
  retire(key) {
    if(key===null)return;
    this.retired.add(key);
    if(this.retired.size>128)this.retired.delete(this.retired.values().next().value);
  }
  begin({text='',turnId,segmentIndex=0}={}) {
    const index=Number.isSafeInteger(segmentIndex) && segmentIndex>=0 ? segmentIndex : 0;
    const key=turnId ?? (index===0 || this.turnId===null ? `anonymous:${++this.anonymousTurn}` : this.turnId);
    if(this.retired.has(key))return false;
    // Repeated playing events, an already-finished segment, and stale earlier
    // segments cannot reset a timeline or bring back an old question gaze.
    if(key===this.turnId && this.segmentIndex!==null && index<=this.segmentIndex)return false;
    if(key!==this.turnId)this.retire(this.turnId);
    this.turnId=key;
    this.segmentIndex=index;
    this.serial++;
    this.segmentActive=true;
    this.lastMedia=null;
    this.played=false;
    this.candidates=CompanionSpeechCues.extract(String(text ?? ''));
    this.schedule=[];
    this.consumed.clear();
    this.duration=null;
    this.emitted=0;
    this.lastCueMedia=-Infinity;
    this.focus='';
    this.focusRemaining=0;
    this.questionReached=false;
    this.allowQuestionHold=true;
    this.holdRemaining=0;
    return true;
  }
  clearFocus() {
    this.focus=this.segmentActive && this.played ? 'engage' : '';
    this.focusRemaining=0;
    this.questionReached=false;
    this.allowQuestionHold=false;
    this.holdRemaining=0;
  }
  end({completed=false}={},now) {
    if(!this.segmentActive) {
      if(!completed) { this.clearFocus(); this.retire(this.turnId); }
      return;
    }
    const hold=completed && this.played && this.questionReached && this.allowQuestionHold;
    this.segmentActive=false;
    this.lastMedia=null;
    this.focus=hold ? 'question' : '';
    this.focusRemaining=0;
    this.holdRemaining=hold ? 1.8 : 0;
    if(Number.isFinite(now) && (this.last===null || now>=this.last))this.last=now;
    if(!completed)this.retire(this.turnId);
  }
  resetSession() {
    this.clearFocus();
    this.segmentActive=false;
    this.last=null;
    this.lastMedia=null;
    this.turnId=null;
    this.segmentIndex=null;
    this.anonymousTurn=0;
    this.retired.clear();
    this.candidates=[];
    this.schedule=[];
    this.consumed.clear();
    this.duration=null;
    this.played=false;
    this.lastCueMedia=-Infinity;
  }
  prepare(duration,media) {
    if(!Number.isFinite(duration) || duration<=.2 || duration>1800)return false;
    if(this.duration!==null && Math.abs(duration-this.duration)<.05)return false;
    this.duration=duration;
    const planned=this.candidates.map(item=>({...item,
      at:Math.max(.12,Math.min(duration-.1,item.position*duration))}));
    // Keep purposeful cues well apart, preferring the final question and an
    // explicit focal clause over another small reflective beat in short audio.
    const selected=[];
    for(const item of planned.sort((a,b)=>b.priority-a.priority || a.at-b.at)) {
      if(selected.every(other=>Math.abs(item.at-other.at)>=1.6))selected.push(item);
      if(selected.length===3)break;
    }
    this.schedule=selected.sort((a,b)=>a.at-b.at);
    this.skipThrough(media);
    return true;
  }
  skipThrough(media) {
    if(!Number.isFinite(media))return;
    for(const item of this.schedule)if(item.at<=media)this.consumed.add(item.key);
  }
  frame(now,{speaking=false,paused=false,listening=false,mediaTime=0,duration=NaN}={}) {
    if(listening) {
      if(this.segmentActive)this.end({completed:false},now);
      this.clearFocus();
    }
    const validTime=Number.isFinite(now) && (this.last===null || now>=this.last);
    const timestamp=validTime ? now : (this.last ?? 0);
    const elapsed=this.last===null ? 0 : (timestamp-this.last)/1000;
    const wallDt=validTime && elapsed>=0 && elapsed<=.25 ? elapsed : 0;
    this.last=timestamp;
    if(!this.segmentActive) {
      if(this.holdRemaining>0 && !paused && !listening) {
        // A completed question expires while its tab is hidden. Unlike an
        // active motion, its short social hold must not resume minutes later.
        this.holdRemaining=Math.max(0,this.holdRemaining-(validTime ? elapsed : 0));
        if(!this.holdRemaining)this.focus='';
      }
      return {cue:null,focus:this.focus};
    }
    const media=Number.isFinite(mediaTime) && mediaTime>=0 ? mediaTime : null;
    const previous=this.lastMedia;
    const delta=media!==null && previous!==null ? media-previous : 0;
    const revised=media!==null && this.prepare(duration,media);
    const progressing=speaking && !paused && wallDt>0 && delta>0
      && delta<=.25 && delta<=wallDt+.035;
    this.lastMedia=media;
    let cue=null;
    if(progressing) {
      this.played=true;
      if(!this.focus)this.focus='engage';
      if(this.focusRemaining>0) {
        this.focusRemaining=Math.max(0,this.focusRemaining-delta);
        if(!this.focusRemaining)this.focus='engage';
      }
      // Initial/late duration knowledge only schedules future cues. Never
      // invent a beat or replay one passed while paused, seeking or suspended.
      if(!revised && this.duration!==null && this.emitted<3) {
        const item=this.schedule.find(entry=>!this.consumed.has(entry.key)
          && entry.at>previous && entry.at<=media);
        if(item && media-this.lastCueMedia>=1.6-1e-6) {
          this.consumed.add(item.key);
          this.emitted++;
          this.lastCueMedia=media;
          cue={id:`${this.serial}:${item.key}`,kind:item.kind};
          this.focus=item.kind==='emphasis' || (item.kind==='question' && !this.allowQuestionHold)
            ? 'engage' : item.kind;
          this.focusRemaining=item.kind==='reflect' ? .95 : item.kind==='question' ? 1.8 : 0;
          if(item.kind==='question' && this.allowQuestionHold)this.questionReached=true;
        }
      }
    } else if(media!==null) {
      this.skipThrough(media);
      if(previous!==null && (delta<0 || delta>.25 || (delta>0 && wallDt===0))) {
        this.focus=this.played ? 'engage' : '';
        this.focusRemaining=0;
        this.questionReached=false;
      }
    }
    this.skipThrough(media);
    return {cue,focus:this.focus};
  }
  static extract(text) {
    // Malformed/unreasonably long telemetry falls back to simple eye contact.
    // It must not make a huge numeric token or regex workload dictate a pose.
    if(!text || text.length>4096)return [];
    const weights=new Float64Array(text.length+1);
    let total=0;
    for(const token of text.matchAll(/[A-Za-z]+(?:['’-][A-Za-z]+)*|\d+(?:[.,:]\d+)*|[\s\S]/gu)) {
      const value=token[0];
      let weight;
      if(/^[A-Za-z]/.test(value)) {
        const syllables=(value.match(/[aeiouy]+/gi)||[]).length;
        weight=Math.max(.8,Math.min(5,syllables || value.length*.55));
      } else if(/^\d/.test(value))weight=Math.max(1,Math.min(12,value.replace(/\D/g,'').length*.8));
      else if(/[。！？!?；;]/u.test(value))weight=.85;
      else if(/[，,、：:]/u.test(value))weight=.45;
      else if(/\s/u.test(value))weight=.04;
      else if(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(value))weight=1;
      else weight=.12;
      for(let i=0;i<value.length;i++)weights[token.index+i]=total+weight*i/value.length;
      total+=weight;
      weights[token.index+value.length]=total;
    }
    if(total<=0)return [];
    const literal=[];
    for(const match of text.matchAll(/https?:\/\/\S+/gu))
      literal.push([match.index,match.index+match[0].length]);
    const quotes={'"':'"','“':'”','「':'」','『':'』','‘':'’','`':'`'};
    for(let index=0;index<text.length;index++) {
      const closing=quotes[text[index]];
      if(!closing)continue;
      const end=text.indexOf(closing,index+1);
      // A streaming segment may end at the question mark before the closing
      // quote arrives. It is still reported speech, not a question to the user.
      literal.push([index,end<0 ? text.length : end+1]);
      index=end<0 ? text.length : end;
    }
    const quoted=index=>literal.some(([start,end])=>index>=start && index<end);
    const negative=index=>/(?:不|没有|没|并非|不是|别|不要|谈不上|算不上).{0,5}$|\b(?:not|don't|doesn't|isn't|never|no)\b.{0,12}$/iu.test(
      text.slice(Math.max(0,index-20),index).split(/[，,。！？!?；;]|但是|不过|而是/u).at(-1));
    const result=[];
    const add=(kind,index,priority)=>{
      if(!quoted(index) && !negative(index))result.push({kind,key:`${kind}:${index}`,
        position:Math.max(0,Math.min(1,weights[index]/total)),priority});
    };
    const reflect=/让我想一下|让我想想|我想一下|我想想|该怎么说呢|怎么说呢|\blet me think\b|\bhow (?:should|can) I put (?:it|this)\b/giu;
    for(const match of text.matchAll(reflect)) {
      add('reflect',match.index,1);
      if(result.some(item=>item.kind==='reflect'))break;
    }
    const emphasis=/更(?:喜欢|在意|看重|想要|希望|重要)|最(?:喜欢|重要|在意)|重点(?:是|在于)|关键(?:是|在于)|尤其是|\bthe (?:key|main) point is\b|\bwhat (?:really )?matters (?:is|most)\b|\bI (?:really )?prefer\b/giu;
    for(const match of text.matchAll(emphasis))add('emphasis',match.index,2);
    // Only a direct final question gets waiting-for-an-answer gaze. A quoted
    // question, an earlier rhetorical question, or a URL cannot acquire it.
    const terminal=/[？?]\s*$/u.exec(text);
    if(terminal && !quoted(terminal.index)) {
      const clauseStart=Math.max(text.lastIndexOf('。',terminal.index-1),text.lastIndexOf('！',terminal.index-1),
        text.lastIndexOf('？',terminal.index-1),text.lastIndexOf('?',terminal.index-1))+1;
      const index=Math.max(clauseStart,terminal.index-2);
      result.push({kind:'question',key:`question:${terminal.index}`,
        position:Math.max(0,Math.min(1,weights[index]/total)),priority:3});
    }
    return result;
  }
}

if(typeof window!=='undefined')window.CompanionSpeechCues=CompanionSpeechCues;
if(typeof module!=='undefined' && module.exports)module.exports=CompanionSpeechCues;
