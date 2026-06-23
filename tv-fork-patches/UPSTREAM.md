# Upstream pin

The patched files in `patched/` were developed against
[yuliskov/SmartTubeNext](https://github.com/yuliskov/SmartTubeNext), tracking
the `stable` branch at the time of writing.

## Tested against

| SmartTubeNext upstream | Status of patch overlay |
|------------------------|------------------------|
| `stable` branch, HEAD as of 22 Jun 2026 | ✅ `bash apply.sh` dry-run + copy succeeded, sanity checks pass |

Patches assume the upstream tree exposes these anchor paths:

- `smarttubetv/build.gradle` (`productFlavors` block present)
- `smarttubetv/src/main/java/com/liskovsoft/smartyoutubetv2/tv/ui/main/SplashActivity.java`
- `smarttubetv/src/main/java/com/liskovsoft/smartyoutubetv2/tv/ui/main/MainApplication.java`
- `SharedModules/constants.gradle` and `MediaServiceCore/SharedModules/constants.gradle`

If any anchor is renamed upstream the patcher will warn (or error with
`--strict`). To rebase against a newer upstream:

```bash
# 1. Fresh checkout of newer upstream
git clone https://github.com/yuliskov/SmartTubeNext.git /tmp/st-new
cd /tmp/st-new && git submodule update --init

# 2. Dry-run overlay
bash /path/to/litetube/tv-fork-patches/apply.sh --dry-run /tmp/st-new

# 3. If clean → apply and rebuild
bash /path/to/litetube/tv-fork-patches/apply.sh --strict /tmp/st-new
cd /tmp/st-new && ./gradlew assembleStlitetube
```

Capture differences with:

```bash
diff -urN \
  /path/to/litetube/tv-fork-patches/patched \
  /tmp/st-new | tee patch-shifts.log
```
