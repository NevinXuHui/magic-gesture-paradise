#!/usr/bin/env bash
set -eo pipefail

[[ -f /opt/ros/foxy/setup.bash ]] || exit 0
[[ -f /usr/bin/cmcc_robot/install/setup.bash ]] || exit 0

set +u
source /opt/ros/foxy/setup.bash
source /usr/bin/cmcc_robot/install/setup.bash
set -u
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-2}"
timeout 8 ros2 service call \
  /expression/config \
  homi_speech_interface/srv/ExpressionConfig \
  "{action: set, default_video: '', default_image: '', expression_enabled: 'false', status_publish_enabled: 'true'}" \
  >/dev/null
