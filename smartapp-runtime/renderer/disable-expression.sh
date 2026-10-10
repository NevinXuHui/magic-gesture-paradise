#!/usr/bin/env bash
set -eo pipefail

RENDERER_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [[ "${1:-}" != "--serve" ]]; then
  # The warm daemon needs no ROS imports or environment setup in this client.
  if /usr/bin/python3 "$RENDERER_DIR/expression-control.py" request; then
    exit 0
  else
    status=$?
    [[ "$status" == 2 ]] || exit "$status"
  fi
fi

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
if [[ "${1:-}" == "--serve" ]]; then
  exec /usr/bin/python3 "$RENDERER_DIR/expression-control.py" serve
fi
exec timeout 8 /usr/bin/python3 "$RENDERER_DIR/expression-control.py" disable
