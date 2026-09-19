"""离线单元测试（不依赖 Xorg / pactl / 浏览器，容器内外均可运行）。

  python3 tests/test_offline.py

覆盖的是"纯逻辑"部分——最容易写错、又最不需要真实图形环境的部分：
1. audio_manager 对 `pactl list sink-inputs` / `list sinks` 输出的解析
2. audio_manager 按 pid 路由（move-sink-input）与批量迁移
3. config 的 settings 字段向后兼容、layout/pids 读写与 int key 还原、应用日志写入
4. window_manager 输出后端探测（xrandr）、通用多输出解析（HDMI/DP/USB-C/VGA/
   DVI/eDP 分类、目标输出选择、可见区外接矩形）、截图返回码判定、窗口矩形钳制
5. window_manager.clamp_windows_to_screen（旋转/改分辨率/切屏后的越界回屏，
   含多显示器原点平移）
6. app_scanner 的 Docker 容器扫描（Web 端口挑选、镜像 tag 解析、与飞牛应用去重、
   自身容器排除、socket 未挂载时的优雅降级）

涉及窗口的操作（xdotool/xwininfo/xprop/wmctrl/scrot）需要真实 X server，
测试里一律打桩；真实环境验证见 dev-doc.md 第 7 节。
"""
import os
import re
import sys
import json
import types
import asyncio
import threading
import tempfile
import http.client
import http.server

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

# config 的 DATA_DIR 在 import 时确定，必须在导入前指向临时目录
_TMP_DATA = tempfile.mkdtemp(prefix="wc-test-data-")
os.environ["FNWC_DATA_DIR"] = _TMP_DATA
# desktop_env._runtime_dir 会调 os.geteuid()（Windows 无此函数），
# 显式给出 XDG_RUNTIME_DIR 让探测逻辑短路，测试才能在 Windows 上跑
os.environ.setdefault("XDG_RUNTIME_DIR", _TMP_DATA)

import audio_manager as am_mod  # noqa: E402
import config  # noqa: E402

# window_manager → app_runner → psutil；测试只覆盖不触达进程生命周期的部分
# （所有外部命令均已打桩），本地没有 psutil 时注入空桩模块即可导入
try:
    import psutil  # noqa: F401
except ImportError:
    sys.modules["psutil"] = types.ModuleType("psutil")

import window_manager as wm_mod  # noqa: E402
import app_runner as ar_mod  # noqa: E402
import app_scanner as sc_mod  # noqa: E402
import access_control as ac_mod  # noqa: E402

# main.py 在导入时以相对路径挂载 static/templates（容器内工作目录就是 src），
# 测试从项目根运行，这里临时切目录仅用于导入，之后立即还原
_old_cwd = os.getcwd()
try:
    os.chdir(SRC)
    import main as main_mod  # noqa: E402
finally:
    os.chdir(_old_cwd)

# 无真实 psutil 时（环境里注入了空桩），进程身份类用例直接跳过
_HAS_PSUTIL = hasattr(ar_mod.psutil, "Process")

_failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name} {detail}")
        _failures.append(name)


# ---------------------------------------------------------------- 桩数据
SINK_INPUTS_OUTPUT = """Sink Input #12
\tDriver: PipeWire
\tOwner Module: n/a
\tClient: 45
\tSink: 0
\tSample Specification: float32le 2ch 44100Hz
\tChannel Map: front-left,front-right
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB
\tProperties:
\t\tapplication.name = "VLC media player"
\t\tapplication.process.id = "1234"
\t\tmedia.name = "movie.mp4"

Sink Input #13
\tDriver: PipeWire
\tSink: 1
\tProperties:
\t\tapplication.name = "Chromium"
\t\tapplication.process.id = "5678"
"""

SINKS_OUTPUT = """Sink #0
\tState: RUNNING
\tName: alsa_out_a
\tDescription: Internal Audio
\tMute: no
\tVolume: front-left: 45875 /  70% / -9.00 dB,
\t        front-right: 45875 /  70% / -9.00 dB

Sink #1
\tState: SUSPENDED
\tName: alsa_out_b
\tDescription: HDMI Output
\tMute: yes
\tVolume: front-left: 22937 /  35% / -27.00 dB,
\t        front-right: 22937 /  35% / -27.00 dB
"""


def test_sink_inputs_parse():
    print("audio_manager: 解析 pactl list sink-inputs")
    am = am_mod.AudioManager()
    am_mod.subprocess.check_output = lambda cmd, **kw: (
        SINK_INPUTS_OUTPUT if cmd[:3] == ["pactl", "list", "sink-inputs"]
        else SINKS_OUTPUT if cmd[:3] == ["pactl", "list", "sinks"]
        else "alsa_out_a\n"
    )
    inputs = am.list_sink_inputs()
    check("解析出 2 个播放流", len(inputs) == 2, inputs)
    check("流 1 字段完整",
          inputs[0] == {"index": 12, "sink": 0, "pid": 1234,
                        "app_name": "VLC media player"}, inputs[0])
    check("流 2 字段完整",
          inputs[1] == {"index": 13, "sink": 1, "pid": 5678,
                        "app_name": "Chromium"}, inputs[1])

    sinks = am.list_sinks()
    check("解析出 2 个 sink", len(sinks) == 2, sinks)
    check("默认 sink 标记正确", sinks[0]["is_default"] and not sinks[1]["is_default"], sinks)
    # 音量取首个声道百分比；静音看 Mute: yes/no
    check("sink a 音量解析为 70", sinks[0]["volume"] == 70, sinks[0])
    check("sink a 未静音", sinks[0]["muted"] is False, sinks[0])
    check("sink b 音量解析为 35", sinks[1]["volume"] == 35, sinks[1])
    check("sink b 静音标记", sinks[1]["muted"] is True, sinks[1])


def test_routing():
    print("audio_manager: 按 pid 路由与批量迁移")
    am = am_mod.AudioManager()
    calls = []
    am_mod.subprocess.run = lambda cmd, **kw: (
        calls.append(tuple(cmd)) or types.SimpleNamespace(returncode=0)
    )

    calls.clear()
    am.route_process_audio(5678, "alsa_out_b")
    migrated = [c for c in calls if c[1] == "move-sink-input"]
    check("只迁移目标 pid 的流",
          migrated == [("pactl", "move-sink-input", "13", "alsa_out_b")], migrated)
    check("同时切换默认 sink",
          ("pactl", "set-default-sink", "alsa_out_b") in calls, calls)

    calls.clear()
    n = am.move_all_sink_inputs("alsa_out_c")
    check("批量迁移 2 条流", n == 2, n)
    check("批量迁移覆盖两条流",
          sorted(c[2] for c in calls) == ["12", "13"], calls)

    calls.clear()
    am.route_process_audio(9999, "alsa_out_a")
    check("无流的进程不报错且不迁移",
          [c for c in calls if c[1] == "move-sink-input"] == [], calls)


def test_config_roundtrip():
    print("config: settings 兼容 / layout / pids")
    cfg = config.load_settings()
    check("首次读取写入默认配置", cfg["web_port"] == 8181, cfg)

    with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write('{"web_port": 9000}')  # 模拟旧版本残缺配置
    cfg = config.load_settings()
    check("缺字段补默认值", cfg["web_port"] == 9000 and cfg["display_height"] == 1080, cfg)

    layout = [{"app_name": "web:影视", "args": "http://172.17.0.1:5666/x",
               "x": 0, "y": 0, "w": 1920, "h": 1080, "fullscreen": True}]
    config.save_layout(layout)
    check("layout 读写一致", config.load_layout() == layout, config.load_layout())

    config.save_pids({1234: "web:影视", 5678: "xterm"})
    loaded = config.load_pids()
    check("pids key 还原为 int", loaded == {1234: "web:影视", 5678: "xterm"}, loaded)
    check("损坏的 layout 文件返回空列表",
          (open(config.LAYOUT_PATH, "w").write("{not json") or
           config.load_layout() == []), None)


def test_log_line():
    print("config: 应用日志写入 app.log")
    config.log_line("单元测试写入的一行日志")
    logs = config.read_logs(50)
    check("日志被写入且可读回", "单元测试写入的一行日志" in logs, logs[-200:])
    check("带时间戳与级别标记",
          "【INFO】" in logs and logs.strip().startswith("["), logs[-120:])
    config.log_line("出错了", "ERROR")
    check("级别可标注", "【ERROR】出错了" in config.read_logs(50))
    check("日志文件即 /data/app.log 同一份",
          os.path.abspath(config.LOG_PATH).startswith(os.path.abspath(_TMP_DATA)))


# ---------------------------------------------------------------- X11 桩
class _FakeSubprocess:
    """替身 subprocess：check_output 按命令关键字返回桩文本，run 返回指定码。"""

    DEVNULL = object()

    def __init__(self, check_output_map=None, run_rc=0, run_effect=None):
        self._map = check_output_map or {}
        self.run_rc = run_rc
        self.run_effect = run_effect
        self.calls = []

    def check_output(self, cmd, **kw):
        self.calls.append(tuple(cmd))
        for key, val in self._map.items():
            if key in cmd:
                if isinstance(val, Exception):
                    raise val
                return val
        raise FileNotFoundError(cmd[0])

    def run(self, cmd, **kw):
        self.calls.append(tuple(cmd))
        if self.run_effect is not None:
            return self.run_effect(cmd, **kw)
        return types.SimpleNamespace(returncode=self.run_rc)


# 竖屏 1536x2048 + HDMI 断开时的 xrandr --current 输出
XRR_CONNECTED = """Screen 0: minimum 320 x 200, current 1536 x 2048, maximum 16384 x 16384
HDMI-1 disconnected (normal left inverted right x axis y axis)
HDMI-2 connected primary 1536x2048+0+0 (normal left inverted right x axis y axis) 300mm x 200mm
"""
XRR_DISCONNECTED = """Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384
HDMI-1 disconnected (normal left inverted right x axis y axis)
HDMI-2 disconnected (normal left inverted right x axis y axis)
"""

# 多接口混合的真实形态：HDMI 与 DP(USB-C) 已启用并排铺开、VGA 接了屏但
# 还没 --auto、DVI/eDP 空口。用来验证"通用多输出"解析不写死任何接口名。
XRR_MULTI = """Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 16384 x 16384
HDMI-A-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 509mm x 286mm
   1920x1080     60.00*+  50.00
   1280x720      60.00
DP-1 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+
DP-2 disconnected (normal left inverted right x axis y axis)
VGA-1 connected (normal left inverted right x axis y axis)
   1024x768      60.00
DVI-I-1 disconnected (normal left inverted right x axis y axis)
eDP-1 disconnected (normal left inverted right x axis y axis)
"""

# 竖屏旋转：width/height 是旋转后的几何，rotation 记在行尾的 left
XRR_ROTATED = """Screen 0: minimum 320 x 200, current 1536 x 2048, maximum 16384 x 16384
HDMI-2 connected primary 1536x2048+0+0 left (normal left inverted right x axis y axis) 300mm x 200mm
   2048x1536     60.00*+
"""


def test_backend_detect():
    print("window_manager: 输出后端探测（xrandr 实际输出）")
    wm = wm_mod.wm
    orig_sub = wm_mod.subprocess
    try:
        wm_mod.subprocess = _FakeSubprocess({"xrandr": XRR_CONNECTED})
        wm.invalidate_outputs()  # get_outputs 带短 TTL 缓存，换桩后必须失效
        info = wm.get_output_info()
        check("已连接输出判定为 drm", info["backend"] == "drm", info)
        check("输出名解析正确", info["output"] == "HDMI-2", info)
        check("模式分辨率解析正确",
              (info["width"], info["height"]) == (1536, 2048), info)

        wm_mod.subprocess = _FakeSubprocess({"xrandr": XRR_DISCONNECTED})
        wm.invalidate_outputs()
        check("无已连接输出判定为 headless",
              wm.get_output_info()["backend"] == "headless")

        wm_mod.subprocess = _FakeSubprocess({})  # 一律 FileNotFoundError
        wm.invalidate_outputs()
        check("xrandr 不可用时回退 headless",
              wm.get_output_info()["backend"] == "headless")

        os.environ["WC_FORCE_BACKEND"] = "headless"
        wm_mod.subprocess = _FakeSubprocess({"xrandr": XRR_CONNECTED})
        wm.invalidate_outputs()
        check("WC_FORCE_BACKEND 可强制覆盖",
              wm.get_output_info()["backend"] == "headless")
    finally:
        os.environ.pop("WC_FORCE_BACKEND", None)
        wm_mod.subprocess = orig_sub
        wm.invalidate_outputs()


def test_outputs_generic():
    print("window_manager: 通用多输出解析（HDMI/DP/USB-C/VGA/DVI/eDP）")
    co = wm_mod.classify_output
    check("HDMI 归类", co("HDMI-A-1") == ("HDMI", "HDMI"), co("HDMI-A-1"))
    check("DP 归类（含 USB-C DP Alt Mode）",
          co("DP-2") == ("DP", "DisplayPort / USB-C"), co("DP-2"))
    check("DisplayPort 全称归类",
          co("DisplayPort-1")[0] == "DP", co("DisplayPort-1"))
    check("VGA 归类", co("VGA-1") == ("VGA", "VGA"), co("VGA-1"))
    check("DVI 归类", co("DVI-I-1") == ("DVI", "DVI"), co("DVI-I-1"))
    check("eDP 归类", co("eDP-1")[0] == "eDP", co("eDP-1"))
    check("未知接口不丢信息", co("Foo-9") == ("Other", "其他接口"), co("Foo-9"))

    wm = wm_mod.wm
    orig_sub, orig_target = wm_mod.subprocess, wm.target_output
    try:
        wm_mod.subprocess = _FakeSubprocess({"xrandr": XRR_MULTI})
        wm.invalidate_outputs()  # 短 TTL 缓存：换桩后强制重探
        outs = wm.get_outputs()
        check("枚举全部输出（含未连接/未启用）", len(outs) == 6, len(outs))
        by = {o["name"]: o for o in outs}

        hdmi = by["HDMI-A-1"]
        check("HDMI 已连接且已启用",
              hdmi["connected"] and hdmi["enabled"], hdmi)
        check("HDMI primary 标记", hdmi["primary"] is True, hdmi)
        check("HDMI 几何与接口",
              (hdmi["width"], hdmi["height"], hdmi["x"], hdmi["y"]) ==
              (1920, 1080, 0, 0) and hdmi["connector"] == "HDMI", hdmi)
        check("HDMI 物理尺寸(EDID)",
              (hdmi["mm_width"], hdmi["mm_height"]) == (509, 286), hdmi)
        check("HDMI 首选模式与模式表",
              hdmi["preferred"] == "1920x1080" and hdmi["modes"][:2] ==
              ["1920x1080", "1280x720"], hdmi)

        dp = by["DP-1"]
        check("DP 已启用输出在 X 屏幕中的偏移 (1920,0)",
              (dp["enabled"], dp["x"], dp["y"]) == (True, 1920, 0), dp)
        check("USB-C/DP 展示名标注", dp["type_label"] == "DisplayPort / USB-C", dp)

        check("空口 DP-2 判未连接", by["DP-2"]["connected"] is False, by["DP-2"])
        check("空口 DVI-I-1 判未连接", by["DVI-I-1"]["connected"] is False)
        check("接了屏但未 --auto 的 VGA-1 = 已连接未启用",
              by["VGA-1"]["connected"] and not by["VGA-1"]["enabled"],
              by["VGA-1"])
        check("未启用输出无几何",
              (by["VGA-1"]["width"], by["VGA-1"]["x"]) == (0, 0), by["VGA-1"])
        check("eDP 空口仍被枚举",
              by["eDP-1"]["connector"] == "eDP", by["eDP-1"])

        check("可见区=所有已启用输出的外接矩形",
              wm.get_visible_area() ==
              {"x": 0, "y": 0, "width": 3840, "height": 1080},
              wm.get_visible_area())

        # 目标输出：默认 primary(HDMI)，可切到 DP
        wm.set_target_output("")
        check("默认目标输出选 primary",
              wm.resolve_output()["name"] == "HDMI-A-1")
        wm.set_target_output("DP-1")
        g = wm.get_display_geometry()
        check("切到 DP-1 后布局原点随输出平移",
              (g["x"], g["y"], g["width"], g["height"]) == (1920, 0, 1920, 1080),
              g)
        g = wm.get_display_geometry()
        check("目标输出信息带名称", g.get("output") == "DP-1", g)

        wm.set_target_output("DP-2")   # 未连接的口 → 退回 primary
        check("目标输出未连接时退回 primary",
              wm.resolve_output()["name"] == "HDMI-A-1")

        wm_mod.subprocess = _FakeSubprocess({"xrandr": XRR_ROTATED})
        wm.invalidate_outputs()
        o = wm.get_outputs()[0]
        check("旋转输出几何为旋转后尺寸",
              (o["width"], o["height"]) == (1536, 2048), o)
        check("left 旋转映射为 90°", o["rotation"] == 90, o)
    finally:
        wm.target_output = orig_target
        wm_mod.subprocess = orig_sub
        wm.invalidate_outputs()


def test_screenshot_returncode():
    print("window_manager: 截图按 scrot 返回码判定（旧图不再冒充成功）")
    wm = wm_mod.wm
    orig_sub, orig_path = wm_mod.subprocess, wm.SCREENSHOT_PATH
    shot = os.path.join(_TMP_DATA, "shot.png")
    wm.SCREENSHOT_PATH = shot
    try:
        with open(shot, "w", encoding="utf-8") as f:
            f.write("stale")  # 上一次截图残留
        wm_mod.subprocess = _FakeSubprocess({}, run_rc=1)
        check("scrot 失败时不误报成功", wm.take_screenshot() is False)
        check("失败时旧截图被清掉", not os.path.exists(shot))

        wm_mod.subprocess = _FakeSubprocess({}, run_rc=0)
        check("返回 0 但没写出文件 → 仍算失败", wm.take_screenshot() is False)

        def _write_png(cmd, **kw):
            with open(shot, "w", encoding="utf-8") as f:
                f.write("png")
            return types.SimpleNamespace(returncode=0)

        wm_mod.subprocess = _FakeSubprocess({}, run_effect=_write_png)
        check("真正写出文件才算成功", wm.take_screenshot() is True)
    finally:
        wm_mod.subprocess = orig_sub
        wm.SCREENSHOT_PATH = orig_path


def test_clamp_rect():
    print("window_manager: clamp_rect（布局几何钳进当前屏幕）")
    cr = wm_mod.WindowManager.clamp_rect
    check("越界位置被拉回",
          cr(1600, 100, 400, 300, 1536, 2048) == (1136, 100, 400, 300),
          cr(1600, 100, 400, 300, 1536, 2048))
    check("负坐标归零", cr(-50, -50, 400, 300, 1536, 2048) == (0, 0, 400, 300))
    check("超出屏幕的尺寸裁到屏幕",
          cr(0, 0, 1920, 3000, 1536, 2048) == (0, 0, 1536, 2048))
    check("缺字段按整屏处理",
          cr(None, None, None, None, 1536, 2048) == (0, 0, 1536, 2048))
    check("min_px 生效（布局恢复用）",
          cr(0, 0, 10, 10, 1536, 2048, 120) == (0, 0, 120, 120))
    check("min_px=0 不放大用户的小窗口",
          cr(0, 0, 10, 10, 1536, 2048) == (0, 0, 10, 10))
    check("屏幕尺寸未知时原样返回", cr(5, 6, 0, 0, 0, 0) == (5, 6, 0, 0))


def test_clamp_windows_to_screen():
    print("window_manager: 旋转/改分辨率后越界窗口回屏")
    wm = wm_mod.wm
    orig = (wm.list_windows, wm.get_outputs, wm.set_window_rect)
    moved = []

    def _out(w, h, x=0, y=0, name="DP-1"):
        return {"name": name, "connected": True, "enabled": True,
                "width": w, "height": h, "x": x, "y": y}

    try:
        # 单屏：可见区 = (0,0)-(1536,2048)，与旧"整屏"实现等价
        wm.get_outputs = lambda: [_out(1536, 2048)]
        wm.list_windows = lambda: [
            {"pid": 1, "x": 0, "y": 0, "width": 1920, "height": 1080},     # 太宽
            {"pid": 2, "x": 1600, "y": 100, "width": 400, "height": 300},  # 出右边界
            {"pid": 3, "x": 10, "y": 10, "width": 800, "height": 600},     # 正常
        ]
        wm.set_window_rect = lambda pid, x, y, w, h: (
            moved.append((pid, x, y, w, h)) or True
        )
        n = wm.clamp_windows_to_screen()
        check("只修正真正越界的 2 个窗口", n == 2, n)
        check("修正参数正确",
              moved == [(1, 0, 0, 1536, 1080), (2, 1136, 100, 400, 300)], moved)

        # 多屏：可见区原点不在 (0,0)。唯一输出挂在 X 屏幕右侧 (1920,0)，
        # 左侧 0~1920 是没有任何输出的空白区；落在空白区的窗口坐标"合法"
        # 却在显示器上看不见，必须被平移回已启用输出。
        moved.clear()
        wm.get_outputs = lambda: [_out(1920, 1080, x=1920, y=0, name="HDMI-A-1")]
        wm.list_windows = lambda: [
            {"pid": 4, "x": 100, "y": 50, "width": 400, "height": 300},      # 空白区
            {"pid": 5, "x": 1920, "y": 0, "width": 1920, "height": 1080},    # 贴目标屏
        ]
        n = wm.clamp_windows_to_screen()
        check("多屏时落在空白区的窗口被拉回已启用输出", n == 1, n)
        check("多屏偏移修正参数正确（含原点平移）",
              moved == [(4, 1920, 50, 400, 300)], moved)

        # 无已启用输出 → get_visible_area 回退整屏尺寸（无 X 时兜底 1920x1080），
        # 照常钳制而不是抛异常
        moved.clear()
        wm.get_outputs = lambda: []
        wm.list_windows = lambda: [
            {"pid": 6, "x": 0, "y": 0, "width": 3000, "height": 1080},     # 超宽
        ]
        n = wm.clamp_windows_to_screen()
        check("无已启用输出时回退整屏并照常钳制", n == 1, n)

        moved.clear()
        wm.get_outputs = lambda: [_out(1536, 2048)]
        wm.list_windows = lambda: []
        check("无窗口时返回 0", wm.clamp_windows_to_screen() == 0)
    finally:
        wm.list_windows, wm.get_outputs, wm.set_window_rect = orig


def test_sink_volume_mute():
    print("audio_manager: 音量/静音命令、钳制与写后失效")
    am = am_mod.AudioManager()
    calls = []
    orig_run, orig_co = am_mod.subprocess.run, am_mod.subprocess.check_output

    def fake_run(cmd, **kw):
        calls.append(tuple(cmd))
        return types.SimpleNamespace(returncode=0)

    def fake_co(cmd, **kw):
        # 每次都重新执行命令，便于验证缓存是否失效
        calls.append(("__check_output__",) + tuple(cmd))
        if cmd[:3] == ["pactl", "list", "sinks"]:
            return SINKS_OUTPUT
        if cmd[:3] == ["pactl", "get-default-sink"]:
            return "alsa_out_a\n"
        return ""

    am_mod.subprocess.run = fake_run
    am_mod.subprocess.check_output = fake_co
    try:
        am.invalidate_sinks()
        calls.clear()
        check("设置音量返回成功", am.set_sink_volume("alsa_out_a", 50) is True)
        check("音量命令带百分号",
              ("pactl", "set-sink-volume", "alsa_out_a", "50%") in calls, calls)

        calls.clear()
        am.set_sink_volume("alsa_out_a", 150)
        check("音量上限钳到 100",
              ("pactl", "set-sink-volume", "alsa_out_a", "100%") in calls, calls)
        am.set_sink_volume("alsa_out_a", -20)
        check("音量下限钳到 0",
              ("pactl", "set-sink-volume", "alsa_out_a", "0%") in calls, calls)

        calls.clear()
        am.set_sink_mute("alsa_out_b", True)
        check("静音发 1",
              ("pactl", "set-sink-mute", "alsa_out_b", "1") in calls, calls)
        am.set_sink_mute("alsa_out_b", False)
        check("取消静音发 0",
              ("pactl", "set-sink-mute", "alsa_out_b", "0") in calls, calls)

        check("非数字音量被拒绝", am.set_sink_volume("alsa_out_a", "x") is False)

        # 写后失效：先灌满 TTL 缓存，设置音量后下一次 list 必须真的重查 pactl
        am.list_sinks()
        base = sum(1 for c in calls if c[:3] == ("__check_output__", "pactl", "list"))
        am.list_sinks()
        check("TTL 内不重复查询",
              sum(1 for c in calls if c[:3] == ("__check_output__", "pactl", "list"))
              == base)
        am.set_sink_volume("alsa_out_a", 42)
        am.list_sinks()
        check("写后缓存失效、重新查询",
              sum(1 for c in calls if c[:3] == ("__check_output__", "pactl", "list"))
              == base + 1, calls)
    finally:
        am_mod.subprocess.run, am_mod.subprocess.check_output = orig_run, orig_co
        am.invalidate_sinks()


def test_poller_caches():
    print("window_manager: get_outputs/list_windows 短 TTL 缓存与失效")
    wm = wm_mod.wm
    orig_sub = wm_mod.subprocess
    n = {"xrandr": 0, "xdotool": 0}

    class CountingSub(_FakeSubprocess):
        def check_output(self, cmd, **kwargs):
            if cmd and cmd[0] in n:
                n[cmd[0]] += 1
            return super().check_output(cmd, **kwargs)

    try:
        wm_mod.subprocess = CountingSub({"xrandr": XRR_MULTI, "xdotool": ""})
        wm.invalidate_outputs()
        wm.invalidate_windows()

        wm.get_outputs()
        n1 = n["xrandr"]
        wm.get_outputs()
        check("outputs 第二次读命中缓存", n["xrandr"] == n1, n)
        wm.invalidate_outputs()
        wm.get_outputs()
        check("invalidate_outputs 后重新探测", n["xrandr"] == n1 + 1, n)
        check("force=True 绕过缓存",
              wm.get_outputs(force=True) and n["xrandr"] == n1 + 2, n)

        wm.list_windows()
        m1 = n["xdotool"]
        wm.list_windows()
        check("windows 第二次读命中缓存", n["xdotool"] == m1, n)
        wm.invalidate_windows()
        wm.list_windows()
        check("invalidate_windows 后重新枚举", n["xdotool"] == m1 + 1, n)
    finally:
        wm_mod.subprocess = orig_sub
        wm.invalidate_outputs()
        wm.invalidate_windows()


def test_setrect_fullscreen_backoff():
    print("window_manager: set_window_rect 对全屏窗口先退全屏再几何")
    wm = wm_mod.wm
    orig_sub = wm_mod.subprocess
    calls = []

    def make_fake(state_text):
        def fake_co(cmd, **kw):
            calls.append(("co",) + tuple(cmd))
            if cmd[:2] == ["xdotool", "search"]:
                return "7777\n"
            if cmd[:2] == ["xprop", "-id"]:
                if "_NET_WM_STATE" in cmd:
                    return state_text
                return "not found.\n"  # frame extents / transient 均按无处理
            if cmd and cmd[0] == "xwininfo":
                return ("xwininfo: Window id: 7777\n"
                        "  Absolute upper-left X: 0\n"
                        "  Absolute upper-left Y: 0\n"
                        "  Width: 800\n  Height: 600\n")
            return ""

        def fake_run(cmd, **kw):
            calls.append(("run",) + tuple(cmd))
            return types.SimpleNamespace(returncode=0)

        return fake_co, fake_run

    try:
        fake_co, fake_run = make_fake(
            "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN")
        wm_mod.subprocess.check_output = fake_co
        wm_mod.subprocess.run = fake_run
        wm.invalidate_windows()
        ok = wm.set_window_rect(4321, 10, 20, 300, 200)
        check("全屏窗口 setrect 返回成功", ok is True)
        run_cmds = [c for c in calls if c[0] == "run"]
        idx_remove = next((i for i, c in enumerate(run_cmds)
                           if c[1:6] == ("wmctrl", "-i", "-r", "7777", "-b")
                           and c[6] == "remove,fullscreen"), -1)
        idx_move = next((i for i, c in enumerate(run_cmds)
                         if c[1] == "xdotool" and c[2] == "windowmove"), -1)
        check("先 wmctrl 退全屏", idx_remove >= 0, run_cmds)
        check("退全屏早于 windowmove",
              0 <= idx_remove < idx_move, (idx_remove, idx_move, run_cmds))

        # 静态解析器
        check("含 FULLSCREEN 原子判定全屏",
              wm._state_has_fullscreen(
                  "_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE,"
                  "_NET_WM_STATE_FULLSCREEN") is True)
        check("仅 ABOVE 不判为全屏",
              wm._state_has_fullscreen(
                  "_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE") is False)
        check("ABOVE 原子判定置顶",
              wm._state_has_above(
                  "_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE") is True)

        # 非全屏窗口：不发 wmctrl，直接移动
        calls.clear()
        fake_co, fake_run = make_fake("_NET_WM_STATE(ATOM) = ")
        wm_mod.subprocess.check_output = fake_co
        wm_mod.subprocess.run = fake_run
        check("普通窗口 setrect 成功", wm.set_window_rect(4321, 1, 2, 300, 200))
        run_cmds = [c for c in calls if c[0] == "run"]
        check("普通窗口不发 wmctrl",
              all(c[1] != "wmctrl" for c in run_cmds), run_cmds)
        check("普通窗口直接 windowmove",
              any(c[1] == "xdotool" and c[2] == "windowmove" for c in run_cmds),
              run_cmds)
    finally:
        wm_mod.subprocess = orig_sub
        wm.invalidate_windows()


def test_minimized_window_completion():
    print("window_manager: 最小化窗口经 pid 注册表以 visible=False 补全")
    wm = wm_mod.wm
    import app_runner as ar_mod
    orig_sub = wm_mod.subprocess
    orig_reg = dict(ar_mod.pid_app_name)
    orig_psutil = ar_mod.psutil

    class _FakeProc:
        def __init__(self, pid):
            self._pid = pid

        def status(self):
            if self._pid == 31337:
                return "running"
            raise ProcessLookupError(self._pid)

    fake_psutil = types.ModuleType("psutil")
    fake_psutil.Process = _FakeProc

    def make_co(wid_for_pid):
        def fake_co(cmd, **kw):
            if cmd[:3] == ["xdotool", "search", "--onlyvisible"]:
                return ""  # 当前没有任何可见窗口
            if cmd[:3] == ["xdotool", "search", "--pid"]:
                return wid_for_pid
            if cmd and cmd[0] == "xwininfo":
                return ("xwininfo: Window id: %s\n"
                        "  Absolute upper-left X: 10\n"
                        "  Absolute upper-left Y: 10\n"
                        "  Width: 300\n  Height: 200\n" % wid_for_pid.strip())
            if cmd[:2] == ["xprop", "-id"]:
                if "_NET_WM_PID" in cmd:
                    return "_NET_WM_PID(CARDINAL) = 31337\n"
                return "not found.\n"
            return ""
        return fake_co

    try:
        ar_mod.psutil = fake_psutil
        ar_mod.pid_app_name.clear()
        ar_mod.pid_app_name[31337] = "xedit"
        wm_mod.subprocess.check_output = make_co("4242\n")
        wm.invalidate_windows()

        wins = wm.list_windows(force=True)
        check("补全出 1 个最小化窗口", len(wins) == 1, wins)
        if wins:
            w0 = wins[0]
            check("补全条目标记 visible=False", w0.get("visible") is False, w0)
            check("补全条目 pid/app_name 来自注册表",
                  w0.get("pid") == 31337 and w0.get("app_name") == "xedit", w0)
            check("补全条目带几何",
                  (w0.get("width"), w0.get("height")) == (300, 200), w0)

        # search --pid 查不到窗口时 wid 回退为 str(pid)，该条目必须丢弃
        wm_mod.subprocess.check_output = make_co("")
        wm.invalidate_windows()
        check("注册表 pid 无真实窗口时不补幽灵条目",
              wm.list_windows(force=True) == [], wm.list_windows(force=True))

        # 已死 pid 不补全
        ar_mod.pid_app_name.clear()
        ar_mod.pid_app_name[99999] = "ghost"
        wm_mod.subprocess.check_output = make_co("5555\n")
        wm.invalidate_windows()
        check("已退出的注册表 pid 不补全",
              wm.list_windows(force=True) == [], wm.list_windows(force=True))
    finally:
        ar_mod.psutil = orig_psutil
        ar_mod.pid_app_name.clear()
        ar_mod.pid_app_name.update(orig_reg)
        wm_mod.subprocess = orig_sub
        wm.invalidate_windows()


def test_window_minimize_commands():
    print("window_manager: 最小化 / 恢复 / 置顶命令序列")
    wm = wm_mod.wm
    orig_sub = wm_mod.subprocess
    calls = []

    def fake_co(cmd, **kw):
        calls.append(("co",) + tuple(cmd))
        if cmd[:2] == ["xdotool", "search"]:
            return "8888\n"
        return ""

    def fake_run(cmd, **kw):
        calls.append(("run",) + tuple(cmd))
        return types.SimpleNamespace(returncode=0)

    try:
        wm_mod.subprocess.check_output = fake_co
        wm_mod.subprocess.run = fake_run
        check("最小化成功", wm.set_minimized(2024, True) is True)
        check("最小化发 windowminimize",
              any(c[1] == "xdotool" and c[2] == "windowminimize" for c in calls),
              calls)

        calls.clear()
        check("恢复成功", wm.set_minimized(2024, False) is True)
        check("恢复发 windowactivate --sync",
              any(c[1] == "xdotool" and c[2] == "windowactivate"
                  and "--sync" in c for c in calls), calls)
        check("恢复后 windowraise",
              any(c[1] == "xdotool" and c[2] == "windowraise" for c in calls),
              calls)

        calls.clear()
        check("置顶成功", wm.set_above(2024, True) is True)
        check("置顶发 wmctrl add,above",
              any(c[1] == "wmctrl" and "add,above" in c for c in calls), calls)
        wm.set_above(2024, False)
        check("取消置顶发 wmctrl remove,above",
              any(c[1] == "wmctrl" and "remove,above" in c for c in calls), calls)
    finally:
        wm_mod.subprocess = orig_sub
        wm.invalidate_windows()


def test_profiles_store():
    print("config: 布局方案 profiles 读写与损坏兜底")
    if os.path.exists(config.PROFILES_PATH):
        os.remove(config.PROFILES_PATH)
    check("无文件时为空", config.load_profiles() == {})

    prof = {"办公": [{"app_name": "xterm", "x": 0, "y": 0, "w": 800, "h": 600}],
            "影院": []}
    config.save_profiles(prof)
    loaded = config.load_profiles()
    check("profiles 读写一致（中文名）", loaded == prof, loaded)

    with open(config.PROFILES_PATH, "w", encoding="utf-8") as f:
        f.write("{broken json")
    check("损坏文件兜底空 dict", config.load_profiles() == {})

    with open(config.PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump(["not", "a", "dict"], f)
    check("非 dict 兜底空 dict", config.load_profiles() == {})

    with open(config.PROFILES_PATH, "w", encoding="utf-8") as f:
        json.dump({"好方案": [{"app_name": "x"}], "脏数据": "not-a-list",
                   7: [{"app_name": "y"}]}, f)
    cleaned = config.load_profiles()
    check("非 list 值被剔除、int key 转字符串",
          cleaned == {"好方案": [{"app_name": "x"}],
                      "7": [{"app_name": "y"}]}, cleaned)
    os.remove(config.PROFILES_PATH)


def test_profile_name_and_interval():
    print("main: 方案名校验 + 定时截图间隔 API")
    nn = main_mod._normalize_profile_name
    check("空名拒绝", nn("")[1] != "" and nn("   ")[1] != "")
    check("两侧空白被剥离", nn("  办公布局 ") == ("办公布局", ""), nn("  办公布局 "))
    check("超长名拒绝", nn("方" * 41)[1] != "")
    check("40 字以内通过", nn("方" * 40) == ("方" * 40, ""))
    check("控制字符拒绝", nn("a\tb")[1] != "")

    check("默认设置含 screenshot_interval=0",
          config.DEFAULT_SETTINGS["screenshot_interval"] == 0)

    # 端点已改为同步（FastAPI 自动放线程池），直接调用即可
    def call(sec):
        return main_mod.api_set_screenshot_interval(sec)

    r = call(30)
    check("30 秒被接受", r["ok"] and r["screenshot_interval"] == 30, r)
    r = call(5)
    check("低于下限拒绝", not r["ok"], r)
    r = call(4000)
    check("超过上限拒绝", not r["ok"], r)
    r = call(0)
    check("0 关闭被接受", r["ok"] and r["screenshot_interval"] == 0, r)
    r = call("abc")
    check("非整数拒绝", not r["ok"], r)
    # 复位 settings 文件，避免污染其它用例
    cfg = config.load_settings()
    cfg["screenshot_interval"] = 0
    config.save_settings(cfg)


def test_ui_themes():
    """面板配色主题：CSS / app.js / main.py / config.py 四处必须一一对应。

    加主题最容易漏改其中一处，而漏改的症状很隐蔽：
      - CSS 有块、JS 列表没有 → 色板少一个，用户根本不知道有这套配色
      - JS 有、后端白名单没有   → 点下去被拒，界面只是"点了没反应"
      - config 默认值不在清单里 → 首屏 data-theme 指到不存在的块，变量全空
    这类问题在浏览器里排查成本远高于在这里断言一次。
    """
    print("ui_theme: 配色主题清单在各处保持一致")

    def read(*parts):
        with open(os.path.join(SRC, *parts), encoding="utf-8") as f:
            return f.read()

    css = read("static", "index.css")
    js = read("static", "app.js")
    py = read("main.py")
    cfg = read("config.py")

    css_themes = set(re.findall(r':root\[data-theme="([^"]+)"\]', css))
    css_themes.add("midnight")           # :root 本身即默认主题，无 [data-theme] 选择器
    js_themes = set(re.findall(r'id:\s*"(\w+)",\s*name:\s*"', js))
    m = re.search(r"UI_THEMES\s*=\s*\(([^)]*)\)", py)
    check("main.py 定义了 UI_THEMES 白名单", m is not None)
    py_themes = set(re.findall(r'"(\w+)"', m.group(1))) if m else set()

    check("至少提供 2 套配色", len(css_themes) >= 2, sorted(css_themes))
    check("CSS 与 app.js 的主题清单一致", css_themes == js_themes,
          f"仅CSS有={sorted(css_themes - js_themes)} 仅JS有={sorted(js_themes - css_themes)}")
    check("CSS 与 main.py 白名单一致", css_themes == py_themes,
          f"仅CSS有={sorted(css_themes - py_themes)} 仅后端有={sorted(py_themes - css_themes)}")

    d = re.search(r'"ui_theme":\s*"(\w+)"', cfg)
    check("config 默认 ui_theme 在清单内", bool(d) and d.group(1) in css_themes,
          d.group(1) if d else "未定义")

    # 每套主题都必须真的覆盖关键变量，否则切过去跟没切一样
    def block(theme):
        if theme == "midnight":
            mm = re.search(r":root\s*\{(.*?)\}", css, re.S)
        else:
            mm = re.search(r':root\[data-theme="%s"\]\s*\{(.*?)\}' % re.escape(theme),
                           css, re.S)
        return mm.group(1) if mm else ""

    needed = ("--bg-base", "--bg-elev-1", "--border", "--text-primary",
              "--accent-blue", "--accent-blue-rgb", "--accent-green",
              "--accent-red", "--panel-grad-from", "--canvas-grad-from")
    for t in sorted(css_themes):
        body = block(t)
        missing = [v for v in needed if v not in body]
        check(f"主题「{t}」覆盖了全部关键变量", not missing, missing)

    # 浅色主题必须把叠加层翻成深色，否则白色叠白底等于没有
    light = block("daylight")
    if light:
        check("浅色主题覆盖了 --overlay-*（白叠白会看不见）",
              all(v in light for v in ("--overlay-bg", "--overlay-border",
                                       "--handle-color", "--select-ring")))
        check("浅色主题 --text-primary 是深色",
              bool(re.search(r"--text-primary:\s*#(1|2|3)[0-9a-fA-F]{5}", light)),
              re.search(r"--text-primary:\s*(\S+);", light).group(1)
              if re.search(r"--text-primary:\s*(\S+);", light) else "?")
    print()


def test_docker_scanner():
    """Docker 容器扫描：端口挑选 / 镜像 tag / 与飞牛应用去重 / 自身排除 / 降级。

    这几处都是"不看真实 docker 也能断言"的纯逻辑，而它们恰好最容易写错：
      - 挑错端口   → 卡片点开是空白页（把 http 打到 https 端口上、或挑到未发布的端口）
      - 不去重     → 同一个应用在「第三方应用」和「Docker 应用」里各出现一次
      - 不排除自身 → 面板把自己也列成一个"可启动的应用"
      - 不降级     → 没挂 socket 时整个应用列表接口报错
    """
    print("docker_scanner: 端口挑选 / 镜像 tag / 去重 / 自身排除 / 降级")

    # 端口挑选：优先常见 Web 端口，TLS 端口排最后
    ports = [{"Type": "tcp", "PrivatePort": 443, "PublicPort": 44300},
             {"Type": "tcp", "PrivatePort": 8080, "PublicPort": 18080}]
    check("优先挑出常见 Web 端口而非 TLS 端口",
          sc_mod._pick_web_port(ports) == (18080, "http"), sc_mod._pick_web_port(ports))

    ports = [{"Type": "tcp", "PrivatePort": 443, "PublicPort": 443}]
    check("只剩 TLS 端口时用 https",
          sc_mod._pick_web_port(ports) == (443, "https"), sc_mod._pick_web_port(ports))

    ports = [{"Type": "udp", "PrivatePort": 53, "PublicPort": 5353}]
    check("UDP 映射不作为 Web 入口",
          sc_mod._pick_web_port(ports) == (0, "http"), sc_mod._pick_web_port(ports))

    check("没有已发布端口时返回 0",
          sc_mod._pick_web_port([]) == (0, "http"))
    check("exposed 但未发布（PublicPort=0）不算",
          sc_mod._pick_web_port([{"Type": "tcp", "PrivatePort": 80,
                                  "PublicPort": 0}]) == (0, "http"))

    # 镜像 tag
    check("镜像 tag 正常解析", sc_mod._image_tag("nginx:1.25") == "1.25")
    check("无 tag 返回空", sc_mod._image_tag("nginx") == "")
    check("latest 正常返回", sc_mod._image_tag("nginx:latest") == "latest")
    check("registry 端口不被当成 tag", sc_mod._image_tag("registry:5000/foo/bar") == "")

    # 与飞牛应用去重
    known = {"jellyfin", "qbit"}
    check("同名容器被判为已由飞牛应用覆盖",
          sc_mod._covered_by_fnos("jellyfin", "", known))
    check("compose 序号后缀也算覆盖",
          sc_mod._covered_by_fnos("jellyfin-1", "jellyfin", known))
    check("下划线后缀也算覆盖", sc_mod._covered_by_fnos("qbit_web", "", known))
    check("无关容器不误杀",
          not sc_mod._covered_by_fnos("qbittorrent", "mystack", known))

    # 驼峰目录名 vs 连字符容器名（真机：FnMessageBot / fn-message-bot）
    n = sc_mod._norm_app_name
    check("归一：驼峰转小写连写", n("FnMessageBot") == "fnmessagebot")
    check("归一：连字符小写", n("fn-message-bot") == "fnmessagebot")
    check("归一：下划线/空格等价",
          n("fn_message bot") == "fnmessagebot")
    check("驼峰 vs 连字符判为同一应用",
          sc_mod._covered_by_fnos("fn-message-bot", "", {"FnMessageBot"}))
    check("compose project 驼峰归一也命中",
          sc_mod._covered_by_fnos("x", "FnMessageBot", {"FnMessageBot"}))
    check("归一后不同名不误杀",
          not sc_mod._covered_by_fnos("fnpackup", "", {"FnMessageBot"}))

    # _match_fnos_app：端口匹配优先 + Docker 运行状态回填依据
    fnos = [
        {"name": "FnMessageBot", "port": "18230", "running": False, "group": "第三方应用"},
        {"name": "fnpackup", "port": "1069", "running": False, "group": "第三方应用"},
        {"name": "fndesk", "port": None, "running": False, "group": "第三方应用"},
    ]
    hit = sc_mod._match_fnos_app(
        {"name": "fn-message-bot", "project": "", "port": 18230,
         "running": True}, fnos)
    check("端口匹配到 FnMessageBot（名称完全不同）",
          hit is not None and hit["name"] == "FnMessageBot")
    hit = sc_mod._match_fnos_app(
        {"name": "fnpackup", "project": "", "port": 1069,
         "running": True}, fnos)
    check("同名+端口匹配 fnpackup",
          hit is not None and hit["name"] == "fnpackup")
    hit = sc_mod._match_fnos_app(
        {"name": "unrelated-nginx", "project": "", "port": 8080,
         "running": True}, fnos)
    check("无关容器不命中", hit is None)
    hit = sc_mod._match_fnos_app(
        {"name": "fndesk-web", "project": "", "port": None,
         "running": True}, fnos)
    check("名称前缀命中（无端口时）",
          hit is not None and hit["name"] == "fndesk")
    check("host_apps_mounted 返回布尔",
          isinstance(sc_mod.host_apps_mounted(), bool))

    # 自身容器排除
    check("按容器名排除自身",
          sc_mod._is_self_container("a" * 64, "window-composer", set()))
    check("按 hostname(短 id) 排除自身",
          sc_mod._is_self_container("abc123def456" + "0" * 52, "whatever",
                                    {"abc123def456"}))
    check("其他容器不受影响",
          not sc_mod._is_self_container("f" * 64, "qbittorrent", {"abc123def456"}))

    # 降级：socket 不存在时必须返回空/False，而不是抛异常
    orig_sock = sc_mod.DOCKER_SOCK
    try:
        sc_mod.DOCKER_SOCK = os.path.join(_TMP_DATA, "definitely-missing.sock")
        check("socket 缺失时 docker_available 为 False",
              sc_mod.docker_available() is False)
        check("socket 缺失时扫描返回空列表", sc_mod._scan_docker_apps() == [])

        # 宿主没有 docker.sock 时，Docker 会按 bind mount 在该路径建一个空目录；
        # 若只判断 exists 就会误判成"已挂载"
        fake_dir = os.path.join(_TMP_DATA, "fake-sock-dir")
        os.makedirs(fake_dir, exist_ok=True)
        sc_mod.DOCKER_SOCK = fake_dir
        check("挂成目录不被误判为可用 socket",
              sc_mod.docker_available() is False)
    finally:
        sc_mod.DOCKER_SOCK = orig_sock
    print()


def test_docker_http_pipeline():
    """Docker 扫描的 HTTP 链路：请求路径、响应解析、字段提取、异常降级。

    本机（Windows）的 socket 模块没有 AF_UNIX，真 unix socket 跑不起来，于是把
    连接工厂换成 TCP，打一个本地假 Docker 服务。这样覆盖到 _scan_docker_apps 里
    除"socket 怎么连"之外的全部逻辑 —— 而解析这一段恰恰最容易**静默**写错：
    写错的表现是"Docker 分组永远空的"，在 NAS 上极难排查。
    """
    print("docker_scanner: HTTP 链路（请求路径 / 响应解析 / 异常降级）")

    class FakeDocker(http.server.BaseHTTPRequestHandler):
        payload = b"[]"
        status = 200
        seen = []

        def do_GET(self):
            type(self).seen.append(self.path)
            body = type(self).payload
            self.send_response(type(self).status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDocker)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class TcpConn(http.client.HTTPConnection):
        def __init__(self, _sock_path, timeout=5.0):
            super().__init__("127.0.0.1", port, timeout=timeout)

    containers = [
        {"Id": "a" * 64, "Names": ["/qbittorrent"],
         "Image": "linuxserver/qbittorrent:4.6.0", "State": "running",
         "Status": "Up 3 hours", "Labels": {},
         "Ports": [{"Type": "tcp", "PrivatePort": 8080, "PublicPort": 8080},
                   {"Type": "udp", "PrivatePort": 6881, "PublicPort": 6881}]},
        {"Id": "b" * 64, "Names": ["/vault"], "Image": "hashicorp/vault:1.15",
         "State": "exited", "Status": "Exited (0) 2 days ago",
         "Labels": {"org.opencontainers.image.title": "Vault"},
         "Ports": [{"Type": "tcp", "PrivatePort": 8200, "PublicPort": 8200}]},
        {"Id": "c" * 64, "Names": ["/window-composer"],
         "Image": "window-composer:1.0.0", "State": "running",
         "Status": "Up 1 hour", "Labels": {}, "Ports": []},
        {"Id": "d" * 64, "Names": ["/sidecar"], "Image": "alpine:3.19",
         "State": "running", "Status": "Up 5 minutes", "Labels": {}, "Ports": []},
    ]

    orig_avail, orig_conn = sc_mod.docker_available, sc_mod._docker_conn
    try:
        sc_mod.docker_available = lambda: True
        sc_mod._docker_conn = TcpConn
        FakeDocker.status = 200
        FakeDocker.seen = []
        FakeDocker.payload = json.dumps(containers).encode("utf-8")

        apps = sc_mod._scan_docker_apps()
        by_name = {a["name"]: a for a in apps}

        check("请求打到 /containers/json?all=1（含已停止容器）",
              bool(FakeDocker.seen) and "/containers/json" in FakeDocker.seen[0]
              and "all=1" in FakeDocker.seen[0], FakeDocker.seen)
        check("自身容器被排除", "window-composer" not in by_name, sorted(by_name))
        check("其余容器全部识别", len(apps) == 3, sorted(by_name))
        check("全部归入「Docker 应用」分组",
              all(a["group"] == sc_mod.DOCKER_GROUP and a["source"] == "docker"
                  for a in apps))

        qb = by_name.get("qbittorrent", {})
        check("发布端口变成可访问地址",
              qb.get("port") == 8080 and qb.get("url", "").endswith(":8080/")
              and qb.get("has_url") is True, qb.get("url"))
        check("UDP 端口不被选为 Web 入口", qb.get("port") != 6881)
        check("运行中状态取自 State", qb.get("running") is True)
        check("镜像 tag 提取为版本", qb.get("version") == "4.6.0", qb.get("version"))
        check("卡片副标题用的镜像引用保留",
              qb.get("image") == "linuxserver/qbittorrent:4.6.0")

        vt = by_name.get("vault", {})
        check("已停止容器 running=False", vt.get("running") is False)
        check("label 标题优先作为展示名", vt.get("label") == "Vault", vt.get("label"))

        side = by_name.get("sidecar", {})
        check("无端口映射的容器仍列出但不可显示",
              side.get("has_url") is False and side.get("port") is None
              and side.get("url") == "", side)

        # 降级：非 200 / 坏 JSON 都必须是空列表，不能把接口带崩
        FakeDocker.status = 500
        FakeDocker.payload = b'{"message":"boom"}'
        check("守护进程返回非 200 时降级为空列表", sc_mod._scan_docker_apps() == [])

        FakeDocker.status = 200
        FakeDocker.payload = b"<html>not json</html>"
        check("响应不是合法 JSON 时降级为空列表", sc_mod._scan_docker_apps() == [])

        class DeadConn(http.client.HTTPConnection):
            def __init__(self, _sock_path, timeout=5.0):
                super().__init__("127.0.0.1", 1, timeout=1.0)

            def connect(self):
                raise ConnectionRefusedError("模拟 socket 消失")

        sc_mod._docker_conn = DeadConn
        FakeDocker.payload = json.dumps(containers).encode("utf-8")
        check("连接失败时降级为空列表", sc_mod._scan_docker_apps() == [])
    finally:
        sc_mod.docker_available = orig_avail
        sc_mod._docker_conn = orig_conn
        srv.shutdown()
        srv.server_close()

    # 真实连接工厂：Windows 的 socket 没有 AF_UNIX，Linux 上是文件不存在。
    # 两种都必须是"返回 None"，而不是抛出去把应用列表接口一起带崩。
    orig_sock = sc_mod.DOCKER_SOCK
    try:
        sc_mod.DOCKER_SOCK = os.path.join(_TMP_DATA, "no-such.sock")
        try:
            res = sc_mod._docker_get("/containers/json", timeout=1.0)
            check("平台不支持 unix socket 时返回 None 而不抛异常", res is None, res)
        except Exception as exc:  # noqa: BLE001 - 这里就是要断言不抛
            check("平台不支持 unix socket 时返回 None 而不抛异常", False,
                  f"{type(exc).__name__}: {exc}")
    finally:
        sc_mod.DOCKER_SOCK = orig_sock
    print()


def test_config_resilience():
    """settings 损坏兜底 / layout 结构校验 / 原子写不留临时文件 / 日志防伪造。"""
    print("config: 损坏兜底、原子写、日志防伪造")
    # settings.json 被截断成半截 JSON：不能让面板 500，回退默认并重写
    with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write('{"web_port": 9000, "bg_co')
    cfg = config.load_settings()
    check("截断的 settings 回退默认", cfg["web_port"] == 8181, cfg)
    check("回退后文件已重写为合法 JSON",
          json.load(open(config.SETTINGS_PATH, encoding="utf-8"))["web_port"] == 8181)

    with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write("[1, 2, 3]")
    check("非 dict 的 settings 同样回退", config.load_settings()["bg_color"] == "#000000")

    config.save_settings(dict(config.DEFAULT_SETTINGS))
    leftovers = [f for f in os.listdir(_TMP_DATA) if f.endswith(".tmp")
                 or ".tmp." in f]
    check("原子写不留临时文件", not leftovers, leftovers)

    # layout 结构非法：非 list / 混入非 dict 项都要被挡掉，
    # 否则 apply_layout 的 item.get 会抛 AttributeError
    with open(config.LAYOUT_PATH, "w", encoding="utf-8") as f:
        f.write('[{"app_name": "web:影视"}, "junk", 7, null]')
    layout = config.load_layout()
    check("layout 只保留 dict 项", layout == [{"app_name": "web:影视"}], layout)
    with open(config.LAYOUT_PATH, "w", encoding="utf-8") as f:
        f.write('{"not": "a list"}')
    check("非 list 的 layout 返回空", config.load_layout() == [])

    # 日志防伪造：入参里的换行不能制造出一条假日志行
    config.log_line("首行\n[2020-01-01 00:00:00] 【INFO】伪造行\n尾行")
    tail = config.read_logs(50).strip().splitlines()
    joined_last = tail[-1]
    check("换行被压平，伪造内容无法另起一行", "伪造行" in joined_last
          and not any("伪造行" in ln and ln.strip().startswith("[2020") for ln in tail[:-1]),
          tail[-3:])


def test_browser_cmd():
    """firefox/chromium 启动参数：独立 profile、几何参数合法。"""
    print("app_runner: 浏览器启动参数")
    ff = ar_mod._build_browser_cmd(
        "/usr/bin/firefox", "http://nas/x", True,
        "/tmp/chromium-profile-abc", "abc", 10, 20, 800, 600)
    joined = " ".join(ff)
    check("firefox 独立 -profile", "-profile" in ff
          and "/tmp/firefox-profile-abc" in ff, ff)
    check("firefox 带 --no-remote（支持多开）", "--no-remote" in ff, ff)
    check("firefox 不使用非法的 --geometry", "--geometry" not in joined, ff)
    check("firefox 用 --width/--height",
          "--width" in ff and "800" in ff and "--height" in ff and "600" in ff, ff)
    check("URL 是末位参数", ff[-1] == "http://nas/x", ff)

    ch = ar_mod._build_browser_cmd(
        "/usr/bin/chromium", "http://nas/y", False,
        "/tmp/chromium-profile-def", "def", 10, 20, 800, 600)
    check("chromium 独立 user-data-dir",
          "--user-data-dir=/tmp/chromium-profile-def" in ch, ch)
    check("chromium 窗口位置/尺寸参数",
          "--window-position=10,20" in ch and "--window-size=800,600" in ch, ch)
    check("chromium 带 --disable-gpu/--test-type",
          "--disable-gpu" in ch and "--test-type" in ch, ch)
    check("chromium URL 是末位参数", ch[-1] == "http://nas/y", ch)


def test_stop_guard():
    """停止护栏：PID 1 / 自己 / 祖先进程绝不允许被杀。"""
    print("app_runner: 误杀防护与进程组")
    if not _HAS_PSUTIL:
        print("  [SKIP] 环境无真实 psutil，跳过进程护栏用例")
        return
    import psutil
    me = os.getpid()
    check("拒绝杀 Web 自身", bool(ar_mod.stop_refused_reason(me)))
    check("拒绝杀 PID 1/0", bool(ar_mod.stop_refused_reason(1))
          and bool(ar_mod.stop_refused_reason(0)))
    ppid = psutil.Process(me).ppid()
    check("拒绝杀祖先进程（entrypoint 链）",
          ar_mod.is_protected_pid(ppid), ppid)

    alive_before = psutil.pid_exists(me)
    rc = ar_mod.stop_by_pid(me)
    check("stop_by_pid 对自身返回 False", rc is False, rc)
    check("自身进程安然无恙", psutil.pid_exists(me) == alive_before)

    # 不存在的 pid：不视为受保护，返回 True（语义=调用后已不存在），不抛异常
    rc = ar_mod.stop_by_pid(99999999)
    check("stop_by_pid 对死 pid 返回 True", rc is True, rc)


def test_pid_identity():
    """容器重建标记 + create_time 双重身份校验，防 PID 复用错杀系统进程。"""
    print("app_runner: pid 注册表身份校验")
    if not _HAS_PSUTIL:
        print("  [SKIP] 环境无真实 psutil，跳过 pid 身份校验用例")
        return
    import psutil
    me = os.getpid()
    my_ctime = psutil.Process(me).create_time()
    saved_names, saved_ctimes = None, None
    # 打桩固定实例标记：Windows 上无权读 PID 1 的 create_time，
    # 真实容器内 root 可以；打桩后两个平台行为一致、测试确定。
    fixed_marker = "test-instance-marker"
    orig_marker_fn = ar_mod._current_instance_marker
    ar_mod._current_instance_marker = lambda: fixed_marker
    try:
        with ar_mod._registry_lock:
            saved_names = dict(ar_mod.pid_app_name)
            saved_ctimes = dict(ar_mod.pid_ctime)
            ar_mod.pid_app_name.clear()
            ar_mod.pid_ctime.clear()

        # 场景 1：同一容器内 pid 被复用——存活但 create_time 对不上 → 丢弃
        config.write_instance_marker(fixed_marker)
        with ar_mod._registry_lock:
            ar_mod.pid_app_name[me] = "web:旧应用"
            ar_mod.pid_ctime[me] = my_ctime + 100000.0  # 明显是另一个进程
            ar_mod.pid_app_name[99999999] = "x:已死"
            ar_mod.pid_ctime[99999999] = 0.0
        n = ar_mod.reconcile_pids()
        check("create_time 不匹配的存活 pid 被剔除", me not in ar_mod.pid_app_name,
              dict(ar_mod.pid_app_name))
        check("已死 pid 被剔除", 99999999 not in ar_mod.pid_app_name)
        check("清理后注册表为空", n == 0 and len(ar_mod.pid_app_name) == 0, n)

        # 场景 2：容器重建——实例标记变化，即使 pid 活着且 ctime 吻合也全丢
        config.write_instance_marker("BOGUS-PREVIOUS-INSTANCE")
        with ar_mod._registry_lock:
            ar_mod.pid_app_name[me] = "web:上个容器的浏览器"
            ar_mod.pid_ctime[me] = my_ctime
        n = ar_mod.reconcile_pids()
        check("实例标记变化时注册表整体丢弃", n == 0
              and me not in ar_mod.pid_app_name, dict(ar_mod.pid_app_name))

        # 场景 3：同实例 + ctime 吻合 → 保留
        with ar_mod._registry_lock:
            ar_mod.pid_app_name[me] = "web:本实例应用"
            ar_mod.pid_ctime[me] = my_ctime
        n = ar_mod.reconcile_pids()
        check("身份一致的 pid 被保留", n == 1
              and ar_mod.pid_app_name.get(me) == "web:本实例应用",
              dict(ar_mod.pid_app_name))

        # 场景 4：旧版本无 ctime 记录（升级场景）→ 退化为仅存活判定，不误删
        with ar_mod._registry_lock:
            ar_mod.pid_app_name[me] = "web:升级前的应用"
            ar_mod.pid_ctime.clear()
        n = ar_mod.reconcile_pids()
        check("缺少 ctime 记录时退化为存活判定", n == 1
              and ar_mod.pid_app_name.get(me) == "web:升级前的应用", n)
    finally:
        ar_mod._current_instance_marker = orig_marker_fn
        with ar_mod._registry_lock:
            ar_mod.pid_app_name.clear()
            ar_mod.pid_app_name.update(saved_names)
            ar_mod.pid_ctime.clear()
            ar_mod.pid_ctime.update(saved_ctimes)
        ar_mod._persist()
        config.write_instance_marker(orig_marker_fn() or "restored")


def test_csrf_origin():
    """Origin/Referer 与 Host 一致性判定。"""
    print("main: CSRF 来源校验")
    f = main_mod._origin_allowed
    check("同源 Origin 放行",
          f("http://127.0.0.1:8181/", "127.0.0.1:8181") is True)
    check("飞牛 iframe 内同源（NAS IP）放行",
          f("http://192.168.1.10:8181/app", "192.168.1.10:8181") is True)
    check("外域 Origin 拒绝",
          f("http://evil.example.com/", "192.168.1.10:8181") is False)
    check("端口不一致拒绝",
          f("http://192.168.1.10:5666/", "192.168.1.10:8181") is False)
    check("畸形来源拒绝", f("not-a-url", "127.0.0.1:8181") is False)


class _CI(dict):
    """不区分大小写的请求头映射（模拟 Starlette Headers）。"""

    def __init__(self, items):
        super().__init__()
        for k, v in items:
            self[k.lower()] = v

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def test_access_control_pure():
    """访问控制纯函数：回环/主机名/令牌提取/浏览器入口判定/令牌生成。"""
    print("access_control: 回环 / 主机名 / 令牌提取 / 入口判定")
    check("IPv4 回环", ac_mod.is_loopback("127.0.0.1:51234"))
    check("IPv6 回环带括号端口", ac_mod.is_loopback("[::1]:8181"))
    check("::1 裸地址", ac_mod.is_loopback("::1"))
    check("局域网地址非回环", not ac_mod.is_loopback("192.168.31.205:8181"))
    check("空地址非回环", not ac_mod.is_loopback(""))

    h = ac_mod._hostname_of
    check("去端口", h("192.168.31.205:5666") == "192.168.31.205")
    check("URL 取主机", h("http://192.168.31.205:5666/desktop") == "192.168.31.205")
    check("IPv6 去括号去端口", h("[2001:db8::1]:443") == "2001:db8::1")
    check("主机名小写", h("NAS-01:8181") == "nas-01")

    # 令牌提取四个通道
    H = _CI
    check("X-Access-Token 头",
          ac_mod.presented_token(H([("X-Access-Token", "abc123")])) == "abc123")
    check("Bearer 头",
          ac_mod.presented_token(H([("Authorization", "Bearer abc123")])) == "abc123")
    check("Cookie 提取",
          ac_mod.presented_token(H([("Cookie", "foo=1; wc_at=abc123; x=y")]))
          == "abc123")
    check("无令牌返回空", ac_mod.presented_token(H([])) == "")
    check("畸形 Bearer 不取值",
          ac_mod.presented_token(H([("Authorization", "Basic xyz")])) == "")

    # 浏览器入口判定
    check("面板自身同源 XHR 放行", ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Sec-Fetch-Site", "same-origin"),
        ("Sec-Fetch-Dest", "empty"), ("Sec-Fetch-Mode", "cors")])))
    check("飞牛桌面 iframe（5666→8181）放行", ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Dest", "iframe"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Referer", "http://192.168.31.205:5666/app")])))
    check("飞牛桌面新标签页跳转放行", ac_mod.browser_entry_allowed(H([
        ("Host", "nas.local:8181"),
        ("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Referer", "https://nas.local:5666/")])))
    check("外域 iframe 嵌入拒绝", not ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Dest", "iframe"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Referer", "http://evil.example.com/")])))
    check("无 Referer 的跨站 iframe 拒绝", not ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Dest", "iframe"),
        ("Sec-Fetch-Mode", "navigate")])))
    check("地址栏直输（Sec-Fetch-Site:none）拒绝", not ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Sec-Fetch-Site", "none"), ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate")])))
    check("curl 无任何浏览器头拒绝", not ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181")])))
    check("旧浏览器同主机 Referer 放行", ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181"),
        ("Referer", "http://192.168.31.205:5666/x")])))
    check("旧浏览器无 Referer 拒绝", not ac_mod.browser_entry_allowed(H([
        ("Host", "192.168.31.205:8181")])))

    # 综合判定
    tok = "t0ken"
    check("回环直接放行", ac_mod.is_authorized("127.0.0.1:5", H([]), tok)[0])
    check("关闭保护全放行",
          ac_mod.is_authorized("10.0.0.9", H([]), tok, False)[0])
    check("正确令牌放行", ac_mod.is_authorized("10.0.0.9", H([
        ("X-Access-Token", "t0ken")]), tok)[0])
    check("错误令牌拒绝", not ac_mod.is_authorized("10.0.0.9", H([
        ("X-Access-Token", "wrong")]), tok)[0])
    check("无令牌局域网拒绝", not ac_mod.is_authorized("10.0.0.9", H([]), tok)[0])

    # 令牌初始化：出厂固定初始令牌、幂等、env 预置、旧数据升级
    old_env = os.environ.pop("WC_ACCESS_TOKEN", None)
    try:
        s1 = {}
        t1 = ac_mod.ensure_access_token(s1)
        check("首次写入出厂固定初始令牌",
              t1 == ac_mod.DEFAULT_ACCESS_TOKEN == "admin123", t1)
        check("初始令牌标记为未定制",
              s1.get("access_token_customized") is False)
        check("初始状态可被识别", ac_mod.token_is_default(s1))

        s2 = {"access_token": "fixed-token"}
        check("已有令牌不覆盖", ac_mod.ensure_access_token(s2) == "fixed-token")
        check("旧版数据（无定制标记）补为已定制",
              s2.get("access_token_customized") is True)
        check("旧随机令牌不被误判为初始令牌",
              not ac_mod.token_is_default(s2))

        os.environ["WC_ACCESS_TOKEN"] = "preset-xyz"
        s3 = {}
        check("env 预置令牌生效", ac_mod.ensure_access_token(s3) == "preset-xyz")
        check("env 预置视为已定制",
              s3.get("access_token_customized") is True)
    finally:
        if old_env is not None:
            os.environ["WC_ACCESS_TOKEN"] = old_env

    # 用户自定义令牌校验
    v = ac_mod.validate_custom_token
    check("合法自定义令牌通过", v("My-Token_1.0")[0])
    check("6 位下边界通过", v("abc123")[0])
    check("64 位上边界通过", v("a" * 64)[0])
    check("少于 6 位拒绝", not v("abc12")[0])
    check("超过 64 位拒绝", not v("a" * 65)[0])
    check("空白令牌拒绝", not v("   ")[0])
    check("非字符串拒绝", not v(None)[0])
    check("空格/&/? 等 URL 危险字符拒绝",
          not v("ab cd12")[0] and not v("ab&cd12")[0]
          and not v("ab?cd12")[0] and not v("ab=cd12")[0])
    check("与出厂初始令牌相同拒绝", not v(ac_mod.DEFAULT_ACCESS_TOKEN)[0])

    # 自定义 / 随机重置后初始状态清除
    s4 = {"access_token": ac_mod.DEFAULT_ACCESS_TOKEN,
          "access_token_customized": False}
    check("初始状态可被识别(2)", ac_mod.token_is_default(s4))
    s4["access_token"] = "new-custom-token"
    s4["access_token_customized"] = True
    check("自定义后初始标记清除", not ac_mod.token_is_default(s4))
    s4["access_token"] = ac_mod.DEFAULT_ACCESS_TOKEN
    check("已定制后即使值与初始相同也不算初始（标记优先）",
          not ac_mod.token_is_default(s4))

    check("公开路径识别", ac_mod.is_public_path("/static/app.js")
          and ac_mod.is_public_path("/api/access/login")
          and not ac_mod.is_public_path("/api/status"))


def test_access_middleware():
    """ASGI 层集成：未授权 401、令牌各通道放行、登录页、查询令牌种 Cookie。"""
    print("access_control: 中间件 ASGI 集成")
    from starlette.middleware.base import BaseHTTPMiddleware

    reached = {"n": 0}

    async def inner_app(scope, receive, send):
        reached["n"] += 1
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": b'{"ok":true,"who":"inner"}'})

    app = BaseHTTPMiddleware(inner_app,
                             dispatch=main_mod.access_token_guard)

    def call(headers=(), method="GET", path="/api/status", query=b"",
             client=("10.0.0.9", 45000)):
        reached["n"] = 0
        scope = {"type": "http", "http_version": "1.1", "method": method,
                 "path": path, "raw_path": path.encode(),
                 "query_string": query,
                 "headers": [(k.lower().encode(), v.encode())
                             for k, v in headers],
                 "client": client,
                 "server": ("192.168.31.205", 8181), "scheme": "http"}
        out = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
                out["headers"] = m["headers"]
            elif m["type"] == "http.response.body":
                out["body"] = out.get("body", b"") + m.get("body", b"")
        asyncio.run(app(scope, receive, send))
        return out

    cfg = config.load_settings()
    token = ac_mod.ensure_access_token(cfg, config.save_settings)
    cfg = config.load_settings()
    cfg["require_token"] = True
    config.save_settings(cfg)

    r = call()
    check("局域网无令牌 → 401", r["status"] == 401 and reached["n"] == 0,
          r.get("status"))
    body = r.get("body", b"").decode("utf-8", "replace")
    check("401 返回 JSON 提示", "需要访问令牌" in body, body[:80])

    r = call(client=("127.0.0.1", 55000))
    check("本机回环放行", r["status"] == 200 and reached["n"] == 1)

    r = call([("X-Access-Token", token)])
    check("头部令牌放行", r["status"] == 200 and reached["n"] == 1)
    r = call([("X-Access-Token", "nope")])
    check("错误头部令牌 401", r["status"] == 401)
    r = call([("Cookie", "wc_at=" + token)])
    check("Cookie 令牌放行", r["status"] == 200 and reached["n"] == 1)
    r = call(query=("wc_at=" + token).encode())
    check("查询参数令牌放行", r["status"] == 200 and reached["n"] == 1)
    sc = [v for k, v in r["headers"] if k == b"set-cookie"]
    check("查询令牌首次访问种下 Cookie",
          any(b"wc_at=" + token.encode() in v and b"HttpOnly" in v for v in sc),
          sc)

    r = call([("Host", "192.168.31.205:8181"),
              ("Sec-Fetch-Site", "same-origin")])
    check("同源浏览器请求放行", r["status"] == 200)
    r = call([("Host", "192.168.31.205:8181"),
              ("Sec-Fetch-Site", "cross-site"),
              ("Sec-Fetch-Dest", "iframe"), ("Sec-Fetch-Mode", "navigate"),
              ("Referer", "http://192.168.31.205:5666/desktop")])
    check("飞牛桌面 iframe 入口放行", r["status"] == 200)
    r = call([("Host", "192.168.31.205:8181"),
              ("Sec-Fetch-Site", "cross-site"),
              ("Sec-Fetch-Dest", "iframe"), ("Sec-Fetch-Mode", "navigate"),
              ("Referer", "http://evil.example.com/")])
    check("外域 iframe 401", r["status"] == 401)

    r = call(path="/", headers=[("Accept", "text/html")])
    body = r.get("body", b"").decode("utf-8", "replace")
    check("浏览器直输首页给令牌输入页",
          r["status"] == 401 and "访问验证" in body, r.get("status"))

    r = call(path="/static/app.js")
    check("静态资源公开", r["status"] == 200 and reached["n"] == 1)
    r = call(path="/api/access/status")
    check("鉴权状态接口公开", r["status"] == 200 and reached["n"] == 1)

    # 关闭保护后全部放行
    cfg = config.load_settings()
    cfg["require_token"] = False
    config.save_settings(cfg)
    r = call()
    check("关闭保护后无令牌放行", r["status"] == 200 and reached["n"] == 1)
    cfg = config.load_settings()
    cfg["require_token"] = True
    config.save_settings(cfg)

    # 自定义令牌端点（直接调用视图函数；DATA_DIR 已是测试临时目录）
    cfg = config.load_settings()
    ac_mod.ensure_access_token(cfg, config.save_settings)
    resp = main_mod.api_access_set_token(token="bad 1")
    check("set_token 非法/过短令牌 → 422", resp.status_code == 422,
          resp.status_code)
    resp = main_mod.api_access_set_token(token=ac_mod.DEFAULT_ACCESS_TOKEN)
    check("set_token 与出厂初始令牌相同 → 422", resp.status_code == 422)
    resp = main_mod.api_access_set_token(token="unit-test-token-001")
    check("set_token 合法令牌 → 200", resp.status_code == 200,
          resp.status_code)
    body = json.loads(resp.body.decode("utf-8"))
    check("set_token 响应回带新令牌", body.get("token") == "unit-test-token-001")
    cfg2 = config.load_settings()
    check("自定义令牌已持久化",
          cfg2.get("access_token") == "unit-test-token-001")
    check("自定义后标记为已定制",
          cfg2.get("access_token_customized") is True)
    sc2 = resp.headers.get("set-cookie", "")
    check("set_token 为当前浏览器回种新 Cookie",
          "wc_at=unit-test-token-001" in sc2 and "HttpOnly" in sc2, sc2)
    resp = main_mod.api_access_set_token(token="unit-test-token-001")
    check("重复设置为相同令牌 → 422", resp.status_code == 422)

    # status 公开接口：暴露初始标记但不泄露令牌内容
    sresp = main_mod.api_access_status()
    check("status 返回 token_is_default 布尔",
          sresp.get("token_is_default") is False)
    check("status 不泄露令牌本身", "token" not in sresp)


def test_input_validation():
    """H1 输入收紧：程序名/参数/Web 目标校验。"""
    print("main: 启动参数与 Web 目标校验")
    vn = main_mod.validate_local_app_name
    va = main_mod.validate_local_app_args
    vw = main_mod.validate_web_target

    check("空程序名拒绝", vn("") is not None)
    check("普通命令通过", vn("xedit") is None)
    check("带点/下划线/连字符通过", vn("my-app.bin_2") is None)
    check("数字开头通过", vn("7zip") is None)
    check("绝对路径通过", vn("/usr/bin/xedit") is None)
    check("带空格拒绝（应走 args）", vn("xedit -a") is not None)
    check("点段穿越拒绝", vn("../bin/sh") is not None)
    check("绝对路径含 .. 拒绝", vn("/usr/../bin/xedit") is not None)
    check("分号注入形态拒绝", vn("xedit;reboot") is not None)
    check("反引号拒绝", vn("x`id`") is not None)
    check("路径尾斜杠拒绝", vn("/usr/bin/") is not None)
    check("超长名拒绝", vn("a" * 256) is not None)
    check("内含换行拒绝", vn("xedit\nx") is not None)
    check("纯空白拒绝", vn("  \n\t ") is not None)

    check("正常参数通过", va('--fullscreen "/data/a b.mp4"') is None)
    check("参数超长拒绝", va("a" * 2001) is not None)
    check("未闭合引号拒绝", va("'unclosed") is not None)
    check("参数 token 过多拒绝", va(" ".join(["a"] * 65)) is not None)
    check("参数含换行拒绝", va("--x\ny") is not None)
    check("普通制表符允许", va("-a\t-b") is None)

    check("正常 Web 目标通过",
          vw("影视", "http://192.168.31.205:8096/web/index.html") is None)
    check("https 通过", vw("A", "https://nas.local/x") is None)
    check("空应用名拒绝", vw("", "http://x/") is not None)
    check("应用名含换行拒绝", vw("a\nb", "http://x/") is not None)
    check("超长应用名拒绝", vw("x" * 101, "http://x/") is not None)
    check("ftp 协议拒绝", vw("a", "ftp://x/") is not None)
    check("javascript 协议拒绝", vw("a", "javascript:alert(1)") is not None)
    check("缺主机名拒绝", vw("a", "http:///x") is not None)
    check("超长 URL 拒绝", vw("a", "http://x/" + "a" * 2050) is not None)


def test_restart_policy_and_offload():
    """L4 Docker 重启策略同步（假守护进程）+ L1 阻塞端点已下沉线程池。"""
    print("app_scanner/main: 重启策略同步 + 阻塞端点线程池化")
    import inspect

    class FakeDockerCtl(http.server.BaseHTTPRequestHandler):
        seen = []

        def _reply(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            type(self).seen.append(("GET", self.path, b""))
            if self.path.endswith("/json"):
                self._reply(200, b'{"HostConfig":{"RestartPolicy":'
                                 b'{"Name":"unless-stopped"}}}')
            else:
                self._reply(404, b"{}")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            type(self).seen.append(("POST", self.path, body))
            self._reply(200, b'{"Warnings":[]}')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDockerCtl)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class TcpConn(http.client.HTTPConnection):
        def __init__(self, _sock_path, timeout=5.0):
            super().__init__("127.0.0.1", port, timeout=timeout)

    orig_avail = sc_mod.docker_available
    orig_host = os.environ.get("HOSTNAME")
    try:
        sc_mod.docker_available = lambda: True
        os.environ["HOSTNAME"] = "abcdef012345"
        FakeDockerCtl.seen = []
        ok = sc_mod.set_self_restart_policy("no", conn_factory=TcpConn)
        check("关闭自启 → update 成功", ok)
        method, path, body = FakeDockerCtl.seen[-1]
        check("请求打到自身容器 update", method == "POST"
              and path == "/containers/abcdef012345/update", (method, path))
        payload = json.loads(body.decode())
        check("重启策略体为 no",
              payload["RestartPolicy"]["Name"] == "no", payload)

        ok = sc_mod.set_self_restart_policy("unless-stopped",
                                            conn_factory=TcpConn)
        check("开启自启 → unless-stopped 成功", ok
              and json.loads(FakeDockerCtl.seen[-1][2])["RestartPolicy"]["Name"]
              == "unless-stopped")

        check("非法策略名拒绝",
              sc_mod.set_self_restart_policy("bogus", conn_factory=TcpConn)
              is False)
        pol = sc_mod.get_self_restart_policy(conn_factory=TcpConn)
        check("读取当前策略", pol == "unless-stopped", pol)
    finally:
        sc_mod.docker_available = orig_avail
        if orig_host is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = orig_host
        srv.shutdown()

    # mountinfo 容器 ID 发现（飞牛真机：host 网络 + cgroup v2 形态）
    mi = os.path.join(tempfile.gettempdir(), "wc-mountinfo.txt")
    snippet = (
        "856 729 0:63 / / rw,relatime - overlay overlay rw,"
        "upperdir=/vol1/docker/overlay2/"
        "1d9346f92f31d96a828ffa36e908cdf96d669187ffe850dcf0e338c14a5fb293"
        "/diff,workdir=/x/work\n"
        "882 856 0:55 /docker/containers/"
        "5049b762ef9fa1a774fb6995d4f02affaf57deedd928ec7439e875f3a6d4beb6"
        "/resolv.conf /etc/resolv.conf rw - btrfs /dev/mapper/x rw\n")
    with open(mi, "w", encoding="utf-8") as f:
        f.write(snippet)
    ids = sc_mod._mountinfo_ids(paths=(mi,))
    check("mountinfo 绑定挂载路径提取 64 位 ID",
          ids and ids[0] == "5049b762ef9fa1a774fb6995d4f02affaf57deedd928ec7439e875f3a6d4beb6",
          ids)
    with open(mi, "w", encoding="utf-8") as f:
        f.write("9 8 0:1 / / rw - overlay overlay rw,upperdir=/var/lib/"
                "docker/overlay2/abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789/diff\n")
    ids = sc_mod._mountinfo_ids(paths=(mi,))
    check("仅有 overlay upperdir 时兜底提取",
          ids and ids[0].startswith("abcdef012345"), ids)
    with open(mi, "w", encoding="utf-8") as f:
        f.write("nothing useful here\n")
    check("无 ID 的 mountinfo 返回空", sc_mod._mountinfo_ids(paths=(mi,)) == [])
    check("不存在的路径返回空", sc_mod._mountinfo_ids(paths=(mi + ".nope",)) == [])

    # host 网络：HOSTNAME=NAS 主机名、cgroup 为空，仍靠 mountinfo 拿到短 ID
    orig_fn = sc_mod._mountinfo_ids
    sc_mod._mountinfo_ids = lambda paths=(): [
        "5049b762ef9fa1a774fb6995d4f02affaf57deedd928ec7439e875f3a6d4beb6"]
    old_hn = os.environ.get("HOSTNAME")
    os.environ["HOSTNAME"] = "mozi-nas"
    try:
        check("host 网络下经 mountinfo 得到 12 位短 ID",
              sc_mod._self_container_id() == "5049b762ef9f")
        toks = sc_mod._self_tokens()
        check("自容器标识含长/短 ID",
              "5049b762ef9f" in toks and
              "5049b762ef9fa1a774fb6995d4f02affaf57deedd928ec7439e875f3a6d4beb6"
              in toks)
    finally:
        sc_mod._mountinfo_ids = orig_fn
        if old_hn is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = old_hn
    try:
        os.remove(mi)
    except OSError:
        pass

    # 无 socket 环境（如本机 Windows）安全降级为 False
    sc_mod.docker_available = lambda: False
    check("无 docker socket 时降级 False",
          sc_mod.set_self_restart_policy("no") is False)
    sc_mod.docker_available = orig_avail

    # L1：重阻塞端点必须是同步函数（FastAPI 自动放线程池），
    # 事件循环线程绝不能直接跑 scrot/xdotool/pactl
    for name in ("api_screenshot", "api_layout_restore", "api_app_start",
                 "api_apps_installed", "api_app_stop", "api_app_stop_all",
                 "api_window_setrect", "api_audio_set_sink", "api_windows",
                 "api_layout_apply_profile"):
        fn = getattr(main_mod, name)
        check(f"{name} 为同步端点", not inspect.iscoroutinefunction(fn), name)
    check("apply_layout 为同步函数",
          not inspect.iscoroutinefunction(main_mod.apply_layout))
    check("含 sleep 的显示端点仍是协程",
          inspect.iscoroutinefunction(main_mod.api_display_rotate))
    check("线程锁为 threading.Lock",
          isinstance(main_mod._layout_lock, type(threading.Lock())))
    # 空布局直接返回零值统计（不触达任何 X 工具）
    res = main_mod.apply_layout_threadsafe([])
    check("空布局幂等返回零值",
          res == {"total": 0, "started": 0, "repositioned": 0,
                  "failed": 0, "details": []}, res)


def test_audio_stack_assets():
    """真机验证过的 WirePlumber 音频配方必须固化在镜像/入口/compose 里。

    飞牛 NAS 实测：PipeWire 单独运行时 pactl 只有 auto_null（Dummy Output）；
    WirePlumber 0.4.13 在容器内必须同时具备：
      1. dbus 系统总线（dbus-daemon --system）+ machine-id
      2. /run/systemd/{users,seats,sessions} 占位目录（logind 插件 inotify
         add_watch 目标，缺失时 ENOENT 直接中止，wireplumber 随即退出）
      3. 宿主 /run/udev 只读挂载（ALSA 设备枚举数据源）
    满足后 alsa_card.pci-* 与 alsa_output.pci-* 物理 sink 正常出现且可出声。
    """
    root = os.path.dirname(HERE)

    def _find(*rels):
        """同一文件在仓库与镜像内路径不同（镜像只 COPY src/tests/entrypoint）。"""
        for rel in rels:
            p = os.path.join(root, rel) if not os.path.isabs(rel) else rel
            if os.path.exists(p):
                return p
        return None

    def _read(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # 镜像内 entrypoint 位于 /entrypoint.sh，仓库内在项目根目录
    ep_path = _find("entrypoint.sh", "../entrypoint.sh",
                    "/entrypoint.sh", "/app/entrypoint.sh")
    check("能定位到 entrypoint.sh（仓库或镜像布局）", ep_path is not None)
    ep = _read(ep_path) if ep_path else ""
    check("entrypoint 依赖自检含 wireplumber",
          "pipewire wireplumber pipewire-pulse" in ep)
    check("entrypoint 依赖自检含 dbus-daemon/dbus-uuidgen",
          "dbus-launch dbus-daemon dbus-uuidgen" in ep)
    check("entrypoint 启动 dbus 系统总线",
          'dbus-daemon --system --fork' in ep)
    check("entrypoint 对 dbus 系统总线做探活（restart 后清理失效 socket）",
          "org.freedesktop.DBus.Peer.Ping" in ep and
          "rm -f /run/dbus/system_bus_socket /var/run/dbus/pid" in ep)
    check("entrypoint 确保 /etc/machine-id",
          "dbus-uuidgen --ensure /etc/machine-id" in ep)
    check("entrypoint 创建 logind 占位目录",
          "/run/systemd/users /run/systemd/seats /run/systemd/sessions" in ep)
    check("entrypoint 启动 WirePlumber",
          "wireplumber >/data/wireplumber.log 2>&1 &" in ep)
    check("entrypoint 记录 WirePlumber PID",
          "WIREPLUMBER_PID=$!" in ep)
    check("entrypoint 监控循环保活 WirePlumber",
          ep.count("wireplumber >/data/wireplumber.log 2>&1 &") >= 2)
    check("entrypoint 轮转 wireplumber.log",
          "rotate_log /data/wireplumber.log" in ep)
    check("entrypoint 默认输出避开 pcspkr 蜂鸣器",
          "*pcspkr*|*beep*|*dummy*|*auto_null*" in ep)
    check("entrypoint 应用已保存的 default_audio_sink",
          "default_audio_sink // empty" in ep)
    check("旧注释（不启动 WirePlumber）已删除",
          "不启动 WirePlumber" not in ep)

    # Dockerfile / compose 只存在于构建上下文，镜像内不存在 —— 找到才断言，
    # 镜像自检时跳过（这些路径在仓库测试与 fnpack 校验里已覆盖）。
    df_path = _find("Dockerfile", "../Dockerfile")
    if df_path:
        df = _read(df_path)
        check("Dockerfile 安装 wireplumber 包",
              any(ln.strip() == "wireplumber \\" or ln.strip() == "wireplumber"
                  for ln in df.splitlines()))

    cc_path = _find("docker-compose.yml", "../docker-compose.yml")
    if cc_path:
        check("独立 compose 挂载 /run/udev:ro",
              "/run/udev:/run/udev:ro" in _read(cc_path))
    fc_path = _find(os.path.join("packaging", "fnos", "app", "docker",
                                 "docker-compose.yaml"),
                    os.path.join("..", "packaging", "fnos", "app", "docker",
                                 "docker-compose.yaml"))
    if fc_path:
        check("飞牛 compose 挂载 /run/udev:ro",
              "/run/udev:/run/udev:ro" in _read(fc_path))


def test_fnos_app_scan_assets():
    """飞牛应用扫描配方必须固化：软链根挂载 + entrypoint 健康检查。

    真机实测：/var/apps/<应用>/target 是绝对软链，第三方应用指向
    /volN/@appcenter/<应用>、内置 trim.* 指向 /usr/local/apps/@appcenter/<应用>。
    只挂 /var/apps 时容器内软链断裂：扫得到应用名却读不到 target/ui/config，
    Web 入口全空。正式 fpk compose 一度漏挂 /var/apps 本身，这里一并固化。
    """
    print("app_scanner: 飞牛应用扫描挂载配方")
    root = os.path.dirname(HERE)

    def _find(*rels):
        for rel in rels:
            p = os.path.join(root, rel) if not os.path.isabs(rel) else rel
            if os.path.exists(p):
                return p
        return None

    def _read(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    ep_path = _find("entrypoint.sh", "../entrypoint.sh", "/entrypoint.sh")
    check("能定位 entrypoint.sh", ep_path is not None)
    ep = _read(ep_path) if ep_path else ""
    check("entrypoint 含飞牛应用挂载检查", "check_host_apps" in ep)
    check("entrypoint 检查 target 软链断裂",
          "/host_apps/*/target" in ep)
    check("entrypoint 点名 appcenter 软链根",
          "/usr/local/apps/@appcenter" in ep)

    required_mounts = [
        "/var/apps:/host_apps:ro",
        "/proc:/hostproc:ro",
        "/vol1/@appcenter:/vol1/@appcenter:ro",
        "/usr/local/apps/@appcenter:/usr/local/apps/@appcenter:ro",
    ]
    cc_path = _find("docker-compose.yml", "../docker-compose.yml")
    if cc_path:
        cc = _read(cc_path)
        for m in required_mounts:
            check(f"独立 compose 挂载 {m}", m in cc)
    fc_path = _find(os.path.join("packaging", "fnos", "app", "docker",
                                 "docker-compose.yaml"),
                    os.path.join("..", "packaging", "fnos", "app", "docker",
                                 "docker-compose.yaml"))
    if fc_path:
        fc = _read(fc_path)
        for m in required_mounts:
            check(f"飞牛 compose 挂载 {m}", m in fc)
        check("飞牛 compose 覆盖多卷 appcenter（vol2）",
              "/vol2/@appcenter:/vol2/@appcenter:ro" in fc)


if __name__ == "__main__":
    test_sink_inputs_parse()
    test_routing()
    test_config_roundtrip()
    test_config_resilience()
    test_log_line()
    test_backend_detect()
    test_outputs_generic()
    test_screenshot_returncode()
    test_clamp_rect()
    test_clamp_windows_to_screen()
    test_sink_volume_mute()
    test_poller_caches()
    test_setrect_fullscreen_backoff()
    test_minimized_window_completion()
    test_window_minimize_commands()
    test_profiles_store()
    test_profile_name_and_interval()
    test_ui_themes()
    test_docker_scanner()
    test_docker_http_pipeline()
    test_browser_cmd()
    test_stop_guard()
    test_pid_identity()
    test_csrf_origin()
    test_access_control_pure()
    test_access_middleware()
    test_input_validation()
    test_restart_policy_and_offload()
    test_audio_stack_assets()
    test_fnos_app_scan_assets()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} -> {', '.join(_failures)}")
        sys.exit(1)
    print("ALL OFFLINE TESTS PASSED")
