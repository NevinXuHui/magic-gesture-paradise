import { readFileSync } from "node:fs"
import assert from "node:assert/strict"

const manifest = JSON.parse(readFileSync("manifest.json", "utf8"))
assert.equal(manifest.schemaVersion, 1)
assert.equal(manifest.appId, "cloud_show_display")
assert.equal(manifest.web.enabled, true)
assert.equal(manifest.backend.enabled, false)
assert.equal(manifest.routing.defaultTarget, "web")
const page = readFileSync("pages/english_show_800x480.html", "utf8")
assert.match(page, /window\.cloudShow = window\.englishShow/)
assert.match(page, /message\.event === "cloud_data"/)
assert.match(page, /smartApp\.onMessage/)
console.log("SmartApp 源码契约校验通过")
