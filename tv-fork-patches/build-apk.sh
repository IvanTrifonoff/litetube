#!/usr/bin/env bash
# Build the Litetube APK (stlitetube flavor) from a SmartTubeFork checkout.
#
# Prerequisites:
#   - Android SDK (ANDROID_HOME or ANDROID_SDK_ROOT set)
#   - JDK 11+
#   - SmartTubeFork repo cloned adjacent to this script: ../SmartTubeFork
#
# Usage:
#   ./build-apk.sh            # debug build
#   ./build-apk.sh release    # signed release build (requires keystore.properties)
#
# Output:
#   SmartTubeFork/smarttubetv/build/outputs/apk/stlitetube/<variant>/*.apk

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FORK_DIR="${SCRIPT_DIR}/../../SmartTubeFork"

cd "$FORK_DIR"

VARIANT="${1:-debug}"
case "$VARIANT" in
  debug)   GRADLE_TASK="assembleStlitetubeDebug"   ; KEYSTORE_NEEDED=false ;;
  release) GRADLE_TASK="assembleStlitetubeRelease" ; KEYSTORE_NEEDED=true  ;;
  *) echo "Usage: $0 [debug|release]" >&2; exit 1 ;;
esac

# Verify keystore for release builds
if $KEYSTORE_NEEDED; then
  if [ ! -f keystore.properties ]; then
    echo "ERROR: keystore.properties not found. Create it with:" >&2
    echo "  storeFile=../litetube-release.keystore" >&2
    echo "  storePassword=..." >&2
    echo "  keyAlias=litetube" >&2
    echo "  keyPassword=..." >&2
    exit 1
  fi
fi

echo "Building Litetube APK (flavor=stlitetube, variant=$VARIANT)..."
./gradlew "$GRADLE_TASK"

APK_DIR="smarttubetv/build/outputs/apk/stlitetube/${VARIANT}"
APK=$(find "$APK_DIR" -name '*.apk' -not -name '*unaligned*' | head -1)

if [ -z "$APK" ]; then
  echo "ERROR: APK not found in $APK_DIR" >&2
  exit 1
fi

echo ""
echo "Build successful: $APK"
echo "Size: $(du -sh "$APK" | cut -f1)"
echo ""
echo "Copy to static/app/:"
echo "  cp $APK ${SCRIPT_DIR}/../backend/api/litetube/static/app/litetube.apk"
echo ""
echo "Or to server directly:"
echo "  scp $APK root@your-server:/srv/litetube/app/litetube.apk"
