# 实体屏 Renderer

该目录实现 SmartApp Runtime 的生产 `process` Renderer：

```text
Runtime JSONL
  -> screen-runtime.sh
  -> Xvfb + Electron Offscreen
  -> FFmpeg BGRA/NUT FIFO
  -> 设备 MPV IPC
  -> 480 x 800 实体屏
```

## 安装

在目标设备安装系统依赖并安装本目录固定版本的 Electron：

```bash
sudo apt-get install -y --no-install-recommends ffmpeg xvfb socat
npm ci --prefix /opt/smartapp-runtime/renderer
npm run --prefix /opt/smartapp-runtime/renderer install:electron
chmod +x /opt/smartapp-runtime/renderer/*.sh
```

Electron 44 的 npm 包需要显式执行平台二进制安装。目标网络无法访问默认下载源时可指定镜像：

```bash
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ \
  npm run --prefix /opt/smartapp-runtime/renderer install:electron
```

OpenGL/WebGL 使用 Electron 的 ANGLE/SwiftShader。设备必须已有 `/tmp/mpv-socket`，并允许 Runtime 服务账号访问该 socket、X11 临时目录和 ROS2 表情服务。

## Runtime 配置

```toml
[renderer]
kind = "process"
process_argv = [
  "/opt/smartapp-runtime/renderer/screen-runtime.sh",
  "{url}", "{session_id}", "{app_id}", "{version}"
]
load_argv = []
send_argv = []
stop_argv = []
restore_argv = ["/opt/smartapp-runtime/renderer/restore-expression.sh"]
```

支持的环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ELECTRON_BIN_OVERRIDE` | 本目录 Electron | Electron 可执行文件覆盖路径 |
| `FFMPEG_BIN` | `ffmpeg` | FFmpeg 路径或命令名 |
| `XVFB_BIN` | `Xvfb` | Xvfb 路径或命令名 |
| `MPV_SOCKET` | `/tmp/mpv-socket` | 设备 MPV IPC socket |
| `SCREEN_DISPLAY` | `:99` | Xvfb 显示号 |
| `SCREEN_FPS` | `15` | 输出帧率 |
| `SCREEN_RUNTIME_DIR` | `/run/smartapp-renderer` | FIFO、日志和用户数据目录 |

## H5 Bridge

preload 向 H5 暴露：

```javascript
const unsubscribe = window.smartApp.onMessage(message => {
  // runtime_init 或 cloud_data
})

window.smartApp.postMessage({
  event: 'app_data',
  dataType: 'game_result',
  data: { result: 'win' },
})
```

Runtime 在收到 Electron 的 `renderer_ready` 后才投递 `runtime_init`。在 H5 注册监听器前到达的消息会由 preload 暂存。

实体设备的 `timeouts.renderer` 必须覆盖 ROS 表情切换、Electron 启动以及 H5 首帧时间。当前 RK3588 上包含 MediaPipe 的游戏实测约 28 秒，生产配置建议从 60 秒起，并让 `timeouts.startup` 大于该值。

## 目标机检查

```bash
npm run --prefix /opt/smartapp-runtime/renderer check
test -x /opt/smartapp-runtime/renderer/node_modules/electron/dist/electron
test -S /tmp/mpv-socket
```
