'use strict';
(function(root){
 class DrawingPlayer{
  constructor(canvas,data){
   this.canvas=canvas;this.ctx=canvas.getContext('2d');this.data=data;this.time=0;
   canvas.width=data.width;canvas.height=data.height;
   let start=0;
   this.strokes=data.strokes.map(s=>{
    const distances=[0];for(let i=1;i<s.points.length;i++)distances.push(distances[i-1]+Math.hypot(s.points[i][0]-s.points[i-1][0],s.points[i][1]-s.points[i-1][1]));
    const result={...s,start,end:start+s.duration,distances,length:distances.at(-1)};start=result.end;return result;
   });this.duration=start;this.reset();
  }
  reset(){this.time=0;this.ctx.fillStyle=this.data.background;this.ctx.fillRect(0,0,this.canvas.width,this.canvas.height);}
  portion(s,from,to){
   const c=this.ctx;c.strokeStyle=s.color;c.fillStyle=s.color;c.lineCap='round';c.lineJoin='round';
   if(s.length===0){const p=s.points[0];c.beginPath();c.arc(p[0],p[1],s.width*p[2]/2,0,Math.PI*2);c.fill();return;}
   const low=from*s.length,high=to*s.length;
   for(let i=1;i<s.points.length;i++){
    const a=s.distances[i-1],b=s.distances[i];if(b<=low||a>=high||b===a)continue;
    const p=s.points[i-1],q=s.points[i];const t0=Math.max(0,(low-a)/(b-a)),t1=Math.min(1,(high-a)/(b-a));
    const at=t=>p.map((v,k)=>v+(q[k]-v)*t);const u=at(t0),v=at(t1);
    c.lineWidth=s.width*(u[2]+v[2])/2;c.beginPath();c.moveTo(u[0],u[1]);c.lineTo(v[0],v[1]);c.stroke();
   }
  }
  seek(target){
   target=Math.max(0,Math.min(this.duration,Number(target)||0));if(target<this.time)this.reset();
   for(const s of this.strokes){if(s.end<=this.time||s.start>=target)continue;this.portion(s,Math.max(0,(this.time-s.start)/s.duration),Math.min(1,(target-s.start)/s.duration));}
   this.time=target;return this.state();
  }
  state(){const index=this.strokes.findIndex(s=>s.end>this.time);const done=index<0;return{done,time:this.time,duration:this.duration,progress:this.time/this.duration,index:done?this.strokes.length:index,phase:done?'完成':this.strokes[index].phase,total:this.strokes.length};}
 }
 if(typeof module==='object'&&module.exports)module.exports=DrawingPlayer;else root.DrawingPlayer=DrawingPlayer;
})(globalThis);
