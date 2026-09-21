# 拳拳超人：剪刀石头布

面向儿童的 800 x 480 手势识别 H5 游戏。前端使用 Vue 3 和 MediaPipe Tasks Vision，在浏览器 Worker 中完成剪刀、石头、布识别。

正式部署采用 SmartApp Runtime 的 `Web+Python` Hybrid 应用模式：

```text
SmartApp Runtime :18080
        |
        v
      H5 页面 ----------------------+
        |                           |
        | JPEG                      | MediaPipe WASM
        v                           v
backend/main.py :18081 -> 摄像头共享流 -> 游戏识别与交互
```

Runtime 负责应用安装、静态服务、backend 生命周期和实体屏显示。Electron、FFmpeg、MPV 和表情恢复实现已经迁移到 `smartapp-runtime/renderer/`，不再由 H5 项目或应用包启动。

## 本地开发

```bash
npm ci
npm run dev -- --port 5174
```

打开 `http://localhost:5174/`，由浏览器使用本机摄像头。首次访问需要允许摄像头权限。

生产构建和本地静态预览：

```bash
npm run build
./start-local.sh
```

需要通过局域网访问本机摄像头时，可以执行 `./start-https.sh`，并在浏览器接受本地自签名证书。

## 测试

```bash
npm test
```

测试覆盖胜负判断、摇拳触发、静止过滤、回合切换和主手跟踪。

## SmartApp 封装

```bash
npm run build:smartapp
```

默认生成：

```text
build/smartapp/rock_paper_scissors-0.1.0.tar.gz
```

应用标识、版本和组件入口由 [manifest.json](manifest.json) 定义。脚本会执行 SmartApp 专用前端构建、目录组装和 Manifest 校验，并输出 `packageSize` 与 `sha256`。

包内结构：

```text
rock_paper_scissors/
├── manifest.json
├── web/
│   └── index.html
└── backend/
    └── main.py
```

详细协议、运行要求和发布步骤见 [SmartApp 封装指南](docs/SMARTAPP_PACKAGE.md)。

## 项目结构

```text
backend/main.py                 # Runtime 管理的摄像头动态服务
docs/                           # 项目文档
public/models/                  # MediaPipe 模型
public/vendor/                  # MediaPipe WASM 运行资源
scripts/package-smartapp.sh     # SmartApp 组包脚本
scripts/validate-smartapp.mjs   # 包结构校验
src/                            # H5 源码
manifest.json                   # SmartApp Runtime v1 清单
```

素材授权说明见 [public/art/GENERATED-ASSETS.md](public/art/GENERATED-ASSETS.md) 和 [public/art/LICENSE-TWEMOJI.txt](public/art/LICENSE-TWEMOJI.txt)，模型信息见 [public/MODEL_INFO.json](public/MODEL_INFO.json)。
