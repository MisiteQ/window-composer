# window-composer 窗口合成容器管理平台

> **开发者**：Misite齊 ｜ [GitHub](https://github.com/MisiteQ) ｜ [问题反馈](https://github.com/MisiteQ/window-composer/issues)

项目代号：window-composer
这是一套基于飞牛 NAS 视频输出端、用来展示飞牛内部应用程序的小系统，AI 快速搓出来的，还有不少 BUG，大家先凑活用。
如果下载的安装包无法正常安装，可以自行拉取源码重新打包。这东西折腾起来比较麻烦。

> 基于 **Xorg(modesetting/DRM) + openbox + PipeWire 音频 + FastAPI Web 后台**，运行于飞牛 NAS（Docker 容器）
> 目标：把飞牛应用的 Web 界面渲染到 NAS 的**物理显示器**上，并通过网页远程管理窗口布局，
> 实现多媒体播放、窗口布局持久化、远程控制。

## 核心设计理念

**显示服务常驻优先原则**

Xorg、openbox、PipeWire 作为底层服务常驻容器；Web 管理服务只是控制接口。
即使 Web 服务崩溃、重启，显示器画面、已经打开的 GUI 程序、音频播放**不会中断**。
这是本项目最核心的设计，区别于传统 WebUI 驱动渲染的方案。

**显示接口无关原则**

所有显示接口在内核里最终都是 DRM 上的一个 connector，Xorg 的 `modesetting` 驱动对接口类型无感。
因此 **HDMI / DisplayPort / USB-C(DP Alt Mode) / VGA / DVI / eDP / DSI 走的是同一条渲染路径**，
代码里不写死任何输出名——每个输出都由 `xrandr --current` 运行时枚举并分类。

架构分层：
1. 底层：Xorg（modesetting/DRM，输出到物理显示器） + openbox（X11 窗口管理器） + PipeWire（音频）
2. 控制层：Python FastAPI，提供 REST API，调用 xdotool/xprop/xrandr/pactl 管理窗口、启动应用
3. 前端层：Jinja2 渲染静态网页，浏览器作为控制面板，**不直接渲染显示器画面**
4. 存储层：容器 `/data` 持久卷，保存配置、布局文件、pid 注册表、日志、截图

> 方案演进：早期版本使用 Weston(headless/DRM) + Xwayland。Weston 的 Xwayland WM
> 强制覆盖所有 X11 窗口位置请求（xdotool windowmove 与 chromium --window-position
> 均无效），无法实现"画框布局、边缘吸附、多窗口不重叠"。现迁移到 Xorg + openbox，
> xdotool windowmove/windowsize 精确生效。Weston 时期踩坑记录见 dev-doc.md。

## 功能清单

### 显示输出（通用多接口）
- ✅ **通用显示接口支持**：HDMI / DisplayPort / **USB-C（DP Alt Mode / 雷电扩展坞）** / VGA / DVI / eDP / DSI 全部由同一套 `xrandr` 解析逻辑覆盖，不写死输出名
- ✅ **DRM 卡自动选择**：按 `/sys/class/drm/*/status` 挑"真的有显示器接在上面"的卡，多显卡机器（板载 BMC VGA + iGPU）不会选错
- ✅ **输出后端真实探测**：判定 `drm`（真实输出）/ `virtual`（dummy 虚拟输出）/ `headless`（探测失败），面板显示输出名与接口类型
- ✅ **目标显示器选择**：面板列出全部输出（含未连接/未启用的），可切换窗口布局落到哪块屏；坐标系以该输出左上角为原点，单屏与旧行为完全一致
- ✅ **显示器热插拔守护**：开机没接显示器、之后插上也能自动 `xrandr --auto` 点亮，并把越界窗口拉回可见区，**无需重启容器**；换接口/换机器同样自动恢复
- ✅ 屏幕方向旋转（xrandr 0°/90°/180°/270°，画布与坐标自动适配，越界窗口自动拉回屏幕内）
- ✅ 热插拔批量事件合并提示：开机/换屏瞬间的一批 udev 事件只汇总成一条通知，不刷屏
- ✅ 显示器画面截图（scrot，保存到 `/data/screenshot.png`，网页预览；按 scrot 返回码判定成功，不会拿旧截图冒充）
- ✅ 定时自动截图（间隔 10~3600 秒可设，0 关闭；存 `/data/screenshots/`，自动只保留最新 200 张，设置后最多 1 秒生效）
- ✅ 无显示设备时降级为 dummy 虚拟输出 1920×1080，控制面板与布局编辑仍可用

### 应用与窗口管理
- ✅ 自动扫描飞牛 NAS 已安装应用（读取宿主 `/var/apps` 的 manifest + target/ui/config），一键把应用 Web 界面显示到显示器
- ✅ 自动扫描宿主 **Docker 容器**（挂载 `/var/run/docker.sock`，走 Docker Engine API，容器内无需装 docker CLI），按分组「Docker 应用」列出并可直接显示其 Web 界面
- ✅ 飞牛应用显示引擎：自动检测容器内浏览器（chromium/firefox 等，不硬编码），以独立窗口渲染应用 URL，多窗口并存
- ✅ 网页可视化拖拽画布：选择程序后拖出显示区域，自动换算屏幕坐标；区域拖动/缩放/边缘吸附/防重叠
- ✅ 双击应用卡片 = 直接整屏显示（一键全屏，等价于单选应用独占屏幕）
- ✅ 自定义输入程序名 + 启动参数，启动容器内任意 GUI 程序
- ✅ 启动失败快速反馈：程序不存在/非 X11 程序时进程会秒退，接口直接返回明确原因（而不是只回"已启动"）
- ✅ 单独关闭指定窗口/停止进程（停止有二次确认）；一键清空所有应用窗口（Xorg/openbox 保持运行，画面不断）
- ✅ 窗口最小化 / 一键恢复 / 置顶切换；最小化的窗口仍在列表中以"已最小化"状态显示，随时可找回
- ✅ 拖动/缩放一个**全屏窗口**时后端自动先退出全屏再应用几何（openbox 会吞掉全屏窗口的 setrect，直接移动会"接口成功画面不动"）
- ✅ 窗口布局保存：记录所有窗口程序名、启动参数(URL)、坐标、宽高、是否全屏
- ✅ 一键应用已保存布局（`/api/layout/restore`）：无需重启容器即可把窗口摆回原位——屏幕上已开着的应用只重新定位（不重启，不打断正在播放的视频），已关闭的应用自动拉起；应用布局时最小化的窗口会自动恢复
- ✅ **多套布局方案 profiles**：当前窗口可另存为命名方案（`/data/layout_profiles.json`），随时下拉切换，方案的临时摆放不影响开机自动恢复的那份主布局
- ✅ 画布支持触屏（Pointer Events，手机/平板直接拖窗口）
- ✅ 容器启动自动恢复布局：轮询窗口出现 → 自动拉起应用、还原窗口几何、自动绑定音频路由
- ✅ pid 注册表持久化（`/data/pids.json`）：Web 服务崩溃重启后仍能识别已运行应用，`save_layout` 继续可用
- ✅ Web 服务挂掉自动重启（显示服务常驻优先原则）

### 音频管理
- ✅ 读取 PipeWire 音频输出设备列表（pactl，含当前音量百分比与静音状态）
- ✅ 每个输出设备独立的音量滑块与静音开关（去抖提交，拖动过程不刷请求）
- ✅ 设置默认音频输出设备，**切换时正在播放的应用会立即迁移到新设备**（按 application.process.id 精确定位播放流）
- ✅ 新启动应用自动路由音频到选中声卡（布局恢复时逐进程路由）
- ✅ 查看当前播放流 `/api/audio/sink_inputs`（排查"哪个进程的声卡不对"）

### 配置与运维
- ✅ 桌面背景色自定义（#RRGGBB，xsetroot，重启容器生效）
- ✅ Web 服务端口自定义（1024~65535，重启生效）
- ✅ 容器开机自启开关（挂载 docker.sock 时实时同步 Docker 重启策略 no / unless-stopped，不只是配置记录）
- ✅ 运行日志查看页面：Web 层事件与 entrypoint 服务编排日志同一份 `/data/app.log`
- ✅ 日志自动轮转：超过 2MB 只保留尾部 2000 行（长期运行不会撑爆持久卷，可用 `LOG_MAX_BYTES` / `LOG_KEEP_LINES` 调整）
- ✅ **自包含镜像**：Xorg、驱动、openbox、chromium、中文字体、Python 依赖全部打进镜像，拉取镜像后运行中不再下载任何软件；安装时自动测速择优选择 ghcr 加速源

### 访问安全
- ✅ **出厂初始令牌**：全新部署首次启动写入固定初始令牌 `admin123`（可用环境变量 `WC_ACCESS_TOKEN` 预置）；登录页与面板在未修改前持续醒目提示，首次登录后应立即改为自定义令牌
- ✅ **自定义令牌**：面板可设置 6~64 位自定义令牌（限字母数字与 `. _ @ -`，两次输入确认），也可一键随机重置/开关；旧令牌立即失效，当前浏览器自动续登录
- ✅ **访问令牌**：默认开启，外部访问控制面板与全部 API 需携带令牌（`X-Access-Token` 头或 `?wc_at=`）；未授权请求一律 401
- ✅ **飞牛 iframe 入口免令牌**：从飞牛 Web 桌面同主机入口打开面板时自动放行（Sec-Fetch-Site + Referer 同源校验），外域 iframe 一律拒绝
- ✅ 本机 loopback 与飞牛入口免令牌；令牌可在面板查看/修改/随机轮换/开关（`/api/access/*`）
- ✅ 跨站请求防护：浏览器跨站 Origin 的写请求直接 403（CSRF Origin Guard）

## 部署方式

### 方式一：飞牛应用中心安装（推荐）

```bash
# 在开发/打包机上产出标准安装包（无需本机有 docker）
bash scripts/build-package.sh
# → dist/window-composer-<版本>.fpk（约 130 KB，不含镜像）
```

飞牛桌面 →「应用中心」→ 右上角「**手动安装**」→ 选择该 `.fpk` → 按向导填写目标显示器等 →
「启动」→ 浏览器打开 `http://<NAS_IP>:8181`。

安装包本身很小、不含镜像：`cmd/install_callback` 会对多个 ghcr.io 镜像加速源
（daocloud / 南大 / 1ms 等）**实测延迟并择优拉取**与本机架构匹配的镜像，
之后 compose 以 `pull_policy: missing` 启动。安装过程中 NAS 需要能访问互联网，
镜像拉取一次后本地缓存，后续重启不再下载。

> **离线环境**：在有 docker 的机器上执行 `bash scripts/build-package.sh --offline`，
> 产出 `window-composer-<版本>-offline.fpk`（镜像内置，数百 MB），安装时无需联网。

> 打包请一律走 `scripts/build-package.sh`：`.fpk` 是 **gzip(tar.gz)** 而不是 zip，
> 飞牛拿到 zip 会报「不是有效的程序文件」。脚本优先调用官方 `fnpack`（缺失时自动下载），
> 并在最后复验成品格式，避免把废包交到 NAS 上才发现。

完整安装/排障说明见 **[INSTALL.md](INSTALL.md)**。

### 方式二：docker compose

```bash
docker build -t window-composer:1.0.0 .
docker compose up -d        # compose 文件：docker-compose.yml
```

### 方式三：docker run

```bash
docker run -d --name window-composer \
  --restart unless-stopped \
  --privileged \
  --network host \
  -e TZ=Asia/Shanghai \
  -e WC_TARGET_OUTPUT= \
  -e WC_DISPLAY_POLL_INTERVAL=5 \
  -v /vol1/1000/docker/window-composer/data:/data \
  -v /run/udev:/run/udev:ro \
  -v /var/apps:/host_apps:ro \
  -v /proc:/hostproc:ro \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /vol1/@appcenter:/vol1/@appcenter:ro \
  -v /vol2/@appcenter:/vol2/@appcenter:ro \
  -v /vol3/@appcenter:/vol3/@appcenter:ro \
  -v /vol4/@appcenter:/vol4/@appcenter:ro \
  -v /usr/local/apps/@appcenter:/usr/local/apps/@appcenter:ro \
  window-composer:1.0.0
```

容器需以 `privileged` + root 运行：Xorg 的 modesetting 驱动要通过内核 KMS/DRM
打开 DRM master 才能把画面送到显示器。收窄权限的最小集合（`--device /dev/dri` +
`--device-cgroup-rule='c 226:* rmw'` + `--cap-add SYS_ADMIN`）与说明见 INSTALL.md。

### 容器持久卷与挂载

容器 `/data` 目录必须挂载宿主机持久存储：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 系统配置（web 端口、背景色、开机自启、默认音频设备、目标显示器、定时截图间隔） |
| `layout.json` | 保存的当前窗口布局（应用名、URL/参数、坐标、宽高、全屏标记；坐标为**目标输出局部坐标**），开机自动恢复的那一份 |
| `layout_profiles.json` | 用户保存的多套命名布局方案（可随时下拉切换，不影响 layout.json；文件损坏自动兜底为空） |
| `pids.json` | pid 注册表（pid → app_name 映射，Web 重启后仍能识别已运行应用） |
| `app.log` | 运行日志（`xorg.log` / `openbox.log` 单独记录） |
| `screenshot.png` | 最新手动截图文件 |
| `screenshots/` | 定时自动截图目录（`shot-时间戳.png`，自动只保留最新 200 张） |

宿主目录挂载（应用扫描与运行状态判定）：

| 宿主路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `/var/apps` | `/host_apps` | 飞牛应用根目录（manifest / ui/config 软链），只读 |
| `/proc` | `/hostproc` | 宿主进程表，判定飞牛应用运行状态，只读 |
| `/vol1/@appcenter` ~ `/vol4/@appcenter` | 同名 | 第三方应用实体（target 绝对软链的目标），只读；缺卷时 Docker 自动建空目录 |
| `/usr/local/apps/@appcenter` | 同名 | 飞牛内置应用（trim.影视/相册等）实体，只读 |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker 容器扫描（Docker Engine API），不加 `:ro` |
| `/run/udev` | `/run/udev` | **物理音频必需**：WirePlumber 据此枚举 ALSA 声卡；只读 |

> `/var/apps/<应用>/target` 是**绝对软链**：第三方应用指 `/volN/@appcenter`、
> 内置应用指 `/usr/local/apps/@appcenter`。只挂 `/var/apps` 时软链在容器内断裂——
> 能扫出应用名但读不到 `target/ui/config`，卡片没有 Web 入口（启动日志会 WARN
> 「target 软链在容器内断裂」）。appcenter 各卷必须与 `/var/apps` 成组挂载。

> Docker socket 不加 `:ro`：连接 unix socket 需要写权限，只读挂载在部分环境下
> `connect()` 会失败；本容器本来就以 `privileged` 运行，`:ro` 换不来实质的权限收紧。
> 不需要 Docker 扫描时删掉这一行即可 —— 「Docker 应用」分组会整体消失，其余功能不受影响。
> 同理，宿主上没有 `docker.sock` 时（或没挂），面板会显示「Docker 未接入」，不会报错。

> 设备访问（`/dev/dri`、`/dev/input`）由 `privileged` 覆盖，无需单独映射；
> 但 `/run/udev` 必须显式只读挂载 —— WirePlumber 枚举 ALSA 声卡依赖宿主 udev
> 数据库，缺了它 pactl 里只有 auto_null（Dummy Output），物理音频不可用。

### 容器内预装依赖

镜像基于 `debian:bookworm-slim`，构建期全部装好：

- `xserver-xorg-core` + `xserver-xorg-video-modesetting`：Xorg 与 DRM/KMS 输出（**所有接口通用**）
- `xserver-xorg-video-dummy`：无显示设备时的虚拟输出兜底
- `x11-utils` / `x11-xserver-utils` / `x11-apps`：xprop、xwininfo、xrandr、xsetroot、xdpyinfo、xlogo 等
- `openbox`：轻量 X11 窗口管理器（`rc.xml` 中 resistance=0，窗口移动精确落位）
- `xdotool`、`wmctrl`：窗口移动/缩放与 EWMH 真全屏
- `scrot`：X11 截图
- `pipewire` + `wireplumber` + `pipewire-pulse` + `pipewire-alsa` + `pulseaudio-utils`：音频核心、ALSA 声卡会话管理器（缺 wireplumber 时只有 auto_null）与 pactl 管理
- `alsa-utils`、`dbus`、`dbus-x11`：音频诊断、dbus 会话/系统总线（entrypoint 启动时拉起 `dbus-daemon --system` 并补齐 logind 占位目录，wireplumber 在容器内才能存活）
- `chromium`：默认应用显示引擎（代码自动检测，可换 firefox 等）
- `fonts-noto-cjk`、`fonts-dejavu-core`：中文界面字体（缺了会显示方块）
- `libgl1-mesa-dri`、`jq`、`coreutils`、`procps`、`ca-certificates`、`tzdata`
- `python3` + `python3-pip` + FastAPI/uvicorn/jinja2/psutil

## API 接口文档

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | Web 控制面板首页 |
| `/api/status` | GET | 系统状态（分辨率、端口、背景色、开机自启、默认音频设备、后端 backend、输出名/类型/坐标、目标显示器、全部输出列表、DRM 卡、热插拔事件与守护状态、旋转角度、定时截图间隔 screenshot_interval） |
| `/api/display/outputs` | GET | 列出全部显示输出（含未连接/未启用），含接口代号与可读类型名 |
| `/api/display/refresh` | POST | 立即重新探测输出并自动点亮"已连接但未激活"的接口 |
| `/api/display/set_output` | POST | 切换目标显示器 name（窗口布局的基准屏；空串 = 自动选择） |
| `/api/display/rotate` | POST | 旋转屏幕方向 degrees:0/90/180/270（越界窗口自动拉回可见区） |
| `/api/windows` | GET | 获取当前 X11 窗口列表（pid,wid,app_name,x,y,w,h,visible,fullscreen,above；最小化的本服务应用以 visible=false 补全；同时给出屏幕绝对坐标 screen_x/screen_y 与原点） |
| `/api/window/setrect` | POST | 设置窗口位置大小 pid,x,y,w,h（局部坐标，越界值后端自动钳制） |
| `/api/window/fullscreen` | POST | 设置窗口全屏/退出全屏 pid,fullscreen:bool（全屏窗口 setrect 前会自动先退出全屏） |
| `/api/window/minimize` | POST | 最小化/恢复窗口 pid,minimized:bool（恢复 = activate+raise） |
| `/api/window/activate` | POST | 激活并前置窗口 pid |
| `/api/window/set_above` | POST | 窗口置顶/取消置顶 pid,above:bool（EWMH above） |
| `/api/window/close` | POST | 关闭单个窗口 pid |
| `/api/apps/installed` | GET | 扫描已安装应用：飞牛应用（manifest+ui/config，含运行状态）+ 宿主 Docker 容器；返回 `docker_available` / `docker_count`；refresh=1 强制重扫 |
| `/api/app/start` | POST | 启动容器内程序 app_name, args（shlex 解析；进程秒退时返回失败原因） |
| `/api/app/launch_web` | POST | 把飞牛应用 Web 界面显示到显示器 app_name,url,x,y,w,h,fullscreen |
| `/api/app/stop` | POST | 停止应用 pid |
| `/api/app/stop_all` | POST | 关闭所有应用窗口，保留 Xorg/openbox |
| `/api/pids` | GET | 列出 Web 注册在册的 pid 与对应程序名（含存活状态） |
| `/api/audio/sinks` | GET | 获取音频输出设备列表（含 volume 0-100、muted；读不到时 volume=-1） |
| `/api/audio/sink_inputs` | GET | 获取当前所有音频播放流（诊断用） |
| `/api/audio/set_sink` | POST | 设置默认音频设备 sink_id（migrate_existing=1 时同步迁移正在播放的流） |
| `/api/audio/set_volume` | POST | 设置设备音量 sink_id,volume:0-100（后端钳制，自动失效缓存） |
| `/api/audio/set_mute` | POST | 设备静音/取消静音 sink_id,muted:bool |
| `/api/layout/save` | POST | 保存当前窗口布局（含程序名、URL/参数、坐标、宽高、全屏；跳过最小化窗口） |
| `/api/layout/load` | GET | 读取保存的布局 |
| `/api/layout/restore` | POST | 应用已保存布局（已开着的窗口只重定位，已关闭的自动拉起） |
| `/api/layout/profiles` | GET | 列出命名布局方案（只回名称与窗口数） |
| `/api/layout/save_profile` | POST | 把当前窗口布局另存为命名方案 name（≤40 字符，重名覆盖） |
| `/api/layout/apply_profile` | POST | 应用命名方案 name（临时摆放窗口，不改主布局） |
| `/api/layout/delete_profile` | POST | 删除命名方案 name |
| `/api/settings/set_web_port` | POST | 修改 Web 端口 new_port |
| `/api/settings/set_bg_color` | POST | 修改桌面背景色 bg_color |
| `/api/settings/set_auto_start` | POST | 设置开机自启 enable:bool；挂载 docker.sock 时**实时同步 Docker 重启策略**（false→no，true→unless-stopped），无需重启容器 |
| `/api/access/status` | GET | 公开（无需令牌）：返回是否启用访问令牌、是否仍为出厂初始令牌（不泄露令牌内容） |
| `/api/access/login` | POST | 令牌登录 token：与飞牛 iframe 入口同源的请求自动免令牌 |
| `/api/access/token` | GET | 查看当前访问令牌（需已授权） |
| `/api/access/set_token` | POST | 设置自定义令牌 token（6~64 位，限字母数字与 `. _ @ -`，需已授权） |
| `/api/access/rotate` | POST | 随机轮换访问令牌（需已授权，返回新令牌） |
| `/api/access/set_required` | POST | 开关访问令牌 required:bool（需已授权） |
| `/api/settings/set_display_resolution` | POST | 设置画布换算分辨率 width, height |
| `/api/settings/set_screenshot_interval` | POST | 定时自动截图间隔 seconds（0 关闭，否则 10~3600） |
| `/api/screenshot` | POST | 执行截图（手动一张，存 `/data/screenshot.png`） |
| `/screenshot.png` | GET | 获取截图图片 |
| `/api/logs` | GET | 读取运行日志 |

## 使用说明

1. 部署容器（应用中心安装 / compose / docker run），映射 Web 端口（默认 8181）
2. 浏览器访问 `http://nas-ip:8181` 打开控制面板
3. **确认显示器**：面板「目标显示器」卡片会列出全部接口及其状态；若目标不是你要的那块屏，点一下切换
4. 显示应用：**双击应用卡片** = 该应用直接整屏显示；或单击卡片选中后在画布拖出显示区域（多应用同屏）
5. 窗口调整：拖动画布上的区域移动/缩放（右下角手柄），靠近边缘自动吸附，区域间防重叠
6. 布局：调整好后点【保存当前布局】；容器重启会自动恢复全部窗口（含位置和全屏状态）。运行中点【应用已保存布局】：已开着的应用只重新定位不重启，已关闭的会自动拉起
7. 音频：切换音频输出设备，正在播放的应用会立即迁移到新设备
8. 截图：点击【屏幕截图】，截图完成后弹窗预览
9. 修改背景色、Web 端口、开机自启：修改后需要重启容器生效
10. 屏幕方向：画布工具栏 0°/90°/180°/270° 按钮，即时生效（xrandr），越界窗口自动拉回可见区

## 测试

**三套离线自检，容器内外均可运行，不需要真实显卡/显示器：**

```bash
python3 tests/test_offline.py          # 纯逻辑单元测试
bash    tests/test_entrypoint_drm.sh   # entrypoint 的 DRM 卡选择规则（假 sysfs 树）
python3 scripts/validate-package.py    # 飞牛 FPK 打包结构校验（74 项）
```

覆盖范围：
- `test_offline.py`：pactl 输出解析、按进程音频路由与批量迁移、settings/layout/pids 读写、
  应用日志写入、**通用多输出解析（HDMI/DP/USB-C/VGA/DVI/eDP 分类、目标输出选择、可见区外接矩形）**、
  输出后端探测（xrandr 桩数据）、截图返回码判定、窗口矩形钳制、**越界回屏（含多显示器原点平移）**。
  涉及 xdotool/xwininfo/wmctrl/scrot 的调用全部打桩，真实环境验证见 dev-doc.md 第 7 节。
- `test_entrypoint_drm.sh`：多显卡选择、USB-C 识别为 DP-*、无 DRM 回退、设备节点缺失、
  连接器目录不误判、`list_connectors` 格式等 9 个场景。
- `validate-package.py`：manifest 必填字段与版本格式、privilege/resource/ui/wizard 的 JSON 合法性、
  ICON 尺寸、生命周期脚本齐全、compose 镜像 tag 与版本一致、`pull_policy: never`、离线镜像 tar 存在性。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FNWC_DATA_DIR` | `/data` | 持久化数据目录（本地调试可重定位） |
| `WC_TARGET_OUTPUT` | （空） | 启动时指定目标显示器（如 `HDMI-A-1` / `DP-1`）；空 = 自动选 primary / 首个已连接输出。面板里的选择优先于此变量 |
| `WC_DISPLAY_POLL_INTERVAL` | `5` | 显示器热插拔轮询间隔（秒）；`0` 关闭自动检测，改为手动调 `/api/display/refresh` |
| `WC_FORCE_BACKEND` | （空） | 强制覆盖面板显示的输出后端（drm/virtual/headless），仅调试用 |
| `WC_XORG_MODE` | （entrypoint 设置） | Xorg 实际启动模式（`drm` / `virtual`），用于区分"虚拟输出"与"探测失败" |
| `WC_DRM_CARD` | （entrypoint 设置） | 探测到的 DRM 卡名（如 `card1`） |
| `WC_XORG_CONF` | （空） | 指定自定义 Xorg 配置文件（特殊显卡/时序场景） |
| `WC_SYS_DRM_ROOT` | `/sys/class/drm` | sysfs 路径重定向（测试用） |
| `WC_DEV_DRI_DIR` | `/dev/dri` | 设备节点路径重定向（测试用） |
| `WC_FNOS_PORT` | `5666` | 飞牛系统 Web 端口（无独立端口应用的反代入口） |
| `WC_HOST_GATEWAY` | `172.17.0.1` | 容器访问宿主的网关地址 |
| `LOG_MAX_BYTES` | `2097152` | 单个日志文件轮转阈值（字节） |
| `LOG_KEEP_LINES` | `2000` | 日志轮转后保留的尾部行数 |

## 重要限制与注意事项

1. **Wayland/X11 限制**：网页前端不能直接抓取显示器上的鼠标事件；拖拽是网页模拟画布，按比例换算坐标，调用后端 API 修改窗口位置。
2. **仅支持 X11 程序**：Xorg 栈下原生 Wayland 程序无法直接运行；飞牛应用界面均为 Web UI，由浏览器（X11）渲染，不受影响。
3. **USB-C 与物理 DP 无法区分**：内核里二者都是 `DP-*` 连接器，面板统一显示为「DisplayPort / USB-C」。这是内核层面的限制，不是本项目的取舍。
4. 背景色由 xsetroot 在启动时设置，修改需重启容器生效。
5. 布局恢复采用轮询等待窗口出现（最长 10 秒）；启动极慢的应用可能出现几何设置失败，可增大 `WINDOW_WAIT_TIMEOUT`。
6. 截图依赖 scrot，需 Xorg 正常运行且输出正常；无显示器/未接屏时截图失败或黑屏（失败时不会返回旧截图）。
7. chromium 多窗口依赖独立 user-data-dir（`/tmp/chromium-profile-*`），容器重建后临时 profile 丢失属正常现象。
8. 应用布局时靠"程序名 / URL"识别屏幕上是否已有同一应用：已存在则只重定位不重启，因此同名窗口已开着时不会再新开一个。
9. **一个 X 屏幕上的空白区**：多显示器时若两块屏之间存在没有输出的空隙，窗口落进去仍算"坐标合法"（可见区按所有已启用输出的外接矩形计算）。实际情况中相邻输出通常紧贴，暂不处理该边界。

## 故障排查

1. **Web 页面打不开**：检查容器是否正常启动、端口是否被占用；查看 `/data/app.log`
2. **面板能开但显示器全黑**：看 `/data/app.log` 与 `/data/xorg.log`，搜「选中 DRM 设备」
   - 出现「未找到可用的 /dev/dri/card*」→ 容器没拿到显卡设备，检查是否 `privileged`
   - 出现「降级为 dummy 虚拟输出」→ Xorg 起在虚拟屏上，真实显示器不会出画面
   - 确认面板「目标显示器」选的是**接了屏且已启用**的那个口
3. **面板「目标显示器」列表为空**：`xrandr` 没枚举到任何输出，通常是容器没映射显卡设备，或宿主内核没加载显示驱动
4. **换机器/换接口后不工作**：入口脚本每次启动都重新探测 DRM 卡并生成 Xorg 配置，重启容器即可；若指定了具体目标显示器而接口名变了，重新选一次
5. **窗口位置无法控制**：查看 `/data/openbox.log`，openbox 挂掉会被自动拉起，但期间 windowmove 无效。另确认 `/etc/xdg/openbox/rc.xml` 未被人替换（resistance 必须为 0）
6. **应用启动失败**：检查程序名是否正确、是否为 X11 程序；Web 层日志会明确记录启动失败原因
7. **布局恢复/应用布局失败**：查看日志中的「恢复布局」条目，大概率是应用启动超时，可增大 `main.py` 的 `WINDOW_WAIT_TIMEOUT`
8. **没有音频输出（只有 Dummy Output / auto_null）**：容器内 `pactl list short sinks` 只看到 auto_null 时，按顺序排查 —— ① compose/docker run 是否挂了 `/run/udev:/run/udev:ro`（WirePlumber 枚举 ALSA 声卡的数据源）；② 启动日志中 WirePlumber 是否存活（见 `/data/wireplumber.log`）；③ `pactl list short cards` 是否有 `alsa_card.*`。默认输出被错选为 `pcspkr`（主板蜂鸣器）时 entrypoint 会自动纠偏到真实声卡。另用 `/api/audio/sink_inputs` 查看播放流实际落在哪个声卡
9. **截图黑屏**：确认显示器的物理连接与供电，宿主内核显卡驱动正常

## License

MIT License © 2026 Misite齊 (https://github.com/MisiteQ)

可自由二次开发，用于飞牛 NAS 容器场景。详见 [LICENSE](LICENSE)。
