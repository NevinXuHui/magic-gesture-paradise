#!/usr/bin/env bash
set -eo pipefail

unset COLCON_PREFIX_PATH AMENT_PREFIX_PATH CMAKE_PREFIX_PATH AMENT_CURRENT_PREFIX
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-2}"
source /opt/ros/foxy/setup.bash
source /usr/bin/cmcc_robot/install/setup.bash
if [[ -f /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash ]]; then
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
fi
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
set -u

expression_root=/usr/bin/cmcc_robot/install/expression/share/expression/resource
default_video="${expression_root}/video/default/default.mp4"
mpv_socket=${MPV_SOCKET:-/tmp/mpv-socket}

mpv_command() {
  printf '%s\n' "$1" | socat - "UNIX-CONNECT:${mpv_socket}" >/dev/null
}

restore_once() {
  [[ -S "$mpv_socket" ]] || return 1
  ros2 service call \
    /expression/config \
    homi_speech_interface/srv/ExpressionConfig \
    "{action: set, default_video: '${default_video}', default_image: '', expression_enabled: 'true', status_publish_enabled: 'true'}" \
    >/dev/null 2>&1 || return 1
  mpv_command '{"command":["set_property","cache",true]}' || return 1
  mpv_command '{"command":["set_property","demuxer-readahead-secs",1]}' || return 1
  mpv_command '{"command":["vf","set","fps=15"]}' || return 1
  mpv_command "{\"command\":[\"loadfile\",\"${default_video}\",\"replace\"]}" || return 1
  mpv_command '{"command":["set_property","loop-file","inf"]}' || return 1
  mpv_command '{"command":["set_property","pause",false]}' || return 1
}

for attempt in {1..30}; do
  if restore_once; then
    exit 0
  fi
  sleep 1
done

echo "恢复默认表情失败: MPV/ROS 服务在 30 秒内未就绪" >&2
exit 1
