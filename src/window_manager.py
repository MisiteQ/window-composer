import glob
import os
import re
import shutil
import subprocess
import time
from typing import List, Dict, Optional

import config
from desktop_env import desktop_env
from app_runner import get_app_name_by_pid, list_pid_registry


# ---------------------------------------------------------------- 输出接口类型
# 本项目要支持 NAS 上所有形态的显示输出：HDMI / DisplayPort / VGA / DVI /
# USB-C（DP Alt Mode）等。做法不是逐个接口写分支，而是"不假定任何输出名，
# 全部交给 xrandr 枚举"，再把内核/Xorg 的连接器命名翻译成用户看得懂的接口类型。
# Xorg（modesetting 驱动）的输出名直接来自内核连接器名：
#   HDMI-A-1 / HDMI-1   → HDMI
#   DP-1 / DP-2 / DP-1-1 → DisplayPort
#       USB-C 的 DP Alt Mode（含雷电/USB4 扩展坞）在内核里同样枚举成 DP-*，
#       显示栈层面无法与物理 DP 口区分，因此统一标注为「DisplayPort / USB-C」。
#   VGA-1               → VGA（含通过 BMC/IPMI 提供的板载 VGA）
#   DVI-I-1 / DVI-D-1   → DVI
#   eDP-1 / LVDS-1      → 内置屏
#   DSI-1               → MIPI DSI 排线屏
#   Virtual-1 / DUMMY*  → 虚拟输出（Xorg dummy 驱动，无物理显示器时的兜底）
_CONNECTOR_TYPES = (
    (re.compile(r"^HDMI", re.IGNORECASE), "HDMI", "HDMI"),
    (re.compile(r"^(DP|DisplayPort)", re.IGNORECASE), "DP", "DisplayPort / USB-C"),
    (re.compile(r"^VGA", re.IGNORECASE), "VGA", "VGA"),
    (re.compile(r"^DVI", re.IGNORECASE), "DVI", "DVI"),
    (re.compile(r"^(eDP|LVDS)", re.IGNORECASE), "eDP", "内置屏 (eDP/LVDS)"),
    (re.compile(r"^DSI", re.IGNORECASE), "DSI", "MIPI DSI"),
    (re.compile(r"^(Virtual|DUMMY|VIRTUAL)", re.IGNORECASE), "Virtual", "虚拟输出"),
)


def classify_output(name: str):
    """把 Xorg 输出名翻译成 (接口代号, 可读标签)。

    未知接口不丢信息：返回 ("Other", "其他接口")，前端会连同原始输出名一起展示。
    """
    for pattern, code, label in _CONNECTOR_TYPES:
        if pattern.match(name or ""):
            return code, label
    return "Other", "其他接口"


class WindowManager:
    """X11 窗口底层操作封装（Xorg + openbox + xdotool + xprop）。

    当前显示栈：Xorg（modesetting/DRM）+ openbox（X11 WM，支持 EWMH）。
    openbox 完整实现 ICCCM/EWMH，xdotool 的位置/尺寸请求精确生效，
    因此不再需要早期 Weston 时代的各种绕行方案（见 dev-doc.md 第 6 节历史记录）。
    - xdotool search --onlyvisible --class "": 列出所有可见窗口 ID
    - xwininfo -id <wid>: 窗口绝对坐标与宽高（openbox 重父化后首选）
    - xprop -id <wid> _NET_WM_PID: 获取窗口对应的进程 PID
    - xprop -id <wid> _NET_FRAME_EXTENTS: 窗口装饰尺寸（windowmove 补偿用）
    - xdotool windowmove/windowsize --sync: 精确移动/调整窗口
    - wmctrl -i -r <wid> -b add,fullscreen: EWMH 真全屏
    - xrandr --current: 枚举全部输出（HDMI/DP/USB-C/VGA/DVI/eDP…）
    - xrandr --output <name> --auto / --rotate / --fb: 启用输出、旋转、调 framebuffer
    - xdotool getdisplaygeometry / xdpyinfo: 整个 X 屏幕尺寸（回退用）
    - scrot: 截屏到 /data/screenshot.png

    多显示器说明：布局坐标系以"目标输出"（target_output）的左上角为原点，
    单屏时该原点就是 (0,0)，与单显示器行为完全一致；多屏时窗口会被摆到
    用户选定的那块屏上，而不是贴在整个 X 屏幕的左上角。

    说明：本层只与窗口系统交互，不感知应用进程生命周期。
    若更换合成器/WM，仅需替换此处命令调用，上层 API 保持不变。
    """

    # 跟随 FNWC_DATA_DIR（容器内即 /data），避免本地调试时截图路径分裂
    SCREENSHOT_PATH = config.SCREENSHOT_PATH

    # 轮询场景下的短 TTL 缓存：前端每 3 秒并发拉一次状态，单次 /api/status 内部
    # 又会多次读取输出/窗口列表，不加缓存会在窗口多时形成子进程风暴
    # （每个窗口 xwininfo + 2 次 xprop）。所有"写操作"（启用输出/旋转/移动窗口/
    # 全屏/最小化/启停应用）都会立即失效对应缓存，因此不会读到陈旧状态。
    OUTPUTS_TTL = 0.5
    WINDOWS_TTL = 0.8

    # 形如 "HDMI-2 connected primary 1536x2048+0+0 (normal left ...) 300mm x 200mm"
    # 或   "DP-1 connected (normal left inverted right x axis y axis)"
    _OUTPUT_RE = re.compile(
        r"^(?P<name>\S+)\s+(?P<state>connected|disconnected|unknown)\s*(?P<rest>.*)$"
    )
    _MODE_POS_RE = re.compile(
        r"(?P<w>\d+)x(?P<h>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
        r"(?:\s+(?P<rot>normal|left|right|inverted))?"
    )
    _MODE_LINE_RE = re.compile(r"^(\d+)x(\d+)i?\s")
    _MM_RE = re.compile(r"(\d+)mm\s+x\s+(\d+)mm")
    _ROT_WORD_RE = re.compile(r"\b(normal|left|right|inverted)\b")
    _XDIM_RE = re.compile(r"dimensions:\s+(\d+)x(\d+)")

    def __init__(self, target_output: str = ""):
        # 目标输出：窗口布局坐标系的基准屏。
        # 空串 = 自动选择（primary → 首个已连接输出）。用户可在面板切换，
        # 也可用 WC_TARGET_OUTPUT 环境变量在启动时指定（无面板时的兜底）。
        self.target_output = (target_output or
                              os.environ.get("WC_TARGET_OUTPUT") or "").strip()
        # 输出/窗口列表的 TTL 缓存槽位（见 OUTPUTS_TTL/WINDOWS_TTL 说明）
        self._outputs_cache = {"at": 0.0, "data": []}
        self._windows_cache = {"at": 0.0, "data": []}

    def invalidate_outputs(self) -> None:
        """失效输出缓存（xrandr 写操作后调用）。"""
        self._outputs_cache["at"] = 0.0

    def invalidate_windows(self) -> None:
        """失效窗口列表缓存（启停应用/窗口几何或状态变化后调用）。"""
        self._windows_cache["at"] = 0.0

    def _get_env(self) -> dict:
        """获取 X11/Wayland 客户端环境变量。

        显示号与会话 socket 统一由 desktop_env 动态探测：
        容器 restart 后 X 显示号可能从 :0 递增为 :2，
        写死 :0 会导致所有 X11 操作失败。
        xdotool/xprop/xwininfo/xrandr/scrot 用 DISPLAY。
        """
        return desktop_env()

    def list_windows(self, force: bool = False) -> List[Dict]:
        """列出顶层 X11 窗口（含已最小化的本服务应用窗口）。

        返回格式：[{pid, wid, app_name, x, y, width, height, visible}, ...]
        visible=True  当前映射在屏幕上（画布绘制/几何判断只认这些）
        visible=False 已最小化/不可见的窗口：仅用于窗口列表里提供"恢复窗口"
                    入口，不画到画布上。只回收 pid 注册表里本服务启动的应用，
                    不把浏览器/系统的内部隐藏窗口翻出来。
        结果带 WINDOWS_TTL 秒短缓存（前端 3 秒轮询会高频调用本方法）。
        """
        now = time.time()
        if not force and self._windows_cache["at"] and \
                now - self._windows_cache["at"] < self.WINDOWS_TTL:
            # 返回副本，避免调用方修改污染缓存
            return [dict(w) for w in self._windows_cache["data"]]

        wins = self._list_visible_windows()
        visible_pids = {w["pid"] for w in wins}
        # 补回"本服务启动、但当前不可见（最小化）"的窗口，
        # 否则用户最小化后界面上再也找不到恢复入口
        for reg in list_pid_registry():
            pid = reg.get("pid")
            if not reg.get("alive") or pid in visible_pids:
                continue
            w = self._hidden_window_for_pid(pid)
            if w:
                wins.append(w)

        # 按 PID 去重：chromium 等应用会创建多个子窗口，只保留面积最大的主窗口
        by_pid: Dict[int, Dict] = {}
        for w in wins:
            pid = w["pid"]
            area = w["width"] * w["height"]
            if pid not in by_pid or area > by_pid[pid]["width"] * by_pid[pid]["height"]:
                by_pid[pid] = w
        result = list(by_pid.values())
        self._windows_cache["at"] = now
        self._windows_cache["data"] = [dict(w) for w in result]
        return [dict(w) for w in result]

    def _list_visible_windows(self) -> List[Dict]:
        """枚举当前可见的顶层窗口（xdotool --onlyvisible）。"""
        # xdotool search 列出可见窗口。
        # 不用 --maxdepth 1（太严格，某些应用主窗口不在 depth 1）。
        # 后续通过尺寸过滤排除子窗口（菜单/按钮等子窗口通常宽高 < 50）。
        try:
            res = subprocess.check_output(
                ["xdotool", "search", "--onlyvisible", "--class", "."],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return []

        wins = []
        for line in res.splitlines():
            wid = line.strip()
            if not wid:
                continue
            item = self._window_entry(wid, visible=True)
            if item:
                wins.append(item)
        return wins

    def _hidden_window_for_pid(self, pid: int) -> Optional[Dict]:
        """为已最小化的注册表应用找回窗口条目（不可见标记）。"""
        try:
            wid = self._find_window_id_by_pid(pid)
        except Exception:
            return None
        if not wid or str(wid) == str(pid):
            # _find_window_id_by_pid 找不到时会把 pid 本身当 wid，
            # 那种情况说明该 pid 已无任何 X 窗口
            return None
        item = self._window_entry(wid, visible=False)
        if item:
            item["pid"] = pid
        return item

    def _window_entry(self, wid: str, visible: bool) -> Optional[Dict]:
        """把窗口 ID 解析成标准条目；通不过子窗口/尺寸过滤时返回 None。"""
        geometry = self._get_window_geometry(wid)
        if geometry is None:
            return None
        # 过滤子窗口：xedit 等应用会创建菜单/按钮/文本框等子窗口。
        # 顶层窗口没有 WM_TRANSIENT_FOR 属性；子窗口/对话框有。
        if self._is_transient_window(wid):
            return None
        # 二次过滤：极小窗口（< 50px）通常是装饰性子窗口
        if geometry["width"] < 50 or geometry["height"] < 50:
            return None
        pid = self._get_window_pid(wid)
        # 部分古老 X11 应用（xlogo/xeyes/xedit/xcalc）不设置 _NET_WM_PID，
        # 此时用窗口 ID 作为临时 pid，让上层 API 仍能操作该窗口。
        # set_window_rect/set_fullscreen 内部会先按 pid 查找，失败则把 pid 当 wid 用。
        if pid is None:
            try:
                pid = int(wid)
            except ValueError:
                return None
        # 读一次 _NET_WM_STATE 同时判断全屏与置顶（避免每个窗口发两次 xprop）
        wm_state = self._get_wm_state(wid)
        return {
            "pid": pid,
            "wid": wid,
            "app_name": get_app_name_by_pid(pid) or "",
            "x": geometry["x"],
            "y": geometry["y"],
            "width": geometry["width"],
            "height": geometry["height"],
            "visible": visible,
            "fullscreen": self._state_has_fullscreen(wm_state),
            "above": self._state_has_above(wm_state),
        }

    def _get_window_geometry(self, wid: str) -> Optional[Dict]:
        """获取窗口几何（屏幕绝对坐标 + 客户区宽高）。

        优先用 xwininfo 的 Absolute upper-left X/Y：xdotool getwindowgeometry
        在 openbox 重父化窗口后报的是相对 frame 的坐标（且数值异常），
        会让位置监控/吸附判断全部偏移。xwininfo 给的是根窗口绝对坐标。
        """
        env = self._get_env()
        try:
            res = subprocess.check_output(
                ["xwininfo", "-id", str(wid)],
                encoding="utf-8", timeout=3,
                env=env,
                stderr=subprocess.DEVNULL,
            )
            vals = {}
            for line in res.splitlines():
                m = re.match(r"\s*(Absolute upper-left [XY]|Width|Height):\s*(-?\d+)", line)
                if m:
                    vals[m.group(1)] = int(m.group(2))
            if "Absolute upper-left X" in vals:
                return {
                    "x": vals["Absolute upper-left X"],
                    "y": vals["Absolute upper-left Y"],
                    "width": vals.get("Width", 0),
                    "height": vals.get("Height", 0),
                }
        except Exception:
            pass
        # 兜底：xdotool（坐标相对父窗口，仅在无 WM/未重父化时可靠）
        try:
            res = subprocess.check_output(
                ["xdotool", "getwindowgeometry", "--shell", str(wid)],
                encoding="utf-8", timeout=3,
                env=env,
                stderr=subprocess.DEVNULL,
            )
            vals = {}
            for line in res.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            return {
                "x": int(vals.get("X", 0)),
                "y": int(vals.get("Y", 0)),
                "width": int(vals.get("WIDTH", 0)),
                "height": int(vals.get("HEIGHT", 0)),
            }
        except Exception:
            return None

    def _is_transient_window(self, wid: str) -> bool:
        """判断窗口是否为子窗口/对话框（有 WM_TRANSIENT_FOR 属性）。

        顶层应用窗口没有 WM_TRANSIENT_FOR；子窗口/对话框有。
        用于过滤 xedit 等应用的菜单/按钮/文本框子窗口。
        """
        try:
            res = subprocess.check_output(
                ["xprop", "-id", wid, "WM_TRANSIENT_FOR"],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
            # 有 WM_TRANSIENT_FOR 属性且值不是 "not found" 就是子窗口
            return "not found" not in res and "WM_TRANSIENT_FOR" in res
        except Exception:
            return False

    def get_window_class(self, wid: str) -> str:
        """获取窗口的 WM_CLASS 类名（如 "xedit", "vlc"）。

        用于 layout save 时，对没有 _NET_WM_PID 的窗口用类名作为 app_name。
        返回 "unknown" 如果获取失败。
        """
        try:
            res = subprocess.check_output(
                ["xprop", "-id", wid, "WM_CLASS"],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
            # 格式: WM_CLASS(STRING) = "xedit", "Xedit"
            match = re.search(r'"([^"]+)"', res)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "unknown"

    def _get_window_pid(self, wid: str) -> Optional[int]:
        """通过 xprop 获取窗口的 _NET_WM_PID。"""
        try:
            res = subprocess.check_output(
                ["xprop", "-id", wid, "_NET_WM_PID"],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
            # 格式: _NET_WM_PID(CARDINAL) = 12345
            match = re.search(r"=\s*(\d+)", res)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    def _find_window_id_by_pid(self, pid: int) -> Optional[str]:
        """用 xdotool search --pid 查找窗口；失败则把 pid 当 wid 用。

        chromium 等应用会创建多个子窗口，返回面积最大的主窗口。
        部分 X11 应用不设置 _NET_WM_PID，list_windows 会用窗口 ID 作为临时 pid。
        此时 xdotool search --pid 找不到，直接把传入的 pid 当作窗口 ID 返回。
        """
        env = self._get_env()
        try:
            res = subprocess.check_output(
                ["xdotool", "search", "--pid", str(pid)],
                encoding="utf-8", timeout=3,
                env=env,
                stderr=subprocess.DEVNULL,
            )
            wids = [line.strip() for line in res.splitlines() if line.strip()]
            if not wids:
                return str(pid)
            if len(wids) == 1:
                return wids[0]
            # 多个窗口：返回面积最大的主窗口
            best_wid = wids[0]
            best_area = -1
            for wid in wids:
                geo = self._get_window_geometry(wid)
                if geo:
                    area = geo["width"] * geo["height"]
                    if area > best_area:
                        best_area = area
                        best_wid = wid
            return best_wid
        except Exception:
            pass
        # 回退：把 pid 当作窗口 ID（list_windows 对无 _NET_WM_PID 的窗口这么做）
        return str(pid)

    def get_outputs(self, force: bool = False) -> List[Dict]:
        """解析 `xrandr --current`，返回全部输出（含未连接/未启用的）。

        这是"支持所有显示接口"的地基：HDMI / DisplayPort（含 USB-C 的
        DP Alt Mode 与雷电扩展坞，内核里同样是 DP-*）/ VGA / DVI / eDP
        全部由同一套解析逻辑覆盖，代码里不写死任何输出名。

        每项字段：
            name        X 输出名（HDMI-1 / DP-2 / VGA-1 / DVI-I-1 …）
            connected   是否插着显示器
            primary     是否为 X 的 primary 输出
            enabled     是否已激活（有像素时钟）；connected 但未启用 = False
            width/height/x/y  当前模式与在 X 屏幕中的位置（未启用时为 0）
            rotation    0/90/180/270
            connector   接口代号（HDMI/DP/VGA/DVI/eDP/DSI/Virtual/Other）
            type_label  接口可读名（"DisplayPort / USB-C" 等）
            mm_width/mm_height  显示器物理尺寸（EDID，0 = 未知）
            modes       可用模式去重列表（xrandr 顺序，首个通常是首选模式）
            preferred   首选模式 "1920x1080"（--auto 失败时手动 --mode 用）

        结果带 OUTPUTS_TTL 秒短缓存：/api/status 一次请求内部会多次读取输出
        （几何/后端/旋转），显示守护也周期性读取；任何 xrandr 写操作都会
        主动 invalidate，force=True 可强制重探。
        """
        now = time.time()
        if not force and self._outputs_cache["at"] and \
                now - self._outputs_cache["at"] < self.OUTPUTS_TTL:
            return [dict(o) for o in self._outputs_cache["data"]]
        outputs: List[Dict] = []
        try:
            out = subprocess.check_output(
                ["xrandr", "--current"], encoding="utf-8", timeout=3,
                env=self._get_env(), stderr=subprocess.DEVNULL,
            )
        except Exception:
            return outputs

        cur = None
        for line in out.splitlines():
            m = self._OUTPUT_RE.match(line)
            if m:
                name = m.group("name")
                rest = m.group("rest")
                code, label = classify_output(name)
                cur = {
                    "name": name,
                    "connected": m.group("state") == "connected",
                    "primary": rest.startswith("primary"),
                    "enabled": False,
                    "width": 0, "height": 0, "x": 0, "y": 0,
                    "rotation": 0,
                    "connector": code, "type_label": label,
                    "mm_width": 0, "mm_height": 0,
                    "modes": [], "preferred": "",
                }
                mp = self._MODE_POS_RE.search(rest)
                if mp:
                    # 有 w×h+x+y 才说明这个输出真的被激活了
                    cur.update(
                        enabled=True,
                        width=int(mp.group("w")), height=int(mp.group("h")),
                        x=int(mp.group("x")), y=int(mp.group("y")),
                    )
                    cur["rotation"] = self._ROT_INV.get(
                        mp.group("rot") or "normal", 0)
                else:
                    rw = self._ROT_WORD_RE.search(rest)
                    if rw:
                        cur["rotation"] = self._ROT_INV.get(rw.group(1), 0)
                mm = self._MM_RE.search(rest)
                if mm:
                    cur["mm_width"] = int(mm.group(1))
                    cur["mm_height"] = int(mm.group(2))
                outputs.append(cur)
                continue

            if cur is None:
                continue
            stripped = line.strip()
            if not stripped:
                continue
            # 模式行形如 "   1920x1080     60.00*+  50.00  30.00"
            mline = self._MODE_LINE_RE.match(stripped)
            if mline:
                mode = f"{mline.group(1)}x{mline.group(2)}"
                if mode not in cur["modes"]:
                    cur["modes"].append(mode)
                if not cur["preferred"]:
                    cur["preferred"] = mode
        self._outputs_cache["at"] = now
        self._outputs_cache["data"] = [dict(o) for o in outputs]
        return outputs

    def resolve_output(self, outputs: Optional[List[Dict]] = None,
                       name: str = "") -> Optional[Dict]:
        """选出用于布局的"目标输出"。

        优先级：显式 name → self.target_output → primary → 首个已连接输出。
        disconnected 的输出一律不选（会把窗口摆到不存在的屏上）。
        一个都没连接时返回 None，由调用方走 headless 回退。
        """
        outs = outputs if outputs is not None else self.get_outputs()
        connected = [o for o in outs if o.get("connected")]
        if not connected:
            return None
        for want in (name, self.target_output):
            if not want:
                continue
            for o in connected:
                if o["name"] == want:
                    return o
        for o in connected:
            if o.get("primary"):
                return o
        return connected[0]

    def set_target_output(self, name: str) -> bool:
        """切换目标输出（面板选择显示器 / 环境变量兜底）。

        不在这里调 xrandr，只登记选择；真正激活交给 display_monitor
        与 /api/display/set_output 流程，避免"选了一个没插屏的口"时静默失败。
        """
        self.target_output = (name or "").strip()
        return True

    def get_screen_geometry(self) -> Dict:
        """整个 X 屏幕（根窗口）的尺寸：所有已启用输出的外接矩形。"""
        try:
            res = subprocess.check_output(
                ["xdotool", "getdisplaygeometry"],
                encoding="utf-8", timeout=3,
                env=self._get_env(), stderr=subprocess.DEVNULL,
            )
            parts = res.strip().split()
            if len(parts) >= 2:
                return {"width": int(parts[0]), "height": int(parts[1])}
        except Exception:
            pass
        # xdotool 缺失时的兜底（x11-utils 的 xdpyinfo）
        try:
            out = subprocess.check_output(
                ["xdpyinfo"], encoding="utf-8", timeout=3,
                env=self._get_env(), stderr=subprocess.DEVNULL,
            )
            m = self._XDIM_RE.search(out)
            if m:
                return {"width": int(m.group(1)), "height": int(m.group(2))}
        except Exception:
            pass
        return {"width": 1920, "height": 1080}

    def get_display_geometry(self) -> Dict:
        """目标输出的几何：宽高 + 它在 X 屏幕中的原点 (x, y)。

        多带 x/y 是"通用多显示器"的关键：布局坐标系以目标输出左上角为原点，
        单屏时 (0,0) 与旧行为完全一致；双屏时窗口会被摆到用户选定的那块屏，
        而不是默认贴在整个 X 屏幕的左上角。
        目标输出尚未激活时退而使用任意已激活输出；一个都没有则回退整屏尺寸。
        """
        outs = self.get_outputs()
        o = self.resolve_output(outs)
        if not (o and o.get("enabled") and o["width"] > 0 and o["height"] > 0):
            for cand in outs:
                if (cand.get("connected") and cand.get("enabled")
                        and cand["width"] > 0 and cand["height"] > 0):
                    o = cand
                    break
        if o and o.get("enabled") and o["width"] > 0 and o["height"] > 0:
            return {"width": o["width"], "height": o["height"],
                    "x": o["x"], "y": o["y"], "output": o["name"]}
        g = self.get_screen_geometry()
        return {"width": g["width"], "height": g["height"],
                "x": 0, "y": 0, "output": ""}

    def get_visible_area(self) -> Dict:
        """所有已启用输出的外接矩形——窗口真正可见的范围。

        X 屏幕可能比"可见范围"大（例如输出只占 (1920,0)-(3840,1080)，
        左侧 0~1920 没有任何输出）。旋转/改分辨率后把窗口拉回这个范围，
        才不会出现"窗口坐标合法、但屏幕上看不见也点不到"的情况。
        """
        outs = [o for o in self.get_outputs()
                if o.get("connected") and o.get("enabled")
                and o["width"] > 0 and o["height"] > 0]
        if not outs:
            g = self.get_screen_geometry()
            return {"x": 0, "y": 0, "width": g["width"], "height": g["height"]}
        x0 = min(o["x"] for o in outs)
        y0 = min(o["y"] for o in outs)
        x1 = max(o["x"] + o["width"] for o in outs)
        y1 = max(o["y"] + o["height"] for o in outs)
        return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}

    def _grow_framebuffer(self, need_w: int, need_h: int) -> bool:
        """确保 X framebuffer 至少能容纳 need_w × need_h（不够才调 xrandr --fb）。"""
        cur = self.get_screen_geometry()
        cur_w = int(cur.get("width") or 0)
        cur_h = int(cur.get("height") or 0)
        need_w = max(int(need_w or 0), cur_w)
        need_h = max(int(need_h or 0), cur_h)
        if need_w <= 0 or need_h <= 0 or (cur_w >= need_w and cur_h >= need_h):
            return True
        try:
            res = subprocess.run(
                ["xrandr", "--fb", f"{need_w}x{need_h}"],
                timeout=5, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=self._get_env(),
            )
            return res.returncode == 0
        except Exception:
            return False

    def enable_output(self, name: str) -> Dict:
        """启用一个"已连接但未激活"的输出（xrandr --output NAME --auto）。

        场景：NAS 开机时显示器还没插、或用户换了个接口（HDMI → DP/USB-C）
        热插拔，X server 起来了但那个输出没有像素时钟 → 屏幕全黑。
        X 不会自动给新插上的输出分配模式，必须显式 --auto。

        常见失败：framebuffer 装不下新模式，xrandr 报
        "screen cannot be larger than ..."，此时先放大 --fb 再重试。
        返回 {ok, name, width, height, mode, msg}。
        """
        outs = self.get_outputs()
        target = next((o for o in outs if o["name"] == name), None)
        if target is None:
            return {"ok": False, "name": name, "msg": "输出不存在"}
        if not target.get("connected"):
            return {"ok": False, "name": name, "msg": "该接口未接显示器"}
        if target.get("enabled"):
            return {"ok": True, "name": name, "mode": "",
                    "width": target["width"], "height": target["height"],
                    "msg": "已处于激活状态"}

        want = target.get("preferred") or ""
        if "x" in want:
            try:
                pw, ph = (int(v) for v in want.split("x", 1))
                self._grow_framebuffer(pw, ph)
            except ValueError:
                pass

        ok = False
        try:
            ok = subprocess.run(
                ["xrandr", "--output", name, "--auto"],
                timeout=8, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=self._get_env(),
            ).returncode == 0
            if not ok and want:
                ok = subprocess.run(
                    ["xrandr", "--output", name, "--mode", want],
                    timeout=8, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=self._get_env(),
                ).returncode == 0
        except Exception:
            ok = False
        if not ok:
            return {"ok": False, "name": name, "msg": "xrandr 启用输出失败"}
        self.invalidate_outputs()
        after = next((o for o in self.get_outputs() if o["name"] == name), {})
        return {"ok": True, "name": name, "mode": want,
                "width": after.get("width", 0), "height": after.get("height", 0),
                "msg": "已启用"}

    def auto_enable_connected(self) -> List[str]:
        """启用所有"已连接但未激活"的输出，返回被成功启用的输出名。

        这是"通用接口 + 热插拔"的核心兜底：无论用户插的是 HDMI、DP、
        USB-C 还是 VGA，只要内核认出了连接器，这里就能把它点亮。
        """
        enabled = []
        for o in self.get_outputs():
            if o.get("connected") and not o.get("enabled"):
                if self.enable_output(o["name"]).get("ok"):
                    enabled.append(o["name"])
        return enabled

    def get_output_info(self) -> Dict:
        """探测全部显示输出与当前生效的目标输出（不依赖环境变量声明）。

        历史问题：/api/status 的 backend 取自 WESTON_BACKEND 环境变量，
        而 entrypoint.sh 从不设置它 → 容器明明在驱动真实显示器，
        控制面板却永远显示"Headless 虚拟"。现在按 xrandr 实际输出判定。

        返回:
            backend      "drm"（有已连接输出）/ "virtual"（Xorg 跑在 dummy 虚拟
                         输出上，通常是容器没映射 /dev/dri）/ "headless"（探测不到）
            output       生效的目标输出名
            output_type  接口代号（HDMI/DP/VGA/DVI/eDP/DSI/Virtual/Other）
            output_label 接口可读名（"DisplayPort / USB-C" 等）
            width/height 目标输出当前分辨率
            outputs      全部输出明细（含未连接的，面板可展示"本机有哪些口"）
            card         实际使用的 DRM 设备名（entrypoint 探测，便于排障）
        调试时可用 WC_FORCE_BACKEND=drm|headless 强制覆盖。
        """
        info = {
            "backend": "headless", "output": "", "output_type": "",
            "output_label": "", "width": 0, "height": 0,
            "outputs": [], "card": (os.environ.get("WC_DRM_CARD") or "").strip(),
        }
        outs = self.get_outputs()
        info["outputs"] = outs
        o = self.resolve_output(outs)
        if o is not None:
            info["backend"] = "drm"
            info["output"] = o["name"]
            info["output_type"] = o["connector"]
            info["output_label"] = o["type_label"]
            info["width"] = o["width"]
            info["height"] = o["height"]
        elif (os.environ.get("WC_XORG_MODE") or "").strip().lower() == "virtual":
            # entrypoint 在无 /dev/dri 时会用 dummy 驱动起一个虚拟输出。
            # 此时 xrandr 里没有任何 output（dummy 不是 KMS 驱动），
            # 但屏幕是真的存在且能渲染/截图，与"探测失败"要区分开，
            # 否则面板会显示"Headless"，用户以为程序坏了。
            info["backend"] = "virtual"
            info["output_label"] = "虚拟输出（容器未映射 /dev/dri）"
        forced = (os.environ.get("WC_FORCE_BACKEND") or "").strip().lower()
        if forced in ("drm", "headless"):
            info["backend"] = forced
        return info

    # ---------- 屏幕方向旋转 ----------
    _ROT_MAP = {0: "normal", 90: "left", 180: "inverted", 270: "right"}
    _ROT_INV = {"normal": 0, "left": 90, "inverted": 180, "right": 270}

    def _get_connected_output(self) -> str:
        """返回目标输出名；没有任何已连接输出时返回空串。

        早期版本失败时回退硬编码 "HDMI-2"：在只有 VGA/DP 接口的机器上，
        所有 xrandr 调用都会打到一个不存在的输出上，旋转/回屏静默失效。
        现在一律以实际探测结果为准，空串表示"没有可用输出"，
        调用方据此直接失败而不是把一个假输出名传给 xrandr。
        """
        o = self.resolve_output()
        return o["name"] if o else ""

    def get_rotation(self) -> int:
        """读取目标输出的旋转角度（0/90/180/270）。

        直接取 get_outputs() 解析结果：xrandr 在已启用输出的几何后面
        带旋转词（如 'DP-1 connected 2048x1536+0+0 left (normal ...)'），
        未启用/未连接时视为 0。目标输出不存在时回退 0。
        """
        o = self.resolve_output()
        return int(o.get("rotation") or 0) if o else 0

    def set_rotation(self, degrees: int) -> bool:
        """旋转目标输出（0/90/180/270）。

        旋转后宽高互换，get_display_geometry 会自动返回新的宽高，
        上层 /api/status 的 display_width/height 随之更新，前端画布比例自动适配。
        没有任何已连接输出时返回 False（不再对假输出名发命令）。
        """
        rot = self._ROT_MAP.get(int(degrees))
        if not rot:
            return False
        output = self._get_connected_output()
        if not output:
            return False
        try:
            subprocess.run(
                ["xrandr", "--output", output, "--rotate", rot],
                env=self._get_env(), timeout=8,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True,
            )
            self.invalidate_outputs()
            return True
        except Exception:
            return False

    @staticmethod
    def _state_has_fullscreen(prop_text: str) -> bool:
        """解析 xprop _NET_WM_STATE 输出中是否含全屏状态原子。"""
        return "_NET_WM_STATE_FULLSCREEN" in (prop_text or "")

    @staticmethod
    def _state_has_above(prop_text: str) -> bool:
        """解析 xprop _NET_WM_STATE 输出中是否含置顶（above）状态原子。"""
        return "_NET_WM_STATE_ABOVE" in (prop_text or "")

    def _get_wm_state(self, wid: str) -> str:
        """读取窗口 _NET_WM_STATE 属性原文（读不到返回空串）。"""
        try:
            return subprocess.check_output(
                ["xprop", "-id", str(wid), "_NET_WM_STATE"],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return ""

    def is_fullscreen(self, pid: int) -> bool:
        """判断窗口当前是否处于 EWMH 全屏状态。"""
        wid = self._find_window_id_by_pid(pid)
        if not wid:
            return False
        return self._state_has_fullscreen(self._get_wm_state(wid))

    def set_fullscreen(self, pid: int, fullscreen: bool = True) -> bool:
        """设置窗口全屏（真全屏：窗口移到 (0,0) 并铺满整个输出）。

        关键实现：通过 wmctrl 发送 EWMH _NET_WM_STATE ClientMessage 给根窗口。
        注意不能用 `xprop -set _NET_WM_STATE`——那只是 ChangeProperty 改属性，
        不会通知窗口管理器；WM 只在收到 ClientMessage 事件后才执行全屏
        （移动到输出原点 + 铺满 + 置顶）。这一结论在 Weston 与 openbox 下均成立。

        - fullscreen=True:  -b add,fullscreen
        - fullscreen=False: -b remove,fullscreen；退出后恢复原尺寸位置
        """
        wid = self._find_window_id_by_pid(pid)
        if wid is None:
            return False
        action = "add" if fullscreen else "remove"
        try:
            res = subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", f"{action},fullscreen"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._get_env(),
            )
            ok = res.returncode == 0
            if ok:
                self.invalidate_windows()
            return ok
        except Exception:
            return False

    def set_minimized(self, pid: int, minimized: bool = True) -> bool:
        """最小化 / 恢复窗口。

        最小化走 xdotool windowminimize（ICCCM ChangeWMState iconic）；
        恢复走 windowactivate + windowraise：仅 map 回来还不够，不 raise
        的话窗口可能被其他窗口挡住，用户以为"恢复没反应"。
        最小化后窗口不再出现在 --onlyvisible 列表里，list_windows 会通过
        pid 注册表把它以 visible=False 补回窗口列表，供面板提供恢复入口。
        """
        wid = self._find_window_id_by_pid(pid)
        if wid is None:
            return False
        env = self._get_env()
        try:
            if minimized:
                cmd = ["xdotool", "windowminimize", wid]
            else:
                cmd = ["xdotool", "windowactivate", "--sync", wid]
            ok = subprocess.run(
                cmd, timeout=3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            ).returncode == 0
            if ok and not minimized:
                subprocess.run(
                    ["xdotool", "windowraise", wid],
                    timeout=3,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
            if ok:
                self.invalidate_windows()
            return ok
        except Exception:
            return False

    def set_above(self, pid: int, above: bool = True) -> bool:
        """窗口置顶 / 取消置顶（EWMH _NET_WM_STATE_ABOVE）。

        与全屏同理，状态变更必须通过 wmctrl 发 ClientMessage，
        直接改 _NET_WM_STATE 属性 openbox 不会响应。
        """
        wid = self._find_window_id_by_pid(pid)
        if wid is None:
            return False
        action = "add" if above else "remove"
        try:
            ok = subprocess.run(
                ["wmctrl", "-i", "-r", wid, "-b", f"{action},above"],
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._get_env(),
            ).returncode == 0
            if ok:
                self.invalidate_windows()
            return ok
        except Exception:
            return False

    def is_above(self, pid: int) -> bool:
        """判断窗口当前是否置顶。"""
        wid = self._find_window_id_by_pid(pid)
        return bool(wid) and self._state_has_above(self._get_wm_state(wid))

    def _get_frame_extents(self, wid: str):
        """读取 openbox 窗口装饰尺寸 (left, right, top, bottom)。

        xdotool windowmove 定位的是 frame 左上角，而用户画的区域对应
        客户区，需要用边框/标题栏宽度做反向补偿，客户区才能精确落位。
        无装饰（无边框窗口）时全为 0。
        """
        try:
            res = subprocess.check_output(
                ["xprop", "-id", str(wid), "_NET_FRAME_EXTENTS"],
                encoding="utf-8", timeout=3,
                env=self._get_env(),
                stderr=subprocess.DEVNULL,
            )
            m = re.search(r"=\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", res)
            if m:
                return tuple(int(m.group(i)) for i in range(1, 5))
        except Exception:
            pass
        return (0, 0, 0, 0)

    def set_window_rect(self, pid: int, x: int, y: int, w: int, h: int) -> bool:
        """通过 PID 找到 window_id，设置窗口位置和大小（客户区精确匹配）。

        在 Xorg + openbox 环境下，xdotool windowmove 和 windowsize 均生效。
        windowmove 作用于 frame，故按 _NET_FRAME_EXTENTS 反向补偿装饰边框；
        windowsize 设置的是客户区尺寸，无需补偿。

        全屏窗口的几何请求会被 openbox 忽略（全屏状态由 WM 全权托管位置尺寸），
        因此先退出全屏、稍等 WM 还原窗口后再移动缩放。否则用户在画布上拖动
        一个全屏窗口时接口返回成功、画面却纹丝不动。
        """
        wid = self._find_window_id_by_pid(pid)
        if wid is None:
            return False
        env = self._get_env()
        try:
            if self._state_has_fullscreen(self._get_wm_state(wid)):
                subprocess.run(
                    ["wmctrl", "-i", "-r", wid, "-b", "remove,fullscreen"],
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                # openbox 退出全屏是异步还原几何，立刻发 windowmove 会被吞掉
                time.sleep(0.15)
                wid = self._find_window_id_by_pid(pid) or wid
            left, _right, top, _bottom = self._get_frame_extents(wid)
            subprocess.run(
                ["xdotool", "windowmove", "--sync", wid,
                 str(int(x) - left), str(int(y) - top)],
                timeout=3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            subprocess.run(
                ["xdotool", "windowsize", "--sync", wid, str(w), str(h)],
                timeout=3,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            self.invalidate_windows()
            return True
        except Exception:
            return False

    @staticmethod
    def clamp_rect(x: int, y: int, w: int, h: int,
                   screen_w: int, screen_h: int, min_px: int = 0):
        """把窗口矩形钳进屏幕内，返回 (x, y, w, h)。

        两处调用方共享同一套数学：
        - clamp_windows_to_screen：屏幕旋转/改分辨率后把越界窗口拉回来
        - main.apply_layout：布局里的几何可能来自旋转前的旧屏幕，应用前先钳制

        参数:
            min_px: 尺寸下限（布局恢复用 MIN_WINDOW_PX，避免把布局里的
                    异常小尺寸当成合法值）；旋转回屏时传 0，
                    否则会把用户故意缩小的小窗口强行放大。
            宽高传 0/None 时按屏幕尺寸处理（布局缺字段时的兜底）。
        """
        sw, sh = int(screen_w), int(screen_h)
        if sw <= 0 or sh <= 0:
            return int(x or 0), int(y or 0), int(w or 0), int(h or 0)
        nw = sw if not w else max(int(min_px), min(int(w), sw))
        nh = sh if not h else max(int(min_px), min(int(h), sh))
        nx = max(0, min(int(x or 0), sw - nw))
        ny = max(0, min(int(y or 0), sh - nh))
        return nx, ny, nw, nh

    def clamp_windows_to_screen(self) -> int:
        """把所有越界窗口拉回屏幕可见区域，返回被修正的窗口数。

        使用场景：屏幕旋转（90°/270° 宽高互换）、改分辨率、切换目标显示器、
        热插拔后，原先贴着旧边界的窗口会落到可见范围之外——用户在显示器上
        完全看不到，也无法用鼠标操作，只能重启应用。本函数把这类窗口钳回可见区。

        边界取"所有已启用输出的外接矩形"（get_visible_area），而不是整个
        X 屏幕：多显示器下 X 屏幕可能包含没有任何输出的空白区域，窗口落进去
        同样是看不见的。单屏时两者等价。

        规则：
        - 尺寸不超过可见区（超出则缩到可见区大小；全屏窗口本就是屏幕尺寸，不受影响）
        - 位置钳制到 [可见区原点, 可见区原点+尺寸-窗口尺寸]
        仅对确实越界的窗口调用 set_window_rect，避免无意义地扰动其他窗口。
        """
        area = self.get_visible_area()
        ax = int(area.get("x") or 0)
        ay = int(area.get("y") or 0)
        sw = int(area.get("width") or 0)
        sh = int(area.get("height") or 0)
        if sw <= 0 or sh <= 0:
            return 0
        fixed = 0
        for win in self.list_windows():
            # 先在"可见区局部坐标"里钳制，再平移回屏幕绝对坐标，
            # 这样 clamp_rect 的数学与布局恢复完全复用同一套
            nx, ny, nw, nh = self.clamp_rect(
                win["x"] - ax, win["y"] - ay,
                win["width"], win["height"], sw, sh,
            )
            nx += ax
            ny += ay
            if (nx, ny, nw, nh) == (win["x"], win["y"],
                                    win["width"], win["height"]):
                continue
            if self.set_window_rect(win["pid"], nx, ny, nw, nh):
                fixed += 1
        return fixed

    def take_screenshot(self) -> bool:
        """截取 HDMI 画面（Xorg 环境下用 scrot）。

        先删除上一次的截图再执行：scrot 失败（未安装 / Xorg 未运行 / 无输出）
        时若旧文件仍在，`os.path.exists` 会把过期画面当成本次成功，
        用户在弹窗里看到的是几分钟前的画面却以为截图成功。
        因此改为"先删旧图 + 校验 scrot 返回码"，只有本次真的写出文件才算成功。
        """
        target_dir = os.path.dirname(self.SCREENSHOT_PATH) or "/data"
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            pass
        try:
            os.remove(self.SCREENSHOT_PATH)
        except OSError:
            pass
        try:
            res = subprocess.run(
                ["scrot", self.SCREENSHOT_PATH],
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._get_env(),
            )
        except Exception:
            return False
        return res.returncode == 0 and os.path.exists(self.SCREENSHOT_PATH)

    # 定时截图最多保留多少张；/data 是持久卷，不能无限堆积 PNG
    TIMED_SHOT_LIMIT = 200

    def take_timed_screenshot(self) -> Optional[str]:
        """定时截图：按时间戳存到 /data/screenshots/，超过上限删最旧的。

        返回新文件路径；失败返回 None。与手动截图（固定 screenshot.png
        供弹窗预览）分开，避免定时任务把用户还没看的手动截图覆盖掉。
        """
        shot_dir = getattr(config, "SCREENSHOT_DIR", "/data/screenshots")
        try:
            os.makedirs(shot_dir, exist_ok=True)
        except Exception:
            return None
        path = os.path.join(
            shot_dir, "shot-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            res = subprocess.run(
                ["scrot", path],
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._get_env(),
            )
        except Exception:
            return None
        if res.returncode != 0 or not os.path.exists(path):
            return None
        self._prune_timed_shots(shot_dir)
        return path

    def _prune_timed_shots(self, shot_dir: str) -> None:
        """只保留最新 TIMED_SHOT_LIMIT 张定时截图。"""
        try:
            files = [os.path.join(shot_dir, f) for f in os.listdir(shot_dir)
                     if f.startswith("shot-") and f.endswith(".png")]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in files[self.TIMED_SHOT_LIMIT:]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except Exception:
            pass


wm = WindowManager()
