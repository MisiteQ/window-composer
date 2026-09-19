# 开发文档 window-composer
> 面向接手开发人员：项目设计思路、模块职责、扩展方案、坑点、调试方法
> 注：显示栈已从早期 Weston+Xwayland 方案**迁移到 Xorg + openbox**。Weston 时期
> 的关键踩坑仍保留在第 6 节（部分结论对同类容器图形项目有参考价值）。

## 1. 项目目标与背景
本项目是飞牛NAS上的Docker容器应用。
解决痛点：NAS自带显示输出，希望可以远程网页管理GUI程序——把飞牛应用的Web界面渲染到物理显示器、播放视频、多窗口布局、断电重启自动恢复，不需要接键盘鼠标操作本地显示器。

**显示接口无关性**是当前版本的核心目标：HDMI / DisplayPort / USB-C(DP Alt Mode) / VGA / DVI / eDP / DSI
在内核里都是 DRM 上的 connector，Xorg 的 modesetting 驱动对接口类型无感。
因此代码里不写死任何输出名（早期版本硬编码 `HDMI-2`，在只有 VGA/DP 的机器上旋转与回屏会静默失效），
全部输出由 `xrandr --current` 运行时枚举 + `classify_output()` 分类。

选型思考（当前方案）：
- **Xorg + modesetting/DRM**：直接输出到物理显示器（接口类型无关），分辨率跟随显示器 EDID；打开 DRM master 需要 root 与 /dev/dri。相比 Weston+Xwayland，X11 窗口位置请求不再被 WM 覆盖
- **openbox**：轻量 X11 窗口管理器，完整支持 EWMH/ICCCM，`xdotool windowmove/windowsize` 精确生效（rc.xml 中 resistance=0、placement=undermouse 允许自由摆放）。这是弃用 Weston 的核心原因：Weston 的 Xwayland WM 强制覆盖所有 X11 窗口位置（windowmove 与 chromium --window-position 均无效），无法实现"画框布局、边缘吸附、多窗口不重叠"
- **PipeWire + pipewire-pulse**：现代音频服务，pipewire-pulse 暴露 PulseAudio 兼容接口，pactl 管理设备。不用 WirePlumber（容器内无 system dbus + 无 logind 会 crash-loop）
- **FastAPI**：轻量Python Web框架，API简单，适合作为控制后台，不做画面渲染
- **前端**：原生JS+Jinja模板，不引入Vue/React，降低容器依赖
- **chromium**：飞牛应用显示引擎。飞牛应用的界面是 Web UI，必须用浏览器渲染到 HDMI；代码不硬编码浏览器，launch_web_app() 自动检测 chromium/firefox 等

核心约束：**Web服务不能是系统核心，Web挂掉画面和音频不能挂**。
所以架构设计为：Xorg、openbox、PipeWire 独立常驻，Web只是外部控制接口。

## 2. 模块职责拆分
### entrypoint.sh（容器入口，系统启动脚本）
职责：
1. 按实际 uid 创建 XDG_RUNTIME_DIR（root=/run/user/0，uid1000=/run/user/1000）、/tmp/.X11-unix、初始化日志
2. 通过 jq 读取 /data/settings.json 配置：web端口、背景色、开机自启标记
3. **日志轮转**：rotate_log() 检查 /data/app.log 体积，超过 LOG_MAX_BYTES（默认 2MB）只保留尾部 LOG_KEEP_LINES（默认 2000）行；启动时执行一次，之后在监控循环里每约 60 秒检查一次
4. **显示设备探测**（`detect_drm_card()`）：扫 `/sys/class/drm/card[0-9]*`，跳过 `card0-HDMI-A-1` 这类连接器目录，
   按连接器 `status == connected` 挑一张"真的有显示器接在上面"的卡；多显卡机器（板载 BMC VGA + iGPU）不会选到空卡；
   sysfs 拿不到信息时退回 `card0`，一个 DRM 设备都没有则返回空串（降级虚拟输出）。
   `SYS_DRM` / `DEV_DRI` 可用 `WC_SYS_DRM_ROOT` / `WC_DEV_DRI_DIR` 重定向，供 tests/test_entrypoint_drm.sh 用假 sysfs 树覆盖
5. **Xorg 配置运行时生成**：卡号随宿主机变化，把配置写死在镜像里意味着"换台机器就要重打镜像"。
   启动时生成 `modesetting.conf`（`kmsdev /dev/dri/$DRM_CARD`）与 `dummy.conf`；
   也可用 `WC_XORG_CONF` 指定用户自带配置（特殊显卡/时序）
6. **三级降级启动 Xorg**：modesetting → modesetting + `-sharevts -novtswitch` → dummy 虚拟输出；
   前两级失败仍能用虚拟屏把容器拉起来，保证控制面板可用。实际模式写入 `WC_XORG_MODE`（drm/virtual），
   供 Web 层区分"虚拟输出"与"探测失败"
7. 顺序启动 dbus session → dbus system → PipeWire → **WirePlumber** → pipewire-pulse → 默认输出纠偏（后台）→ **Xorg（:0）** → **openbox（/etc/xdg/openbox/rc.xml）** → xsetroot 背景色 → FastAPI
8. 主循环监控 Xorg / PipeWire / WirePlumber / pipewire-pulse / openbox / Web：
   - Xorg 或 PipeWire 退出 → 容器终止（核心服务不可恢复）
   - WirePlumber、pipewire-pulse、openbox 退出 → 自动重启（WP 退出只让音频降级为 Dummy，不终止容器）
   - Web 退出 → 5 秒后重启（不影响 Xorg/音频）
> 关键：Web 服务挂掉主循环不退出，Xorg/PipeWire 继续运行，符合「显示服务常驻优先」原则。

**容器内 WirePlumber 存活三要素（飞牛真机实测，strace 定位）**：
1. dbus 系统总线：容器无 systemd，entrypoint 用 `dbus-daemon --system --fork` 手动拉起；
   启动前 Ping 探活，docker restart 后清掉残留 socket/pidfile 再启动（host 网络下
   容器与宿主共享 netns，情况更复杂，探活比单纯判断 socket 文件可靠）
2. machine-id：`dbus-uuidgen --ensure` 写 /etc/machine-id 与 /var/lib/dbus/machine-id
3. /run/systemd/{users,seats,sessions} 占位目录：logind 内置插件启动即 inotify_add_watch
   这些路径，缺失时返回 ENOENT 直接中止，wireplumber 随即与 PipeWire 断开退出
   （0.4.13 上这是致命错误不是 WARN）
ALSA 设备枚举还需要宿主 udev 数据库：compose/docker run 必须挂 `/run/udev:/run/udev:ro`，
否则即使 WP 存活，pactl 里仍只有 auto_null（media-session 方案同样依赖 udev，已排除）。
默认输出纠偏：多声卡机器上 WP 可能把 pcspkr 主板蜂鸣器选成默认 sink，entrypoint
启动时优先应用 settings.default_audio_sink（面板持久化的用户选择），否则避开
pcspkr/beep/dummy/auto_null 挑第一张真实声卡。

其他关键点：
- 启动 Xorg 前 `rm -f /tmp/.X11-unix/X[0-9]*` 与 `X[0-9]*-lock`，避免 docker restart 后残留 socket 导致显示号递增
- 启动时记录 `list_connectors()`（内核看到的全部连接器与状态）与 `xrandr --current` 摘要，
  排障时第一眼就能看出"接口认到了没有"
- **依赖自检**：check_deps() 校验 Xorg/openbox/xdotool/xwininfo/xprop/xrandr/wmctrl/scrot/dbus-launch/dbus-daemon/dbus-uuidgen/pipewire/wireplumber/pipewire-pulse/pactl/jq/python3，
  缺什么直接写进日志（镜像已全部预装，自检是改造精简版时的护栏）
- Xorg 日志写 /data/xorg.log；openbox 日志写 /data/openbox.log；WirePlumber 日志写 /data/wireplumber.log（均轮转）；应用日志统一写 /data/app.log
- 开机自启 auto_start：挂载 docker.sock 时由 `app_scanner.set_self_restart_policy()`
  **实时同步 Docker 重启策略**（false→no，true→unless-stopped，走 unix socket 上的
  Docker Engine HTTP API）；host 网络 + cgroup v2 下容器 ID 经 `/proc/1/mountinfo`
  的 `/docker/containers/<64hex>/` 绑定挂载发现（HOSTNAME 不是 12hex、cgroup 路径为空）。
  未挂 socket 时降级为纯配置记录
- 容器以 root + privileged 运行；`/dev/input` 不需要单独挂（privileged 覆盖），
  但 `/run/udev` 必须只读挂载（物理音频的 ALSA 枚举数据源）

### src/config.py
职责：持久化数据读写封装，所有json配置、layout、日志、pid注册表读取统一放这里。
- load_settings / save_settings：系统设置（含 web_port、bg_color、auto_start、default_audio_sink、display_width、display_height、screenshot_interval；load_settings 用 setdefault 补字段，旧 settings.json 自动兼容）
- load_layout / save_layout：窗口布局（开机自动恢复的"当前布局"）
- load_profiles / save_profiles：命名布局方案 /data/layout_profiles.json（用户保存的多套可切换方案，与主布局分离）；文件损坏/非 dict/值非 list 均兜底为空 dict 或剔除脏条目
- load_pids / save_pids：pid → app_name 注册表（Web 崩溃重启后仍能识别已运行应用）
- SCREENSHOT_DIR=/data/screenshots：定时截图目录（区别于手动截图 /data/screenshot.png）
- read_logs：读取日志文件尾部内容
- log_line(msg, level)：向 /data/app.log 追加一行应用层日志（与 entrypoint 同格式、同一份文件）。
  每次调用独立 `open(append)`，不持有长期句柄——entrypoint 的轮转是
  `tail > f.rotating && mv f.rotating f`，长句柄会继续写进已被 unlink 的 inode，
  轮转后新文件不再增长（日志"凭空消失"）。写失败必须静默，日志不能拖垮业务接口。
> 所有持久化文件都放在 /data（FNWC_DATA_DIR 可重定位，便于本地调试），不要写死在容器内只读层，容器重建会丢失。

### src/desktop_env.py（桌面会话环境探测）
职责：动态探测 DISPLAY / WAYLAND_DISPLAY / XDG_RUNTIME_DIR，供 window_manager 与 app_runner 共用。
- 背景坑：容器 `docker restart`（非 recreate）后 /tmp/.X11-unix 里残留 X0/X1 socket 文件，X server 显示号递增；写死 `DISPLAY=:0` 会导致所有 X11 应用 "Can't open display" 启动即死、xdotool 全部失败。
- detect_display()：读 `/proc/net/unix`，在 `/tmp/.X11-unix/X<n>` 中找内核 socket 表里真实存在（有进程持有）的最大编号；进程死后残留的 socket 文件不会出现在该表中。带 2 秒 TTL 缓存。
> entrypoint.sh 启动 Xorg 前也会 `rm -f /tmp/.X11-unix/X[0-9]*` 从源头避免递增；Python 探测是热更新/Web 重启场景的自愈兜底。

### src/app_scanner.py（已安装应用扫描器：飞牛应用 + Docker 容器）
职责：给后台"已安装应用"列表提供数据，扫描飞牛 NAS 应用目录与宿主 Docker 容器，**不内置任何写死的业务程序名单**。
数据来源（Docker 卷挂载，缺一不可的成组挂载）：
- /host_apps ← 宿主 /var/apps（应用根目录）
- /hostproc ← 宿主 /proc（运行状态判定）
- /vol1~/vol4/@appcenter、/usr/local/apps/@appcenter ← target 绝对软链的实体目录
- /var/run/docker.sock ← 宿主 Docker 守护进程（容器扫描）

> **软链陷阱（真机踩过）**：`/var/apps/<appname>/target` 是**绝对软链**——第三方
> 应用指向 `/volN/@appcenter/<appname>`（N 随安装所在存储卷），内置 trim.* 指向
> `/usr/local/apps/@appcenter/<appname>`。只挂 /var/apps 时容器内软链断裂：
> 扫得到应用名却读不到 ui/config，Web 入口全空。entrypoint.sh 的 `check_host_apps()`
> 会遍历 `/host_apps/*/target` 统计断裂数并 WARN；`host_apps_mounted()`（判
> /host_apps 是否存在）随 `/api/apps/installed` 响应返回，前端在飞牛应用为 0 时
> 显示黄色排障条。

每个飞牛应用结构：
  /var/apps/<appname>/manifest（display_name/source/version/service_port，key=value 格式，${common.xxx} 变量取自 i18n/zh 的 [common] 节）
  /var/apps/<appname>/target/ui/config（UI 入口：type: url|iframe, port, url；target 为绝对软链）

**飞牛应用**
- 端口优先级：ui/config 的 port > manifest 的 service_port；URL 由 port + url_path 组装（经 docker0 网关 172.17.0.1 访问宿主，可用 WC_FNOS_PORT / WC_HOST_GATEWAY 环境变量覆盖）
- 运行状态：读宿主 /proc 全量 cmdline，按应用路径/目录名匹配
- 应用清单带 10 秒 TTL 缓存，`/api/apps/installed?refresh=1` 强制重扫

**Docker 容器（分组「Docker 应用」）**
- 只走 Docker Engine HTTP API —— stdlib `http.client` 覆写 `connect()` 接到 unix socket，**镜像里不装 docker CLI**。这是"装完即用、NAS 端不再下载任何东西"的前提。
- 请求路径**不带 /vX.Y 前缀**，交给守护进程按自身版本回应，跨飞牛版本的 Docker 差异不会 404。
- 端口挑选优先级：常见 Web 端口（80/8080/3000…）> 其它已发布 TCP 端口 > TLS 端口（443/8443 回 https）。只认已发布的端口（PublicPort>0）——exposed 但没映射到宿主的端口，面板根本访问不到。
- 与飞牛应用去重（`_match_fnos_app()`，否则同一应用会在两个分组里各出现一次），判定优先级：
  1. **宿主发布端口**与飞牛应用 service_port 相同 —— 最可靠（真机：飞牛目录名
     `FnMessageBot` 是驼峰，容器名却是 `fn-message-bot`，名称完全对不上但端口都是 18230）；
  2. 容器名/compose project 与飞牛应用名原名精确，或以 `<应用名>-` / `_` 加前缀后缀
     （compose 给服务名补序号，如 jellyfin-1）；
  3. `_norm_app_name()` 归一后同名（小写字母/数字与大写字母之间插分隔符，再去掉
     `- _` 和空白并转小写：FnMessageBot = fn-message-bot = fn_message_bot → fnmessagebot）。
- 命中飞牛条目的容器不重复展示，并把 Docker Engine 报告的 running 状态**回填**给飞牛
  条目：容器型应用（fnpackup / FnMessageBot 等）的进程 cmdline 不含 /var/apps 路径，
  `/proc` 匹配会漏报，容器状态才是权威来源。缓存的是去重前原始列表，合并每次实时做。
- 排除自身容器：hostname（Docker 默认设为容器短 id）+ `/proc/self/cgroup` 里的 64 位 id + 容器名兜底。
- `docker_available()` 必须用 `S_ISSOCK` 判断，不能只看 `exists`：宿主没有 docker.sock 时，Docker 会按 bind mount 在那个路径**建一个空目录**，存在性检查会误判成"已挂载"。
- socket 缺失 / 请求失败 → 返回空列表并整体静默降级，飞牛应用扫描与面板功能都不受影响；接口另返回 `docker_available` / `docker_count` / `host_apps_mounted` 供前端提示「Docker 未接入」与飞牛挂载缺失。
> 本扫描器只提供元信息与运行状态，不负责启动/显示；显示走 /api/app/launch_web（浏览器渲染）。

### src/app_runner.py
职责：管理GUI应用进程生命周期 + pid 注册表持久化
- start_local_app(app_name, args)：subprocess启动程序（支持 shlex 解析参数），env 使用 desktop_env.desktop_env()（动态 DISPLAY），登记 pid_app_name 并写盘
- launch_web_app(url, app_name, x, y, w, h, fullscreen)：浏览器渲染飞牛应用。自动检测浏览器（chromium 系用独立 user-data-dir 保证多窗口独立进程 + --window-position/--window-size；firefox 用 --geometry）；fullscreen=True 且未给几何时取整屏几何（动态探测分辨率，不硬编码）；若已存在同名窗口先关闭再以新几何重启；启动后由 _enforce_geometry 后台线程处理几何——fullscreen 时窗口一出现就发 EWMH 全屏并结束，非全屏时才做约前 4 秒的多次 xdotool 纠正（chromium 会从 profile 恢复旧窗口位置）
- stop_by_pid：先 SIGTERM 3 秒后升级 SIGKILL；无论 pid 是否在注册表都尝试 terminate，再清理注册表
- stop_all：批量终止所有应用进程（不杀Xorg/openbox/PipeWire）
- stop_app_by_name：按 "web:应用名" 停止
- restore_app(app_name, args, x, y, w, h)：布局恢复入口。web: 前缀 → args 约定为 URL 本身，几何透传 launch_web_app；本地程序 → start_local_app（几何由调用方窗口出现后设置）
- get_app_name_by_pid：优先查注册表，缺失时回退到 psutil.cmdline() 兜底
- get_app_args_by_pid：读取进程 cmdline 第二个参数起，用于本地程序布局保存
- get_web_url_by_pid：web 应用取 URL——优先 pid_url 内存表，Web 重启后内存丢失时从 psutil cmdline 扫 http(s) 参数兜底
- probe_app_alive(pid, timeout)：启动后短暂存活校验（默认 0.6s）。程序不存在/非 X11/参数错误时子进程会秒退，提前发现才能给前端明确提示；只用于手动启动 API，布局恢复流程不用（避免拖慢恢复）
- list_pid_registry / reconcile_pids：注册表列出与启动时清理死 pid
- `_invalidate_window_cache()`：start_local_app / stop_by_pid / launch_web_app 成功后失效 wm 的窗口列表短缓存（延迟 import wm 单例，避免 window_manager ↔ app_runner 循环导入），保证前端下一次轮询立刻看到变化
> 关键：pid_app_name 在启动时从 /data/pids.json 加载，每次 start/stop 都同步写盘。
> 这是「Web 崩溃不影响 Xorg/PipeWire」原则的关键支撑——Web 重启后仍能识别已有应用。

### src/window_manager.py
职责：X11 窗口与显示输出底层操作封装（xdotool + xprop + xwininfo + xrandr + wmctrl + scrot）

**显示输出（通用多接口）**
- `classify_output(name)`：把 X 输出名翻译成 `(接口代号, 可读标签)`。规则表 `_CONNECTOR_TYPES`：
  `HDMI-*`→HDMI、`DP-*`/`DisplayPort-*`→「DisplayPort / USB-C」、`VGA-*`、`DVI-*`、
  `eDP-*`/`LVDS-*`→内置屏、`DSI-*`→MIPI DSI、`Virtual-*`/`DUMMY-*`→虚拟输出，未知归 `Other`（不丢信息，前端连原名一起展示）
- `get_outputs()`：解析 `xrandr --current`，返回**全部**输出（含未连接/未启用）。
  每项：name / connected / primary / enabled / width,height,x,y / rotation / connector / type_label /
  mm_width,mm_height（EDID 物理尺寸）/ modes / preferred。
  **判"是否已启用"的依据是有没有 `wxh+x+y` 几何**——只有 `connected` 而没几何 = 接了屏但还没 `--auto`
- `resolve_output()`：选目标输出，优先级 显式 name → `self.target_output` → primary → 首个已连接输出；disconnected 一律不选
- `set_target_output()`：只登记选择，**不在这里调 xrandr**（选一个没插屏的口时避免静默失败）；真正激活交给 `/api/display/set_output` + display_monitor
- `get_screen_geometry()`：整个 X 屏幕（根窗口）尺寸＝所有已启用输出的外接矩形（xdotool getdisplaygeometry，兜底 xdpyinfo，最后 1920×1080）
- `get_display_geometry()`：**目标输出**的几何，含它在 X 屏幕中的原点 `(x, y)`。多显示器坐标系的基石：
  单屏时返回 `(0,0)` 与旧行为完全一致；双屏时窗口会被摆到用户选定的那块屏，而不是默认贴整个 X 屏幕左上角
- `get_visible_area()`：所有**已启用**输出的外接矩形——"窗口真正可见的范围"。旋转/改分辨率/切屏后
  把窗口拉回这个范围，才不会出现"坐标合法、但屏幕上看不见也点不到"
- `enable_output(name)` / `auto_enable_connected()`：对"已连接但未启用"的输出执行 `xrandr --auto`（热插拔点亮）
- `_grow_framebuffer(need_w, need_h)`：必要时 `xrandr --fb` 扩大 framebuffer，容纳比当前更大的输出
- `get_output_info()`：后端探测，返回 backend=`drm`/`virtual`/`headless`、output / output_type / output_label / outputs[] / card

**轮询短缓存（TTL + 写后失效）**
前端 3 秒轮询会高频打 `get_outputs` / `list_windows`，而一次 list_windows 对每个窗口
要跑 xwininfo + 多个 xprop；热插拔/状态查询同理。约定：
- `OUTPUTS_TTL=0.5s`（get_outputs，force=True 可绕过）、`WINDOWS_TTL=0.8s`（list_windows），
  缓存返回 dict 副本防止调用方污染
- 所有写操作成功后必须显式失效：enable_output/set_rotation → invalidate_outputs；
  set_window_rect/set_fullscreen/set_minimized/set_above 及启停应用 → invalidate_windows
- audio_manager 同款约定：SINKS_TTL=2.0s，set_default_sink/set_sink_volume/set_sink_mute
  及路由操作后 invalidate_sinks
> 测试打桩换 subprocess 后必须同步调 invalidate_*，否则会读到上一份桩数据的缓存
> （tests/test_offline.py 里每个换桩点都有对应注释）。

**窗口操作**
- list_windows：`xdotool search --onlyvisible --class .` 列出可见窗口 ID；几何优先用 **xwininfo 的 Absolute upper-left X/Y**（xdotool getwindowgeometry 在 openbox 重父化后报相对 frame 的坐标，会偏移）；`xprop _NET_WM_PID` 取 PID；**子窗口过滤**：WM_TRANSIENT_FOR 属性排除对话框，宽高 <50px 过滤装饰窗口；按 PID 去重保留面积最大主窗口。无 _NET_WM_PID 的古老应用用窗口 ID 作临时 pid。条目额外带 visible/fullscreen/above 字段
- **最小化窗口补全**：--onlyvisible 拿不到最小化窗口，list_windows 再遍历
  `list_pid_registry()` 里 alive 但不在可见集合的 pid，经 `_hidden_window_for_pid`
  用 `xdotool search --pid` 找回真实窗口（找不到时 wid 回退为 str(pid)，此情况丢弃，
  不造幽灵条目），以 visible=False 补进列表，前端才有"恢复窗口"入口
- set_window_rect：`xdotool windowmove --sync` + `windowsize --sync`。windowmove 定位的是 frame 左上角，用 `_NET_FRAME_EXTENTS` 反向补偿装饰边框，客户区精确落位。**Xorg+openbox 下位置与尺寸均精确生效**（对比 Weston 方案只生效尺寸）。**全屏窗口的几何请求会被 openbox 忽略**：移动前先读 `_NET_WM_STATE`，含 FULLSCREEN 原子时先 `wmctrl -b remove,fullscreen`、sleep 0.15s 等 WM 异步还原几何，再重新定位 wid 并移动，否则会出现"接口返回成功、画面纹丝不动"
- set_fullscreen(pid, fullscreen)：`wmctrl -i -r <wid> -b add/remove,fullscreen` 发送 EWMH ClientMessage，真全屏 (0,0) 铺满、退出恢复原几何
- set_minimized(pid, minimized)：最小化走 `xdotool windowminimize`；恢复走 `windowactivate --sync` + `windowraise`（只 map 不 raise 可能被别的窗挡住，用户以为恢复没反应）
- set_above(pid, above)：`wmctrl -b add/remove,above`，EWMH 置顶，与全屏同理必须走 ClientMessage
- is_fullscreen/is_above：xprop 读 `_NET_WM_STATE` 后用 `_state_has_fullscreen/_state_has_above` 判断
- take_timed_screenshot：定时截图写 config.SCREENSHOT_DIR（shot-YYYYmmdd-HHMMSS.png），`_prune_timed_shots` 按 mtime 只留最新 TIMED_SHOT_LIMIT=200 张；手动 take_screenshot 仍写 /data/screenshot.png
- clamp_rect(x, y, w, h, sw, sh, min_px)：把几何钳进给定矩形（尺寸不超、位置钳到 `[原点, 原点+尺寸-窗口尺寸]`）
- clamp_windows_to_screen()：对所有窗口按 `get_visible_area()` 钳制，返回被修正的窗口数。
  实现上先在"可见区局部坐标"里调用 clamp_rect，再平移回屏幕绝对坐标——复用同一套几何数学，
  保证与布局恢复的行为一致。仅在确实越界时调 set_window_rect，避免无意义地扰动其他窗口
- get_rotation/set_rotation：xrandr 读取/设置**目标输出**的旋转（0/90/180/270，90/270 宽高互换）
- `_get_connected_output()`：返回目标输出名；没有任何已连接输出时返回空串（早期版本回退硬编码 `"HDMI-2"`，
  在只有 VGA/DP 的机器上所有 xrandr 调用都打到不存在的输出上，旋转/回屏静默失效）
- get_window_class(wid)：`xprop WM_CLASS` 取类名，layout save 时 app_name 回退
- take_screenshot：scrot 直接写 /data/screenshot.png（按返回码判定成功）
> 这一层是和窗口系统交互的边界。若更换合成器/WM，只需修改此文件命令调用，上层 API 保持不变。

### src/display_monitor.py（显示器热插拔守护）
职责：后台线程轮询 `xrandr`，把"开机时还没插显示器 / 中途换接口 / 拔了又插"这些宿主侧变化
自动收敛到可用状态，**不需要重启容器**。
- `snapshot()`：抓一次全部输出的关键状态（name/connected/enabled/w/h/x/y/rotation）作为比对基线
- `visible_area()`：当前可见区快照
- `diff(before, after)`：产出事件列表，事件类型如 `output_connected` / `output_disconnected` /
  `output_enabled` / `output_disabled` / `resolution_changed` / `rotation_changed` / `target_changed`；
  前端据此弹提示（`OUTPUT_EVENT_TOAST`）
- `check_once(first=False)`：一轮检查——比对快照 → 对新出现的"已连接但未启用"输出执行 `xrandr --auto` →
  目标输出丢失时重选 → 可见区变化时调 `clamp_windows_to_screen()` 把越界窗口拉回 → 记录事件
- `_loop()`：后台循环。**吞掉所有异常**并继续下一轮——守护线程一旦因单次异常死掉就永久失效，
  而这类问题表现为"热插拔再也不生效"，极难排查
- `build_monitor(wm, logger)`：按 `WC_DISPLAY_POLL_INTERVAL` 构造；**≤0 关闭轮询**
  （排障时可先关掉自动行为，只手动调 `/api/display/refresh` 观察）
- 状态通过 `/api/status` 的 `display_monitor` / `display_events` 暴露给前端
> 为什么用轮询而不是 udev：容器里 `netlink`/`/run/udev` 的可达性依赖宿主映射，
> 而 `xrandr` 是已经在用的依赖，轮询同一条信息源最省事也最不容易在别人机器上失效。


### src/audio_manager.py
职责：基于 pactl（PulseAudio CLI）的音频设备管理
- list_sinks：解析 `pactl list sinks` 输出（Name/Description），默认 sink 用 `pactl get-default-sink` 标记；每个 sink 额外解析 volume（取首个声道的百分比，钳 0-100，读不到为 -1）与 muted（Mute: yes/no）；结果带 SINKS_TTL=2.0s 短缓存
- set_default_sink：`pactl set-default-sink`（写后 invalidate_sinks）
- set_sink_volume(sink_id, volume)：`pactl set-sink-volume <name> <N%>`，入参钳到 0-100、非数字拒绝；set_sink_mute(sink_id, muted)：`pactl set-sink-mute <name> 1/0`；均写后失效缓存
- list_sink_inputs：解析 `pactl list sink-inputs` 每个流块的 index / Sink / `application.process.id`（pid）/ `application.name`
- move_sink_input / move_all_sink_inputs：`pactl move-sink-input <index> <sink>`
- route_process_audio(pid, sink)：两步——先把 default sink 切到目标（影响后续新流），再按 pid 匹配已在播放的 sink-input 迁移（**只切 default sink 对已经在播放的应用完全无效**，必须 move-sink-input）
> 容器内 WirePlumber 在无 system dbus + 无 logind 时会 crash-loop，改用 pipewire-pulse + pactl。
> 解析逻辑有离线单测保护（tests/test_offline.py），改正则后务必跑一遍。

### src/main.py FastAPI主程序
职责：
1. 提供REST API，接收前端请求，调用上面几个模块
2. startup事件：记录启动日志 + 自动异步执行布局恢复任务 restore_layout_task
3. Jinja2模板渲染首页
4. 文件接口返回截图
5. 兜底异常处理器：未捕获异常写进 /data/app.log（否则接口 500 只在容器 stdout 一闪而过，网页日志里查不到）
6. 关键事件写日志：应用拉起/停止、布局恢复结果、旋转、音频切换、截图失败

apply_layout(layout_override=None) ——布局恢复/应用布局的统一实现（容器启动、网页按钮、
应用命名方案共用）：传 layout_override 时只按它临时摆放窗口，不读写 layout.json
（/api/layout/apply_profile 用）；不传则读"当前布局"。
1. load_layout 读取布局；空则直接返回 total=0
2. 读当前屏幕尺寸，把每项几何按 clamp_rect(..., MIN_WINDOW_PX) 钳进当前屏幕（旧分辨率的布局不会把窗口摆到屏幕外）
3. 一次性快照 wm.list_windows()，逐项判断：
   - 屏幕上已有该应用（`_find_window_for_layout_item`：先按 app_name 精确匹配，web 应用再用 URL 兜底匹配 cmdline）→ 若该窗口 visible=False（被最小化）先 set_minimized(pid, False) 恢复，再**只重定位，不重启应用**
   - 没有 → restore_app 拉起（web 应用透传几何+fullscreen）→ _wait_window_appear 轮询窗口出现（最长 10 秒）→ fullscreen 则 set_fullscreen，否则 set_window_rect → route_process_audio（按 pid 迁移该进程已有的流）
4. 返回 {total, started, repositioned, failed, details[...]}

**命名布局方案 profiles**：
- 存储 /data/layout_profiles.json（config.load_profiles/save_profiles），与开机恢复用的 layout.json 分离
- `_build_layout_from_windows()`：保存布局与保存方案共用的"从当前窗口拍照"逻辑，
  跳过 visible=False（最小化）、宽高为 0、无 app_name 的条目；fullscreen 优先取窗口实时状态
- `_normalize_profile_name()`：非空、≤40 字符、无控制字符；重名保存=覆盖
- 四个路由 GET /api/layout/profiles、POST save_profile/apply_profile/delete_profile，
  apply 复用 _layout_lock 串行化

**定时截图后台循环**：
- `_screenshot_loop(stop_event)`：模块级 asyncio task（lifespan 启动、finally set stop），
  每 _SHOT_TICK=1 秒读一次 settings.screenshot_interval（>=10 才截图，0=关闭），
  在 executor 里跑 scrot（不阻塞事件循环），异常只记日志——截图失败绝不能拖死后台循环
- 截图存 SCREENSHOT_DIR，take_timed_screenshot 内部按 mtime 裁剪到最新 200 张
- 设置经 POST /api/settings/set_screenshot_interval 持久化（0 或 10~3600），
  最多 1 秒后生效，无需重启

restore_layout_task()（容器启动）：
reconcile_pids() 清理死 pid → 预设 default sink → `async with _layout_lock: apply_layout()` → 写汇总日志
> `_layout_lock` 串行化：容器启动恢复与用户点【应用已保存布局】可能撞车，并发拉起同一应用会互相顶掉窗口。

**lifespan 取代 startup 事件**：`@app.on_event("startup")` 已废弃（FastAPI 会告警），
改用 `lifespan` 上下文管理器（`_on_startup()`）。`_display_monitor = build_monitor(wm, ...)` 在模块级构造。

**多显示器坐标系（关键设计）**
布局坐标系以**目标输出的左上角**为原点，而不是整个 X 屏幕。单屏时原点就是 `(0,0)`，与旧行为完全一致；
多屏时窗口会被摆到用户选定的那块屏上。为此一组坐标换算辅助函数贯穿所有涉及几何的接口：
- `_output_origin()`：目标输出在 X 屏幕中的 `(ox, oy)`
- `_to_local(x, y)`：屏幕绝对坐标 → 布局局部坐标
- `_to_screen(x, y)`：布局局部坐标 → 屏幕绝对坐标
- `_public_outputs()`：把 `wm.get_outputs()` 精简成前端需要的字段

具体落点：
- `/api/status`：返回 output_name/type/label、output_x/output_y、target_output、outputs[]、drm_card、display_events、display_monitor
- `/api/windows`：同时返回**局部坐标**（x/y，给画布用）与**屏幕绝对坐标**（screen_x/screen_y）以及 origin
- `/api/window/setrect`、`/api/app/launch_web`：收到的前端坐标是局部坐标，写窗口前先 `_to_screen` 平移
- `/api/layout/save`：存**局部坐标**（换块屏/换分辨率后仍能正确还原）
- `apply_layout()`：读出的局部坐标先 `_to_screen(ox, oy)` 平移再落到窗口

**显示输出相关接口**
- `GET /api/display/outputs`：全部输出（含未连接/未启用），供面板选择目标显示器
- `POST /api/display/refresh`：立即重探并自动点亮"已连接但未激活"的接口
- `POST /api/display/set_output`：切换目标显示器（写 settings.target_output，`wm.set_target_output` 后重探 + 回屏）

> 优先级：面板里选的目标显示器（`settings.target_output`）优先于启动时的 `WC_TARGET_OUTPUT` 环境变量；
> 环境变量只在用户还没在面板里选过时生效（无面板场景的兜底）。

接口层的兜底逻辑（不做的话只靠前端约束，直接用 curl/脚本调 API 就会出问题）：
- `/api/window/setrect`：x/y/w/h 后端再钳制一次（不小于 MIN_WINDOW_PX、不超出屏幕），返回钳制后的实际值
- `/api/window/fullscreen`：失败时写 WARN 日志，不再静默返回 ok=False
- `/api/display/rotate`：旋转后调 wm.clamp_windows_to_screen()，把越界窗口拉回屏幕内，响应里带 reflowed 数量
- `/api/app/start`：启动后 probe_app_alive 校验，秒退的进程清理注册表并返回失败原因（exited_pid 供排查）
- `/api/app/launch_web`：fullscreen 参数（未给几何时整屏）
- `/api/audio/set_sink`：migrate_existing 默认开启，切换设备时把已有播放流一并迁移，返回 moved 数量
- `/api/audio/set_volume` / `set_mute`：volume 后端再 int 化并钳 0-100；失败（pactl 报错）回 ok=False
- `/api/window/minimize` / `activate` / `set_above`：窗口状态操作，pid 找不到窗口时回 ok=False
- `/api/settings/set_screenshot_interval`：只接受 0 或 10~3600，其余（含非整数）回 ok=False
- profiles 三个写接口：方案名统一过 `_normalize_profile_name`；不存在/无可保存窗口/布局锁占用都有明确提示
- `/api/layout/restore`：占用 _layout_lock 时立刻返回"正在进行中"；无已保存布局时提示先保存
- `/api/display/set_output`：切换目标输出后必须**重探 + 回屏**（新屏尺寸/原点可能不同），并写回 settings
- `/api/display/refresh`：手动触发一次守护逻辑，返回事件列表（自动检测关闭时的替代手段）
- `/api/window/setrect` / `/api/app/launch_web`：局部坐标 → 屏幕绝对坐标的平移只在这一层做，
  避免"前端以为自己在绝对坐标系"与"后端以为收到的是局部坐标"这种两边都对的错位

### 前端代码 templates/index.html + static/app.js + index.css
职责：控制面板UI，不渲染显示器画面。
功能：
- 已安装应用卡片网格（按系统/第三方分组、运行状态绿点、端口角标）；**单击卡片=选中程序**（随后在画布拖出区域），**双击卡片=该应用直接整屏显示**（launchWebApp → launch_web?fullscreen=1）
- 拖拽画布：选中程序后拖出区域（draw）→ launch_web；区域拖动（move）/缩放（resize，右下角手柄）→ setrect；边缘吸附参考线（屏幕边框+其他区域边缘+中心）、AABB 防重叠（MTV 最小位移推出）
- 布局按钮组：【保存当前布局】/【应用已保存布局】（applyLayout → /api/layout/restore）/【关闭所有应用】
- **「目标显示器」卡片**：列出全部输出（接口类型徽标、连接/启用状态、分辨率、物理尺寸、是否 primary/preferred），
  点击切换目标屏（`setTargetOutput()` → `/api/display/set_output`），旁边有刷新按钮（`refreshDisplays()` → `/api/display/refresh`）。
  "已连接但未启用"的输出会显式标注（提示可点亮），而不是当作不存在
- **热插拔提示**：`handleDisplayEvents()` 对比前后两次 `/api/status` 的 display_events，
  用 `OUTPUT_EVENT_TOAST` 映射成中文提示（接了屏/拔了屏/分辨率变了/目标屏丢失…）。
  首帧建立 `_seenDisplayEventKeys` 基线只看不弹；开机/换屏瞬间的一批事件用
  `displayEventKey()` 去重后**合并成一条 toast**（标题归并、明细最多 4 条、level 取最高），
  不再每个事件弹一个
- 窗口列表（buildWinItem）：visible=false 的最小化窗口渲染为"已最小化"条目（恢复窗口+停止），
  可见窗口带全屏/置顶徽标与全屏/置顶/最小化/关闭按钮；窗口计数显示"N 显示 / M 最小化"；
  画布 currentRegions 只放可见窗口
- 音频卡片（renderSinkList）：每个 sink 一行——设为输出、静音切换（🔇/🔊）、音量滑块；
  滑块 input 事件 120ms 去抖（onSinkVolumeInput + _volTimers），拖动过程不刷请求；
  轮询重绘时若 activeElement 在列表内则跳过，避免拖动滑块被 3 秒刷新打断
- 定时截图卡片：renderShotInterval 从 /api/status 回填间隔，setScreenshotInterval 提交
- 布局方案 profile-row：下拉（loadProfiles）+ 应用/保存为方案（prompt 取名）/删除（confirm）
- 触屏：画布拖拽统一用 Pointer Events（pointerdown 画布、pointermove/up/cancel 绑 document，
  touch-action:none），鼠标只响应 button 0；stopApp 有二次确认
- 窗口名 SSR：index.html 用 Jinja 直接渲染窗口名（去掉 web: 前缀，回退 PID），
  首屏不再出现闪一下 PID 再变名字
- 启动时从 /api/status 拉取 display_width/height 动态设置画布纵横比（DRM 竖屏自动适配），旋转按钮组；
  头部指标条按 backend/output_name/output_label 显示（如 `DRM · HDMI-A-1`、`虚拟 · Virtual-1`），鼠标悬停看说明
- 日志弹窗、截图预览、窗口列表与音频设备列表自动刷新（3 秒一次）
- 手动命令输入行：程序名 + args（shlex 解析，支持带引号的路径参数）
> 命名注意：前端历史变量名带 HDMI 字样（HDMI_WIDTH、toHdmi 等），已统一改为 OUTPUT_WIDTH / toPixels
> 之类的中性命名——留着旧名字会让后来者以为只支持 HDMI。
> 改 css/js 后记得同步 index.html 里的缓存戳（`?v=YYYYMMDD`），否则浏览器拿旧文件。

### tests/ 与 scripts/（离线自检三件套）
职责：不依赖 Xorg/pactl/浏览器/显卡的纯逻辑测试，用桩数据验证最容易写错的部分。
在真实显示器上排查这些问题的成本极高，所以能离线覆盖的都尽量离线覆盖。

**1. `tests/test_offline.py`**
- audio_manager：`pactl list sink-inputs` / `list sinks` 输出解析、按 pid 路由、批量迁移
- config：settings 缺字段补默认值、layout 读写一致、pids 的 int key 还原、损坏 json 兜底、log_line 写入
- window_manager（用 `_FakeSubprocess` 替身，覆盖 xrandr/scrot 调用）：
  - 输出后端探测（连接/断开/命令缺失/强制覆盖）
  - **通用多输出解析**：`classify_output` 各接口归类（含未知接口不丢信息）、
    `XRR_MULTI` 桩数据下 6 个输出的连接/启用/几何/EDID 尺寸/首选模式、
    "接了屏但未 --auto = 已连接未启用"、可见区外接矩形、目标输出选择与切换、未连接口退回 primary、旋转几何
  - 截图按返回码判定
  - clamp_rect 边界、clamp_windows_to_screen（**单屏**、"**可见区原点非 (0,0)** 时把窗口平移回输出"、无已启用输出回退整屏）
  - sink 音量/静音解析（多声道行取首个百分比、Mute: yes/no）与 set_sink_volume/set_sink_mute 命令、钳制、写后失效
  - get_outputs/list_windows 的 TTL 缓存命中、force 绕过、invalidate 重查
  - set_window_rect 全屏退避序（wmctrl remove,fullscreen 必须早于 xdotool windowmove）、普通窗口不发 wmctrl、_NET_WM_STATE 解析
  - 最小化窗口补全（注册表 alive pid → visible=False 条目；无真实 wid / 已死 pid 不造幽灵条目）
  - set_minimized/set_above 的命令序列、config profiles 读写与三种损坏兜底、main 的方案名校验与截图间隔 API
> window_manager 间接依赖 psutil（app_runner 导入），本地无 psutil 时测试会往 sys.modules 注入空桩模块；
> 同时显式设置 XDG_RUNTIME_DIR，绕开 desktop_env._runtime_dir 里的 os.geteuid()（Windows 无此函数）。
> main.py 导入时以相对路径挂载 static/templates，测试在 import 前临时 chdir 到 src 再还原。
>
> 注意：`clamp_windows_to_screen` 现在走 `get_visible_area()` → `get_outputs()`，
> 测试要打桩 `wm.get_outputs`（早期打桩的是 `get_display_geometry`，改实现后会静默失效）。
> 带有短 TTL 缓存的接口，测试里每次换 subprocess 桩后必须立刻 invalidate_*，否则读到的是上一份桩数据。

**2. `tests/test_entrypoint_drm.sh`**（bash，Windows Git Bash 也能跑）
把 entrypoint.sh 里的 `detect_drm_card()` / `list_connectors()` 抽出来，用假 sysfs 树覆盖 9 个场景：
空卡跳过选有屏的卡、双卡取编号小的、都没接屏回退第一张、sysfs 无信息回退 card0、
完全没有 DRM 设备返回空串、sysfs 有卡但设备节点缺失不误选、连接器目录不被误识别为卡、
USB-C(DP-2) 已连接即被选中、list_connectors 汇总格式。
> 假 sysfs 树必须同时建出 `cardN` 目录（真实 sysfs 里它指向 DRM 设备）；只建 `cardN-XXX-1`
> 连接器目录的话外层循环全被跳过，测的就不是真实分支（这个错误的桩曾让"选错卡"的用例假通过）。

**3. `scripts/validate-package.py`**（FPK 打包结构校验，74 项）
manifest 必填字段与版本格式、privilege/resource/ui/wizard 的 JSON 合法性、
ICON 尺寸（读 PNG 头，不依赖 Pillow）、app/cmd/wizard 目录存在、desktop_uidir 目录存在、
生命周期脚本齐全且引用公共库、install/upgrade_callback 必须调用 `wc_load_image`（否则"装完不用再下载"落空）、
compose 镜像 tag 与 manifest.version 一致、`pull_policy: never`、`privileged: true`、
`--require-image` 时镜像 tar 必须存在且文件名含版本号。

运行：本地 `python3 tests/test_offline.py`；镜像内 `python3 /app/tests/test_offline.py`
（Dockerfile 已 COPY tests/，`build-package.sh` 的镜像自检也会跑它）。
**新增/修改纯逻辑（正则、配置读写、路由规则、几何钳制、输出分类）时请同步补用例。**

## 3. 数据流举例：保存布局
1. 用户网页点击【保存当前布局】
2. JS调用 /api/layout/save POST接口
3. main.py调用 wm.list_windows() 获取当前所有窗口信息
4. 遍历窗口pid：
   - app_name：查注册表 → psutil.cmdline() 兜底 → wm.get_window_class(wid) 回退
   - args：本地程序抓 cmdline 参数；**web 应用（app_name 以 "web:" 开头）按 restore_app 约定存 URL 本身**（pid_url 内存表 → cmdline 扫 http(s) 兜底），不能存 chromium 完整命令行
   - 判断全屏：`is_fullscreen = (width >= screen_w and height >= screen_h)`
5. 组装数组 [{"app_name":"web:影视","args":"http://172.17.0.1:5666/xxx","x":0,"y":0,"w":1920,"h":1080,"fullscreen":true}]
6. config.save_layout写入 /data/layout.json

## 4. 数据流举例：容器启动自动恢复布局 / 手动应用布局
（两条路径共用 main.apply_layout()，区别只是启动时先 reconcile_pids）
1. entrypoint.sh 启动 dbus → PipeWire → pipewire-pulse → Xorg → openbox → uvicorn
2. FastAPI触发 startup_event：写启动日志 + 启动异步任务 restore_layout_task
3. reconcile_pids() 清理 pids.json 中已死 pid
4. 预设 default sink，让新启动应用音频自动路由到目标设备
5. load_layout读取layout.json；空则记日志并结束
6. 读当前屏幕尺寸，每项几何经 clamp_rect 钳进屏幕（布局可能来自旋转前的旧分辨率）
7. 快照 wm.list_windows()，逐项判断：
   - 已有该应用窗口 → set_fullscreen / set_window_rect 重定位（不重启应用）
   - 没有 → restore_app(app_name, args, x, y, w, h, fullscreen)
     - web 应用：chromium --window-position/--window-size 带几何启动 + _enforce_geometry 后台纠偏（全屏项直接发 EWMH 全屏）
     - 本地程序：start_local_app 启动
   - _wait_window_appear 轮询直至窗口出现或超时（10秒）→ 全屏或定位 → route_process_audio 路由音频
8. 汇总 started / repositioned / failed 写进 /data/app.log；网页点【应用已保存布局】时同一结果直接返回给前端 toast

## 4.1 数据流举例：网页点击【应用已保存布局】
1. app.js applyLayout() → POST /api/layout/restore
2. 后端若 _layout_lock 被占用（正在自动恢复）→ 立即返回"正在进行中"
3. 否则 `async with _layout_lock: apply_layout()`，与启动恢复完全同一份逻辑
4. 无已保存布局 → 提示先【保存当前布局】；否则返回 msg（拉起 N 个 / 重定位 M 个 / 失败 K 个）
5. 前端 refresh() + 2.5 秒后再 refresh 一次（chromium 建窗有延迟）

## 5. 扩展开发指南（新增功能怎么加）
### 新增API接口
1. main.py 添加路由函数，调用对应模块（window_manager/app_runner等）
2. 前端index.html增加按钮，app.js增加fetch调用
3. 如果需要持久化：在config.py的settings结构增加字段，前端增加保存接口

### 支持多分辨率
真实输出尺寸由 xdotool getdisplaygeometry 实时返回（DRM 跟随 EDID）；
settings 的 display_width/height 仅作兜底与画布换算；
main.py 暴露 /api/settings/set_display_resolution；前端从 /api/status 拉取真实值。
Xorg 输出分辨率可用 xrandr 调整（window_manager 已有 xrandr 封装可扩展 cvt/addmode）。

### 增加定时任务
例如定时截图、定时切换视频：参照 `_screenshot_loop` 的写法——lifespan 里
asyncio.create_task 启动、stop_event 在 lifespan finally 置位、循环体整体 try/except
吞异常、阻塞型子进程丢 executor 跑；任务参数放 settings 持久化，循环内每秒重读，
做到"设置即生效、不重启"。

### 增加视频播放列表
在layout结构扩展，增加播放顺序、文件路径；本地程序通过 args 传参（如 vlc）。

### 更换浏览器/显示引擎
launch_web_app 自动检测 chromium/chromium-browser/google-chrome/firefox；
新增候选改 app_runner._find_browser 的 candidates 元组即可；firefox/chromium 分支命令行参数已分别实现。

## 6. 已知坑点 & 开发踩坑记录
### 当前方案（Xorg + openbox）
1. **X11窗口创建异步**：启动程序后，窗口不会立刻出现。已用 `_wait_window_appear` 轮询直至 pid 出现或超时（10秒）。
2. **X显示号递增**：docker restart 不重建文件系统，/tmp/.X11-unix socket 残留。修复双保险：entrypoint 启动 Xorg 前清理；运行期 desktop_env.py 读 /proc/net/unix 探测真实显示号。
3. **xdotool getwindowgeometry 坐标偏移**：openbox 重父化窗口后它报相对 frame 的坐标。统一用 xwininfo 的 Absolute upper-left。
4. **windowmove 作用于 frame**：需按 _NET_FRAME_EXTENTS 反向补偿，否则客户区偏移一个标题栏宽度。
5. **chromium 忽略 --window-position**：会从持久化 profile 恢复上次几何。对策：独立 user-data-dir + 启动后 _enforce_geometry 线程多次纠正（覆盖约前 4 秒的延迟恢复）。
6. **chromium 多窗口合并**：不加独立 user-data-dir 时 --new-window 会在已有实例开窗，pid 归属错乱。
7. **WirePlumber crash-loop**：容器内无 system dbus + 无 logind。改用 pipewire-pulse + pactl。
8. **pid_app_name 内存易失**：Web 崩溃重启后内存表丢失。已加 pids.json 持久化 + 启动时 reconcile + psutil.cmdline 兜底；web 应用的 URL 同理（pid_url + cmdline 扫描兜底）。
9. **web 布局 args 约定**：layout.json 中 web: 应用的 args 必须存 URL 本身，存浏览器完整命令行会导致 restore 失败（历史 bug，已修）。
10. **entrypoint.sh 执行位**：Windows sftp 上传丢失执行位，用 `ENTRYPOINT ["bash", "/entrypoint.sh"]` 绕过。
11. **容器权限**：Xorg 需 root（DRM master）；/dev/input 是 char 13:*，bind-mount 之外需 `--device-cgroup-rule='c 13:* rmw'`。
12. **只切 default sink 不会改变正在播放的应用**：default sink 只对新建 stream 生效。已启动并正在播放的进程必须 `pactl move-sink-input <index> <sink>`。所以 /api/audio/set_sink 默认 migrate_existing=1，route_process_audio 也要按 `application.process.id` 匹配流后再迁移。
13. **全屏与几何纠正的顺序陷阱**：先用 xdotool 设了几何、再发 EWMH 全屏，或反过来在已全屏的窗口上继续设几何，都可能与 WM 的全屏状态互相干扰。约定：fullscreen 请求一出现就发全屏并停止几何纠正。
14. **屏幕旋转后窗口跑到屏幕外**：90°/270° 宽高互换，原先贴旧边界的窗口会完全落在新屏幕之外（显示器上不可见、鼠标也点不到），必须主动 clamp 回可见区（wm.clamp_windows_to_screen）。切换目标显示器、热插拔后同理。
15. **日志只增不减**：/data/app.log 是唯一的持续增长型持久化数据，容器跑数月会撑爆持久卷。entrypoint 内置轮转（默认超 2MB 保留尾部 2000 行），阈值可用 LOG_MAX_BYTES/LOG_KEEP_LINES 调整。
16. **backend 字段靠环境变量是假的**：/api/status 早期直接返回 `os.environ.get("WESTON_BACKEND", "headless")`，而 entrypoint 从不设置该变量 → 明明在驱动真实 HDMI，面板却永远显示「Headless 虚拟」。改为 wm.get_output_info() 按 xrandr 实际已连接输出判定，环境变量只留 WC_FORCE_BACKEND 作调试覆盖。**教训：面板上的状态字段必须来自实际探测，不能来自"期望的启动参数"。**
17. **恢复全屏窗口时丢了 fullscreen 标记**：restore_app 早期不接收 fullscreen，_enforce_geometry 便走非全屏分支持续 set_window_rect，与随后 main.py 发的 EWMH 全屏互抢（见坑点 13）。凡是"启动 → 后置设置"两段式的参数，必须整条链路透传，不能只传给最终那一步。
18. **截图用 exists() 判成功会骗人**：scrot 失败（未安装/X 未起/无输出）时旧 screenshot.png 仍在，`os.path.exists` 直接把它当成"本次成功"，用户看到的是几分钟前的画面。改为先删旧图 + 校验 scrot 返回码。
19. **日志轮转与长期文件句柄冲突**：entrypoint 的轮转实现是 `tail > f.rotating && mv f.rotating f`。若应用层持有该文件的长期句柄（logging.FileHandler 之类），轮转后进程仍写入已被 unlink 的 inode，新文件永不增长。所以 config.log_line 每次调用都重新 open(append)。
20. **布局几何不能照搬**：布局可能是旋转 90°/改分辨率之前保存的，直接应用会把窗口摆到屏幕外。apply_layout 统一先过 clamp_rect；同时"已有同名窗口就只重定位、不重启"——用户点【应用已保存布局】的意图通常是摆整齐，误重启会打断正在播放的视频。
21. **硬编码输出名 = 只支持一种接口**：早期 `_get_connected_output()` 在探测失败时回退 `"HDMI-2"`，`get_output_info()` 也按固定名解析。结果是只有 VGA/DP 接口的机器上，旋转、回屏、分辨率读取全部静默失效——因为 xrandr 调用打到了一个不存在的输出上。**教训：任何"默认值"都必须来自实际枚举结果，不能来自对硬件的假设。** 现在所有输出处理都从 `xrandr --current` 派生，`classify_output()` 只负责贴标签。
22. **DRM 卡号不固定**：多显卡机器（服务器板载 BMC VGA + Intel iGPU）上写死 `/dev/dri/card0` 可能驱动到没人看的空口 → 显示器全黑，而这类问题在真机上排查成本极高。改为按 `/sys/class/drm/*/status` 的连接器状态选卡。已用 `tests/test_entrypoint_drm.sh` 做离线回归。
23. **openbox 默认配置会"修正"你的坐标**：Debian 版 openbox 的默认 `rc.xml` 带窗口贴边阻力（`resistance`）和屏幕边缘吸附，`xdotool windowmove` 的坐标会被 WM 二次修正，表现为"画框布局总是差几个像素对不齐"。仓库里的 `rc.xml` 把 `strength`/`screen_edge_strength` 设为 0 —— **生产 Dockerfile 一度漏了 `COPY rc.xml`**（只有测试用 Dockerfile 拷了），于是镜像里跑的是带阻力的默认配置。凡是"仓库里有但 Dockerfile 没 COPY"的配置文件，都等于不存在。
24. **多显示器下"坐标合法"不等于"看得见"**：X 屏幕是整个虚拟桌面（所有输出的外接矩形），窗口落在没有任何输出的空白区域时坐标完全合法，但用户既看不见也点不到。因此回屏的边界不能用整屏尺寸，要用 `get_visible_area()`（所有**已启用**输出的外接矩形），并且平移要带上可见区原点。剩余边界：两块屏之间若存在空隙，落进去的窗口仍算合法——相邻输出通常紧贴，暂不处理。
25. **坐标系的"半截改造"最危险**：引入目标输出原点后，只要有一个接口漏了平移，就会出现"面板上拖对了、屏幕上偏了一块屏"的诡异现象。约定：**前端一律收发局部坐标，后端在写窗口/读窗口的边界处统一 `_to_screen` / `_to_local`**，持久化的 layout 也存局部坐标（换屏/换分辨率后仍能正确还原）。
26. **守护线程抛异常就永久失效**：display_monitor 的轮询循环若让单个异常冒出去，线程死掉后所有轮询逻辑（热插拔点亮、越界回屏）静默停摆，表现为"热插拔再也不生效"，而且没有任何报错。循环体必须整体 try/except 并继续下一轮。
27. **`.fpk` 是 gzip(tar.gz)，不是 zip** —— 本工程代价最大的一个坑。早期 `build-package.sh` 在找不到 `fnpack` 时用 Python `zipfile` 组装 `.fpk`，本地解压、校验全部正常，装到飞牛应用中心却直接报「**不是有效的程序文件**」。原因：平台的解析器按 gzip 读包，看到魔术字节 `PK\x03\x04` 就判废。实测（逆向 fnpack 1.2.3 产物）正确格式是：
    - 外层 gzip(tar)，魔术字节 `1f 8b 08 00 00 00 00 00 00 ff`（gzip mtime=0、OS=255）；
    - **`app/` 不以目录形式出现，必须打成 `app.tgz`**（内层再一层 gzip/tar），且 `config/privilege`、`config/resource` 会被复制一份进 `app.tgz`；
    - 所有目录 mode `0777`、文件 mode `0666`（`cmd/main` 也不是可执行的，执行位由飞牛安装时补）；
    - `manifest` 会被重写为 `key<pad>= value`（键宽 27）并追加 `checksum = md5(app.tgz)`，因此 **app.tgz 必须在 manifest 之前生成**。
    现在的做法：优先调用官方 fnpack（找不到就自动下载到 `tools/`），兜底用 `scripts/make-fpk.py` 复刻同一套结构，打包最后一步强制跑 `validate-package.py --fpk` 复验格式 —— 绝不再退回 zip。
    仍然成立的旧经验：shell 脚本必须保持 LF 换行，CRLF 会让 Linux 上的 shebang 解析失败。
28. **离线包的关键是"镜像在包里，而不是在地板上"**：`pull_policy: never` 只保证"有本地镜像时不联网"，前提是镜像真的被导入了。所以 `cmd/install_callback`（以及 `upgrade_callback`）必须执行 `docker load`，`validate-package.py` 也把"这两个脚本调用了 `wc_load_image`"作为必过项——否则离线承诺会在用户断网的 NAS 上当场破功。
29. **manifest 版本 / 镜像 tag / tar 文件名必须同源**：飞牛要求 `version` 是 `X.Y.Z`。三者对不上时，compose 找不到本地镜像会去联网拉取，报出的却是 `manifest unknown` 这类误导性错误。`build-package.sh` 统一从 manifest 读版本号生成后两者，手工改版本时三处都要改。
30. **Windows 上给原生 exe 传 Git Bash 绝对路径会被二次转换**：`"$PY_BIN" /d/CODE/x/validate-package.py` 会变成 `D:\d\CODE\x\...`，Python 报 `can't open file`（这个环境下 `cygpath -m` 也不可靠）。`build-package.sh` 的统一解法是 `py()` 包装：**先 cd 到项目根，再传相对路径**，三平台通用且不依赖 cygpath。
31. **`platform` 要跟包内镜像架构一致**：官方文档说 `all` 只用于"不含任何架构相关二进制"的包。本包自带 amd64 镜像却写 `all`，会让 ARM 飞牛设备也允许安装，结果是容器起不来。现为 `x86`，ARM 机器上打包用 `--platform arm`；构建脚本还会在 `docker save` 后核对镜像架构并直接报错，避免打出自相矛盾的包。
32. **openbox 吞掉全屏窗口的几何请求**：窗口带 _NET_WM_STATE_FULLSCREEN 时位置尺寸由 WM 全权托管，xdotool windowmove/windowsize 静默无效（返回成功、画面不动）。set_window_rect 必须先 wmctrl remove,fullscreen 并 sleep 0.15s 等 WM 异步还原，再移动；apply_layout 移动全屏窗口时同一路径自动覆盖。
33. **最小化窗口从 --onlyvisible 消失**：xdotool search --onlyvisible 不列最小化窗口，用户最小化后界面会"丢失"这个应用。list_windows 用 pid 注册表 + `xdotool search --pid` 补全 visible=False 条目；注意查不到窗口时 wid 会回退成 str(pid)，这种必须丢弃，否则造出点不到的幽灵窗口。
34. **热插拔一批事件刷屏 toast**：开机点亮/换屏瞬间 display_monitor 一轮可能产生多个事件。前端按事件 key 去重合并为一条 toast（明细截断 4 条、level 取最高），而不是连发 N 个通知。
35. **轮询缓存陷阱**：get_outputs/list_windows/list_sinks 有短 TTL 缓存，单元测试或脚本里换 subprocess 桩/改系统状态后必须先 invalidate_*，否则读到上一份数据；所有写操作路径已在末尾显式失效，新增写方法时照此约定补一行。

### 历史记录（Weston + Xwayland 方案，已弃用）
- **Weston 10 desktop-shell 无视 X11 窗口位置请求**：windowmove/override_redirect/_NET_WM_STATE_ABOVE 均无效，仅 windowsize 生效；这是迁移到 Xorg+openbox 的直接原因。
- **全屏必须走 EWMH ClientMessage**：`xprop -set` 只改属性不通知 WM；`wmctrl -i -r <wid> -b add,fullscreen` 才有效（wmctrl 枚举不可用但按 wid 发事件可用）。此经验在 Weston 与 openbox 下通用。
- **weston-screenshooter 忽略路径参数**：PNG 固定写 CWD，需 cwd=/data 再 glob 重命名； grim 不兼容（无 wlr-screencopy）。现方案已改 scrot，无此问题。
- **/tmp/.X11-unix 缺失**：slim 镜像无此目录，Xwayland 会 segfault。entrypoint `mkdir -p && chmod 1777`。
- **Wayland socket 递增**：PipeWire/dbus 隐式占用 wayland-0，需自动检测不硬编码。

## 7. 本地调试方法
1. 本地不能直接跑 Xorg DRM（需要 Linux + KMS 环境），推荐在飞牛NAS容器内调试。
2. 查看实时日志：网页【查看运行日志】或进容器 `tail -f /data/app.log`；Xorg 日志 /data/xorg.log，openbox 日志 /data/openbox.log。
   Web 层事件（应用拉起/停止、布局恢复结果、旋转、接口异常）也在同一份 app.log 里，可直接 `grep -E '【WARN】|【ERROR】|恢复布局|应用布局' /data/app.log`。
3. **离线自检**（不需要图形环境，Windows/Linux 都能跑；改动后请全跑一遍）：
   ```bash
   python3 tests/test_offline.py          # 纯逻辑单元测试（含通用多输出解析、多屏回屏）
   bash    tests/test_entrypoint_drm.sh   # entrypoint 的 DRM 卡选择规则（假 sysfs 树，9 个场景）
   python3 scripts/validate-package.py    # FPK 打包结构校验（74 项，改打包目录后必跑）
   ```
   改 audio_manager 的正则、config 的读写逻辑、window_manager 的几何/输出解析算法、
   packaging/ 下的任一打包文件之后，先跑这几套，再上机验证。
4. **看面板实际枚举到了什么**：进容器执行
   - `xrandr --current` —— 全部输出与连接状态（最直接的"接口认到了没有"）
   - `ls /sys/class/drm` —— 内核连接器
   - `grep -E '选中 DRM 设备|内核显示连接器|显示输出探测|xrandr 输出' /data/app.log`
   - `curl -s localhost:8181/api/status | jq '{backend,output_name,output_type,target_output,outputs}'`
   - `curl -s localhost:8181/api/display/outputs | jq` —— 面板「目标显示器」卡片的数据源
   - 想看虚拟屏上的画面（无真实显示器时）：`docker exec <容器> scrot /data/x.png` 后下载查看
5. API调试工具：Postman / curl 直接调用API，不需要打开网页（后端对越界参数有兜底钳制，见第 2 节 main.py）。
6. Python代码单独调试：进容器 `cd /app/src && python3 -c "from window_manager import wm; print(wm.get_outputs())"`；
   查音频流：`python3 -c "from audio_manager import am; print(am.list_sink_inputs())"`。
7. **想临时关掉显示守护**：`-e WC_DISPLAY_POLL_INTERVAL=0` 重启容器，然后只手动调 `/api/display/refresh` 一步步观察，
   排除"自动行为"与"手动行为"互相掩盖的干扰。
8. 前端调试：浏览器F12，Network面板看API返回，Console看JS报错。

## 8. 打包发布（FPK）

**目标：NAS 端零下载。** 安装包内自带 docker 镜像，装完后不再联网拉取任何东西。
安装步骤面向用户的部分见 [INSTALL.md](INSTALL.md)，这里只说工程侧实现。

### 8.1 目录结构（`packaging/fnos/`，即 fnpack 的工程根）

```
packaging/fnos/
├── manifest                  # INI 元数据（appname/version/display_name/source/service_port…）
├── ICON.PNG  ICON_256.PNG    # 64×64 / 256×256 图标（由 scripts/make_icons.py 生成）
├── config/
│   ├── privilege             # JSON：defaults.run-as = root（生命周期脚本要 docker load）
│   └── resource              # JSON：声明 docker-project（name=window-composer, path=docker）
├── app/
│   ├── docker/
│   │   ├── docker-compose.yaml        # 平台按 docker-project 直接执行
│   │   └── image/window-composer-<版本>.tar   # ★ 离线镜像（docker save 产物，约数百 MB）
│   └── ui/
│       ├── config            # 桌面入口 .url（iframe → http://<nas>:8181）
│       └── images/icon_{64,256}.png
├── cmd/
│   ├── common                # 公共库：日志 / docker 探测 / 容器状态 / wc_load_image
│   ├── main                  # start / stop / status（status 未运行必须 exit 3）
│   ├── install_init          # 安装前预检（docker、宿主连接器）
│   ├── install_callback      # ★ docker load 导入离线镜像
│   ├── upgrade_init / upgrade_callback   # 停旧容器 → 导入新镜像
│   ├── uninstall_init / uninstall_callback
│   └── config_init / config_callback     # 配置变更后按新环境变量重建容器
└── wizard/
    ├── install / config / upgrade / uninstall   # JSON 数组；字段名即环境变量名
```

打包出来的 `.fpk` **不是 zip，是 gzip(tar.gz)**，而且 `app/` 不以目录形式出现：

```
window-composer-<版本>.fpk          外层 gzip，本体是 tar
├── app.tgz                 ← app/ 的内容（外加 config/privilege、config/resource）再打一层 gzip(tar)
├── cmd/  + cmd/*           ← 9 个生命周期脚本 + common
├── config/privilege  config/resource
├── ICON.PNG  ICON_256.PNG
├── manifest                ← 被重写为 `key<pad>= value`，并追加 checksum = md5(app.tgz)
└── wizard/ + wizard/{install,config,upgrade,uninstall}
```

两个必须守住的点：`.fpk` 得是 gzip/tar.gz（zip 会被飞牛判成"不是有效的程序文件"，见坑点 27）；
`app/` 必须以 `app.tgz` 形式出现，否则平台找不到应用文件。

### 8.2 关键约定

1. **`cmd/main` 的 status 必须准确**：0=运行中、3=未运行。应用中心与桌面卡片的"运行中"状态完全依赖它。
   容器启停由平台按 docker-project 处理，所以 start/stop 做成幂等的兜底动作，不与平台争抢生命周期。
2. **离线导入放在 `install_callback` / `upgrade_callback`**：`docker load` 安装包内的 tar，
   compose 里配 `pull_policy: never`，两者缺一不可（见坑点 28）。
3. **版本号同源**：`manifest.version` = 镜像 tag = tar 文件名，三者由 `build-package.sh` 统一生成（见坑点 29）。
4. **`docker-compose.yaml` 的要点**：
   - `privileged: true`（Xorg 打开 DRM master）、`network_mode: host`（面板直接监听 NAS 端口）
   - 不用 `devices:` 声明 `/dev/dri`：`privileged` 已覆盖；写死 `devices` 会在宿主没有 `/dev/dri` 时
     直接起不来，而我们的设计是"没有显示设备也能启动并降级虚拟输出"
   - 持久化 `${TRIM_PKGVAR}/data:/data`；不挂 `/dev/input`、`/run/udev`（热插拔走 xrandr 轮询）
5. **图标必须 PNG 且尺寸精确**：fnpack 会校验。`scripts/make_icons.py` 从展示图裁掉黑边再缩放，
   保证 64/256 两个尺寸视觉一致。
6. **`.fpk` 是 gzip(tar.gz)，且 `app/` 要打成 `app.tgz`**（见坑点 27）。一律优先用官方 `fnpack`
   （https://developer.fnnas.com/docs/cli/fnpack/ ）：`build-package.sh` 按
   `$FNPACK_BIN` → 仓库内 `tools/` → `PATH` 依次查找，都没有就自动下载官方静态二进制到 `tools/`；
   实在拿不到时才退回 `scripts/make-fpk.py`（复刻同一套结构）。
   另外 `platform` 要与包内镜像架构一致：本包自带 amd64 镜像，所以是 `x86`；在 ARM 机器上打包加
   `--platform arm`（构建结束会自动还原 manifest）。

### 8.3 构建流程

```bash
pip install pillow                  # 只有生成图标需要，不进镜像
bash scripts/fetch-wheels.sh        # 可选：预下 wheel，让构建机无网也能 build 镜像
bash scripts/build-package.sh       # → dist/window-composer-<版本>.fpk
```

`build-package.sh` 的步骤：
1. 从 manifest 读 appname/版本号并校验 `X.Y.Z` 格式，同时定位（必要时下载）官方 `fnpack`
2. 生成图标 → `validate-package.py` 目录结构校验
3. `docker build` → 在容器内自检（关键命令齐全 + `test_offline.py` 通过）
4. `docker save` 导出镜像 tar 到 `app/docker/image/`，并核对镜像架构与 `manifest.platform` 是否一致
5. `validate-package.py --require-image` 复核（镜像必须在包里）
6. 官方 `fnpack build` 打 `.fpk`（拿不到 fnpack 时退回 `scripts/make-fpk.py`）
7. `validate-package.py --fpk` 复验成品：必须是 gzip/tar.gz、含 `app.tgz`、
   `manifest.checksum == md5(app.tgz)` —— 把"装到 NAS 上才发现包不对"提前到打包机上

`--skip-image` 复用已有 tar 只重打包；`--no-image` 出瘦包（调试用，安装时需联网）；
`--platform <x86|arm|all>` 声明目标架构；`--no-fetch` 禁止自动下载 fnpack。

### 8.4 镜像设计（Dockerfile）

基于 `debian:bookworm-slim`，构建期装齐：Xorg + modesetting/dummy 驱动、openbox、xdotool/x11-utils、
xrandr/wmctrl/scrot、PipeWire + pipewire-pulse + pulseaudio-utils、alsa-utils、dbus、chromium、
fonts-noto-cjk、libgl1-mesa-dri、python3 + FastAPI/uvicorn/jinja2/psutil。
Python 依赖优先走 `vendor/wheels/` 离线安装，没有该目录才走国内镜像。

- **必须 `COPY ./rc.xml /etc/xdg/openbox/rc.xml`**：否则用的是 Debian 默认配置，贴边阻力会让窗口坐标偏移（见坑点 23）
- **不 COPY 任何 Xorg 静态配置**：`modesetting.conf` / `dummy.conf` 由 entrypoint 运行时按探测到的 DRM 卡生成，
  仓库里不再保留 `20-modesetting.conf` 之类的 xorg.conf.d 片段——避免"仓库里有一份、镜像里却是另一份"的隐性偏差
- `ENTRYPOINT ["bash", "/entrypoint.sh"]`：规避 Windows/sftp 上传丢失执行位
- `HEALTHCHECK` 探 Web 服务；显示栈由 entrypoint 主循环守护

### 8.5 验证清单（上机前）

| 检查 | 命令 |
| --- | --- |
| 打包结构 | `python3 scripts/validate-package.py --require-image` |
| 逻辑单测 | `python3 tests/test_offline.py` |
| DRM 选卡规则 | `bash tests/test_entrypoint_drm.sh` |
| 镜像内依赖自检 | `docker run --rm --entrypoint bash window-composer:<版本> -lc 'command -v Xorg openbox chromium python3'` |
| 上机 | 应用中心手动安装 → 启动 → 面板「目标显示器」列出全部接口 → 拖一个应用看显示器出画 |

### 8.6 其他分发方式

不想用应用中心时，仓库根的 `docker-compose.yml` 可直接 `docker compose up -d`
（同样 `privileged` + host 网络），或用 INSTALL.md 里的 `docker run` 一行命令。
两种方式都需要镜像已经 build 或 `docker load` 过。

