const assert=require('node:assert/strict');
const Player=require('../brain/static/drawing-player.js');
let lines=[],fills=0,last;
const ctx={fillRect(){fills++;},beginPath(){},moveTo(x,y){last=[x,y];},lineTo(x,y){lines.push([last,[x,y]]);},stroke(){},arc(){},fill(){}};
const p=new Player({getContext:()=>ctx},{width:1000,height:750,background:'#ffffff',strokes:[
 {phase:'轮廓',color:'#000000',width:2,duration:1000,points:[[0,0,.5],[100,0,1]]},
 {phase:'细节',color:'#ffffff',width:4,duration:1000,points:[[100,0,1],[100,100,1]]}]});
p.seek(500);assert.deepEqual(lines.at(-1),[[0,0],[50,0]]);assert.equal(p.state().phase,'轮廓');
p.seek(750);assert.deepEqual(lines.at(-1),[[50,0],[75,0]]);
p.seek(1500);assert.deepEqual(lines.at(-1),[[100,0],[100,50]]);assert.equal(p.state().phase,'细节');
p.seek(2000);assert.equal(p.state().done,true);const count=lines.length;p.seek(3000);assert.equal(lines.length,count);
p.seek(250);assert.equal(fills,2);assert.deepEqual(lines.at(-1),[[0,0],[25,0]]);assert.equal(p.state().done,false);
console.log('PASS: partial strokes, incremental drawing, stage changes, completion and rewind');
