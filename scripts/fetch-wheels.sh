#!/usr/bin/env bash
# 把 requirements.txt 的依赖预下载成 wheel，放进 vendor/wheels/，
# 让 Dockerfile 在完全无网的构建机上也能装出镜像。
#
#   bash scripts/fetch-wheels.sh              # 下载 amd64 + arm64 两套（推荐）
#   bash scripts/fetch-wheels.sh amd64        # 只下 x86_64
#   bash scripts/fetch-wheels.sh arm64
#
# 为什么要指定 --platform/--python-version：
#   wheel 是分平台的，在 Windows/macOS 打包机上直接 `pip download` 会拿到
#   本机 wheel，进到 debian:bookworm(python3.11, manylinux) 里装不上。
#   这里显式按目标平台 + cp311 拉取，保证 target 能装。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
OUT="$ROOT/vendor/wheels"
REQ="$ROOT/requirements.txt"

# 镜像基础是 debian:bookworm-slim → python3.11，manylinux2014 兼容
PYVER="3.11"
MANYLINUX="manylinux2014"

mkdir -p "$OUT"

pick_pip() {
    if command -v pip3 >/dev/null 2>&1; then echo "pip3"
    elif command -v pip >/dev/null 2>&1; then echo "pip"
    elif command -v python3 >/dev/null 2>&1; then echo "python3 -m pip"
    else echo ""; fi
}

PIP=$(pick_pip)
if [ -z "$PIP" ]; then
    echo "【ERROR】找不到 pip，请先安装 Python 3.8+ 与 pip" >&2
    exit 1
fi

fetch_one() {  # fetch_one <arch: x86_64|aarch64> <标签>
    local arch="$1" label="$2"
    echo "==> 下载 $label wheel（${MANYLINUX}_${arch} / cp${PYVER//./}）"
    # shellcheck disable=SC2086
    $PIP download \
        -r "$REQ" \
        -d "$OUT" \
        --only-binary=:all: \
        --platform "${MANYLINUX}_${arch}" \
        --python-version "$PYVER" \
        --implementation cp \
        --no-cache-dir
}

TARGETS="${1:-both}"
case "$TARGETS" in
    amd64 | x86_64) fetch_one x86_64 "amd64" ;;
    arm64 | aarch64) fetch_one aarch64 "arm64" ;;
    both | "") fetch_one x86_64 "amd64"; fetch_one aarch64 "arm64" ;;
    *) echo "未知参数：$TARGETS（可选 amd64 / arm64 / both）" >&2; exit 1 ;;
esac

count=$(find "$OUT" -name '*.whl' | wc -l | tr -d ' ')
echo
echo "完成：$OUT 共 $count 个 wheel"
echo "接下来：bash scripts/build-package.sh   # 离线构建镜像并打出 .fpk"
