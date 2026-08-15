#!/usr/bin/env bash
# Build Zoom Meeting SDK raw-data demo and install Multistream's recorder wrapper.
#
# Expects the proprietary SDK tarball at:
#   /opt/multistream/src/zoom-meeting-sdk-linux_x86_64-*.tar.xz
#
# Installs:
#   /opt/multistream/zoom-sdk/sample/demo/bin/meetingSDKDemo
#   /opt/multistream/bin/zoom-sdk-recorder
#   /opt/multistream/bin/zoom-sdk-setup-audio.sh
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

SRC_DIR="${ZOOM_SDK_SRC_DIR:-/opt/multistream/src}"
SDK_ROOT="${ZOOM_SDK_ROOT:-/opt/multistream/zoom-sdk}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/zoom-sdk-recorder.sh" ]]; then
  REPO_ZOOM_SDK="$SCRIPT_DIR"
elif [[ -f /opt/multistream/zoom-sdk/scripts/zoom-sdk-recorder.sh ]]; then
  REPO_ZOOM_SDK=/opt/multistream/zoom-sdk/scripts
elif [[ -f /home/multistream/MultiStreamCustom/vm/zoom-sdk/zoom-sdk-recorder.sh ]]; then
  REPO_ZOOM_SDK=/home/multistream/MultiStreamCustom/vm/zoom-sdk
else
  echo "Cannot find zoom-sdk-recorder.sh next to build script" >&2
  exit 1
fi
SAMPLE_URL="${ZOOM_SDK_SAMPLE_URL:-https://github.com/zoom/meetingsdk-linux-raw-recording-sample.git}"

echo "==> Installing build / runtime packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  build-essential cmake pkg-config git xz-utils ca-certificates \
  libcurl4-openssl-dev openssl ffmpeg libswscale-dev \
  libx11-xcb1 libxcb-xfixes0 libxcb-shape0 libxcb-shm0 \
  libxcb-randr0 libxcb-image0 libxcb-keysyms1 libxcb-xtest0 \
  libdbus-1-3 libglib2.0-0 libgbm1 libxfixes3 libgl1 libdrm2 \
  libgssapi-krb5-2 libegl-mesa0 libsdl2-dev \
  pulseaudio pulseaudio-utils dbus

TARBALL="$(ls -1 "$SRC_DIR"/zoom-meeting-sdk-linux_x86_64-*.tar.xz 2>/dev/null | head -1 || true)"
if [[ -z "$TARBALL" ]]; then
  echo "No SDK tarball in $SRC_DIR" >&2
  exit 1
fi
echo "==> Using SDK tarball: $TARBALL"

echo "==> Extracting SDK"
rm -rf "$SDK_ROOT/sdk-extract"
mkdir -p "$SDK_ROOT/sdk-extract"
tar -xJf "$TARBALL" -C "$SDK_ROOT/sdk-extract"
# SDK layouts vary: sometimes a single top-level folder, sometimes flat.
SDK_TOP="$SDK_ROOT/sdk-extract"
if [[ "$(find "$SDK_TOP" -mindepth 1 -maxdepth 1 -type d | wc -l)" -eq 1 ]]; then
  SDK_TOP="$(find "$SDK_TOP" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
echo "SDK top: $SDK_TOP"
ls -la "$SDK_TOP" | head -30

echo "==> Cloning Zoom raw-recording sample"
rm -rf "$SDK_ROOT/sample"
git clone --depth 1 "$SAMPLE_URL" "$SDK_ROOT/sample"
DEMO="$SDK_ROOT/sample/demo"

echo "==> Copying SDK headers and libs into sample/demo"
mkdir -p "$DEMO/include" "$DEMO/lib/zoom_meeting_sdk"
# Headers
if [[ -d "$SDK_TOP/h" ]]; then
  rm -rf "$DEMO/include/h"
  cp -a "$SDK_TOP/h" "$DEMO/include/h"
elif [[ -d "$SDK_TOP/include/h" ]]; then
  rm -rf "$DEMO/include/h"
  cp -a "$SDK_TOP/include/h" "$DEMO/include/h"
else
  echo "Could not find SDK headers (h/)" >&2
  find "$SDK_TOP" -maxdepth 3 -type d | head -50
  exit 1
fi
# Shared libraries
shopt -s nullglob
LIBS=( "$SDK_TOP"/lib*.so* )
if [[ ${#LIBS[@]} -eq 0 ]]; then
  LIBS=( "$SDK_TOP"/lib/lib*.so* )
fi
if [[ ${#LIBS[@]} -eq 0 ]]; then
  echo "Could not find lib*.so in SDK" >&2
  exit 1
fi
cp -a "${LIBS[@]}" "$DEMO/lib/zoom_meeting_sdk/"
# qt_libs
if [[ -d "$SDK_TOP/qt_libs" ]]; then
  rm -rf "$DEMO/lib/zoom_meeting_sdk/qt_libs"
  cp -a "$SDK_TOP/qt_libs" "$DEMO/lib/zoom_meeting_sdk/qt_libs"
fi
# translation / json
mkdir -p "$DEMO/lib/zoom_meeting_sdk/json"
if [[ -f "$SDK_TOP/translation.json" ]]; then
  cp -a "$SDK_TOP/translation.json" "$DEMO/lib/zoom_meeting_sdk/json/"
elif [[ -f "$SDK_TOP/json/translation.json" ]]; then
  cp -a "$SDK_TOP/json/translation.json" "$DEMO/lib/zoom_meeting_sdk/json/"
fi
# libmeetingsdk.so.1 symlink
(
  cd "$DEMO/lib/zoom_meeting_sdk"
  if [[ -e libmeetingsdk.so && ! -e libmeetingsdk.so.1 ]]; then
    ln -sf libmeetingsdk.so libmeetingsdk.so.1
  elif [[ -L libmeetingsdk.so.1 ]]; then
    :
  elif [[ -e libmeetingsdk.so.1 ]]; then
    :
  fi
  ls -la libmeetingsdk.so* || true
)
shopt -u nullglob

echo "==> Installing Multistream raw-data writers"
# These replace the sample's writers wholesale: they stream into a live encoder
# instead of dumping raw YUV/PCM to disk. The sample's headers are kept as-is.
for src in MultistreamSync.h ZoomSDKRenderer.cpp ZoomSDKAudioRawData.cpp; do
  if [[ ! -f "$REPO_ZOOM_SDK/src/$src" ]]; then
    echo "Missing $REPO_ZOOM_SDK/src/$src" >&2
    exit 1
  fi
  install -m 644 "$REPO_ZOOM_SDK/src/$src" "$DEMO/$src"
done

echo "==> Patching sample for display_name + audio/video capture fixes"
# Sample CMakeLists copies config.txt into the build tree — create a stub if missing.
if [[ ! -f "$DEMO/config.txt" ]]; then
  cat > "$DEMO/config.txt" <<'EOF'
meeting_number: "0"
token: ""
meeting_password: ""
recording_token: ""
display_name: "ISKCON Deoghar Archive"
GetVideoRawData: "true"
GetAudioRawData: "true"
SendVideoRawData: "false"
SendAudioRawData: "false"
EOF
fi
python3 "$REPO_ZOOM_SDK/patch-sample.py" "$DEMO"

echo "==> Building meetingSDKDemo"
rm -rf "$DEMO/build" "$DEMO/bin"
cmake -S "$DEMO" -B "$DEMO/build"
cmake --build "$DEMO/build" -j"$(nproc)"

if [[ ! -x "$DEMO/bin/meetingSDKDemo" ]]; then
  # Some builds put the binary in build/
  if [[ -x "$DEMO/build/meetingSDKDemo" ]]; then
    mkdir -p "$DEMO/bin"
    cp -a "$DEMO/build/meetingSDKDemo" "$DEMO/bin/meetingSDKDemo"
    # Sample often expects to run from bin/ with libs relative
    ln -sfn ../lib "$DEMO/bin/lib" 2>/dev/null || true
  fi
fi

if [[ ! -x "$DEMO/bin/meetingSDKDemo" ]]; then
  echo "Build finished but meetingSDKDemo not found" >&2
  find "$DEMO" -name 'meetingSDKDemo' 2>/dev/null
  exit 1
fi

# Ensure libs are visible next to the binary (sample convention)
mkdir -p "$DEMO/bin"
if [[ ! -e "$DEMO/bin/lib" ]]; then
  ln -sfn ../lib "$DEMO/bin/lib"
fi
# Copy config reader expects config.txt beside binary; wrapper writes into workdir instead.

echo "==> Installing Multistream wrappers"
install -m 755 "$REPO_ZOOM_SDK/zoom-sdk-recorder.sh" /opt/multistream/bin/zoom-sdk-recorder
install -m 755 "$REPO_ZOOM_SDK/setup-audio.sh" /opt/multistream/bin/zoom-sdk-setup-audio.sh
# Marker so update.sh knows a real recorder is present
touch /opt/multistream/zoom-sdk/.built
date -u +%Y-%m-%dT%H:%M:%SZ > /opt/multistream/zoom-sdk/.built
echo "SDK=$(basename "$TARBALL")" >> /opt/multistream/zoom-sdk/.built

echo "==> Smoke: shared libraries resolve"
# Never launch the demo here: it ignores its arguments and joins whatever
# meeting config.txt names, so a smoke run would hang forever.
if ldd "$DEMO/bin/meetingSDKDemo" | grep -q "not found"; then
  echo "Unresolved shared libraries:" >&2
  ldd "$DEMO/bin/meetingSDKDemo" | grep "not found" >&2
  exit 1
fi
ldd "$DEMO/bin/meetingSDKDemo" | grep -E "swscale|meetingsdk" || true

echo "==> Selftest: live encoder path"
install -m 755 "$REPO_ZOOM_SDK/selftest-encoder.sh" /opt/multistream/bin/zoom-sdk-selftest.sh
if [[ "${ZOOM_SDK_SKIP_SELFTEST:-0}" != "1" ]]; then
  /opt/multistream/bin/zoom-sdk-selftest.sh 20
fi

echo "==> Done. Recorder ready at /opt/multistream/bin/zoom-sdk-recorder"
