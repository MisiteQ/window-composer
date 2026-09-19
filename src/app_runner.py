import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
import psutil
from typing import Dict, List, Optional

from config import (
    load_pids, save_pids, load_pid_ctimes, save_pid_ctimes,
    read_instance_marker, write_instance_marker,
)
from desktop_env import desktop_env

# pid -> app_name 映射。启动时从 /data/pids.json 加载，
# 使 Web 服务崩溃重启后仍能识别已有进程（核心架构原则：Web 挂掉，Xorg/openbox/PipeWire 不挂）
pid_app_name: Dict[int, str] = load_pids()
# pid -> 进程启动时间（create_time）。容器重建后 PID namespace 重置，
# 光凭 pid 存活会把复用该 pid 的系统进程错认成旧应用，用它做身份校验。
pid_ctime: Dict[int, float] = load_pid_ctimes()
# pid -> url 映射（仅 web 应用），用于调整窗口位置时重启到新位置
pid_url: Dict[int, str] = {}

# 注册表会被 API 协程线程与显示守护线程同时读写，迭代时加锁快照，
# 避免 "dictionary changed size during iteration"。
_registry_lock = threading.RLock()

# 绝不允许通过面板关闭的底层服务进程名（精确匹配进程名与可执行文件名）。
# 杀掉它们会导致容器退出重启或显示/音频整体中断。
PROTECTED_NAMES = {
    "Xorg", "Xwayland", "openbox", "pipewire", "pipewire-pulse",
    "pulse-server", "dbus-daemon", "dbus-launch", "entrypoint.sh",
    "bash", "sh",  # entrypoint 以 bash 运行；容器内没有别的 bash GUI 程序
}


def _record_ctime(pid: int) -> None:
    """登记进程 create_time；取不到写 0.0（reconcile 时退化为仅存活判定）。"""
    try:
        pid_ctime[pid] = psutil.Process(pid).create_time()
    except Exception:
        pid_ctime[pid] = 0.0


def _persist():
    """把内存里的映射同步到磁盘。每次启动/停止都调用一次，开销可忽略。"""
    try:
        with _registry_lock:
            save_pids(pid_app_name)
            save_pid_ctimes(pid_ctime)
    except Exception:
        pass


def is_protected_pid(pid: int) -> bool:
    """判断 pid 是否为"不可杀"的系统/底层服务进程。

    覆盖三类：
    1. PID 1（entrypoint 本身）与 Web 服务自己；
    2. 当前 Web 进程的祖先链（一路到 entrypoint）；
    3. 显示/音频/WM 基础设施（按进程名与可执行文件名匹配）。
    注册表里本服务启动的 GUI 应用不受影响。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 1 or pid == os.getpid():
        return True
    try:
        p = psutil.Process(pid)
    except Exception:
        return False  # 进程已不存在，无保护必要（调用方随后即视为成功）
    try:
        if p.name() in PROTECTED_NAMES:
            return True
        exe_name = os.path.basename(p.exe() or (p.cmdline() or [""])[0])
        if exe_name in PROTECTED_NAMES:
            return True
    except Exception:
        pass
    # 祖先链：Web 服务由 entrypoint(bash) 拉起，链上任何进程都不能杀
    try:
        ancestors = {a.pid for a in psutil.Process(os.getpid()).parents()}
        if pid in ancestors:
            return True
    except Exception:
        pass
    return False


def stop_refused_reason(pid: int) -> str:
    """stop 被安全护栏拒绝时给前端的原因；未拒绝返回空串。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "pid 不合法"
    if pid <= 1 or pid == os.getpid() or is_protected_pid(pid):
        return "拒绝关闭系统关键进程（Xorg/openbox/PipeWire/入口脚本等），请只关闭应用窗口"
    return ""


def _invalidate_window_cache():
    """应用启停后失效 wm 的窗口列表短缓存。

    window_manager 在模块顶部反向导入了本模块，直接 import 会形成循环，
    故在函数体内延迟导入；失败静默（缓存 TTL 本身也会自然过期）。
    """
    try:
        from window_manager import wm
        wm.invalidate_windows()
    except Exception:
        pass


def start_local_app(app_name: str, args: str = "") -> Optional[int]:
    """启动 GUI 程序。支持 args 参数（用 shlex 解析，允许带引号的参数）。

    例：start_local_app("vlc", "--fullscreen /data/movie.mp4")

    stdout/stderr 走 DEVNULL：GUI 程序生命周期长，若用 PIPE 又不读，
    64KB 缓冲写满后子进程会阻塞在 write 上挂死。
    """
    try:
        cmd = [app_name] + (shlex.split(args) if args else [])
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # 动态 DISPLAY/WAYLAND_DISPLAY：容器 restart 后 X 显示号可能
            # 不再是 :0，继承写死的环境会导致应用启动即死。
            env=desktop_env(),
        )
        with _registry_lock:
            pid_app_name[proc.pid] = app_name
            _record_ctime(proc.pid)
        _persist()
        _invalidate_window_cache()
        return proc.pid
    except Exception:
        return None


def _terminate_pid_tree(pid: int) -> None:
    """终止本服务启动的整个进程组：先 SIGTERM，3 秒后 SIGKILL。

    启动时用了 start_new_session=True，浏览器是新进程组的组长，组内还有
    zygote/renderer/GPU 等子进程。只杀组长会留下孤儿进程与 profile 锁，
    下次同应用启动异常。非注册进程（用户在容器里手开的 X11 程序）只杀单
    进程，绝不替它杀整个组。
    """
    use_group = hasattr(os, "killpg") and hasattr(signal, "SIGTERM")
    try:
        pgid = os.getpgid(pid) if use_group else pid
    except ProcessLookupError:
        return
    except Exception:
        pgid = pid
        use_group = False
    try:
        if use_group:
            os.killpg(pgid, signal.SIGTERM)
        else:
            psutil.Process(pid).terminate()
    except ProcessLookupError:
        return
    except Exception:
        try:
            psutil.Process(pid).terminate()
        except Exception:
            return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.1)
    try:
        if use_group:
            os.killpg(pgid, signal.SIGKILL)
        else:
            psutil.Process(pid).kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass
    try:
        psutil.Process(pid).wait(timeout=2)
    except Exception:
        pass


def stop_by_pid(pid: int) -> bool:
    """终止进程；注册表内的应用杀整个进程组，非注册进程只杀单进程。

    安全护栏：PID 1、Web 自身、Xorg/openbox/PipeWire 等基础设施一律拒绝，
    避免一个错误/伪造的 pid 把显示栈杀掉导致容器重启（旧实现进函数第一句
    就 terminate，校验在后面，等于没防）。
    返回值语义：进程在调用后已不存在即视为成功；被护栏拒绝返回 False。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if stop_refused_reason(pid):
        return False
    registered = pid in pid_app_name
    if registered:
        _terminate_pid_tree(pid)
    else:
        # 非本服务注册的窗口（容器内手工启动的 X11 程序）：只精确杀这一个
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=3)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
        except Exception:
            # 进程可能已退出，或 pid 无效；继续清理注册表
            pass
    with _registry_lock:
        pid_app_name.pop(pid, None)
        pid_ctime.pop(pid, None)
    _persist()
    pid_url.pop(pid, None)
    _invalidate_window_cache()
    # 真正成功 = 进程已不存在
    return not psutil.pid_exists(pid)


def stop_all() -> int:
    with _registry_lock:
        pids = list(pid_app_name.keys())
    cnt = 0
    for pid in pids:
        if stop_by_pid(pid):
            cnt += 1
    return cnt


def _find_browser() -> str:
    """自动检测容器内可用的浏览器（可插拔，不绑定特定浏览器）。

    飞牛应用的界面是 Web UI，要在 HDMI 上显示必须用浏览器渲染。
    系统不硬编码 chromium，按优先级自动检测可用浏览器。
    """
    candidates = (
        "chromium", "chromium-browser",
        "google-chrome", "google-chrome-stable",
        "firefox", "firefox-esr",
    )
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return ""


def probe_app_alive(pid: int, timeout: float = 0.6) -> bool:
    """启动后短暂存活校验：等待 timeout 秒，进程仍在则视为存活。

    程序不存在、非 X11 程序（连接不上 DISPLAY）或参数错误时，
    子进程会立即退出；提前发现可以给前端"启动失败"的明确提示。
    注意：对 fork 后立即退出的启动脚本可能误判，故调用方（手动启动 API）
    只把它当作提示信息，布局恢复流程不使用本函数。
    """
    if timeout > 0:
        time.sleep(timeout)
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _enforce_geometry(pid: int, x: int, y: int, w: int, h: int,
                      fullscreen: bool = False) -> None:
    """后台线程：浏览器窗口出现后用 xdotool 强制纠正位置/大小。

    chromium 会从持久化 user-data-dir 恢复上次窗口几何，忽略
    --window-position/--window-size。因此窗口出现后需要多次纠正，
    覆盖 chromium 启动期的延迟恢复（约前 4 秒）。
    fullscreen=True 时改为窗口一出现就调 wmctrl 真全屏并结束线程：
    全屏由 WM 负责移到 (0,0)、铺满、置顶，此时再用 xdotool windowsize
    抢几何会与 WM 的全屏状态互相干扰，故不再纠正。
    延迟导入 window_manager 以避免循环依赖（它反向依赖本模块）。
    """
    def _worker():
        from window_manager import wm
        deadline = time.time() + 10.0
        reapply_at = []  # 窗口首次出现后的再纠正时刻
        while time.time() < deadline:
            if not psutil.pid_exists(pid):
                return
            try:
                wid = wm._find_window_id_by_pid(pid)
                geo = wm._get_window_geometry(wid) if wid else None
            except Exception:
                geo = None
            now = time.time()
            if geo:
                if fullscreen:
                    wm.set_fullscreen(pid, True)
                    return
                if not reapply_at:
                    reapply_at = [now + 1.0, now + 2.2, now + 3.8]
                wm.set_window_rect(pid, x, y, w, h)
                reapply_at = [t for t in reapply_at if t > now]
                if not reapply_at:
                    return
                time.sleep(min(0.5, max(0.05, reapply_at[0] - now)))
                continue
            time.sleep(0.4)

    threading.Thread(target=_worker, daemon=True).start()


def _build_browser_cmd(browser: str, url: str, is_firefox: bool,
                       user_data_dir: str, safe_name: str,
                       x: Optional[int], y: Optional[int],
                       w: Optional[int], h: Optional[int]) -> List[str]:
    """构造浏览器启动命令（抽出来便于离线单测参数正确性）。

    Firefox 注意点：
    - 不支持 --geometry（会被当成 URL/操作数打开错误标签页），定位完全交给
      启动后的 _enforce_geometry（xdotool），这里只给 --width/--height；
    - 必须为每个应用分配独立 -profile + --no-remote，否则第二个应用会复用
      默认 profile 进程，"多窗口并存"直接失败（与 chromium 独立
      user-data-dir 同理）。
    """
    if is_firefox:
        cmd = [browser, "-profile", f"/tmp/firefox-profile-{safe_name}",
               "--no-remote", "--new-window"]
        if w and h:
            cmd += ["--width", str(w), "--height", str(h)]
        cmd.append(url)
        return cmd
    # Chromium 系浏览器：独立 user-data-dir + --window-position / --window-size
    cmd = [
        browser,
        f"--user-data-dir={user_data_dir}",
        "--no-sandbox",
        "--test-type",                       # 隐藏 --no-sandbox 顶部警告条
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-session-crashed-bubble",  # 容器重启后不弹 "Restore pages?"
        "--disable-features=Translate,Infobars",
    ]
    if x is not None and y is not None:
        cmd.append(f"--window-position={x},{y}")
    if w and h:
        cmd.append(f"--window-size={w},{h}")
    cmd.append(url)
    return cmd


def launch_web_app(url: str, app_name: str = "",
                   x: Optional[int] = None, y: Optional[int] = None,
                   w: Optional[int] = None, h: Optional[int] = None,
                   fullscreen: bool = False) -> Optional[int]:
    """在目标显示器上显示飞牛应用的 Web 界面。

    自动检测容器内可用的浏览器，以普通窗口模式打开应用 URL，
    通过 Xorg + openbox 渲染链路输出到显示器（HDMI/DP/USB-C/VGA/DVI 均可，
    由 Xorg 按内核连接器实际枚举，本函数不关心具体接口类型）。
    每次调用启动独立浏览器进程，可同时显示多个应用。

    几何控制两条腿走路：
    1. 启动参数 --window-position/--window-size（或 firefox --geometry）先定位
    2. openbox 允许 xdotool 精确移动窗口，窗口出现后由 _enforce_geometry
       后台线程多次纠正，抵消 chromium 从 profile 恢复旧几何的行为
    fullscreen=True：未显式给几何时按整屏几何启动，并在窗口出现后追加
    wmctrl 真全屏，等价于"双击应用卡片即全屏显示"。
    若已存在同名应用窗口，先关闭再以新位置/大小重启。

    参数:
        x, y: 窗口左上角坐标（显示像素，屏幕绝对坐标），None 则由 WM 决定
        w, h: 窗口宽高（显示像素），None 则由 WM 决定
        fullscreen: 是否真全屏（未给几何时自动使用目标输出整屏尺寸）
    返回: 浏览器进程 PID；无可用浏览器时返回 None。
    """
    browser = _find_browser()
    if not browser:
        return None

    # 全屏快捷路径：未显式指定几何时用整屏尺寸；屏幕尺寸动态探测，
    # 兼容竖屏/旋转后的分辨率（不硬编码 1920x1080）。
    # 原点取目标输出在 X 屏幕中的位置而非写死 0,0：多显示器时把窗口先放到
    # 用户选定的那块屏上，随后的 EWMH 全屏才会铺满那块屏（openbox 按窗口
    # 所在输出处理全屏），否则会全屏到 X 屏幕左上角那块屏上。
    if fullscreen and (x is None or y is None or not w or not h):
        from window_manager import wm
        geo = wm.get_display_geometry()
        sw = int(geo.get("width") or 1920)
        sh = int(geo.get("height") or 1080)
        x, y, w, h = int(geo.get("x") or 0), int(geo.get("y") or 0), sw, sh

    # 若已存在同名应用窗口，先关闭再以新参数重启
    if app_name:
        stop_app_by_name(f"web:{app_name}")

    is_firefox = "firefox" in browser.lower()
    # 为每个应用使用独立的 user-data-dir，确保多窗口独立进程，
    # 否则 chromium 的 --new-window 会在已有实例中打开新窗口。
    import hashlib
    safe_name = hashlib.md5(app_name.encode()).hexdigest()[:12]
    user_data_dir = f"/tmp/chromium-profile-{safe_name}"

    cmd = _build_browser_cmd(browser, url, is_firefox, user_data_dir,
                             safe_name, x, y, w, h)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=desktop_env(),
        )
        with _registry_lock:
            pid_app_name[proc.pid] = f"web:{app_name or url}"
            _record_ctime(proc.pid)
        pid_url[proc.pid] = url
        _persist()
        # chromium 会从 profile 恢复窗口几何，启动后由 xdotool 强制纠正到绘制区域
        if x is not None and y is not None and w and h:
            _enforce_geometry(proc.pid, int(x), int(y), int(w), int(h),
                              fullscreen=fullscreen)
        _invalidate_window_cache()
        return proc.pid
    except Exception:
        return None


def get_web_url_by_pid(pid: int) -> Optional[str]:
    """获取 web 应用（浏览器窗口）对应的 URL。

    优先查 pid_url 内存表；Web 服务重启后内存表丢失，
    回退到 psutil cmdline 里查找 http(s) 开头的参数（浏览器 URL 恒为末位参数，
    但不依赖位置，扫全量更稳）。用于 layout save 时按 restore_app 的约定存 URL。
    """
    url = pid_url.get(pid)
    if url:
        return url
    try:
        p = psutil.Process(pid)
        for a in p.cmdline():
            if a.startswith("http://") or a.startswith("https://"):
                return a
    except Exception:
        pass
    return None


def stop_app_by_name(app_name: str) -> bool:
    """按应用名称停止进程（如 web:影视）。"""
    with _registry_lock:
        to_kill = [pid for pid, name in pid_app_name.items() if name == app_name]
    ok = False
    for pid in to_kill:
        ok = stop_by_pid(pid) or ok
    return ok


def restore_app(app_name: str, args: str = "",
                x: Optional[int] = None, y: Optional[int] = None,
                w: Optional[int] = None, h: Optional[int] = None,
                fullscreen: bool = False) -> Optional[int]:
    """恢复已注册的应用进程（支持 web: 前缀的飞牛应用）。

    web: 应用的 args 约定为 URL 本身（api_layout_save 保存时已按此约定写入）；
    布局中记录的窗口几何透传给 launch_web_app，chromium 启动期即按
    --window-position/--window-size 定位，避免窗口先出现在默认位置再被纠正。

    fullscreen 必须一并透传：_enforce_geometry 在 fullscreen=True 时走
    "发一次 EWMH 全屏即结束"分支，若这里丢掉该标记，恢复全屏窗口时
    后台线程会持续用 xdotool 抢几何，与随后 main.py 发的全屏请求互相打架。

    本地程序的几何由调用方在窗口出现后用 set_window_rect 设置，此处忽略。
    """
    if app_name.startswith("web:"):
        url = args.strip()
        return launch_web_app(url, app_name[4:], x=x, y=y, w=w, h=h,
                              fullscreen=fullscreen)
    return start_local_app(app_name, args)


def get_app_name_by_pid(pid: int) -> Optional[str]:
    """优先用注册表；缺失时尝试 psutil 读取 cmdline 作为兜底，
    用于识别不是通过本 Web 服务启动的进程。"""
    if pid in pid_app_name:
        return pid_app_name[pid]
    try:
        p = psutil.Process(pid)
        cmdline = p.cmdline()
        if cmdline:
            return os.path.basename(cmdline[0])
    except Exception:
        pass
    return None


def get_app_args_by_pid(pid: int) -> str:
    """读取进程 cmdline 中除程序名之外的参数，用于布局保存。
    返回以空格拼接的字符串（已 shlex.quote 处理，可被 shlex 正确还原）。
    """
    try:
        p = psutil.Process(pid)
        cmdline = p.cmdline()
        if len(cmdline) > 1:
            return " ".join(shlex.quote(a) for a in cmdline[1:])
    except Exception:
        pass
    return ""


def list_pid_registry() -> List[Dict]:
    """列出注册表内全部 pid 与对应程序名，便于前端展示与排查。"""
    with _registry_lock:
        items = list(pid_app_name.items())
    out = []
    for pid, name in items:
        alive = True
        try:
            psutil.Process(pid).status()
        except Exception:
            alive = False
        out.append({"pid": pid, "app_name": name, "alive": alive})
    return out


def _current_instance_marker() -> str:
    """本容器实例标识：PID 1 的启动时间。

    容器重建后 PID namespace 全新，PID 1 是新 entrypoint，其 create_time
    必然变化；同一容器内 Web 重启时它保持不变——正好对应"注册表是否还有
    效"的判定边界。取不到（平台限制）时返回空串，调用方退化为仅 ctime 校验。
    """
    try:
        return f"{psutil.Process(1).create_time():.3f}"
    except Exception:
        return ""


def reconcile_pids() -> int:
    """启动时清理注册表，返回剩余可信条目数。

    两道身份校验，解决"pid 存活但已不是原来那个进程"：
    1. 容器实例标记：/data 跨容器重建保留、PID namespace 却重置。
       标记对不上时注册表里的 pid 全部来自上一个容器实例，整体丢弃
       （旧浏览器 pid 被新 namespace 的 Xorg/openbox 复用的真实事故路径）；
    2. create_time：同一容器内 Web 重启期间 pid 被回收复用，
       注册的 create_time 与现进程不一致即丢弃。无登记记录的旧条目
       （pids.ctime.json 缺失，例如从旧版本升级）退化为仅存活判定。
    """
    marker = _current_instance_marker()
    previous = read_instance_marker()
    with _registry_lock:
        if marker and previous is not None and previous != marker:
            pid_app_name.clear()
            pid_ctime.clear()
            _persist()
        if marker:
            write_instance_marker(marker)

        alive: Dict[int, str] = {}
        alive_ctime: Dict[int, float] = {}
        for pid, name in list(pid_app_name.items()):
            try:
                proc = psutil.Process(pid)
                proc.status()
                ctime = proc.create_time()
            except Exception:
                continue
            recorded = pid_ctime.get(pid)
            if recorded is not None and recorded > 0 and \
                    abs(recorded - ctime) > 1.0:
                continue
            alive[pid] = name
            alive_ctime[pid] = ctime
        pid_app_name.clear()
        pid_app_name.update(alive)
        pid_ctime.clear()
        pid_ctime.update(alive_ctime)
    _persist()
    return len(pid_app_name)
