#!/bin/bash
# Build VoiceInputMac.app WITHOUT SwiftPM.
#
# Why not `swift build`: on some machines the Command Line Tools are in a
# half-updated state (old swift-package vs newer BuildServerProtocol.framework,
# and/or an SDK newer than the compiler) so `swift build` / `swiftc` with the
# default SDK fails. We compile directly with swiftc and, when the default SDK
# is rejected, retry with an older CLT SDK that matches the installed compiler.
set -euo pipefail
cd "$(dirname "$0")"

OUT=".build/release"
mkdir -p "$OUT"
SOURCES=(Sources/App.swift Sources/Recorder.swift Sources/Transcriber.swift Sources/RecordingIndicator.swift)

# Candidate SDKs: default first, then older CLT SDKs as fallback.
SDKS=("$(xcrun --show-sdk-path 2>/dev/null || echo /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk)")
for s in /Library/Developer/CommandLineTools/SDKs/MacOSX26*.sdk \
         /Library/Developer/CommandLineTools/SDKs/MacOSX15*.sdk; do
  [ -d "$s" ] && SDKS+=("$s")
done

built=0
for sdk in "${SDKS[@]}"; do
  [ -d "$sdk" ] || continue
  if swiftc -sdk "$sdk" -O \
       -framework AppKit -framework AVFoundation -framework Carbon \
       -o "$OUT/VoiceInputMac" "${SOURCES[@]}" 2>"$OUT/.build.err"; then
    echo "[build] compiled with SDK: $sdk"
    built=1
    break
  fi
  echo "[build] SDK failed, retrying: $sdk"
done
if [ "$built" != 1 ]; then
  echo "[build] FAILED"; cat "$OUT/.build.err"; exit 1
fi

APP="$OUT/VoiceInputMac.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$OUT/VoiceInputMac" "$APP/Contents/MacOS/VoiceInputMac"
# App icon — regenerate with: swift tools/make-icon.swift Resources/AppIcon.icns
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Voice Input</string>
    <key>CFBundleDisplayName</key><string>Voice Input</string>
    <key>CFBundleIdentifier</key><string>local.hunghanchu.voice-input-mac</string>
    <key>CFBundleExecutable</key><string>VoiceInputMac</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><false/>
    <key>NSMicrophoneUsageDescription</key><string>語音輸入需要麥克風權限以錄音並轉寫。</string>
    <key>NSAppleEventsUsageDescription</key><string>需要送出 Cmd+V 以將轉寫結果貼到目前游標位置。</string>
</dict>
</plist>
PLIST

# --- 簽章：穩定身分（不是 adhoc）------------------------------------------------
# adhoc 簽章的 designated requirement 就是 binary 的 cdhash → 每次重建都變，macOS 會
# 當成新 app，麥克風/輔助使用授權每次都要重問（使用者回報的「一直跳權限通知」）。
# 用固定憑證簽章後，DR 變成 identifier + 憑證 leaf hash → 常數，授權只問一次。
#   一次性設定：./tools/make-identity.sh
#   臨時退回 adhoc：VOICE_INPUT_SIGN=adhoc ./build.sh
SIGN_KC="$HOME/Library/Keychains/voice-input-mac-signing.keychain-db"
SIGN_ID="${VOICE_INPUT_SIGN_ID:-VoiceInputMac Local Signing}"
sign_mode="adhoc"
if [ "${VOICE_INPUT_SIGN:-stable}" != "adhoc" ] && [ -f "$SIGN_KC" ]; then
  security unlock-keychain -p "" "$SIGN_KC" 2>/dev/null || true
  SIGN_HASH="$(security find-identity -p codesigning "$SIGN_KC" 2>/dev/null |
               awk -v n="$SIGN_ID" 'index($0, n) { print $2; exit }')"
  if [ -n "${SIGN_HASH:-}" ] &&
     codesign --force --sign "$SIGN_HASH" --keychain "$SIGN_KC" "$APP" >/dev/null 2>&1; then
    sign_mode="stable($SIGN_ID)"
  fi
fi
if [ "$sign_mode" = "adhoc" ]; then
  codesign --force --sign - "$APP" >/dev/null 2>&1 || sign_mode="none"
  echo "[build] ⚠️  adhoc 簽章：每次重建 macOS 都會重新問權限。"
  echo "[build]     一次性修法：./tools/make-identity.sh"
fi
echo "[build] signed: $sign_mode"
echo "[build] designated requirement: $(codesign -dr - "$APP" 2>&1 | tail -1)"
echo "[build] ok -> $APP"