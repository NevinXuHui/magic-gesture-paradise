#!/usr/bin/env node

import { app, BrowserWindow } from 'electron'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

const PAGE_WIDTH = 800
const PAGE_HEIGHT = 480
const FPS = Math.max(1, Number(process.env.SCREEN_FPS || 30))
const OUTPUT_FIFO = process.env.SCREEN_FIFO || '/tmp/rps-kids-h5/video.nut'
const URL = process.env.SCREEN_URL || `http://127.0.0.1:${process.env.PORT || 5174}/?camera=server&debug=0&screen=1`

let windowRef
let encoder
let encoderSize
let frameTimer
let statsTimer
let latestFrame
let stopping = false
let encoderWanted = true
let paintCount = 0
let writeCount = 0
let skipCount = 0
let firstPaintLogged = false

function log(message) {
  process.stderr.write(`[screen-renderer] ${message}\n`)
}

function startEncoder(inputWidth, inputHeight) {
  if (encoder || stopping) return

  const transformFilter = inputWidth === PAGE_WIDTH && inputHeight === PAGE_HEIGHT
    ? 'transpose=2'
    : `scale=${PAGE_WIDTH}:${PAGE_HEIGHT}:flags=fast_bilinear,transpose=2`

  const child = spawn(process.env.FFMPEG_BIN || 'ffmpeg', [
    '-y', '-hide_banner', '-loglevel', 'warning',
    '-f', 'rawvideo', '-pixel_format', 'bgra',
    '-video_size', `${inputWidth}x${inputHeight}`, '-framerate', String(FPS), '-i', '-',
    '-an', '-vf', transformFilter,
    '-c:v', 'rawvideo', '-pix_fmt', 'bgra', '-f', 'nut', OUTPUT_FIFO,
  ], { stdio: ['pipe', 'ignore', 'pipe'] })

  encoder = child
  encoderSize = { width: inputWidth, height: inputHeight }
  child.stderr.on('data', data => process.stderr.write(`[ffmpeg] ${data}`))
  child.stdin.on('error', error => {
    if (encoder === child) encoder = null
    log(`FFmpeg stdin ${error.code || error.message}; dropping encoder`)
  })
  child.on('error', error => {
    if (encoder === child) encoder = null
    log(`FFmpeg 启动失败：${error.message}`)
  })
  child.on('exit', (code, signal) => {
    if (encoder === child) encoder = null
    if (!stopping && encoderWanted) {
      log(`FFmpeg 退出：code=${code} signal=${signal || 'none'}，等待重启`)
      setTimeout(() => {
        if (!stopping && encoderWanted && !encoder && encoderSize) {
          startEncoder(encoderSize.width, encoderSize.height)
        }
      }, 300)
    }
  })
  log(`FFmpeg input ${inputWidth}x${inputHeight}; output ${PAGE_HEIGHT}x${PAGE_WIDTH}@${FPS}`)
}

function writeLatestFrame() {
  if (stopping || !latestFrame || !encoder?.stdin?.writable) return
  if (encoder.stdin.writableNeedDrain) {
    skipCount++
    return
  }
  encoder.stdin.write(latestFrame)
  writeCount++
}

function cleanup() {
  if (stopping) return
  stopping = true
  encoderWanted = false
  if (frameTimer) clearInterval(frameTimer)
  if (statsTimer) clearInterval(statsTimer)
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
    transparent: false,
    backgroundColor: '#9edcff',
    webPreferences: {
      offscreen: true,
      backgroundThrottling: false,
      sandbox: false,
    },
  })

  windowRef.webContents.setFrameRate(FPS)
  windowRef.webContents.on('paint', (_event, _dirty, image) => {
    const bitmap = image.toBitmap()
    const size = image.getSize()
    if (!firstPaintLogged) {
      log(`First paint: ${size.width}x${size.height}, ${bitmap.length} bytes`)
      firstPaintLogged = true
    }
    if (!encoder && encoderWanted) startEncoder(size.width, size.height)
    if (bitmap.length === size.width * size.height * 4) {
      latestFrame = bitmap
      paintCount++
    }
  })
  windowRef.webContents.on('render-process-gone', (_event, details) => {
    log(`H5 渲染进程退出：${details.reason}`)
    app.quit()
  })
  windowRef.webContents.on('did-fail-load', (_event, code, description, _url, isMainFrame) => {
    if (isMainFrame) log(`H5 加载失败：${code} ${description}`)
  })
  windowRef.webContents.on('did-finish-load', async () => {
    try {
      const graphics = await windowRef.webContents.executeJavaScript(`(() => {
        const canvas = document.createElement('canvas')
        const gl = canvas.getContext('webgl2') || canvas.getContext('webgl')
        if (!gl) return null
        const debug = gl.getExtension('WEBGL_debug_renderer_info')
        return {
          version: gl.getParameter(gl.VERSION),
          renderer: debug
            ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL)
            : gl.getParameter(gl.RENDERER),
        }
      })()`)
      if (!graphics) {
        log('WebGL unavailable; MediaPipe cannot start')
        return
      }
      log(`WebGL ready: ${graphics.version}; renderer=${graphics.renderer}`)
    } catch (error) {
      log(`WebGL probe failed: ${error.message}`)
    }
  })
  windowRef.loadURL(URL)
}

app.commandLine.appendSwitch('no-sandbox')
app.commandLine.appendSwitch('disable-gpu-sandbox')
app.commandLine.appendSwitch('disable-dev-shm-usage')
app.commandLine.appendSwitch('mute-audio')
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required')
// ANGLE exposes SwiftShader as a complete WebGL implementation under Xvfb.
// MediaPipe's CPU delegate still needs this context for its image pipeline.
app.commandLine.appendSwitch('use-gl', 'angle')
app.commandLine.appendSwitch('use-angle', 'swiftshader')
app.commandLine.appendSwitch('enable-unsafe-swiftshader')
app.commandLine.appendSwitch('ignore-gpu-blocklist')

app.whenReady().then(() => {
  if (!existsSync(OUTPUT_FIFO)) {
    log(`FIFO 不存在：${OUTPUT_FIFO}`)
    app.exit(1)
    return
  }
  createWindow()
  frameTimer = setInterval(writeLatestFrame, 1000 / FPS)
  statsTimer = setInterval(() => {
    log(`STATS paint=${(paintCount / 10).toFixed(1)}fps write=${(writeCount / 10).toFixed(1)}fps skip=${(skipCount / 10).toFixed(1)}/s rss=${Math.round(process.memoryUsage().rss / 1048576)}MB`)
    paintCount = 0
    writeCount = 0
    skipCount = 0
  }, 10000)
})

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.once(signal, () => {
    cleanup()
    app.quit()
  })
}
process.on('uncaughtException', error => {
  log(`未捕获异常：${error.stack || error.message}`)
  cleanup()
  app.exit(1)
})
app.on('before-quit', cleanup)
app.on('window-all-closed', () => app.quit())
