// Run the real page against Cubism's update order, not a string-matching mock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../brain/static/index.html'), 'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
const markup = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
const ids = new Set([...markup.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const vendor = fs.readFileSync(path.join(__dirname, '../brain/static/vendor/pixi-live2d-display.min.js'), 'utf8');
function pinnedVendorSection(start, end) {
  const from = vendor.indexOf(start), to = vendor.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, 'the pinned Cubism adapter changed; review this native contract test');
  return vendor.slice(from, to);
}
// Execute the actual pinned runtime's small classes. Only the underlying moc
// memory is synthetic; the APIs, breath curve and frame order are vendor code.
const nativeContract = vm.runInNewContext(`(() => {
  class pe {update() {}}
  ${pinnedVendorSection('class a{', 'const n=class')}
  ${pinnedVendorSection('class D{', 'class F{')}
  ${pinnedVendorSection('class He extends pe{', 'let Ye,qe=')}
  return {Breath:a, BreathParameter:o, Core:D, Internal:He};
})()`);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

async function fixture({omit = [], ranges = {}, spectrum = null, performanceSetting = 1,
  stored = {}, reducedMotion = false, deferModel = false, realMedia = false, mediaFetch = null} = {}) {
  let now = 1000, timerId = 0, audio, socket, application, analyserSamples = 0, spectrumSamples = 0;
  let spectrumProfile = spectrum;
  const timers = new Map(), elements = new Map(), sent = [], expressions = [], motionCalls = [];
  const expressionRequests = [], physicsFrames = [], renderedFrames = [], invalidWrites = [];
  const missingLookups = [], faceEvents = [], faceFrames = [], faceInputs = [], faceRecords = [], movementFrames = [], gazeFrames = [];
  const gestureEvents = [], gestureFrames = [];
  const speechPerformanceEvents = [], speechPerformanceFrames = [], speechPerformanceInputs = [];
  const storage = new Map(Object.entries(stored)), fetches = [];
  if (performanceSetting !== null) storage.set('avatar-performance-intensity', String(performanceSetting));
  let nativeExpression = '', delayedExpressions = false, physics = () => {};
  class Element {
    constructor() {
      this.style = {}; this.value = ''; this.textContent = ''; this.innerHTML = '';
      this.children = []; this.listeners = {}; this.clientWidth = 800; this.clientHeight = 600;
      this.attributes = {}; this.hidden = false; this.playCount = 0; this.paused = true;
      const classes = new Set();
      this.classList = {add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name), toggle(name) {classes.has(name) ? classes.delete(name) : classes.add(name);}};
    }
    addEventListener(name, fn) {(this.listeners[name] ||= []).push(fn);}
    emit(name, event = {}) {for (const fn of this.listeners[name] || []) fn(event);}
    appendChild(child) {this.children.push(child); if (child && typeof child === 'object') child.parentNode = this; return child;}
    remove() {
      if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);
      this.parentNode = null;
    }
    append(...children) {for (const child of children) this.appendChild(child);}
    replaceChildren(...children) {
      for (const child of this.children) if (child && typeof child === 'object') child.parentNode = null;
      this.children = []; this.append(...children);
    }
    play() {this.paused = false; this.playCount++; return Promise.resolve();}
    pause() {this.paused = true; this.onpause?.();}
    load() {}
    setAttribute(name, value) {this.attributes[name] = value;}
    removeAttribute(name) {delete this.attributes[name]; if (name === 'src') delete this.src;}
    focus() {}
    scrollIntoView() {}
  }
  function element(id) {
    if (!ids.has(id)) return null;
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }
  class FakeAudio extends Element {
    constructor() {super(); audio = this; this.currentTime = 0; this.duration = 12; this.readyState = 2; this.paused = true; this.playCount = 0;}
    play() {this.paused = false; this.playCount++; return Promise.resolve();}
    pause() {this.paused = true; this.emit('pause');}
    removeAttribute(name) {if (name === 'src') this.src = ''; super.removeAttribute(name);}
    fire(name) {
      if (name === 'playing') {this.paused = false; this.readyState = 4;}
      if (name === 'waiting') this.readyState = 2;
      if (name === 'pause') this.paused = true;
      if (name === 'ended') this.paused = true;
      this.emit(name);
    }
  }
  class FakeSocket {
    static OPEN = 1;
    constructor() {socket = this; this.readyState = 1;}
    send(raw) {sent.push(JSON.parse(raw));}
  }
  class FakeRecognition {
    start() {this.onstart?.();}
    stop() {this.onend?.();}
    abort() {this.onend?.();}
  }
  class FakeAudioContext {
    constructor() {this.state = 'running'; this.destination = {}; this.sampleRate = 48000;}
    addEventListener() {}
    resume() {return Promise.resolve();}
    createMediaElementSource() {return {connect() {}};}
    createAnalyser() {
      const analyser = {fftSize: 1024, frequencyBinCount: 512, smoothingTimeConstant: 0, connect() {},
        getByteTimeDomainData(array) {analyserSamples++; array.fill(151);}};
      if (spectrum !== null) analyser.getByteFrequencyData = array => {
        spectrumSamples++;
        if (spectrumProfile === 'throw') throw new Error('Frequency analysis unavailable');
        array.fill(0);
        if (spectrumProfile === 'low') array.fill(200, 5, 15);
        if (spectrumProfile === 'high') array.fill(200, 48, 83);
      };
      return analyser;
    }
    createBuffer() {return {};}
    createBufferSource() {return {connect() {}, start() {}, stop() {}};}
  }
  const definitions = {
    ParamMouthOpenY: [0, 1, 0], ParamMouthForm: [-1, 1, 0], MouthPuckerWiden: [-1, 1, 0],
    MouthShrug: [0, 1, 0], MouthX: [-1, 1, 0],
    ParamBrowLForm: [-1, 1, 0], ParamBrowRForm: [-1, 1, 0],
    ParamEyeLSmile: [0, 1, 0], ParamEyeRSmile: [0, 1, 0],
    ParamEyeLOpen: [0, 1.2, 1], ParamEyeROpen: [0, 1.2, 1],
    ParamEyeBallX: [-1, 1, 0], ParamEyeBallY: [-1, 1, 0], Param: [-10, 10, 0], Param2: [-10, 10, 0],
    ParamBreath: [0, 1, .5], ParamBodyAngleX: [-10, 10, 0], ParamBodyAngleY: [-10, 10, 0],
    ParamBodyAngleZ: [-10, 10, 0], ParamAngleX: [-30, 30, 0], ParamAngleY: [-30, 30, 0],
    ParamAngleZ: [-30, 30, 0], ParamPositionZ: [-1, 1, 0],
  };
  for (const name of ['Anjian1','Anjian2','Anjian3','Anjian4','Anjian5','Anjian6','Anjian7','Anjian8',
    'Anjian9','Anjian10','Anjian11','Anjian12','Anjian13','Anjian14','Anjian15','Anjian16','Anjian17','Anjian18','Anjian19',
    'shengqi','shangxin','TKDiZhengS']) definitions[name] = [0, 1, 0];
  Object.assign(definitions, ranges);
  for (const id of omit) delete definitions[id];
  const parameterIds = Object.keys(definitions), values = parameterIds.map(id => definitions[id][2]);
  const parameterValues = values;
  let saved = [...values];
  const writes = [];
  const parameters = {ids: parameterIds, count: parameterIds.length, values,
    minimumValues: parameterIds.map(id => definitions[id][0]),
    maximumValues: parameterIds.map(id => definitions[id][1]),
    defaultValues: parameterIds.map(id => definitions[id][2])};
  const core = {
    _model: {parameters}, _parameterIds: parameterIds, _parameterValues: parameterValues,
    getModel() {return this._model;},
    getParameterCount() {return parameterIds.length;},
    getParameterId(index) {return parameterIds[index];},
    getParameterIndex(id) {
      const index = parameterIds.indexOf(String(id));
      if (index < 0) missingLookups.push(id);
      return index;
    },
    getParameterMinimumValue(index) {return parameters.minimumValues[index];},
    getParameterMaximumValue(index) {return parameters.maximumValues[index];},
    getParameterDefaultValue(index) {return parameters.defaultValues[index];},
    getParameterValueById(id) {return values[parameterIds.indexOf(String(id))];},
    getParameterValueByIndex(index) {return values[index];},
    setParameterValueById(id, value, weight = 1) {
      const index = parameterIds.indexOf(String(id));
      if (index < 0) {invalidWrites.push({id, value}); return;}
      writes.push({id, value});
      const clamped = Math.max(parameters.minimumValues[index], Math.min(parameters.maximumValues[index], value));
      values[index] = values[index] * (1 - weight) + clamped * weight;
    },
    setParameterValueByIndex(index, value, weight = 1) {this.setParameterValueById(parameterIds[index], value, weight);},
    addParameterValueById(id, value, weight = 1) {this.setParameterValueById(id, this.getParameterValueById(id) + value * weight);},
    saveParameters() {saved = [...values];},
    loadParameters() {saved.forEach((value, index) => {values[index] = value;});},
    update() {renderedFrames.push(snapshot());},
  };
  function snapshot() {return Object.fromEntries(parameterIds.map((id, index) => [id, values[index]]));}
  const nativeBreath = nativeContract.Breath.create();
  nativeBreath.setParameters([
    ['ParamAngleX', 0, 15, 6.5345, .5], ['ParamAngleY', 0, 8, 3.5345, .5],
    ['ParamAngleZ', 0, 10, 5.5345, .5], ['ParamBodyAngleX', 0, 4, 15.5345, .5],
    ['ParamBreath', 0, .5, 3.2345, .5],
  ].map(args => new nativeContract.BreathParameter(...args)));
  const callbacks = {};
  const expressionManager = {
    resetCount: 0,
    resetExpression() {this.resetCount++; nativeExpression = '';},
    restoreExpression() {nativeExpression = expressions.at(-1) || '';},
    update() {
      if (!nativeExpression) return;
      // Deliberately use real native facial inputs as the underlying expression.
      for (const [id, value] of Object.entries({Anjian3: 1, ParamBrowLForm: .35,
        ParamBrowRForm: -.28, ParamEyeLSmile: .5, ParamEyeRSmile: .4, ParamMouthForm: .25,
        ParamEyeLOpen: .4, ParamEyeROpen: .35, MouthPuckerWiden: -.22, MouthShrug: .18, MouthX: -.16})) {
        if (id in definitions) core.setParameterValueById(id, value);
      }
    },
  };
  const internalModel = {
    coreModel: core, focusController: {focus() {}}, motionManager: {expressionManager},
    pose: null, updateNaturalMovements(deltaTime) {nativeBreath.updateParameters(core, deltaTime / 1000);},
    on(name, fn) {(callbacks[name] ||= []).push(fn);},
    emit(name) {for (const fn of callbacks[name] || []) fn();},
  };
  const model = {
    width: 1000, height: 2000, scale: {set() {}}, internalModel,
    motion(...args) {motionCalls.push(args); return Promise.resolve(true);},
    expression(name) {
      expressions.push(name);
      if (delayedExpressions) {
        const request = deferred();
        expressionRequests.push({name, resolve(value = true) {nativeExpression = name; request.resolve(value);}});
        return request.promise;
      }
      nativeExpression = name;
      return Promise.resolve(true);
    },
  };
  class Application {
    constructor() {
      application = this;
      this.ticker = {maxFPS: 0};
      this.view = new Element(); this.screen = {width: 800, height: 600};
      this.stage = {addChild() {}};
      this.renderer = {resolution: 1, on() {}, resize: (width, height) => {this.screen = {width, height};}};
    }
  }
  const modelRequest = deferred();
  const PIXI = {Application, live2d: {Live2DModel: {from: async () => deferModel ? modelRequest.promise : model}}};
  const document = {documentElement: new Element(), body: new Element(), getElementById: element,
    createElement: () => new Element(), querySelectorAll: () => []};
  const window = {Live2DCubismCore: {}, PIXI, AudioContext: FakeAudioContext, SpeechRecognition: FakeRecognition,
    devicePixelRatio: 1, matchMedia: () => ({matches: reducedMotion}), addEventListener() {}};
  const CompanionFace = require('../brain/static/face.js');
  class ObservedFace extends CompanionFace {
    constructor() {super({random: () => .5});}
    begin(...args) {faceEvents.push({controller:this, type: 'begin', args}); return super.begin(...args);}
    end(...args) {faceEvents.push({controller:this, type: 'end', args}); return super.end(...args);}
    frame(now, options) {
      const result = super.frame(now, options);
      faceRecords.push({controller:this, options:{...options}, result});
      faceInputs.push({...options}); faceFrames.push(result); return result;
    }
  }
  const CompanionMotion = require('../brain/static/motion.js');
  class ObservedMotion extends CompanionMotion {
    frame(now, options) {
      const result = super.frame(now, options);
      movementFrames.push({controller:this, options: {...options}, result});
      return result;
    }
  }
  const CompanionGaze = require('../brain/static/gaze.js');
  class ObservedGaze extends CompanionGaze {
    constructor() {super({random: () => .5});}
    frame(now, options) {
      const result = super.frame(now, options);
      gazeFrames.push({controller:this, options: {...options}, result});
      return result;
    }
  }
  const CompanionGestures = require('../brain/static/gestures.js');
  class ObservedGestures extends CompanionGestures {
    constructor() {super({random: () => .5});}
    begin(...args) {gestureEvents.push({controller:this, type:'begin', args}); return super.begin(...args);}
    end(...args) {gestureEvents.push({controller:this, type:'end', args}); return super.end(...args);}
    start(name, source) {
      const started = super.start(name, source);
      gestureEvents.push({controller:this, type:'start', name, source, started});
      return started;
    }
    frame(now, options) {
      const result = super.frame(now, options);
      gestureFrames.push({controller:this, options:{...options}, result:{...result}});
      return result;
    }
  }
  const CompanionSpeechPerformance = require('../brain/static/speech-performance.js');
  class ObservedSpeechPerformance extends CompanionSpeechPerformance {
    constructor() {super({random: () => .5});}
    begin(...args) {speechPerformanceEvents.push({controller:this, type:'begin', args}); return super.begin(...args);}
    end(...args) {speechPerformanceEvents.push({controller:this, type:'end', args}); return super.end(...args);}
    resetSession(...args) {speechPerformanceEvents.push({controller:this, type:'resetSession', args}); return super.resetSession(...args);}
    start(...args) {speechPerformanceEvents.push({controller:this, type:'cue', cue:args[0]}); return super.start(...args);}
    frame(now, options) {
      const result = super.frame(now, options);
      speechPerformanceInputs.push({...options});
      speechPerformanceFrames.push({controller:this, options:{...options}, result:{...result}});
      return result;
    }
  }
  const context = vm.createContext({document, window, PIXI, Audio: FakeAudio, WebSocket: FakeSocket,
    CompanionFace: ObservedFace,
    ChatInteraction: require('../brain/static/interaction.js'),
    CompanionPresence: require('../brain/static/presence.js'),
    CompanionMotion: ObservedMotion,
    CompanionGaze: ObservedGaze,
    CompanionGestures: ObservedGestures,
    CompanionSpeechPerformance: ObservedSpeechPerformance,
    CompanionSpeechCues: require('../brain/static/speech-cues.js'),
    location: {host: 'localhost', protocol: 'http:', search: ''}, URLSearchParams, URL, AbortController,
    localStorage: {getItem: key => storage.has(key) ? storage.get(key) : null,
      setItem(key, value) {storage.set(key, String(value));}},
    SongDeck: class {stop() {} command() {return false;}},
    ResizeObserver: class {observe() {}},
    performance: {now: () => now}, console,
    fetch: async (...args) => {fetches.push(args); return mediaFetch ? mediaFetch(...args) : {ok: false};},
    setTimeout(fn, delay = 0) {const id = ++timerId; timers.set(id, {at: now + delay, fn}); return id;},
    clearTimeout(id) {timers.delete(id);}, setInterval() {}, clearInterval() {},
  });
  element('autoplay').checked = true;
  element('test-scenario').value = 'chat';
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../brain/static/console.js'), 'utf8'), context);
  if (realMedia) {
    const createProvider = require('./fixtures/bilibili_browser_provider.cjs');
    context.BilibiliBrowserSearch = class {constructor(){return createProvider(context.fetch);}};
    for (const file of ['songs.js','bilibili-player.js'])
      vm.runInContext(fs.readFileSync(path.join(__dirname, '../brain/static', file), 'utf8'), context);
    if (window.BilibiliDeck) context.BilibiliDeck = window.BilibiliDeck;
  }
  vm.runInContext(source, context);
  async function flush() {for (let i = 0; i < 5; i++) await Promise.resolve();}
  await flush();
  assert.equal(vm.runInContext('l2d', context), deferModel ? null : model,
    'the actual page follows the mocked asynchronous model loading lifecycle');
  function receive(message) {socket.onmessage({data: JSON.stringify(message)});}
  receive({type: 'hello', tts: 'indextts2', music_protocol: 6, bilibili_direct:true, bilibili_narration: true, song_narration: true, audio_owner: true, playback: {state: 'idle', queued: 0, paused: false}});
  function tick(ms) {
    const end = now + ms;
    for (let safety = 0; safety < 1000; safety++) {
      const next = [...timers.entries()].filter(([, timer]) => timer.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      timers.delete(next[0]); now = next[1].at; next[1].fn();
      assert(safety < 999, 'timer callback must not spin forever');
    }
    now = end;
  }
  function frame(ms = 16) {
    tick(ms);
    // Cubism4 vendor: motion -> save -> expression -> natural -> physics ->
    // beforeModelUpdate -> model.update -> load. The final load is essential.
    core.saveParameters();
    expressionManager.update();
    internalModel.updateNaturalMovements(ms, now);
    physicsFrames.push(snapshot());
    physics(core);
    internalModel.emit('beforeModelUpdate');
    core.update();
    core.loadParameters();
    return physicsFrames.at(-1);
  }
  function queue({turn = 1, segment = 0, emotion = '开心', actions = [], id = turn * 10 + segment + 1,
    text = '这是一段足够长的测试句子。', duration = 12} = {}) {
    audio.currentTime = 0;
    audio.duration = duration;
    receive({type: 'event', id: turn, source: 'companion', kind: 'chat', user: '你', text: '测试说话表现'});
    receive({type: 'audio', id, turn_id: turn, segment_index: segment,
      url: `/audio/${id}.wav`, text, emotion, actions,
      event: {id: turn, source: 'companion', text: '测试说话表现'}});
  }
  return {context, model, core, audio, app:application, expressions, expressionRequests, expressionManager,
    physicsFrames, renderedFrames, invalidWrites, writes, motionCalls, sent,
    missingLookups, faceEvents, faceFrames, faceInputs, faceRecords, movementFrames, gazeFrames, gestureEvents, gestureFrames,
    speechPerformanceEvents, speechPerformanceFrames, speechPerformanceInputs,
    receive, tick, frame, queue, flush, element, storage, fetches,
    async resolveModel() {modelRequest.resolve(model); await flush();},
    async disconnect() {socket.readyState = 3; await socket.onclose();},
    read: code => vm.runInContext(code, context),
    setDelayedExpressions(value) {delayedExpressions = value;},
    setPhysics(value) {physics = value;},
    setSpectrum(value) {spectrumProfile = value;},
    get analyserSamples() {return analyserSamples;},
    get spectrumSamples() {return spectrumSamples;},
    get nativeExpression() {return nativeExpression;},
  };
}

async function main() {
  const f = await fixture();
  assert.equal(f.app.ticker.maxFPS, 60,
    'the renderer is capped at 60fps so the pinned Cubism physics receives a full fixed-step interval');
  f.frame();
  assert.equal(f.invalidWrites.length, 0);
  const initialBegins = f.faceEvents.filter(event => event.type === 'begin').length;
  f.queue({emotion: '开心'});
  await f.flush();
  f.frame();
  assert.equal(f.faceEvents.filter(event => event.type === 'begin').length, initialBegins,
    'receiving and scheduling an audio source must not begin the facial performance');
  assert.equal(f.read('audioPlaying'), false);
  assert.equal(f.physicsFrames.at(-1).ParamMouthOpenY, 0, 'silent prepared audio keeps the mouth closed');
  f.audio.fire('playing');
  const before = f.faceFrames.length, sampleBefore = f.analyserSamples;
  for (let i = 0; i < 20; i++) {f.audio.currentTime += .016; f.frame();}
  assert.equal(f.faceFrames.length - before, 20, 'one facial frame is sampled per Cubism frame');
  assert.equal(f.analyserSamples - sampleBefore, 20, 'audio energy is sampled once, not again in the final hook');
  assert(f.physicsFrames.at(-1).ParamMouthOpenY > .05,
    'the native physics pass must receive current speech mouth input before model commit/load');
  assert.notEqual(f.physicsFrames.at(-1).ParamEyeLOpen, 1,
    'the native physics pass must receive the custom eyelid input');
  for (let i = 0; i < 150; i++) {f.audio.currentTime += .016; f.frame();}
  assert.equal(f.faceEvents.filter(event => event.type === 'end').length, 0,
    'automatic emotion must remain active beyond the old 1.8 second timer during actual speech');
  f.element('face-intensity').value = '0'; f.element('face-intensity').oninput();
  f.audio.currentTime += .016; f.frame();
  for (const key of ['mouthForm', 'mouthWidth', 'mouthRound', 'mouthShift', 'browL', 'browR', 'eyeSmile']) {
    assert.equal(f.faceFrames.at(-1)[key], 0, 'zero facial intensity suppresses only the optional micro-expression');
  }
  assert(f.physicsFrames.at(-1).ParamMouthOpenY > .05,
    'turning down facial intensity must preserve audible speech lip sync');
  console.log('PASS: actual-playing gate, single sampling and native physics input timing');

  const explicit = await fixture();
  explicit.queue({emotion: '委屈', actions: ['弯眼', '挥手', '前倾']});
  explicit.audio.fire('playing');
  await explicit.flush();
  for (let i = 0; i < 25; i++) {explicit.audio.currentTime += .016; explicit.frame();}
  const native = explicit.physicsFrames.at(-1);
  assert.equal(native.Anjian3, 1, 'native expression switch must survive micro-expression application');
  assert(native.ParamBrowLForm > .2 && native.ParamBrowRForm < -.15,
    'native asymmetric eyebrow expression is the base, not replaced by a neutral face');
  assert(native.ParamEyeLSmile >= .45 && native.ParamEyeRSmile >= .35,
    'native eye smiles are retained');
  assert(native.ParamEyeLOpen <= .5 && native.ParamEyeROpen <= .45,
    'native partial eyelid closure is preserved by the blink multiplier');
  explicit.read('appearance.coatVisible=0;appearance.glassesParam12=1;appearance.glassesParam13=0;');
  explicit.frame();
  assert.equal(explicit.renderedFrames.at(-1).Anjian9, 0, 'facial performance never restores a removed coat');
  assert.equal(explicit.renderedFrames.at(-1).Anjian12, 1, 'glasses appearance survives the facial pass');

  const clock = explicit.read('avatarSpeech.clockMs');
  const waveUntil = explicit.read('timedActions.wave.until');
  const expressionCount = explicit.expressions.length;
  const begins = explicit.faceEvents.filter(event => event.type === 'begin').length;
  explicit.audio.fire('pause');
  explicit.tick(5000); explicit.frame();
  assert.equal(explicit.read('avatarSpeech.clockMs'), clock, 'paused wall time must not consume action duration');
  assert.equal(explicit.read('timedActions.wave.until'), waveUntil);
  explicit.audio.fire('playing');
  await explicit.flush();
  assert.equal(explicit.expressions.length, expressionCount, 'resume must not reload or retrigger the explicit expression');
  assert.equal(explicit.faceEvents.filter(event => event.type === 'begin').length, begins,
    'resume must not restart the speech facial envelope');
  explicit.audio.currentTime += .2; explicit.frame();
  const afterResume = explicit.read('avatarSpeech.clockMs');
  assert(afterResume > clock);
  explicit.audio.fire('waiting');
  explicit.tick(4000); explicit.frame();
  assert.equal(explicit.read('avatarSpeech.clockMs'), afterResume, 'buffering time also freezes the performance clock');
  explicit.audio.fire('playing');
  assert.equal(explicit.read('timedActions.wave.until'), waveUntil, 'a resumed one-shot gesture is never rescheduled');
  console.log('PASS: native expression/appearance composition and pause/buffering continuity');

  explicit.audio.fire('ended');
  explicit.tick(400);
  assert.equal(explicit.read('currentExpression'), '弯眼', 'completed segments retain the expression during the gap grace');
  explicit.queue({turn: 1, segment: 1, emotion: '委屈', actions: ['挥手']});
  explicit.audio.fire('playing');
  await explicit.flush();
  explicit.tick(1100); explicit.frame();
  assert.equal(explicit.read('currentExpression'), '弯眼', 'same-turn segments keep an explicitly selected face');
  assert.equal(explicit.expressions.length, expressionCount);
  assert.equal(explicit.read('timedActions.wave.until'), waveUntil,
    'repeated actions on same-turn segments must not restart a one-shot gesture');
  explicit.queue({turn: 2, segment: 0, emotion: '平静'});
  explicit.audio.fire('playing');
  assert.equal(explicit.read('currentExpression'), '', 'a new turn releases the prior explicit expression');
  assert.equal(explicit.read('Object.keys(timedActions).length'), 0, 'a new turn does not inherit prior transient gestures');
  explicit.receive({type: 'audio_control', id: 21, action: 'interrupted'});
  assert.equal(explicit.read('currentExpression'), '');
  assert.equal(explicit.read('Object.keys(timedActions).length'), 0);
  console.log('PASS: segment grace, same-turn action deduplication, new-turn and interruption cleanup');

  const complete = await fixture();
  complete.queue({actions: ['弯眼', '挥手']}); complete.audio.fire('playing'); await complete.flush();
  complete.frame(); complete.audio.fire('ended');
  const endings = complete.faceEvents.filter(event => event.type === 'end').length;
  complete.tick(899);
  assert.equal(complete.read('currentExpression'), '弯眼');
  assert.equal(complete.faceEvents.filter(event => event.type === 'end').length, endings);
  complete.tick(2);
  assert.equal(complete.read('currentExpression'), '');
  assert.equal(complete.read('avatarSpeech.active'), false);
  assert.equal(complete.faceEvents.filter(event => event.type === 'end').length, endings + 1,
    'completion releases the face after the segment-gap grace expires');
  const clearedGap = await fixture();
  clearedGap.queue({actions: ['弯眼', '挥手']}); clearedGap.audio.fire('playing'); await clearedGap.flush();
  clearedGap.audio.fire('ended'); clearedGap.tick(100);
  assert.equal(clearedGap.read('currentAudioItem'), null);
  clearedGap.receive({type: 'audio_control', action: 'clear', id: null});
  assert.equal(clearedGap.read('currentExpression'), '', 'clear during the segment gap releases the held expression immediately');
  assert.equal(clearedGap.read('Object.keys(timedActions).length'), 0);
  const disconnectedGap = await fixture();
  disconnectedGap.queue({actions: ['弯眼', '挥手']}); disconnectedGap.audio.fire('playing'); await disconnectedGap.flush();
  disconnectedGap.audio.fire('ended'); disconnectedGap.tick(100);
  await disconnectedGap.disconnect();
  assert.equal(disconnectedGap.read('currentExpression'), '', 'disconnect during the segment gap does not leave an orphan face');
  assert.equal(disconnectedGap.read('Object.keys(timedActions).length'), 0);
  assert.equal(disconnectedGap.read('avatarSpeech.releaseTimer'), null);
  console.log('PASS: completion grace expiry and cleanup during silent inter-segment gaps');

  const optional = await fixture({omit: ['MouthPuckerWiden', 'MouthShrug', 'MouthX', 'ParamEyeLSmile', 'ParamEyeRSmile', 'ParamBrowRForm']});
  optional.queue(); optional.audio.fire('playing'); optional.frame();
  assert.deepEqual(optional.invalidWrites, [], 'absent model parameters must never receive fabricated writes');
  assert.deepEqual(optional.missingLookups, [], 'absence is checked from the parameter IDs before Cubism can create a virtual ID');
  optional.context.testCore = optional.core;
  optional.context.extremeFace = {mouthOpen: 99, mouthForm: 99, mouthWidth: -99, mouthRound: 99, mouthShift: -99,
    browL: -99, browR: 99, eyeSmile: 99, eyeOpenL: 99, eyeOpenR: -99, accent: 99};
  const oldWriteCount = optional.writes.length;
  optional.read('applyAvatarFace(testCore,extremeFace)');
  for (const write of optional.writes.slice(oldWriteCount)) {
    const index = optional.core.getParameterIndex(write.id);
    assert(Number.isFinite(write.value), `${write.id} receives a finite value`);
    assert(write.value >= optional.core.getParameterMinimumValue(index)
      && write.value <= optional.core.getParameterMaximumValue(index), `${write.id} respects actual model bounds before write`);
  }
  assert.deepEqual(optional.invalidWrites, []);
  const defaults = await fixture({ranges: {ParamMouthForm: [-.4, .8, .3], ParamBrowLForm: [-1, .6, .2]}});
  defaults.context.testCore = defaults.core;
  defaults.context.offsetFace = {mouthForm: -.5, browL: .5};
  defaults.read('applyAvatarFace(testCore,offsetFace)');
  assert(Math.abs(defaults.core.getParameterValueById('ParamMouthForm') - (.3 - (1 - Math.exp(-5)) / (1 - Math.exp(-10)) * .7)) < 1e-8,
    'calibrated negative mouth offsets still use this model\'s asymmetric default-to-minimum span');
  assert(Math.abs(defaults.core.getParameterValueById('ParamBrowLForm') - .4) < 1e-8,
    'positive offsets use the default-to-maximum span of this actual model');
  console.log('PASS: optional parameter compatibility and actual model range clamping');

  const race = await fixture();
  race.setDelayedExpressions(true);
  race.queue({turn: 1, actions: ['弯眼']}); race.audio.fire('playing');
  await race.flush();
  race.queue({turn: 2, actions: ['生气']}); race.audio.fire('playing');
  await race.flush();
  assert.equal(race.expressionRequests.length, 1, 'asset loads serialize while an older request remains pending');
  assert.equal(race.read('currentExpression'), '生气', 'the desired face advances immediately to the new turn');
  race.expressionRequests[0].resolve(); await race.flush();
  assert.equal(race.expressionRequests.length, 2);
  assert.equal(race.nativeExpression, '', 'a late asset is cleared before the next serialized load');
  race.expressionRequests[1].resolve();
  await race.flush();
  assert.equal(race.read('currentExpression'), '生气');
  assert.equal(race.nativeExpression, '生气', 'a late expression load cannot replace the newer visible expression');
  race.receive({type: 'audio_control', id: 21, action: 'interrupted'});
  assert.equal(race.nativeExpression, '', 'interrupt immediately clears the applied expression');
  race.queue({turn: 3, actions: ['脸红']}); race.audio.fire('playing');
  await race.flush();
  assert.equal(race.expressionRequests.length, 3);
  race.receive({type: 'audio_control', id: 31, action: 'interrupted'});
  race.expressionRequests[2].resolve(); await race.flush();
  assert.equal(race.nativeExpression, '', 'an asset completing after interruption cannot restore the cancelled face');
  console.log('PASS: out-of-order asynchronous expression loads cannot revive stale facial state');

  // Compare simultaneous performances with identical gaze/body baselines. This
  // checks the actual parameter values arriving at physics, not just an isolated
  // controller's return value, and needs no amplitude spikes to trigger accents.
  const head = await fixture(), noHead = await fixture(), noFace = await fixture();
  const headFixtures = [head, noHead, noFace];
  for (const item of headFixtures) {
    item.read('eyeMotion.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:false});');
  }
  noHead.element('speech-head-intensity').value = '0';
  noHead.element('speech-head-intensity').oninput();
  noFace.element('face-intensity').value = '0';
  noFace.element('face-intensity').oninput();
  for (let i = 0; i < 90; i++) for (const item of headFixtures) item.frame();
  for (const item of headFixtures) {
    item.queue({emotion: '平静'});
    item.frame();
    assert.equal(item.movementFrames.at(-1).options.speechEnergy, 0,
      'queued audio cannot create speech-driven head motion before actual playing');
    assert.equal(item.faceEvents.length, 0);
    item.audio.fire('playing');
  }
  let headXPeak = 0, headZPeak = 0, silentFaceDifference = 0;
  for (let i = 0; i < 420; i++) {
    const poses = headFixtures.map(item => {item.audio.currentTime += .016; return item.frame();});
    headXPeak = Math.max(headXPeak, Math.abs(poses[0].ParamAngleX - poses[1].ParamAngleX));
    headZPeak = Math.max(headZPeak, Math.abs(poses[0].ParamAngleZ - poses[1].ParamAngleZ));
    silentFaceDifference = Math.max(silentFaceDifference,
      Math.abs(poses[0].ParamAngleX - poses[2].ParamAngleX),
      Math.abs(poses[0].ParamAngleZ - poses[2].ParamAngleZ));
    assert.equal(head.faceFrames.at(-1).accent, 0, 'steady speech must exercise the continuous motion path without accent events');
    assert.equal(noHead.movementFrames.at(-1).options.headIntensity, 0,
      'the dedicated head slider must reach the motion controller');
  }
  assert(headXPeak > 1.5, `steady speaking adds visible head turns before physics (${headXPeak.toFixed(2)} degrees)`);
  assert(headZPeak > .8, `steady speaking adds visible head tilts before physics (${headZPeak.toFixed(2)} degrees)`);
  assert(head.movementFrames.at(-1).options.speechEnergy > .3,
    'the page passes continuous actual audio energy to body motion');
  assert(noFace.movementFrames.at(-1).options.speechEnergy > .3,
    'turning facial offsets off does not remove real speech energy');
  assert(silentFaceDifference < 1e-8, 'facial intensity zero keeps independent head movement unchanged');
  assert.deepEqual(head.invalidWrites, []);
  for (const item of headFixtures) item.audio.fire('pause');
  for (let i = 0; i < 240; i++) for (const item of headFixtures) item.frame();
  // The independent phrase performance holds its deliberate pose while paused
  // (covered in test_avatar_speech_performance_ui); continuous energy-driven
  // motion still releases its speech sway to the underlying idle baseline.
  for (const axis of ['headX', 'headY', 'headZ']) {
    assert(Math.abs(head.movementFrames.at(-1).result[axis] - noHead.movementFrames.at(-1).result[axis]) < .1,
      `${axis} continuous sway returns to its idle baseline after speech pauses`);
  }
  assert.equal(head.movementFrames.at(-1).options.speaking, false);
  assert.equal(head.movementFrames.at(-1).options.speechEnergy, 0);
  console.log('PASS: continuous speech head motion, actual-playing gate, independent head/face controls and pause recovery');

  const eyes = await fixture();
  assert.equal(eyes.element('eye-intensity').value, '75', 'eye activity has its independent default');
  eyes.frame();
  assert.equal(eyes.gazeFrames.length, 1, 'gaze is sampled once in each real model frame');
  assert.equal(eyes.faceInputs.length, 1, 'gaze integration must not sample the face twice');
  assert.equal(eyes.gazeFrames.at(-1).options.speaking, false);
  assert.equal(eyes.gazeFrames.at(-1).options.intensity, .75);
  assert.equal(eyes.faceInputs.at(-1).eyeIntensity, .75);
  eyes.receive({type: 'thinking', id: 99});
  eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.thinking, true, 'server thinking state reaches gaze');
  eyes.element('voice-input').onclick();
  eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.listening, true, 'actual microphone lifecycle reaches gaze');
  eyes.element('voice-input').onclick();
  eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.listening, false);
  eyes.queue({actions: ['弯眼']});
  eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.speaking, false,
    'prepared audio must not select speaking gaze before a playing event');
  eyes.audio.fire('playing'); await eyes.flush();
  eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.speaking, true);
  assert.equal(eyes.gazeFrames.at(-1).options.thinking, false);
  assert.equal(eyes.gazeFrames.at(-1).options.explicitFace, true,
    'an explicit native expression suppresses conflicting optional eye gestures');
  assert(eyes.gazeFrames.at(-1).options.eyeOpen <= .4,
    'the gaze controller sees the native expression eyelid state');
  eyes.audio.fire('waiting'); eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.speaking, false);
  assert.equal(eyes.gazeFrames.at(-1).options.paused, true, 'buffering cannot run a speaking glance');
  eyes.audio.fire('playing'); eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.paused, false);
  eyes.audio.fire('pause'); eyes.frame();
  assert.equal(eyes.gazeFrames.at(-1).options.paused, true);
  console.log('PASS: independent eye activity and actual playback, microphone, thinking and native-expression gaze wiring');

  const coordinatedEyes = await fixture();
  coordinatedEyes.queue({emotion: '惊讶'}); coordinatedEyes.audio.fire('playing');
  coordinatedEyes.setPhysics(core => {
    // These are the model's real pupil-depth physics output channels. The
    // final gaze correction may mix them, but cannot undo the face's eyelids.
    core.setParameterValueById('Param', 7);
    core.setParameterValueById('Param2', -3);
  });
  let previousBlinkRequest = false, maxPupilCorrection = 0, maxOpen = 0;
  for (let i = 0; i < 360; i++) {
    coordinatedEyes.audio.currentTime += .016;
    const beforePhysics = coordinatedEyes.frame(), rendered = coordinatedEyes.renderedFrames.at(-1);
    const raw = coordinatedEyes.gazeFrames.at(-1).result;
    const motion = coordinatedEyes.movementFrames.at(-1);
    assert.equal(motion.options.gazeX, raw.headX, 'head follow uses fixation, not the eye-only micro movement');
    assert.equal(motion.options.gazeY, raw.headY);
    assert.equal(coordinatedEyes.faceInputs.at(-1).gazeBlink, previousBlinkRequest,
      'blink cues advance exactly one frame without taking an extra face sample');
    previousBlinkRequest = raw.blinkRequest;
    for (const id of ['ParamEyeBallX', 'ParamEyeBallY']) {
      assert.equal(rendered[id], beforePhysics[id], 'physics input and final eye position use the same coordinated gaze');
      assert(Number.isFinite(rendered[id]) && Math.abs(rendered[id]) <= 1);
    }
    for (const id of ['ParamEyeLOpen', 'ParamEyeROpen', 'ParamBrowLForm', 'ParamBrowRForm', 'ParamEyeLSmile', 'ParamEyeRSmile']) {
      assert.equal(rendered[id], beforePhysics[id], 'final pupil correction preserves face inputs applied before physics');
    }
    maxPupilCorrection = Math.max(maxPupilCorrection, Math.abs(beforePhysics.ParamEyeBallX - raw.x));
    maxOpen = Math.max(maxOpen, beforePhysics.ParamEyeLOpen);
    assert.notEqual(rendered.Param, 7, 'pupil-depth physics is blended with the final gaze');
  }
  assert(maxPupilCorrection > .015, 'real head movement adds bounded eye compensation');
  assert(maxOpen > 1, 'the upper eyelid has room for a visible surprised lift');
  assert.equal(coordinatedEyes.gazeFrames.length, coordinatedEyes.faceFrames.length);
  console.log('PASS: shared pre/post-physics gaze, head compensation and preserved eyelid/eyebrow inputs');

  const noEyes = await fixture();
  noEyes.element('eye-intensity').value = '0'; noEyes.element('eye-intensity').oninput();
  noEyes.queue({emotion: '平静'}); noEyes.audio.fire('playing');
  let blinkMinimum = 1, jawMaximum = 0, headMaximum = 0;
  for (let i = 0; i < 500; i++) {
    noEyes.audio.currentTime += .016;
    const pose = noEyes.frame(), gaze = noEyes.gazeFrames.at(-1);
    assert.equal(gaze.options.intensity, 0);
    assert.equal(noEyes.faceInputs.at(-1).eyeIntensity, 0);
    for (const axis of ['x', 'y', 'headX', 'headY']) {
      assert(Math.abs(gaze.result[axis]) < 1e-8, 'zero eye activity suppresses optional automatic glances and gaze-driven head follow');
    }
    assert.equal(gaze.result.blinkRequest, false, 'optional gaze blinks are disabled at zero activity');
    blinkMinimum = Math.min(blinkMinimum, pose.ParamEyeLOpen);
    jawMaximum = Math.max(jawMaximum, pose.ParamMouthOpenY);
    headMaximum = Math.max(headMaximum, Math.abs(pose.ParamAngleX));
  }
  assert(blinkMinimum < .05, 'basic physiological blinking survives the optional eye activity control');
  assert(jawMaximum > .1, 'turning off extra eye activity preserves lip sync');
  assert(headMaximum > 1.5, 'turning off extra eye activity preserves independent speech head motion');
  assert.deepEqual(noEyes.invalidWrites, []);
  console.log('PASS: zero eye activity disables glances while keeping basic blinks, lip sync and speech head motion');

  const smile = await fixture();
  // A freshly loaded model may start at dt=0. Let its default expression ease
  // in without any begin(), audio message or native expression asset.
  for (let i = 0; i < 60; i++) smile.frame();
  const idleSmile = smile.physicsFrames.at(-1).ParamMouthForm;
  assert(idleSmile > .58 && idleSmile < .7,
    'the default smile reaches its stronger calibrated native input near .65');
  assert.equal(smile.renderedFrames.at(-1).ParamMouthForm, idleSmile,
    'the idle smile reaches both physics and the final rendered model');
  assert.equal(smile.physicsFrames.at(-1).ParamMouthOpenY, 0,
    'a default smile does not open a silent mouth');
  assert.equal(smile.faceEvents.length, 0, 'the default smile needs no synthetic speech start');
  smile.queue({emotion: '平静'});
  for (let i = 0; i < 30; i++) smile.frame();
  assert.equal(smile.physicsFrames.at(-1).ParamMouthOpenY, 0, 'prepared audio still keeps the smiling mouth closed');
  assert(smile.physicsFrames.at(-1).ParamMouthForm > .005);
  smile.audio.fire('playing');
  for (let i = 0; i < 60; i++) {smile.audio.currentTime += .016; smile.frame();}
  assert(smile.physicsFrames.at(-1).ParamMouthForm > .005, 'neutral speech retains the gentle default smile');
  assert(smile.physicsFrames.at(-1).ParamMouthOpenY > .05, 'actual neutral speech animates the smiling jaw');
  smile.audio.fire('ended');
  for (let i = 0; i < 200; i++) smile.frame();
  assert.equal(smile.physicsFrames.at(-1).ParamMouthOpenY, 0);
  assert(smile.physicsFrames.at(-1).ParamMouthForm > .58
    && smile.physicsFrames.at(-1).ParamMouthForm < .7, 'completed speech returns to the stronger calibrated resting smile');
  smile.queue({turn: 2, emotion: '委屈'}); smile.audio.fire('playing');
  for (let i = 0; i < 90; i++) {smile.audio.currentTime += .016; smile.frame();}
  assert(smile.physicsFrames.at(-1).ParamMouthForm < -.015,
    'a downcast spoken expression takes priority over the default smile');
  smile.queue({turn: 3, emotion: '开心', actions: ['弯眼']}); smile.audio.fire('playing'); await smile.flush();
  for (let i = 0; i < 45; i++) {smile.audio.currentTime += .016; smile.frame();}
  assert.equal(smile.physicsFrames.at(-1).ParamMouthForm, .25,
    'an explicit native mouth shape is preserved exactly, without an extra default smile');
  assert.equal(smile.faceFrames.at(-1).mouthForm, 0);
  assert(smile.physicsFrames.at(-1).ParamEyeLOpen <= .4
    && smile.physicsFrames.at(-1).ParamEyeROpen <= .35, 'the resting smile never forces native eyelids open');
  console.log('PASS: slight resting and neutral smile, silent-mouth gating, negative-emotion and native-expression priority');

  const eyeOnlySpeech = await fixture();
  for (const id of ['motion-intensity', 'speech-head-intensity']) {
    eyeOnlySpeech.element(id).value = '0'; eyeOnlySpeech.element(id).oninput();
  }
  eyeOnlySpeech.queue({emotion: '平静',text:'我想想，这段旋律的细节值得再听一遍。',duration:5});
  for (let i = 0; i < 60; i++) eyeOnlySpeech.frame();
  assert(eyeOnlySpeech.physicsFrames.every(pose => Math.abs(pose.ParamEyeBallX) < .03),
    'queued but unplayed speech does not start its reflective glance');
  eyeOnlySpeech.audio.fire('playing');
  let left = 0, right = 0, finalLeft = 0, finalRight = 0;
  for (let i = 0; i < 312; i++) {
    eyeOnlySpeech.audio.currentTime += .016;
    const pose = eyeOnlySpeech.frame(), rendered = eyeOnlySpeech.renderedFrames.at(-1);
    left = Math.min(left, pose.ParamEyeBallX); right = Math.max(right, pose.ParamEyeBallX);
    finalLeft = Math.min(finalLeft, rendered.ParamEyeBallX); finalRight = Math.max(finalRight, rendered.ParamEyeBallX);
    assert.equal(pose.ParamAngleX, 0, 'this test cannot mistake head turns or their eye compensation for pupil movement');
    assert.equal(pose.ParamAngleZ, 0);
    assert.equal(rendered.ParamEyeBallX, pose.ParamEyeBallX, 'both physics and final render receive the speaking glance');
  }
  assert(Math.max(-left,right) > .18,
    `reflective wording makes a visible glance independently of head motion (${left.toFixed(3)}, ${right.toFixed(3)})`);
  assert(Math.max(-finalLeft,finalRight) > .18, 'physics cannot erase the intentional pupil movement');
  assert(Math.abs(eyeOnlySpeech.renderedFrames.at(-1).ParamEyeBallX)<.03,
    'the reflective glance returns to the listener without periodically scanning the other side');
  eyeOnlySpeech.audio.fire('pause');
  for (let i = 0; i < 90; i++) eyeOnlySpeech.frame();
  assert(Math.abs(eyeOnlySpeech.renderedFrames.at(-1).ParamEyeBallX) < .005,
    'paused speech smoothly returns its glances to the camera');
  assert.equal(eyeOnlySpeech.gazeFrames.at(-1).options.paused, true);
  console.log('PASS: reflective wording produces a visible glance and return independently of head motion');

  const noBodyMotion = await fixture();
  noBodyMotion.element('motion-intensity').value = '0'; noBodyMotion.element('motion-intensity').oninput();
  noBodyMotion.queue({emotion: '平静',text:'我想想，这段旋律的细节值得再听一遍。',duration:5}); noBodyMotion.audio.fire('playing');
  for (let i = 0; i < 320; i++) {
    noBodyMotion.audio.currentTime += .016;
    const pose = noBodyMotion.frame();
    for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX', 'ParamBodyAngleY', 'ParamBodyAngleZ', 'ParamBreath']) {
      assert.equal(pose[id], 0, `${id} must not inherit hidden vendor breathing when global motion is disabled`);
    }
  }
  assert(noBodyMotion.physicsFrames.at(-1).ParamMouthOpenY > .05,
    'disabling body motion does not disable the actual-playing lip sync');
  assert(noBodyMotion.gazeFrames.some(frame => Math.abs(frame.result.x) > .055),
    'disabling body motion does not disable the independent speaking pupils');
  console.log('PASS: global motion control suppresses real vendor breath without suppressing mouth or eye animation');

  const darkVoice = await fixture({spectrum:'low'}), brightVoice = await fixture({spectrum:'high'});
  for (const item of [darkVoice, brightVoice]) item.queue({emotion:'平静'});
  for (let i = 0; i < 20; i++) for (const item of [darkVoice, brightVoice]) item.frame();
  for (const item of [darkVoice, brightVoice]) {
    assert.equal(item.spectrumSamples, 0, 'frequency analysis cannot animate prepared but unplayed speech');
    assert.equal(item.faceInputs.at(-1).brightness, null);
    item.audio.fire('playing');
  }
  let jawDifference = 0, widthDifference = 0, roundDifference = 0, shiftPeak = 0;
  for (let i = 0; i < 120; i++) {
    const poses = [darkVoice, brightVoice].map(item => {item.audio.currentTime += .016; return item.frame();});
    jawDifference = Math.max(jawDifference, Math.abs(poses[0].ParamMouthOpenY - poses[1].ParamMouthOpenY));
    shiftPeak = Math.max(shiftPeak, Math.abs(poses[0].MouthX), Math.abs(poses[1].MouthX));
    if (i >= 60) {
      widthDifference = Math.max(widthDifference, poses[1].MouthPuckerWiden - poses[0].MouthPuckerWiden);
      roundDifference = Math.max(roundDifference, poses[0].MouthShrug - poses[1].MouthShrug);
    }
    for (let j = 0; j < 2; j++) {
      const item = [darkVoice, brightVoice][j];
      for (const id of ['ParamMouthOpenY', 'MouthPuckerWiden', 'MouthShrug', 'MouthX']) {
        assert.equal(item.renderedFrames.at(-1)[id], poses[j][id],
          'all mouth axes are present before physics and survive the final pupil/parts pass');
      }
    }
  }
  assert(darkVoice.faceInputs.at(-1).brightness < .1 && brightVoice.faceInputs.at(-1).brightness > .9,
    'the real spectrum helper distinguishes low and high band energy at equal RMS');
  assert(jawDifference < 1e-8, 'frequency brightness changes lip shape independently of RMS-driven jaw opening');
  assert(widthDifference > .2, `equal-volume speech has a visible native width contrast (${widthDifference.toFixed(3)})`);
  assert(roundDifference > .025, `low-band speech adds rounding at equal jaw opening (${roundDifference.toFixed(3)})`);
  assert(shiftPeak > .05 && shiftPeak < .25, 'the sideways mouth channel visibly participates while staying subordinate to jaw and width');
  for (const item of [darkVoice, brightVoice]) {
    assert.equal(item.spectrumSamples, 120, 'the spectrum is sampled once per actual playing frame');
    assert.equal(item.analyserSamples, 120, 'adding spectrum does not duplicate waveform/RMS sampling');
    assert.deepEqual(item.invalidWrites, []);
    item.audio.fire('pause');
  }
  for (let i = 0; i < 90; i++) for (const item of [darkVoice, brightVoice]) item.frame();
  for (const item of [darkVoice, brightVoice]) {
    const pose = item.physicsFrames.at(-1);
    assert.equal(pose.ParamMouthOpenY, 0, 'paused spectrum cannot hold the jaw open');
    assert(Math.abs(pose.MouthX) < .01 && pose.MouthShrug < .01,
      'paused speech releases auxiliary lip rounding and sideways movement');
    assert.equal(item.faceInputs.at(-1).brightness, null);
    assert.equal(item.spectrumSamples, 120);
  }
  assert(Math.abs(darkVoice.physicsFrames.at(-1).MouthPuckerWiden
    - brightVoice.physicsFrames.at(-1).MouthPuckerWiden) < .01, 'paused vowels settle to the same emotional baseline');
  console.log('PASS: equal-RMS spectral contrast drives independent width/rounding before physics with pause recovery');

  const noSpectrum = await fixture();
  noSpectrum.queue({emotion:'平静'}); noSpectrum.audio.fire('playing');
  for (let i = 0; i < 90; i++) {
    noSpectrum.audio.currentTime += .016;
    const pose = noSpectrum.frame();
    assert.equal(noSpectrum.faceInputs.at(-1).brightness, null, 'missing frequency APIs use the deliberate null fallback');
    for (const id of ['ParamMouthOpenY', 'MouthPuckerWiden', 'MouthShrug', 'MouthX']) assert(Number.isFinite(pose[id]));
  }
  assert(noSpectrum.physicsFrames.at(-1).ParamMouthOpenY > .05,
    'RMS-only playback keeps audible lip sync without inventing a spectrum');
  darkVoice.audio.fire('playing'); darkVoice.setSpectrum('throw');
  darkVoice.audio.currentTime += .016; darkVoice.frame();
  assert.equal(darkVoice.faceInputs.at(-1).brightness, null, 'a failed frequency read degrades to the same supported fallback');
  assert(darkVoice.physicsFrames.at(-1).ParamMouthOpenY > .05);
  darkVoice.setSpectrum('high');
  for (let i = 0; i < 30; i++) {darkVoice.audio.currentTime += .016; darkVoice.frame();}
  assert(darkVoice.faceInputs.at(-1).brightness > .9, 'frequency analysis can recover after a transient read error');
  darkVoice.element('face-intensity').value = '0'; darkVoice.element('face-intensity').oninput();
  darkVoice.audio.currentTime += .016; darkVoice.frame();
  for (const id of ['MouthPuckerWiden', 'MouthShrug', 'MouthX']) {
    assert.equal(darkVoice.physicsFrames.at(-1)[id], 0, 'zero face intensity suppresses every optional lip-shape channel');
  }
  assert(darkVoice.physicsFrames.at(-1).ParamMouthOpenY > .05);
  brightVoice.queue({turn:2, emotion:'开心', actions:['弯眼']}); brightVoice.audio.fire('playing'); await brightVoice.flush();
  for (let i = 0; i < 30; i++) {brightVoice.audio.currentTime += .016; brightVoice.frame();}
  for (const [id, expected] of [['MouthPuckerWiden',-.22], ['MouthShrug',.18], ['MouthX',-.16]]) {
    assert.equal(brightVoice.physicsFrames.at(-1)[id], expected,
      'an explicit native expression owns its width, rounded shape and asymmetric mouth placement');
  }
  assert(brightVoice.physicsFrames.at(-1).ParamMouthOpenY > .05,
    'native expression priority still leaves the speech jaw active');
  console.log('PASS: spectral fallback/error recovery and zero/native-expression priority preserve safe multi-axis lip sync');

  const nativePage = await fixture();
  const nativeIds = ['ParamMouthForm', 'ParamMouthOpenY', 'ParamEyeLOpen', 'ParamEyeROpen',
    'ParamEyeBallX', 'ParamEyeBallY', 'ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamBodyAngleX'];
  const nativeDefaults = nativeIds.map(id => id === 'ParamEyeLOpen' || id === 'ParamEyeROpen' ? 1 : 0);
  const calls = [], observed = {};
  const nativeModel = {
    parameters: {ids:nativeIds, count:nativeIds.length, values:Float32Array.from(nativeDefaults),
      minimumValues:Float32Array.from(nativeIds.map(id => id === 'ParamMouthOpenY' || /Eye[LR]Open/.test(id) ? 0 : -1)),
      maximumValues:Float32Array.from(nativeIds.map(id => /Eye[LR]Open/.test(id) ? 1.2 : 1)),
      defaultValues:Float32Array.from(nativeDefaults)},
    parts: {count:0, ids:[], opacities:new Float32Array()},
    drawables: {count:0, ids:[], resetDynamicFlags() {}},
    update() {calls.push('core.update'); observed.rendered = realCore.getParameterValueById('ParamMouthForm');},
  };
  const realCore = new nativeContract.Core(nativeModel);
  nativePage.context.realCore = realCore;
  assert.deepEqual(JSON.parse(JSON.stringify(nativePage.read('avatarParameter(realCore,"ParamMouthForm")'))),
    {low:-1, high:1, center:0}, 'the page reads native adapter IDs and real range APIs');
  assert.equal(nativePage.read('avatarParameter(realCore,"NotInModel")'), undefined);
  const runtime = Object.create(nativeContract.Internal.prototype);
  Object.assign(runtime, {
    coreModel:realCore, focusController:{x:0,y:0}, emit:name=>calls.push(name),
    idParamEyeBallX:'ParamEyeBallX', idParamEyeBallY:'ParamEyeBallY',
    idParamAngleX:'ParamAngleX', idParamAngleY:'ParamAngleY', idParamAngleZ:'ParamAngleZ', idParamBodyAngleX:'ParamBodyAngleX',
    motionManager:{update() {calls.push('motion'); return false;}, expressionManager:{update() {calls.push('expression');}}},
    eyeBlink:{updateParameters() {calls.push('nativeBlink');}},
    updateNaturalMovements() {
      calls.push('customFace');
      nativePage.read('applyAvatarFace(realCore,{mouthForm:.052,mouthOpen:.6,eyeOpenL:.9,eyeOpenR:.9})');
    },
    physics:{evaluate() {calls.push('physics'); observed.physics = realCore.getParameterValueById('ParamMouthForm');}},
    pose:{updateParameters() {calls.push('pose');}},
  });
  const save = realCore.saveParameters.bind(realCore), load = realCore.loadParameters.bind(realCore);
  realCore.saveParameters = () => {calls.push('save'); save();};
  realCore.loadParameters = () => {calls.push('load'); load();};
  runtime.update(16, 1000);
  assert.deepEqual(calls, ['beforeMotionUpdate', 'motion', 'afterMotionUpdate', 'save', 'expression',
    'nativeBlink', 'customFace', 'physics', 'pose', 'beforeModelUpdate', 'core.update', 'load']);
  const calibratedSmile = (1 - Math.exp(-.052 * 10)) / (1 - Math.exp(-10));
  assert(Math.abs(observed.physics - calibratedSmile) < 1e-6);
  assert(Math.abs(observed.rendered - calibratedSmile) < 1e-6,
    'the true adapter submits the custom face before restoring saved parameters');
  assert.equal(realCore.getParameterValueById('ParamMouthForm'), 0,
    'the vendor reload happens after model.update and is not a second render');
  assert.equal(Object.keys(realCore._notExistParameterId).length, 0,
    'optional channels never create virtual IDs in the real adapter');
  console.log('PASS: actual pinned Cubism adapter parameter APIs and real update lifecycle preserve the page face input');

  const settings = await fixture({performanceSetting:null});
  assert.equal(settings.element('performance-intensity').value, '180', 'fresh installs use the visibly stronger 180 percent preset');
  assert.match(settings.element('performance-level').textContent, /180%/);
  assert.equal(settings.element('performance-bold').attributes['aria-pressed'], 'true');
  settings.frame();
  for (const options of [settings.faceInputs.at(-1), settings.gazeFrames.at(-1).options, settings.movementFrames.at(-1).options]) {
    assert.equal(options.performance, 1.8, 'the selected global performance reaches every controller');
  }
  for (const [id, percent, value] of [['performance-natural','100',1], ['performance-bold','180',1.8], ['performance-exaggerated','260',2.6]]) {
    settings.element(id).onclick(); settings.frame();
    assert.equal(settings.element('performance-intensity').value, percent);
    assert.equal(settings.storage.get('avatar-performance-intensity'), String(value));
    assert.equal(settings.element(id).attributes['aria-pressed'], 'true');
    assert.equal(settings.faceInputs.at(-1).performance, value);
    assert.equal(settings.gazeFrames.at(-1).options.performance, value);
    assert.equal(settings.movementFrames.at(-1).options.performance, value);
  }
  for (const [raw, expected] of [['0',0], ['300',3], ['999',3], ['-20',0]]) {
    settings.element('performance-intensity').value = raw; settings.element('performance-intensity').oninput(); settings.frame();
    assert.equal(settings.storage.get('avatar-performance-intensity'), String(expected));
    assert.equal(settings.element('performance-intensity').value, String(expected * 100));
    assert.equal(settings.faceInputs.at(-1).performance, expected);
    assert.equal(settings.gazeFrames.at(-1).options.performance, expected);
    assert.equal(settings.movementFrames.at(-1).options.performance, expected);
  }
  for (const [saved, percent] of [['0','0'], ['2.6','260'], ['9','300'], ['-2','0'], ['invalid','180'], ['   ','180']]) {
    const restored = await fixture({performanceSetting:null, stored:{'avatar-performance-intensity':saved}});
    assert.equal(restored.element('performance-intensity').value, percent,
      'saved settings restore, clamp or fall back without losing an intentional zero');
  }
  const reduced = await fixture({performanceSetting:null, reducedMotion:true});
  assert.equal(reduced.element('performance-intensity').value, '100');
  const noPerformance = await fixture({performanceSetting:0});
  noPerformance.queue({emotion:'平静'}); noPerformance.audio.fire('playing');
  for (let i = 0; i < 120; i++) {noPerformance.audio.currentTime += .016; noPerformance.frame();}
  const quietPose = noPerformance.physicsFrames.at(-1);
  for (const id of ['ParamAngleX', 'ParamAngleY', 'ParamAngleZ', 'ParamMouthForm', 'MouthPuckerWiden', 'MouthShrug', 'MouthX']) {
    assert.equal(quietPose[id], 0, 'global zero suppresses optional performance without losing the setting');
  }
  assert(quietPose.ParamMouthOpenY > .05, 'global zero keeps actual speech lip sync available');
  console.log('PASS: performance defaults, presets, storage, reduced-motion preference and 0–300 controller wiring');

  const preview = await fixture({performanceSetting:null});
  const [liveFace, liveEyes, liveMotion] = preview.read('[faceMotion, eyeMotion, bodyMotion]');
  const initialPreviewState = {
    sent:JSON.stringify(preview.sent), fetches:preview.fetches.length, plays:preview.audio.playCount,
    timeline:preview.element('events').children.length,
    speech:preview.read('JSON.stringify(avatarSpeech)'),
    faceClock:liveFace.clock, gazeClock:liveEyes.clock, motionTime:liveMotion.time,
  };
  preview.element('avatar-preview').onclick();
  assert.equal(preview.read('Boolean(avatarPreview)'), true);
  assert.equal(preview.read('avatarPreview.face === faceMotion || avatarPreview.eyes === eyeMotion || avatarPreview.motion === bodyMotion'), false,
    'the silent preview owns independent face, gaze and body controllers');
  const previewState = preview.read('avatarPreview');
  assert.equal(preview.element('avatar-preview').attributes['aria-pressed'], 'true');
  for (let i = 0; i < 55; i++) preview.frame();
  assert.equal(preview.faceRecords.filter(frame => frame.controller === previewState.face).at(-1).options.playing,
    false, 'the first preview second is idle');
  assert.equal(preview.physicsFrames.at(-1).ParamMouthOpenY, 0);
  for (let i = 0; i < 80; i++) preview.frame();
  assert.equal(preview.faceRecords.filter(frame => frame.controller === previewState.face).at(-1).options.playing,
    true, 'preview speech begins after its idle introduction');
  assert(preview.physicsFrames.slice(60).some(pose => pose.ParamMouthOpenY > .05), 'the silent demonstration visibly exercises lip sync');
  assert.equal(preview.read('audioPlaying'), false);
  assert.equal(preview.read('currentAudioItem'), null);
  assert.equal(preview.read('JSON.stringify(avatarSpeech)'), initialPreviewState.speech);
  assert(liveFace.clock > initialPreviewState.faceClock + 2 && liveEyes.clock > initialPreviewState.gazeClock + 2
    && liveMotion.time > initialPreviewState.motionTime + 2, 'the real controllers keep advancing their idle clocks behind the preview');
  const liveFaceFrames = preview.faceRecords.filter(frame => frame.controller === liveFace);
  const liveEyeFrames = preview.gazeFrames.filter(frame => frame.controller === liveEyes);
  const liveBodyFrames = preview.movementFrames.filter(frame => frame.controller === liveMotion);
  assert.equal(liveFaceFrames.length, 135, 'the real face is updated once on every preview frame');
  assert.equal(liveEyeFrames.length, 135);
  assert.equal(liveBodyFrames.length, 135);
  assert(liveFaceFrames.every(frame => !frame.options.playing && frame.options.level === 0 && frame.result.speechEnergy === 0),
    'synthetic speech never enters the real face or its energy output');
  assert(liveEyeFrames.every(frame => !frame.options.speaking), 'the real gaze keeps its actual idle mode');
  assert(liveBodyFrames.every(frame => !frame.options.speaking && frame.options.speechEnergy === 0),
    'synthetic speech energy never reaches the real body controller');
  assert.equal(preview.faceEvents.filter(event => event.controller === liveFace && event.type === 'begin').length, 0);
  assert(preview.movementFrames.some(frame => frame.controller === previewState.motion
    && frame.options.speaking && frame.options.speechEnergy > .1), 'only the independent preview body receives synthetic voiced energy');
  assert.equal(JSON.stringify(preview.sent), initialPreviewState.sent, 'preview never submits chat, memory, audio or playback messages');
  assert.equal(preview.fetches.length, initialPreviewState.fetches, 'preview has no fetch/model request after startup');
  assert.equal(preview.audio.playCount, initialPreviewState.plays, 'preview never starts the audio element');
  assert.equal(preview.element('events').children.length, initialPreviewState.timeline);
  assert.equal(preview.analyserSamples, 0, 'preview does not sample or initialize live audio analysis');
  for (let i = 0; i < 405; i++) preview.frame();
  assert.equal(preview.read('Boolean(avatarPreview)'), true);
  assert.equal(preview.faceRecords.filter(frame => frame.controller === previewState.face).at(-1).options.playing,
    false, 'the final two preview seconds return to idle');
  assert.equal(preview.physicsFrames.at(-1).ParamMouthOpenY, 0);
  for (let i = 0; i < 86; i++) preview.frame();
  assert.equal(preview.read('avatarPreview'), null, 'the demonstration automatically releases after ten seconds');
  assert.equal(preview.read('avatarPreviewTimer'), null);
  assert.equal(preview.element('avatar-preview').attributes['aria-pressed'], 'false');
  preview.element('avatar-preview').onclick(); preview.frame();
  preview.element('avatar-preview').onclick();
  assert.equal(preview.read('avatarPreview'), null, 'the same button stops the local demonstration immediately');
  preview.element('avatar-preview').onclick();
  preview.tick(10000);
  assert.equal(preview.read('avatarPreview'), null, 'the timeout also releases a preview when render frames are suspended');
  assert.equal(JSON.stringify(preview.sent), initialPreviewState.sent);
  assert.equal(preview.audio.playCount, initialPreviewState.plays);
  console.log('PASS: independent silent preview phases, no external side effects, manual stop and ten-second cleanup');

  const previewCues = await fixture();
  previewCues.element('avatar-preview').onclick();
  previewCues.read('avatarPreview.eyes.frame=()=>({x:0,y:0,headX:0,headY:0,blinkRequest:true})');
  const cueLiveFace = previewCues.read('faceMotion'), cuePreviewFace = previewCues.read('avatarPreview.face');
  previewCues.frame(); previewCues.frame();
  assert.equal(previewCues.faceRecords.filter(frame => frame.controller === cuePreviewFace).at(-1).options.gazeBlink, true);
  assert(previewCues.faceRecords.filter(frame => frame.controller === cueLiveFace).every(frame => !frame.options.gazeBlink),
    'preview gaze blink requests cannot leak into the real face');
  previewCues.element('avatar-preview').onclick(); previewCues.frame();
  assert.equal(previewCues.faceRecords.at(-1).options.gazeBlink, false, 'stopping preview does not replay its last blink cue');
  const returningIdle = await fixture();
  returningIdle.queue({emotion:'开心'}); returningIdle.audio.fire('playing');
  for (let i = 0; i < 90; i++) {returningIdle.audio.currentTime += .016; returningIdle.frame();}
  assert(returningIdle.physicsFrames.at(-1).ParamMouthForm > .75, 'the real face first reaches a distinct happy speaking pose');
  returningIdle.audio.fire('ended');
  returningIdle.element('avatar-preview').onclick();
  assert.equal(returningIdle.read('Boolean(avatarPreview)'), true);
  for (let i = 0; i < 250; i++) returningIdle.frame();
  assert.equal(returningIdle.read('faceMotion.active'), false, 'the real completed speech lifecycle finishes while preview is visible');
  assert(returningIdle.read('bodyMotion.speechEnergy') < .02, 'old real speech energy decays instead of being replenished by preview speech');
  returningIdle.element('avatar-preview').onclick();
  const returnedPose = returningIdle.frame();
  assert.equal(returnedPose.ParamMouthOpenY, 0);
  assert(returnedPose.ParamMouthForm > .58 && returnedPose.ParamMouthForm < .7,
    'preview exit shows the current resting smile rather than reviving the old happy pose');
  assert.equal(returningIdle.movementFrames.at(-1).options.speechEnergy, 0);
  console.log('PASS: live idle progression, isolated preview blink/body energy and no stale expression on preview exit');

  const loading = await fixture({deferModel:true});
  loading.element('avatar-preview').onclick();
  assert.equal(loading.read('avatarPreview'), null, 'preview cannot start while the model is loading');
  assert.match(loading.element('avatar-preview-status').textContent, /加载/);
  await loading.resolveModel();
  loading.element('avatar-preview').onclick();
  assert.equal(loading.read('Boolean(avatarPreview)'), true, 'the same control becomes usable after model readiness');
  loading.element('avatar-preview').onclick();
  const busyStates = [
    'audioPlaying=true', 'currentAudioItem={id:42}', 'presence.listen(true)', 'presence.thinking=true',
    'generationActive=true', 'currentExpression="弯眼"', 'voiceActive=true', 'voiceStarting=true',
    'synthesisTurns.add(42)', 'window.songDeck.active=true',
  ];
  for (const state of busyStates) {
    const busy = await fixture();
    busy.element('avatar-preview').onclick(); busy.frame();
    assert.equal(busy.read('Boolean(avatarPreview)'), true);
    busy.read(state); busy.frame();
    assert.equal(busy.read('avatarPreview'), null, `${state} automatically takes priority over the local preview`);
    assert.equal(busy.read('avatarPreviewTimer'), null);
    busy.element('avatar-preview').onclick();
    assert.equal(busy.read('avatarPreview'), null, `${state} also refuses a new preview while interaction is active`);
  }
  const liveArrival = await fixture();
  liveArrival.element('avatar-preview').onclick();
  for (let i = 0; i < 135; i++) liveArrival.frame();
  assert(liveArrival.read('avatarPreview.motion.speechEnergy') > .1, 'real handoff is tested during voiced preview motion');
  liveArrival.queue({turn:42, emotion:'开心'}); liveArrival.frame();
  assert.equal(liveArrival.read('avatarPreview'), null, 'real queued audio terminates preview before actual playback');
  assert.equal(liveArrival.movementFrames.at(-1).options.speechEnergy, 0);
  assert.equal(liveArrival.read('bodyMotion.speechEnergy'), 0, 'the waiting real reply never inherits the preview energy envelope');
  assert.equal(liveArrival.physicsFrames.at(-1).ParamMouthOpenY, 0);
  liveArrival.audio.fire('playing'); liveArrival.audio.currentTime += .016; liveArrival.frame();
  assert.equal(liveArrival.read('avatarSpeech.turnId'), 42);
  assert.equal(liveArrival.faceInputs.at(-1).playing, true, 'the actual audio lifecycle continues normally after taking priority');
  const broadcastPreview = await fixture();
  broadcastPreview.context.document.documentElement.classList.add('broadcast-display');
  broadcastPreview.element('avatar-preview').onclick();
  assert.equal(broadcastPreview.read('avatarPreview'), null, 'the broadcast view cannot start this console-only preview');
  console.log('PASS: preview model readiness and automatic handoff to every real interaction state');
}

if (require.main === module) main().catch(error => {console.error(error); process.exitCode = 1;});
module.exports = {fixture};
