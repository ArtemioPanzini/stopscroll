#!/bin/bash
# Optional acceptance check. Requires sudo, Chrome, Node >=20, Playwright, Xvfb.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
fixture=$(mktemp -d /tmp/stopscroll-browser-test-XXXXXX)
python3 "$root/browser/tests/prepare.py" "$root" "$fixture"
node_binary=$(command -v node)
printf 'Disposable fixture: %s\n' "$fixture"
sudo -n unshare -m "$root/browser/tests/namespace.sh" "$fixture" "$(id -u)" "$node_binary" "${PLAYWRIGHT_MODULE:-playwright}"
