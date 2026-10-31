#!/usr/bin/env bash
# Build, install and launch the Night Hacks app on a connected iPhone.
#   tools/run_ios.sh                 # prefers an iPhone 17, else the first available iPhone
#   NH_DEVICE="Iphone 15" tools/run_ios.sh
#   NH_HOST=192.168.1.20 tools/run_ios.sh   # Mac address baked in as the app's default
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/ios"

# The Mac's Bonjour name survives IP changes (venue Wi-Fi <-> phone hotspot).
HOST="${NH_HOST:-$(scutil --get LocalHostName).local}"
JSON="$(mktemp)"
xcrun devicectl list devices --json-output "$JSON" >/dev/null

read -r CORE_ID UDID NAME < <(python3 - "$JSON" "${NH_DEVICE:-}" <<'PY'
import json, sys
devices = json.load(open(sys.argv[1]))["result"]["devices"]
want = sys.argv[2].lower()
phones = [d for d in devices
          if d["hardwareProperties"].get("deviceType") == "iPhone"
          and d.get("connectionProperties", {}).get("tunnelState") != "unavailable"]
def score(d):
    name = d["deviceProperties"]["name"].lower()
    model = d["hardwareProperties"].get("marketingName", "").lower()
    if want:
        return 0 if want in name or want in model else 9
    return 0 if "17" in model else 1
phones.sort(key=score)
if not phones or (want and score(phones[0]) == 9):
    sys.exit("no matching iPhone connected")
d = phones[0]
print(d["identifier"], d["hardwareProperties"]["udid"], d["deviceProperties"]["name"].replace(" ", "_"))
PY
)
rm -f "$JSON"
echo "device: $NAME ($UDID)  backend host: $HOST"

command -v xcodegen >/dev/null && xcodegen generate --quiet
xcodebuild -project NightHacks.xcodeproj -scheme NightHacks -configuration Debug \
  -destination "id=$UDID" -derivedDataPath build \
  -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  NH_DEFAULT_HOST="$HOST" build | grep -E "error:|BUILD (SUCCEEDED|FAILED)" || true

APP="build/Build/Products/Debug-iphoneos/NightHacks.app"
[ -d "$APP" ] || { echo "build failed"; exit 1; }
xcrun devicectl device install app --device "$CORE_ID" "$APP" >/dev/null
xcrun devicectl device process launch --terminate-existing --device "$CORE_ID" com.nickzjl.nighthacks >/dev/null
echo "launched on $NAME"
