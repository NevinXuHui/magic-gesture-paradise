#!/usr/bin/env bash
set -eo pipefail

ROS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROS_SETUP="${ROS_SETUP:-/opt/ros/foxy/setup.bash}"
CYCLONEDDS_SETUP="${CYCLONEDDS_SETUP:-/opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash}"
ROBOT_SETUP="${ROBOT_SETUP:-/usr/bin/cmcc_robot/install/setup.bash}"
LOCAL_SETUP="$ROS_ROOT/install/local_setup.bash"

usage() {
  printf 'Usage: %s [hermes|openclaw|node|chat|test] [--check] [-- ROS arguments]\n' "$0"
}

if (($#)) && [[ "$1" == -h || "$1" == --help ]]; then
  usage
  exit 0
fi

target=hermes
check=0
if (($#)) && [[ "$1" != --* ]]; then
  target="$1"
  shift
fi
case "$target" in
  hermes) executable=hermes_bridge; config=hermes_bridge.yaml ;;
  openclaw) executable=openclaw_bridge; config=openclaw_bridge.yaml ;;
  node) executable=claw_client_node; config= ;;
  chat) executable=external_chat; config= ;;
  test) executable=test_send; config= ;;
  *) usage >&2; exit 2 ;;
esac
if (($#)) && [[ "$1" == --check ]]; then
  check=1
  shift
fi
if (($#)) && [[ "$1" == -- ]]; then
  shift
fi

for setup in "$ROS_SETUP" "$CYCLONEDDS_SETUP" "$ROBOT_SETUP" "$LOCAL_SETUP"; do
  if [[ ! -f "$setup" ]]; then
    printf 'Setup not found: %s\n' "$setup" >&2
    if [[ "$setup" == "$LOCAL_SETUP" ]]; then
      printf 'Run %s/build.sh first\n' "$ROS_ROOT" >&2
    fi
    exit 1
  fi
  source "$setup"
done

export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_DOMAIN_ID=2
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'

local_executable="$ROS_ROOT/install/claw_client/lib/claw_client/$executable"
if [[ ! -x "$local_executable" ]]; then
  printf 'Local executable not found: %s\nRun %s/build.sh first\n' \
    "$local_executable" "$ROS_ROOT" >&2
  exit 1
fi

if [[ "$target" == hermes || "$target" == openclaw ]]; then
  if existing_pid="$(pgrep -x -o "$executable")"; then
    state="$(ps -o stat= -p "$existing_pid" 2>/dev/null)"
    if [[ "$state" == T* ]]; then
      parent_pid="$(ps -o ppid= -p "$existing_pid" | tr -d '[:space:]')"
      if [[ -r "/proc/$parent_pid/cmdline" ]] \
          && tr '\0' '\n' < "/proc/$existing_pid/cmdline" | rg -Fqx -- "$local_executable" \
          && [[ "$(tr '\0' ' ' < "/proc/$parent_pid/cmdline")" == *"/ros2 run claw_client $executable"* ]]; then
        if ((check)); then
          printf '%s (PID %s) is a suspended local instance; a normal run will replace it\n' \
            "$executable" "$existing_pid" >&2
          exit 1
        fi
        printf 'Stopping suspended local %s (PID %s)\n' "$executable" "$existing_pid"
        kill -TERM "$existing_pid" "$parent_pid" 2>/dev/null || true
        kill -CONT "$existing_pid" "$parent_pid" 2>/dev/null || true
        for ((attempt=0; attempt<50; attempt++)); do
          if ! kill -0 "$existing_pid" 2>/dev/null; then
            break
          fi
          sleep 0.1
        done
        if pgrep -x "$executable" >/dev/null 2>&1; then
          printf '%s did not exit; stop PID %s before retrying\n' \
            "$executable" "$existing_pid" >&2
          exit 1
        fi
      else
        printf '%s (PID %s) is suspended; stop it in its original terminal before retrying\n' \
          "$executable" "$existing_pid" >&2
        exit 1
      fi
    else
      printf '%s is already running (PID %s); stop the existing instance first\n' \
        "$executable" "$existing_pid" >&2
      exit 1
    fi
  fi
fi

cmd=(ros2 run claw_client "$executable")
if [[ -n "$config" ]]; then
  config_path="$ROS_ROOT/install/claw_client/share/claw_client/config/$config"
  if [[ ! -f "$config_path" ]]; then
    printf 'Config not found: %s\n' "$config_path" >&2
    exit 1
  fi
  cmd+=(--ros-args --params-file "$config_path")
fi
cmd+=("$@")

printf 'Command: '
printf '%q ' "${cmd[@]}"
printf '\n'
if ((check)); then
  exit 0
fi
exec "${cmd[@]}"
