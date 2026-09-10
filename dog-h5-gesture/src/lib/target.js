// Require deliberate motion to select a hand; retain its identity at rest.
export class MotionTarget {
  constructor(){this.reset()}
  reset(){this.tracks=[];this.serial=0;this.selected=null;this.changed=false}
  update(hands,now,aspect=16/9){
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
        const speed=candidate.d/Math.max(dt,.03)
        t={...prev,x,y,size,index,seen:now,count:prev.count+1,moving:speed>1.0?prev.moving+1:0}
      }else t={id:++this.serial,x,y,size,index,seen:now,count:1,moving:0}
      current.push(t)
    }
    this.tracks=[...current,...old.filter(t=>!used.has(t.id)&&!current.some(c=>c.id===t.id)).map(t=>({...t,index:-1}))]
    const active=current.filter(t=>t.count>=3&&t.moving>=2).sort((a,b)=>b.moving-a.moving)[0]
    if(active&&active.id!==this.selected){this.selected=active.id;this.changed=true}
    const locked=this.tracks.find(t=>t.id===this.selected)
    if(!locked){this.selected=null;return -1}
    return locked.index
  }
}
