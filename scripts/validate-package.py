#!/usr/bin/env python3
"""离线校验 packaging/fnos 是否符合飞牛 fnpack 的打包规则。

  python3 scripts/validate-package.py
  python3 scripts/validate-package.py --require-image   # 离线包用：必须附带镜像 tar

为什么要有这个脚本：fnpack 是 Go 二进制（Windows/Linux/macOS 各有版本），
在没有它的机器上无法"先验证再打包"。这里把 fnpack 的检查项（manifest 必填字段、
privilege/resource 是否合法 JSON、ICON 尺寸、app/cmd/wizard 目录存在、
desktop_uidir 目录存在）以及本应用自己的约定（镜像 tag 与版本号一致、
生命周期脚本齐全、compose 里引用的环境变量有声明）全部静态复刻一遍，
让"打包前的失败"发生在本地，而不是用户装到 NAS 上才发现。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "packaging", "fnos")

# fnOS manifest 必填字段（见 https://developer.fnnas.com/docs/guide）
REQUIRED_MANIFEST = ["appname", "version", "display_name", "desc", "platform", "source"]
# cmd/ 下约定的生命周期脚本
LIFECYCLE = [
    "main", "install_init", "install_callback",
    "upgrade_init", "upgrade_callback",
    "uninstall_init", "uninstall_callback",
    "config_init", "config_callback",
]

_passed = 0
_failed = []
_warned = []


def check(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed.append(name)
        print(f"  [FAIL] {name} {detail}")


def warn(name, detail=""):
    _warned.append(name)
    print(f"  [WARN] {name} {detail}")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def parse_manifest(text):
    """解析 INI 形态的 manifest（key=value，; 与 # 起注释）。"""
    data = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#", "[")):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


def png_size(path):
    """只读 PNG 头 24 字节拿到宽高，避免依赖 Pillow。"""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


# ---------------------------------------------------------------- 成品 .fpk 校验
# 这一节是"装到 NAS 上才发现的错"里代价最大的一个：.fpk 到底是 gzip/tar.gz 还是 zip。
# 实测（fnpack 1.2.3 产物）：.fpk = gzip(tar)，app/ 打成 app.tgz，
# manifest 里带 checksum = md5(app.tgz)。若打成 zip，飞牛应用中心会直接报
# 「不是有效的程序文件」。所以打包脚本每次跑完都拿这里复验一遍。
FPK_REQUIRED = [
    "manifest",
    "app.tgz",
    "ICON.PNG",
    "ICON_256.PNG",
    "config/privilege",
    "config/resource",
    "cmd/main",
    "cmd/install_init",
    "cmd/install_callback",
    "cmd/upgrade_init",
    "cmd/upgrade_callback",
    "cmd/uninstall_init",
    "cmd/uninstall_callback",
    "cmd/config_init",
    "cmd/config_callback",
    "wizard/install",
    "wizard/config",
]


def validate_fpk(path):
    """校验成品 .fpk 的封装格式与内容清单。"""
    global _passed
    print(f"\n成品校验：{path}")

    if not os.path.isfile(path):
        check(".fpk 文件存在", False, path)
        return

    raw_head = open(path, "rb").read(4)
    check(".fpk 是 gzip/tar.gz（魔术字节 1f 8b）", raw_head[:2] == b"\x1f\x8b",
          f"实际 {raw_head.hex(' ')}"
          + ("；这是 ZIP！飞牛会报「不是有效的程序文件」" if raw_head[:2] == b"PK" else ""))
    if raw_head[:2] != b"\x1f\x8b":
        return

    try:
        tar = tarfile.open(path, "r:gz")
        names = tar.getnames()
        check("gzip 里是合法 tar", True)
    except tarfile.TarError as e:
        check("gzip 里是合法 tar", False, str(e))
        return

    check("app/ 未以目录形式出现（必须是 app.tgz）",
          not any(n == "app" or n.startswith("app/") for n in names),
          [n for n in names if n == "app" or n.startswith("app/")][:3])
    for need in FPK_REQUIRED:
        check(f"含 {need}", need in names)

    # manifest：字段 + checksum 必须等于 md5(app.tgz)
    fpk_manifest = {}
    try:
        mtext = tar.extractfile("manifest").read().decode("utf-8")
        fpk_manifest = parse_manifest(mtext)
        check("manifest 可解析且非空", bool(fpk_manifest))
    except Exception as e:  # noqa: BLE001
        check("manifest 可读取", False, str(e))

    for field in REQUIRED_MANIFEST:
        check(f"成品 manifest.{field} 已声明", bool(fpk_manifest.get(field)),
              fpk_manifest.get(field))

    try:
        app_bytes = tar.extractfile("app.tgz").read()
        md5 = hashlib.md5(app_bytes).hexdigest()
        check("manifest.checksum == md5(app.tgz)",
              fpk_manifest.get("checksum") == md5,
              f"checksum={fpk_manifest.get('checksum')} md5={md5}")

        inner = tarfile.open(fileobj=io.BytesIO(app_bytes), mode="r:gz")
        inames = inner.getnames()
        uidir = fpk_manifest.get("desktop_uidir", "ui")
        for need in (f"{uidir}/config", "docker/docker-compose.yaml",
                     f"{uidir}/images/icon_64.png", f"{uidir}/images/icon_256.png"):
            check(f"app.tgz 含 {need}", need in inames)
        # 应用文件里不能有镜像 tar 之外的大块垃圾
        check("app.tgz 内条目数合理", 0 < len(inames) < 200, len(inames))
    except Exception as e:  # noqa: BLE001
        check("app.tgz 可读取", False, str(e))


# 镜像归档支持的扩展名（docker load 三种都能吃；.tar.gz/.tgz 免去用户自己解压）
IMAGE_SUFFIXES = (".tar", ".tar.gz", ".tgz")


def validate_image_archive(path, ver):
    """校验一个镜像归档确实是 docker 能 load 的格式。

    为什么单独校验：归档可能是从别的机器拷过来的（--image-tar），也可能被
    误当成普通 tar 塞进去。若它不是 docker save / OCI 格式，打包会"成功"，
    而 NAS 上 docker load 失败——那时安装已经看起来完成了，排查成本极高。
    """
    print(f"\n镜像归档校验：{path}")
    if not os.path.isfile(path):
        check("镜像归档存在", False, path)
        return

    size_mb = os.path.getsize(path) / 1024 / 1024
    check(f"镜像归档非空（{size_mb:.0f} MB）", size_mb > 1)

    with open(path, "rb") as f:
        magic = f.read(4)
    is_gz = magic[:2] == b"\x1f\x8b"
    if path.endswith((".tar.gz", ".tgz")):
        check("扩展名是 .tar.gz/.tgz 时内容确实是 gzip", is_gz, magic.hex(" "))

    try:
        tar = tarfile.open(path, "r:gz" if is_gz else "r:")
    except (tarfile.TarError, OSError) as e:
        check("归档是可读的 tar", False, str(e))
        return
    check("归档是可读的 tar", True)

    names = tar.getnames()
    is_docker_save = "manifest.json" in names
    is_oci = "oci-layout" in names and "index.json" in names
    check("是 docker save 格式（含 manifest.json）或 OCI 布局",
          is_docker_save or is_oci, f"顶层条目示例：{names[:6]}")

    if not is_docker_save:
        return
    try:
        mf = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        check("manifest.json 可解析", False, str(e))
        return
    check("manifest.json 是 JSON 数组且非空", isinstance(mf, list) and bool(mf))
    if not (isinstance(mf, list) and mf):
        return
    item = mf[0]
    tags = item.get("RepoTags") or []
    check("manifest.json 声明了 RepoTags", bool(tags), tags)
    check(f"镜像 tag 与 manifest.version 一致（{tags[0] if tags else '?'}）",
          any(t.endswith(":" + ver) for t in tags), tags)
    layers = item.get("Layers") or []
    check("manifest.json 声明了 Layers", bool(layers), len(layers))
    missing = [ln for ln in layers if ln not in names]
    check("Layers 里的每个文件都在归档内", not missing, missing[:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-image", action="store_true",
                    help="镜像归档必须存在（发布/交付时使用）")
    ap.add_argument("--image-tar", action="append", default=[],
                    help="校验指定的镜像归档 .tar/.tar.gz/.tgz（可重复）")
    ap.add_argument("--fpk", action="append", default=[],
                    help="额外校验已打好的成品 .fpk（可重复；打包脚本会自动带上）")
    args = ap.parse_args()

    print(f"校验打包目录：{os.path.relpath(PKG, ROOT)}")

    # ---------------------------------------------------------- 顶层结构
    print("fnpack 规则：目录与必需文件")
    check("packaging/fnos 存在", os.path.isdir(PKG), PKG)
    if not os.path.isdir(PKG):
        return finish()

    for d in ("app", "cmd", "wizard", "config"):
        check(f"{d}/ 目录存在", os.path.isdir(os.path.join(PKG, d)))

    for f in ("manifest", "ICON.PNG", "ICON_256.PNG",
              os.path.join("config", "privilege"), os.path.join("config", "resource")):
        check(f"{f} 存在", os.path.isfile(os.path.join(PKG, f)))

    # ---------------------------------------------------------- manifest
    print("manifest：必填字段与版本格式")
    mpath = os.path.join(PKG, "manifest")
    manifest = {}
    if os.path.isfile(mpath):
        manifest = parse_manifest(read(mpath))
    for field in REQUIRED_MANIFEST:
        check(f"manifest.{field} 已声明", bool(manifest.get(field)),
              f"缺失或为空（{field}）")

    ver = manifest.get("version", "")
    check("version 为 X.Y.Z 形式（飞牛强制）",
          bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), f"实际[{ver}]")
    check("source=thirdparty（第三方应用）",
          manifest.get("source") == "thirdparty", manifest.get("source"))
    check("platform 合法", manifest.get("platform") in ("x86", "arm", "all"),
          manifest.get("platform"))
    check("appname 合法（字母数字与 - _ .）",
          bool(re.fullmatch(r"[\w.\-]+", manifest.get("appname", ""))),
          manifest.get("appname"))
    check("checkport 为布尔值",
          manifest.get("checkport", "").lower() in ("true", "false"),
          manifest.get("checkport"))
    uidir = manifest.get("desktop_uidir", "ui")
    check(f"desktop_uidir={uidir} 对应目录存在",
          os.path.isdir(os.path.join(PKG, "app", uidir)))

    # ---------------------------------------------------------- JSON 合法性
    print("config / ui / wizard：JSON 必须合法")
    priv = None
    ppath = os.path.join(PKG, "config", "privilege")
    if os.path.isfile(ppath):
        try:
            priv = json.loads(read(ppath))
            check("config/privilege 是合法 JSON", True)
        except json.JSONDecodeError as e:
            check("config/privilege 是合法 JSON", False, str(e))
    if isinstance(priv, dict):
        run_as = (priv.get("defaults") or {}).get("run-as")
        check("privilege 声明了 defaults.run-as",
              run_as in ("package", "root"), run_as)

    res = None
    rpath = os.path.join(PKG, "config", "resource")
    if os.path.isfile(rpath):
        try:
            res = json.loads(read(rpath))
            check("config/resource 是合法 JSON", True)
        except json.JSONDecodeError as e:
            check("config/resource 是合法 JSON", False, str(e))

    projects = []
    if isinstance(res, dict):
        projects = ((res.get("docker-project") or {}).get("projects") or [])
    check("resource 声明了 docker-project.projects", bool(projects))
    for proj in projects:
        name, path = proj.get("name"), proj.get("path")
        check(f"docker-project『{name}』的 path={path} 存在",
              bool(path) and os.path.isdir(os.path.join(PKG, "app", path)))

    ucfg_path = os.path.join(PKG, "app", uidir, "config")
    ucfg = None
    if os.path.isfile(ucfg_path):
        try:
            ucfg = json.loads(read(ucfg_path))
            check("app/ui/config 是合法 JSON", True)
        except json.JSONDecodeError as e:
            check("app/ui/config 是合法 JSON", False, str(e))
    else:
        check("app/ui/config 存在", False, ucfg_path)

    entries = (ucfg or {}).get(".url", {})
    launch = manifest.get("desktop_applaunchname", "")
    if launch:
        check(f"桌面入口『{launch}』已在 app/ui/config 注册",
              launch in entries, f"已注册：{list(entries)}")
    for key, val in entries.items():
        check(f"入口『{key}』含 type/port/url",
              all(k in val for k in ("type", "port", "url")), val)

    for wiz in ("install", "config", "upgrade", "uninstall"):
        wp = os.path.join(PKG, "wizard", wiz)
        if not os.path.isfile(wp):
            warn(f"wizard/{wiz} 不存在（可选）")
            continue
        try:
            data = json.loads(read(wp))
            check(f"wizard/{wiz} 是合法 JSON 数组",
                  isinstance(data, list), type(data).__name__)
            for step in data if isinstance(data, list) else []:
                for item in step.get("items", []):
                    fname = item.get("field", "")
                    check(f"wizard/{wiz} 字段『{fname}』使用 wizard_ 前缀",
                          fname.startswith("wizard_"), fname)
        except json.JSONDecodeError as e:
            check(f"wizard/{wiz} 是合法 JSON", False, str(e))

    # ---------------------------------------------------------- 图标
    print("图标：必须是 PNG 且尺寸精确")
    for fname, size in (("ICON.PNG", 64), ("ICON_256.PNG", 256)):
        p = os.path.join(PKG, fname)
        if os.path.isfile(p):
            dim = png_size(p)
            check(f"{fname} 是 {size}x{size} PNG", dim == (size, size), dim)
        else:
            check(f"{fname} 存在", False, p)

    # ---------------------------------------------------------- 生命周期脚本
    print("cmd/：生命周期脚本齐全且可执行")
    for name in LIFECYCLE:
        p = os.path.join(PKG, "cmd", name)
        check(f"cmd/{name} 存在", os.path.isfile(p), p)
    check("cmd/common 存在（公共函数库）",
          os.path.isfile(os.path.join(PKG, "cmd", "common")))
    for name in LIFECYCLE:
        p = os.path.join(PKG, "cmd", name)
        if not os.path.isfile(p):
            continue
        text = read(p)
        if name == "main":
            check("cmd/main 处理 start/stop/status",
                  all(k in text for k in ("start)", "stop)", "status)")))
            check("cmd/main status 未运行返回 3", "exit 3" in text)
        else:
            check(f"cmd/{name} 引用公共库 common", "common" in text)

    # 安装/升级脚本必须调用 wc_pull_image（测速择优拉取/复用本地镜像）
    for name in ("install_callback", "upgrade_callback"):
        p = os.path.join(PKG, "cmd", name)
        if os.path.isfile(p):
            check(f"cmd/{name} 调用 wc_pull_image（测速拉取镜像）",
                  "wc_pull_image" in read(p))

    # ---------------------------------------------------------- compose
    print("compose：镜像 tag 与版本一致、本地优先缺失才拉取")
    cpath = os.path.join(PKG, "app", "docker", "docker-compose.yaml")
    check("docker-compose.yaml 存在", os.path.isfile(cpath), cpath)
    compose = read(cpath) if os.path.isfile(cpath) else ""
    if compose:
        m = re.search(r"^\s*image:\s*(\S+)", compose, re.MULTILINE)
        image_ref = m.group(1) if m else ""
        check(f"compose 镜像 tag == manifest.version（{image_ref}）",
              image_ref.endswith(":" + ver) and ver != "", image_ref)
        check("compose 声明 pull_policy: missing（本地优先，缺失才拉取）",
              "pull_policy" in compose and "missing" in compose)
        check("compose 使用 privileged（Xorg 需要 DRM master）",
              "privileged: true" in compose)
        check("compose 持久化到 ${TRIM_PKGVAR}",
              "TRIM_PKGVAR" in compose)
        for var in ("wizard_target_output", "wizard_poll_interval", "wizard_timezone"):
            check(f"compose 引用的 {var} 在 wizard 中有定义",
                  any(var in read(os.path.join(PKG, "wizard", w))
                      for w in ("install", "config")
                      if os.path.isfile(os.path.join(PKG, "wizard", w))),
                  var)

    # ---------------------------------------------------------- 镜像 tar
    print("镜像归档：默认联网安装包不含镜像，离线包（--offline）才附带")
    img_dir = os.path.join(PKG, "app", "docker", "image")
    tars = ([f for f in os.listdir(img_dir) if f.endswith(IMAGE_SUFFIXES)]
            if os.path.isdir(img_dir) else [])
    if tars:
        for t in tars:
            size_mb = os.path.getsize(os.path.join(img_dir, t)) / 1024 / 1024
            check(f"镜像归档 {t} 非空（{size_mb:.0f} MB，离线模式）", size_mb > 1)
            check(f"镜像归档文件名含版本号 {ver}", ver in t, t)
    elif args.require_image:
        check("离线包内附带镜像归档", False,
              f"{os.path.relpath(img_dir, ROOT)}/ 下没有 .tar/.tar.gz/.tgz，"
              "请先执行 bash scripts/build-package.sh --offline")
    else:
        check("未附带镜像归档（联网安装模式，安装时测速拉取）", True)

    for path in args.image_tar:
        validate_image_archive(os.path.join(ROOT, path), ver)

    for fpk in args.fpk:
        validate_fpk(fpk)

    return finish()


def finish():
    print()
    if _warned:
        print(f"WARN: {len(_warned)} 项 -> {', '.join(_warned)}")
    if _failed:
        print(f"FAILED: {len(_failed)} 项（通过 {_passed} 项）")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print(f"打包校验通过（{_passed} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
