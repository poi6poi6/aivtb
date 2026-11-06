// Real Cubism geometry regression. This does not render a browser or simulate physics.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const crypto=require('node:crypto');
const Face=require('../brain/static/face.js');
const Gaze=require('../brain/static/gaze.js');

const root=path.resolve(__dirname,'..');
const vendorPath=path.join(root,'brain/static/vendor/live2dcubismcore.min.js');
const mocPath=path.join(root,'skin/avatar/avatar.moc3');
if (!fs.existsSync(mocPath)) {
  console.log('SKIP: install a local avatar to run the model geometry test');
  process.exit(0);
}
const indexPath=path.join(root,'brain/static/index.html');
const outputPath=path.join(root,'data/validation/bold-performance-model-20260929.json');
const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
const round=value=>Number(value.toFixed(6));
function seeded(seed){return ()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};}

async function loadCore(){
  // This pinned vendor embeds its compiled runtime and initializes through a
  // local Promise chain. A loop turn drains that chain without arbitrary sleeps
  // or a second Core instance. No fetch/browser/network is provided.
  const context=vm.createContext({console,require,process,Buffer,
    setTimeout,clearTimeout,__dirname:path.dirname(vendorPath),__filename:vendorPath});
  vm.runInContext(fs.readFileSync(vendorPath,'utf8'),context,{filename:vendorPath});
  await new Promise(resolve=>setImmediate(resolve));
  assert(context.Live2DCubismCore?.Model,'real Cubism Core must be available');
  return context.Live2DCubismCore;
}

(async()=>{
  const started=Date.now(),coreApi=await loadCore();
  const bytes=fs.readFileSync(mocPath);
  const moc=coreApi.Moc.fromArrayBuffer(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength));
  assert(moc,'the actual model file must load');
  const model=coreApi.Model.fromMoc(moc);
  assert(model,'the actual model must instantiate');
  try{
    const parameters=model.parameters,drawables=model.drawables;
    const parameterIndex=new Map(Array.from(parameters.ids,(id,index)=>[id,index]));
    const indexOf=id=>{assert(parameterIndex.has(id),`native parameter ${id} must exist`);return parameterIndex.get(id);};
    const adapter={
      _model:model,
      getParameterMinimumValue:index=>parameters.minimumValues[index],
      getParameterMaximumValue:index=>parameters.maximumValues[index],
      getParameterDefaultValue:index=>parameters.defaultValues[index],
      getParameterValueById:id=>parameters.values[indexOf(id)],
      setParameterValueById:(id,value)=>{
        const i=indexOf(id);
        parameters.values[i]=clamp(value,parameters.minimumValues[i],parameters.maximumValues[i]);
      },
    };
    // Execute the production mapping verbatim. The adapter writes native Core
    // memory; values and deformed vertex positions are not mock responses.
    const html=fs.readFileSync(indexPath,'utf8');
    const from=html.indexOf('const avatarParameters ='),to=html.indexOf('function applyAvatarParts');
    assert(from>=0&&to>from,'production mapping source must remain extractable');
    const mappingSource=html.slice(from,to);
    const {applyAvatarFace,applyPupilGaze}=new Function('clamp',mappingSource+
      '\nreturn {applyAvatarFace,applyPupilGaze};')(clamp);
    const reset=()=>parameters.values.set(parameters.defaultValues);
    const snapshot=(indices)=>indices.map(i=>Float32Array.from(drawables.vertexPositions[i]));
    const all=Array.from({length:drawables.count},(_,i)=>i);
    const ppu=model.canvasinfo.PixelsPerUnit;
    const viewport={width:575,height:480,view:'half',zoom:2.15};
    const screenScale=Math.min(viewport.width/model.canvasinfo.CanvasWidth,
      viewport.height/model.canvasinfo.CanvasHeight)*viewport.zoom;
    function displacement(indices,base){
      return indices.map((index,k)=>{
        const positions=drawables.vertexPositions[index],before=base[k];
        let max=0,dx=0,dy=0;
        for(let n=0;n<positions.length;n+=2){
          const x=(positions[n]-before[n])*ppu,y=(positions[n+1]-before[n+1])*ppu;
          max=Math.max(max,Math.hypot(x,y));dx+=x;dy+=y;
        }
        const vertices=positions.length/2;
        return {index,id:drawables.ids[index],opacity:drawables.opacities[index],
          maxNativePx:max,maxScreenPx:max*screenScale,
          centroidXScreenPx:dx/vertices*screenScale,centroidYScreenPx:dy/vertices*screenScale};
      });
    }
    const maximum=changes=>Math.max(0,...changes.filter(x=>x.opacity>.5).map(x=>x.maxScreenPx));
    function bounds(index){
      const positions=drawables.vertexPositions[index];
      let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity,centerX=0,centerY=0;
      for(let n=0;n<positions.length;n+=2){
        minX=Math.min(minX,positions[n]);maxX=Math.max(maxX,positions[n]);
        minY=Math.min(minY,positions[n+1]);maxY=Math.max(maxY,positions[n+1]);
        centerX+=positions[n];centerY+=positions[n+1];
      }
      const scale=ppu*screenScale,count=positions.length/2;
      return {width:(maxX-minX)*scale,height:(maxY-minY)*scale,
        centerX:centerX/count*scale,centerY:centerY/count*scale};
    }
    reset();model.update();const neutral=snapshot(all);
    // Discover mouth meshes from their response to the native mouth channel,
    // excluding floating-point noise and unrelated blinking/eyelid geometry.
    adapter.setParameterValueById('ParamMouthForm',1);model.update();
    const mouthIndices=displacement(all,neutral).filter(x=>x.opacity>.5&&x.maxNativePx>.1).map(x=>x.index);
    assert(mouthIndices.length>=4,'native mouth form must deform visible mouth meshes');
    reset();model.update();const mouthBaseline=snapshot(mouthIndices);
    adapter.setParameterValueById('Param',1);model.update();
    const pupilIndices=displacement(all,neutral).filter(x=>x.opacity>.5&&x.maxNativePx>.1).map(x=>x.index);
    assert(pupilIndices.length>=2,'native pupil-depth channel must deform visible eye meshes');
    reset();model.update();const pupilBaseline=snapshot(pupilIndices);
    applyPupilGaze(adapter,{x:1,y:0},0);model.update();
    const primaryPupil=displacement(pupilIndices,pupilBaseline)
      .sort((a,b)=>Math.abs(b.centroidXScreenPx)-Math.abs(a.centroidXScreenPx))[0].index;

    reset();adapter.setParameterValueById('ParamMouthOpenY',.5);model.update();
    const openMouthBaseline=snapshot(mouthIndices);
    adapter.setParameterValueById('MouthPuckerWiden',-.6);model.update();
    const primaryMouth=displacement(mouthIndices,openMouthBaseline)
      .sort((a,b)=>b.maxScreenPx-a.maxScreenPx)[0].index;
    reset();adapter.setParameterValueById('ParamMouthOpenY',.5);model.update();
    const openMouthBounds=bounds(primaryMouth),nativeChannels=[];
    for(const id of ['MouthFunnel','MouthShrug','MouthX','MouthPuckerWiden','MouthPressLipOpen']){
      const index=indexOf(id),samples=[];
      for(const value of [-.6,-.3,.3,.6]){
        reset();adapter.setParameterValueById('ParamMouthOpenY',.5);adapter.setParameterValueById(id,value);model.update();
        const shape=bounds(primaryMouth);
        samples.push({requested:value,actual:round(adapter.getParameterValueById(id)),
          maxScreenPx:round(maximum(displacement(mouthIndices,openMouthBaseline))),
          boundsDelta:Object.fromEntries(Object.entries(shape).map(([key,current])=>[key,round(current-openMouthBounds[key])]))});
      }
      nativeChannels.push({id,min:parameters.minimumValues[index],max:parameters.maximumValues[index],default:parameters.defaultValues[index],samples});
    }

    const idleFace=new Face({random:seeded(101)});
    let idle;
    for(let time=0;time<=6000;time+=16)idle=idleFace.frame(time,{intensity:.8,eyeIntensity:.75});
    reset();applyAvatarFace(adapter,idle);model.update();
    const idleMouthParameter=adapter.getParameterValueById('ParamMouthForm');
    const idleSmilePx=maximum(displacement(mouthIndices,mouthBaseline));
    // Quantify the former uncalibrated mapping against the same real meshes.
    reset();adapter.setParameterValueById('ParamMouthForm',idle.mouthForm);model.update();
    const beforeSmilePx=maximum(displacement(mouthIndices,mouthBaseline));
    const mouthSamples=[];
    for(const value of [.052,.2,.3,.4,1]){
      reset();adapter.setParameterValueById('ParamMouthForm',value);model.update();
      mouthSamples.push({nativeParameter:value,maxScreenPx:round(maximum(displacement(mouthIndices,mouthBaseline)))});
    }
    // Disabled styling must return to the genuine native closed-mouth baseline.
    let disabled;
    for(let time=6016;time<=12000;time+=16)disabled=idleFace.frame(time,{intensity:0,eyeIntensity:0});
    reset();applyAvatarFace(adapter,disabled);model.update();
    const disabledMouthPx=maximum(displacement(mouthIndices,mouthBaseline));
    const disabledMouthOpen=adapter.getParameterValueById('ParamMouthOpenY');
    const explicitFace=new Face({random:seeded(102)});
    explicitFace.begin({turnId:'explicit',emotion:'开心',explicitFace:true},0);
    let explicit;
    for(let time=0;time<=3000;time+=16)explicit=explicitFace.frame(time,{intensity:.8,eyeIntensity:.75});
    reset();adapter.setParameterValueById('ParamMouthForm',-.4);applyAvatarFace(adapter,explicit);
    const explicitMouthParameter=adapter.getParameterValueById('ParamMouthForm');
    const explicitShapeDefaults={MouthPuckerWiden:-.3,MouthShrug:.2,MouthX:-.1};
    reset();for(const [id,value] of Object.entries(explicitShapeDefaults))adapter.setParameterValueById(id,value);
    applyAvatarFace(adapter,explicit);
    const explicitShapes=Object.fromEntries(Object.keys(explicitShapeDefaults).map(id=>[id,adapter.getParameterValueById(id)]));

    // The same RMS and media timing produce real controller poses. Fixing only
    // the jaw for this comparison isolates mouth shape from ordinary opening;
    // retain the original jaw outputs and their real mesh bounds in the report.
    const articulation=[];
    let silenceMaxDelta=0,silenceMaxOpen=0;
    for(const brightness of [.05,.5,.95]){
      const controller=new Face({random:seeded(201)});
      controller.begin({turnId:'shape',emotion:'平静'},0);
      let frame;
      for(let time=0;time<=3000;time+=16)frame=controller.frame(time,{
        playing:true,level:.1,brightness,mediaTime:time/1000,intensity:.8,eyeIntensity:.75});
      for(const key of ['mouthOpen','mouthWidth','mouthRound','mouthShift'])
        assert(Number.isFinite(frame[key]),`production Face must provide finite ${key}`);
      reset();applyAvatarFace(adapter,frame);model.update();
      const actualJaw=adapter.getParameterValueById('ParamMouthOpenY'),actualBounds=bounds(primaryMouth);
      assert(Math.abs(actualJaw-frame.mouthOpen)<1e-6,'report the real jaw output instead of replacing it with the comparison jaw');
      reset();applyAvatarFace(adapter,{...frame,mouthOpen:.5});model.update();
      const nativeShape=Object.fromEntries(['MouthPuckerWiden','MouthShrug','MouthX'].map(id=>[id,round(adapter.getParameterValueById(id))]));
      articulation.push({brightness,level:.1,controller:{mouthOpen:round(frame.mouthOpen),mouthForm:round(frame.mouthForm),
        mouthWidth:round(frame.mouthWidth),mouthRound:round(frame.mouthRound),mouthShift:round(frame.mouthShift)},
        actualJaw:round(actualJaw),actualBounds:Object.fromEntries(Object.entries(actualBounds).map(([k,v])=>[k,round(v)])),
        nativeShape,fixedJaw:.5,fixedJawBounds:bounds(primaryMouth),vertices:snapshot(mouthIndices)});
      let silentBaseline;
      for(let time=3008;time<=7200;time+=16){
        const silent=controller.frame(time,{playing:true,level:0,brightness:time%32 ? .05 : .95,
          mediaTime:time/1000,intensity:.8,eyeIntensity:.75});
        if(time<6200)continue;
        reset();applyAvatarFace(adapter,silent);model.update();
        if(!silentBaseline)silentBaseline=snapshot(mouthIndices);
        silenceMaxDelta=Math.max(silenceMaxDelta,maximum(displacement(mouthIndices,silentBaseline)));
        silenceMaxOpen=Math.max(silenceMaxOpen,adapter.getParameterValueById('ParamMouthOpenY'));
      }
    }
    const sameJawWidthRange=Math.max(...articulation.map(x=>x.fixedJawBounds.width))-
      Math.min(...articulation.map(x=>x.fixedJawBounds.width));
    const sameJawPairDifferences=[];
    for(let a=0;a<articulation.length;a++)for(let b=a+1;b<articulation.length;b++){
      let max=0;
      for(let mesh=0;mesh<mouthIndices.length;mesh++){
        const first=articulation[a].vertices[mesh],second=articulation[b].vertices[mesh];
        for(let vertex=0;vertex<first.length;vertex+=2)
          max=Math.max(max,Math.hypot(first[vertex]-second[vertex],first[vertex+1]-second[vertex+1])*ppu*screenScale);
      }
      sameJawPairDifferences.push({brightnessA:articulation[a].brightness,brightnessB:articulation[b].brightness,maxScreenPx:round(max)});
    }

    const gaze=new Gaze({random:seeded(42)}),speakingFace=new Face({random:seeded(103)});
    // The same spoken intent drives both strength comparisons: briefly look
    // aside while reflecting, then re-engage. Ordinary speech stays on camera.
    const gazeFocus=time=>time>=1000&&time<2000?'reflect':'engage';
    speakingFace.begin({turnId:'spoken',emotion:'平静'},0);
    let minX=Infinity,maxX=-Infinity,minParameter=Infinity,maxParameter=-Infinity;
    let previousGazeBlink=false,blinkRequests=0,headRequests=0,visibleSamples=0;
    let reflectPeakPx=0,returnPeakPx=0,beforeReflectPeakPx=0,reflectSamples=0,returnSamples=0;
    for(let time=0;time<5000;time+=16){
      const face=speakingFace.frame(time,{playing:true,level:.08,mediaTime:time/1000,
        intensity:.8,eyeIntensity:.75,gazeBlink:previousGazeBlink});
      reset();applyAvatarFace(adapter,face);model.update();
      const eyeOpen=Math.min(adapter.getParameterValueById('ParamEyeLOpen'),adapter.getParameterValueById('ParamEyeROpen'));
      const before=snapshot([primaryPupil]);
      const look=gaze.frame(time,{speaking:true,intensity:.75,eyeOpen,focus:gazeFocus(time)});
      previousGazeBlink=look.blinkRequest;
      if(look.blinkRequest)blinkRequests++;
      if(look.headX!==0||look.headY!==0)headRequests++;
      applyPupilGaze(adapter,look,0);model.update();
      // Compare the same facial pose before/after gaze; lid movement is not
      // counted as pupil motion. Closed/hidden eyes are not visibility samples.
      if(eyeOpen>.65&&drawables.opacities[primaryPupil]>.5){
        const shift=displacement([primaryPupil],before)[0].centroidXScreenPx;
        minX=Math.min(minX,shift);maxX=Math.max(maxX,shift);
        if(time<1000)beforeReflectPeakPx=Math.max(beforeReflectPeakPx,Math.abs(shift));
        if(time>=1000&&time<2000){reflectPeakPx=Math.max(reflectPeakPx,Math.abs(shift));reflectSamples++;}
        if(time>=3200){returnPeakPx=Math.max(returnPeakPx,Math.abs(shift));returnSamples++;}
        const value=adapter.getParameterValueById('ParamEyeBallX');
        minParameter=Math.min(minParameter,value);maxParameter=Math.max(maxParameter,value);
        visibleSamples++;
      }
    }
    // Measure the new global dial against the same native geometry. Eyelid
    // comparisons use matched, pre-blink frames so a blink cannot fake a gain.
    reset();adapter.setParameterValueById('ParamEyeLOpen',.5);adapter.setParameterValueById('ParamEyeROpen',.5);model.update();
    const eyeIndices=displacement(all,neutral).filter(x=>x.opacity>.5&&x.maxNativePx>.1).map(x=>x.index);
    assert(eyeIndices.length>=6,'the native eyelid controls must affect actual visible eye meshes');
    reset();model.update();const eyeBaseline=snapshot(eyeIndices);
    adapter.setParameterValueById('ParamBrowLForm',1);adapter.setParameterValueById('ParamBrowRForm',1);model.update();
    const directBrowMeshes=displacement(all,neutral).filter(x=>x.opacity>.5&&x.maxNativePx>.01).map(x=>x.id);
    const performanceScenes=[];
    for(const emotion of [null,'开心']){
      let baseEyes;
      for(const performance of [1,1.8]){
        const controller=new Face({random:()=>.5});
        if(emotion)controller.begin({turnId:'performance',emotion},0);
        let frame;
        for(let time=0;time<=1800;time+=16)frame=controller.frame(time,{
          playing:Boolean(emotion),level:emotion?.1:0,brightness:.5,mediaTime:time/1000,
          intensity:.8,eyeIntensity:.75,performance});
        reset();applyAvatarFace(adapter,frame);model.update();
        performanceScenes.push({scene:emotion||'idle',performance,
          mouthMaxScreenPx:round(maximum(displacement(mouthIndices,mouthBaseline))),
          eyelidMaxScreenPx:round(maximum(displacement(eyeIndices,eyeBaseline))),
          eyelidDifferenceFromBasePx:baseEyes?round(maximum(displacement(eyeIndices,baseEyes))):0,
          native:{mouthForm:round(adapter.getParameterValueById('ParamMouthForm')),
            mouthOpen:round(adapter.getParameterValueById('ParamMouthOpenY')),
            eyeOpenL:round(adapter.getParameterValueById('ParamEyeLOpen')),
            eyeSmileL:round(adapter.getParameterValueById('ParamEyeLSmile')),
            browL:round(adapter.getParameterValueById('ParamBrowLForm')),
            browR:round(adapter.getParameterValueById('ParamBrowRForm'))}});
        if(performance===1)baseEyes=snapshot(eyeIndices);
      }
    }
    const boldShapeWidths=[];
    for(const brightness of [.05,.5,.95]){
      const controller=new Face({random:seeded(201)});
      controller.begin({turnId:'bold-shape',emotion:'平静'},0);
      let frame;
      for(let time=0;time<=3000;time+=16)frame=controller.frame(time,{
        playing:true,level:.1,brightness,mediaTime:time/1000,intensity:.8,eyeIntensity:.75,performance:1.8});
      reset();applyAvatarFace(adapter,{...frame,mouthOpen:.5});model.update();
      boldShapeWidths.push({brightness,productionJaw:round(frame.mouthOpen),fixedJaw:.5,
        mouthWidth:round(bounds(primaryMouth).width),nativeShape:{
          width:round(adapter.getParameterValueById('MouthPuckerWiden')),
          round:round(adapter.getParameterValueById('MouthShrug')),
          shift:round(adapter.getParameterValueById('MouthX'))}});
    }
    const boldWidthRange=Math.max(...boldShapeWidths.map(x=>x.mouthWidth))-Math.min(...boldShapeWidths.map(x=>x.mouthWidth));
    const boldGaze=new Gaze({random:seeded(42)}),boldFace=new Face({random:seeded(103)});
    boldFace.begin({turnId:'bold-spoken',emotion:'平静'},0);
    let boldMinX=Infinity,boldMaxX=-Infinity,boldEyeSamples=0;
    let boldPreviousGazeBlink=false,boldReflectPeakPx=0,boldReturnPeakPx=0;
    for(let time=0;time<5000;time+=16){
      const face=boldFace.frame(time,{playing:true,level:.08,brightness:.5,mediaTime:time/1000,
        intensity:.8,eyeIntensity:.75,performance:1.8,gazeBlink:boldPreviousGazeBlink});
      reset();applyAvatarFace(adapter,face);model.update();
      const eyeOpen=Math.min(adapter.getParameterValueById('ParamEyeLOpen'),adapter.getParameterValueById('ParamEyeROpen'));
      const before=snapshot([primaryPupil]);
      const look=boldGaze.frame(time,{speaking:true,intensity:.75,performance:1.8,eyeOpen,focus:gazeFocus(time)});
      boldPreviousGazeBlink=look.blinkRequest;
      applyPupilGaze(adapter,look,0);model.update();
      if(eyeOpen>.65&&drawables.opacities[primaryPupil]>.5){
        const shift=displacement([primaryPupil],before)[0].centroidXScreenPx;
        boldMinX=Math.min(boldMinX,shift);boldMaxX=Math.max(boldMaxX,shift);boldEyeSamples++;
        if(time>=1000&&time<2000)boldReflectPeakPx=Math.max(boldReflectPeakPx,Math.abs(shift));
        if(time>=3200)boldReturnPeakPx=Math.max(boldReturnPeakPx,Math.abs(shift));
      }
    }
    const report={
      description:'Real bundled Cubism Core + model vertex regression; screen pixels are a fit-based estimate, not visual acceptance.',
      model:{path:path.relative(root,mocPath),sha256:crypto.createHash('sha256').update(bytes).digest('hex'),
        parameterCount:parameters.count,drawableCount:drawables.count,
        canvasWidth:model.canvasinfo.CanvasWidth,canvasHeight:model.canvasinfo.CanvasHeight,pixelsPerUnit:ppu},
      productionMappingSha256:crypto.createHash('sha256').update(mappingSource).digest('hex'),
      viewport,screenScale:round(screenScale),
      smile:{faceIntensity:.8,controllerMouthForm:round(idle.mouthForm),mappedNativeMouthForm:round(idleMouthParameter),
        previousUncalibratedMaxScreenPx:round(beforeSmilePx),currentMaxScreenPx:round(idleSmilePx),
        mouthMeshes:mouthIndices.map(i=>drawables.ids[i]),nativeParameterSamples:mouthSamples,
        disabledMaxScreenPx:round(disabledMouthPx),disabledMouthOpen:disabledMouthOpen,
        explicitNativeMouthForm:round(explicitMouthParameter)},
      speechGaze:{seconds:5,intensity:.75,primaryPupilMesh:drawables.ids[primaryPupil],
        focusTimeline:[{fromSeconds:0,focus:'engage'},{fromSeconds:1,focus:'reflect'},{fromSeconds:2,focus:'engage'}],
        nativeXMin:round(minParameter),nativeXMax:round(maxParameter),
        screenXMin:round(minX),screenXMax:round(maxX),screenXRange:round(maxX-minX),
        beforeReflectMaxAbsPx:round(beforeReflectPeakPx),reflectMaxAbsPx:round(reflectPeakPx),
        settledReturnMaxAbsPx:round(returnPeakPx),reflectSamples,returnSamples,
        visibleSamples,headRequests,blinkRequests},
      mouthChannelSurvey:{fixedJaw:.5,primaryMouthMesh:drawables.ids[primaryMouth],nativeChannels},
      articulation:{sameJawWidthRange:round(sameJawWidthRange),sameJawPairDifferences,
        shapes:articulation.map(({vertices,...shape})=>({...shape,
          fixedJawBounds:Object.fromEntries(Object.entries(shape.fixedJawBounds).map(([key,value])=>[key,round(value)]))})),
        silenceMaxMeshDelta:round(silenceMaxDelta),silenceMaxMouthOpen:round(silenceMaxOpen),
        explicitShapes:Object.fromEntries(Object.entries(explicitShapes).map(([id,value])=>[id,round(value)]))},
      performanceComparison:{base:1,bold:1.8,faceScenes:performanceScenes,
        baselineMouthWidthRange:round(sameJawWidthRange),boldMouthWidthRange:round(boldWidthRange),boldShapes:boldShapeWidths,
        baselinePupilRange:round(maxX-minX),boldPupilMin:round(boldMinX),boldPupilMax:round(boldMaxX),
        boldPupilRange:round(boldMaxX-boldMinX),boldEyeSamples,directBrowMeshes,
        baselineReflectMaxAbsPx:round(reflectPeakPx),boldReflectMaxAbsPx:round(boldReflectPeakPx),
        baselineReturnMaxAbsPx:round(returnPeakPx),boldReturnMaxAbsPx:round(boldReturnPeakPx),
        browMeasurement:directBrowMeshes.length?'Direct native brow meshes exist':'Brow form is a physics input: report native input gain only; no brow displacement claim without physics'},
      limitations:['No complete Cubism physics simulation','No browser render or screenshot acceptance',
        'Fixed 575x480 half-view layout estimate; other viewport sizes change pixel scale',
        'Pupil measurement isolates gaze from face/lid motion and does not include head compensation',
        'Articulation shape comparison fixes jaw at .5; the original production jaw outputs and mesh bounds are reported separately',
        'Brightness is a spectral shape cue, not phoneme recognition',
        'Brow-form inputs require Cubism physics in this model; their input increase is not a measured brow displacement'],
      elapsedMs:Date.now()-started,
    };
    fs.mkdirSync(path.dirname(outputPath),{recursive:true});
    fs.writeFileSync(outputPath,JSON.stringify(report,null,2)+'\n');
    assert(idleSmilePx>1.15&&idleSmilePx<1.6,'default smile must move real mouth geometry by a clearly visible but restrained estimated amount');
    assert(disabledMouthPx<.001&&disabledMouthOpen===0,'zero intensity must preserve the native closed-mouth baseline');
    assert(Math.abs(explicitMouthParameter+.4)<1e-6,'automatic styling must not override an explicit mouth expression');
    for(const [id,value] of Object.entries(explicitShapeDefaults))
      assert(Math.abs(explicitShapes[id]-value)<1e-6,`explicit ${id} must retain ownership of its shape`);
    assert(sameJawWidthRange>2,'different voice spectra must visibly change mouth width even at an identical jaw opening');
    assert(sameJawPairDifferences.every(pair=>pair.maxScreenPx>.35),'all three voice colors must produce distinct real mouth meshes');
    assert(silenceMaxDelta<.002&&silenceMaxOpen<.001,'silent audio must stop mouth articulation despite varying spectral input');
    assert(visibleSamples>200,'enough open-eye frames must be measured');
    assert(reflectSamples>30&&returnSamples>60,'both the reflection and settled camera return need visible-eye samples');
    assert(reflectPeakPx>1,'semantic reflection must visibly shift the real pupil mesh away from the camera');
    assert(beforeReflectPeakPx<.15&&returnPeakPx<.15,
      'gaze stays near camera before reflection and returns after re-engaging');
    assert(headRequests>5,'semantic reflection supplies a delayed head-follow target, not a deformation claim');
    assert(blinkRequests>0&&blinkRequests<=2,'reflection and re-engagement can coordinate at most two blink requests');
    const baseRest=performanceScenes.find(x=>x.scene==='idle'&&x.performance===1);
    const boldRest=performanceScenes.find(x=>x.scene==='idle'&&x.performance===1.8);
    const baseHappy=performanceScenes.find(x=>x.scene==='开心'&&x.performance===1);
    const boldHappy=performanceScenes.find(x=>x.scene==='开心'&&x.performance===1.8);
    assert(boldRest.mouthMaxScreenPx>baseRest.mouthMaxScreenPx*1.2,'bold rest must visibly increase the real smile deformation');
    assert(boldRest.eyelidDifferenceFromBasePx>.6,'bold rest must visibly change actual eyelids rather than only a smile input');
    assert(boldHappy.eyelidDifferenceFromBasePx>1.3,'bold smiling speech must visibly change actual eye geometry');
    assert(Math.abs(boldHappy.native.browL)>Math.abs(baseHappy.native.browL)*1.5,
      'bold speech increases the real brow physics input without pretending to measure its physics output');
    assert(boldWidthRange>sameJawWidthRange*1.4,'bold speaking must noticeably widen the range of real mouth shapes');
    assert(boldEyeSamples>200&&boldReflectPeakPx>reflectPeakPx*1.4&&boldMaxX-boldMinX>(maxX-minX)*1.4,
      'the same semantic reflection must visibly enlarge actual pupil travel at higher strength');
    assert(boldReturnPeakPx<.25,'higher strength still returns gaze to the camera');
    console.log(`MEASURE: real mouth ${beforeSmilePx.toFixed(3)} -> ${idleSmilePx.toFixed(3)} estimated screen px; pupil X ${minX.toFixed(3)}..${maxX.toFixed(3)}px, range ${(maxX-minX).toFixed(3)}px`);
    console.log(`MEASURE: identical jaw=0.5, voice-color mouth width range ${sameJawWidthRange.toFixed(3)}px; silence delta ${silenceMaxDelta.toFixed(6)}px`);
    console.log(`MEASURE: performance 1 -> 1.8, pupil range ${(maxX-minX).toFixed(3)} -> ${(boldMaxX-boldMinX).toFixed(3)}px; mouth width range ${sameJawWidthRange.toFixed(3)} -> ${boldWidthRange.toFixed(3)}px; idle eyelid difference ${boldRest.eyelidDifferenceFromBasePx.toFixed(3)}px; smiling-speech eyelid difference ${boldHappy.eyelidDifferenceFromBasePx.toFixed(3)}px`);
    console.log(`MEASURE: shared 1s..2s reflect cue, peak pupil displacement ${reflectPeakPx.toFixed(3)} -> ${boldReflectPeakPx.toFixed(3)}px; settled return ${returnPeakPx.toFixed(3)} -> ${boldReturnPeakPx.toFixed(3)}px`);
    console.log('PASS: production face/gaze mappings deform the actual moc3 meshes; zero intensity and explicit ownership preserved');
    console.log('Report: '+path.relative(root,outputPath));
  }finally{model.release();moc._release();}
})().catch(error=>{console.error(error);process.exitCode=1;});
