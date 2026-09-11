<script setup>
defineProps({kind:{type:String,default:'fist'},label:{type:String,default:''}})
const positions={fist:'0% 0%',peace:'50% 0%',palm:'100% 0%',dog:'0% 100%',boy:'50% 100%',crown:'100% 100%'}
</script>
<template><span class="game-sprite" :class="`sprite-${kind}`" role="img" :aria-label="label || kind" :style="{backgroundPosition:positions[kind]}"></span></template>
<style scoped>
.game-sprite{display:block;width:100%;height:100%;background-image:url('/art/game-atlas-v2.png');background-size:300% 200%;background-repeat:no-repeat;}
</style>
