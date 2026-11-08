#!/usr/bin/env bash
# 应用 portal 补丁（薄封装，真正逻辑在 03-apply.py 的 hash 校验 + CRLF 保留）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${1:-/opt/ygobot}
exec python3 "$HERE/03-apply.py" --root "$ROOT" "${@:2}"
