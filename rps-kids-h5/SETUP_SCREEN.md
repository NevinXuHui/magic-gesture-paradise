# 手势识别游戏 - 机器狗实体屏幕部署指南

本文档说明如何在机器狗实体屏幕上运行"拳拳超人"手势识别游戏。

## 系统架构

```
服务端摄像头(server.py) → HTTP API (:5174/api/frame)
                              ↓
Xvfb :99 虚拟显示 → Electron Offscreen (加载 H5 800x480)
                              ↓
                    FFmpeg transpose=2 (旋转 90°)
                              ↓
                    FIFO (rawvideo NUT → 480x800)
                              ↓
                    MPV via /tmp/mpv-socket (DRM 直接输出)
                              ↓
                        机器狗液晶屏
```

**关键特性：**
- 不启动新的 MPV 进程，复用系统已有的 MPV socket
- 通过 socket 控制命令接管液晶，避免与表情系统 DRM 冲突
- 自动禁用表情系统，退出时恢复默认表情

---

## 一、前置要求检查

### 1. 系统依赖

```bash
# 进入项目目录
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5

# 检查必需工具
which xvfb ffmpeg socat python3

# 检查 MPV socket（必需）
ls -la /tmp/mpv-socket
```

**如果缺少依赖**，脚本会自动安装（需要 sudo 权限）：
- `xvfb` - X 虚拟帧缓冲
- `ffmpeg` - 视频转换
- `socat` - Socket 通信

### 2. Node.js 和 Electron

```bash
# 检查 Node.js
node --version  # 需要 >= 18.x

# 检查 Electron
ls -la node_modules/electron/dist/electron

# 如果未安装
npm install
```

### 3. 摄像头环境

游戏使用**服务端摄像头**，优先级：
1. 机器狗额头相机 `/tmp/foo_jpeg`（如果存在）
2. USB 摄像头 `/dev/video0`

```bash
# 检查机器狗相机
ls -la /tmp/foo_jpeg

# 或检查 USB 摄像头
ls -la /dev/video0

# 测试服务端摄像头
python3 server.py &
sleep 2
curl http://127.0.0.1:5174/api/status
kill %1
```

---

## 二、构建 H5 应用

**重要：** 必须先构建，生成 `dist/` 目录。

```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5

# 安装依赖（如果尚未安装）
npm install

# 构建生产版本
npm run build

# 验证构建结果
ls -la dist/index.html
```

**构建产物：**
- `dist/index.html` - H5 入口
- `dist/assets/` - JS/CSS 资源
- `dist/models/` - MediaPipe 手势识别模型
- `dist/art/` - 游戏素材（小汪、手势图标等）

---

## 三、启动实体屏模式

### 基础启动

```bash
# 默认配置启动
bash start-screen.sh
```

**启动流程：**
1. 检查并启动服务端摄像头（127.0.0.1:5174）
2. 创建视频 FIFO (`/run/rps-kids-h5/video.nut`)
3. 启动 Xvfb 虚拟显示 (:99)
4. 启动 Electron 加载 H5 游戏
5. 禁用表情系统
6. 通过 MPV socket 接管液晶屏

**成功输出：**
```
启动服务端摄像头：127.0.0.1:5174
启动 Xvfb :99：802x482x24
启动 H5 Electron：http://127.0.0.1:5174/?camera=server&debug=0&screen=1
H5 已交给设备液晶：480x800，30 FPS
Electron 日志：/tmp/rps-kids-h5-electron.log
按 Ctrl-C 退出并尝试恢复默认表情。
```

### 高级配置

通过环境变量自定义：

```bash
# 自定义端口
PORT=8080 bash start-screen.sh

# 修改帧率（默认 30 FPS）
SCREEN_FPS=25 bash start-screen.sh

# 使用不同的 Electron 路径
ELECTRON_BIN=/opt/electron/electron bash start-screen.sh

# 禁用自动安装依赖
AUTO_INSTALL_DEPS=0 bash start-screen.sh

# 自定义 MPV socket 路径（如果系统不是 /tmp/mpv-socket）
MPV_SOCKET=/var/run/mpv.sock bash start-screen.sh

# 启用调试面板
SCREEN_URL="http://127.0.0.1:5174/?camera=server&debug=1&screen=1" bash start-screen.sh
```

**完整环境变量列表：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `5174` | 服务端摄像头端口 |
| `SCREEN_FPS` | `30` | 屏幕输出帧率 |
| `SCREEN_DISPLAY` | `:99` | Xvfb 显示编号 |
| `MPV_SOCKET` | `/tmp/mpv-socket` | MPV IPC socket 路径 |
| `SCREEN_RUNTIME_DIR` | `/run/rps-kids-h5` | 运行时文件目录 |
| `AUTO_INSTALL_DEPS` | `1` | 自动安装缺失依赖 |
| `ELECTRON_BIN` | 自动检测 | Electron 可执行文件路径 |
| `FFMPEG_BIN` | 自动检测 | FFmpeg 可执行文件路径 |
| `XVFB_BIN` | 自动检测 | Xvfb 可执行文件路径 |
| `SOCAT_BIN` | 自动检测 | socat 可执行文件路径 |
| `SCREEN_URL` | 自动生成 | H5 加载 URL |

---

## 四、停止和恢复

### 正常退出

```bash
# 在运行窗口按 Ctrl-C
# 脚本会自动：
# 1. 停止 Electron
# 2. 停止 Xvfb
# 3. 恢复默认表情
```

### 强制停止

```bash
# 如果脚本卡住，手动清理
pkill -f screen-renderer.js
pkill -f "Xvfb :99"

# 手动恢复表情
../../electron/restore_expression.sh
```

### 检查状态

```bash
# 查看运行进程
ps aux | grep -E "(electron|Xvfb|server.py)" | grep -v grep

# 查看 Electron 日志
tail -f /tmp/rps-kids-h5-electron.log

# 查看 Xvfb 日志
tail -f /tmp/rps-kids-h5-xvfb.log

# 检查 MPV 当前播放内容
echo '{"command":["get_property","path"]}' | socat - UNIX-CONNECT:/tmp/mpv-socket
```

---

## 五、常见问题排查

### 1. Electron 未找到

**症状：**
```
未找到 Electron。请先执行 npm install。
```

**解决：**
```bash
npm install electron@44.4.3

# 验证安装
ls -la node_modules/electron/dist/electron
file node_modules/electron/dist/electron  # 应该是 ARM64
```

### 2. 缺少 dist/index.html

**症状：**
```
缺少 dist/index.html，请先运行 npm run build。
```

**解决：**
```bash
npm run build
ls -la dist/index.html
```

### 3. MPV socket 不存在

**症状：**
```
未找到设备 MPV socket：/tmp/mpv-socket
```

**原因：** 机器狗的表情系统未启动

**解决：**
```bash
# 检查表情系统进程
ps aux | grep mpv | grep -v grep

# 检查 socket 文件
ls -la /tmp/mpv-socket

# 如果表情系统正常但 socket 不存在，可能路径不同
# 使用 MPV_SOCKET 环境变量指定正确路径
```

### 4. 摄像头无法访问

**症状：**
```
服务端摄像头未能就绪。
```

**解决：**
```bash
# 检查摄像头设备
ls -la /tmp/foo_jpeg    # 机器狗相机
ls -la /dev/video0      # USB 摄像头

# 测试独立运行服务端
python3 server.py
# 在另一个终端
curl http://127.0.0.1:5174/api/status

# 检查 Python 依赖
python3 -c "import cv2; print('OpenCV OK')"
```

### 5. Xvfb 启动失败

**症状：**
```
Fatal server error: Server is already active for display :99
```

**解决：**
```bash
# 检查冲突进程
ps aux | grep "Xvfb :99"

# 清理锁文件
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# 使用不同的显示编号
SCREEN_DISPLAY=:98 bash start-screen.sh
```

### 6. MPV 未切换到游戏画面

**症状：**
```
MPV 未切换到 H5 FIFO：<empty>
```

**解决：**
```bash
# 检查 FIFO 创建
ls -la /run/rps-kids-h5/video.nut

# 检查 FFmpeg 进程
ps aux | grep ffmpeg | grep video.nut

# 手动测试 MPV 加载
echo '{"command":["loadfile","/run/rps-kids-h5/video.nut","replace"]}' | \
  socat - UNIX-CONNECT:/tmp/mpv-socket
```

### 7. 画面卡顿或帧率低

**原因：** CPU 负载过高或帧率设置不合理

**解决：**
```bash
# 降低帧率（默认 30 FPS）
SCREEN_FPS=20 bash start-screen.sh

# 检查 CPU 使用率
top -p $(pgrep -f screen-renderer.js)

# 查看性能统计
tail -f /tmp/rps-kids-h5-electron.log | grep STATS
```

---

## 六、与云端展示系统的区别

| 特性 | 云端展示 (electron/) | 手势识别 (rps-kids-h5/) |
|------|---------------------|------------------------|
| **页面来源** | 远程 URL 或本地 HTML | 本地构建的 dist/ |
| **摄像头** | 无 | 服务端摄像头 (server.py) |
| **交互方式** | UDP 指令推送内容 | 用户手势识别 |
| **显示编号** | `:98` | `:99` |
| **守护进程** | `cloud_daemon.py` (ROS 集成) | `start-screen.sh` (独立) |
| **端口** | `24021-24023` | `5174` |
| **运行时目录** | `/run/cloud-show/` | `/run/rps-kids-h5/` |

---

## 七、游戏使用说明

### 玩法

1. **识别主手**：握拳上下摇动（完成一次往返），系统会锁定你的主手
2. **等待倒计时**：两侧开始摇拳动画
3. **出手势**：摆出剪刀✌️、石头✊或布✋，停稳片刻
4. **亮牌**：双方同时显示手势，判定胜负
5. **结果**：
   - 胜利 → 播放星星彩纸动画
   - 失败 → 播放鼓励动画
   - 平局 → 播放默契双环动画
6. **下一轮**：约 2.2 秒后自动等待下一轮

### 注意事项

- 手离开画面超过 1.2 秒会取消当前回合
- 保持静止不会重复触发回合
- 一轮等待超过 10 秒自动取消
- 摄像头画面默认隐藏，只显示游戏界面

### 调试模式

启用调试面板：
```bash
SCREEN_URL="http://127.0.0.1:5174/?camera=server&debug=1&screen=1" bash start-screen.sh
```

**调试功能：**
- 显示摄像头画面和手部关键点
- 黄色骨架标记主手
- 调整停稳时间、移动速度阈值等参数
- 记录和导出游戏日志

---

## 八、性能优化

### 1. CPU 绑定

脚本未自动绑定 CPU 核心。如需优化：

```bash
# 绑定到大核 (4-7)
taskset -c 4-7 bash start-screen.sh
```

### 2. 帧率调优

根据设备性能调整：

```bash
# 低端设备：20 FPS
SCREEN_FPS=20 bash start-screen.sh

# 高端设备：30 FPS（默认）
SCREEN_FPS=30 bash start-screen.sh
```

### 3. 内存优化

监控内存使用：
```bash
tail -f /tmp/rps-kids-h5-electron.log | grep STATS
# 输出示例：STATS paint=30.0fps write=29.5fps skip=0.0/s rss=180MB
```

---

## 九、开发调试

### 本地开发

```bash
# 安装依赖
npm install

# 启动开发服务器
npm run dev -- --port 5174

# 在浏览器打开
# http://localhost:5174/?camera=server&debug=1
```

### 测试

```bash
# 运行自动化测试
npm test
```

### 重新构建

```bash
# 构建生产版本
npm run build

# 预览构建结果
npm run preview
```

---

## 十、完整启动流程示例

```bash
# 1. 进入项目目录
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5

# 2. 确保依赖已安装
npm install

# 3. 构建 H5 应用
npm run build

# 4. 检查 MPV socket
ls -la /tmp/mpv-socket

# 5. 启动实体屏模式
bash start-screen.sh

# 6. 观察日志输出，确认成功
# 应该看到：H5 已交给设备液晶：480x800，30 FPS

# 7. 开始游戏！握拳摇动识别主手

# 8. 停止：按 Ctrl-C
# 脚本会自动恢复默认表情
```

---

## 十一、目录结构

```
/mine/Code/ROS/dice/手势识别/rps-kids-h5/
├── src/                          # Vue 3 源代码
│   ├── App.vue                   # 主应用组件
│   ├── lib/
│   │   ├── game.js              # 游戏回合逻辑
│   │   ├── rps.js               # 手势识别（剪刀石头布）
│   │   └── target.js            # 主手跟踪
│   └── ...
├── public/                       # 静态资源
│   ├── models/                   # MediaPipe 模型
│   ├── art/                      # 游戏素材
│   └── vendor/                   # 第三方库
├── dist/                         # 构建输出（需先 npm run build）
│   ├── index.html
│   ├── assets/
│   └── models/
├── package.json                  # Node.js 依赖配置
├── server.py                     # 服务端摄像头 HTTP API
├── screen-renderer.js            # Electron 渲染器（实体屏）
├── start-screen.sh               # 实体屏启动脚本
├── start-server.sh               # 服务端摄像头启动脚本
├── start-local.sh                # 本地浏览器启动脚本
└── node_modules/
    └── electron/                 # Electron 运行时
```

---

## 完成！

现在你的机器狗应该可以运行手势识别游戏了。如有问题，请参考"常见问题排查"章节。

**快速测试命令：**
```bash
cd /mine/Code/ROS/dice/手势识别/rps-kids-h5 && npm run build && bash start-screen.sh
```
