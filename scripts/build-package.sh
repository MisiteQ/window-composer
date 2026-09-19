#!/usr/bin/env bash
# 一键产出可离线安装的飞牛 fnOS 安装包（.fpk）。
#
#   bash scripts/build-package.sh                 # 完整流程：构建镜像 → 打包 → 出 .fpk
#   bash scripts/build-package.sh --skip-image    # 复用已有的镜像 tar，只重新打 .fpk
#   bash scripts/build-package.sh --no-image      # 打个不含镜像的"瘦包"（需联网拉取，仅调试用）
#   bash scripts/build-package.sh --platform arm  # 在 ARM 机器上打包时声明架构
#   bash scripts/build-package.sh --print-fnpack  # 只打印 fnpack 路径（供 CI 判断）
#
# 跨机协作（本机没有 docker 时）：
#   bash scripts/build-package.sh --image-only                  # 在 A 机：只构建镜像并导出 tar
#   bash scripts/build-package.sh --image-tar path/to/image.tar # 在 B 机：用 A 机的 tar 出包
#
# 产物：dist/window-composer-<版本>.fpk
#
# 说明：本脚本只跑在开发/打包机上；NAS 端从不需要执行它，
# 也不需要装 docker/ 编译器 —— 拿到 .fpk 在应用中心手动安装即可。
#
# 【网络】构建需要能访问 Docker Hub（取基础镜像）与 Debian 源（装软件包）。
# 二者都不可达时脚本会自动切到国内镜像站；也可用环境变量固定下来：
#   WC_BASE_IMAGE      基础镜像引用（如 docker.m.daocloud.io/library/debian:bookworm-slim）
#   WC_APT_MIRROR      Debian 源前缀（如 https://mirrors.aliyun.com/debian；空串=不换源）
#   WC_PIP_INDEX       PyPI 源（默认清华）
#   WC_REGISTRY_MIRROR Docker Hub 镜像站（默认 docker.m.daocloud.io）
#
# 【重要】.fpk 是 gzip(tar.gz)，**不是 zip**。打包一律优先用官方 fnpack；
# 没有 fnpack 时退回 scripts/make-fpk.py（复刻 fnpack 的产出格式）。
# 早期版本用 zip 组装，飞牛会直接报「不是有效的程序文件」。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
PKG="$ROOT/packaging/fnos"
DIST="$ROOT/dist"
IMG_REL="packaging/fnos/app/docker/image"
IMG_DIR="$ROOT/$IMG_REL"
TOOLS="$ROOT/tools"
FNPACK_VERSION="${FNPACK_VERSION:-1.2.3}"

SKIP_IMAGE=0
NO_IMAGE=0
IMAGE_ONLY=0
PLATFORM=""
PRINT_FNPACK=0
NO_FETCH=0
IMAGE_TAR=""
# 单次带索引解析：取值的选项要顺手跳过它的值，否则 `--image-tar xxx.tar`
# 里的 xxx.tar 会被当成未知参数报错（这是最初版本的写法，踩过一次）。
args=("$@")
_i=0
while [ "$_i" -lt "${#args[@]}" ]; do
    arg="${args[$_i]}"
    case "$arg" in
        --skip-image) SKIP_IMAGE=1 ;;
        --no-image) NO_IMAGE=1 ;;
        --image-only) IMAGE_ONLY=1 ;;
        --no-fetch) NO_FETCH=1 ;;
        --print-fnpack) PRINT_FNPACK=1 ;;
        --platform=*) PLATFORM="${arg#--platform=}" ;;
        --image-tar=*) IMAGE_TAR="${arg#--image-tar=}" ;;
        --platform | --image-tar)
            if [ $((_i + 1)) -ge ${#args[@]} ]; then
                echo "【ERROR】$arg 需要一个值" >&2
                exit 1
            fi
            if [ "$arg" = "--platform" ]; then
                PLATFORM="${args[$((_i + 1))]}"
            else
                IMAGE_TAR="${args[$((_i + 1))]}"
            fi
            _i=$((_i + 1))
            ;;
        -h | --help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg" >&2; exit 1 ;;
    esac
    _i=$((_i + 1))
done

if [ "$IMAGE_ONLY" = "1" ] && [ "$NO_IMAGE" = "1" ]; then
    echo "【ERROR】--image-only 与 --no-image 互相矛盾" >&2
    exit 1
fi
if [ -n "$IMAGE_TAR" ] && [ "$NO_IMAGE" = "1" ]; then
    echo "【ERROR】--image-tar 与 --no-image 互相矛盾" >&2
    exit 1
fi

# ---------------------------------------------------------------- python
PY_BIN=""
for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 && { PY_BIN="$c"; break; }
done
if [ -z "$PY_BIN" ]; then
    echo "【ERROR】需要 python3（用于打包校验与兜底打包）" >&2
    exit 1
fi

# ---------------------------------------------------------------- 调用 python 的方式
# 统一「cd 到项目根 + 传相对路径」。
# 原因：Windows 上 Git Bash 交给原生 python.exe 的绝对路径会被 MSYS 二次转换，
# `/d/CODE/x` 会变成 `D:\d\CODE\x`（cygpath 在这个环境里也不可靠），
# 于是 python 报 "can't open file"；相对路径则完全没有这个问题，且三平台通用。
py() {
    ( cd "$ROOT" && "$PY_BIN" "$@" )
}

# ---------------------------------------------------------------- 网络可达性探测
# 构建镜像要访问两处外部资源：Docker Hub（取基础镜像）与 Debian 源（装软件包）。
# 国内网络常见「Docker Hub / GitHub 不通，但国内镜像站通」，这里先探一次，
# 不通就自动换源，避免构建跑到一半才失败。
# 判定标准：只要拿到任何 HTTP 状态码（含 401/403）就算网络可达——
# registry-1.docker.io/v2/ 正常就返回 401，不能用 curl -f 判。
_http_code() {
    if command -v curl >/dev/null 2>&1; then
        curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$1" 2>/dev/null || echo 000
    else
        echo "" # 没有 curl：跳过探测，交给显式环境变量
    fi
}

reachable() {
    local code
    code=$(_http_code "$1")
    [ -n "$code" ] && [ "$code" != "000" ]
}

# ---------------------------------------------------------------- fnpack
# 解析顺序：$FNPACK_BIN → 仓库内 tools/ → PATH。
# fnpack 是官方 Go 静态二进制，只有它能保证产出与飞牛完全对齐的 .fpk。
find_fnpack() {
    if [ -n "${FNPACK_BIN:-}" ]; then
        [ -x "$FNPACK_BIN" ] || [ -f "$FNPACK_BIN" ] && { echo "$FNPACK_BIN"; return 0; }
        echo "【WARN】FNPACK_BIN=$FNPACK_BIN 不可用，忽略" >&2
    fi
    if command -v fnpack >/dev/null 2>&1; then
        command -v fnpack
        return 0
    fi
    local f
    for f in "$TOOLS"/fnpack.exe "$TOOLS"/fnpack "$TOOLS"/fnpack-*; do
        [ -f "$f" ] && { echo "$f"; return 0; }
    done
    echo ""
}

# 官方静态二进制的平台后缀
fnpack_suffix() {
    local os arch
    case "$(uname -s)" in
        MINGW* | MSYS* | CYGWIN*) os="windows" ;;
        Darwin) os="darwin" ;;
        *) os="linux" ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64) arch="amd64" ;;
        aarch64 | arm64) arch="arm64" ;;
        *) arch="amd64" ;;
    esac
    if [ "$os" = "windows" ] && [ "$arch" != "amd64" ]; then
        echo "" # 官方只提供 windows-amd64
        return 0
    fi
    echo "$os-$arch"
}

download_fnpack() {
    local suffix url dest
    suffix=$(fnpack_suffix)
    if [ -z "$suffix" ]; then
        echo "    （官方未提供当前平台的 fnpack，跳过下载）"
        return 1
    fi
    url="https://static2.fnnas.com/fnpack/fnpack-${FNPACK_VERSION}-${suffix}"
    dest="$TOOLS/fnpack-${FNPACK_VERSION}-${suffix}"
    mkdir -p "$TOOLS"
    echo "==> 下载官方 fnpack：$url"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$dest" "$url" || { rm -f "$dest"; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$dest" "$url" || { rm -f "$dest"; return 1; }
    elif command -v powershell >/dev/null 2>&1; then
        powershell -NoProfile -Command "Invoke-WebRequest -Uri '$url' -OutFile '$dest'" || { rm -f "$dest"; return 1; }
    else
        echo "    （curl / wget / powershell 都不可用，跳过下载）"
        return 1
    fi
    chmod +x "$dest" 2>/dev/null || true
    echo "    已保存到 $dest"
    echo "$dest"
    return 0
}

FNPACK=$(find_fnpack)
if [ -z "$FNPACK" ] && [ "$NO_FETCH" = "0" ]; then
    FNPACK=$(download_fnpack | tail -1) || FNPACK=""
    [ -f "$FNPACK" ] || FNPACK=""
fi
if [ "$PRINT_FNPACK" = "1" ]; then
    echo "${FNPACK:-<none>}"
    exit 0
fi

# ---------------------------------------------------------------- 版本号
# 容忍 `version=1.0.0` 与 fnpack 重写后的 `version                    = 1.0.0`
VERSION=$(grep -E '^[[:space:]]*version[[:space:]]*=' "$PKG/manifest" | head -1 | sed 's/^[^=]*=//' | tr -d ' \r')
APPNAME=$(grep -E '^[[:space:]]*appname[[:space:]]*=' "$PKG/manifest" | head -1 | sed 's/^[^=]*=//' | tr -d ' \r')
if [ -z "$VERSION" ] || [ -z "$APPNAME" ]; then
    echo "【ERROR】无法从 manifest 读取 appname/version" >&2
    exit 1
fi
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "【ERROR】version 必须是 X.Y.Z 形式，当前：$VERSION" >&2
    exit 1
fi
IMAGE_REF="$APPNAME:$VERSION"
echo "应用：$APPNAME    版本：$VERSION    镜像：$IMAGE_REF"
if [ -n "$FNPACK" ]; then
    echo "打包器：官方 fnpack → $FNPACK"
else
    echo "打包器：scripts/make-fpk.py（未找到 fnpack，将复刻其产出格式）"
fi

# ---------------------------------------------------------------- platform 注入
# manifest 的 platform 必须与实际打包进去的镜像架构一致：声明 all 而镜像只有
# amd64 时，ARM 设备会装上一个起不来的包。--platform 只在本次构建生效，结束还原。
MANIFEST_BAK=""
restore_manifest() {
    if [ -n "$MANIFEST_BAK" ] && [ -f "$MANIFEST_BAK" ]; then
        mv -f "$MANIFEST_BAK" "$PKG/manifest"
    fi
}
trap restore_manifest EXIT

if [ -n "$PLATFORM" ]; then
    case "$PLATFORM" in
        x86 | arm | all) ;;
        *) echo "【ERROR】--platform 只能是 x86 / arm / all，收到：$PLATFORM" >&2; exit 1 ;;
    esac
    CURRENT_PLATFORM=$(grep -E '^[[:space:]]*platform[[:space:]]*=' "$PKG/manifest" | head -1 | sed 's/^[^=]*=//' | tr -d ' \r')
    if [ "$PLATFORM" != "$CURRENT_PLATFORM" ]; then
        MANIFEST_BAK="$PKG/.manifest.bak"
        cp -p "$PKG/manifest" "$MANIFEST_BAK"
        py - packaging/fnos/manifest "$PLATFORM" <<'PY'
import re, sys
path, plat = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    text = f.read()
text = re.sub(r"(?m)^([ \t]*platform[ \t]*=).*$", lambda m: f"{m.group(1)} {plat}", text, count=1)
with open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)
PY
        echo "==> platform 临时改为 $PLATFORM（构建结束自动还原）"
    fi
fi

# ---------------------------------------------------------------- 0. 图标
if "$PY_BIN" -c "import PIL" >/dev/null 2>&1; then
    echo "==> 生成图标"
    py scripts/make_icons.py
else
    echo "==> 跳过图标生成（未安装 Pillow）；沿用 packaging/fnos 下已有图标"
fi

# ---------------------------------------------------------------- 1. 结构校验
echo "==> 校验打包目录结构"
py scripts/validate-package.py

# ---------------------------------------------------------------- 2. 构建镜像
mkdir -p "$IMG_DIR" "$DIST"

if [ "$NO_IMAGE" = "1" ]; then
    echo "==> --no-image：跳过镜像（产出的是瘦包，安装时需要联网拉取）"
elif [ -n "$IMAGE_TAR" ]; then
    # 本机没有 docker 时的正路：在别的机器上 --image-only 出 tar，拿回来出包。
    echo "==> --image-tar：使用外部构建好的镜像归档"
    if [ ! -f "$IMAGE_TAR" ]; then
        echo "【ERROR】镜像归档不存在：$IMAGE_TAR" >&2
        exit 1
    fi
    case "$IMAGE_TAR" in
        *.tar) IMG_EXT="tar" ;;
        *.tar.gz) IMG_EXT="tar.gz" ;;
        *.tgz) IMG_EXT="tgz" ;;
        *)
            echo "【ERROR】镜像归档必须是 .tar / .tar.gz / .tgz，收到：$IMAGE_TAR" >&2
            exit 1
            ;;
    esac
    # 先拷进包里再校验：后续只对 Python 暴露相对路径，
    # 绕开 Windows Git Bash 的 /d/... 绝对路径被 MSYS 二次改写的问题。
    rm -f "$IMG_DIR"/window-composer-*.tar \
          "$IMG_DIR"/window-composer-*.tar.gz \
          "$IMG_DIR"/window-composer-*.tgz 2>/dev/null || true
    OUT_TAR="$IMG_DIR/window-composer-$VERSION.$IMG_EXT"
    cp -f "$IMAGE_TAR" "$OUT_TAR"
    echo "    已放入 $IMG_REL/window-composer-$VERSION.$IMG_EXT（$(du -h "$OUT_TAR" | cut -f1)）"

    # 光有文件还不够：塞进去一个普通 tar 的话，NAS 上 docker load 会失败，
    # 而那时安装已经"看起来成功"了。这里先验它是 docker save / OCI 格式。
    echo "==> 校验镜像归档可被 docker load"
    py scripts/validate-package.py --image-tar "$IMG_REL/window-composer-$VERSION.$IMG_EXT"
elif [ "$SKIP_IMAGE" = "1" ]; then
    echo "==> --skip-image：复用 $IMG_REL 下已有镜像归档"
    if ! ls "$IMG_DIR"/window-composer-*.tar >/dev/null 2>&1 \
        && ! ls "$IMG_DIR"/window-composer-*.tar.gz >/dev/null 2>&1 \
        && ! ls "$IMG_DIR"/window-composer-*.tgz >/dev/null 2>&1; then
        echo "【ERROR】$IMG_REL 下没有镜像归档，请先跑一次完整构建或 --image-only" >&2
        exit 1
    fi
else
    if ! command -v docker >/dev/null 2>&1; then
        echo "【ERROR】本机未找到 docker，无法构建镜像。三种选择：" >&2
        echo "        1) 在装了 docker 的机器上执行：" >&2
        echo "           bash scripts/build-package.sh --image-only" >&2
        echo "           把 $IMG_REL/window-composer-$VERSION.tar 拷回本机，再执行：" >&2
        echo "           bash scripts/build-package.sh --image-tar <拷贝回来的 tar 路径>" >&2
        echo "        2) --skip-image 复用已有镜像归档" >&2
        echo "        3) --no-image 打瘦包（安装时需要联网，仅调试用）" >&2
        exit 1
    fi

    # ---- 构建期网络自适应 --------------------------------------------------
    # Docker Hub 与 deb.debian.org 在国内网络通常不可达。这里探测一次并自动
    # 换成国内镜像站；每一项都可以用环境变量固定，避免探测结果不符合实际。
    if [ -n "${WC_BASE_IMAGE+x}" ]; then
        BASE_IMAGE_ARG="$WC_BASE_IMAGE"
    elif reachable https://registry-1.docker.io/v2/; then
        BASE_IMAGE_ARG="debian:bookworm-slim"
    else
        BASE_IMAGE_ARG="${WC_REGISTRY_MIRROR:-docker.m.daocloud.io}/library/debian:bookworm-slim"
    fi

    if [ -n "${WC_APT_MIRROR+x}" ]; then
        APT_MIRROR_ARG="$WC_APT_MIRROR"   # 显式给出（空串 = 坚持用官方源，不换）
    elif reachable https://deb.debian.org/debian/dists/bookworm/Release; then
        APT_MIRROR_ARG=""
    else
        APT_MIRROR_ARG="https://mirrors.aliyun.com/debian"
    fi

    PIP_INDEX_ARG="${WC_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

    echo "==> 构建镜像 $IMAGE_REF"
    echo "    基础镜像：$BASE_IMAGE_ARG"
    echo "    Debian 源：${APT_MIRROR_ARG:-官方（未换源）}"
    echo "    PyPI 源：  $PIP_INDEX_ARG"

    BUILD_ARGS=(--build-arg "BASE_IMAGE=$BASE_IMAGE_ARG" --build-arg "PIP_INDEX=$PIP_INDEX_ARG")
    if [ -n "$APT_MIRROR_ARG" ]; then
        BUILD_ARGS+=(--build-arg "APT_MIRROR=$APT_MIRROR_ARG")
    fi
    docker build "${BUILD_ARGS[@]}" -t "$IMAGE_REF" "$ROOT"

    # 顺手做个自检：镜像里 Python 依赖与关键命令必须齐全，
    # 否则"装完就能用"会在 NAS 上变成"点开就报错"
    echo "==> 镜像自检"
    docker run --rm --entrypoint bash "$IMAGE_REF" -lc '
        # pipefail 必须开：测试结果通过管道给 tail，否则测试失败的退出码会被
        # tail 的 0 吞掉，坏镜像照样打出"正式" fpk。
        set -eo pipefail
        for b in Xorg openbox xdotool xrandr wmctrl scrot python3 chromium jq \
                 pipewire wireplumber pipewire-pulse pactl dbus-daemon; do
            command -v "$b" >/dev/null 2>&1 || { echo "缺少命令: $b"; exit 1; }
        done
        python3 -c "import fastapi, uvicorn, jinja2; print(\"python 依赖 OK\")"
        python3 /app/tests/test_offline.py 2>&1 | tail -3
    '

    echo "==> 导出镜像为离线 tar"
    rm -f "$IMG_DIR"/window-composer-*.tar \
          "$IMG_DIR"/window-composer-*.tar.gz \
          "$IMG_DIR"/window-composer-*.tgz 2>/dev/null || true
    OUT_TAR="$IMG_DIR/window-composer-$VERSION.tar"
    docker save -o "$OUT_TAR" "$IMAGE_REF"
    echo "    镜像包大小：$(du -h "$OUT_TAR" | cut -f1)"

    # 镜像架构 vs manifest.platform：不一致会在另一种架构的设备上装出"起不来"的包
    IMG_ARCH=$(docker image inspect -f '{{.Architecture}}' "$IMAGE_REF" 2>/dev/null || echo "")
    DECL_PLATFORM=$(grep -E '^[[:space:]]*platform[[:space:]]*=' "$PKG/manifest" | head -1 | sed 's/^[^=]*=//' | tr -d ' \r')
    case "$IMG_ARCH" in
        amd64) IMG_PLATFORM="x86" ;;
        arm64) IMG_PLATFORM="arm" ;;
        *) IMG_PLATFORM="" ;;
    esac
    if [ -n "$IMG_PLATFORM" ]; then
        if [ "$DECL_PLATFORM" != "$IMG_PLATFORM" ] && [ "$DECL_PLATFORM" != "all" ]; then
            echo "【ERROR】manifest.platform=$DECL_PLATFORM 但镜像是 $IMG_ARCH" >&2
            echo "        请用 --platform $IMG_PLATFORM 重新打包，否则另一种架构的设备会装上跑不起来的包" >&2
            exit 1
        fi
        if [ "$DECL_PLATFORM" = "all" ]; then
            echo "【WARN】manifest.platform=all，但包内镜像是 $IMG_ARCH。" >&2
            echo "       平台会允许 ARM 设备安装，而这些设备上容器起不来。" >&2
            echo "       建议用 --platform $IMG_PLATFORM 打包。" >&2
        fi
    fi
fi

# ---------------------------------------------------------------- 2.5 只出镜像
# 给"本机没有 docker"的分工用：在构建机上跑 --image-only 出 tar，
# 拷回本机再 --image-tar 出包。
if [ "$IMAGE_ONLY" = "1" ]; then
    TAR_NAME=$(ls "$IMG_DIR"/window-composer-*.tar 2>/dev/null | head -1 || true)
    echo
    echo "=============================================="
    echo " --image-only：镜像已导出，按需继续出包"
    echo " 归档：${TAR_NAME:-<未找到>}"
    echo " 拷到本机后执行："
    echo "   bash scripts/build-package.sh --image-tar <该 tar 的路径>"
    echo " 直接在本机构建镜像时，去掉 --image-only 即可一步出 .fpk"
    echo "=============================================="
    exit 0
fi

# ---------------------------------------------------------------- 3. 复校（含镜像）
if [ "$NO_IMAGE" = "0" ]; then
    echo "==> 复核（要求附带镜像 tar）"
    py scripts/validate-package.py --require-image
fi

# ---------------------------------------------------------------- 4. 生成 .fpk
# 瘦包（--no-image）显式加 -thin 后缀。它和完整包同名是危险的：用户很容易
# 把瘦包当正式包拿去安装，装完才发现 pull_policy: never 找不到本地镜像起不来。
FPK_NAME="$APPNAME-$VERSION"
if [ "$NO_IMAGE" = "1" ]; then
    FPK_NAME="$FPK_NAME-thin"
fi
FPK="$DIST/$FPK_NAME.fpk"
FPK_REL="dist/$FPK_NAME.fpk"
mkdir -p "$DIST"
rm -f "$FPK"
rm -f "$PKG/$APPNAME.fpk" "$PKG"/*.fpk 2>/dev/null || true

if [ -n "$FNPACK" ]; then
    echo "==> 用官方 fnpack 打包"
    ( cd "$PKG" && "$FNPACK" build )
    built=""
    for c in "$PKG/$APPNAME.fpk" "$PKG"/*.fpk "$ROOT/$APPNAME.fpk"; do
        [ -f "$c" ] && { built="$c"; break; }
    done
    if [ -n "$built" ]; then
        mv -f "$built" "$FPK"
    else
        echo "【WARN】fnpack 执行成功但没找到产物，改用 make-fpk.py" >&2
    fi
fi

if [ ! -f "$FPK" ]; then
    echo "==> 用 scripts/make-fpk.py 打包（复刻 fnpack 的 gzip/tar 结构）"
    py scripts/make-fpk.py packaging/fnos "$FPK_REL"
fi

# ---------------------------------------------------------------- 5. 产物自检
# 这一步专治历史上那个坑：产物是 zip 而不是 gzip/tar.gz，
# 飞牛会直接报「不是有效的程序文件」。打包脚本自己先验一遍。
echo "==> 校验产出的 .fpk"
py scripts/validate-package.py --fpk "$FPK_REL"

echo
echo "=============================================="
echo " 完成：$FPK"
echo " 大小：$(du -h "$FPK" | cut -f1)"
echo " 版本：$VERSION   镜像：$IMAGE_REF"
if [ "$NO_IMAGE" = "1" ]; then
    echo " 【注意】这是瘦包（不含镜像）：只能用来验证飞牛是否接受该包格式，"
    echo "        装完后容器会因 pull_policy: never 找不到本地镜像而起不来。"
    echo "        正式包请在有 docker 的机器上跑：bash scripts/build-package.sh"
fi
echo " 安装：飞牛 fnOS『应用中心 → 手动安装』选择上面的 .fpk"
echo "=============================================="
