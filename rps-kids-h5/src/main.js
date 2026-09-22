requestAnimationFrame(()=>setTimeout(()=>{
  import('./app-entry.js').catch(error=>{
    console.error(error)
    const message=document.querySelector('.boot-screen p')
    if(message)message.textContent='游戏加载失败，请重试'
  })
},0))
