#!/usr/bin/env bash
# Litetube patch applier — overlays tv-fork-patches/ onto a fresh
# yuliskov/SmartTubeNext checkout. Run from SmartTubeNext/, NOT from litetube/.
#
# Usage:
#   bash /path/to/litetube/tv-fork-patches/apply.sh [--dry-run] [--strict] [TARGET_DIR]
#
#   --dry-run   Echo what would be copied, but do not write anything
#   --strict    Treat any missing upstream file as a hard error (default: warn)
#   TARGET_DIR  Defaults to cwd

set -euo pipefail

DRY_RUN=0
STRICT=0
TARGET=""
for a in "$@"; do
  case $a in
    --dry-run) DRY_RUN=1 ;;
    --strict)  STRICT=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    --*)       echo "unknown flag: $a" >&2; exit 2 ;;
    *)         TARGET="$a" ;;
  esac
done

PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$TARGET" ]]; then TARGET="$(pwd)"; fi
if [[ ! -d "$TARGET" ]]; then
  echo "target dir not found: $TARGET" >&2; exit 2
fi
if [[ ! -f "$TARGET/smarttubetv/build.gradle" ]]; then
  echo "target does not look like a SmartTubeNext checkout" \
       "(missing smarttubetv/build.gradle): $TARGET" >&2; exit 2
fi

copy_one() {
  local rel="$1" kind="$2"   # kind ∈ {PATCHED,NEW}
  local src="$PATCH_DIR/patched/$rel"
  local dst="$TARGET/$rel"
  if [[ ! -f "$src" ]]; then
    echo "ERR  missing patch file: $src" >&2; exit 2
  fi
  if [[ ! -f "$dst" && "$kind" == "PATCHED" ]]; then
    if [[ $STRICT -eq 1 ]]; then
      echo "ERR  upstream is missing expected file $rel" >&2; exit 3
    else
      echo "WARN upstream is missing expected file $rel"
    fi
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY   $kind  $rel"
  else
    cp "$src" "$dst"
    echo "COPY  $kind  $rel"
  fi
}

PATCHED=(
  SharedModules/constants.gradle
  MediaServiceCore/SharedModules/constants.gradle
  smarttubetv/build.gradle
  smarttubetv/src/main/AndroidManifest.xml
  smarttubetv/src/main/java/com/liskovsoft/smartyoutubetv2/tv/ui/main/SplashActivity.java
  smarttubetv/src/main/java/com/liskovsoft/smartyoutubetv2/tv/ui/main/MainApplication.java
  smarttubetv/src/main/res/values/strings.xml
)
NEW=(
  common/src/main/java/com/liskovsoft/smartyoutubetv2/common/litetube/LitetubePrefs.kt
  common/src/main/java/com/liskovsoft/smartyoutubetv2/common/litetube/LitetubeApi.kt
  smarttubetv/src/main/java/com/liskovsoft/smartyoutubetv2/tv/ui/litetube/LitetubeActivationActivity.kt
  smarttubetv/src/main/res/layout/activity_litetube_activation.xml
)

echo "Patch dir : $PATCH_DIR"
echo "Target    : $TARGET"
echo "Mode      : $([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo APPLY)"
echo "Strictness: $([[ $STRICT -eq 1 ]] && echo STRICT || echo WARN)"
echo
for f in "${PATCHED[@]}"; do copy_one "$f" PATCHED; done
for f in "${NEW[@]}";     do copy_one "$f" NEW;     done

cat <<EOF

Done. Next:
  cd $TARGET
  ./gradlew assembleStlitetube

APK: $TARGET/smarttubetv/build/outputs/apk/stlitetube/{debug,release}/app-litetube-tv-{debug,release}.apk
EOF
