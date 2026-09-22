#!/usr/bin/env node

const { app, BrowserWindow, ipcMain } = require('electron')
const { spawn } = require('node:child_process')
const { existsSync } = require('node:fs')
const net = require('node:net')
const path = require('node:path')
const readline = require('node:readline')

const PAGE_WIDTH = 800
const PAGE_HEIGHT = 480
const FPS = Math.max(1, Number(process.env.SCREEN_FPS || 30))
const OUTPUT_FIFO = process.env.SCREEN_FIFO || '/run/smartapp-renderer/video.nut'
const MPV_SOCKET = process.env.MPV_SOCKET || '/tmp/mpv-socket'
const URL = process.env.SCREEN_URL || process.argv[2]
const FFMPEG = process.env.FFMPEG_BIN || 'ffmpeg'

let windowRef
let encoder
let frameTimer
let latestFrame
let firstPaint = false
let pageLoaded = false
let readySent = false
let claiming = false
let stopping = false
const pendingAppData = []

function log(message) {
  process.stderr.write(`[smartapp-screen] ${message}\n`)
}

function emit(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`)
}

function mpvRequest(command, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    let settled = false
    let buffer = ''
    const socket = net.createConnection(MPV_SOCKET)
    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      socket.destroy()
      if (error) reject(error)
      else resolve(value)
    }
    const timer = setTimeout(() => finish(Error('MPV IPC timed out')), timeoutMs)
    socket.setEncoding('utf8')
    socket.on('connect', () => socket.write(`${JSON.stringify({ command })}\n`))
    socket.on('data', chunk => {
      buffer += chunk
      const newline = buffer.indexOf('\n')
      if (newline < 0) return
      try {
        const response = JSON.parse(buffer.slice(0, newline))
        if (response.error && response.error !== 'success') finish(Error(`MPV: ${response.error}`))
        else finish(null, response)
      } catch (error) {
        finish(error)
      }
    })
    socket.on('error', error => finish(error))
    socket.on('end', () => finish(Error('MPV IPC closed without a response')))
  })
}

async function waitForMpvPath(expected, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs
  let currentPath = ''
  do {
    const current = await mpvRequest(['get_property', 'path'])
    currentPath = String(current.data || '')
    if (currentPath === expected) return
    await new Promise(resolve => setTimeout(resolve, 100))
  } while (Date.now() < deadline)
  throw Error(`MPV 未加载 SmartApp FIFO，当前路径: ${currentPath || '<empty>'}`)
}

function startEncoder(width, height) {
  if (encoder || stopping) return
  const transform = width === PAGE_WIDTH && height === PAGE_HEIGHT
    ? 'transpose=2'
    : `scale=${PAGE_WIDTH}:${PAGE_HEIGHT}:flags=fast_bilinear,transpose=2`
  encoder = spawn(FFMPEG, [
    '-y', '-hide_banner', '-loglevel', 'warning',
    '-f', 'rawvideo', '-pixel_format', 'bgra',
    '-video_size', `${width}x${height}`, '-framerate', String(FPS), '-i', '-',
    '-an', '-vf', transform,
    '-c:v', 'rawvideo', '-pix_fmt', 'bgra', '-f', 'nut', OUTPUT_FIFO,
  ], { stdio: ['pipe', 'ignore', 'pipe'] })
  encoder.stderr.on('data', data => process.stderr.write(`[ffmpeg] ${data}`))
  encoder.stdin.on('error', error => log(`FFmpeg stdin: ${error.code || error.message}`))
  encoder.on('error', error => {
    log(`FFmpeg 启动失败: ${error.message}`)
    if (!stopping) app.exit(1)
  })
  encoder.on('exit', (code, signal) => {
    encoder = null
    if (!stopping) {
      log(`FFmpeg 意外退出: code=${code} signal=${signal || 'none'}`)
      app.exit(1)
    }
  })
  log(`FFmpeg ${width}x${height} -> ${PAGE_HEIGHT}x${PAGE_WIDTH}@${FPS}`)
}

async function claimDisplay() {
  if (readySent || claiming || !firstPaint || !pageLoaded || stopping) return
  claiming = true
  try {
    await mpvRequest(['set_property', 'hwdec', 'no'])
    await mpvRequest(['set_property', 'hwdec-codecs', 'no'])
    await mpvRequest(['set_property', 'loop-file', false])
    await mpvRequest(['set_property', 'keepaspect', true])
    await mpvRequest(['set_property', 'cache', false])
    await mpvRequest(['set_property', 'demuxer-readahead-secs', 0])
    await mpvRequest(['set_property', 'video-sync', 'desync'])
    await mpvRequest(['vf', 'set', `fps=${FPS}`])
    await mpvRequest(['loadfile', OUTPUT_FIFO, 'replace'], 8000)
    await mpvRequest(['set_property', 'pause', false])
    await waitForMpvPath(OUTPUT_FIFO)
    readySent = true
    emit({ event: 'renderer_ready' })
    while (pendingAppData.length > 0) emit(pendingAppData.shift())
  } catch (error) {
    log(`接管屏幕失败: ${error.message}`)
    app.exit(1)
  } finally {
    claiming = false
  }
}

function writeLatestFrame() {
  if (stopping || !latestFrame || !encoder?.stdin?.writable || encoder.stdin.writableNeedDrain) return
  encoder.stdin.write(latestFrame)
}

function validAppData(message) {
  return message && typeof message === 'object' && !Array.isArray(message)
    && message.event === 'app_data'
    && typeof message.dataType === 'string' && message.dataType.length > 0
    && message.data && typeof message.data === 'object' && !Array.isArray(message.data)
    && Object.keys(message).length === 3
}

function installBridge() {
  ipcMain.on('smartapp:post-message', (_event, message) => {
    if (validAppData(message)) {
      if (readySent) emit(message)
      else pendingAppData.push(message)
    }
    else log('忽略无效 H5 app_data')
  })
  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity })
  input.on('line', line => {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      log('忽略无效 Runtime JSONL')
      return
    }
    if (message?.event === 'renderer_stop') {
      app.quit()
      return
    }
    if (windowRef && !windowRef.isDestroyed()) {
      windowRef.webContents.send('smartapp:message', message)
    }
  })
  input.on('close', () => {
    if (!stopping) app.quit()
  })
}

function cleanup() {
  if (stopping) return
  stopping = true
  if (frameTimer) clearInterval(frameTimer)
  if (encoder) {
    encoder.stdin.destroy()
    encoder.kill('SIGTERM')
    encoder = null
  }
  if (windowRef && !windowRef.isDestroyed()) windowRef.destroy()
}

function createWindow() {
  windowRef = new BrowserWindow({
    width: PAGE_WIDTH,
    height: PAGE_HEIGHT,
    show: false,
    frame: false,
    backgroundColor: '#000000',
    webPreferences: {
      offscreen: true,
      backgroundThrottling: false,
      contextIsolation: true,
      sandbox: false,
      preload: path.join(__dirname, 'screen-preload.js'),
    },
  })
  windowRef.webContents.setFrameRate(FPS)
  windowRef.webContents.on('paint', (_event, _dirty, image) => {
    const bitmap = image.toBitmap()
    const size = image.getSize()
    if (!encoder) startEncoder(size.width, size.height)
    if (bitmap.length === size.width * size.height * 4) latestFrame = bitmap
    if (!firstPaint) {
      firstPaint = true
      log(`First paint: ${size.width}x${size.height}`)
      void claimDisplay()
    }
  })
  windowRef.webContents.on('did-finish-load', () => {
    pageLoaded = true
    void claimDisplay()
  })
  windowRef.webContents.on('did-fail-load', (_event, code, description, _url, main) => {
    if (main) {
      log(`H5 加载失败: ${code} ${description}`)
      app.exit(1)
    }
  })
  windowRef.webContents.on('render-process-gone', (_event, details) => {
    log(`H5 渲染进程退出: ${details.reason}`)
    app.exit(1)
  })
  windowRef.loadURL(URL)
}

app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('disable-gpu-sandbox')
app.commandLine.appendSwitch('disable-dev-shm-usage')
app.commandLine.appendSwitch('mute-audio')
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')
app.commandLine.appendSwitch('use-gl', 'angle')
app.commandLine.appendSwitch('use-angle', 'swiftshader')
app.commandLine.appendSwitch('enable-unsafe-swiftshader')
app.commandLine.appendSwitch('ignore-gpu-blocklist')

app.whenReady().then(() => {
  if (!URL || !existsSync(OUTPUT_FIFO) || !existsSync(MPV_SOCKET)) {
    log('缺少 URL、FIFO 或 MPV socket')
    app.exit(1)
    return
  }
  installBridge()
  createWindow()
  frameTimer = setInterval(writeLatestFrame, 1000 / FPS)
})

for (const name of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.once(name, () => app.quit())
}
process.on('uncaughtException', error => {
  log(`未捕获异常: ${error.stack || error.message}`)
  cleanup()
  app.exit(1)
})
app.on('before-quit', cleanup)
app.on('window-all-closed', () => app.quit())
