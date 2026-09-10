#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if curl -fsS http://127.0.0.1:8091/api/status >/dev/null 2>&1; then
  echo '服务已经运行：http://192.168.123.99:8091'
  exit 0
fi
if [[ ! -S /tmp/foo_jpeg ]]; then
  (
    set +u
    source /opt/ros/foxy/setup.bash
    source /usr/bin/cmcc_robot/install/setup.bash
    export ROS_DOMAIN_ID=2 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    timeout 20 ros2 service call /video_gst/get_video_service homi_speech_interface/srv/GetVideoStream '{resolution: jpeg}'
  )
  [[ -S /tmp/foo_jpeg ]] || { echo '额头相机 JPEG 流尚未开启'; exit 1; }
fi
nohup /usr/bin/python3 server.py >server.log 2>&1 </dev/null &
echo $! >server.pid
sleep 2
kill -0 "$(cat server.pid)"
echo '已启动：http://192.168.123.99:8091'
