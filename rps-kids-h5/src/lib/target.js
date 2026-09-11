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
    let selectedTrack=this.tracks.find(t=>t.id===this.selected)
    // MediaPipe can briefly split one real hand into two tracks while a fist
    // opens into scissors. During a round the player has already been chosen;
    // if its old track disappears and exactly one hand remains, keep the round
    // attached to that visible hand instead of passing index -1 downstream.
    // This is deliberately not marked as `changed`: App.vue would otherwise
    // reset the shake-stop gate and discard the just-finished gesture.
    if(locked&&selectedTrack?.index===-1&&current.length===1){
      const visible=current[0]
      const distance=Math.hypot(visible.x-selectedTrack.x,visible.y-selectedTrack.y)/Math.max(visible.size,selectedTrack.size)
      const scale=visible.size/selectedTrack.size
      // A nearby, similarly sized hand is the detector's replacement for the
      // same player. Do not transfer the lock to an unrelated person entering
      // from elsewhere in the frame.
      if(distance<1.3&&scale>.5&&scale<2){
        this.selected=visible.id
        selectedTrack=visible
      }
    }
    if(!selectedTrack){this.selected=null;return -1}
    return selectedTrack.index
  }
}
