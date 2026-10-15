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
# Cross-arch vehicle: export DOODLE_BUILD_PLATFORM=linux/<arch> (default: host-native). The image has
# no compiled code, so building under qemu emulation is practical.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

REGISTRY="${1:?usage: build_image.sh <registry> [tag]}"
TAG="${2:-latest}"
IMAGE="${REGISTRY}/doodle_radio:${TAG}"

docker build ${DOODLE_BUILD_PLATFORM:+--platform "${DOODLE_BUILD_PLATFORM}"} \
  ${RIG_BUILD_NO_CACHE:+--no-cache} ${RIG_BUILD_NO_CACHE:+--pull} \
  --build-arg "ROS_DISTRO=${ROS_DISTRO:-lyrical}" \
  --build-arg "BASE_IMAGE=${RIG_BASE_IMAGE:-ros:${ROS_DISTRO:-lyrical}-ros-core}" \
  -f "$REPO/docker/Dockerfile.runtime" -t "$IMAGE" "$REPO"
docker push "$IMAGE"
echo "build_image.sh: pushed $IMAGE" >&2
