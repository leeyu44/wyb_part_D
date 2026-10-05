#!/bin/bash
# Build a reproducible, network-independent MemHall .deb on openKylin.
set -Eeuo pipefail
umask 022

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${SRC:-$ROOT}"
DIST="${DIST:-$SRC/dist}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1791072000}"
if [[ ! "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]]; then
  echo "SOURCE_DATE_EPOCH must be an integer" >&2
  exit 2
fi
export SOURCE_DATE_EPOCH PYTHONHASHSEED=0 TZ=UTC LC_ALL=C.UTF-8

for command in python3 dpkg dpkg-deb sha256sum find sort xargs touch awk grep uname wc; do
  command -v "$command" >/dev/null || {
    echo "missing build command: $command" >&2
    exit 2
  }
done

UV="${UV:-$(command -v uv || true)}"
if [[ -z "$UV" && -x "$HOME/.hermes/bin/uv" ]]; then
  UV="$HOME/.hermes/bin/uv"
fi
if [[ -z "$UV" ]]; then
  echo "uv is required to export the frozen uv.lock (set UV=/path/to/uv)" >&2
  exit 2
fi

UV_REQUIRED_VERSION="${UV_REQUIRED_VERSION:-0.12.16}"
UV_VERSION="$("$UV" --version | awk '{print $2}')"
if [[ "$UV_VERSION" != "$UV_REQUIRED_VERSION" ]]; then
  echo "uv $UV_REQUIRED_VERSION is required (found $UV_VERSION at $UV)" >&2
  exit 2
fi

VERSION="$(python3 - "$SRC/pyproject.toml" <<'PY'
import sys
import tomllib
with open(sys.argv[1], "rb") as stream:
    print(tomllib.load(stream)["project"]["version"])
PY
)"
ARCHITECTURE="${DEB_ARCH:-$(dpkg --print-architecture)}"
if [[ ! "$ARCHITECTURE" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "invalid Debian architecture: $ARCHITECTURE" >&2
  exit 2
fi
PACKAGE="memhall_${VERSION}_${ARCHITECTURE}"
BUILD_ROOT="${BUILD_ROOT:-$(mktemp -d)}"
KEEP_BUILD="${KEEP_BUILD:-0}"
cleanup() {
  if [[ "$KEEP_BUILD" != "1" ]]; then
    rm -rf -- "$BUILD_ROOT"
  fi
}
trap cleanup EXIT

WHEELS="$BUILD_ROOT/wheels"
STAGE="$BUILD_ROOT/$PACKAGE"
mkdir -p "$WHEELS" "$STAGE/DEBIAN" "$STAGE/usr/bin"
mkdir -p "$STAGE/usr/lib/memhall" "$STAGE/usr/share/memhall/scripts"
mkdir -p "$STAGE/usr/share/applications" "$STAGE/etc/memhall"

cd "$SRC"
"$UV" export --quiet --frozen --no-dev --no-emit-project --no-header --no-annotate \
  --output-file "$BUILD_ROOT/requirements.lock"
python3 -m pip download --quiet --require-hashes --only-binary=:all: \
  --dest "$WHEELS" --requirement "$BUILD_ROOT/requirements.lock"
python3 -m pip wheel --quiet --no-deps --wheel-dir "$WHEELS" "$SRC"

cp -a "$WHEELS" "$STAGE/usr/share/memhall/wheels"
cp -a "$SRC/cases" "$STAGE/usr/share/memhall/cases"
cp -a "$SRC/README.md" "$SRC/LICENSE" "$STAGE/usr/share/memhall/"
cp -a "$SRC/.env.example" "$STAGE/etc/memhall/memhall.env.example"
cp -a "$SRC/scripts/judge_selftest.py" "$STAGE/usr/share/memhall/scripts/"

cat > "$STAGE/usr/bin/memhall" <<'EOF'
#!/bin/sh
export PYTHONPATH=/usr/lib/memhall/pylib${PYTHONPATH:+:$PYTHONPATH}
export MPLCONFIGDIR="${XDG_CACHE_HOME:-$HOME/.cache}/memhall/matplotlib"
exec python3 -c 'from memhall.cli import main; main()' "$@"
EOF
chmod 0755 "$STAGE/usr/bin/memhall"

cat > "$STAGE/usr/share/applications/memhall.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=麟阁 MemHall
Comment=智能体长期记忆评测
Exec=memhall ui
Icon=utilities-system-monitor
Terminal=false
Categories=Development;Utility;
Keywords=AI;Agent;Benchmark;Memory;
EOF

cat > "$STAGE/DEBIAN/control" <<EOF
Package: memhall
Version: $VERSION
Architecture: $ARCHITECTURE
Maintainer: MemHall Team <memhall@openkylin.example>
Depends: python3 (>= 3.11), python3-pip, fonts-noto-cjk
Recommends: auditd, openssh-client
Section: utils
Priority: optional
Homepage: https://gitee.com/mazhuoran23/MemHall
Description: MemHall agent long-term memory evaluation for openKylin
 Runs scripted teach-confound-probe scenarios, records dialogue, memory,
 action and filesystem evidence, then produces deterministic metrics and reports.
 The package includes all Python wheels required for an offline installation.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -eu
case "${1:-configure}" in
  configure)
    mkdir -p /usr/lib/memhall
    target="$(mktemp -d /usr/lib/memhall/.pylib.XXXXXX)"
    trap 'rm -rf "$target"' EXIT
    PIP_BREAK_SYSTEM_PACKAGES=1 python3 -m pip install \
      --quiet --no-index --no-deps --disable-pip-version-check \
      --target "$target" /usr/share/memhall/wheels/*.whl
    chmod -R a+rX "$target"
    rm -rf /usr/lib/memhall/pylib
    mv "$target" /usr/lib/memhall/pylib
    trap - EXIT
    PYTHONPATH=/usr/lib/memhall/pylib python3 -c \
      'import memhall, pydantic, yaml, matplotlib, paramiko, fastapi; print("memhall", memhall.__version__)'
    ;;
esac
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -eu
case "${1:-}" in
  remove|purge)
    rm -rf /usr/lib/memhall
    ;;
esac
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# dpkg records mtimes, so normalize every staged file before compression.
find "$STAGE" -print0 | xargs -0 touch --no-dereference --date="@$SOURCE_DATE_EPOCH"
mkdir -p "$DIST"
DEB="$DIST/$PACKAGE.deb"
rm -f -- "$DEB"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB"
touch --date="@$SOURCE_DATE_EPOCH" "$DEB"

dpkg-deb --info "$DEB" >/dev/null
dpkg-deb --contents "$DEB" > "$BUILD_ROOT/deb.contents"
grep -F './usr/bin/memhall' "$BUILD_ROOT/deb.contents" >/dev/null
DEB_SHA256="$(sha256sum "$DEB" | awk '{print $1}')"
WHEELSET_SHA256="$(
  cd "$WHEELS"
  find . -maxdepth 1 -type f -name '*.whl' -printf '%P\0' |
    sort -z |
    xargs -0 sha256sum |
    sha256sum |
    awk '{print $1}'
)"
WHEEL_COUNT="$(find "$WHEELS" -maxdepth 1 -type f -name '*.whl' | wc -l | awk '{print $1}')"
PYTHON_VERSION="$(python3 -c 'import platform; print(platform.python_version())')"
cat > "$DIST/$PACKAGE.build.json" <<EOF
{
  "package": "memhall",
  "version": "$VERSION",
  "architecture": "$ARCHITECTURE",
  "build_machine": "$(uname -m)",
  "python_version": "$PYTHON_VERSION",
  "uv_version": "$UV_VERSION",
  "source_date_epoch": $SOURCE_DATE_EPOCH,
  "uv_lock_sha256": "$(sha256sum "$SRC/uv.lock" | awk '{print $1}')",
  "wheel_count": $WHEEL_COUNT,
  "wheelset_sha256": "$WHEELSET_SHA256",
  "deb_sha256": "$DEB_SHA256"
}
EOF
touch --date="@$SOURCE_DATE_EPOCH" "$DIST/$PACKAGE.build.json"

echo "$DEB"
echo "sha256=$DEB_SHA256"
