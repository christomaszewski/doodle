#!/usr/bin/env bash
# rig's BUILD-phase entrypoint (rigging.yaml `build.command`): build the runtime image and push it to
# the fleet registry. rig invokes it as
#   tools/build_image.sh <registry> [tag]        (cwd = repo root, tag defaults to latest)
# and the deploy compose pulls the result as ${RIG_IMAGE_REGISTRY}/doodle_radio:${RIG_IMAGE_TAG}.
#
# rig build-env contract (rig exports these; all are set-or-popped, so the expansions below are safe
# under set -u):
#   RIG_BUILD_NO_CACHE  set => --no-cache --pull (a deliberate rebuild re-pulls the parent image)
#   ROS_DISTRO          fleet distro; default lyrical
#   RIG_BASE_IMAGE      the deployment's shared runtime base; absent => standalone ros-core parent
#
# RIG_BUILD_PLATFORMS selects Rig's scoped CPU list (linux/arm64,linux/amd64); Buildx publishes
# one runtime-independent index and reports its digest through RIG_BUILD_RESULT.
# Standalone: DOODLE_BUILD_PLATFORM=linux/<arch> selects a CPU (default: host-native).
# PUSH=0 keeps standalone builds local; a Buildx --load needs exactly one CPU.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/doodle_radio:${TAG}"

# Rig's selected CPUs take precedence over a standalone override. Keep each argument intact.
set --
if [ -z "${RIG_BUILD_PLATFORMS:-}" ] && [ -n "${DOODLE_BUILD_PLATFORM:-}" ]; then
  set -- --platform "$DOODLE_BUILD_PLATFORM"
fi
# shellcheck source=tools/rig-build.sh
. "$REPO/tools/rig-build.sh"
rig_build "$IMAGE" "$@" \
  --build-arg "ROS_DISTRO=${ROS_DISTRO:-lyrical}" \
  --build-arg "BASE_IMAGE=${RIG_BASE_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-core}" \
  -f "$REPO/docker/Dockerfile.runtime" "$REPO"
echo "build_image.sh: built $IMAGE (PUSH=${PUSH:-1})" >&2
