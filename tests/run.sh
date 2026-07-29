#!/usr/bin/env bash
# テストを走らせる。本物のキーチェーン・アカウント・ネットワークには触れない。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec python3 -m unittest discover -s tests "$@"
