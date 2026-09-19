"""面板访问控制：令牌校验 + 飞牛桌面入口免令牌识别。

威胁模型：容器以 network_mode: host + privileged 运行，面板可以
启动/停止容器内进程。若不加防护，局域网任何设备直接访问
http://<NAS-IP>:8181 就能操作显示与应用。本模块实现"入口免令牌、
外部访问需令牌"的策略：

放行（不需要令牌）：
  1. NAS 本机回环地址（127.0.0.1 / ::1）——只有 NAS 上的本地进程能用；
  2. 带正确令牌的请求（Cookie / X-Access-Token / Bearer / ?wc_at=）；
  3. 面板自身的同源请求（Sec-Fetch-Site: same-origin）——面板页面加载后
     的轮询/操作天然同源，浏览器无法伪造该头（Sec-Fetch-* 是浏览器保留头，
     禁止 JS 设置）；
  4. 从飞牛桌面发起的入口导航（iframe 嵌入或新标签页跳转）：
     Sec-Fetch-Mode: navigate 且 Referer/Origin 的主机与面板 Host 相同
     （飞牛 Web 跑在同一 NAS 的 5666 端口，主机相同、端口不同）。
     攻击者控制不了同主机上的 Referer，跨站 iframe/链接均过不了这一关。

其余访问（直接输 IP 打开、curl、脚本）一律要求令牌。

注意：这不是完整的账号体系，只是把"局域网无门槛操控 privileged 容器"
降到"需要先拿到一次性令牌"。令牌在面板内可查看/重置，也可用环境变量
WC_ACCESS_TOKEN 预置。
"""
import hmac
import os
import re
import secrets
from urllib.parse import urlparse

# Cookie / 查询参数 / 头部里的令牌字段名（同名一致，便于记忆）
TOKEN_COOKIE = "wc_at"
TOKEN_QUERY = "wc_at"
TOKEN_HEADER = "x-access-token"

# 出厂固定初始令牌：全新部署首次启动时写入，用户第一次直连 IP 即可用它登录。
# 初始令牌是公开的出厂约定，不能长期使用——面板与登录页会在"仍是初始令牌"
# 时醒目提示修改；用户设置自定义令牌或随机重置后，该标记清除。
DEFAULT_ACCESS_TOKEN = "admin123"

# 用户自定义令牌的约束。字符集刻意收窄：令牌会经查询参数 / Cookie / 请求头
# 传递，排除空格与 & = ? / 等会破坏 URL 解析或 shell 拼接的字符。
TOKEN_MIN_LEN = 6
TOKEN_MAX_LEN = 64
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._@\-]{%d,%d}$" % (TOKEN_MIN_LEN, TOKEN_MAX_LEN))

# 无需鉴权即可访问的路径：静态资源、鉴权状态查询、登录接口本身
PUBLIC_PATHS = (
    "/static/",
    "/favicon.ico",
    "/api/access/status",
    "/api/access/login",
)


def _persist(settings: dict, save) -> None:
    if save is None:
        return
    try:
        save(settings)
    except Exception:
        pass


def ensure_access_token(settings: dict, save=None) -> str:
    """确保 settings 里有访问令牌；没有就写入并（可选）持久化。

    首次写入的优先级：
      1. 环境变量 WC_ACCESS_TOKEN（批量部署预置）—— 视为已定制；
      2. 出厂固定初始令牌 DEFAULT_ACCESS_TOKEN —— 标记为未定制，
         面板/登录页提示尽快修改。
    历史版本（随机令牌时代）留下的 settings 没有 customized 标记，
    一律按"已定制"处理，避免升级后误报初始令牌警告。
    返回当前令牌。
    """
    token = str(settings.get("access_token") or "").strip()
    if not token:
        preset = os.environ.get("WC_ACCESS_TOKEN", "").strip()
        if preset:
            token = preset
            settings["access_token_customized"] = True
        else:
            token = DEFAULT_ACCESS_TOKEN
            settings["access_token_customized"] = False
        settings["access_token"] = token
        _persist(settings, save)
        return token

    # 已有令牌：补齐缺失的定制标记（旧版本数据升级）。只有显式 False 才算
    # 出厂初始状态；键不存在时按 True 处理。
    if "access_token_customized" not in settings:
        settings["access_token_customized"] = True
        _persist(settings, save)
    return token


def token_is_default(settings: dict) -> bool:
    """当前是否仍在使用出厂初始令牌（用于 UI 醒目提示改密）。"""
    return str(settings.get("access_token") or "").strip() == DEFAULT_ACCESS_TOKEN \
        and settings.get("access_token_customized") is False


def validate_custom_token(token) -> tuple:
    """校验用户自定义令牌，返回 (ok: bool, msg: str)。"""
    if not isinstance(token, str):
        return False, "令牌必须是字符串"
    token = token.strip()
    if not token:
        return False, "令牌不能为空"
    if len(token) < TOKEN_MIN_LEN or len(token) > TOKEN_MAX_LEN:
        return False, f"令牌长度需为 {TOKEN_MIN_LEN}~{TOKEN_MAX_LEN} 个字符"
    if not _TOKEN_RE.match(token):
        return False, "令牌只能包含字母、数字及 . _ @ - 字符"
    if token == DEFAULT_ACCESS_TOKEN:
        return False, "新令牌不能与出厂初始令牌相同"
    return True, ""


def is_loopback(host: str) -> bool:
    """客户端地址是否为本机回环（去掉可能的端口/IPv6 括号再判断）。"""
    if not host:
        return False
    client = host.split(",")[0].strip()
    if client in ("127.0.0.1", "::1", "localhost"):
        return True
    if client.startswith("["):
        # [::1]:8181 形态
        return client[1:].split("]", 1)[0] in ("::1", "localhost")
    if client.count(":") == 1:
        # 仅 IPv4:端口 形态才去端口；裸 ::1 含多个冒号不能切
        client = client.rsplit(":", 1)[0]
    return client in ("127.0.0.1", "::1", "localhost")


def _hostname_of(uri_or_netloc: str) -> str:
    """从 Origin/Referer/Host 中取纯主机名（去端口、去 IPv6 括号、小写）。

    飞牛桌面（:5666）与本面板（:8181）端口不同但主机相同，
    判定一律只比主机名不比端口。
    """
    raw = (uri_or_netloc or "").strip()
    if "://" in raw:
        netloc = urlparse(raw).netloc
    else:
        netloc = raw
    if netloc.startswith("["):
        host = netloc[1:].split("]", 1)[0]
    else:
        host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
    # 裸 IPv6 不带括号但带端口时上面已处理；常规 IPv4/主机名直接小写
    return host.lower()


def presented_token(headers) -> str:
    """从 Cookie / X-Access-Token / Bearer / 查询参数中提取调用方令牌。

    headers 为任意不区分大小写的映射（Starlette Headers 或测试 dict 包装）。
    """
    def _get(name: str) -> str:
        try:
            return headers.get(name) or ""
        except Exception:
            return ""

    val = _get(TOKEN_HEADER).strip()
    if val:
        return val
    auth = _get("authorization").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie = _get("cookie")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(TOKEN_COOKIE + "="):
            return part[len(TOKEN_COOKIE) + 1:].strip()
    # 查询参数由调用方在鉴权中间件里解析后通过 x-presented-query-token
    # 传入（保持本函数只依赖请求头，便于离线测试）
    return _get("x-presented-query-token").strip()


def browser_entry_allowed(headers) -> bool:
    """判断是否为"飞牛桌面入口 / 面板自身"的浏览器流量（免令牌）。

    纯函数，headers 需支持不区分大小写的 .get()。判定依据全部是浏览器
    自动生成、前端 JS 无法伪造的 Sec-Fetch-* 头，外加同主机 Referer 兜底。
    """
    def _get(name: str) -> str:
        try:
            return (headers.get(name) or "").strip()
        except Exception:
            return ""

    host = _get("host")
    host_name = _hostname_of(host)
    site = _get("sec-fetch-site").lower()
    dest = _get("sec-fetch-dest").lower()
    mode = _get("sec-fetch-mode").lower()

    # 面板页面加载后的同源 XHR/fetch（Sec-Fetch-Site: same-origin）
    if site == "same-origin":
        return True

    # 飞牛桌面以 iframe 嵌入，或从飞牛桌面新标签页打开：
    # 都是一次浏览器导航，且来源主机必须与面板主机相同（端口忽略）
    source = _get("origin") or _get("referer")
    if mode == "navigate" and dest in ("document", "iframe") and source:
        if host_name and _hostname_of(source) == host_name:
            return True

    # 旧版浏览器没有 Sec-Fetch-*：只有"带着同主机 Referer/Origin 的导航或
    # 页面子资源"放行，地址栏直输（无 Referer）仍要令牌
    if not site and not mode:
        if source and host_name and _hostname_of(source) == host_name:
            return True
    return False


def is_authorized(client_host: str, headers, expected_token: str,
                  require_token: bool = True) -> tuple:
    """综合判定请求是否放行。返回 (allowed: bool, reason: str)。

    reason 仅用于日志/401 文案，不含令牌内容。
    """
    if not require_token:
        return True, "token-disabled"
    if is_loopback(client_host):
        return True, "loopback"
    token = presented_token(headers)
    if token and expected_token and hmac.compare_digest(token, expected_token):
        return True, "token"
    if browser_entry_allowed(headers):
        return True, "browser-entry"
    return False, "token-required"


def is_public_path(path: str) -> bool:
    for p in PUBLIC_PATHS:
        if p.endswith("/"):
            if path.startswith(p):
                return True
        elif path == p:
            return True
    return False


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>window-composer · 访问验证</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #0d1117; color: #e6edf3;
         font-family: system-ui, "Microsoft YaHei", sans-serif; }
  .box { width: 420px; max-width: 92vw; background: #161b22;
         border: 1px solid #30363d; border-radius: 14px; padding: 32px; }
  h1 { font-size: 18px; margin-bottom: 8px; }
  p { font-size: 13px; color: #9da7b3; line-height: 1.7;
      margin: 10px 0; }
  input { width: 100%; padding: 11px 12px; margin: 8px 0 14px;
          background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
          color: #e6edf3; font-size: 14px; }
  button { width: 100%; padding: 11px; border: 0; border-radius: 8px;
           background: #2f81f7; color: #fff; font-size: 14px;
           cursor: pointer; }
  button:hover { background: #1f6feb; }
  .err { color: #f85149; font-size: 13px; min-height: 18px;
         margin-bottom: 8px; }
  .default-warn { display: none; background: #3d2e00;
         border: 1px solid #9e7b14; color: #f0d98a;
         font-size: 12.5px; line-height: 1.7; border-radius: 8px;
         padding: 10px 12px; margin-bottom: 12px; }
  code { background: #0d1117; padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<form class="box" onsubmit="return doLogin(event)">
  <h1>window-composer 访问验证</h1>
  <div id="default-warn" class="default-warn"></div>
  <p>面板已开启访问令牌保护。从飞牛桌面打开无需令牌；直接用 IP 访问
     请输入在面板「访问令牌」中查看/设置的令牌。</p>
  <input id="tok" type="password" autocomplete="off"
         placeholder="请输入访问令牌" autofocus>
  <div class="err" id="err"></div>
  <button type="submit">验证并进入</button>
  <p>令牌也可用查询参数携带：<code>?wc_at=令牌</code>，
     或请求头 <code>X-Access-Token: 令牌</code>。</p>
</form>
<script>
(async function () {
  // 公开接口：若仍是出厂初始令牌，给出首次登录提示（不泄露自定义令牌）
  try {
    const r = await fetch('/api/access/status');
    const d = await r.json();
    if (d.ok && d.token_is_default) {
      const w = document.getElementById('default-warn');
      w.style.display = 'block';
      w.textContent = '当前仍为出厂初始令牌（admin123），可直接输入登录。'
                    + '进入面板后请立即在「访问令牌」卡片修改为自定义令牌。';
      document.getElementById('tok').placeholder = '出厂初始令牌：admin123';
    }
  } catch (ex) { /* 状态查询失败不阻塞登录页 */ }
})();
async function doLogin(e) {
  e.preventDefault();
  const tok = document.getElementById('tok').value.trim();
  const err = document.getElementById('err');
  if (!tok) { err.textContent = '请输入令牌'; return false; }
  try {
    const r = await fetch('/api/access/login?token=' + encodeURIComponent(tok),
                          { method: 'POST' });
    const d = await r.json();
    if (d.ok) { location.href = '/'; return false; }
    err.textContent = d.msg || '令牌不正确';
  } catch (ex) { err.textContent = '请求失败：' + ex; }
  return false;
}
</script>
</body>
</html>"""
