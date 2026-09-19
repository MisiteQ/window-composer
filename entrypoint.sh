#!/bin/bash
set -e

# =====================================================================
# window-composer 容器入口
#
# 显示栈：Xorg（modesetting/DRM）+ openbox（X11 窗口管理器）
# 接口无关：Xorg 的 modesetting 驱动走内核 KMS/DRM，HDMI、DisplayPort、
#           USB-C（DP Alt Mode / 雷电扩展坞）、VGA、DVI、eDP 都是同一条
#           渲染路径。本脚本不做接口分支，只在启动时按 /sys/class/drm
#           的连接器状态挑一张"真的有显示器接在上面"的 DRM 卡并生成配置。
#
# 为什么用 Xorg 而不是 Weston：Weston 的 Xwayland WM 会强制覆盖所有 X11
# 窗口位置（xdotool windowmove 与 chromium --window-position 均无效），
# 无法实现"画框布局、边缘吸附、多窗口不重叠"。Xorg + openbox 下
# xdotool windowmove/windowsize 精确生效。
#
# docker run 需要：
#   --device /dev/dri
#   --device-cgroup-rule='c 13:* rmw' -v /dev/input:/dev/input   （输入设备，可选）
#   -v /run/udev:/run/udev:ro                                    （物理音频必需：
#                                                                 WirePlumber 枚举
#                                                                 ALSA 声卡的数据源；
#                                                                 同时支持显示热插拔）
# 容器以 root 运行（Xorg 打开 DRM master 需要）。
#
# 三级降级：modesetting → modesetting + sharevts → dummy 虚拟输出。
# 前两级失败时仍会用 dummy 驱动把容器拉起来，保证控制面板可用
# （可以先配好布局、截好图，再补上显卡设备映射）。
# =====================================================================

# ---------------------------------------------------------------- 基础环境
# 按实际 uid 选择 XDG_RUNTIME_DIR（PipeWire 需要）
CURRENT_UID=$(id -u)
if [ "$CURRENT_UID" = "0" ]; then
    export XDG_RUNTIME_DIR=/run/user/0
else
    export XDG_RUNTIME_DIR=/run/user/1000
fi
mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
mkdir -p /data 2>/dev/null || true

# HOME 默认是 /（不可写），pipewire-pulse 会尝试创建状态目录失败
export HOME=/home/appuser
mkdir -p "$HOME/.local/state" 2>/dev/null || true

LOG_FILE="/data/app.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE" 2>/dev/null || \
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# ===== 日志轮转 =====
# 容器长期运行（数月）时 $LOG_FILE 会持续增长，持久卷有被撑爆的风险
# （日志文件是唯一只增不减的持久化数据）。超过阈值时只保留尾部若干行。
LOG_MAX_BYTES=${LOG_MAX_BYTES:-2097152}   # 默认 2MB
LOG_KEEP_LINES=${LOG_KEEP_LINES:-2000}
rotate_log() {
    local f="$1"
    [ -f "$f" ] || return 0
    local size
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
    if [ "$size" -le "$LOG_MAX_BYTES" ] 2>/dev/null; then
        return 0
    fi
    if tail -n "$LOG_KEEP_LINES" "$f" > "$f.rotating" 2>/dev/null; then
        mv "$f.rotating" "$f" 2>/dev/null || true
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 【INFO】日志超过 $((LOG_MAX_BYTES / 1024))KB，已轮转，仅保留尾部 $LOG_KEEP_LINES 行" >> "$f"
    else
        rm -f "$f.rotating" 2>/dev/null || true
    fi
}

rotate_log "$LOG_FILE"

log "===== 系统启动 ====="

# ---------------------------------------------------------------- 读取配置
WEB_PORT=8181
BG_COLOR="#000000"
AUTO_START=true
SETTINGS_FILE="/data/settings.json"

if [ -f "$SETTINGS_FILE" ]; then
    WEB_PORT=$(jq -r '.web_port // 8181' "$SETTINGS_FILE" 2>/dev/null || echo 8181)
    BG_COLOR=$(jq -r '.bg_color // "#000000"' "$SETTINGS_FILE" 2>/dev/null || echo "#000000")
    AUTO_START=$(jq -r '.auto_start // true' "$SETTINGS_FILE" 2>/dev/null || echo true)
fi
# 环境变量 WC_AUTO_START 可强制覆盖（安装向导/命令行 docker run 用）
case "${WC_AUTO_START:-}" in
    true|false) AUTO_START="$WC_AUTO_START" ;;
esac
log "读取配置：Web端口=$WEB_PORT，背景色=$BG_COLOR，开机自启=$AUTO_START"

# ---------------------------------------------------------------- 依赖自检
# 镜像已经把全部依赖预装好；万一有人基于精简版改造，这里能第一时间
# 指出缺了什么，而不是等到"点了没反应"再去猜。
check_deps() {
    local missing="" bin
    for bin in Xorg openbox xdotool xwininfo xprop xrandr wmctrl scrot \
               dbus-launch dbus-daemon dbus-uuidgen \
               pipewire wireplumber pipewire-pulse pactl jq python3; do
        command -v "$bin" >/dev/null 2>&1 || missing="$missing $bin"
    done
    if [ -n "$missing" ]; then
        log "【WARN】镜像内缺少命令：$missing —— 对应功能将不可用"
    else
        log "依赖自检通过：Xorg / 窗口控制 / 截图 / 音频 / 工具链齐全"
    fi
}
check_deps

# ---------------------------------------------------------------- 飞牛应用挂载检查
# /var/apps/<应用>/target 是绝对软链：第三方应用 -> /volN/@appcenter/<应用>，
# 飞牛内置应用（trim.*）-> /usr/local/apps/@appcenter/<应用>。
# 只挂 /var/apps 时软链目标在容器内不存在：能扫到应用名，却读不到
# target/ui/config（Web 入口为空），表现为"飞牛应用扫不出来/点不开"。
# 启动时点名一次，避免对着面板猜。
check_host_apps() {
    if [ ! -d /host_apps ]; then
        log "【WARN】未挂载宿主 /var/apps（容器内 /host_apps 不存在）：飞牛应用分组将为空，仅能扫描 Docker 容器"
        return 0
    fi
    local total=0 broken=0 names="" t
    shopt -s nullglob
    for t in /host_apps/*/target; do
        total=$((total + 1))
        if [ ! -e "$t" ]; then
            broken=$((broken + 1))
            names="$names $(basename "$(dirname "$t")")"
        fi
    done
    shopt -u nullglob
    if [ "$broken" -gt 0 ]; then
        log "【WARN】$broken/$total 个飞牛应用的 target 软链在容器内断裂（如：$names ）"
        log "【WARN】请在 compose 补挂软链目标（:ro）：/vol1~/volN/@appcenter 与 /usr/local/apps/@appcenter，否则飞牛应用无 Web 入口"
    elif [ "$total" -gt 0 ]; then
        log "飞牛应用挂载检查通过：$total 个应用 target 可访问"
    fi
}
check_host_apps

# 浏览器是"把飞牛应用 Web 界面渲染到显示器"的引擎，缺失时整个应用显示不可用
BROWSER_BIN=""
for b in chromium chromium-browser google-chrome firefox firefox-esr; do
    if command -v "$b" >/dev/null 2>&1; then BROWSER_BIN="$b"; break; fi
done
if [ -n "$BROWSER_BIN" ]; then
    log "显示引擎：$BROWSER_BIN"
else
    log "【WARN】镜像内未检测到浏览器（chromium/firefox），无法把应用界面显示到显示器"
fi

# ---------------------------------------------------------------- dbus / socket
# dbus session：PipeWire / pipewire-pulse / wireplumber 都依赖 session bus
if command -v dbus-launch >/dev/null 2>&1; then
    log "启动 dbus session"
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID
else
    log "【WARN】未找到 dbus-launch，PipeWire 可能连不上 session bus"
fi

# ---------------------------------------------------------------- dbus system
# WirePlumber 的 logind 插件要求：
#   1) dbus 系统总线可用（容器没有 systemd，手动拉起 dbus-daemon --system）
#   2) machine-id 存在（系统总线身份标识，缺失时总线拒绝启动）
#   3) /run/systemd/{users,seats,sessions} 目录存在 —— logind 插件启动即
#      inotify_add_watch 这些路径，目录缺失会直接 ENOENT 中止插件，
#      wireplumber 随后与 PipeWire 断开并退出（实测 0.4.13 非 WARN 而是致命）
# 另外 ALSA 设备枚举依赖宿主 udev 数据，compose 需挂 /run/udev:/run/udev:ro。
if command -v dbus-uuidgen >/dev/null 2>&1; then
    dbus-uuidgen --ensure /etc/machine-id 2>/dev/null || true
    dbus-uuidgen --ensure /var/lib/dbus/machine-id 2>/dev/null || true
fi
mkdir -p /run/dbus /run/systemd/users /run/systemd/seats /run/systemd/sessions 2>/dev/null || true
# docker restart 复用容器可写层，/run 里会留下上次的失效 socket（守护进程已死），
# 只判断 socket 存在会跳过启动；用 Ping 探活，失效则清掉重建。
system_bus_alive() {
    # 精简镜像没有 dbus-send 时退化为"socket 存在即可"
    command -v dbus-send >/dev/null 2>&1 || { [ -S /run/dbus/system_bus_socket ]; return; }
    dbus-send --system --dest=org.freedesktop.DBus \
        --print-reply / org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1
}
if command -v dbus-daemon >/dev/null 2>&1 && ! system_bus_alive; then
    log "启动 dbus system（供 WirePlumber logind 插件使用）"
    # restart 复用容器层：socket/pidfile 都是上次的残留，不清掉新守护进程会拒绝启动
    rm -f /run/dbus/system_bus_socket /var/run/dbus/pid 2>/dev/null || true
    if ! dbus-daemon --system --fork 2>/dev/null; then
        log "【WARN】dbus system 启动失败，WirePlumber 可能无法接管物理声卡"
    fi
fi

unset WAYLAND_DISPLAY
rm -f "$XDG_RUNTIME_DIR"/wayland-[0-9]* 2>/dev/null || true

# X11 socket 目录 + 清理残留（避免 docker restart 后显示号递增到 :1/:2）
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
rm -f /tmp/.X11-unix/X[0-9]* /tmp/.X[0-9]*-lock 2>/dev/null || true

# ---------------------------------------------------------------- 音频
# PipeWire 音频核心 + WirePlumber 会话管理器 + pipewire-pulse 兼容层。
# WirePlumber 负责枚举 ALSA 物理声卡并创建真实 sink；没有它物理音频不可用。
log "启动 PipeWire"
pipewire &
PIPEWIRE_PID=$!
sleep 2

WIREPLUMBER_PID=""
if command -v wireplumber >/dev/null 2>&1; then
    log "启动 WirePlumber（ALSA 物理声卡会话管理器）"
    wireplumber >/data/wireplumber.log 2>&1 &
    WIREPLUMBER_PID=$!
    # 设备枚举需要几秒，等它完成再启动 pulse 兼容层
    sleep 3
    if ! kill -0 "$WIREPLUMBER_PID" 2>/dev/null; then
        log "【WARN】WirePlumber 启动后退出，物理声卡可能不可用（见 /data/wireplumber.log）"
        WIREPLUMBER_PID=""
    fi
else
    log "【WARN】未安装 wireplumber，将只有 Dummy 输出（auto_null），物理声卡不可用"
fi

if command -v pipewire-pulse >/dev/null 2>&1; then
    log "启动 pipewire-pulse（PulseAudio 兼容层）"
    pipewire-pulse &
    PULSE_PID=$!
    sleep 1
else
    PULSE_PID=""
    log "【WARN】未安装 pipewire-pulse，音频管理功能不可用"
fi

# 默认输出纠偏：多声卡机器（飞牛实测：sof-hda-dsp + 主板 pcspkr 蜂鸣器）上
# WirePlumber 可能把 pcspkr 选成默认 sink，声音会从蜂鸣器放出。
# 优先应用面板里持久化的用户选择；没有选择则避开蜂鸣器/虚拟设备挑真实声卡。
# 后台执行（sink 枚举可能晚几秒），不阻塞 Xorg 启动。
init_default_sink() {
    command -v pactl >/dev/null 2>&1 || return 0
    local saved tries cur sinks s
    saved=$(jq -r '.default_audio_sink // empty' "$SETTINGS_FILE" 2>/dev/null || true)
    for tries in 1 2 3 4 5; do
        if [ -n "$saved" ]; then
            if pactl set-default-sink "$saved" 2>/dev/null; then
                log "应用已保存的默认音频输出：$saved"
                return 0
            fi
        else
            cur=$(pactl get-default-sink 2>/dev/null || true)
            case "$cur" in
                *pcspkr*|*beep*|*dummy*|*auto_null*)
                    sinks=$(pactl list short sinks 2>/dev/null | awk '{print $2}')
                    for s in $sinks; do
                        case "$s" in
                            *pcspkr*|*beep*|*dummy*|*auto_null*) continue ;;
                        esac
                        if pactl set-default-sink "$s" 2>/dev/null; then
                            log "默认音频输出纠偏：$cur -> $s（蜂鸣器/虚拟设备不作系统默认）"
                            return 0
                        fi
                    done
                    ;;
                *)
                    [ -n "$cur" ] && return 0
                    ;;
            esac
        fi
        sleep 2
    done
}
init_default_sink &

# ---------------------------------------------------------------- 显示设备探测
# 多显卡机器（例如服务器板载 BMC VGA + Intel iGPU）上卡号不固定，
# 写死 /dev/dri/card0 可能驱动到错误的设备；这里按 sysfs 里连接器的
# "connected" 状态挑一张真的有显示器接在上面的卡。
# SYS_DRM / DEV_DRI 可以重定向（tests/test_entrypoint_drm.sh 用假 sysfs 树
# 覆盖这两条最容易出错的规则，不需要真实 Linux 硬件）。
SYS_DRM=${WC_SYS_DRM_ROOT:-/sys/class/drm}
DEV_DRI=${WC_DEV_DRI_DIR:-/dev/dri}

detect_drm_card() {
    local path name cpath best="" first=""
    for path in "$SYS_DRM"/card[0-9]*; do
        [ -e "$path" ] || continue
        name=$(basename "$path")
        case "$name" in *-*) continue ;; esac   # 跳过 card0-HDMI-A-1 这类连接器目录
        case "$name" in card[0-9]*) ;; *) continue ;; esac
        [ -e "$DEV_DRI/$name" ] || continue
        [ -n "$first" ] || first="$name"
        for cpath in "$SYS_DRM/$name"-*/status; do
            [ -r "$cpath" ] || continue
            if [ "$(cat "$cpath" 2>/dev/null)" = "connected" ]; then
                best="$name"
                break
            fi
        done
        [ -n "$best" ] && break
    done
    if [ -n "$best" ]; then echo "$best"; return 0; fi
    if [ -n "$first" ]; then echo "$first"; return 0; fi
    # sysfs 里拿不到信息（受限容器 / 老内核）时退回 card0
    [ -e "$DEV_DRI/card0" ] && echo "card0" || echo ""
}

# 列出内核看到的全部显示连接器（HDMI-A-1 / DP-1 / VGA-1 / DVI-I-1 / eDP-1 …）
list_connectors() {
    local path name out=""
    for path in "$SYS_DRM"/card*-*/status; do
        [ -r "$path" ] || continue
        name=$(basename "$(dirname "$path")")
        out="$out $name:$(cat "$path" 2>/dev/null)"
    done
    echo "${out:- （无）}"
}

DRM_CARD=""
if [ -d "$DEV_DRI" ]; then
    DRM_CARD=$(detect_drm_card)
fi
export WC_DRM_CARD="$DRM_CARD"

log "内核显示连接器：$(list_connectors)"
if [ -n "$DRM_CARD" ]; then
    log "选中 DRM 设备：/dev/dri/$DRM_CARD"
else
    log "【WARN】未找到可用的 /dev/dri/card*：容器可能未映射显卡设备"
fi

# ---------------------------------------------------------------- Xorg 配置生成
# 不把配置文件写死在镜像里：卡号、驱动选项都随宿主机硬件变化，
# 启动时按实际探测结果生成，避免"换台机器就要重新打镜像"。
XORG_CONF_DIR=/tmp/xorg
mkdir -p "$XORG_CONF_DIR/conf.d"
CONF_DRM="$XORG_CONF_DIR/modesetting.conf"
CONF_DUMMY="$XORG_CONF_DIR/dummy.conf"

if [ -n "$DRM_CARD" ]; then
    cat > "$CONF_DRM" <<EOF
# 由 entrypoint.sh 运行时生成
Section "Device"
    Identifier "GPU"
    Driver "modesetting"
    Option "kmsdev" "/dev/dri/$DRM_CARD"
EndSection
Section "Screen"
    Identifier "Default"
    Device "GPU"
EndSection
EOF
fi

# 虚拟输出兜底：没有 DRM（容器没映射 /dev/dri）也能把容器拉起来，
# 让控制面板、布局编辑、截图可用
cat > "$CONF_DUMMY" <<'EOF'
# 由 entrypoint.sh 运行时生成（无显示器/无 DRM 时的虚拟输出）
Section "Device"
    Identifier "Virtual"
    Driver "dummy"
    VideoRam 32768
EndSection
Section "Monitor"
    Identifier "VirtualMonitor"
    HorizSync 5.0 - 1000.0
    VertRefresh 5.0 - 200.0
    Modeline "1920x1080" 148.50 1920 2008 2052 2200 1080 1084 1089 1125 +HSync +VSync
EndSection
Section "Screen"
    Identifier "Default"
    Device "Virtual"
    Monitor "VirtualMonitor"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
        Virtual 1920 1080
        Modes "1920x1080"
    EndSubSection
EndSection
EOF

# 允许用户自带 Xorg 配置（特殊显卡 / 特殊时序场景）
WC_XORG_CONF=${WC_XORG_CONF:-}
if [ -n "$WC_XORG_CONF" ] && [ -f "$WC_XORG_CONF" ]; then
    CONF_DRM="$WC_XORG_CONF"
    log "使用用户指定的 Xorg 配置：$WC_XORG_CONF"
fi

XORG_BIN="/usr/lib/xorg/Xorg"
[ -x "$XORG_BIN" ] || XORG_BIN="$(command -v Xorg || true)"
if [ -z "$XORG_BIN" ]; then
    log "【ERROR】容器内未找到 Xorg，镜像不完整（应安装 xserver-xorg-core）"
    exit 1
fi

# 启动一次 Xorg 并等 X0 socket；返回 0 表示成功
XORG_PID=""
start_xorg() {
    local conf="$1"; shift
    rm -f /tmp/.X11-unix/X0 /tmp/.X0-lock 2>/dev/null || true
    "$XORG_BIN" :0 \
        -config "$conf" \
        -configdir "$XORG_CONF_DIR/conf.d" \
        -noreset -background none \
        "$@" >>/data/xorg.log 2>&1 &
    local pid=$!
    local i
    for i in $(seq 1 15); do
        if [ -S /tmp/.X11-unix/X0 ]; then
            XORG_PID=$pid
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f /tmp/.X11-unix/X0 /tmp/.X0-lock 2>/dev/null || true
    return 1
}

# ===== 三级降级启动 Xorg =====
# 1) modesetting（正常路径：真实显示输出，HDMI/DP/USB-C/VGA/DVI 通用）
# 2) modesetting + -sharevts -novtswitch（部分宿主环境不允许容器切 VT）
# 3) dummy 虚拟输出（没有 DRM 设备，或显卡驱动加载失败）
XORG_MODE=""
if [ -n "$DRM_CARD" ]; then
    log "启动 Xorg（modesetting/DRM → /dev/dri/$DRM_CARD）"
    if start_xorg "$CONF_DRM"; then
        XORG_MODE="drm"
        log "【INFO】Xorg 已就绪（DRM 真实输出）"
    else
        log "【WARN】modesetting 启动失败，改用 sharevts 模式重试（详见 /data/xorg.log）"
        if start_xorg "$CONF_DRM" -sharevts -novtswitch; then
            XORG_MODE="drm"
            log "【INFO】Xorg 已就绪（DRM + sharevts）"
        fi
    fi
fi

if [ -z "$XORG_MODE" ]; then
    log "【WARN】无法使用 DRM 真实输出，降级为 dummy 虚拟输出 1920x1080"
    log "        控制面板/布局编辑/截图仍可用，但真实显示器不会出画面；"
    log "        请确认容器以 root 运行并映射了 --device /dev/dri"
    if start_xorg "$CONF_DUMMY" -sharevts -novtswitch; then
        XORG_MODE="virtual"
        log "【INFO】Xorg 已就绪（虚拟输出 1920x1080）"
    fi
fi

if [ -z "$XORG_MODE" ]; then
    log "【ERROR】Xorg 三种模式均启动失败，容器终止。详见 /data/xorg.log"
    exit 1
fi

export DISPLAY=:0
# 供 Web 层判断"虚拟输出"与"探测失败"的区别（/api/status 的 backend 字段）
export WC_XORG_MODE="$XORG_MODE"

# ===== openbox（轻量 X11 WM，允许 xdotool 精确控制窗口几何）=====
sleep 1
log "启动 openbox 窗口管理器"
openbox --config-file /etc/xdg/openbox/rc.xml >/data/openbox.log 2>&1 &
OPENBOX_PID=$!
sleep 1
if ! kill -0 "$OPENBOX_PID" 2>/dev/null; then
    log "【WARN】openbox 启动失败（可能已有 WM），窗口位置控制将不可用，见 /data/openbox.log"
fi

# 背景色（可选，xsetroot 存在时应用到根窗口）
if command -v xsetroot >/dev/null 2>&1 && [[ "$BG_COLOR" =~ ^#[0-9A-Fa-f]{6}$ ]]; then
    xsetroot -solid "$BG_COLOR" 2>/dev/null || true
fi

# 记录实际枚举到的输出：排障时第一眼就能看出"接口认到了没有"
if command -v xrandr >/dev/null 2>&1; then
    log "xrandr 输出：$(xrandr --current 2>/dev/null | grep -E ' (connected|disconnected)' | tr '\n' ' ' | tr -s ' ')"
fi

# ---------------------------------------------------------------- Web 服务
# 把"开机自启"开关同步成 Docker 重启策略：auto_start=false → no，
# true → unless-stopped。必须在 uvicorn 启动前执行（用户在面板里改设置后
# 重启容器即生效）。socket 未挂载（非飞牛/裸 docker run）时静默跳过，
# 此时该开关仅作为配置记录。
sync_auto_start() {
    local policy="unless-stopped"
    [ "$AUTO_START" = "false" ] && policy="no"
    if [ -S /var/run/docker.sock ] && command -v python3 >/dev/null 2>&1; then
        if out=$(cd /app/src && python3 -m app_scanner restart-policy "$policy" 2>&1); then
            log "开机自启策略已同步：restart=$policy（$out）"
        else
            log "【WARN】开机自启策略同步失败（$out），开关仅记录在配置中"
        fi
    else
        log "未挂载 docker.sock，开机自启开关仅记录在配置中（平台/手工启动时生效）"
    fi
}
sync_auto_start

log "启动 FastAPI Web服务，端口 $WEB_PORT"
cd /app/src
uvicorn main:app --host 0.0.0.0 --port "$WEB_PORT" &
WEB_PID=$!

log "全部服务启动完成，进入进程监控循环"

TICK=0
while true; do
    # 每约 60 秒检查一次日志体积并轮转。
    # xorg.log/openbox.log 同样在持久卷上只增不减：显卡驱动异常刷屏时
    # （不断重试 modeset）可能比 app.log 更快撑爆 /data，一视同仁轮转。
    TICK=$((TICK + 1))
    if [ $((TICK % 60)) -eq 0 ]; then
        rotate_log "$LOG_FILE"
        rotate_log /data/xorg.log
        rotate_log /data/openbox.log
        rotate_log /data/wireplumber.log
    fi
    if ! kill -0 "$XORG_PID" 2>/dev/null; then
        log "【ERROR】Xorg 进程退出，容器终止"
        exit 1
    fi
    if ! kill -0 "$PIPEWIRE_PID" 2>/dev/null; then
        log "【ERROR】PipeWire进程退出，容器终止"
        exit 1
    fi
    # WirePlumber 退出只影响物理音频（降级为 Dummy），不终止容器，自动拉起
    if [ -n "$WIREPLUMBER_PID" ] && ! kill -0 "$WIREPLUMBER_PID" 2>/dev/null; then
        if command -v wireplumber >/dev/null 2>&1; then
            log "【WARN】WirePlumber 退出，2 秒后重新启动"
            sleep 2
            wireplumber >/data/wireplumber.log 2>&1 &
            WIREPLUMBER_PID=$!
        fi
    fi
    if [ -n "$PULSE_PID" ] && ! kill -0 "$PULSE_PID" 2>/dev/null; then
        log "【WARN】pipewire-pulse 退出，尝试重启"
        pipewire-pulse &
        PULSE_PID=$!
    fi
    # openbox 挂掉会导致窗口位置无法控制，自动拉起
    if ! kill -0 "$OPENBOX_PID" 2>/dev/null; then
        log "【WARN】openbox 退出，2 秒后重新启动"
        sleep 2
        openbox --config-file /etc/xdg/openbox/rc.xml >/data/openbox.log 2>&1 &
        OPENBOX_PID=$!
    fi
    # Web 服务挂掉不影响 Xorg/PipeWire；自动重启 Web，保证可远程控制
    if ! kill -0 "$WEB_PID" 2>/dev/null; then
        log "【WARN】Web服务退出，5 秒后自动重启"
        sleep 5
        cd /app/src
        uvicorn main:app --host 0.0.0.0 --port "$WEB_PORT" &
        WEB_PID=$!
        log "【INFO】Web服务已重启，新 PID=$WEB_PID"
    fi
    sleep 1
done
