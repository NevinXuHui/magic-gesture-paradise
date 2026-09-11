const ids=['fist','peace','palm']
const names={Closed_Fist:'fist',Victory:'peace',Open_Palm:'palm'}
const empty=(phase)=>({id:'no_gesture',score:null,source:'rps',stable:false,phase})
const distance=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z)
const valid=p=>p?.length===21&&p.every(v=>v&&[v.x,v.y,v.z].every(Number.isFinite))
const clamp=n=>Math.max(0,Math.min(1,n))
function angle(a,b,c){const u=[a.x-b.x,a.y-b.y,a.z-b.z],v=[c.x-b.x,c.y-b.y,c.z-b.z];return Math.acos(Math.max(-1,Math.min(1,u.reduce((s,n,i)=>s+n*v[i],0)/(Math.hypot(...u)*Math.hypot(...v)||1))))*180/Math.PI}

// Continuous finger openness avoids all-or-nothing angle rules. These are
// heuristic matching scores, not calibrated probabilities.
function geometryScores(points,{world=false}={}){
  if(!valid(points))return null
  const scale=distance(points[0],points[9])
  // World points are metric; image points are normalized. Keep their validity
  // checks separate so a weak 3D estimate cannot discard a clear 2D hand.
  if(world?(scale<.015||scale>.20):(scale<.025||scale>.60))return null
  const fingers=[]
  const openness=[5,9,13,17].map(b=>{
    const p=points.slice(b,b+4), bones=[distance(p[0],p[1]),distance(p[1],p[2]),distance(p[2],p[3])]
    if(bones.some(n=>n<scale*.055||n>scale*(world?1.2:1.8)))return NaN
    const straight=distance(p[0],p[3])/bones.reduce((a,b)=>a+b,0)
    const reach=distance(points[0],p[3])/(distance(points[0],p[0])||1)
    const pip=clamp((angle(p[0],p[1],p[2])-82)/82)
    const dip=clamp((angle(p[1],p[2],p[3])-82)/82)
    const pipAngle=angle(p[0],p[1],p[2]), dipAngle=angle(p[1],p[2],p[3])
    fingers.push({extended:pipAngle>145&&dipAngle>140&&straight>.82,
      curled:pipAngle<130||dipAngle<125||straight<.70})
    // A finger can look straight at PIP while folding back at its tip.
    return .5*Math.min(pip,dip)+.3*clamp((straight-.52)/.43)+.2*clamp((reach-1.05)/.85)
  })
  if(!openness.every(Number.isFinite))return null
  const pattern=[[0,0,0,0],[1,1,0,0],[1,1,1,1]]
  const scores=Object.fromEntries(pattern.map((expected,i)=>[ids[i],Math.exp(-7*expected.reduce((sum,value,j)=>sum+(value-openness[j])**2,0)/4)]))
  const ranked=Object.values(scores).sort((a,b)=>b-a)
  const scissors=fingers[0].extended&&fingers[1].extended&&fingers[2].curled&&fingers[3].curled
  const palm=fingers.every(f=>f.extended)
  return {scores,certainty:ranked[0]-ranked[1],openness,scissors,palm}
}

export function rpsScores(categories=[],world,landmarks){
  const model=Object.fromEntries(ids.map(id=>[id,0]))
  for(const c of categories)if(names[c.categoryName]&&Number.isFinite(c.score))model[names[c.categoryName]]=Math.max(model[names[c.categoryName]],clamp(c.score))
  const candidates=[['world',geometryScores(world,{world:true})],['image',geometryScores(landmarks)]].filter(([,value])=>value)
  // Select the coordinate set that most clearly separates first from second
  // place. It gives camera-facing hands a 2D fallback when world depth is
  // noisy, while preserving 3D robustness for side-on scissors.
  let [geometrySource,geometry]=candidates.sort((a,b)=>b[1].certainty-a[1].certainty)[0]||[]
  const worldGeometry=candidates.find(([source])=>source==='world')?.[1]
  const scissors=g=>g&&(g.scissors||(Math.min(g.openness[0],g.openness[1])>.70&&Math.max(g.openness[2],g.openness[3])<.45))
  // Camera-facing fingers overlap in the image; a confident projected palm
  // must not overrule clear curled ring/little fingers in metric 3D.
  const imageGeometry=candidates.find(([source])=>source==='image')?.[1]
  // Normalized image landmarks still contain estimated depth. A separate XY
  // projection recovers a visibly clear V when that depth estimate flickers.
  const projectedGeometry=valid(landmarks)?geometryScores(landmarks.map(p=>({...p,z:0}))):null
  if(scissors(worldGeometry)) {geometry=worldGeometry;geometrySource='world'}
  else if(imageGeometry?.scissors) {geometry=imageGeometry;geometrySource='image'}
  else if(projectedGeometry?.scissors) {geometry=projectedGeometry;geometrySource='projection'}
  const scores=Object.fromEntries(ids.map(id=>[id,geometry?.scores[id]!=null?.85*geometry.scores[id]+.15*model[id]:model[id]]))
  // Disagreement about curled fingers is ambiguity, not evidence of a palm.
  if(candidates.some(([,g])=>scissors(g))) scores.palm=Math.min(scores.palm,.20)
  if(geometry?.scissors){
    scores.peace=Math.max(scores.peace,.85)
    scores.fist=Math.min(scores.fist,.15)
  }
  // A nearest-template winner is not enough for an open palm: all four
  // non-thumb fingers must individually pass the extension checks.
  if(geometry&&!geometry.palm) scores.palm=Math.min(scores.palm,.18)
  if(worldGeometry&&worldGeometry.openness.slice(2).some(v=>v<.45)) scores.palm=Math.min(scores.palm,.18)
  return {
    scores,
    hasGeometry:!!geometry,
    geometrySource:geometrySource||null,
    openness:geometry?.openness||null,
  }
}

export class StillRps {
  constructor(){this.reset()}
  reset(){this.last=null;this.anchor=null;this.since=0;this.time=0;this.samples=[];this.motionFrames=0}
  update({landmarks,worldLandmarks,categories=[],now,holdMs=250,aspect=4/3,handCount=1,motionSpeed=1.8,maxDrift=.22}){
    if(handCount!==1||!valid(landmarks)){this.reset();return empty(handCount>1?'multiple':'no_hand')}
    const raw=landmarks.map(p=>({x:p.x*aspect,y:p.y,z:p.z*aspect}))
    // Smooth only the motion signal. Classification remains based on the
    // original landmarks so a crisp scissors hand does not get blurred.
    const points=this.last?raw.map((p,i)=>({x:.45*p.x+.55*this.last[i].x,y:.45*p.y+.55*this.last[i].y,z:.45*p.z+.55*this.last[i].z})):raw
    const scale=distance(points[0],points[9])
    if(scale<.025){this.reset();return empty('unclear')}
    // Estimated depth jitters more than image coordinates. Downweight it for
    // motion only; classification still uses the original 3D landmarks.
    const delta=p=>Math.sqrt(points.reduce((s,v,i)=>s+(v.x-p[i].x)**2+(v.y-p[i].y)**2+(.35*(v.z-p[i].z))**2,0)/21)/scale
    const dt=(now-this.time)/1000
    if(!this.last||dt<=0||dt>.5){this.reset();this.last=points;this.anchor=points;this.time=now;this.since=now;return empty('settling')}
    const speed=delta(this.last)/dt, drift=delta(this.anchor)
    const moving=speed>motionSpeed || drift>maxDrift
    const hardMove=speed>motionSpeed*1.8 || drift>maxDrift*1.6
    this.last=points;this.time=now
    if(moving){
      this.motionFrames++
      if(hardMove||this.motionFrames>=2){this.anchor=points;this.since=now;this.samples=[]}
      return empty('moving')
    }
    this.motionFrames=0
    this.samples.push(rpsScores(categories,worldLandmarks,raw));this.samples=this.samples.slice(-8)
    if(now-this.since<holdMs||this.samples.length<3)return empty('settling')
    const ranked=ids.map(id=>({id,value:this.samples.reduce((s,v)=>s+v.scores[id],0)/this.samples.length})).sort((a,b)=>b.value-a.value)
    const hasGeometry=this.samples.some(s=>s.hasGeometry)
    // Prefer one of the three, but reject distant or near-tied hand shapes.
    if(ranked[0].value<(hasGeometry?.36:.22)||ranked[0].value-ranked[1].value<.07)return {...empty('unclear'),candidates:ranked}
    return {id:ranked[0].id,score:null,matchScore:ranked[0].value,source:'rps',method:'still_rps',stable:true,phase:'recognized',candidates:ranked}
  }
}
