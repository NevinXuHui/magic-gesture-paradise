#!/usr/bin/env bash
set -eo pipefail

ROS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROS_SETUP="${ROS_SETUP:-/opt/ros/foxy/setup.bash}"
INSTALL_DIR="${ROS_ROOT}/install"
BUILD_DIR="${ROS_ROOT}/build"
LOG_DIR="${ROS_ROOT}/log"

usage() {
  printf 'Usage: %s [--package all|interface|client] [--no-symlink]\n' "$0"
}

package=all
symlink=1
while (($#)); do
  case "$1" in
    --package)
      if (($# < 2)); then usage >&2; exit 2; fi
      package="$2"
      shift 2
      ;;
    --no-symlink) symlink=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

case "$package" in
  all) packages=(homi_speech_interface claw_client) ;;
  interface) packages=(homi_speech_interface) ;;
  client) packages=(claw_client) ;;
  *) usage >&2; exit 2 ;;
esac

if [[ ! -f "$ROS_SETUP" ]]; then
  printf 'ROS setup not found: %s\n' "$ROS_SETUP" >&2
  exit 1
fi
source "$ROS_SETUP"
if ! command -v colcon >/dev/null 2>&1; then
  printf 'colcon is not installed\n' >&2
  exit 1
fi

if [[ "$package" == client && ! -f "$INSTALL_DIR/local_setup.bash" ]]; then
  printf 'Build the interface first: %s --package interface\n' "$0" >&2
  exit 1
fi
if [[ "$package" == client ]]; then
  source "$INSTALL_DIR/local_setup.bash"
fi

args=(--log-base "$LOG_DIR" build
  --base-paths "$ROS_ROOT"
  --build-base "$BUILD_DIR"
  --install-base "$INSTALL_DIR"
  --packages-select "${packages[@]}"
  --cmake-args -DBUILD_TESTING=OFF)
if ((symlink)); then
  args+=(--symlink-install)
fi

printf 'Building: %s\n' "${packages[*]}"
colcon "${args[@]}"
printf 'Installed to: %s\n' "$INSTALL_DIR"
