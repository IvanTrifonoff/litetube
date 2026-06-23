# tv-fork-patches

Files in this directory are the complete Litetube-specific delta against a
clean `yuliskov/SmartTubeNext` checkout. To build the Litetube TV flavor:

```bash
# 1. Clone upstream SmartTubeNext (no fork — apply patches to fresh tree)
git clone https://github.com/yuliskov/SmartTubeNext.git /tmp/st-upstream
cd /tmp/st-upstream
git submodule update --init

# 2. Overlay all Litetube patches
cp -R /path/to/litetube/tv-fork-patches/* .

# 3. Build the Litetube flavor (`app.litetube.tv`)
./gradlew assembleStlitetube

# APK: smarttubetv/build/outputs/apk/stlitetube/{debug,release}/app-litetube-tv-{debug,release}.apk
```

## Files in this directory

| Path | Purpose |
|------|---------|
| `SharedModules/constants.gradle` | Bump `minSdkVersion` 17 → 21. |
| `MediaServiceCore/SharedModules/constants.gradle` | Same bump in the MediaServiceCore subtree. |
| `smarttubetv/build.gradle` | Add `stlitetube` productFlavor (`applicationId "app.litetube.tv"`) + `LITETUBE_API_BASE` / `LITETUBE_DEVICE_START_TIMEOUT_SEC` BuildConfig fields. |
| `smarttubetv/src/main/AndroidManifest.xml` | Register `LitetubeActivationActivity`. |
| `smarttubetv/src/main/java/.../common/litetube/LitetubePrefs.kt` | SharedPreferences-backed JWT / activation-code / cached-proxy storage. |
| `smarttubetv/src/main/java/.../common/litetube/LitetubeApi.kt` | OkHttp helpers for `/api/devices/*` and `/api/proxy/pool`. |
| `smarttubetv/src/main/java/.../tv/ui/litetube/LitetubeActivationActivity.kt` | First-launch screen: QR + 6-digit code, long-polls the backend. |
| `smarttubetv/src/main/java/.../tv/ui/main/SplashActivity.java` | Gated splash: forwards to `LitetubeActivationActivity` on the `stlitetube` flavor when JWT is absent. |
| `smarttubetv/src/main/java/.../tv/ui/main/MainApplication.java` | Skips `RussiaProxySelector.bootstrap()` until JWT is present. |
| `smarttubetv/src/main/res/layout/activity_litetube_activation.xml` | Leanback layout: 720 dp QR + 72 sp monospace 6-digit code + status hint. |
| `smarttubetv/src/main/res/values/strings.xml` | Adds nine `litetube_activation_*` resources (Russian copy). |

## Upstream license

SmartTubeNext is MIT (yuliskov 2020-present). When overlaying these files
on a fresh upstream checkout, retain upstream's `LICENSE`, `README.md`,
and `PRIVACY.md`. Litetube's own copyright line is appended to `LICENSE`
at the repo root:

```
Copyright (c) 2020-present yuliskov
Copyright (c) 2026 Litetube contributors
```
