"""桌面会话环境探测（X11 DISPLAY / Wayland socket）。

背景：容器 restart 但不 recreate 时，/tmp/.X11-unix 里会残留上一代
X server 的 X0/X1 socket 文件，新启动的 Xorg 发现旧 socket 被占就会把
显示号递增（:1、:2……）；若代码写死 DISPLAY=:0，所有
X11 应用启动即死（Can't open display）。

本模块通过 /proc/net/unix 找到"当前真正有监听者"的 X socket，
不依赖写死的显示号；Wayland socket 同理在 XDG_RUNTIME_DIR 下探测。
window_manager（xdotool/xprop/wmctrl/xrandr/scrot）与 app_runner
（启动 GUI 应用）共用本模块，保证两端永远连同一个会话。
"""
import os
import glob
import time
import threading

# 2 秒 TTL 缓存：单次扫描很便宜（读一个 /proc 文件 + 几次 stat），
# 但 xdotool 调用频繁，缓存避免重复系统调用。
_TTL = 2.0
_lock = threading.Lock()
_cache = {"at": 0.0, "display": None, "wayland": None, "runtime_dir": None}


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or (
        "/run/user/0" if os.geteuid() == 0 else "/run/user/1000"
    )


def _listening_unix_paths() -> set:
    """内核 unix socket 表中存在的 socket 路径集合。

    /proc/net/unix 最后一列为路径；abstract socket 以 @ 开头，
    文件系统 socket 为绝对路径（X server 与 Wayland 合成器两种都会绑定）。
    不按状态码过滤：进程退出后残留的 socket 文件不会出现在此表中，
    因此"路径在表里"即等价于"当前有进程持有"（含 lazy 监听）。
    """
    paths = set()
    try:
        with open("/proc/net/unix", "r", encoding="utf-8", errors="replace") as f:
            next(f, None)  # 跳过表头
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                path = parts[-1]
                if path.startswith(("@", "/")):
                    paths.add(path.lstrip("@"))
    except OSError:
        pass
    return paths


def detect_display() -> str:
    """探测当前活跃的 X11 显示号，返回形如 ':0'。

    优先级：
    1. 环境变量 DISPLAY 指向的 socket 确实有监听者 → 直接用
    2. /tmp/.X11-unix/X<n> 中有监听者的最大编号
    3. 都失败时回退环境变量或 ':0'
    """
    listening = _listening_unix_paths()

    env_display = os.environ.get("DISPLAY", "").strip()
    if env_display:
        n = env_display.lstrip(":").split(".")[0]
        if n.isdigit() and f"/tmp/.X11-unix/X{n}" in listening:
            return f":{n}"

    candidates = []
    for path in glob.glob("/tmp/.X11-unix/X[0-9]*"):
        base = os.path.basename(path)
        n = base[1:]
        if n.isdigit() and path in listening:
            candidates.append((int(n), f":{n}"))
    if candidates:
        candidates.sort()
        return candidates[-1][1]

    return env_display or ":0"


def detect_wayland(runtime_dir: str) -> str:
    """探测 XDG_RUNTIME_DIR 下实际存在的 wayland-* socket。"""
    env_name = os.environ.get("WAYLAND_DISPLAY", "").strip()
    if env_name and os.path.exists(os.path.join(runtime_dir, env_name)):
        return env_name
    sockets = [
        s for s in glob.glob(os.path.join(runtime_dir, "wayland-*"))
        if not s.endswith(".lock")
    ]
    if sockets:
        return os.path.basename(sorted(sockets)[0])
    return env_name or "wayland-0"


def desktop_env(force: bool = False) -> dict:
    """返回供 X11/Wayland 客户端使用的环境变量（含完整继承环境）。"""
    now = time.time()
    with _lock:
        if not force and _cache["at"] and now - _cache["at"] < _TTL:
            env = os.environ.copy()
            env["DISPLAY"] = _cache["display"]
            env["XDG_RUNTIME_DIR"] = _cache["runtime_dir"]
            env["WAYLAND_DISPLAY"] = _cache["wayland"]
            return env

    runtime_dir = _runtime_dir()
    display = detect_display()
    wayland = detect_wayland(runtime_dir)

    with _lock:
        _cache.update(at=now, display=display, wayland=wayland,
                      runtime_dir=runtime_dir)

    env = os.environ.copy()
    env["DISPLAY"] = display
    env["XDG_RUNTIME_DIR"] = runtime_dir
    env["WAYLAND_DISPLAY"] = wayland
    return env
