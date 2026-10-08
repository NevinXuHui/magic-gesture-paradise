const handNames={fist:'rock',peace:'scissors',palm:'paper'}

export function gameResultData(round){
  if(!round||!Object.hasOwn(handNames,round.user)||!Object.hasOwn(handNames,round.computer)||!['win','lose','draw'].includes(round.outcome))return null
  return {user:handNames[round.user],robot:handNames[round.computer],result:round.outcome}
}
