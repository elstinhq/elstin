#!/usr/bin/env bash
#
# Assemble the Mac app: build/Elstin.app — the native window (packaging/app, Swift) with the
# frozen backend (packaging/build-backend.sh) inside it. Phase 3 of the DMG; signing for
# distribution, notarization and the .dmg itself come after.
#
#   packaging/build-app.sh                  # build the backend, the window, the app
#   packaging/build-app.sh --skip-backend   # reuse build/dist/elstin-backend as it is
#
# The result is signed ad hoc, which is enough to run on the Mac that built it and no other.
# Needs everything build-backend.sh needs, plus Xcode (Swift, SwiftPM, iconutil).
#
# Layout (Contents/):  MacOS/Elstin            the window
#                      Resources/backend/      elstin-backend + _internal/ (the frozen backend)
#                      Resources/Elstin.icns   Info.plist

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build="$root/build"
app="$build/Elstin.app"
BUNDLE_ID="${ELSTIN_BUNDLE_ID:-com.elstin.Elstin}"
MIN_MACOS="${ELSTIN_MAX_MACOS:-15.0}"   # the same floor build-backend.sh measured and enforces

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

cd "$root"
skip_backend=0
[ "${1:-}" = "--skip-backend" ] && skip_backend=1

if [ "$skip_backend" = 0 ]; then
    say "Building the backend"
    packaging/build-backend.sh
else
    [ -x build/dist/elstin-backend/elstin-backend ] || die "no backend in build/dist; run without --skip-backend."
fi

say "Testing the window's logic (swift test)"
if ! swift_tests="$(swift test --package-path packaging/app --scratch-path build/swift 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; exit "${PIPESTATUS[0]}")"; then
    printf '%s\n' "$swift_tests" | grep -E "error|failed" | head -20
    die "the Swift tests failed."
fi
printf '%s\n' "$swift_tests" | grep -E "Executed [0-9]+ tests" | tail -1

say "Building the window (arm64, macOS $MIN_MACOS and up)"
swift build --package-path packaging/app --scratch-path build/swift -c release --arch arm64 \
    -Xswiftc -target -Xswiftc "arm64-apple-macosx$MIN_MACOS" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E "error|warning: |Build complete" || true
exe="$(swift build --package-path packaging/app --scratch-path build/swift -c release --arch arm64 --show-bin-path)/Elstin"
[ -x "$exe" ] || die "the window did not build."

say "Drawing the placeholder icon"
icons="$build/icon"
rm -rf "$icons" && mkdir -p "$icons/Elstin.iconset"
swift packaging/app/Tools/make-icon.swift "$icons/master.png" 1024
for size in 16 32 128 256 512; do
    sips -z $size $size "$icons/master.png" --out "$icons/Elstin.iconset/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$icons/master.png" --out "$icons/Elstin.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$icons/Elstin.iconset" -o "$icons/Elstin.icns"

say "Assembling $app"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)"
buildnumber="$(git rev-list --count HEAD 2>/dev/null || echo 1)"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$exe" "$app/Contents/MacOS/Elstin"
cp -R build/dist/elstin-backend "$app/Contents/Resources/backend"
cp "$icons/Elstin.icns" "$app/Contents/Resources/Elstin.icns"
cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>                 <string>Elstin</string>
    <key>CFBundleDisplayName</key>          <string>Elstin</string>
    <key>CFBundleIdentifier</key>           <string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key>           <string>Elstin</string>
    <key>CFBundleIconFile</key>             <string>Elstin</string>
    <key>CFBundlePackageType</key>          <string>APPL</string>
    <key>CFBundleShortVersionString</key>   <string>$version</string>
    <key>CFBundleVersion</key>              <string>$buildnumber</string>
    <key>LSMinimumSystemVersion</key>       <string>$MIN_MACOS</string>
    <key>LSApplicationCategoryType</key>    <string>public.app-category.developer-tools</string>
    <key>NSPrincipalClass</key>             <string>NSApplication</string>
    <key>NSHighResolutionCapable</key>      <true/>
    <key>NSSupportsAutomaticTermination</key> <false/>
    <key>NSSupportsSuddenTermination</key>  <false/>
    <key>NSHumanReadableCopyright</key>     <string>Elstin</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>  <true/>
    </dict>
</dict>
</plist>
PLIST
plutil -lint "$app/Contents/Info.plist" >/dev/null || die "Info.plist is not valid."

say "Signing ad hoc (for this Mac only; distribution signing is a later phase)"
codesign --force --sign - --timestamp=none "$app/Contents/MacOS/Elstin"
codesign --force --deep --sign - --timestamp=none "$app" 2>&1 | grep -v "replacing existing signature" || true
codesign --verify --deep --strict "$app" || die "the assembled app does not verify."

say "Checking the app finds its own backend and the backend passes its self-test"
"$app/Contents/MacOS/Elstin" --check-backend | tail -1

say "Done: $app ($(du -sh "$app" | cut -f1))"
