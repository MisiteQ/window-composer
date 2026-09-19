#!/usr/bin/env python3
"""在没有官方 fnpack 的机器上，产出与 fnpack 等价的 `.fpk`。

用法：
    python3 scripts/make-fpk.py packaging/fnos dist/window-composer-1.0.0.fpk

--------------------------------------------------------------------------
.fpk 的真实格式（由 fnpack 1.2.3 产物逐字节逆向确认）
--------------------------------------------------------------------------
`window-composer.fpk` 是 **gzip(tar)**，不是 zip：

    window-composer.fpk        (gzip, mtime=0, OS=255)
    └── tar (ustar, dir=0777 / file=0666, uid=gid=0)
        ├── app.tgz                    ← app/ 目录**整体再打一层** gzip(tar)
        ├── cmd/                       ← 目录项，mode 0777
        ├── cmd/main 等 9 个生命周期脚本 + cmd/common
        ├── config/
        ├── config/privilege
        ├── config/resource
        ├── ICON.PNG
        ├── ICON_256.PNG
        ├── manifest                   ← 被重写：`key<pad>= value`，并追加 checksum
        ├── wizard/
        └── wizard/{install,config,upgrade,uninstall}

    app.tgz 内部（不带 app/ 前缀，而是 app/ 的内容；config/ 会被复制一份进来）

        docker/  docker/docker-compose.yaml  docker/image/
        ui/      ui/config  ui/images/icon_64.png  ui/images/icon_256.png
        config/  config/privilege  config/resource

两个容易致命的点：

1. **必须是 gzip/tar.gz**。飞牛的解析器按 gzip 读包，拿到 zip（魔术字节 `PK\\x03\\x04`）
   会直接报「不是有效的程序文件」。
2. **`app/` 不能以目录形式出现**，必须打成 `app.tgz`；否则飞牛找不到应用文件。

另外 manifest 里会被追加 `checksum = <md5(app.tgz)>`（32 位小写十六进制），
飞牛用它校验应用文件完整性——所以 app.tgz 必须在写 manifest 之前先生成。
"""

import hashlib
import io
import os
import sys
import tarfile
import time
import zlib

# manifest 重写后的键对齐宽度（fnpack 实测：键名 + 空格 = 27 列，再接 "= "）
KEY_WIDTH = 27
# fnpack 实测：所有目录写 0777，所有文件写 0666（执行位由飞牛安装时补）
MODE_DIR = 0o777
MODE_FILE = 0o666

# 全部条目的 mtime 统一取打包时刻（fnpack 也是这样，便于复现）
_MTIME = int(time.time())


def _sort_key(name):
    """fnpack 的遍历顺序是大小写不敏感的字典序（`ICON.PNG` 排在 `app.tgz` 之后）。"""
    return (name.lower(), name)


def _tarinfo(arc, is_dir, size=0):
    ti = tarfile.TarInfo(arc)
    ti.mode = MODE_DIR if is_dir else MODE_FILE
    ti.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    ti.size = 0 if is_dir else size
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = _MTIME
    return ti


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _add_dir(tar, arc):
    tar.addfile(_tarinfo(arc, is_dir=True))


def _add_file(tar, full, arc):
    data = _read(full)
    tar.addfile(_tarinfo(arc, is_dir=False, size=len(data)), io.BytesIO(data))


def _add_bytes(tar, arc, data):
    tar.addfile(_tarinfo(arc, is_dir=False, size=len(data)), io.BytesIO(data))


def _add_tree(tar, base, prefix):
    """把 base 下的内容加入 tar（不含 base 自身），条目名前缀 prefix。"""
    for entry in sorted(os.listdir(base), key=_sort_key):
        full = os.path.join(base, entry)
        arc = prefix + entry
        if os.path.isdir(full):
            _add_dir(tar, arc)
            _add_tree(tar, full, arc + "/")
        else:
            _add_file(tar, full, arc)


def gzip_bytes(data, level=6):
    """确定性 gzip：mtime=0、无文件名、XFL=0、OS=255（与 Go archive/gzip 一致）。"""
    comp = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = comp.compress(data) + comp.flush()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return (
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
        + body
        + crc.to_bytes(4, "little")
        + (len(data) & 0xFFFFFFFF).to_bytes(4, "little")
    )


def build_app_tgz(pkg):
    """app/ 的内容 + config/{privilege,resource} → gzip(tar)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        _add_tree(tar, os.path.join(pkg, "app"), "")
        cfg = os.path.join(pkg, "config")
        if os.path.isdir(cfg):
            _add_dir(tar, "config")
            _add_tree(tar, cfg, "config/")
    return gzip_bytes(buf.getvalue())


def rewrite_manifest(text, checksum):
    """按 fnpack 的规则重写 manifest：去空行、键对齐、追加 checksum。"""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue  # fnpack 会把空行去掉
        if stripped.startswith((";", "#", "[")):
            out.append(line)  # 注释原样保留
            continue
        if "=" not in line:
            out.append(line)
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "checksum":
            continue  # 由本脚本重新计算
        out.append(f"{key:<{KEY_WIDTH}}= {value}")
    out.append(f"{'checksum':<{KEY_WIDTH}}= {checksum}")
    return ("\n".join(out) + "\n").encode("utf-8")


def build(pkg, out):
    if not os.path.isfile(os.path.join(pkg, "manifest")):
        raise SystemExit(f"【ERROR】{pkg}/manifest 不存在，打包目录不对？")

    # 1) 先出 app.tgz —— manifest 的 checksum 依赖它的字节，顺序不能颠倒
    app_tgz = build_app_tgz(pkg)
    checksum = hashlib.md5(app_tgz).hexdigest()

    # 2) 重写 manifest
    manifest = rewrite_manifest(_read(os.path.join(pkg, "manifest")).decode("utf-8"), checksum)

    # 3) 组装外层 tar（app/ 已并入 app.tgz，跳过）
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        _add_bytes(tar, "app.tgz", app_tgz)
        for entry in sorted(os.listdir(pkg), key=_sort_key):
            if entry == "app":
                continue
            full = os.path.join(pkg, entry)
            if os.path.isdir(full):
                _add_dir(tar, entry)
                _add_tree(tar, full, entry + "/")
            elif entry == "manifest":
                _add_bytes(tar, "manifest", manifest)
            else:
                _add_file(tar, full, entry)

    blob = gzip_bytes(buf.getvalue())
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(blob)
    return checksum


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        print("用法：make-fpk.py <打包目录> <输出 .fpk>", file=sys.stderr)
        return 2
    pkg, out = sys.argv[1], sys.argv[2]
    checksum = build(pkg, out)
    print(f"    app.tgz checksum = {checksum}")
    print(f"    已写入 {out}（gzip/tar，{os.path.getsize(out)} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
