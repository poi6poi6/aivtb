// Offline regression: production gestures -> pinned physics -> real head meshes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Gestures = require('../brain/static/gestures.js');

const root = path.resolve(__dirname, '..');
const asset = name => path.join(root, 'skin/avatar', name);
if (!fs.existsSync(asset('avatar.moc3'))) {
  console.log('SKIP: install a local avatar to run the physics test');
  process.exit(0);
}
const vendorPath = path.join(root, 'brain/static/vendor/live2dcubismcore.min.js');
const vendor = fs.readFileSync(path.join(root, 'brain/static/vendor/pixi-live2d-display.min.js'), 'utf8');
function section(start, end) {
  const from = vendor.indexOf(start), to = vendor.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, 'pinned runtime changed; review the extracted native contract');
  return vendor.slice(from, to);
}
// Only rendering is omitted. These are the actual vendor parameter adapter,
// physics solver, internal save/update/load lifecycle and outer render clock.
const Native = new Function(`
  class pe {update() {}}
  const Re = {copyFrom() {return {append() {return {};}};}};
  ${section('class c{', 'class p{')}
  ${section('class D{', 'class F{')}
  ${section('const rt=-1;', 'class Tt{')}
  ${section('class He extends pe{', 'let Ye,qe=')}
  class Host {${section('update(t){this.deltaTime+=t', 'destroy(t){this.emit("destroy")')}}
  return {Core:D, Physics:gt, Internal:He, Host};
`)();

async function main() {
  const context = vm.createContext({console, require, process, Buffer, setTimeout, clearTimeout,
    __dirname:path.dirname(vendorPath), __filename:vendorPath});
  vm.runInContext(fs.readFileSync(vendorPath, 'utf8'), context, {filename:vendorPath});
  await new Promise(resolve => setImmediate(resolve));
  const bytes = fs.readFileSync(asset('avatar.moc3'));
  const moc = context.Live2DCubismCore.Moc.fromArrayBuffer(
    bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  assert(moc, 'the real moc3 must load');
  const model = context.Live2DCubismCore.Model.fromMoc(moc);
  assert(model, 'the real model must instantiate');
  try {
    const physicsJson = JSON.parse(fs.readFileSync(asset('avatar.physics3.json'), 'utf8'));
    const display = JSON.parse(fs.readFileSync(asset('avatar.cdi3.json'), 'utf8'));
    const html = fs.readFileSync(path.join(root, 'brain/static/index.html'), 'utf8');
    const capStatement = html.match(/app\.ticker\.maxFPS\s*=\s*\d+\s*;/)?.[0];
    assert(capStatement, 'the real page must cap its renderer, where Cubism physics runs');
    const app = {ticker:{}};
    new Function('app', capStatement)(app);
    assert.equal(app.ticker.maxFPS, 60);
    const parameters = model.parameters, drawables = model.drawables;
    const nativeIds = new Map(Array.from(parameters.ids, (id, index) => [id, index]));
    for (const id of ['ParamAngleZ', 'ParamAngleSCZ']) assert(nativeIds.has(id));
    const partIndex = label => Array.from(model.parts.ids).indexOf(display.Parts.find(p => p.Name === label)?.Id);
    const descendants = ancestor => Array.from({length:drawables.count}, (_, i) => i).filter(i => {
      if (drawables.opacities[i] <= .5) return false;
      for (let part = drawables.parentPartIndices[i]; part >= 0; part = model.parts.parentIndices[part])
        if (part === ancestor) return true;
      return false;
    });
    parameters.values.set(parameters.defaultValues); model.update();
    const head = descendants(partIndex('头部')), face = descendants(partIndex('面部'));
    assert(head.length > 20 && face.length > 10, 'measure actual head/face parts, not body or hidden meshes');
    const neutral = drawables.vertexPositions.map(vertices => Float32Array.from(vertices));
    // Same half-body viewport estimate as test_avatar_model_ui.js; this is not
    // a screenshot acceptance test. It proves real deformation after physics.
    const scale = model.canvasinfo.PixelsPerUnit * Math.min(
      575 / model.canvasinfo.CanvasWidth, 480 / model.canvasinfo.CanvasHeight) * 2.15;
    function displacement(indices) {
      let maximum = 0;
      for (const i of indices) {
        const current = drawables.vertexPositions[i], before = neutral[i];
        for (let j = 0; j < current.length; j += 2)
          maximum = Math.max(maximum, Math.hypot(current[j] - before[j], current[j + 1] - before[j + 1]) * scale);
      }
      return maximum;
    }
    const renderer = {CONTEXT_UID:0, plugins:{}, gl:{}, batch:{reset() {}}, geometry:{reset() {}},
      shader:{reset() {}}, state:{reset() {}}, texture:{reset() {}},
      framebuffer:{viewport:{x:0,y:0,width:575,height:480}}, globalUniforms:{uniforms:{projectionMatrix:{}}}};

    function run(refreshHz, cap = app.ticker.maxFPS) {
      parameters.values.set(parameters.defaultValues);
      const core = new Native.Core(model), physics = Native.Physics.create(physicsJson);
      const gestures = new Gestures({random:() => .5});
      const outputs = [...new Set(physics._physicsRig.outputs.map(output => output.destination.id))];
      assert(outputs.every(id => nativeIds.has(id)), 'physics may only write registered native outputs');
      let started = false, renders = 0, accumulated = 0, physicsSeconds = 0;
      let sourcePeak = 0, outputPeak = 0, headPeak = 0, facePeak = 0;
      const runtime = Object.create(Native.Internal.prototype);
      Object.assign(runtime, {coreModel:core, physics, motionManager:{update() {return false;}},
        updateFocus() {}, updateTransform() {}, draw() {},
        updateNaturalMovements(deltaMs, now) {
          physicsSeconds += deltaMs / 1000;
          if (now >= 1000 && !started) {gestures.begin({turnId:'preview', actions:['疑惑']}); started = true;}
          const frame = gestures.frame(now, {speaking:started, mediaTime:Math.max(0, (now - 1000) / 1000),
            intensity:.8, performance:1.8, eyeIntensity:.75, faceIntensity:.8});
          for (const axis of ['X', 'Y', 'Z']) {
            core.addParameterValueById('ParamAngle' + axis, frame['head' + axis]);
            core.addParameterValueById('ParamBodyAngle' + axis, frame['body' + axis]);
          }
        },
        emit(event) {
          if (event !== 'beforeModelUpdate') return;
          sourcePeak = Math.max(sourcePeak, Math.abs(core.getParameterValueById('ParamAngleZ')));
          outputPeak = Math.max(outputPeak, Math.abs(core.getParameterValueById('ParamAngleSCZ')));
          for (const id of outputs) {
            const i = nativeIds.get(id), value = parameters.values[i];
            assert(Number.isFinite(value) && value >= parameters.minimumValues[i] - 1e-4
              && value <= parameters.maximumValues[i] + 1e-4, `${id} remains within its native bounds`);
          }
        },
      });
      const host = Object.assign(Object.create(Native.Host.prototype), {
        deltaTime:0, elapsedTime:0, glContextID:0, textures:[], worldTransform:{},
        registerInteraction() {}, internalModel:runtime,
      });
      // The shared model ticker still updates at display cadence. Only render
      // is capped; vendor _render consumes accumulated time and then resets it.
      const delta = 1000 / refreshHz, interval = 1000 / cap;
      for (let tick = 0; tick < refreshHz * 4; tick++) {
        host.update(delta);
        accumulated += delta;
        if (accumulated + 1e-8 < interval) continue;
        accumulated -= Math.floor((accumulated + 1e-8) / interval) * interval;
        host._render(renderer); renders++;
        headPeak = Math.max(headPeak, displacement(head));
        facePeak = Math.max(facePeak, displacement(face));
        assert.equal(core.getParameterValueById('ParamAngleZ'), 0, 'vendor restores inputs after rendering');
      }
      assert(Math.abs(physicsSeconds + host.deltaTime / 1000 - 4) < 1e-6,
        'render limiting preserves elapsed model/physics time');
      assert.equal(Object.keys(core._notExistParameterId).length, 0, 'no virtual parameters are created');
      physics.release();
      return {refreshHz, cap, renders, sourcePeak, outputPeak, headPeak, facePeak};
    }
    const capped = [60, 160, 240].map(hz => run(hz));
    for (const result of capped) {
      assert(Math.abs(result.renders - 240) <= 1, 'high-refresh displays still render about 60 frames per second');
      assert(result.outputPeak > 5 && result.outputPeak / result.sourcePeak > .7,
        'the gesture must reach actual SC head outputs, not only source inputs');
      assert(result.headPeak > 5 && result.facePeak > 3, 'physics must visibly deform actual head and face meshes');
      for (const key of ['sourcePeak', 'outputPeak', 'headPeak', 'facePeak'])
        assert(Math.abs(result[key] / capped[0][key] - 1) < .2, `${key} remains stable across display refresh rates`);
    }
    // Diagnostic only: a vendor upgrade may fix the uncapped solver. Never
    // assert that the old high-FPS bug must keep existing.
    const raw = run(160, 160);
    console.log('MEASURE: ' + JSON.stringify({capped, uncappedDiagnostic:raw}));
    console.log('PASS: capped rendering preserves real time, native physics bounds and visible head deformation at 60/160/240Hz');
  } finally {model.release(); moc._release();}
}

main().catch(error => {console.error(error); process.exitCode = 1;});
