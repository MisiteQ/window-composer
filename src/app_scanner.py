"""飞牛 NAS 已安装应用 + 宿主 Docker 容器扫描器。

仅负责识别飞牛 NAS 上已安装的应用程序、以及宿主上运行的 Docker 容器，
并提供元信息；不涉及任何显示/启动逻辑，也不依赖容器内浏览器。

数据来源（均通过 Docker 卷挂载进容器，只读）：
  /host_apps              ← 宿主 /var/apps（应用根目录）
  /usr/local/apps/@appcenter ← 系统 trim.* 应用的 target 实际目录
  /vol1/@appcenter        ← 第三方应用的 target 实际目录
  /hostproc               ← 宿主 /proc（运行状态判定）
  /var/run/docker.sock    ← 宿主 Docker 守护进程（容器扫描，见下）

每个飞牛应用结构：
  /var/apps/<appname>/
    ├── manifest              应用元信息（display_name, source, version, service_port）
    ├── target/ui/config      UI 入口配置（type: url|iframe, port, url）
    └── cmd/main              生命周期脚本（本扫描器不调用，仅识别）

Docker 容器扫描走 Docker Engine HTTP API（Unix socket + stdlib http.client），
**不需要容器里装 docker CLI** —— 这是"安装包装完即用、NAS 端不再下载任何东西"
的前提。socket 没挂进容器时 docker_available() 为 False，Docker 部分整体静默
跳过，飞牛应用扫描与面板功能都不受影响。
"""
import os
import re
import json
import time
import stat
import socket
import threading
import http.client

# 宿主挂载点
HOST_APPS = "/host_apps"               # /var/apps
HOSTPROC = "/hostproc"                 # /proc

# 飞牛系统 Web 端口（无独立端口的应用经 nginx 反代访问）
FNOS_WEB_PORT = os.environ.get("WC_FNOS_PORT", "5666")
# 宿主访问地址（容器内通过 docker0 网关访问宿主服务）
HOST_GATEWAY = os.environ.get("WC_HOST_GATEWAY", "172.17.0.1")

# 系统应用前缀
SYS_APP_PREFIXES = ("trim.",)

# 非应用目录（运行时/共享库）
NON_APP_DIRS = {"nodejs_v22", "nodejs_v24",
                "python39", "python310", "python311", "python312",
                "fndepot.source", "manifest"}

# ui/config 提取正则（JSON 解析失败时兜底）
_RE_PORT = re.compile(r'"port"\s*:\s*"?(\d{2,5})"?')
_RE_URL = re.compile(r'"url"\s*:\s*"([^"]+)"')
_RE_TYPE = re.compile(r'"type"\s*:\s*"([^"]+)"')

_SCAN_TTL = 10.0
_scan_lock = threading.Lock()
_scan_cache = {"at": 0.0, "apps": []}

# ------------------------------------------------------------------ Docker 数据源
# 宿主 Docker 守护进程 socket，由 compose 挂载进来：
#     /var/run/docker.sock:/var/run/docker.sock
DOCKER_SOCK = os.environ.get("WC_DOCKER_SOCK", "/var/run/docker.sock")
DOCKER_GROUP = "Docker 应用"

# 容器内端口命中这些值时优先当作 Web 入口（映射到宿主端口后面板就能显示它）
WEB_PORTS = (80, 443, 3000, 5000, 5001, 5601, 8000, 8006, 8080, 8081, 8088,
             8443, 8888, 9000, 9090, 9443, 10000)

# TLS 端口：排在最后再选，避免把 http 请求打到 https 端口上
TLS_PORTS = (443, 8443)

# 分组展示顺序：系统应用 → 第三方应用 → Docker 应用
GROUP_ORDER = {"系统应用": 0, "第三方应用": 1, DOCKER_GROUP: 2}

# 本容器自身的容器名（除了 hostname / cgroup 识别，再加一道名字兜底）
SELF_NAMES = {"window-composer"} | {
    n.strip() for n in os.environ.get("WC_SELF_NAME", "").split(",") if n.strip()
}

_DOCKER_TTL = 10.0
_docker_lock = threading.Lock()
_docker_cache = {"at": 0.0, "apps": []}


def _parse_i18n(app_dir: str) -> dict:
    """解析 i18n/zh 文件（INI 格式），返回 [common] 节的键值对。

    manifest 中的 ${common.display_name} 等变量需从此文件解析。
    """
    common = {}
    i18n_path = os.path.join(app_dir, "i18n", "zh")
    try:
        with open(i18n_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return common

    in_common = False
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_common = (line[1:-1] == "common")
            continue
        if in_common and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            common[k.strip()] = v
    return common


def _parse_manifest(path: str, app_dir: str = "") -> dict:
    """解析 manifest 文件（key=value 格式），支持 ${common.xxx} 变量替换。"""
    # 先加载 i18n 中的 common 变量
    common = _parse_i18n(app_dir) if app_dir else {}
    info = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                # 替换 ${common.xxx} 变量
                if v.startswith("${common.") and v.endswith("}"):
                    var_name = v[len("${common."):-1]
                    v = common.get(var_name, "")
                info[k.strip()] = v
    except OSError:
        pass
    return info


def _parse_ui_config(path: str) -> dict:
    """解析 target/ui/config，提取 type/port/url。"""
    result = {"type": "", "port": "", "url": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return result

    try:
        data = json.loads(content)
        for _, entry in (data.get(".url") or {}).items():
            result["type"] = entry.get("type", "")
            result["port"] = str(entry.get("port", "") or "")
            result["url"] = entry.get("url", "")
            if result["type"]:
                break
        return result
    except (json.JSONDecodeError, ValueError):
        pass

    for attr, regex in (("type", _RE_TYPE), ("port", _RE_PORT), ("url", _RE_URL)):
        m = regex.search(content)
        if m:
            result[attr] = m.group(1)
    return result


def _app_group(source: str, name: str) -> str:
    if name.startswith(SYS_APP_PREFIXES) or source == "official":
        return "系统应用"
    return "第三方应用"


def _build_access_url(port: str, url_path: str) -> str:
    """根据 port 和 url_path 构建宿主上的访问地址（仅供参考，不负责显示）。"""
    if port:
        try:
            if 1 <= int(port) <= 65535:
                base = f"http://{HOST_GATEWAY}:{port}"
            else:
                base = f"http://{HOST_GATEWAY}:{FNOS_WEB_PORT}"
        except ValueError:
            base = f"http://{HOST_GATEWAY}:{FNOS_WEB_PORT}"
    else:
        base = f"http://{HOST_GATEWAY}:{FNOS_WEB_PORT}"
    if not url_path:
        url_path = "/"
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    return base + url_path


def _list_app_dirs() -> list:
    """扫描宿主应用目录，返回 [(应用名, 完整路径)]。"""
    found = {}
    if not os.path.isdir(HOST_APPS):
        return []
    for name in sorted(os.listdir(HOST_APPS)):
        if name in NON_APP_DIRS:
            continue
        full = os.path.join(HOST_APPS, name)
        if not os.path.isdir(full):
            continue
        # 必须有 manifest 才认为是飞牛应用
        if not os.path.isfile(os.path.join(full, "manifest")):
            continue
        found[name] = full
    return list(found.items())


def _host_process_cmds() -> set:
    """读取宿主进程表，返回所有 cmdline 集合。"""
    cmds = set()
    if not os.path.isdir(HOSTPROC):
        return cmds
    for entry in os.listdir(HOSTPROC):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(HOSTPROC, entry, "cmdline"), "rb") as f:
                raw = f.read()
            if raw:
                cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
                if cmdline:
                    cmds.add(cmdline)
        except (OSError, PermissionError):
            continue
    return cmds


def _is_running(name: str, path: str, proc_cmds: set) -> bool:
    """判定应用是否在运行。"""
    if path:
        for cmd in proc_cmds:
            if path in cmd:
                return True
    marker = f"/{name}/"
    for cmd in proc_cmds:
        if marker in cmd:
            return True
    return False


def _scan_apps() -> list:
    """扫描飞牛应用（目录元信息），不含运行状态。"""
    app_dirs = _list_app_dirs()
    if not app_dirs:
        return []

    apps = []
    for name, path in app_dirs:
        manifest = _parse_manifest(os.path.join(path, "manifest"), app_dir=path)
        ui = _parse_ui_config(os.path.join(path, "target", "ui", "config"))

        display_name = manifest.get("display_name") or manifest.get("name") or name
        source = manifest.get("source", "")
        service_port = manifest.get("service_port", "")

        # 端口优先级：ui/config 的 port > manifest 的 service_port
        port = ui["port"] or service_port
        if port:
            try:
                if not (1 <= int(port) <= 65535):
                    port = ""
            except ValueError:
                port = ""

        url_path = ui["url"]
        url = _build_access_url(port, url_path) if url_path else ""

        apps.append({
            "name": name,
            "label": display_name,
            "group": _app_group(source, name),
            "source": source,
            "version": manifest.get("version", ""),
            "port": port or None,
            "url": url,
            "url_path": url_path,
            "ui_type": ui["type"],
            "has_url": bool(url),
            "running": False,  # 运行状态由 scan_installed_apps 实时判定
            "path": path,
        })

    return apps


# ============================================================ Docker 容器扫描
class _UnixHTTPConnection(http.client.HTTPConnection):
    """把 http.client 接到 Unix domain socket 上（Docker 守护进程就是这个形态）。

    stdlib 没有现成的 unix-socket HTTP 客户端。为了这个扫描功能在镜像里塞一个
    docker CLI、或额外装 requests+urllib3 都不划算：这里只覆写 connect()，
    请求构造与响应解析全部复用 http.client。
    """

    def __init__(self, sock_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._sock_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def docker_available() -> bool:
    """Docker socket 是否已就位。

    必须校验确实是 socket：宿主上没有这个文件时，Docker 会因为 bind mount
    而在该路径建一个**空目录**，os.path.exists 会误判成"已挂载"。
    """
    try:
        return stat.S_ISSOCK(os.stat(DOCKER_SOCK).st_mode)
    except OSError:
        return False


def _docker_conn(sock_path: str, timeout: float) -> _UnixHTTPConnection:
    """连接工厂。

    单独拎出来是为了让测试能把它换成 TCP 连接去打一个本地假 Docker 服务：
    Windows 的 socket 模块没有 AF_UNIX，unixt socket 这一段在本机跑不了。
    """
    return _UnixHTTPConnection(sock_path, timeout)


def _docker_request(method: str, path: str, body=None, timeout: float = 8.0,
                    conn_factory=None):
    """通用 Docker Engine API 请求，返回 (status, json_or_None)。

    与 _docker_get 同样走 unix socket + stdlib http.client；body 传 dict 时
    自动 JSON 序列化。任何连接/解析异常返回 (0, None)，调用方按失败处理。
    """
    factory = conn_factory or _docker_conn
    conn = None
    try:
        conn = factory(DOCKER_SOCK, timeout)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return resp.status, None
    except (AttributeError, OSError, http.client.HTTPException):
        return 0, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _mountinfo_ids(paths=("/proc/1/mountinfo", "/proc/self/mountinfo")) -> list:
    """从 mountinfo 提取本容器 64 位长 ID（host 网络 + cgroup v2 下的兜底）。

    飞牛真机实测：network_mode: host 时容器共享宿主 UTS，HOSTNAME 是 NAS
    主机名（如 mozi-nas）而不是 12 位短 ID；cgroup v2 + systemd cgroup
    driver 时容器内 /proc/self/cgroup 只有 `0::/`——两条老路同时失效。
    mountinfo 仍然稳定带 ID：
      /docker/containers/<64hex>/resolv.conf   容器标准绑定挂载
      overlay2/<64hex>/diff                    根文件系统 upperdir
    优先用前者（与 Docker 自身目录布局一致），找不到再用 overlay。
    """
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        found = re.findall(r"/docker/containers/([0-9a-f]{64})/", text)
        if found:
            return found
        m = re.search(r"upperdir=[^ ]*?/([0-9a-f]{64})/diff(?:[ :]|$)", text)
        if m:
            return [m.group(1)]
    return []


def _self_container_id() -> str:
    """本容器的短 ID。

    发现顺序：HOSTNAME 12 位短 ID（默认 bridge 网络）→ cgroup 64 位长 ID
    （cgroup v1/部分 v2）→ mountinfo（host 网络 + cgroup v2，飞牛实测形态）。
    """
    hostname = (os.environ.get("HOSTNAME") or "").strip()
    if re.fullmatch(r"[0-9a-f]{12}", hostname):
        return hostname
    for tok in _self_tokens():
        if re.fullmatch(r"[0-9a-f]{64}", tok):
            return tok[:12]
    for cid in _mountinfo_ids():
        return cid[:12]
    return ""


# 开机自启开关 → Docker 重启策略的映射。
# unless-stopped：守护进程/NAS 重启后自动拉起，被手动 stop 后不抢跑；
# no：任何情况下都不自动拉起（飞牛应用中心重新"启用"本应用会按 compose
# 重新写回 unless-stopped，这是平台层行为）。
RESTART_POLICY_ON = "unless-stopped"
RESTART_POLICY_OFF = "no"


def set_self_restart_policy(policy: str, conn_factory=None) -> bool:
    """通过 Docker Engine API 修改本容器自身的重启策略。

    socket 未挂载 / 非容器环境 / 守护进程拒绝时返回 False（调用方降级为
    "仅记录配置"，不能让设置接口报错）。
    """
    if policy not in (RESTART_POLICY_ON, RESTART_POLICY_OFF, "always"):
        return False
    if not docker_available():
        return False
    cid = _self_container_id()
    if not cid:
        return False
    status, data = _docker_request(
        "POST", f"/containers/{cid}/update",
        body={"RestartPolicy": {"Name": policy}}, conn_factory=conn_factory)
    if status not in (200, 201):
        return False
    return not (isinstance(data, dict) and data.get("message"))


def get_self_restart_policy(conn_factory=None) -> str:
    """读取本容器当前重启策略名；取不到返回空串。"""
    if not docker_available():
        return ""
    cid = _self_container_id()
    if not cid:
        return ""
    status, data = _docker_request(
        "GET", f"/containers/{cid}/json", conn_factory=conn_factory)
    if status != 200 or not isinstance(data, dict):
        return ""
    try:
        return str(data["HostConfig"]["RestartPolicy"]["Name"] or "")
    except (KeyError, TypeError):
        return ""


def _docker_get(path: str, timeout: float = 5.0, conn_factory=None):
    """GET 一个 Docker Engine API 路径，返回解析后的 JSON；任何失败都返回 None。

    路径不带 /vX.Y 前缀：守护进程会按自身版本回应，跨飞牛版本的 Docker
    差异不会让请求 404。

    兜底里特意带上 AttributeError：Windows 的 socket 模块根本没有 AF_UNIX，
    这类"平台不支持"不该把上层的应用列表接口一起带崩。
    """
    factory = conn_factory or _docker_conn
    conn = None
    try:
        conn = factory(DOCKER_SOCK, timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return None
        return json.loads(body.decode("utf-8", "replace"))
    except (AttributeError, OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _self_tokens() -> set:
    """本容器自身的标识集合，用来把自己从 Docker 列表里剔掉。

    Docker 默认把 hostname 设成容器**短 id**；容器重建后 id 会变，
    所以再读一次 /proc/self/cgroup 拿 64 位长 id。两者都收。
    """
    tokens = set()
    hostname = (os.environ.get("HOSTNAME") or "").strip()
    if hostname:
        tokens.add(hostname)
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                for part in line.strip().split("/"):
                    if re.fullmatch(r"[0-9a-f]{64}", part):
                        tokens.add(part)
    except OSError:
        pass
    # host 网络 + cgroup v2（飞牛实测）：cgroup 拿不到，mountinfo 兜底
    for cid in _mountinfo_ids():
        tokens.add(cid)
        tokens.add(cid[:12])
    return tokens


def _is_self_container(cid: str, name: str, tokens: set) -> bool:
    """判断某个容器是不是本应用自己（面板把自己列成一个"应用"会很怪）。"""
    if name and name in SELF_NAMES:
        return True
    if not cid:
        return False
    return any(t and cid.startswith(t) for t in tokens)


def _pick_web_port(ports: list) -> tuple:
    """从容器端口映射里挑一个最适合当 Web 入口的宿主端口，返回 (端口, scheme)。

    优先级：常见 Web 端口（80/8080/3000…）> 其它已发布的 TCP 端口 > TLS 端口。
    只考虑已发布的端口 —— 没映射到宿主的端口，面板访问不到。
    """
    published = []
    for p in (ports or []):
        if not isinstance(p, dict):
            continue
        if str(p.get("Type") or "").lower() != "tcp":
            continue
        pub, priv = p.get("PublicPort"), p.get("PrivatePort")
        if not pub:
            continue
        try:
            pub, priv = int(pub), int(priv or 0)
        except (TypeError, ValueError):
            continue
        published.append((priv, pub))

    if not published:
        return 0, "http"
    for priv, pub in published:
        if priv in WEB_PORTS and priv not in TLS_PORTS:
            return pub, "http"
    for priv, pub in published:
        if priv not in TLS_PORTS:
            return pub, "http"
    priv, pub = published[0]
    return pub, ("https" if priv in TLS_PORTS else "http")


def _image_tag(image: str) -> str:
    """从镜像引用里取 tag：nginx:1.25 → 1.25；registry:5000/foo → 空（那是端口）。"""
    if not image or ":" not in image:
        return ""
    tag = image.rsplit(":", 1)[1]
    return "" if "/" in tag else tag


def _norm_app_name(value: str) -> str:
    """归一化应用名用于跨数据源匹配。

    飞牛应用目录名（驼峰，如 FnMessageBot）与它的 Docker 容器名
    （连字符小写，如 fn-message-bot）往往不一致，归一化后再比较：
      FnMessageBot / fn-message-bot / fn_message_bot -> fnmessagebot
    """
    if not value:
        return ""
    # 小写字母/数字后紧跟大写字母视为单词边界：FnMessageBot -> Fn-Message-Bot
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    return re.sub(r"[-_\s]+", "", value).lower()


def _covered_by_fnos(name: str, project: str, known: set) -> bool:
    """飞牛应用中心的 Docker 应用已经被 /var/apps 扫到过，这里不再重复列出。

    否则同一个应用会在「第三方应用」和「Docker 应用」两个分组里各出现一次。
    匹配两种形态：名字与飞牛应用名相同，或以 `<应用名>-` / `<应用名>_` 加后缀
    （compose 默认给服务名补序号，如 jellyfin-1）。名称比较同时做大小写
    不敏感与驼峰/连字符归一（FnMessageBot vs fn-message-bot）。
    """
    known_norm = {_norm_app_name(k): k for k in known}
    for cand in (project, name):
        if not cand:
            continue
        if cand in known:
            return True
        cn = _norm_app_name(cand)
        if cn in known_norm:
            return True
        for kn in known:
            if cand.startswith(kn + "-") or cand.startswith(kn + "_"):
                return True
    return False


def _scan_docker_apps() -> list:
    """扫描宿主 Docker 容器（只读）。socket 未挂载或请求失败时返回空列表。"""
    if not docker_available():
        return []
    data = _docker_get("/containers/json?all=1")
    if not isinstance(data, list):
        return []

    tokens = _self_tokens()
    apps = []
    for c in data:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("Id") or "")
        names = [n.lstrip("/") for n in (c.get("Names") or []) if n]
        name = names[0] if names else cid[:12]
        if _is_self_container(cid, name, tokens):
            continue

        labels = c.get("Labels") or {}
        port, scheme = _pick_web_port(c.get("Ports"))
        image = str(c.get("Image") or "")
        title = str(labels.get("org.opencontainers.image.title") or "").strip()

        apps.append({
            "name": name,
            "label": title or name,
            "group": DOCKER_GROUP,
            "source": "docker",
            "version": _image_tag(image),
            "port": port or None,
            "url": f"{scheme}://{HOST_GATEWAY}:{port}/" if port else "",
            "url_path": "/",
            "ui_type": "",
            "has_url": bool(port),
            "running": str(c.get("State") or "").lower() == "running",
            "path": "",          # Docker 应用没有宿主 /var/apps 目录
            "image": image,
            "status": str(c.get("Status") or ""),
            "project": str(labels.get("com.docker.compose.project") or ""),
            "container_id": cid[:12],
        })
    return apps


def _docker_raw_cached(force_refresh: bool) -> list:
    """带 TTL 的 Docker 原始扫描结果（不去重，含 compose project 字段）。

    去重依赖实时飞牛应用集合（端口/名称都可能变化），合并在调用方做。
    """
    now = time.time()
    with _docker_lock:
        if force_refresh or now - _docker_cache["at"] >= _DOCKER_TTL:
            _docker_cache["apps"] = _scan_docker_apps()
            _docker_cache["at"] = now
        return [dict(a) for a in _docker_cache["apps"]]


def _match_fnos_app(docker_app: dict, fnos_apps: list):
    """判断一个 Docker 容器对应哪个飞牛应用，返回飞牛条目或 None。

    判定优先级：
      1. 宿主发布端口与飞牛应用 service_port 相同 —— 最可靠
         （FnMessageBot 的容器叫 fn-message-bot，名称对不上但端口一致）；
      2. 容器名/compose project 与飞牛应用名原名精确或前缀匹配；
      3. 驼峰/连字符归一后名称相同。
    """
    dp = str(docker_app["port"]) if docker_app.get("port") else ""
    if dp:
        for a in fnos_apps:
            if a.get("port") and str(a["port"]) == dp:
                return a
    name = docker_app.get("name", "")
    project = docker_app.get("project", "")
    nn = _norm_app_name(name)
    pn = _norm_app_name(project)
    for a in fnos_apps:
        kn = a["name"]
        if name == kn or name.startswith(kn + "-") or name.startswith(kn + "_"):
            return a
        kn_norm = _norm_app_name(kn)
        if kn_norm and ((nn and nn == kn_norm) or (pn and pn == kn_norm)):
            return a
    return None


def host_apps_mounted() -> bool:
    """容器内是否挂入了宿主 /var/apps（飞牛应用数据源）。"""
    return os.path.isdir(HOST_APPS)


def scan_installed_apps(force_refresh: bool = False) -> list:
    """扫描已安装应用：飞牛 NAS 应用 + 宿主 Docker 容器，附加实时运行状态。

    返回应用信息列表，每个应用包含：
      name, label, group, source, version, running, port, url, url_path, ui_type, has_url
    飞牛应用额外带 path；Docker 应用额外带 image / status / container_id。
    """
    if force_refresh:
        with _scan_lock:
            _scan_cache["at"] = 0.0
        with _docker_lock:
            _docker_cache["at"] = 0.0

    now = time.time()
    with _scan_lock:
        if _scan_cache["apps"] and now - _scan_cache["at"] < _SCAN_TTL:
            catalog = _scan_cache["apps"]
        else:
            catalog = _scan_apps()
            _scan_cache["at"] = now
            _scan_cache["apps"] = catalog

    proc_cmds = _host_process_cmds()
    apps = []
    for app in catalog:
        item = dict(app)
        item["running"] = _is_running(app["name"], app["path"], proc_cmds)
        apps.append(item)

    # Docker 是独立数据源：socket 没挂时返回空列表，不影响上面的飞牛应用结果。
    # 飞牛应用中心装的容器型应用两边都会扫到，这里做合并去重：
    # 命中间名/端口的容器不再重复展示，其运行状态回填飞牛条目
    # （容器进程 cmdline 不含 /var/apps 路径，进程表判定对这类应用会漏报，
    # Docker Engine 报告的状态才是权威的）。
    for d in _docker_raw_cached(force_refresh):
        hit = _match_fnos_app(d, apps)
        if hit is not None:
            if d.get("running"):
                hit["running"] = True
            continue
        item = dict(d)
        item.pop("project", None)
        apps.append(item)

    apps.sort(key=lambda a: (
        not a["running"],
        GROUP_ORDER.get(a["group"], 9),
        not a["has_url"],
        a["label"].lower(),
    ))
    return apps


# 命令行入口（供 entrypoint.sh 在容器启动时同步开机自启设置）：
#   python3 -m app_scanner restart-policy on
#   python3 -m app_scanner restart-policy off
# 退出码 0=已应用（或环境无 docker socket，按降级处理），1=调用失败
if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "restart-policy":
        flag = sys.argv[2].lower()
        if flag in ("on", "true", "1", "yes", RESTART_POLICY_ON):
            target = RESTART_POLICY_ON
        elif flag in ("off", "false", "0", "no", RESTART_POLICY_OFF):
            target = RESTART_POLICY_OFF
        else:
            print(f"unknown restart-policy: {flag}", file=sys.stderr)
            sys.exit(1)
        if not docker_available():
            print("docker socket unavailable, skip restart-policy sync")
            sys.exit(0)
        ok = set_self_restart_policy(target)
        print(f"restart-policy -> {target}: {'ok' if ok else 'failed'}")
        sys.exit(0 if ok else 1)
    print("usage: python3 -m app_scanner restart-policy <on|off>",
          file=sys.stderr)
    sys.exit(2)
