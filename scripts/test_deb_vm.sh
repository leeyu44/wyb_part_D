#!/bin/bash
# Install a built package on a clean openKylin VM and exercise the offline path.
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /path/to/memhall_VERSION_ARCH.deb" >&2
  exit 2
fi
DEB="$(readlink -f "$1")"
[[ -f "$DEB" ]] || { echo "deb not found: $DEB" >&2; exit 2; }
META="${DEB%.deb}.build.json"
[[ -f "$META" ]] || { echo "build metadata not found: $META" >&2; exit 2; }
EXPECTED_SHA256="$(python3 - "$META" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["deb_sha256"])
PY
)"
ACTUAL_SHA256="$(sha256sum "$DEB" | awk '{print $1}')"
if [[ "$EXPECTED_SHA256" != "$ACTUAL_SHA256" ]]; then
  echo "deb SHA-256 does not match build metadata" >&2
  exit 2
fi
PACKAGE_ARCH="$(dpkg-deb -f "$DEB" Architecture)"
HOST_ARCH="$(dpkg --print-architecture)"
if [[ "$PACKAGE_ARCH" != "$HOST_ARCH" ]]; then
  echo "package architecture $PACKAGE_ARCH does not match host $HOST_ARCH" >&2
  exit 2
fi
REPORT="${REPORT:-/tmp/memhall-deb-acceptance.txt}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

{
  echo "deb=$DEB"
  echo "deb_sha256=$ACTUAL_SHA256"
  echo "build_metadata=$META"
  echo "started_at=$(date --iso-8601=seconds)"
  "${SUDO[@]}" dpkg -i "$DEB"
  memhall --version
  cd "$WORK"
  memhall run --adapter mock --cases cases/quick --out runs --repeat 2 --seed 42
  mapfile -t RUNS < <(find runs -mindepth 2 -maxdepth 2 -type f \
    -name manifest.json -printf '%h\n' | sort)
  [[ "${#RUNS[@]}" -eq 2 ]] || {
    echo "expected 2 completed runs, found ${#RUNS[@]}" >&2
    exit 1
  }
  for run in "${RUNS[@]}"; do
    memhall verify "$run"
  done
  test -s runs/*-stability-mock/stability.json
  echo "package_status=$(dpkg-query -W -f='${Status}' memhall)"
  echo "finished_at=$(date --iso-8601=seconds)"
  echo "result=PASS"
} 2>&1 | tee "$REPORT"

echo "acceptance report: $REPORT"
