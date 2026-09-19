# vendor/ —— 离线构建依赖目录

这个目录只服务于**构建机**，不影响 NAS 端的安装体验：
无论这里有没有内容，最终镜像都把依赖预装好了，安装到飞牛 NAS 后
不需要再下载任何软件。

## 用途

- `vendor/` 为空（只有本文件和 `.gitkeep`）：
  `Dockerfile` 会在构建时从 PyPI 镜像（清华源）安装 `requirements.txt`。
  构建机需要能上网。

- `vendor/wheels/` 里有 wheel 文件：
  `Dockerfile` 自动改用 `--no-index --find-links` 离线安装，
  **构建机完全断网也能出镜像**（适合在内网/隔离环境里做发行包）。

## 如何生成 wheel

在任意一台能上网的 Linux 机器上（Python 版本要与镜像一致，即 bookworm 的 3.11）：

```bash
bash scripts/fetch-wheels.sh
```

脚本会把 wheel 下载到 `vendor/wheels/`。之后 `bash scripts/build-package.sh`
产出的安装包就是完全离线自包含的。

> 提示：wheel 会被 COPY 进镜像的一次构建层（安装完即删除运行时副本），
> 所以 `vendor/wheels/` 越大，镜像体积也会相应增加。默认不需要它。
