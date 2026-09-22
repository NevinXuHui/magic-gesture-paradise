# 拳拳超人：剪刀石头布

面向儿童的 800 x 480 手势识别 H5 游戏。前端使用 Vue 3，约 8 MB 的 MediaPipe GestureRecognizer 模型由独立 Python 进程加载并使用 CPU / XNNPACK 推理。浏览器保留主手跟踪、摇拳停稳和游戏动画，不再加载 WASM 模型。

正式部署采用 SmartApp Runtime 的 `Web+Python` Hybrid 应用模式：

```text
SmartApp Runtime :18080 -> H5 游戏（动画、摇拳/停稳、结果上报）
                            | /api/recognition（关键点、分数）
                            v
                   backend/main.py :18081
                            | Python MediaPipe CPU
                            v
                       摄像头共享流
```

正式模式无需把 JPEG 视频送入 Electron；只有打开调试窗时才返回与关键点同步的 JPEG 预览。

Runtime 负责应用安装、静态服务、backend 生命周期和实体屏显示。Electron、FFmpeg、MPV 和表情恢复实现已经迁移到 `smartapp-runtime/renderer/`，不再由 H5 项目或应用包启动。

## 本地 PC / Mac 运行

建议 Python 3.11。首次安装和构建：

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci
npm run build
bash start-python.sh
```

打开 http://127.0.0.1:5174/?debug=1 ，允许浏览器摄像头权限。浏览器每次只上传一帧 JPEG 到本机 `/api/infer`，Python 返回与旧识别器格式兼容的关键点和分类。图像不落盘，不上传云端。关闭相机取消后续推理，R 重连。每次使用一个游戏页面。

`start-local.sh` 也调用 Python 推理服务。修改前端后重新构建并刷新即可。开发时先保持 Python 服务运行在 5174，再在另一个终端运行 `npm run dev -- --port 5175`，Vite 代理 `/api` 到 Python。

需要 HTTPS 时执行 `bash start-https.sh`（通过 `LOCAL_IP` 指定局域网 IP）。自签名证书需浏览器信任。

## Python 依赖与机器狗部署

本次复用已有模型、双手检测和 0.5 检测/跟踪阈值，保留已经调试的剪刀几何评分。模型约 8 MB，Python SDK 的安装体积另计。

SmartApp 包携带 `backend/main.py`、`backend/inference.py`、`backend/requirements.txt` 和 `backend/models/gesture_recognizer.task`；不会打包本机 `.venv`。Runtime 启动 backend 所用的 Python 必须预先具备兼容的 MediaPipe 0.10.18、NumPy 1.x 和 **带 GStreamer 的系统 OpenCV**。PC `requirements.txt` 的 pip OpenCV wheel 不可直接覆盖狗端系统 cv2，否则共享内存采集会失效。

`backend/requirements.txt` 是模型依赖清单；MediaPipe 的传递依赖包含 pip OpenCV，所以机器人部署应由系统镜像统一准备并验证依赖，而不是在应用启动时直接 pip 安装。请在 Runtime 的同一个 Python 环境检查 `import mediapipe, cv2` 和 `cv2.getBuildInformation()` 中的 GStreamer 支持。ARM64 / Ubuntu 24.04 的实际安装与速度仍需目标设备验证；PC 验证不代表 RK3588 性能保证，本实现不使用 NPU。

## 测试

```bash
npm test
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

测试覆盖胜负判断、摇拳触发、静止过滤、回合切换和主手跟踪。

## SmartApp 封装

```bash
npm run build:smartapp
```

默认生成：

```text
build/smartapp/rock_paper_scissors-0.1.7.tar.gz
```

应用标识、版本和组件入口由 [manifest.json](manifest.json) 定义。脚本会执行 SmartApp 专用前端构建、目录组装和 Manifest 校验，并输出 `packageSize` 与 `sha256`。

包内结构：

```text
rock_paper_scissors/
├── manifest.json
├── web/
│   └── index.html
└── backend/
    ├── main.py
    ├── inference.py
    ├── requirements.txt
    └── models/gesture_recognizer.task
```

详细协议、运行要求和发布步骤见 [SmartApp 封装指南](docs/SMARTAPP_PACKAGE.md)。

## 游戏结果上报

SmartApp 模式下，每局进入结果阶段时，H5 通过 `window.smartApp.postMessage` 向 Runtime 上报一次：

```json
{"event":"app_data","dataType":"game_result","data":{"user":"fist","computer":"peace","outcome":"win","rounds":1}}
```

手势值为 `fist`（石头）、`peace`（剪刀）、`palm`（布）；结果值为玩家视角的 `win`、`lose`、`draw`。Runtime Agent 客户端可通过 `test_client.sh rps listen` 持续订阅。

## 项目结构

```text
backend/main.py                 # Runtime 管理的摄像头 + Python 推理动态服务
backend/inference.py            # PC/机器狗共用原生模型
server.py                      # PC 静态页面与推理接口
docs/                           # 项目文档
public/models/                  # MediaPipe 模型
public/vendor/                  # 旧 WASM 资源，不加载且不进入 SmartApp 包
scripts/package-smartapp.sh     # SmartApp 组包脚本
scripts/validate-smartapp.mjs   # 包结构校验
src/                            # H5 源码
manifest.json                   # SmartApp Runtime v1 清单
```

素材授权说明见 [public/art/GENERATED-ASSETS.md](public/art/GENERATED-ASSETS.md) 和 [public/art/LICENSE-TWEMOJI.txt](public/art/LICENSE-TWEMOJI.txt)，模型信息见 [public/MODEL_INFO.json](public/MODEL_INFO.json)。
