export const catalog = [
  ['peace','剪刀','✌️','model','食指和中指伸直，其余手指收起'],
  ['fist','石头','✊','model','五指握拳，完整露出拳头'],
  ['palm','布','🖐️','model','五指自然张开，手掌朝向镜头'],
  ['like','点赞','👍','model','拇指向上，其余四指收起'],
  ['dislike','不喜欢','👎','model','拇指向下，其余四指收起'],
  ['one','一','☝️','model','仅食指伸直'],
  ['ok','OK','👌','rule','拇指与食指指尖接触，其余三指伸直'],
  ['call','打电话','🤙','rule','拇指和小指伸直，其余收起'],
  ['four','四','4','rule','四指伸直，拇指收起'],
  ['three','三','3','rule','食指、中指、无名指伸直'],
  ['two','二','2','alias','与 peace 共用手型，本版本输出 peace'],
  ['rock','摇滚手','🤘','rule','食指和小指伸直，中指与无名指收起'],
  ['grabbing','抓','—','pending','动态动作，需要时序模型与样本'],
  ['holy','双手合十','—','pending','需要双手交互模型与样本'],
  ['timeout','暂停','—','pending','需要双手交互模型与样本'],
  ['xsign','叉号','—','pending','需要双手交互模型与样本'],
  ['hand_heart','比心','—','pending','需要确定手型定义并训练'],
  ['take_picture','拍照','—','pending','需要双手交互模型与样本'],
  ['mute','嘘','—','pending','需要脸部与手指相对位置'],
  ['stop','停止','—','alias','与 palm 共用张掌手型，本版本输出 palm'],
  ['stop_inverted','手背','—','pending','需要手掌与手背朝向分类样本'],
  ['no_gesture','没手势','∅','system','未检测到手、置信度不足或不支持的手型'],
].map(([id,label,icon,status,hint])=>({id,label,icon,status,hint}))
export const byId = Object.fromEntries(catalog.map(g=>[g.id,g]))
export const rps = ['peace','fist','palm']
const modelMap = { Closed_Fist:'fist', Open_Palm:'palm', Victory:'peace', Thumb_Up:'like', Thumb_Down:'dislike', Pointing_Up:'one' }
export const none = () => ({ id:'no_gesture', score:0, source:'system' })
const dist = (a,b) => Math.hypot(a.x-b.x,a.y-b.y,(a.z||0)-(b.z||0))
function angle(a,b,c) {
  const u=[a.x-b.x,a.y-b.y,(a.z||0)-(b.z||0)], v=[c.x-b.x,c.y-b.y,(c.z||0)-(b.z||0)]
  const norm=Math.hypot(...u)*Math.hypot(...v)
  return norm ? Math.acos(Math.max(-1,Math.min(1,u.reduce((s,n,i)=>s+n*v[i],0)/norm)))*180/Math.PI : 0
}
export function ruleGesture(p) {
  if (!p || p.length!==21 || p.some(v=>!Number.isFinite(v.x)||!Number.isFinite(v.y))) return null
  const scale=dist(p[0],p[9]); if(scale<0.02) return null
  const extended = (m,pip,dip,tip) => angle(p[m],p[pip],p[tip])>155 && angle(p[pip],p[dip],p[tip])>150 && dist(p[tip],p[0])>dist(p[pip],p[0])*1.12
  const [i,m,r,l]=[[5,6,7,8],[9,10,11,12],[13,14,15,16],[17,18,19,20]].map(f=>extended(...f))
  const thumb=angle(p[2],p[3],p[4])>150 && dist(p[4],p[5])>scale*0.65
  if(dist(p[4],p[8])<scale*0.3 && m && r && l) return 'ok'
  if(!i&&!m&&!r&&l&&thumb) return 'call'
  if(i&&!m&&!r&&l&&!thumb) return 'rock'
  if(i&&m&&r&&l&&!thumb) return 'four'
  if(i&&m&&r&&!l&&!thumb) return 'three'
  return null
}
// World landmarks use a common 3D scale. Image x/y/z coordinates are not
// interchangeable here: perspective foreshortening is exactly what we avoid.
export function isWorldPeace(p) {
  if(!p || p.length!==21 || p.some(v=>!v || ![v.x,v.y,v.z].every(Number.isFinite))) return false
  const scale=dist(p[0],p[9])
  if(scale<0.015 || scale>0.20) return false
  const fingers=[5,9,13,17].map(base=>{
    const q=p.slice(base,base+4)
    const bones=[dist(q[0],q[1]),dist(q[1],q[2]),dist(q[2],q[3])]
    const valid=bones.every(n=>n>scale*.07 && n<scale*1.2)
    const straightness=dist(q[0],q[3])/bones.reduce((a,b)=>a+b,0)
    const pip=angle(q[0],q[1],q[2]), dip=angle(q[1],q[2],q[3])
    return {valid,extended:pip>150 && dip>145 && straightness>.88,folded:pip<125 && straightness<.80}
  })
  return fingers.every(f=>f.valid) && fingers[0].extended && fingers[1].extended &&
    fingers[2].folded && fingers[3].folded && dist(p[8],p[12])>scale*.16
}
export function classify(category, landmarks, { mode='rps', threshold=0.65, worldLandmarks, robustPeace=true }={}) {
  const id=modelMap[category?.categoryName]
  if(id && category.score>=threshold && (mode!=='rps'||rps.includes(id))) return {id,score:category.score,source:'model'}
  // Never override a confident conflicting model class, including ILoveYou.
  const conflict=category?.categoryName && !['None','Victory'].includes(category.categoryName) && category.score>=threshold
  if(robustPeace && !conflict && isWorldPeace(worldLandmarks)) return {id:'peace',score:null,source:'rule',method:'peace_3d'}
  if(mode==='all') { const rule=ruleGesture(landmarks); if(rule) return {id:rule,score:null,source:'rule'} }
  return none()
}
// Time-based stability has the same meaning across fast desktops and slower boards.
export class Stabilizer {
  constructor() { this.reset() }
  reset() { this.candidate='no_gesture'; this.since=0; this.count=0; this.current=none(); this.lastSeen=0; this.evidence=[]; this.gap=false }
  update(result, now, holdMs=250, { handPresent=false, tolerateDropout=false }={}) {
    if(result.id==='no_gesture') {
      // Preserve only the candidate through a short classifier dropout. Never
      // publish a stale stable gesture, and never retain a missing/ambiguous hand.
      if(tolerateDropout && handPresent && this.candidate!=='no_gesture' && now-this.lastSeen<=160) {
        this.evidence=[...this.evidence.slice(-7),false];this.gap=true
        return {...none(),stable:false,progress:0}
      }
      this.reset(); return { ...none(), stable:false, progress:0 }
    }
    if(this.gap) {
      if(now-this.lastSeen>160) this.reset()
      else this.since+=now-this.lastSeen // Missing frames do not earn hold time.
    }
    if(result.id!==this.candidate) { this.candidate=result.id; this.since=now; this.count=0; this.current=none() }
    if(this.count===0)this.evidence=[]
    this.evidence=[...this.evidence.slice(-7),true]
    this.lastSeen=now;this.gap=false
    this.count++
    const progress=Math.min(1,(now-this.since)/holdMs)
    const stable=progress>=1 && this.count>=3 && this.evidence.filter(Boolean).length/this.evidence.length>=.7
    this.current=stable?result:none()
    return { ...this.current, stable, progress }
  }
}
export function summarizeTimings(values) {
  if(!values.length) return null
  const ordered=[...values].sort((a,b)=>a-b)
  return {count:values.length,mean:values.reduce((a,b)=>a+b,0)/values.length,p95:ordered[Math.ceil(values.length*.95)-1]}
}
