#!/bin/bash
# Builds Synth.app containing the synthkit Apple-layer binary.
set -euo pipefail
cd "$(dirname "$0")"
APP="../bin/Synth.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O synthkit.swift -o "$APP/Contents/MacOS/synthkit" \
  -framework EventKit -framework Foundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist
# Sign with a stable self-signed identity, not ad-hoc. An ad-hoc signature is identified
# by cdhash, which changes on every build, so TCC treats each rebuild as a new program and
# re-prompts — fatal for an unattended launchd daemon. This identity keeps grants across
# rebuilds. Falls back to ad-hoc if the identity is missing.
IDENTITY="Synth Code Signing"
if security find-certificate -c "$IDENTITY" >/dev/null 2>&1; then
  codesign --force --sign "$IDENTITY" --identifier page.akvaithi.synth "$APP"
else
  echo "WARNING: '$IDENTITY' not in keychain; falling back to ad-hoc (grants will not persist)"
  codesign --force --sign - --identifier page.akvaithi.synth "$APP"
fi
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature" || true
echo "built: $APP"
