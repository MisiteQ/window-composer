"""显示输出热插拔守护（HDMI / DisplayPort / USB-C / VGA / DVI 通用）。

背景：飞牛 NAS 上接显示器的方式五花八门——HDMI、DP、VGA、DVI、USB-C
（DP Alt Mode / 雷电扩展坞），而且用户完全可能"先开 NAS，后插显示器"，
或者运行中换个接口插。这些情况下会出现三个问题：

1. **新接入的输出没有像素时钟**：内核认出了连接器，但 X server 不会自动
   给新输出分配模式，屏幕保持全黑（典型现象："插上显示器没反应，必须重启容器"）。
2. **目标输出被拔掉**：窗口全部失去落点，用户看到的是黑屏。
3. **分辨率/旋转变化**：既有窗口可能落到可见区之外，看不见也点不到。

本模块以固定间隔轮询 xrandr，把上面三件事自动处理掉，并把变更事件留在
内存环形缓冲里供控制面板展示（"几点几分插了哪个接口"），排障时非常有用。

设计取舍：
- 轮询而不是监听 X RandR 事件（python-xlib 会引入额外依赖，且容器镜像里
  装 Xlib 绑定并不划算）。默认 4 秒一次 `xrandr --current`，代价可忽略。
- 只做"显示层修复"，不碰应用进程生命周期：不重启任何应用，只把越界窗口
  拉回可见区。拔掉显示器不会导致正在跑的应用被杀死。
- 线程内所有异常都被吞掉并记日志：守护线程一旦抛出就会永久退出，
  那才是真正会导致"插上显示器永远没反应"的原因。
"""
import threading
import time
from typing import Callable, Dict, List, Optional

# 一次快照里每个输出的状态元组：(connected, enabled, w, h, x, y, rotation)
OutputState = tuple


class DisplayMonitor:
    """周期性检查显示输出，自动启用新输出并回屏越界窗口。"""

    POLL_INTERVAL = 4.0
    MAX_EVENTS = 40

    def __init__(self, window_manager, poll_interval: float = None,
                 logger: Optional[Callable] = None):
        self.wm = window_manager
        self.poll_interval = float(poll_interval or self.POLL_INTERVAL)
        self._log = logger or (lambda msg, level="INFO": None)
        self._snapshot: Optional[Dict[str, OutputState]] = None
        self._area: Optional[tuple] = None
        self._events: List[Dict] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_check = 0.0

    # ------------------------------------------------------------ 纯逻辑（可测）
    @staticmethod
    def snapshot(outputs: List[Dict]) -> Dict[str, OutputState]:
        """把 get_outputs() 的结果压缩成可比较的快照。"""
        snap: Dict[str, OutputState] = {}
        for o in outputs or []:
            name = o.get("name")
            if not name:
                continue
            snap[name] = (
                bool(o.get("connected")), bool(o.get("enabled")),
                int(o.get("width") or 0), int(o.get("height") or 0),
                int(o.get("x") or 0), int(o.get("y") or 0),
                int(o.get("rotation") or 0),
            )
        return snap

    @staticmethod
    def visible_area(snap: Dict[str, OutputState]) -> tuple:
        """快照中所有"已连接且已激活"输出的外接矩形 (x, y, w, h)。"""
        active = [v for v in (snap or {}).values()
                  if v[0] and v[1] and v[2] > 0 and v[3] > 0]
        if not active:
            return (0, 0, 0, 0)
        x0 = min(v[4] for v in active)
        y0 = min(v[5] for v in active)
        x1 = max(v[4] + v[2] for v in active)
        y1 = max(v[5] + v[3] for v in active)
        return (x0, y0, x1 - x0, y1 - y0)

    @staticmethod
    def diff(prev: Optional[Dict[str, OutputState]],
             cur: Dict[str, OutputState],
             labels: Optional[Dict[str, str]] = None) -> List[Dict]:
        """比较两次快照，返回事件列表（首次调用 prev=None 时返回空）。

        事件类型：
            connected     显示器新插入某个接口
            disconnected  某个接口的显示器被拔出
            changed       分辨率 / 位置 / 旋转发生变化
            enabled       输出被本模块自动激活（补充事件，由 check_once 追加）
        """
        labels = labels or {}
        events: List[Dict] = []
        if prev is None:
            return events
        for name, st in cur.items():
            old = prev.get(name)
            if old is None:
                if st[0]:
                    events.append({"type": "connected", "output": name,
                                   "label": labels.get(name, ""),
                                   "width": st[2], "height": st[3]})
                continue
            if st[0] and not old[0]:
                events.append({"type": "connected", "output": name,
                               "label": labels.get(name, ""),
                               "width": st[2], "height": st[3]})
            elif old[0] and not st[0]:
                events.append({"type": "disconnected", "output": name,
                               "label": labels.get(name, "")})
            elif st[0] and st[2:] != old[2:]:
                events.append({"type": "changed", "output": name,
                               "label": labels.get(name, ""),
                               "width": st[2], "height": st[3],
                               "rotation": st[6],
                               "prev": {"width": old[2], "height": old[3],
                                        "x": old[4], "y": old[5],
                                        "rotation": old[6]}})
        for name, old in prev.items():
            if old[0] and name not in cur:
                events.append({"type": "disconnected", "output": name,
                               "label": labels.get(name, "")})
        return events

    # ------------------------------------------------------------ 运行期
    def check_once(self, first: bool = False) -> List[Dict]:
        """执行一轮检查，返回本轮产生的事件（已写入事件缓冲）。

        first=True 表示这是第一轮：只建立基线，不产生"插入"噪声事件，
        但仍会尝试激活"已连接未启用"的输出（容器启动时显示器就插着但
        X 没给它分配模式的情况）。
        """
        outs = self.wm.get_outputs()
        cur = self.snapshot(outs)
        labels = {o["name"]: o.get("type_label", "") for o in outs}
        events = [] if first else self.diff(self._snapshot, cur, labels)

        prev_area = self.visible_area(self._snapshot) if self._snapshot else None

        # 1) 已连接但未激活的输出 → 主动 --auto 点亮（HDMI/DP/USB-C/VGA 一视同仁）
        try:
            activated = self.wm.auto_enable_connected()
        except Exception as exc:  # noqa: BLE001 - 守护线程绝不能因单次失败退出
            activated = []
            self._log(f"显示守护：激活输出时异常 {type(exc).__name__}: {exc}", "WARN")
        if activated:
            outs = self.wm.get_outputs()
            cur = self.snapshot(outs)
            labels = {o["name"]: o.get("type_label", "") for o in outs}
            for name in activated:
                st = cur.get(name, (False, False, 0, 0, 0, 0, 0))
                events.append({"type": "enabled", "output": name,
                               "label": labels.get(name, ""),
                               "width": st[2], "height": st[3]})

        # 2) 目标输出若不可用，resolve_output 会自动回退；这里只记录"改选"事件，
        #    不覆盖 wm.target_output——用户重新插回原来的接口时应当自动回到那块屏
        try:
            resolved = self.wm.resolve_output(outs)
        except Exception:  # noqa: BLE001
            resolved = None
        wanted = getattr(self.wm, "target_output", "") or ""
        if resolved and wanted and resolved["name"] != wanted:
            events.append({"type": "target_changed", "output": resolved["name"],
                           "label": resolved.get("type_label", ""),
                           "from": wanted})

        # 3) 可见区变化 → 把越界窗口拉回来（旋转/改分辨率/换屏都会触发）
        cur_area = self.visible_area(cur)
        reflowed = 0
        if prev_area and cur_area != prev_area and cur_area[2] > 0:
            try:
                reflowed = self.wm.clamp_windows_to_screen()
            except Exception as exc:  # noqa: BLE001
                self._log(f"显示守护：回屏失败 {type(exc).__name__}: {exc}", "WARN")
            if reflowed:
                events.append({"type": "reflow", "count": reflowed,
                               "area": list(cur_area)})

        self._snapshot = cur
        self._area = cur_area
        self._last_check = time.time()

        for ev in events:
            self._push_event(ev)
            self._log_event(ev)
        return events

    def _log_event(self, ev: Dict) -> None:
        kind = ev.get("type")
        name = ev.get("output", "")
        label = ev.get("label") or ""
        tag = f"{name}（{label}）" if label else name
        if kind == "connected":
            self._log(f"检测到显示器接入：{tag} {ev.get('width')}×{ev.get('height')}")
        elif kind == "disconnected":
            self._log(f"检测到显示器断开：{tag}", "WARN")
        elif kind == "changed":
            prev = ev.get("prev") or {}
            self._log(f"显示输出变化：{tag} "
                      f"{prev.get('width')}×{prev.get('height')}"
                      f" → {ev.get('width')}×{ev.get('height')}"
                      f"（旋转 {ev.get('rotation')}°）")
        elif kind == "enabled":
            self._log(f"已自动启用输出：{tag} {ev.get('width')}×{ev.get('height')}")
        elif kind == "target_changed":
            self._log(f"目标输出不可用，自动改选：{ev.get('from')} → {tag}", "WARN")
        elif kind == "reflow":
            self._log(f"可见区变化，已把 {ev.get('count')} 个越界窗口拉回屏幕内")

    def _push_event(self, ev: Dict) -> None:
        ev = dict(ev)
        ev["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self.MAX_EVENTS:
                del self._events[:-self.MAX_EVENTS]

    def events(self) -> List[Dict]:
        """最近的事件（新的在后），供 /api/status 展示。"""
        with self._lock:
            return list(self._events)

    def status(self) -> Dict:
        with self._lock:
            count = len(self._events)
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "poll_interval": self.poll_interval,
            "last_check": self._last_check,
            "event_count": count,
            "area": list(self._area) if self._area else None,
        }

    # ------------------------------------------------------------ 线程控制
    def _loop(self) -> None:
        # poll_interval <= 0：关闭自动轮询（WC_DISPLAY_POLL_INTERVAL=0）
        if self.poll_interval <= 0:
            return
        first = True
        while not self._stop.is_set():
            try:
                self.check_once(first=first)
            except Exception as exc:  # noqa: BLE001
                # 单轮失败不能终止守护线程，否则热插拔从此永久失效
                self._log(f"显示守护：本轮检查异常 {type(exc).__name__}: {exc}",
                          "WARN")
            first = False
            self._stop.wait(self.poll_interval)

    def start(self) -> bool:
        """启动后台线程（幂等：已在运行时不重复启动）。"""
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="display-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()


def build_monitor(window_manager, logger: Optional[Callable] = None) -> DisplayMonitor:
    """按环境变量 WC_DISPLAY_POLL_INTERVAL 构造守护。

    设为 0 或负数即关闭轮询（返回的 monitor 调用 start() 会立刻退出循环），
    排障时可以先关掉自动行为，手动调 /api/display/refresh 观察结果。
    """
    import os
    raw = (os.environ.get("WC_DISPLAY_POLL_INTERVAL") or "").strip()
    try:
        interval = float(raw) if raw else DisplayMonitor.POLL_INTERVAL
    except ValueError:
        interval = DisplayMonitor.POLL_INTERVAL
    return DisplayMonitor(window_manager, poll_interval=interval, logger=logger)
