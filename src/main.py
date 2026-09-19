from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from urllib.parse import urlparse
import asyncio
import hmac
import os
import re
import shlex
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import access_control as ac
from config import (
    load_settings, save_settings, load_layout, save_layout,
    load_profiles, save_profiles, read_logs, log_line, SCREENSHOT_PATH,
)
from app_runner import (
    start_local_app, stop_by_pid, stop_all, restore_app, launch_web_app,
    get_app_name_by_pid, get_app_args_by_pid, get_web_url_by_pid,
    list_pid_registry, reconcile_pids, probe_app_alive,
    stop_refused_reason,
    pid_url,
)
from app_scanner import (
    scan_installed_apps, docker_available, host_apps_mounted,
    set_self_restart_policy, RESTART_POLICY_ON, RESTART_POLICY_OFF,
)
from window_manager import wm
from audio_manager import am
from display_monitor import build_monitor

# 轮询窗口出现的最长等待时间与单次间隔
WINDOW_WAIT_TIMEOUT = 10.0
WINDOW_POLL_INTERVAL = 0.5
# 窗口最小可用尺寸（后端兜底钳制，与前端 MIN_W_PX/MIN_H_PX 取同一量级）
MIN_WINDOW_PX = 120

# 手动启动原生程序的入参上限（纵深防御：subprocess 虽是 list 形式无注入，
# 仍要挡住超长参数/控制字符/异常 token 数量造成的资源消耗）
LOCAL_APP_NAME_MAX = 255
LOCAL_APP_ARGS_MAX = 2000
LOCAL_APP_ARGS_MAX_TOKENS = 64
WEB_APP_NAME_MAX = 100
WEB_URL_MAX = 2048

# 布局恢复串行化：容器启动时的自动恢复与用户点【应用已保存布局】可能撞车，
# 并发拉起同一个应用会互相顶掉窗口（后启动的进程把先启动的窗口挤掉）。
# 布局恢复整段跑在 worker 线程（大量阻塞 subprocess），用线程锁。
_layout_lock = threading.Lock()

# 显示输出热插拔守护（进程内后台线程）。
# 负责"先开 NAS 后插显示器""运行中换接口""分辨率/旋转变化"这三类场景的
# 自动修复。日志复用 log_line，事件缓冲供 /api/status 展示。
_display_monitor = build_monitor(wm, logger=log_line)

# 定时截图循环的唤醒粒度：每秒醒一次检查是否到点，这样用户改间隔后
# 最多 1 秒生效，而不是要等完当前的长睡眠
_SHOT_TICK = 1.0
_screenshot_stop: Optional[asyncio.Event] = None
_screenshot_task: Optional[asyncio.Task] = None


async def _screenshot_loop(stop_event: asyncio.Event):
    """定时截图后台循环（间隔由 settings.screenshot_interval 控制）。

    0 = 关闭；合法范围 10~3600 秒。每次到点在 executor 里跑 scrot
    （subprocess 是阻塞调用，不能直接堵事件循环）。单张失败只记日志，
    绝不能让后台任务挂掉——否则用户关掉再开定时截图也不会恢复。
    """
    elapsed = 0.0
    last_interval = -1
    while not stop_event.is_set():
        try:
            interval = int(load_settings().get("screenshot_interval") or 0)
        except Exception:
            interval = 0
        # 间隔被改小/从关闭切到开启：立刻重置计时，避免还要等完旧周期
        if interval != last_interval:
            elapsed = 0.0
            last_interval = interval
        if interval >= 10:
            elapsed += _SHOT_TICK
            if elapsed >= interval:
                elapsed = 0.0
                try:
                    path = await asyncio.get_event_loop().run_in_executor(
                        None, wm.take_timed_screenshot)
                    if path:
                        log_line(f"定时截图已保存：{os.path.basename(path)}")
                    else:
                        log_line("定时截图失败（scrot/Xorg/输出未就绪），"
                                 "下个周期重试", "WARN")
                except Exception as exc:  # noqa: BLE001
                    log_line(f"定时截图异常：{type(exc).__name__}: {exc}",
                             "WARN")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_SHOT_TICK)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期（代替已废弃的 @app.on_event）。

    Starlette/FastAPI 新版本已移除 on_event，用 lifespan 才能在未来版本上
    继续可用。启动阶段：登记目标显示器、拉起显示守护、记录输出探测结果、
    异步恢复布局、启动定时截图循环；关闭阶段：停掉守护线程与截图循环。
    """
    global _screenshot_stop, _screenshot_task
    await _on_startup()
    _screenshot_stop = asyncio.Event()
    _screenshot_task = asyncio.create_task(_screenshot_loop(_screenshot_stop))
    try:
        yield
    finally:
        _display_monitor.stop()
        if _screenshot_stop:
            _screenshot_stop.set()


app = FastAPI(title="window-composer", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------- CSRF 防护
# 本面板没有独立账号体系（靠 NAS 局域网/飞牛桌面入口保护），但所有状态变更
# 都是 POST，浏览器跨站表单/fetch 可在用户不知情时打到 host 网络下的面板。
# 启发式：请求带 Origin/Referer 时，其主机:端口必须与服务自身 Host 一致
# （飞牛桌面 iframe 内嵌 http://NAS:8181 时二者天然一致）；curl/脚本等
# 非浏览器客户端不发这两个头，照常放行。
def _origin_allowed(origin_or_referer: str, host: str) -> bool:
    try:
        parsed = urlparse(origin_or_referer)
        netloc = parsed.netloc
        return bool(netloc) and netloc == (host or "").split(",")[0].strip()
    except Exception:
        return False


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and not _origin_allowed(source, request.headers.get("host", "")):
            log_line(f"拒绝跨站请求：{request.method} {request.url.path} "
                     f"来源 {source} 与 Host {request.headers.get('host')} 不一致",
                     "WARN")
            return JSONResponse(
                {"ok": False, "msg": "跨站请求被拒绝（Origin/Referer 与服务地址不一致）"},
                status_code=403)
    return await call_next(request)


# ---------------------------------------------------------------- 访问令牌
# host 网络 + privileged 下，面板绝不能对整个局域网无门槛开放。
# 策略见 access_control 模块文档：飞牛桌面入口/同源/回环免令牌，其余要令牌。
def _client_host(request: Request) -> str:
    return request.client.host if request.client else ""


def _has_control_chars(text: str, allow_tab: bool = False) -> bool:
    return any(ord(ch) < 32 and not (allow_tab and ch == "\t")
               for ch in text)


def validate_local_app_name(app_name: str) -> Optional[str]:
    """校验手动启动的程序名。返回错误信息；合法返回 None。

    只允许"裸命令名"或"不含 .. 的绝对路径"：
    - 拒绝空串/控制字符/空白分隔（带参数请走 args）；
    - 长度 ≤ 255（单条 Linux 文件名上限量级）。
    subprocess 以 list 形式执行、没有 shell，不存在命令注入；这里收紧的是
    异常输入与资源消耗，并让报错信息明确。
    """
    name = (app_name or "").strip()
    if not name:
        return "程序名不能为空"
    if len(name) > LOCAL_APP_NAME_MAX:
        return f"程序名不能超过 {LOCAL_APP_NAME_MAX} 个字符"
    if _has_control_chars(name) or any(ch.isspace() for ch in name):
        return "程序名包含非法字符（参数请填在 args 中）"
    if name.startswith("/"):
        parts = name.split("/")
        if ".." in parts or not all(parts[1:]) or \
                not re.fullmatch(r"[A-Za-z0-9._-]+", parts[-1]):
            return "程序路径不合法（不允许 .. 或特殊字符）"
        if any(not re.fullmatch(r"[A-Za-z0-9._-]*", p) for p in parts[1:-1]):
            return "程序路径包含非法字符"
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return "程序名包含非法字符（仅允许字母、数字、点、下划线、连字符）"
    return None


def validate_local_app_args(args: str) -> Optional[str]:
    """校验手动启动参数：长度/token 数/控制字符/shlex 可解析。"""
    args = args or ""
    if len(args) > LOCAL_APP_ARGS_MAX:
        return f"参数过长（上限 {LOCAL_APP_ARGS_MAX} 字符）"
    if _has_control_chars(args, allow_tab=True):
        return "参数包含非法控制字符"
    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        return f"参数引号无法解析：{exc}"
    if len(tokens) > LOCAL_APP_ARGS_MAX_TOKENS:
        return f"参数个数过多（上限 {LOCAL_APP_ARGS_MAX_TOKENS} 个）"
    if any(len(t) > LOCAL_APP_ARGS_MAX for t in tokens):
        return "单个参数过长"
    return None


def validate_web_target(app_name: str, url: str) -> Optional[str]:
    """校验"显示飞牛应用"的展示名与 URL。"""
    name = (app_name or "").strip()
    if not name:
        return "应用名称不能为空"
    if len(name) > WEB_APP_NAME_MAX or _has_control_chars(name):
        return f"应用名称不合法（长度 ≤ {WEB_APP_NAME_MAX}，无控制字符）"
    if not url or len(url) > WEB_URL_MAX:
        return "URL 为空或过长"
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "URL 不合法（需 http/https 开头）"
    parsed = urlparse(url)
    if not parsed.netloc or _has_control_chars(url):
        return "URL 不合法（缺少主机名或含控制字符）"
    return None


@app.middleware("http")
async def access_token_guard(request: Request, call_next):
    cfg = load_settings()
    token = ac.ensure_access_token(cfg, save_settings)
    path = request.url.path
    if not ac.is_public_path(path):
        allowed, reason = ac.is_authorized(
            _client_host(request), request.headers, token,
            bool(cfg.get("require_token", True)))
        if not allowed:
            # 查询参数里携带的令牌（curl/脚本/一次性链接）补判一次
            qtok = request.query_params.get(ac.TOKEN_QUERY)
            if qtok and hmac.compare_digest(qtok, token):
                allowed, reason = True, "token-query"
        if not allowed:
            # 浏览器直接访问首页：给令牌输入页而不是一段 JSON
            is_html_nav = request.method == "GET" and path == "/" and \
                "text/html" in (request.headers.get("accept") or "").lower()
            log_line(f"拒绝未授权访问：{request.method} {path} "
                     f"（{reason}）", "WARN")
            if is_html_nav:
                return HTMLResponse(ac.LOGIN_PAGE, status_code=401)
            return JSONResponse(
                {"ok": False,
                 "msg": "需要访问令牌：请从飞牛桌面打开面板，"
                        "或携带访问令牌（?wc_at=令牌 / X-Access-Token 头）"},
                status_code=401)
    response = await call_next(request)
    # 用 ?wc_at= 进入的浏览器导航：顺手种下 Cookie，后续轮询/操作不再带参
    if request.query_params.get(ac.TOKEN_QUERY) and \
            hmac.compare_digest(request.query_params[ac.TOKEN_QUERY], token) \
            and not request.cookies.get(ac.TOKEN_COOKIE):
        response.set_cookie(
            ac.TOKEN_COOKIE, token, httponly=True, samesite="lax",
            max_age=365 * 24 * 3600, path="/")
    return response


# ---------------------------------------------------------------- 坐标换算
# 布局/画布坐标系以"目标输出"（用户选定的那块屏）左上角为原点；
# 窗口系统（xdotool/xwininfo/xrandr）用的是整个 X 屏幕的绝对坐标。
# 单显示器时两者完全一致（原点为 0,0），双屏时才有偏移。
# 所有对外接口统一暴露局部坐标，避免前端画布要处理多屏偏移。
def _output_origin():
    """目标输出在 X 屏幕中的原点 (x, y)。"""
    geo = wm.get_display_geometry()
    return int(geo.get("x") or 0), int(geo.get("y") or 0)


def _to_local(x: int, y: int):
    """屏幕绝对坐标 → 目标输出局部坐标。"""
    ox, oy = _output_origin()
    return int(x) - ox, int(y) - oy


def _to_screen(x: int, y: int):
    """目标输出局部坐标 → 屏幕绝对坐标。"""
    ox, oy = _output_origin()
    return int(x) + ox, int(y) + oy


def _wait_window_appear(pid: int) -> bool:
    """轮询窗口列表，直到目标 pid 出现或超时（同步版，只在 worker 线程跑）。

    xdotool/xwininfo 是阻塞 subprocess，布局恢复整段已通过 asyncio.to_thread
    下沉到线程池，这里直接 time.sleep 即可——绝不能在事件循环线程里跑。
    """
    deadline = time.time() + WINDOW_WAIT_TIMEOUT
    while time.time() < deadline:
        time.sleep(WINDOW_POLL_INTERVAL)
        for win in wm.list_windows():
            if win["pid"] == pid:
                return True
    return False


def _find_window_for_layout_item(item: dict, wins: list):
    """在屏幕上找布局项对应的"已经开着"的窗口，找不到返回 None。

    优先按 app_name 精确匹配（"web:影视" / "xedit"）；
    web 应用再加一条 URL 兜底——Web 服务重启后注册表里的名字可能对不上，
    但只要窗口还开着，就能从进程 cmdline 认出是同一个应用。
    """
    name = item.get("app_name") or ""
    for w in wins:
        if w.get("app_name") == name:
            return w
    if name.startswith("web:"):
        url = (item.get("args") or "").strip()
        if url:
            for w in wins:
                if get_web_url_by_pid(w["pid"]) == url:
                    return w
    return None


def apply_layout(layout_override: Optional[list] = None) -> dict:
    """按已保存布局把窗口摆回原位（容器启动恢复与网页手动恢复共用，同步）。

    本函数全程调用阻塞 subprocess（xrandr/xdotool/浏览器拉起，最坏十几秒），
    只允许在 worker 线程执行：事件循环线程直接调用会把面板所有轮询接口堵死。
    协程/接口请走 apply_layout_threadsafe()。

    layout_override 不为 None 时直接使用传入布局（布局方案"应用方案"走这条
    路径），否则读 layout.json——即当前布局，也是开机自动恢复的那一份。
    应用方案只临时摆放窗口，不覆盖 layout.json，重启后语义不变。

    逐项语义：
    - 屏幕上已有该应用的窗口 → 只按布局重新定位，不重启应用
      （用户往往只想"摆整齐"；误重启会打断正在播放的视频/正在操作的表单）
    - 没有 → 拉起应用 → 等窗口出现 → 按布局定位（全屏项发 EWMH 全屏）

    布局里的几何可能来自旋转前/改分辨率前的旧屏幕，先按当前屏幕钳制，
    否则窗口会被摆到屏幕外——用户在 HDMI 上既看不到也点不到。
    返回统计与逐项明细，供接口返回与日志记录使用。
    """
    layout = load_layout() if layout_override is None else layout_override
    if not layout:
        return {"total": 0, "started": 0, "repositioned": 0, "failed": 0,
                "details": []}

    geo = wm.get_display_geometry()
    sw = int(geo.get("width") or 1920)
    sh = int(geo.get("height") or 1080)
    # 目标输出在 X 屏幕中的原点：布局坐标是"相对目标输出"的，
    # 双屏时必须平移，否则窗口会被摆到整个 X 屏幕的左上角（另一块屏上）
    ox = int(geo.get("x") or 0)
    oy = int(geo.get("y") or 0)
    sink_id = load_settings().get("default_audio_sink")
    # 一次性快照：逐项重新 list_windows 会为每个窗口反复起 xdotool/xwininfo/xprop
    wins = wm.list_windows()

    started = repositioned = failed = 0
    details = []
    for item in layout:
        app_name = item.get("app_name") or ""
        args = item.get("args", "")
        fullscreen = bool(item.get("fullscreen", False))
        # 先钳进当前屏幕：布局里的几何可能来自旋转前/改分辨率前的旧屏幕，
        # 直接照搬会把窗口摆到屏幕外（用户在 HDMI 上既看不到也点不到）
        lx, ly, w, h = wm.clamp_rect(item.get("x"), item.get("y"),
                                     item.get("w"), item.get("h"),
                                     sw, sh, MIN_WINDOW_PX)
        if fullscreen:
            lx, ly, w, h = 0, 0, sw, sh
        # 局部坐标 → 屏幕绝对坐标（单屏时是恒等变换）
        x, y = lx + ox, ly + oy

        existing = _find_window_for_layout_item(item, wins)
        if existing:
            pid = existing["pid"]
            if not existing.get("visible", True):
                # 窗口处于最小化状态：先恢复并 raise，否则后面的几何操作
                # 打在一个不可见的窗口上，用户看不到任何效果
                wm.set_minimized(pid, False)
            # 非全屏项若窗口当前是全屏，set_window_rect 会先发
            # remove,fullscreen 再移动（openbox 会忽略全屏窗口的几何请求）；
            # 全屏项直接发 add,fullscreen（幂等）
            ok = (wm.set_fullscreen(pid, True) if fullscreen
                  else wm.set_window_rect(pid, x, y, w, h))
            repositioned += 1 if ok else 0
            failed += 0 if ok else 1
            details.append({"app_name": app_name, "action": "reposition",
                            "ok": ok, "pid": pid})
            continue

        pid = restore_app(app_name, args, x=x, y=y, w=w, h=h,
                          fullscreen=fullscreen)
        if pid is None:
            failed += 1
            details.append({"app_name": app_name, "action": "start",
                            "ok": False, "pid": None,
                            "msg": "启动失败（程序不存在或浏览器缺失）"})
            continue
        found = _wait_window_appear(pid)
        if found:
            if fullscreen:
                # _enforce_geometry 可能已发过全屏；重复 add,fullscreen 幂等
                wm.set_fullscreen(pid, True)
            else:
                wm.set_window_rect(pid, x, y, w, h)
        if sink_id:
            am.route_process_audio(pid, sink_id)
        started += 1
        details.append({"app_name": app_name, "action": "start", "ok": True,
                        "pid": pid, "window_found": found})

    return {"total": len(layout), "started": started,
            "repositioned": repositioned, "failed": failed,
            "details": details}


def apply_layout_threadsafe(layout_override: Optional[list] = None) -> dict:
    """串行化执行 apply_layout（同步阻塞，调用方须已在 worker 线程）。

    容器启动恢复、手动恢复、应用方案共用一把线程锁，杜绝并发拉起同一应用
    互相顶掉窗口。
    """
    with _layout_lock:
        return apply_layout(layout_override)


async def restore_layout_task():
    """容器启动后异步恢复布局（启动阶段：注册表已清理）。"""
    await asyncio.to_thread(reconcile_pids)

    cfg = load_settings()
    sink_id = cfg.get("default_audio_sink")
    if sink_id:
        # 提前切 default sink，影响随后启动的所有新 stream
        await asyncio.to_thread(am.set_default_sink, sink_id)

    res = await asyncio.to_thread(apply_layout_threadsafe)

    if res["total"] == 0:
        log_line("Web 服务启动，无已保存布局，跳过自动恢复")
        return
    level = "WARN" if res["failed"] else "INFO"
    log_line(f"容器启动自动恢复布局：共 {res['total']} 项，"
             f"拉起 {res['started']}，重定位 {res['repositioned']}，"
             f"失败 {res['failed']}", level)


async def _on_startup():
    """Web 服务启动钩子（由 lifespan 调用）。"""
    cfg = load_settings()
    # 首次启动生成访问令牌并落盘（之后复用，重启不变）
    token = ac.ensure_access_token(cfg, save_settings)
    if cfg.get("require_token", True):
        log_line("访问令牌保护已开启：飞牛桌面入口/同源/本机回环免令牌，"
                 "其他访问需令牌（面板内可查看/重置）")
    # 多接口/多屏场景：优先使用用户在面板里选定的目标显示器
    if cfg.get("target_output"):
        wm.set_target_output(cfg["target_output"])

    # 显示输出守护：自动点亮"已连接但未激活"的接口（HDMI/DP/USB-C/VGA/DVI
    # 一视同仁），并在分辨率/旋转/换屏后把越界窗口拉回可见区
    if _display_monitor.start():
        log_line(f"显示输出守护已启动（每 {_display_monitor.poll_interval:g} 秒检查一次）")

    info = wm.get_output_info()
    listed = "，".join(
        f"{o['name']}（{o['type_label']}，"
        f"{'已连接' if o['connected'] else '未接显示器'}）"
        for o in info["outputs"]
    ) or "无"
    log_line(f"显示输出探测：后端 {info['backend']}，"
             f"目标 {info['output'] or '无'}"
             f"（{info['output_label'] or '—'}）"
             f"{info['width']}×{info['height']}；全部接口：{listed}")
    if info["backend"] == "headless":
        log_line("当前没有任何已连接的显示输出：窗口只会渲染到虚拟屏幕。"
                 "接上显示器后会自动点亮并出画面，无需重启容器。", "WARN")

    log_line(f"Web 服务启动（PID {os.getpid()}），开始异步恢复布局")
    asyncio.create_task(restore_layout_task())


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """兜底异常处理器：未捕获的接口异常写进运行日志。

    没有它时接口 500 只在容器 stdout 里一闪而过，而网页【查看运行日志】
    读的是 /data/app.log，用户永远看不到任何线索。
    """
    log_line(f"接口异常 {request.method} {request.url.path}："
             f"{type(exc).__name__}: {exc}", "ERROR")
    return JSONResponse(
        {"ok": False, "msg": f"服务器内部错误：{type(exc).__name__}"},
        status_code=500,
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    settings = load_settings()
    ac.ensure_access_token(settings, save_settings)
    windows = wm.list_windows()
    sinks = am.list_sinks()
    # Starlette 0.30+ 改了 TemplateResponse 签名：
    #   旧：TemplateResponse(name, {"request": request, ...})
    #   新：TemplateResponse(request, name, {...})
    # 旧写法在新版 Starlette 会抛 TypeError: unhashable type: 'dict'
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "windows": windows,
            "sinks": sinks,
            "token_is_default": ac.token_is_default(settings),
        },
    )


def _public_outputs(outs: list) -> list:
    """输出列表的对外表示（模式列表截断，避免响应体过大）。"""
    result = []
    for o in outs:
        item = dict(o)
        item["modes"] = (o.get("modes") or [])[:16]
        result.append(item)
    return result


@app.get("/api/status")
def api_status():
    cfg = load_settings()
    # 尺寸取目标输出（用户选定那块屏）的实测值：由显示器 EDID / xrandr 当前
    # 模式决定（竖屏如 1536x2048），旋转后宽高互换。读不到时回退 settings。
    geo = wm.get_display_geometry()
    width = geo.get("width") or cfg.get("display_width", 1920)
    height = geo.get("height") or cfg.get("display_height", 1080)
    # backend / output 由 xrandr 实际输出探测（曾错误地取 WESTON_BACKEND
    # 环境变量，而 entrypoint 从不设置它 → 面板永远显示 Headless）
    out = wm.get_output_info()
    return {
        "ok": True,
        "msg": "running",
        "web_port": cfg.get("web_port"),
        "bg_color": cfg.get("bg_color"),
        "auto_start": cfg.get("auto_start"),
        "default_audio_sink": cfg.get("default_audio_sink"),
        "backend": out["backend"],
        "output_name": out["output"],
        "output_type": out["output_type"],
        "output_label": out["output_label"],
        "output_x": int(geo.get("x") or 0),
        "output_y": int(geo.get("y") or 0),
        "target_output": cfg.get("target_output") or "",
        "outputs": _public_outputs(out["outputs"]),
        "drm_card": out["card"],
        "display_events": _display_monitor.events()[-10:],
        "display_monitor": _display_monitor.status(),
        "display_width": width,
        "display_height": height,
        "rotation": wm.get_rotation(),
        "screenshot_interval": int(cfg.get("screenshot_interval") or 0),
    }


@app.get("/api/display/outputs")
def api_display_outputs():
    """列出全部显示输出（含未连接的接口），供面板选择目标显示器。

    这是"通用接口"能力的对外体现：HDMI / DisplayPort（含 USB-C DP Alt Mode
    与雷电扩展坞）/ VGA / DVI / eDP / DSI 都由同一份 xrandr 解析给出，
    不区分接口类型，也不需要用户在配置里声明自己插的是哪个口。
    """
    out = wm.get_output_info()
    return {
        "ok": True,
        "backend": out["backend"],
        "active": out["output"],
        "target": load_settings().get("target_output") or "",
        "outputs": _public_outputs(out["outputs"]),
        "drm_card": out["card"],
    }


@app.post("/api/display/refresh")
async def api_display_refresh():
    """立即重新探测输出，并尝试点亮"已连接但未激活"的接口。

    自动守护每几秒会做同样的事；本接口用于"刚插上显示器想立刻看到画面"，
    不必等下一个轮询周期。守护被关闭（WC_DISPLAY_POLL_INTERVAL=0）时
    这是唯一的手动触发入口。
    """
    activated = await asyncio.to_thread(wm.auto_enable_connected)
    # 会话发生了可见变化：把越界窗口拉回可见区
    reflowed = await asyncio.to_thread(wm.clamp_windows_to_screen) \
        if activated else 0
    info = await asyncio.to_thread(wm.get_output_info)
    if activated or reflowed:
        log_line(f"手动刷新显示输出：新启用 {activated or '无'}"
                 f"，回屏 {reflowed} 个窗口")
    # xrandr 生效有短暂延迟，读一次最新的尺寸给前端
    await asyncio.sleep(0.3)
    geo = await asyncio.to_thread(wm.get_display_geometry)
    return {
        "ok": True,
        "activated": activated,
        "reflowed": reflowed,
        "backend": info["backend"],
        "active": info["output"],
        "width": geo.get("width"),
        "height": geo.get("height"),
        "outputs": _public_outputs(info["outputs"]),
    }


@app.post("/api/display/set_output")
async def api_display_set_output(name: str):
    """选择目标显示器（多接口/多屏时布局基准屏）。

    只登记选择并持久化，不改动 X 的 primary；若该输出已连接但未激活会先
    尝试 --auto 点亮。切换后把画布坐标原点换到新屏，并记录日志，
    避免"切了显示器但不知道窗口会去哪"。
    """
    name = (name or "").strip()
    outs = await asyncio.to_thread(wm.get_outputs)
    target = next((o for o in outs if o["name"] == name), None)
    if target is None:
        return {"ok": False, "msg": f"未找到输出 {name}，请先刷新列表",
                "outputs": _public_outputs(outs)}
    if not target.get("connected"):
        return {"ok": False,
                "msg": f"{name} 当前未接显示器（{target['type_label']}），"
                       "接上后它会自动被点亮",
                "outputs": _public_outputs(outs)}
    activated = False
    if not target.get("enabled"):
        activated = (await asyncio.to_thread(wm.enable_output, name)) \
            .get("ok", False)

    cfg = load_settings()
    cfg["target_output"] = name
    save_settings(cfg)
    wm.set_target_output(name)

    # 屏幕换了：原屏上的窗口在新坐标系里可能越界
    await asyncio.sleep(0.3)
    reflowed = await asyncio.to_thread(wm.clamp_windows_to_screen)
    geo = await asyncio.to_thread(wm.get_display_geometry)
    log_line(f"切换目标显示器为 {name}（{target['type_label']}）"
             f"{'，已自动启用' if activated else ''}"
             f"，分辨率 {geo.get('width')}×{geo.get('height')}"
             f"，回屏 {reflowed} 个窗口")
    return {
        "ok": True,
        "msg": f"目标显示器已切换为 {name}（{target['type_label']}）"
               f" {geo.get('width')}×{geo.get('height')}",
        "active": name,
        "activated": activated,
        "reflowed": reflowed,
        "width": geo.get("width"),
        "height": geo.get("height"),
    }


@app.post("/api/display/rotate")
async def api_display_rotate(degrees: int):
    """旋转目标显示器的画面方向（0/90/180/270）。

    通过 xrandr --rotate 对目标输出做硬件旋转，旋转后屏幕宽高互换
    （90°/270°），/api/status 的 display_width/height 会自动更新。
    多屏时只旋转用户选定的那块屏（旋转哪块屏由 /api/display/set_output 决定）。
    """
    if degrees not in (0, 90, 180, 270):
        return {"ok": False, "msg": "degrees 必须为 0/90/180/270"}
    ok = await asyncio.to_thread(wm.set_rotation, degrees)
    if not ok:
        return {"ok": False,
                "msg": "xrandr 旋转失败：没有已连接的显示输出，"
                       "或该输出的旋转不受驱动支持"}
    log_line(f"旋转显示输出 {degrees}°")
    # xrandr 生效有短暂延迟，等 0.4s 后再读真实几何
    await asyncio.sleep(0.4)
    geo = await asyncio.to_thread(wm.get_display_geometry)
    # 90°/270° 后宽高互换，原先贴着旧边界的窗口会跑到屏幕外（用户完全看不到），
    # 这里把越界窗口钳回可见区域
    fixed = await asyncio.to_thread(wm.clamp_windows_to_screen)
    return {
        "ok": True,
        "degrees": degrees,
        "width": geo.get("width"),
        "height": geo.get("height"),
        "reflowed": fixed,
    }


@app.get("/api/windows")
def api_windows():
    """当前窗口列表。

    坐标以目标输出左上角为原点（单显示器时等于屏幕绝对坐标，双屏时减去
    目标输出原点），这样前端画布（尺寸 = 目标输出尺寸）能直接按比例绘制。
    screen_x/screen_y 保留屏幕绝对坐标，便于排查"窗口跑到另一块屏上了"。
    """
    ox, oy = _output_origin()
    windows = []
    for w in wm.list_windows():
        item = dict(w)
        item["screen_x"] = int(w["x"])
        item["screen_y"] = int(w["y"])
        item["x"] = int(w["x"]) - ox
        item["y"] = int(w["y"]) - oy
        windows.append(item)
    return {"windows": windows, "origin": {"x": ox, "y": oy}}


@app.post("/api/window/setrect")
def api_window_setrect(pid: int, x: int, y: int, w: int, h: int):
    """设置窗口位置和大小（入参为目标输出局部坐标）。

    Xorg + openbox 环境下，xdotool windowmove/windowsize 均生效，
    对 web 应用和原生 X11 应用统一用 xdotool 控制，无需重启。

    后端兜底钳制：前端画布已按分辨率限制，但直接调 API（curl/脚本）
    可能传入越界值，这里保证窗口始终落在目标输出内且不小于最小可用尺寸。
    钳制在局部坐标系里做，最后再平移成屏幕绝对坐标交给 xdotool。
    """
    geo = wm.get_display_geometry()
    sw, sh = int(geo.get("width") or 1920), int(geo.get("height") or 1080)
    w = max(MIN_WINDOW_PX, min(int(w), sw))
    h = max(MIN_WINDOW_PX, min(int(h), sh))
    x = max(0, min(int(x), sw - w))
    y = max(0, min(int(y), sh - h))
    sx, sy = _to_screen(x, y)
    ok = wm.set_window_rect(pid, sx, sy, w, h)
    return {"ok": ok, "method": "xdotool", "x": x, "y": y, "w": w, "h": h,
            "screen_x": sx, "screen_y": sy}


@app.post("/api/window/fullscreen")
def api_window_fullscreen(pid: int, fullscreen: bool = True):
    """设置窗口全屏状态（WM 侧 EWMH，openbox 下为真全屏）。

    实现交给 wm.set_fullscreen（wmctrl 发 _NET_WM_STATE ClientMessage），
    对 web 应用与原生 X11 应用一视同仁，不区分渲染技术栈。
    """
    ok = wm.set_fullscreen(pid, fullscreen)
    if not ok:
        log_line(f"设置窗口全屏失败：pid={pid} fullscreen={fullscreen}", "WARN")
    return {"ok": ok}


@app.post("/api/window/minimize")
def api_window_minimize(pid: int, minimized: bool = True):
    """最小化 / 恢复窗口。

    最小化后窗口从 --onlyvisible 列表消失，但仍在 pid 注册表中，
    /api/windows 会以 visible=False 补回，前端窗口列表据此显示"恢复"入口。
    """
    ok = wm.set_minimized(pid, minimized)
    if not ok:
        log_line(f"窗口最小化/恢复失败：pid={pid} minimized={minimized}", "WARN")
    return {"ok": ok, "minimized": minimized if ok else None}


@app.post("/api/window/activate")
def api_window_activate(pid: int):
    """激活窗口：取消最小化并提到最前（任务栏式"切换到该窗口"）。"""
    ok = wm.set_minimized(pid, False)
    return {"ok": ok}


@app.post("/api/window/set_above")
def api_window_set_above(pid: int, above: bool = True):
    """窗口置顶 / 取消置顶（EWMH _NET_WM_STATE_ABOVE）。"""
    ok = wm.set_above(pid, above)
    if not ok:
        log_line(f"窗口置顶切换失败：pid={pid} above={above}", "WARN")
    return {"ok": ok, "above": above if ok else None}


@app.post("/api/window/close")
def api_window_close(pid: int):
    reason = stop_refused_reason(pid)
    if reason:
        log_line(f"拒绝关闭窗口请求：pid={pid}（{reason}）", "WARN")
        return {"ok": False, "msg": reason}
    ok = stop_by_pid(pid)
    return {"ok": ok}


@app.get("/api/apps/installed")
def api_apps_installed(refresh: int = 0):
    """扫描已安装应用：飞牛 NAS 应用 + 宿主 Docker 容器。

    飞牛应用数据来自挂载的宿主 /var/apps（含 manifest 与 target/ui/config），
    运行状态由宿主 /proc 判定；Docker 容器经挂载的 /var/run/docker.sock 走
    Docker Engine API 获取（容器内不需要装 docker CLI）。
    只提供应用元信息，不涉及启动/显示。
    socket 未挂载时 docker_available=False、docker_count=0，Docker 部分静默跳过。
    refresh=1 时强制重新扫描（安装了新应用或新容器后使用）。
    """
    apps = scan_installed_apps(force_refresh=bool(refresh))
    return {"apps": apps, "count": len(apps),
            "running_count": sum(1 for a in apps if a["running"]),
            "docker_available": docker_available(),
            "docker_count": sum(1 for a in apps if a.get("source") == "docker"),
            "host_apps_mounted": host_apps_mounted()}


@app.post("/api/app/start")
def api_app_start(app_name: str, args: str = ""):
    """启动容器内原生 GUI 程序（手动输入命令用，非飞牛应用）。"""
    err = validate_local_app_name(app_name) or validate_local_app_args(args)
    if err:
        return {"ok": False, "msg": err, "pid": None}
    pid = start_local_app(app_name.strip(), args)
    if pid is None:
        log_line(f"启动程序失败：{app_name} {args}（无法创建进程）", "WARN")
        return {"ok": False, "msg": f"启动 {app_name} 失败，请检查程序是否存在", "pid": None}
    # 存活校验：程序不存在/非 X11 程序/参数错误时会立即退出，
    # 若不校验，前端只会显示"已启动"，用户在 HDMI 上找不到任何窗口。
    if not probe_app_alive(pid, 0.6):
        exited_pid = pid
        stop_by_pid(pid)  # 顺带清理注册表里的脏 pid
        log_line(f"启动程序 {app_name} 后立即退出（PID {exited_pid}），"
                 "已清理注册表，常见原因：程序不存在/不支持 X11/参数错误", "WARN")
        return {
            "ok": False, "pid": None, "exited_pid": exited_pid,
            "msg": f"{app_name} 启动后立即退出（PID {exited_pid} 已回收）："
                   "请确认程序存在、支持 X11 显示、启动参数正确",
        }
    log_line(f"启动程序：{app_name} {args}（PID {pid}）")
    return {"ok": True, "pid": pid, "app_name": app_name, "args": args}


@app.post("/api/app/launch_web")
def api_app_launch_web(
    app_name: str, url: str,
    x: Optional[int] = None, y: Optional[int] = None,
    w: Optional[int] = None, h: Optional[int] = None,
    fullscreen: bool = False,
):
    """在目标显示器上显示飞牛应用的 Web 界面。

    自动检测容器内可用浏览器（chromium/firefox 等），以普通窗口模式
    打开应用 URL 并渲染到显示器，支持多窗口并存。接口类型无关：
    HDMI / DP / USB-C / VGA / DVI 只要 xrandr 能枚举到就能出画面。
    app_name：应用展示名；url：应用的 Web 访问地址。
    x, y：窗口位置（目标输出局部坐标，与画布坐标系一致）；
    w, h：窗口大小（显示像素）。
    fullscreen=True：不传几何时自动铺满目标输出整屏并置顶（快捷全屏显示）。
    若已存在同名窗口，先关闭再以新位置/大小重启。
    """
    err = validate_web_target(app_name, url)
    if err:
        return {"ok": False, "msg": err, "pid": None}
    # 与 /api/window/setrect 同一套后端兜底：直连接入可能传负坐标/超大尺寸，
    # 浏览器窗口会跑到屏幕外或越过目标输出。给了几何（非整屏全屏快捷路径）
    # 就先在目标输出局部坐标系里钳制，再平移成屏幕绝对坐标。
    if not fullscreen and (x is not None or y is not None
                           or w is not None or h is not None):
        geo = wm.get_display_geometry()
        sw, sh = int(geo.get("width") or 1920), int(geo.get("height") or 1080)
        x, y, w, h = wm.clamp_rect(
            int(x or 0), int(y or 0), int(w or sw), int(h or sh),
            sw, sh, min_px=MIN_WINDOW_PX)
    # 入参是画布坐标系（目标输出左上角为原点），浏览器需要屏幕绝对坐标
    sx = sy = None
    if x is not None and y is not None:
        sx, sy = _to_screen(x, y)
    pid = launch_web_app(url, app_name, x=sx, y=sy, w=w, h=h,
                         fullscreen=fullscreen)
    if pid is None:
        log_line(f"显示飞牛应用失败：{app_name} <{url}>——容器内未检测到浏览器", "ERROR")
        return {"ok": False,
                "msg": "显示失败：容器内未安装浏览器（chromium/firefox），无法显示",
                "pid": None}
    if fullscreen:
        geo_desc = "整屏全屏"
    elif x is None and w is None:
        geo_desc = "默认位置"
    else:
        geo_desc = f"({x},{y}) {w}×{h}"
    log_line(f"显示飞牛应用：{app_name} <{url}> PID {pid} 位置 {geo_desc}")
    return {"ok": True, "pid": pid, "app_name": app_name, "url": url,
            "fullscreen": fullscreen}


@app.post("/api/app/stop")
def api_app_stop(pid: int):
    """停止指定应用进程。"""
    reason = stop_refused_reason(pid)
    if reason:
        log_line(f"拒绝停止进程请求：pid={pid}（{reason}）", "WARN")
        return {"ok": False, "msg": reason}
    name = get_app_name_by_pid(pid) or "?"
    ok = stop_by_pid(pid)
    log_line(f"停止应用：{name}（PID {pid}）{'成功' if ok else '失败'}",
             "INFO" if ok else "WARN")
    return {"ok": ok}


@app.post("/api/app/stop_all")
def api_app_stop_all():
    count = stop_all()
    log_line(f"关闭所有应用窗口：共停止 {count} 个进程（Xorg/openbox 保持运行）")
    return {"ok": True, "closed_count": count}


@app.get("/api/pids")
def api_pids():
    """列出 Web 服务记录在册的 pid 与对应程序名（含存活状态）。"""
    return {"pids": list_pid_registry()}


@app.get("/api/audio/sinks")
def api_audio_sinks():
    return {"sinks": am.list_sinks()}


@app.post("/api/audio/set_sink")
def api_audio_set_sink(sink_id: str, migrate_existing: bool = True):
    """切换默认音频输出设备。

    migrate_existing=True（默认）时把当前正在播放的流一并迁移到新设备，
    否则只影响之后新建的流（表现为"切了设备但正在播放的窗口没换声卡"）。
    """
    cfg = load_settings()
    cfg["default_audio_sink"] = sink_id
    save_settings(cfg)
    ok = am.set_default_sink(sink_id)
    moved = am.move_all_sink_inputs(sink_id) if migrate_existing else 0
    if not ok and not moved:
        log_line(f"切换音频输出失败：sink={sink_id}", "WARN")
        return {"ok": False, "msg": "切换失败，请检查设备ID"}
    log_line(f"切换音频输出到 {sink_id}，迁移已有播放流 {moved} 个")
    msg = "音频输出设备已切换"
    if moved:
        msg += f"，已迁移 {moved} 个播放流"
    return {"ok": True, "msg": msg, "moved": moved}


@app.post("/api/audio/set_volume")
def api_audio_set_volume(sink_id: str, volume: int):
    """设置输出设备音量（0~100）。

    前端滑块拖动会高频调用；am 侧写后立刻失效缓存，下一次 sinks 轮询
    拿到的就是新值。越界值在 audio_manager 内钳制，不报错给用户。
    """
    try:
        volume = int(volume)
    except (TypeError, ValueError):
        return {"ok": False, "msg": "音量必须是 0~100 的整数"}
    ok = am.set_sink_volume(sink_id, volume)
    if not ok:
        return {"ok": False, "msg": "设置音量失败，请检查设备ID"}
    return {"ok": True, "sink_id": sink_id,
            "volume": max(0, min(100, volume))}


@app.post("/api/audio/set_mute")
def api_audio_set_mute(sink_id: str, muted: bool):
    """静音 / 取消静音指定输出设备。"""
    ok = am.set_sink_mute(sink_id, bool(muted))
    if not ok:
        return {"ok": False, "msg": "设置静音失败，请检查设备ID"}
    return {"ok": True, "sink_id": sink_id, "muted": bool(muted)}


@app.get("/api/audio/sink_inputs")
def api_audio_sink_inputs():
    """列出当前所有音频播放流（诊断用：看某进程的流落在哪个声卡）。"""
    return {"inputs": am.list_sink_inputs()}


def _build_layout_from_windows() -> list:
    """把当前可见窗口快照成布局数组（当前布局与命名方案共用）。

    坐标统一换算成"目标输出局部坐标"（单显示器时即屏幕绝对坐标），
    与 apply_layout / 画布坐标系保持一致——否则多屏时布局里存的是绝对
    坐标，恢复时又按局部坐标处理，窗口每保存恢复一次就偏移一个屏幕宽度。
    已最小化 / 几何读不到（w 或 h 为 0）的窗口跳过：把 0×0 存进布局，
    恢复时会被钳制成最小尺寸的小窗口，纯属垃圾数据。
    """
    wins = wm.list_windows()
    geo = wm.get_display_geometry()
    ox = int(geo.get("x") or 0)
    oy = int(geo.get("y") or 0)
    ow = int(geo.get("width") or 1920)
    oh = int(geo.get("height") or 1080)
    layout = []
    for win in wins:
        if not win.get("visible", True):
            continue
        if not int(win.get("width") or 0) or not int(win.get("height") or 0):
            continue
        pid = win["pid"]
        app_name = get_app_name_by_pid(pid)
        # 非通过 Web 服务启动的进程（如手动 docker exec 启动）没有 app_name，
        # 用窗口 WM_CLASS 类名作为回退（如 "xedit", "vlc"）。
        if app_name is None:
            app_name = wm.get_window_class(win["wid"])
        if not app_name:
            # 既不在注册表也读不到 WM_CLASS 的窗口无法被 restore_app 拉起，
            # 存进布局只会产生永远失败的恢复项
            continue
        # args 默认抓进程 cmdline；web 应用（浏览器窗口）例外：
        # restore_app 对 web: 前缀约定 args 是 URL 本身，若存完整 chromium
        # 命令行，布局恢复时会被当成 URL 导致启动失败。
        # pid_url 内存表丢失（Web 重启）时从 cmdline 提取 http(s) URL 兜底。
        args = get_app_args_by_pid(pid)
        if (app_name or "").startswith("web:"):
            args = get_web_url_by_pid(pid) or args
        # 判断是否全屏——list_windows 已查过 _NET_WM_STATE，直接用；
        # 尺寸不小于目标输出作为兜底（极少数 WM 不回状态属性）
        is_fullscreen = bool(win.get("fullscreen")) or (
            win["width"] >= ow and win["height"] >= oh
        )
        layout.append({
            "app_name": app_name,
            "args": args,
            "x": int(win["x"]) - ox,
            "y": int(win["y"]) - oy,
            "w": win["width"],
            "h": win["height"],
            "fullscreen": is_fullscreen,
        })
    return layout


@app.post("/api/layout/save")
def api_layout_save():
    """保存当前窗口布局（覆盖当前布局 layout.json，开机自动恢复的那一份）。"""
    layout = _build_layout_from_windows()
    save_layout(layout)
    log_line(f"保存当前布局：{len(layout)} 个窗口"
             f"（全屏 {sum(1 for it in layout if it['fullscreen'])} 个）")
    return {"ok": True, "layout": layout, "msg": f"已保存 {len(layout)} 个窗口"}


@app.get("/api/layout/load")
def api_layout_load():
    return {"layout": load_layout()}


@app.post("/api/layout/restore")
def api_layout_restore():
    """把已保存的布局应用回屏幕（已有窗口重定位，缺失的重新拉起）。

    在此之前，布局保存后只能靠"重启整个容器"来恢复，用户想在运行中
    把窗口摆回原位没有任何入口。典型用法：关闭所有应用 → 应用已保存布局。
    本端点是长阻塞操作（可能拉起多个浏览器），FastAPI 会把它放进线程池，
    不阻塞面板其他接口的事件循环。
    """
    if _layout_lock.locked():
        return {"ok": False, "msg": "布局恢复正在进行中，请稍候再试"}
    res = apply_layout_threadsafe()
    if res["total"] == 0:
        return {"ok": False, "msg": "尚未保存过布局，请先点击【保存当前布局】",
                **res}
    msg = f"布局已应用：拉起 {res['started']} 个，重定位 {res['repositioned']} 个"
    if res["failed"]:
        msg += f"，{res['failed']} 个失败"
    log_line(f"手动应用已保存布局：{msg}", "WARN" if res["failed"] else "INFO")
    return {"ok": True, "msg": msg, **res}


# 布局方案名规则：仅长度做约束（1~40 字符），允许中文/空格/标点——
# 它只作为 JSON 对象的 key，不参与任何文件路径拼接，无需担心路径穿越
PROFILE_NAME_MAX = 40


def _normalize_profile_name(name: str):
    """校验方案名，返回 (clean_name, error_msg)。"""
    name = (name or "").strip()
    if not name:
        return "", "方案名不能为空"
    if len(name) > PROFILE_NAME_MAX:
        return "", f"方案名不能超过 {PROFILE_NAME_MAX} 个字符"
    if any(ord(ch) < 32 for ch in name):
        return "", "方案名不能包含控制字符"
    return name, ""


@app.get("/api/layout/profiles")
def api_layout_profiles():
    """列出全部命名布局方案（只回名称与窗口数，不回完整几何以减小响应）。"""
    profiles = load_profiles()
    items = [{"name": name, "windows": len(layout or [])}
             for name, layout in profiles.items()]
    items.sort(key=lambda it: it["name"])
    return {"ok": True, "profiles": items}


@app.post("/api/layout/save_profile")
def api_layout_save_profile(name: str):
    """把当前窗口布局另存为命名方案（不影响当前 layout.json）。"""
    name, err = _normalize_profile_name(name)
    if err:
        return {"ok": False, "msg": err}
    layout = _build_layout_from_windows()
    if not layout:
        return {"ok": False, "msg": "当前没有可保存的可见窗口"}
    profiles = load_profiles()
    existed = name in profiles
    profiles[name] = layout
    save_profiles(profiles)
    log_line(f"保存布局方案「{name}」：{len(layout)} 个窗口"
             f"（{'覆盖已有方案' if existed else '新建方案'}）")
    return {"ok": True, "msg": f"方案「{name}」已保存（{len(layout)} 个窗口）",
            "name": name, "overwritten": existed}


@app.post("/api/layout/apply_profile")
def api_layout_apply_profile(name: str):
    """应用指定命名方案（临时摆放窗口，不改动当前布局 layout.json）。"""
    name, err = _normalize_profile_name(name)
    if err:
        return {"ok": False, "msg": err}
    profiles = load_profiles()
    if name not in profiles:
        return {"ok": False, "msg": f"方案「{name}」不存在"}
    if _layout_lock.locked():
        return {"ok": False, "msg": "布局恢复正在进行中，请稍候再试"}
    res = apply_layout_threadsafe(layout_override=profiles[name])
    msg = f"方案「{name}」已应用：拉起 {res['started']} 个，" \
          f"重定位 {res['repositioned']} 个"
    if res["failed"]:
        msg += f"，{res['failed']} 个失败"
    log_line(f"应用布局方案：{msg}", "WARN" if res["failed"] else "INFO")
    return {"ok": True, "msg": msg, "name": name, **res}


@app.post("/api/layout/delete_profile")
def api_layout_delete_profile(name: str):
    """删除命名布局方案（当前布局 layout.json 不受影响）。"""
    name, err = _normalize_profile_name(name)
    if err:
        return {"ok": False, "msg": err}
    profiles = load_profiles()
    if name not in profiles:
        return {"ok": False, "msg": f"方案「{name}」不存在"}
    del profiles[name]
    save_profiles(profiles)
    log_line(f"删除布局方案「{name}」")
    return {"ok": True, "msg": f"方案「{name}」已删除", "name": name}


@app.post("/api/settings/set_web_port")
def api_set_web_port(new_port: int):
    if not (1024 <= new_port <= 65535):
        return {"ok": False, "msg": "端口范围错误，应为 1024~65535"}
    cfg = load_settings()
    cfg["web_port"] = new_port
    save_settings(cfg)
    return {"ok": True, "msg": "端口已保存，重启容器生效"}


@app.post("/api/settings/set_bg_color")
def api_set_bg_color(bg_color: str):
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", bg_color):
        return {"ok": False, "msg": "背景色格式错误，应为 #RRGGBB"}
    cfg = load_settings()
    cfg["bg_color"] = bg_color
    save_settings(cfg)
    return {"ok": True, "msg": "背景色已保存，重启容器生效"}


@app.post("/api/settings/set_auto_start")
def api_set_auto_start(enable: bool):
    """开机自启开关：持久化设置，并立即同步为本容器的 Docker 重启策略。

    - 开启 → unless-stopped：NAS/容器守护进程重启后自动拉起；
    - 关闭 → no：守护进程重启后不自动拉起。
    docker.sock 未挂载（非飞牛环境/裸 docker run）时只持久化设置，
    由 entrypoint 在下次启动时或平台层负责生效。
    """
    cfg = load_settings()
    cfg["auto_start"] = bool(enable)
    save_settings(cfg)
    policy = RESTART_POLICY_ON if enable else RESTART_POLICY_OFF
    applied = False
    try:
        applied = set_self_restart_policy(policy)
    except Exception:
        applied = False
    if applied:
        msg = ("已开启开机自启（Docker 重启策略 unless-stopped，NAS 重启后自动拉起）"
               if enable else
               "已关闭开机自启（Docker 重启策略 no；在飞牛应用中心重新启用本应用会恢复自启）")
    elif not docker_available():
        msg = ("开机自启设置已保存（本环境未挂载 docker.sock，"
               "由平台启动策略决定，重启容器后生效）")
    else:
        msg = "开机自启设置已保存，但 Docker 重启策略同步失败，请查看运行日志"
        log_line(f"同步 Docker 重启策略失败：policy={policy}", "WARN")
    return {"ok": True, "msg": msg, "restart_policy": policy if applied else None}


# 面板配色主题。合法值必须与 index.html / index.css / app.js 里的主题清单一致；
# 存服务端而不是只存 localStorage，是为了换设备、清缓存后配色不丢。
UI_THEMES = ("midnight", "graphite", "pine", "violet", "amber", "daylight")


@app.post("/api/settings/set_theme")
def api_set_theme(theme: str):
    theme = (theme or "").strip()
    if theme not in UI_THEMES:
        return {"ok": False, "msg": f"未知主题「{theme}」，可选：{' / '.join(UI_THEMES)}"}
    cfg = load_settings()
    cfg["ui_theme"] = theme
    save_settings(cfg)
    return {"ok": True, "msg": f"配色已切换为「{theme}」"}


@app.post("/api/settings/set_screenshot_interval")
def api_set_screenshot_interval(seconds: int):
    """设置定时自动截图间隔。

    0 = 关闭；否则必须在 10~3600 秒之间（下限 10 秒防止 scrot 把 CPU/磁盘
    打满；上限 1 小时）。后台循环每秒检查一次设置，保存后最多 1 秒生效，
    无需重启容器。截图存 /data/screenshots，自动保留最新 200 张。
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return {"ok": False, "msg": "间隔必须是整数（0 或 10~3600）"}
    if seconds != 0 and not (10 <= seconds <= 3600):
        return {"ok": False, "msg": "间隔范围错误：0（关闭）或 10~3600 秒"}
    cfg = load_settings()
    cfg["screenshot_interval"] = seconds
    save_settings(cfg)
    if seconds == 0:
        msg = "定时截图已关闭"
    elif seconds % 60 == 0:
        msg = f"定时截图已开启：每 {seconds // 60} 分钟一张"
    else:
        msg = f"定时截图已开启：每 {seconds} 秒一张"
    log_line(msg)
    return {"ok": True, "msg": msg, "screenshot_interval": seconds}


@app.post("/api/settings/set_display_resolution")
def api_set_display_resolution(width: int, height: int):
    """设置 HDMI 输出分辨率，用于前端拖拽画布的坐标换算。

    注意：本接口不修改 Xorg 实际输出分辨率（由显示器 EDID / xrandr 决定，
    /api/status 会返回真实值）；该字段仅作为真实值读不到时的兜底
    以及前端画布换算的备用值。
    """
    if not (1 <= width <= 7680 and 1 <= height <= 4320):
        return {"ok": False, "msg": "分辨率范围错误（1~7680 / 1~4320）"}
    cfg = load_settings()
    cfg["display_width"] = width
    cfg["display_height"] = height
    save_settings(cfg)
    return {"ok": True, "msg": f"分辨率已保存：{width}x{height}"}


@app.post("/api/screenshot")
def api_screenshot():
    # scrot 最坏阻塞数秒：同步端点由 FastAPI 放进线程池，不堵事件循环
    ok = wm.take_screenshot()
    if ok:
        return {"ok": True, "msg": "截图完成"}
    log_line("HDMI 截图失败（scrot 未安装 / Xorg 未运行 / 输出未连接）", "WARN")
    return {"ok": False, "msg": "截图失败，请检查 scrot 是否安装、Xorg 是否运行、HDMI 是否连接"}


@app.get("/screenshot.png")
def get_screenshot_img():
    if not os.path.exists(SCREENSHOT_PATH):
        return PlainTextResponse("截图文件不存在，请先调用 /api/screenshot", status_code=404)
    return FileResponse(SCREENSHOT_PATH)


@app.get("/api/logs")
def api_logs():
    """返回运行日志尾部，供网页【查看运行日志】展示。"""
    return {"logs": read_logs(300)}


# ---------------------------------------------------------------- 访问令牌 API
@app.get("/api/access/status")
def api_access_status():
    """鉴权状态（公开接口，登录页据此渲染）。

    只返回开关状态与"是否仍为出厂初始令牌"，不泄露令牌内容。
    """
    cfg = load_settings()
    ac.ensure_access_token(cfg, save_settings)
    return {"ok": True,
            "require_token": bool(cfg.get("require_token", True)),
            "token_is_default": ac.token_is_default(cfg)}


@app.post("/api/access/login")
def api_access_login(token: str, request: Request):
    """用令牌换取登录 Cookie（公开接口，供令牌输入页调用）。"""
    cfg = load_settings()
    expected = ac.ensure_access_token(cfg, save_settings)
    if not expected or not token or not hmac.compare_digest(token.strip(), expected):
        log_line("访问令牌校验失败（令牌不匹配）", "WARN")
        return JSONResponse({"ok": False, "msg": "令牌不正确"}, status_code=403)
    resp = JSONResponse({"ok": True, "msg": "验证通过"})
    # 每次登录轮换 Cookie 内容的一小段随机盐没有必要（令牌本身即密钥），
    # 直接种 HttpOnly Cookie：JS 读不到，能缓解面板页面内 XSS 窃取。
    resp.set_cookie(ac.TOKEN_COOKIE, expected, httponly=True,
                    samesite="lax", max_age=365 * 24 * 3600, path="/")
    return resp


@app.get("/api/access/token")
def api_access_get_token():
    """查看当前令牌（能打开面板即有权查看：飞牛入口/已有令牌/回环）。"""
    cfg = load_settings()
    token = ac.ensure_access_token(cfg, save_settings)
    return {"ok": True, "token": token,
            "require_token": bool(cfg.get("require_token", True))}


@app.post("/api/access/rotate")
def api_access_rotate_token():
    """重新生成随机访问令牌（旧令牌与旧 Cookie 立即失效）。"""
    import secrets
    cfg = load_settings()
    ac.ensure_access_token(cfg, save_settings)
    new_token = secrets.token_hex(16)
    cfg["access_token"] = new_token
    cfg["access_token_customized"] = True
    save_settings(cfg)
    log_line("访问令牌已由面板随机重置")
    resp = JSONResponse({"ok": True, "token": new_token,
                         "msg": "令牌已重置，旧令牌立即失效"})
    resp.set_cookie(ac.TOKEN_COOKIE, new_token, httponly=True,
                    samesite="lax", max_age=365 * 24 * 3600, path="/")
    return resp


@app.post("/api/access/set_token")
def api_access_set_token(token: str):
    """把访问令牌改为用户自定义值（校验长度与字符集；旧令牌立即失效）。"""
    token = (token or "").strip()
    valid, msg = ac.validate_custom_token(token)
    if not valid:
        return JSONResponse({"ok": False, "msg": msg}, status_code=422)
    cfg = load_settings()
    old_token = ac.ensure_access_token(cfg, save_settings)
    if hmac.compare_digest(token, old_token):
        return JSONResponse(
            {"ok": False, "msg": "新令牌与当前令牌相同，未修改"},
            status_code=422)
    cfg["access_token"] = token
    cfg["access_token_customized"] = True
    save_settings(cfg)
    log_line("访问令牌已由用户修改为自定义令牌")
    resp = JSONResponse(
        {"ok": True, "token": token,
         "msg": "令牌已更新，其他设备需用新令牌重新登录"})
    # 当前浏览器种上新 Cookie，避免改完自己被登出
    resp.set_cookie(ac.TOKEN_COOKIE, token, httponly=True,
                    samesite="lax", max_age=365 * 24 * 3600, path="/")
    return resp


@app.post("/api/access/set_required")
def api_access_set_required(enable: bool):
    """开启/关闭令牌保护。关闭仅建议在受信隔离网络中使用。"""
    cfg = load_settings()
    ac.ensure_access_token(cfg, save_settings)
    cfg["require_token"] = bool(enable)
    save_settings(cfg)
    log_line(f"访问令牌保护已{'开启' if enable else '关闭'}",
             "WARN" if not enable else "INFO")
    return {"ok": True, "require_token": bool(enable),
            "msg": "访问令牌保护已开启" if enable
                   else "访问令牌保护已关闭（面板对局域网开放，请确保网络可信）"}
