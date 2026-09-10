// Require deliberate motion to select a hand; retain its identity at rest.
export class MotionTarget {
  constructor(){this.reset()}
  reset(){this.tracks=[];this.serial=0;this.selected=null;this.changed=false}
  update(hands,now,aspect=16/9,{locked=false,eligible=null,verticalOnly=false}={}){
    this.changed=false
    const old=this.tracks.filter(t=>now-t.seen<900), used=new Set(), current=[]
    for(let index=0;index<hands.length;index++){
      const p=hands[index]
      if(p?.length!==21||!p.every(v=>Number.isFinite(v.x)&&Number.isFinite(v.y)))continue
      const x=[0,5,9,13,17].reduce((s,i)=>s+p[i].x*aspect,0)/5
      const y=[0,5,9,13,17].reduce((s,i)=>s+p[i].y,0)/5
      const size=Math.hypot((p[0].x-p[9].x)*aspect,p[0].y-p[9].y)
      if(size<.025)continue
      const candidate=old.filter(t=>!used.has(t.id)).map(t=>({t,d:Math.hypot(x-t.x,y-t.y)/Math.max(size,t.size)})).filter(v=>v.d<2.5&&size/v.t.size>.45&&size/v.t.size<2.2).sort((a,b)=>a.d-b.d)[0]
      let t
      if(candidate){
        const prev=candidate.t;used.add(prev.id)
        const dt=(now-prev.seen)/1000
        const safeDt=Math.max(dt,.03), normal=Math.max(size,prev.size)
        const vx=(x-prev.x)/normal/safeDt, vy=(y-prev.y)/normal/safeDt
        const speed=Math.hypot(vx,vy)
        t={...prev,x,y,size,index,seen:now,count:prev.count+1,moving:speed>1.0?prev.moving+1:0,vx,vy,speed}
      }else t={id:++this.serial,x,y,size,index,seen:now,count:1,moving:0,vx:0,vy:0,speed:0}
      current.push(t)
    }
    this.tracks=[...current,...old.filter(t=>!used.has(t.id)&&!current.some(c=>c.id===t.id)).map(t=>({...t,index:-1}))]
    const active=current.filter(t=>t.count>=3&&t.moving>=2&&(eligible===null||eligible[t.index])&&(!verticalOnly||Math.abs(t.vy)>Math.abs(t.vx)*.65)).sort((a,b)=>b.moving-a.moving||b.speed-a.speed)[0]
    if(active&&active.id!==this.selected&&(!locked||this.selected===null)){this.selected=active.id;this.changed=true}
    const selectedTrack=this.tracks.find(t=>t.id===this.selected)
    if(!selectedTrack){this.selected=null;return -1}
    return selectedTrack.index
  }
}
