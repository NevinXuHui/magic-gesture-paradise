# english SmartApp

该目录现在同时保留旧版 Electron 云端展示调试脚本，并提供与 `rps-kids-h5` 相同的 SmartApp v1 组包入口。正式运行时由 `smartapp-runtime` 统一提供 Electron/FFmpeg/MPV 渲染器。

构建并校验应用包：

```bash
npm test
npm run build:smartapp
```

产物位于 `build/smartapp/cloud_show_display-0.2.0.tar.gz`。Runtime 启动后，向该应用发送如下 `cloud_data.data` 即可展示英文单词：

```json
{"word":"Apple","meaning":"苹果"}
```
