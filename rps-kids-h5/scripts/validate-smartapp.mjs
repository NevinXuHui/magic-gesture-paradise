import { readFileSync, statSync } from 'node:fs'
import { join, resolve, sep } from 'node:path'

const root = resolve(process.argv[2] || '.')
const manifest = JSON.parse(readFileSync(join(root, 'manifest.json'), 'utf8'))
const exactKeys = (value, expected, field) => {
  const actual = Object.keys(value).sort()
  const wanted = [...expected].sort()
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) throw Error(`${field} 字段不符合 Runtime v1 schema`)
}
const entry = (directory, component, field) => {
  if (typeof component.enabled !== 'boolean' || typeof component.entry !== 'string' || !component.entry) throw Error(`${field} 配置无效`)
  if (!component.enabled) return
  const path = resolve(root, directory, component.entry)
  if (!path.startsWith(resolve(root, directory) + sep) || !statSync(path).isFile()) throw Error(`${field}.entry 不在应用包内`)
}

exactKeys(manifest, ['schemaVersion', 'appId', 'version', 'web', 'backend', 'routing'], 'manifest')
exactKeys(manifest.web, ['enabled', 'entry'], 'web')
exactKeys(manifest.backend, ['enabled', 'entry', 'dynamicService'], 'backend')
exactKeys(manifest.routing, ['defaultTarget'], 'routing')
if (manifest.schemaVersion !== 1) throw Error('仅支持 schemaVersion=1')
if (!/^[a-z][a-z0-9_]{0,63}$/.test(manifest.appId)) throw Error('appId 格式无效')
if (typeof manifest.version !== 'string' || !manifest.version) throw Error('version 格式无效')
if (!['web', 'python'].includes(manifest.routing.defaultTarget)) throw Error('routing.defaultTarget 无效')
if (typeof manifest.backend.dynamicService !== 'boolean') throw Error('backend.dynamicService 必须是布尔值')
entry('web', manifest.web, 'web')
entry('backend', manifest.backend, 'backend')
console.log(`SmartApp 结构校验通过：${manifest.appId}@${manifest.version}`)

for (const file of ['inference.py', 'requirements.txt', 'requirements-robot.txt', 'models/gesture_recognizer.task', 'vendor/mediapipe/__init__.py', 'vendor/numpy/__init__.py']) {
  if (!statSync(join(root, 'backend', file)).isFile()) throw Error(`缺少 Python 推理资源 ${file}`)
}
