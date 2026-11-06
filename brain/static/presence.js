// Small, bounded listening gestures. The clock is supplied by the caller.
class CompanionPresence {
  constructor() {
    this.listening = false;
    this.thinking = false;
    this.weight = 0;
    this.nodAt = -Infinity;
    this.nodSuppressed = false;
    this.lastFrame = null;
    this.lastFinalText = '';
    this.lastFinalCue = '';
    this.lastNodText = '';
    this.listeningCue = 'neutral';
    this.readiness = 0;
    this.curiosity = 0;
    this.cueAt = -Infinity;
  }
  listen(enabled) {
    this.listening = enabled;
    if (enabled) {
      this.listeningCue = 'neutral';
      this.cueAt = -Infinity;
      this.nodSuppressed = true;
      this.thinking = false;
      this.lastFinalText = '';
      this.lastFinalCue = '';
      this.lastNodText = '';
    }
  }
  resetSession() {
    this.listening = false;
    this.thinking = false;
    this.weight = 0;
    this.nodAt = -Infinity;
    this.nodSuppressed = false;
    this.lastFrame = null;
    this.lastFinalText = '';
    this.lastFinalCue = '';
    this.lastNodText = '';
    this.listeningCue = 'neutral';
    this.readiness = 0;
    this.curiosity = 0;
    this.cueAt = -Infinity;
  }
  hear(text, now, {final=false, decision} = {}) {
    if (!this.listening || !Number.isFinite(now)) return;
    const clean = String(text || '').trim();
    // The speech-recognition policy owns interpretation. Reuse its review and
    // unfinished-clause decisions instead of guessing agreement from word count.
    const cue = decision?.reviewRequired || decision?.delay === null ? 'wait'
      : ['wait','question','complete','neutral'].includes(decision?.listeningCue)
        ? decision.listeningCue : 'neutral';
    if (cue === 'wait' || cue === 'question') this.nodSuppressed = true;
    if (!final || !clean) {
      this.listeningCue = 'neutral';
      this.cueAt = -Infinity;
      this.nodSuppressed = true;
      return;
    }
    const normalize = value => value.replace(/[\s，,。.!！?？；;：:、…“”"'‘’]/g, '');
    const previous = this.lastFinalText;
    const previousCue = this.lastFinalCue;
    const key = normalize(clean);
    this.lastFinalText = clean;
    this.lastFinalCue = cue;
    // Browser recognition may replay the same final every few hundred ms.
    // Do not extend a reaction, including one already withdrawn by new speech.
    // A changed interpretation (e.g. reviewed wording becoming a question) is
    // new evidence and can still trigger its first appropriate reaction.
    if (key === normalize(previous) && cue === previousCue) return;
    this.listeningCue = cue;
    this.cueAt = now;
    // Repeated final events and replacements of a final transcript are still
    // the same listening beat. Only a later sentence can earn another nod.
    const newSentence = !this.lastNodText || (/[。！？.!?][”’"']?$/.test(previous)
      && clean.startsWith(previous) && normalize(clean.slice(previous.length)).length > 0);
    if (key === normalize(previous) || !newSentence || cue === 'wait' || cue === 'question') return;
    if (key !== this.lastNodText && now - this.nodAt > 2200) {
      this.nodAt = now;
      this.nodSuppressed = false;
      this.lastNodText = key;
    }
  }
  acknowledge(now) {
    if (now - this.nodAt > 1800) { this.nodAt = now; this.nodSuppressed = false; }
  }
  frame(now, speaking) {
    const dt = this.lastFrame === null ? 0 : Math.max(0, Math.min(0.05, (now - this.lastFrame) / 1000));
    this.lastFrame = now;
    const target = this.listening ? 1 : this.thinking && !speaking ? 0.45 : 0;
    this.weight += (target - this.weight) * (1 - Math.exp(-dt / 0.22));
    // Readiness is brief: a late final event cannot leave a fixed questioning
    // face while the user continues. Speaking always owns the expression.
    // A normal recognition stop follows final speech immediately. Let that
    // short reaction finish even though microphone attention is now fading.
    // Starting a new utterance or the avatar's voice retires it instead.
    if (speaking) {
      this.cueAt = -Infinity;
      this.readiness = 0;
      this.curiosity = 0;
      this.nodSuppressed = true;
    }
    const fresh = !speaking && now - this.cueAt >= 0 && now - this.cueAt < 1800;
    const ready = fresh ? this.listeningCue === 'complete' ? 1
      : this.listeningCue === 'question' ? .65 : this.listeningCue === 'neutral' ? .35 : 0 : 0;
    const curious = fresh && this.listeningCue === 'question' ? 1 : 0;
    this.readiness += (ready - this.readiness) * (1 - Math.exp(-dt / .2));
    this.curiosity += (curious - this.curiosity) * (1 - Math.exp(-dt / .24));
    const phase = (now - this.nodAt) / 650;
    const nod = !speaking && !this.nodSuppressed && phase >= 0 && phase <= 1 ? Math.sin(phase * Math.PI) * 1.2 : 0;
    return { attention: this.weight, nod, tilt: this.thinking && !this.listening && !speaking ? this.weight * 1.3 : 0,
      readiness: speaking ? 0 : this.readiness, curiosity: speaking ? 0 : this.curiosity };
  }
}
if (typeof module !== 'undefined') module.exports = CompanionPresence;
else window.CompanionPresence = CompanionPresence;
