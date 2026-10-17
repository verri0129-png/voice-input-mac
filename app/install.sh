#!/bin/bash
# Build VoiceInputMac.app and install it into /Applications.
#
#   ./install.sh            build + install + (re)launch
#   ./install.sh --no-open  build + install only
#
# Why /Applications: it is the only place Launchpad and Finder's Applications
# folder index, so the app becomes double-clickable like any other app. The
# binary does not care where it lives — Transcriber.swift resolves
# ~/orca/voice-input-mac/{.venv/bin/python,transcribe.py} absolutely.
set -euo pipefail
cd "$(dirname "$0")"

DEST="/Applications/VoiceInputMac.app"
DO_OPEN=1
[ "${1:-}" = "--no-open" ] && DO_OPEN=0

./build.sh

# Two live instances would each register Left Cmd+E -> the text gets pasted twice.
pkill -f "VoiceInputMac.app/Contents/MacOS/VoiceInputMac" 2>/dev/null || true
sleep 1

rm -rf "$DEST"
cp -R ".build/release/VoiceInputMac.app" "$DEST"
touch "$DEST"
echo "[install] -> $DEST"

# Let Launchpad pick the new bundle up and drop any cached icon for the path.
killall Dock 2>/dev/null || true
sleep 2

if [ "$DO_OPEN" = 1 ]; then
  open "$DEST"
  echo "[install] launched from $DEST"
fi

echo "[install] running instances:"
pgrep -fl "VoiceInputMac.app/Contents/MacOS/VoiceInputMac" || echo "  (none)"
