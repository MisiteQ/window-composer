#!/usr/bin/env bash
# 离线校验 entrypoint.sh 里"选哪张 DRM 卡"的规则。
#
# 为什么单独测这段：飞牛 NAS 上常见的硬件形态是"板载 BMC VGA + Intel iGPU"
# 或"独显 + 集显"，card0 未必是接了显示器的那张。选错卡 = 显示器全黑，
# 而这类问题在真机上排查成本极高。这里用假 sysfs 树覆盖全部分支，
# 不需要 Linux 显卡，Windows(Git Bash)/macOS/Linux 都能跑。
#
#   bash tests/test_entrypoint_drm.sh
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ENTRY="$HERE/../entrypoint.sh"

# 只抽取两个探测函数（不执行 entrypoint 主体，否则会真的去起 Xorg）
FUNCS=$(sed -n '/^detect_drm_card()/,/^}/p;/^list_connectors()/,/^}/p' "$ENTRY")
if [ -z "$FUNCS" ]; then
    echo "无法从 $ENTRY 抽取探测函数（函数名或缩进被改动？）"
    exit 1
fi
eval "$FUNCS"

ROOT=$(mktemp -d 2>/dev/null || mktemp -d -t wc)
trap 'rm -rf "$ROOT"' EXIT
SYS_DRM="$ROOT/sys/class/drm"
DEV_DRI="$ROOT/dev/dri"
mkdir -p "$SYS_DRM" "$DEV_DRI"

_pass=0
_fail=0
ck() {  # ck 描述 实际值 期望值
    if [ "$2" = "$3" ]; then
        _pass=$((_pass + 1))
        echo "  [PASS] $1"
    else
        _fail=$((_fail + 1))
        echo "  [FAIL] $1  期望[$3] 实际[$2]"
    fi
}

# 真实 sysfs 里 /sys/class/drm/card0 是指向 DRM 设备的目录（连接器是它的兄弟
# 目录 card0-HDMI-A-1）。假树必须同时建出 cardN 目录，否则外层循环只看得到
# 连接器目录、全被跳过，测的就不是真实分支了。
mk_drm_dir() { mkdir -p "$SYS_DRM/$1"; }                 # 只建 sysfs 卡目录
mk_card() { mk_drm_dir "$1"; : >"$DEV_DRI/$1"; }         # 卡目录 + 设备节点
mk_conn() {  # mk_conn card0 HDMI-A-1 connected
    mkdir -p "$SYS_DRM/$1-$2"
    printf '%s\n' "$3" >"$SYS_DRM/$1-$2/status"
}
reset_tree() { rm -rf "$SYS_DRM" "$DEV_DRI"; mkdir -p "$SYS_DRM" "$DEV_DRI"; }

echo "entrypoint: DRM 卡探测（多显卡 / 多接口）"

# --- 场景 1：card0 是 BMC VGA（空口），card1 的 DP 接了显示器 ---
# 典型服务器主板：写死 card0 会驱动到没人看的 BMC VGA 上
reset_tree
mk_card card0
mk_conn card0 VGA-1 disconnected
mk_card card1
mk_conn card1 HDMI-A-1 disconnected
mk_conn card1 DP-1 connected
ck "跳过没人接的 card0，选中接了显示器的 card1" "$(detect_drm_card)" "card1"

# --- 场景 2：两个卡都接了显示器，取编号小的（可预测，不随机） ---
reset_tree
mk_card card0
mk_conn card0 HDMI-A-1 connected
mk_card card1
mk_conn card1 DP-1 connected
ck "两张卡都有显示器时取 card0" "$(detect_drm_card)" "card0"

# --- 场景 3：所有连接器都没接显示器 → 取第一张卡 ---
# （开机时显示器还没插，之后热插拔由 Web 层的显示守护负责点亮）
reset_tree
mk_card card0
mk_conn card0 HDMI-A-1 disconnected
mk_card card1
mk_conn card1 DP-1 disconnected
ck "都没接显示器时回退第一张卡" "$(detect_drm_card)" "card0"

# --- 场景 4：sysfs 里没有 DRM 信息，只有 /dev/dri/card0 ---
# 受限容器 / 老内核 / 用户手动映射设备节点
reset_tree
: >"$DEV_DRI/card0"          # 只映射设备节点，sysfs 里查不到卡
ck "无 sysfs 信息时回退 card0" "$(detect_drm_card)" "card0"

# --- 场景 5：完全没有 DRM 设备 → 空串（由调用方降级到虚拟输出） ---
reset_tree
ck "没有任何 DRM 设备时返回空串" "$(detect_drm_card)" ""

# --- 场景 6：sysfs 有卡但设备节点没映射（/dev/dri 里没有）→ 跳过 ---
# 只有 --device-cgroup-rule 而漏了 --device /dev/dri 的典型症状
reset_tree
mk_drm_dir card1
mk_conn card1 DP-1 connected
ck "sysfs 有卡但设备节点缺失时不误选" "$(detect_drm_card)" ""

# --- 场景 7：连接器目录不会被当成显卡目录 ---
reset_tree
mk_card card0
mk_conn card0 HDMI-A-1 connected
ck "连接器目录 card0-HDMI-A-1 不被误识别为卡" "$(detect_drm_card)" "card0"

# --- 场景 8：USB-C(DP Alt Mode) 与物理 DP 都是 DP-*，同样被识别 ---
reset_tree
mk_card card0
mk_conn card0 DP-1 disconnected
mk_conn card0 DP-2 connected      # 例如 USB-C 转 DP 的扩展坞
ck "DP-2（USB-C/扩展坞）已连接即被选中" "$(detect_drm_card)" "card0"

# --- 场景 9：list_connectors 汇总格式 ---
reset_tree
mk_card card0
mk_conn card0 HDMI-A-1 disconnected
mk_conn card0 VGA-1 connected
_list=$(list_connectors)
case "$_list" in
    *"card0-HDMI-A-1:disconnected"*"card0-VGA-1:connected"*)
        ck "list_connectors 汇总连接器与状态" "ok" "ok" ;;
    *)
        ck "list_connectors 汇总连接器与状态" "$_list" "包含 card0-HDMI-A-1:disconnected 与 card0-VGA-1:connected" ;;
esac

echo
if [ "$_fail" -ne 0 ]; then
    echo "FAILED: $_fail 项（通过 $_pass 项）"
    exit 1
fi
echo "ALL DRM PROBE TESTS PASSED（$_pass 项）"
