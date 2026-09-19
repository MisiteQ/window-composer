# window-composer 生产镜像（自包含，不依赖任何私有基础镜像）
#
# 目标：装完即用——NAS 端不需要再装/下载任何其它软件。
# chromium（应用显示引擎）、Xorg + modesetting/dummy 驱动、openbox、
# xdotool/xrandr/wmctrl/scrot、PipeWire 音频、Python + FastAPI 全部预装在本镜像里。
#
# 显示输出：Xorg 的 modesetting 驱动走内核 KMS/DRM，对接口类型无感——
# HDMI / DisplayPort / USB-C(DP Alt Mode) / VGA / DVI / eDP 都是同一条路径，
# 由 entrypoint.sh 运行时按 /sys/class/drm 的连接器状态自动选择 DRM 卡并生成配置。
#
# 构建：
#   docker build -t window-composer:1.0.0 .
# 离线构建（先把 wheel 放到 vendor/wheels/，见 scripts/fetch-wheels.sh）：
#   docker build -t window-composer:1.0.0 .
#
# ---------------------------------------------------------------- 构建期网络参数
# 这台机器能不能直连 Docker Hub / deb.debian.org，完全取决于你所在的网络
# （国内常见：Docker Hub 与 GitHub 不通，但阿里/清华的 Debian 源、
#  daocloud 的 Docker Hub 镜像通）。下面三个 ARG 让你**不改 Dockerfile** 就能换源，
# 默认值仍是官方源 —— 所以无网络限制的机器构建结果与行为完全不变。
#
#   BASE_IMAGE            基础镜像引用
#   APT_MIRROR            Debian 主源前缀，如 https://mirrors.aliyun.com/debian
#   APT_SECURITY_MIRROR   安全源前缀；留空则按 APT_MIRROR 自动推导
#   PIP_INDEX             PyPI 源
#
# 例（Docker Hub 不可达时）：
#   docker build \
#     --build-arg BASE_IMAGE=docker.m.daocloud.io/library/debian:bookworm-slim \
#     --build-arg APT_MIRROR=https://mirrors.aliyun.com/debian \
#     -t window-composer:1.0.0 .
# 一般不必手敲：scripts/build-package.sh 会先探测网络再自动带上这些参数。
ARG BASE_IMAGE=debian:bookworm-slim

FROM ${BASE_IMAGE}

# ARG 在 FROM 之后要重新声明，后续 RUN 里才可见
ARG APT_MIRROR=""
ARG APT_SECURITY_MIRROR=""
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian 源切换：只在显式传入 APT_MIRROR 时执行。
# 同时覆盖 sources.list 与 deb822 形态的 sources.list.d/*.sources
# （bookworm 官方镜像已改用 deb822，只改 sources.list 是不生效的）。
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        sec="${APT_SECURITY_MIRROR:-$(printf '%s' "$APT_MIRROR" | sed -E 's|/[^/]+$||')/debian-security}"; \
        files=$(ls /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list 2>/dev/null || true); \
        if [ -z "$files" ]; then echo "【WARN】未找到 apt 源文件，跳过换源"; exit 0; fi; \
        for f in $files; do \
            sed -i -E \
                -e "s|https?://deb\.debian\.org/debian|${APT_MIRROR}|g" \
                -e "s|https?://security\.debian\.org/debian-security|${sec}|g" \
                "$f"; \
        done; \
        echo "--- apt 源已切换：$APT_MIRROR / $sec ---"; \
        cat $files; \
    fi

# ------------------------------------------------------------------ 系统依赖
# X 服务器与驱动：
#   xserver-xorg-core                   X 服务器本体
#   xserver-xorg-video-modesetting      KMS/DRM 通用驱动（HDMI/DP/USB-C/VGA/DVI 全靠它）
#   xserver-xorg-video-dummy            无 DRM 时提供虚拟输出（无显示器也能配置布局）
# 窗口管理 / 控制：
#   openbox  轻量 X11 WM（ECWMH 完整，xdotool 移动/缩放精确生效）
#   xdotool x11-utils x11-xserver-utils (xrandr/xsetroot/xdpyinfo/xwininfo/xprop)
#   wmctrl   发 EWMH ClientMessage 实现真全屏
#   x11-apps 自带 xlogo/xedit/xcalc，用于上机验证显示链路
# 截图：scrot
# 音频：pipewire + pipewire-pulse（PulseAudio 兼容层）+ wireplumber（会话管理器，
#   物理 ALSA 声卡靠它枚举；缺失时 pactl 里永远只有 auto_null/Dummy Output）
#   + pulseaudio-utils（提供 pactl！注意 pipewire-pulse 只 Recommends
#   pulseaudio-utils，--no-install-recommends 下不会自动装，漏掉它音频管理功能
#   会整体失效）+ alsa-utils 诊断
# 会话：dbus / dbus-x11（PipeWire 依赖 session bus；wireplumber 还要 system bus，
#   由 entrypoint.sh 在运行时手动拉起 dbus-daemon --system）
# 显示引擎：chromium（代码自适应，可换 firefox）
# 字体：fonts-noto-cjk —— 没有 CJK 字体时，飞牛应用的中文界面全是方块
# 工具：jq/coreutils/procps/ca-certificates/tzdata
# 说明：apt 带重试与超时参数。构建机网络抖动时，一次失败让整次构建白跑代价很高。
RUN apt-get update \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        -o Acquire::https::Timeout=30 \
    && apt-get install -y --no-install-recommends \
        xserver-xorg-core \
        xserver-xorg-video-modesetting \
        xserver-xorg-video-dummy \
        x11-xserver-utils \
        x11-utils \
        x11-apps \
        openbox \
        xdotool \
        wmctrl \
        scrot \
        pipewire \
        pipewire-pulse \
        pipewire-alsa \
        wireplumber \
        pulseaudio-utils \
        alsa-utils \
        dbus \
        dbus-x11 \
        chromium \
        fonts-noto-cjk \
        fonts-dejavu-core \
        libgl1-mesa-dri \
        jq \
        coreutils \
        procps \
        ca-certificates \
        tzdata \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------------ Python 依赖
# 优先使用 vendor/wheels 里的离线 wheel（无网络也能构建）；
# 没有该目录时走国内镜像安装。两种情况都不影响"安装到 NAS 后无需再下载"。
COPY vendor/ /tmp/vendor/
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    if [ -d /tmp/vendor/wheels ] && [ -n "$(ls -A /tmp/vendor/wheels 2>/dev/null)" ]; then \
        pip3 install --break-system-packages --no-cache-dir \
            --no-index --find-links=/tmp/vendor/wheels -r /tmp/requirements.txt; \
    else \
        pip3 install --break-system-packages --no-cache-dir \
            -i "$PIP_INDEX" -r /tmp/requirements.txt; \
    fi; \
    rm -rf /tmp/vendor /tmp/requirements.txt

# uid 1000 用户：dbus-launch 需要在 /etc/passwd 查到当前 uid，
# pipewire 需要 HOME 可写。容器默认以 root 跑（Xorg 打开 DRM master 需要），
# 该用户供 --user 1000 场景与 dbus 使用。
RUN useradd -u 1000 -m -s /bin/bash appuser

WORKDIR /app
COPY ./src /app/src
# openbox 配置：必须覆盖 Debian 默认的 /etc/xdg/openbox/rc.xml。
# 默认配置带窗口贴边阻力（resistance）与屏幕边缘吸附，xdotool windowmove
# 的坐标会被 WM 二次修正，导致画框布局"差几个像素"对不齐。rc.xml 里把
# strength / screen_edge_strength 设为 0 后移动才精确落位。
COPY ./rc.xml /etc/xdg/openbox/rc.xml
# 离线自检脚本：容器内 `python3 /app/tests/test_offline.py` 直接可跑
COPY ./tests /app/tests
COPY entrypoint.sh /entrypoint.sh
# Windows/sftp 上传的文件权限可能是 600；统一放宽为 a+rX。
# entrypoint.sh 用 bash 显式调用，规避上传丢失执行位的问题。
RUN chmod -R a+rX /app/src /app/tests && chmod 755 /entrypoint.sh

# Web 控制面板端口（可在飞牛应用设置里改）
EXPOSE 8181

# 健康检查：Web 服务活着即视为健康（显示栈由 entrypoint 主循环守护）
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8181/', timeout=4).status==200 else 1)" \
    || exit 1

ENTRYPOINT ["bash", "/entrypoint.sh"]
