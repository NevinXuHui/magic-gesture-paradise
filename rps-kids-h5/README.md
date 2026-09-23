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

机器人相机通过 `start_app.initData.cameraSource` 选择：`forehead` 使用额头 JPEG 共享流 `/tmp/foo_jpeg`，`neck` 使用领结 JPEG 共享流 `/tmp/neck_jpeg`。缺省为 `neck`；显式选择后不会在两个相机之间自动回退。额头流需要原厂 `/video_gst/get_video_service` 预先以 `{resolution: jpeg}` 开启。

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

SmartApp 0.1.11 包携带 `backend/main.py`、`backend/inference.py`、`backend/models/gesture_recognizer.task` 和 `backend/vendor/`。vendor 是 CPython 3.8 / Linux ARM64 的 MediaPipe 0.10.9 及精简依赖，不包含 Python 解释器或 OpenCV；机器人需要自带 **带 GStreamer 的系统 OpenCV**。Mac/PC 仍使用 MediaPipe 0.10.18。PC `requirements.txt` 的 pip OpenCV wheel 不可覆盖狗端系统 cv2，否则共享内存采集会失效。

机器人依赖锁定在 `backend/requirements-robot.txt`。构建脚本下载匹配 Python 3.8 的 ARM64 wheels 并离线安装进包内 vendor，启动时无需网络或 pip。可设置 `SMARTAPP_WHEELHOUSE=/path/to/wheels npm run build:smartapp` 从本地轮子构建。请在狗端确认 `/usr/bin/python3` 为 3.8.10、`cv2.getBuildInformation()` 中 GStreamer 为 YES，并用实际摄像头流验收。PC 构建无法验证 RK3588 上的 OpenCV/NumPy ABI 和推理速度；本实现使用 CPU，不使用 NPU。

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
build/smartapp/rock_paper_scissors-0.1.11.tar.gz
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

## 精简 PC 推理环境（独立试用）

完整环境保留不动，新建 `.venv-lean`，仅为本项目的 GestureRecognizer 推理安装已验证的依赖。不修改 MediaPipe 源码、不替换模型、不调整识别参数。

```bash
bash scripts/setup-lean.sh
# 若未构建过前端，先 npm ci && npm run build
bash start-lean.sh
```

打开 http://127.0.0.1:5185/?debug=1 。默认使用 5185，以免占用已有 5174 服务。需要指定端口可用 `PORT=5174 bash start-lean.sh`。同时测试两个版本时请先关闭另一个页面的摄像头。

精简清单在 `requirements-lean.txt`，必须通过 `pip install --no-deps -r requirements-lean.txt` 安装，避免 pip 再拉入完整 SDK 依赖。脚本只操作 `.venv-lean`，不会卸载原环境的软件。Python 版本选择可用 `RPS_SETUP_PYTHON=/path/to/python3.11 bash scripts/setup-lean.sh`。

省略 JAX/JAXLIB、SciPy、ml-dtypes、SoundDevice、SentencePiece：它们用于 SDK 的其他功能，当前手势路径不需要。保留 Matplotlib 及其依赖，因为 MediaPipe 0.10.18 顶层导入会加载绘图模块；直接删除会导致启动失败。标准 `pip check` 会报告 MediaPipe 声明的全功能依赖不完整，这是有意限制功能范围；不要以 `pip install mediapipe` 修复，否则会重新安装这些大包。使用 `scripts/check-lean.py` 和本项目真实推理测试验证此专用环境。

本机 macOS ARM64 初次测量：完整环境约 773 MiB，精简环境约 388 MiB，均含 OpenCV、NumPy 和 Python 环境自带工具，不含项目已有的约 8 MiB 模型。省去约 385 MiB（约 50%）；安装后字节码缓存会带来小幅变化。该大小不是机器人 ARM64 Linux 的实测值。此清单含 pip OpenCV，仅用于 PC 测试；狗端必须保留带 GStreamer 的系统 OpenCV，不能直接套用。

验证：`.venv-lean/bin/python -m unittest discover -s tests -p 'test_*.py'`。恢复完整版本只需停止精简服务，使用原来的 Python 环境及启动脚本；无需重新安装原环境。
