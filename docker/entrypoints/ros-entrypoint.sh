#!/usr/bin/env bash
# Sources the ROS environment, applies the launcher-rendered Zenoh session overrides (rmw_zenoh
# only), then execs the given command.
#
# NOTE: ROS setup.bash references variables that may be unset, so no `set -u` around the sourcing.
set -eo pipefail

ROS_DISTRO="${ROS_DISTRO:-lyrical}"
ROS_SETUP="/opt/ros/${ROS_DISTRO}/setup.bash"
if [[ -f "${ROS_SETUP}" ]]; then
  # shellcheck disable=SC1090
  source "${ROS_SETUP}"
fi

# Config `zenoh:` block -> DOODLE_ZENOH_OVERRIDE ("path=json;..."). Appended after any
# deployment-level ZENOH_CONFIG_OVERRIDE so the config's pairs win; inert under Fast DDS.
if [[ "${RMW_IMPLEMENTATION:-}" == "rmw_zenoh_cpp" && -n "${DOODLE_ZENOH_OVERRIDE:-}" ]]; then
  export ZENOH_CONFIG_OVERRIDE="${ZENOH_CONFIG_OVERRIDE:+${ZENOH_CONFIG_OVERRIDE};}${DOODLE_ZENOH_OVERRIDE}"
  echo "ros-entrypoint: ZENOH_CONFIG_OVERRIDE=${ZENOH_CONFIG_OVERRIDE}" >&2
fi

exec "$@"
