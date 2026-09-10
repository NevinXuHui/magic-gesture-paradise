import { cp, mkdir, access } from 'node:fs/promises'
import { build } from 'esbuild'
await mkdir('public/vendor', { recursive: true })
await cp('node_modules/@mediapipe/tasks-vision/wasm', 'public/vendor/wasm', { recursive: true })
await build({entryPoints:['node_modules/@mediapipe/tasks-vision/vision_bundle.mjs'],bundle:true,format:'iife',globalName:'vision',outfile:'public/vendor/vision_bundle.js',minify:true})
try { await access('public/models/gesture_recognizer.task') }
catch { console.warn('缺少模型：请按照 README 下载 public/models/gesture_recognizer.task') }
