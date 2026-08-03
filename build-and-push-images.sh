#!/usr/bin/env bash
# Builds and pushes the 4 service images this project deploys as, one Docker
# Hub repo per service, tagged "home-assistant-<service>" so they're easy to
# find alongside any other project on the same account.
#
# Usage:
#   ./build-and-push-images.sh                      # build + push all 4
#   ./build-and-push-images.sh orchestrator          # build + push just one service
#   ./build-and-push-images.sh orchestrator web-adapter   # or a few, by name
#   ./build-and-push-images.sh --build-only          # skip the push step (all 4, or...)
#   ./build-and-push-images.sh --build-only orchestrator  # ...combine with a service name
#   DOCKERHUB_USER=someoneelse TAG=0.1.0 ./build-and-push-images.sh
#
# Service names match the directory names: telegram-adapter, web-adapter,
# orchestrator, doc-ingestion-worker.
#
# Requires you to already be logged in (docker login) — this script never
# touches your Docker Hub credentials itself.
set -euo pipefail

DOCKERHUB_USER="${DOCKERHUB_USER:-kikeramirez}"
TAG="${TAG:-latest}"

# SERVICE (directory / build arg) : MODULE (Python package, build arg)
ALL_SERVICES=(
  "telegram-adapter:telegram_adapter"
  "web-adapter:web_adapter"
  "orchestrator:orchestrator"
  "doc-ingestion-worker:doc_ingestion_worker"
)

BUILD_ONLY=false
REQUESTED=()
for arg in "$@"; do
  if [[ "$arg" == "--build-only" ]]; then
    BUILD_ONLY=true
  else
    REQUESTED+=("$arg")
  fi
done

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! docker info >/dev/null 2>&1; then
  echo "Error: Docker isn't reachable from this shell (daemon not running, or" >&2
  echo "WSL integration not enabled for this distro in Docker Desktop). Fix that" >&2
  echo "first, then re-run this script." >&2
  exit 1
fi

if [[ "$BUILD_ONLY" == false ]] && ! docker info 2>/dev/null | grep -qi "Username:"; then
  echo "Warning: 'docker info' doesn't show a logged-in Docker Hub user." >&2
  echo "Run 'docker login' first if the push step below fails with 'access denied'." >&2
fi

# No names given -> everything. Names given -> just those, in the order
# ALL_SERVICES declares them (not the order typed), and fail fast on a typo
# instead of silently building nothing for it.
SERVICES=()
if [[ ${#REQUESTED[@]} -eq 0 ]]; then
  SERVICES=("${ALL_SERVICES[@]}")
else
  for entry in "${ALL_SERVICES[@]}"; do
    service="${entry%%:*}"
    for wanted in "${REQUESTED[@]}"; do
      if [[ "$service" == "$wanted" ]]; then
        SERVICES+=("$entry")
      fi
    done
  done
  if [[ ${#SERVICES[@]} -ne ${#REQUESTED[@]} ]]; then
    echo "Error: one or more requested service names don't match a known service." >&2
    echo "Known services: telegram-adapter, web-adapter, orchestrator, doc-ingestion-worker" >&2
    exit 1
  fi
fi

for entry in "${SERVICES[@]}"; do
  service="${entry%%:*}"
  module="${entry##*:}"
  image="${DOCKERHUB_USER}/home-assistant-${service}:${TAG}"

  echo "=== Building ${image} ==="
  docker build -f Dockerfile.service \
    --build-arg SERVICE="${service}" \
    --build-arg MODULE="${module}" \
    -t "${image}" \
    .

  if [[ "$BUILD_ONLY" == false ]]; then
    echo "=== Pushing ${image} ==="
    docker push "${image}"
  fi
done

echo "Done. Images tagged for ${DOCKERHUB_USER} at tag '${TAG}'."
