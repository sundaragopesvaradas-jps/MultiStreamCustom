#!/usr/bin/env bash
# Virtual PulseAudio sink for Zoom Meeting SDK on a headless VM (no sound card).
set -euo pipefail

export HOME="${HOME:-/root}"
mkdir -p "$HOME/.config"

if ! command -v pulseaudio >/dev/null 2>&1; then
  echo "pulseaudio not installed" >&2
  exit 1
fi

# Stateless-ish: restart if already running so modules load cleanly.
pulseaudio --kill 2>/dev/null || true
sleep 1
pulseaudio -D --exit-idle-time=-1 --log-target=syslog 2>/dev/null || \
  pulseaudio -D --exit-idle-time=-1

# Wait for the daemon.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if pactl info >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

pactl unload-module module-null-sink 2>/dev/null || true
pactl load-module module-null-sink sink_name=SpeakerOutput
pactl set-default-sink SpeakerOutput
pactl set-default-source SpeakerOutput.monitor

cat > "$HOME/.config/zoomus.conf" <<'EOF'
[General]
system.audio.type=default
EOF

echo "PulseAudio virtual sink ready."
