# window-composer 飞牛 NAS 安装说明

把运行在飞牛 fnOS 上的应用，以窗口形式编排并输出到 NAS 的**物理显示器**。

- 支持 **全部显示输出接口**：HDMI、DisplayPort、**USB-C（DP Alt Mode / 雷电扩展坞）**、VGA、DVI、eDP
- **安装包轻量、镜像按需拉取**：安装包仅约 130 KB，安装时自动对多个 ghcr 加速源测速，择优下载与本机架构匹配的镜像（也提供镜像内置的离线包）
- 浏览器远程控制面板：画框布局、边缘吸附、多窗口不重叠、屏幕旋转、物理音频路由、实时画面与截图
- **访问令牌保护**：面板默认只对飞牛桌面入口与本机开放，外部 IP 直接访问需令牌

> **1.0.0 已在飞牛 fnOS 真机验证**：x86_64 主机 + Docker 28.x，HDMI 接 1366×768 显示器，
> Xorg/DRM 真实输出、PipeWire + WirePlumber 物理声卡（Intel HDA）、面板音频控制、
> 截图、访问令牌、开机自启策略联动全部实测通过。

---

## 一、安装前准备

| 项目 | 要求 |
| --- | --- |
| NAS 系统 | 飞牛 fnOS ≥ 0.9.27（应用中心支持 `docker-project` 资源），x86_64 或 ARM64 |
| NAS 软件 | 仅需 **Docker**（fnOS 自带，应用中心安装时会自动调用） |
| 显示器 | 任一接口接到 NAS 显卡输出（HDMI / DP / USB-C / VGA / DVI）；**不接显示器也能装**，会以 1920×1080 虚拟输出运行，面板照常可用 |
| 音箱/耳机 | 可选。接 NAS 声卡的 3.5mm/HDMI 音频输出即可；不接不影响安装 |
| 网络 | **安装时需要联网**（约 450 MB 镜像，自动测速择优拉取，只下载一次）；之后运行无需联网 |
| 安装包 | **标准包** `window-composer-1.0.0.fpk`（约 130 KB，文件名**不带** `-offline`） |

> 🔌 **完全离线环境**：请改用离线包 `window-composer-1.0.0-offline.fpk`（约 441 MB，
> 镜像内置），安装时不联网。离线包由 `bash scripts/build-package.sh --offline` 生成。
>
> 辨别方法：标准包约 130 KB、离线包约 441 MB。`.fpk` 本质是 gzip(tar.gz)，
> **不是 zip**，不要手工解压再压缩。

安装前建议先把显示器接好并通电（不接也没关系，应用支持开机后热插拔自动点亮）。

---

## 二、应用中心安装（推荐）

### 步骤

1. 把 `window-composer-1.0.0.fpk` 上传到 NAS 任意目录（或留在电脑上通过浏览器选择）。
2. 打开飞牛桌面 → **应用中心** → 右上角 **手动安装** → 选择该 `.fpk`。
3. 安装向导「显示设置」页填写：

   | 向导项 | 填什么 | 默认 |
   | --- | --- | --- |
   | 目标显示器 | 接口名，如 `HDMI-A-1` / `DP-1` / `VGA-1`；**留空 = 自动选择已连接的那块屏** | 空 |
   | 热插拔检测间隔 | 秒数；`0` = 关闭热插拔自动检测 | `5` |
   | 时区 | 容器时区，国内保持默认即可 | `Asia/Shanghai` |

4. 等待安装完成：安装器会逐个测速 ghcr 加速源（daocloud / 南大 / 1ms 等），
   从最快的源下载约 450 MB 镜像（具体时长取决于网速；某源失败自动切换下一个）。
5. 点 **启动**，在飞牛桌面点击 **Window Composer** 图标打开控制面板。

安装器自动完成的事（可在「应用数据目录/lifecycle.log」查看）：

```
预检 docker 与宿主显示连接器
→ 对 ghcr 加速源实测延迟并排序
→ docker pull 最快源上与本机架构匹配的镜像，打成本地 tag
→ docker compose 按内置 docker-project 启动容器（privileged + host 网络，pull_policy=missing）
→ 容器内 entrypoint：dbus(会话+系统) → PipeWire → WirePlumber → Xorg → openbox → 面板
```

> 镜像拉取失败（所有源都不通）时安装不会卡死：日志会记录失败原因，修复网络后
> 在应用中心重新执行一次安装即可（本地已有的层会复用，支持断点续传）。

### 安装后修改向导配置

应用中心 → Window Composer → **设置**，可改目标显示器、检测间隔、时区；保存后安装器会自动重建容器生效。

---

## 三、首次打开与访问令牌（重要）

面板默认开启**访问令牌保护**，两种打开方式：

| 打开方式 | 是否需要令牌 |
| --- | --- |
| **飞牛桌面点 Window Composer 图标**（应用内 iframe，同主机来源） | **不需要**，自动放行 |
| 浏览器直接访问 `http://<NAS_IP>:8181` | 需要令牌，先看到令牌输入页 |
| NAS 本机 / SSH 回环访问（127.0.0.1） | 不需要 |
| 任意外部网站用 iframe 套嵌面板 | **一律拒绝**（防点击劫持） |

### 出厂初始令牌

全新安装后，访问令牌初始化为**固定初始令牌 `admin123`**：

- 浏览器直连 `http://<NAS_IP>:8181` → 令牌输入页会直接提示当前为出厂初始令牌，
  **输入 `admin123` 即可首次登录**；
- 登录页面板顶部会出现黄色警告条，提醒尽快修改；
- **请在首次登录后立即修改**：面板「访问令牌」卡片 → 输入两遍自定义新令牌
  （6~64 位，仅限字母、数字与 `. _ @ -`）→【保存新令牌】。修改后其他设备需用
  新令牌重新登录，当前浏览器自动保持登录；
- 也可以【随机重置】生成 32 位随机令牌，或在完全可信的内网临时【关闭保护】。

> 初始令牌是公开的出厂约定，只解决"第一次怎么进面板"，不能长期使用——
> 只要 NAS 暴露在局域网，任何知道地址的人都能试 `admin123`。

**其他查看/修改途径**

- 从**飞牛桌面图标**进入面板始终免令牌，即使忘了自定义令牌也能进面板查看/修改；
- 令牌保存在应用数据目录 `data/settings.json` 的 `access_token` 字段
  （`access_token_customized` 标记是否已脱离出厂初始值）；
- 批量部署可用环境变量 `WC_ACCESS_TOKEN` 在首次启动前预置令牌（预置即视为已定制，
  不会出现初始令牌警告）。

浏览器登录成功后会记住令牌一年（HttpOnly Cookie）。其他用法：

```text
http://<NAS_IP>:8181/?wc_at=你的令牌          # 查询参数
X-Access-Token: 你的令牌                       # 调用 API 时用请求头
```

> 安全说明：面板能控制显示器上的窗口与 Docker 应用，因此默认拒绝所有外部未授权访问。
> 跨站伪造的浏览器写请求（带外域 Origin）会直接返回 403。

---

## 四、安装后验证（5 项，约 2 分钟）

1. **看容器状态**：应用中心显示「运行中」；或 SSH 执行
   `docker ps` 能看到 `window-composer` 容器，状态 `healthy`。
2. **看启动日志**：应用数据目录 `data/app.log` 中应依次出现
   「启动 dbus system」「启动 WirePlumber」「Xorg 已就绪（DRM 真实输出）」
   「开机自启策略已同步」，无持续 WARN。
3. **看显示器**：物理屏幕被点亮（黑色背景正常，桌面背景色可在面板改）。
   日志为「降级为 dummy 虚拟输出」时说明容器没拿到显卡，检查容器是否以特权运行。
4. **看音频**：面板音频卡片中输出设备应是真实声卡（如
   `alsa_output.pci-...` / “xxx cAVS Stereo”），而不是 `auto_null`（Dummy Output）。
   主板蜂鸣器（pcspkr）即使存在也不会成为默认输出。拖一下音量滑块即测控制链路。
5. **看截图**：面板点【屏幕截图】，能返回当前屏幕画面（证明 Xorg 物理输出链路正常）。

面板「目标显示器」卡片会列出 NAS 上全部视频接口及连接状态，选错口可一键切换，无需重启。

---

## 五、日常使用要点

- **显示应用**：双击应用卡片 = 该应用整屏显示；单击选中后在画布拖出区域 = 多应用同屏，
  区域自动防重叠、边缘吸附。布局调好点【保存当前布局】，容器重启后自动恢复全部窗口。
- **飞牛 / Docker 应用**：面板自动扫描飞牛已装应用（读 `/var/apps`）与发布了端口的
  Docker 容器（通过 docker.sock），点一下即把其 Web 界面渲染到显示器。
- **热插拔**：开机后再接显示器，约一个检测周期（默认 5 秒）内自动点亮并拉回越界窗口；
  换接口、换显示器同理，无需重启容器。
- **屏幕旋转 / 分辨率**：画布工具栏 0°/90°/180°/270° 即时生效。
- **开机自启开关**：面板设置中的开关会**实时写入 Docker 重启策略**
  （开 = `unless-stopped`，NAS 重启后自动拉起；关 = `no`），不需要手动改容器。
- **音频**：切换输出设备时正在播放的声音会立即迁移；每个设备有独立音量/静音。
- **日志与截图**：均在应用数据目录（`app.log`、`xorg.log`、`wireplumber.log`、
  `screenshot.png`、`screenshots/`），日志超过 2 MB 自动轮转只留尾部 2000 行。

### 数据备份

应用数据目录（fnOS 为应用分配的持久目录，容器内挂载为 `/data`）中需要时可备份：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 端口、背景色、**访问令牌**、开机自启、默认音频设备、目标显示器 |
| `layout.json` | 开机自动恢复的窗口布局 |
| `layout_profiles.json` | 命名布局方案 |

卸载重装默认保留数据目录；删除应用数据目录等于恢复出厂（令牌也会重新生成）。

### 升级 / 卸载

- **升级**：应用中心手动安装新版本 `.fpk`（升级回调会重新导入镜像并重建容器），数据目录沿用。
- **卸载**：应用中心 → 卸载。默认保留已导入镜像（省去重装时的镜像导入时间）；
  卸载向导里把「是否同时删除已导入的 docker 镜像」改为 `1` 可彻底清理。

---

## 六、独立部署（不走应用中心）

适合自行 `docker compose` / `docker run` 管理的用户。镜像可直接从 ghcr 拉取
（`docker pull ghcr.io/misiteq/window-composer:1.0.0`，多架构），
或从源码自行构建（`docker build -t window-composer:1.0.0 .`）。

### docker compose

```bash
docker compose up -d        # 使用仓库根目录的 docker-compose.yml
# 打开 http://<NAS_IP>:8181
```

compose 关键配置（已内置，无需手改）：

```yaml
privileged: true                                       # Xorg DRM master 必需
network_mode: host                                     # 面板直接监听 NAS 的 8181
volumes:
  - ./data:/data                                       # 布局/设置/日志/截图
  - /run/udev:/run/udev:ro                             # 物理音频必需（ALSA 枚举数据源）
  - /var/apps:/host_apps:ro                            # 飞牛应用扫描
  - /proc:/hostproc:ro                                 # 飞牛应用运行状态
  - /var/run/docker.sock:/var/run/docker.sock          # Docker 应用扫描 + 自启开关联动
  # ↓↓ 飞牛应用 target 绝对软链的实体目录（缺了会"扫得到名字、没有 Web 入口"）
  - /vol1/@appcenter:/vol1/@appcenter:ro               # 第三方应用实体（按存储卷）
  - /vol2/@appcenter:/vol2/@appcenter:ro
  - /vol3/@appcenter:/vol3/@appcenter:ro
  - /vol4/@appcenter:/vol4/@appcenter:ro
  - /usr/local/apps/@appcenter:/usr/local/apps/@appcenter:ro  # 飞牛内置应用（trim.*）
```

### docker run

```bash
docker run -d --name window-composer \
  --restart unless-stopped \
  --privileged \
  --network host \
  -e TZ=Asia/Shanghai \
  -v "$(pwd)/data:/data" \
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

> - `/var/apps` 与 5 个 appcenter 目录是「飞牛应用扫描」的一组挂载：
>   `/var/apps/<应用>/target` 是**绝对软链**，第三方应用指向
>   `/vol1`~`/vol4` 下的 `@appcenter/<应用>`、内置应用（trim.影视/相册等）指向
>   `/usr/local/apps/@appcenter/<应用>`。只挂 `/var/apps` 会在容器内软链断裂——
>   扫得到应用名却读不到 `target/ui/config`，卡片没有 Web 入口。
>   `/vol2`~`/vol4` 在没有对应存储卷时 Docker 会自动建空目录，无害。
> - 启动日志里 `飞牛应用挂载检查通过：N 个应用 target 可访问` 表示这组挂载正常；
>   出现 `target 软链在容器内断裂` 的 WARN 时按点名的应用补挂对应卷。
> - `/run/udev` **必须挂且只读**：WirePlumber 靠宿主 udev 数据库枚举 ALSA 声卡，
>   缺了它音频只有 Dummy Output。设备节点（`/dev/dri`、`/dev/input`）由 `privileged` 覆盖，不用单独映射。
> - docker.sock 不能加 `:ro`；不挂时面板显示「Docker 未接入」，其余功能正常，
>   开机自启开关退化为仅记录配置。
> - 与应用中心安装二选一：两者容器名都是 `window-composer`，互换部署方式前先
>   `docker rm -f` 掉旧容器。

### 关于 `--privileged`

Xorg modesetting 驱动需经内核 KMS/DRM 打开 DRM master 输出画面，特权 + root 最稳。
收窄权限的最小集合：

```bash
--device /dev/dri \
--device-cgroup-rule='c 226:* rmw' \
--cap-add SYS_ADMIN \
--security-opt seccomp=unconfined
```

（用 `ls -l /dev/dri` 确认主设备号；VGA 文本设备主设备号为 13。）

---

## 七、接口与显示器说明

| 接口 | 内核连接器名 | 支持 | 备注 |
| --- | --- | --- | --- |
| HDMI | `HDMI-A-1`、`HDMI-1` | ✅ | 最常见 |
| DisplayPort | `DP-1`、`DP-2` | ✅ | |
| USB-C（DP Alt Mode）/ 雷电扩展坞 | `DP-*` | ✅ | 与物理 DP 同族，面板统一显示为 DisplayPort / USB-C |
| VGA | `VGA-1` | ✅ | 服务器 BMC、老主板 |
| DVI | `DVI-I-1`、`DVI-D-1` | ✅ | |
| eDP / LVDS | `eDP-1`、`LVDS-1` | ✅ | 一体机内置屏 |
| 无显示器 | — | ✅ | dummy 虚拟输出 1920×1080，面板可用 |

所有接口在内核里都是 DRM connector，modesetting 驱动对接口类型无感；容器启动时按
`/sys/class/drm/*/status` 自动挑接了显示器的那张卡（「板载 BMC VGA + iGPU」不会选错）。

**多显示器**：面板「目标显示器」点选哪块屏，布局就以那块屏的左上角为原点摆放窗口。

---

## 八、常见问题

**手动安装提示「不是有效的程序文件」**
包是 zip 或传输损坏。确认包未经解压重打包（标准包约 130 KB、离线包约 441 MB）。
Linux/macOS 上执行 `head -c 2 文件 | xxd`，前两字节应为 `1f8b`。重新下载后再试。

**安装时镜像一直拉取失败**
说明当前网络到所有内置 ghcr 加速源都不通。可按日志（`lifecycle.log`）里测速结果
排查网络；或改用离线包 `window-composer-1.0.0-offline.fpk`（镜像内置）安装。

**浏览器打开面板显示令牌输入页 / 返回 401**
正常安全机制。全新安装先用出厂初始令牌 `admin123` 登录；之后用你自定义的令牌，
或在 `data/settings.json` 查 `access_token`；也可用 `?wc_at=令牌`。
令牌忘了可从飞牛桌面图标免令牌进入面板，在「访问令牌」卡片查看或重新设置。

**外域网站 iframe 打不开面板（401）**
设计如此：只允许飞牛同主机入口 iframe 套嵌，防止面板被其他网站嵌套点击劫持。

**显示器黑屏 / 不出画面**
1. 查 `data/app.log`、`data/xorg.log`：出现「选中 DRM 设备」说明认到显卡。
2. 「未找到可用的 /dev/dri/card*」→ 容器没拿到显卡（应用中心安装理论上不会，自行部署检查 `privileged`）。
3. 「降级为 dummy 虚拟输出」→ 当前是虚拟屏，物理屏无画面；接好显示器触发热插拔或重启容器。
4. 确认面板「目标显示器」选中的是接了屏的那个口（状态为 connected/enabled）。

**音频只有 Dummy Output（auto_null）**
自行部署时检查是否挂了 `/run/udev:/run/udev:ro`，以及日志中 WirePlumber 是否存活
（`data/wireplumber.log`）；容器内执行 `pactl list short cards` 应有 `alsa_card.*`。
应用中心安装的 compose 已内置该挂载。默认输出被选成主板蜂鸣器（pcspkr）的情况应用会自动纠偏到真实声卡。

**只扫出 Docker 容器，看不到飞牛应用（系统/第三方分组为空）**
缺少飞牛应用数据源挂载。按部署方式处理：
1. 应用列表上方会出现黄色提示条，启动日志（`data/app.log`）也有对应 WARN，先看它点名缺什么。
2. 自行 `docker run` / compose 部署：必须同时挂 `/var/apps:/host_apps:ro`、
   `/proc:/hostproc:ro`、`/vol1`~`/vol4` 下的 `@appcenter` 与 `/usr/local/apps/@appcenter`
   （模板见上文「独立 Docker 部署」一节，仓库根目录 `docker-compose.yml` 已内置），
   挂齐后重启容器。注意 target 是绝对软链，只挂 `/var/apps` 会扫到名字但没有 Web 入口。
3. 应用中心安装的正式版 compose 已内置全部挂载；若仍为空，多半是从旧版本挂载模板
   手动升级而来——卸载后重新用新版 `.fpk` 安装，或手动把上述挂载补进容器配置。

**同一个应用在「第三方应用」和「Docker 应用」里各出现一次**
旧版本的去重只比容器名，遇到驼峰目录名（如 `FnMessageBot`）与连字符容器名
（`fn-message-bot`）不一致时会漏判。1.0.0 已改为「宿主发布端口 + 名称归一化」双重
匹配，并把 Docker 的运行状态回填给飞牛条目；仍重复时点一次「重新扫描」
（等价 `GET /api/apps/installed?refresh=1`）清掉缓存。

**「Docker 未接入」/ 看不到 Docker 容器**
确认容器挂了 `/var/run/docker.sock`。飞牛应用中心安装的应用显示在「飞牛应用」分组，
只有发布到宿主端口的容器才有可点击的 Web 入口。

**8181 端口被占用**
应用中心会提示端口冲突；自行部署可改 `data/settings.json` 的 `web_port` 后重启容器
（compose/run 为 host 网络，端口直接占用宿主端口）。

**换机器 / 换显示器接口**
无需重新打包：entrypoint 每次启动重新探测显卡与连接器。若向导里写死过目标显示器而
新机器接口名不同，在面板重新选择即可。

---

## 附录：开发者打包

```bash
bash scripts/build-package.sh                 # 标准联网包 → dist/window-composer-1.0.0.fpk（~130 KB）
bash scripts/build-package.sh --offline       # 离线包 → dist/window-composer-1.0.0-offline.fpk（~441 MB）
bash scripts/build-package.sh --image-only    # 只构建并导出镜像 tar（跨机协作）
python3 scripts/validate-package.py --fpk dist/window-composer-1.0.0.fpk   # 校验成品
```

镜像需先推送到 ghcr（多架构 manifest list），标准包安装时才能拉取：

```bash
docker tag window-composer:1.0.0        ghcr.io/misiteq/window-composer:1.0.0-amd64
docker tag window-composer-arm:1.0.0    ghcr.io/misiteq/window-composer:1.0.0-arm64
docker push ghcr.io/misiteq/window-composer:1.0.0-amd64
docker push ghcr.io/misiteq/window-composer:1.0.0-arm64
docker buildx imagetools create --tag ghcr.io/misiteq/window-composer:1.0.0 \
  ghcr.io/misiteq/window-composer:1.0.0-amd64 ghcr.io/misiteq/window-composer:1.0.0-arm64
```

离线构建依次执行：docker build → **容器内镜像自检（关键二进制 +
300+ 项离线断言，`pipefail` 保证测试失败即中止）** → docker save 导出镜像 →
fnpack 出 fpk → 成品格式复验。国内网络用阿里云 Debian 源时注意用 **http**
（精简基础镜像在安装 ca-certificates 之前无法走 https）：

```bash
WC_APT_MIRROR=http://mirrors.aliyun.com/debian bash scripts/build-package.sh --offline
```

其他构建参数、跨机器出包、ARM 打包等细节见 [README.md](README.md) 与
`scripts/build-package.sh` 顶部注释。
