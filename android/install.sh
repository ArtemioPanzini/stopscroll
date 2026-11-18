#!/usr/bin/env bash
set -euo pipefail
umask 077

android_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec python3 -I "$android_dir/release.py" "$@"
