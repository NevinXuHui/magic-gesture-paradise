#!/usr/bin/env bash
set -eo pipefail

[[ -f /opt/ros/foxy/setup.bash ]] || exit 0
[[ -f /usr/bin/cmcc_robot/install/setup.bash ]] || exit 0

unset COLCON_PREFIX_PATH AMENT_PREFIX_PATH CMAKE_PREFIX_PATH AMENT_CURRENT_PREFIX
set +u
source /opt/ros/foxy/setup.bash
source /usr/bin/cmcc_robot/install/setup.bash
if [[ -f /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash ]]; then
  source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
fi
set -u
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-2}"
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
timeout 8 ros2 service call \
  /expression/config \
  homi_speech_interface/srv/ExpressionConfig \
  "{action: set, default_video: '', default_image: '', expression_enabled: 'false', status_publish_enabled: 'true'}" \
  >/dev/null
