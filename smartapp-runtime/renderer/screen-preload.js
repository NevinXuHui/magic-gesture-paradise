const { contextBridge, ipcRenderer } = require('electron')

const listeners = new Set()
const pending = []

ipcRenderer.on('smartapp:message', (_event, message) => {
  if (listeners.size === 0) {
    pending.push(message)
    return
  }
  for (const listener of listeners) {
    try {
      listener(message)
    } catch {
      // Application callback errors must not break the bridge.
    }
  }
})

contextBridge.exposeInMainWorld('smartApp', {
  onMessage(callback) {
    if (typeof callback !== 'function') throw new TypeError('callback must be a function')
    listeners.add(callback)
    while (pending.length > 0) callback(pending.shift())
    return () => listeners.delete(callback)
  },
  postMessage(message) {
    ipcRenderer.send('smartapp:post-message', message)
  },
})
