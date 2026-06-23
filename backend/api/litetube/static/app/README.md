# Litetube APK

Place built APKs here. They will be served by nginx at `https://litetube.trfnv.ru/app/`.

**Server path note:** nginx serves `/app/` from `/srv/litetube/app/`. Set up the symlink on deploy:
```bash
ln -sf /path/to/litetube/backend/api/litetube/static/app /srv/litetube/app
```
Or, in docker-compose, mount this directory to `/srv/litetube/app` inside the nginx container.

## Naming convention

- `litetube.apk` — latest stable build (symlink or copy)
- `litetube-v0.1.0.apk` — versioned builds (keep last 3)

## Build (on a machine with Android SDK)

```bash
cd SmartTubeFork
./gradlew assembleStlitetubeRelease
# Output: smarttubetv/build/outputs/apk/stlitetube/release/smarttubetv-stlitetube-release.apk
```

Then copy to this directory:

```bash
cp smarttubetv/build/outputs/apk/stlitetube/release/smarttubetv-stlitetube-release.apk \
   /srv/litetube/app/litetube.apk
```

Or, in the repo checkout on the server:

```bash
cp <built-apk> litetube/backend/api/litetube/static/app/litetube.apk
```

## Signing

For release builds, create `SmartTubeFork/keystore.properties`:

```
storeFile=../litetube-release.keystore
storePassword=...
keyAlias=litetube
keyPassword=...
```

The APK must be signed with the same keystore for updates to work (Android
verifies the signature matches across installs). Keep the keystore in a safe
place outside version control.
