import json
import os
import time

# 默认 /data（容器持久卷）；可通过 FNWC_DATA_DIR 环境变量重定位，
# 便于本地调试或多实例共用一个镜像
DATA_DIR = os.environ.get("FNWC_DATA_DIR", "/data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LAYOUT_PATH = os.path.join(DATA_DIR, "layout.json")
PROFILES_PATH = os.path.join(DATA_DIR, "layout_profiles.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
PIDS_PATH = os.path.join(DATA_DIR, "pids.json")
# pid 进程身份（create_time）侧车文件：容器重建后 PID namespace 重置，
# 旧 pid 可能被 Xorg/openbox 复用，仅靠"pid 是否存活"会把系统进程错认成
# 旧应用。与 pids.json 分开存放，保持 pids.json 的扁平格式不变（向后兼容）。
PIDS_CTIME_PATH = os.path.join(DATA_DIR, "pids.ctime.json")
# 容器实例标记：内容为本实例 PID 1 的启动时间。/data 跨容器重建保留，
# 标记对不上即说明注册表里的 pid 全部来自上一个容器实例，必须整体丢弃。
INSTANCE_PATH = os.path.join(DATA_DIR, ".container.instance")
SCREENSHOT_PATH = os.path.join(DATA_DIR, "screenshot.png")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")

# 容器场景下 /data 由 entrypoint.sh 提前创建，但本地调试 main.py 时
# 可能没有 entrypoint 兜底，这里 best-effort 创建一次，避免后续写入抛错
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    # /data 在某些只读环境不可创建，后续 save_* 会再次尝试并抛出
    pass

DEFAULT_SETTINGS = {
    "web_port": 8181,
    "bg_color": "#000000",
    "auto_start": True,
    "default_audio_sink": None,
    # 显示输出分辨率，用于前端拖拽画布按比例换算坐标。
    # 仅作真实值读不到时的兜底：正常情况下 /api/status 会返回
    # 目标输出（xrandr 实测）的宽高，跟随显示器 EDID 自动变化。
    "display_width": 1920,
    "display_height": 1080,
    # 目标显示器：多接口/多屏时用哪一块屏做布局基准。
    # 空串 = 自动（primary → 首个已连接输出）；用户可在面板切换。
    # 输出名形如 HDMI-1 / DP-2 / VGA-1 / DVI-I-1，与 xrandr 一致。
    "target_output": "",
    # 面板配色主题。可选值与 main.py 的 UI_THEMES、index.css 的
    # :root[data-theme="..."] 块、app.js 的 THEMES 数组必须保持一致。
    "ui_theme": "midnight",
    # 定时自动截图间隔（秒）。0 = 关闭；允许范围 10~3600。
    # 截图存 /data/screenshots，用于无人值守时回溯现场。
    "screenshot_interval": 0,
    # 面板访问令牌：容器以 host 网络 + privileged 运行，面板能启动/停止
    # 容器内进程，局域网内任何人都能打开面板是真实风险。开启后：
    #   - 从飞牛桌面入口（同 NAS 的 iframe/页面跳转）打开面板：免令牌
    #   - 面板自身同源 XHR：免令牌
    #   - NAS 本机回环地址（127.0.0.1/::1）：免令牌
    #   - 其余访问（直接用 IP 打开、curl/脚本）：必须带令牌
    # 全新部署首次启动写入出厂固定初始令牌（access_control.DEFAULT_ACCESS_TOKEN，
    # 当前为 admin123）并标记 access_token_customized=False，面板与登录页会提示
    # 尽快改为自定义令牌；用户自定义/随机重置后标记 True。也可用环境变量
    # WC_ACCESS_TOKEN 在首次启动时预置（视为已定制）。
    "require_token": True,
    "access_token": "",
    "access_token_customized": False,
}


def atomic_write_json(path, data):
    """先写同目录临时文件再 os.replace 原子替换。

    /data 在 NAS 卷上，直接 open(path,'w') 截断写时若断电/磁盘满，
    会留下半截 JSON，之后所有 load_* 都解析失败（settings 坏掉会让整个
    面板永久 500）。os.replace 在同文件系统上是原子操作，要么是旧文件、
    要么是完整新文件，不存在中间态。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings():
    if not os.path.exists(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("settings 根节点不是对象")
    except Exception:
        # 文件损坏（断电截断/手工改坏）时不能让整个面板不可用：
        # 回退默认配置并立即重写，下次读取恢复正常。
        cfg = DEFAULT_SETTINGS.copy()
        try:
            save_settings(cfg)
        except Exception:
            pass
        return cfg
    # 字段向后兼容：旧 settings.json 缺字段时补默认值
    for k, v in DEFAULT_SETTINGS.items():
        cfg.setdefault(k, v)
    return cfg


def save_settings(cfg):
    atomic_write_json(SETTINGS_PATH, cfg)


def load_layout():
    """读取当前布局。损坏/结构非法时返回 []。

    除 JSON 合法性外还校验结构：必须是 list 且每项是 dict，
    否则 apply_layout 里的 item.get 会抛 AttributeError（接口 500、
    开机自动恢复静默失败）。
    """
    if not os.path.exists(LAYOUT_PATH):
        return []
    try:
        with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_layout(layout):
    atomic_write_json(LAYOUT_PATH, layout)


def load_profiles():
    """读取命名布局方案 {名称: layout}。

    与 layout.json 区分：layout.json 是"当前布局"（开机自动恢复的那一份），
    layout_profiles.json 是用户保存的多套可切换方案。
    文件损坏/非法 JSON 时兜底为空 dict，避免一个坏文件让方案面板不可用。
    """
    if not os.path.exists(PROFILES_PATH):
        return {}
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # 值必须是列表（layout 数组），剔掉脏数据
        return {str(k): v for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


def save_profiles(profiles):
    atomic_write_json(PROFILES_PATH, profiles)


def load_pids():
    """读取持久化的 pid -> app_name 映射。

    Web 服务崩溃重启后，内存里的 pid_app_name 会丢失，
    但 Xorg/openbox/PipeWire 与已启动的 GUI 程序仍在运行（核心架构原则）。
    持久化到 /data/pids.json 后，重启的 Web 仍能知道每个 pid 对应什么程序，
    从而让 save_layout 在 Web 重启后继续工作。
    """
    if not os.path.exists(PIDS_PATH):
        return {}
    try:
        with open(PIDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # JSON 的 key 一律是字符串，需转回 int
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


def save_pids(pids):
    # JSON key 必须是字符串
    data = {str(k): v for k, v in pids.items()}
    atomic_write_json(PIDS_PATH, data)


def load_pid_ctimes():
    """读取 pid -> create_time 身份表（缺失/损坏返回 {}）。"""
    if not os.path.exists(PIDS_CTIME_PATH):
        return {}
    try:
        with open(PIDS_CTIME_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def save_pid_ctimes(ctimes):
    """持久化 pid -> create_time。与 pids.json 同生共死。"""
    data = {str(k): v for k, v in ctimes.items()}
    atomic_write_json(PIDS_CTIME_PATH, data)


def read_instance_marker():
    """读取上次容器实例标记；无文件/不可读返回 None。"""
    try:
        with open(INSTANCE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def write_instance_marker(marker: str) -> None:
    """写入本次容器实例标记（失败静默：最坏情况退化为仅 ctime 校验）。"""
    try:
        directory = os.path.dirname(os.path.abspath(INSTANCE_PATH))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{INSTANCE_PATH}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(marker))
        os.replace(tmp, INSTANCE_PATH)
    except Exception:
        pass


def read_logs(lines=200):
    if not os.path.exists(LOG_PATH):
        return "暂无日志"
    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        content = f.readlines()
    return "".join(content[-lines:])


def log_line(msg: str, level: str = "INFO") -> None:
    """向 /data/app.log 追加一行应用层日志（与 entrypoint 同一格式）。

    entrypoint.sh 只记录"容器与服务编排"级别的日志；Web 层发生的事
    （谁拉起了哪个应用、布局恢复结果、接口报错）此前完全没有留痕，
    而网页上的【查看运行日志】正是为这些排障场景准备的。

    实现要点：每次调用独立 open(append)，不持有长期文件句柄。
    entrypoint 的日志轮转是 `tail > f.rotating && mv f.rotating f`，
    若长期持有句柄，轮转后进程会继续写进已被 unlink 的 inode，
    新文件不再增长——日志看起来"凭空消失"。
    写失败（/data 只读等）时必须静默：日志永远不能让业务接口挂掉。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # 防日志伪造：msg 可能含接口入参（应用名/URL 等），其中的换行符若原样
    # 落盘，攻击者可伪造出"[时间] 【INFO】……"样子的假日志行。统一压成空格。
    safe_msg = str(msg).replace("\r", " ").replace("\n", " ")
    safe_level = str(level).replace("\r", " ").replace("\n", " ")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] 【{safe_level}】{safe_msg}\n")
    except Exception:
        pass
