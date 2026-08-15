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
  libcurl4-openssl-dev openssl \
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

echo "==> Patching sample for display_name + any-resolution capture"
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
python3 - <<'PY'
from pathlib import Path

demo = Path("/opt/multistream/zoom-sdk/sample/demo")
cpp = (demo / "meeting_sdk_demo.cpp").read_text(encoding="utf-8", errors="replace")

# Parse display_name from config.txt
needle = 'if (config.find("recording_token") != config.end())'
insert = '''
	if (config.find("display_name") != config.end()) {
		display_name=config["display_name"];
		std::cout << "display_name: " << display_name << std::endl;
	}
'''
if "display_name=config" not in cpp:
    # declare the variable with the others
    cpp = cpp.replace(
        "std::string meeting_number, token, meeting_password, recording_token,onBehalfOf_Token;",
        "std::string meeting_number, token, meeting_password, recording_token,onBehalfOf_Token, display_name;",
        1,
    )
    cpp = cpp.replace(
        "std::string meeting_number, token, meeting_password, recording_token;",
        "std::string meeting_number, token, meeting_password, recording_token, display_name;",
        1,
    )
    if needle in cpp and "display_name=config" not in cpp:
        cpp = cpp.replace(needle, insert + "\n\t" + needle, 1)

# Use display_name instead of hard-coded LinuxChun
cpp = cpp.replace(
    'withoutloginParam.userName = "LinuxChun";',
    'withoutloginParam.userName = display_name.empty() ? "ISKCON Deoghar Archive" : display_name.c_str();',
)
# Start muted/video-off so the bot is less disruptive
cpp = cpp.replace("withoutloginParam.isVideoOff = false;", "withoutloginParam.isVideoOff = true;")
cpp = cpp.replace("withoutloginParam.isAudioOff = false;", "withoutloginParam.isAudioOff = true;")
# Fix Zoom sample bug: `!recording_token.size() == 0` is always false.
cpp = cpp.replace(
    "if (!recording_token.size() == 0)",
    "if (recording_token.size() != 0)",
)
# Meeting SDK 7.x added FrameDataFormat to setExternalVideoSource.
cpp = cpp.replace(
    "SDKError err = p_videoSourceHelper->setExternalVideoSource(virtual_camera_video_source);",
    "SDKError err = p_videoSourceHelper->setExternalVideoSource(virtual_camera_video_source, FrameDataFormat_I420_FULL);",
)

(demo / "meeting_sdk_demo.cpp").write_text(cpp, encoding="utf-8")

renderer = (demo / "ZoomSDKRenderer.cpp").read_text(encoding="utf-8", errors="replace")
# Save any resolution (not only height==720) and record size for ffmpeg.
replacement = r'''
 static int locked_w = 0, locked_h = 0;
 int w = data->GetStreamWidth();
 int h = data->GetStreamHeight();
 if (locked_w == 0) {
   locked_w = w;
   locked_h = h;
   FILE* meta = fopen("video.size", "w");
   if (meta) {
     fprintf(meta, "width=%d\nheight=%d\n", locked_w, locked_h);
     fclose(meta);
   }
 }
 if (w == locked_w && h == locked_h) {
   SaveToRawYUVFile(data);
 }
'''
if "locked_w" not in renderer:
    import re
    renderer2, n = re.subn(
        r"if\s*\(\s*data->GetStreamHeight\(\)\s*==\s*720\s*\)\s*\{\s*SaveToRawYUVFile\(data\);\s*\}",
        replacement.strip(),
        renderer,
        count=1,
    )
    if n == 0:
        renderer2 = renderer.replace(
            "SaveToRawYUVFile(data);",
            replacement.strip(),
            1,
        )
    renderer = renderer2
if "#include <cstdio>" not in renderer and "fprintf" in renderer:
    renderer = renderer.replace("#include <iostream>", "#include <iostream>\n#include <cstdio>", 1)
(demo / "ZoomSDKRenderer.cpp").write_text(renderer, encoding="utf-8")
print("patches applied")
PY

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

echo "==> Smoke: binary + libs"
ldd "$DEMO/bin/meetingSDKDemo" | head -40 || true
"$DEMO/bin/meetingSDKDemo" --help >/dev/null 2>&1 || true
/opt/multistream/bin/zoom-sdk-recorder 2>&1 | head -5 || true

echo "==> Done. Recorder ready at /opt/multistream/bin/zoom-sdk-recorder"
