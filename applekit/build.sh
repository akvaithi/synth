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
codesign --force --sign - --identifier page.akvaithi.synth "$APP"
codesign -dv "$APP" 2>&1 | grep -E "Identifier|Signature" || true
echo "built: $APP"
