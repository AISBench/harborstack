# 无 root / 共享机器的工具链准备

适用于不能 `sudo`、不能改 `/etc`、`/usr`、`/opt` 的共享 Linux 机器。
所有组件都装在 `$HOME` 下,不影响其他用户。

> 如果你的机器上已经有满足版本要求的 `docker`、`docker compose`、`uv`、Python ≥3.12,
> 可以跳过对应步骤。
>
> 本手册只安装**客户端和用户态工具**,不能替代 Docker Engine/daemon。
> 如果机器上完全没有 Docker daemon,需要管理员先安装并启动;
> dind 路径还要求 daemon 允许 `--privileged` 容器。

## 0. 先确认目录

```bash
mkdir -p ~/.local/bin ~/.local/opt ~/.docker/cli-plugins
```

注意:PATH 里可能已经有 `~/.local/bin`,但目录本身不一定存在。

## 1. Docker CLI + Compose 插件

旧版 Docker CLI **不支持 `docker compose` 插件**。
这里从官方 `docker:27-cli` 镜像里提取用户级 CLI 和 Compose 插件。

```bash
mkdir -p ~/.local/opt/docker-cli/bin ~/.local/opt/docker-cli/cli-plugins

# 用系统已有的 docker 拉取 CLI 镜像(只拉镜像,不影响运行中的容器)
docker pull docker.m.daocloud.io/library/docker:27-cli

cid=$(docker create docker.m.daocloud.io/library/docker:27-cli)
docker cp "$cid":/usr/local/bin/docker \
  ~/.local/opt/docker-cli/bin/docker
docker cp "$cid":/usr/local/libexec/docker/cli-plugins/docker-compose \
  ~/.local/opt/docker-cli/cli-plugins/docker-compose
docker rm "$cid"

chmod +x ~/.local/opt/docker-cli/bin/docker \
  ~/.local/opt/docker-cli/cli-plugins/docker-compose

ln -sfn ~/.local/opt/docker-cli/bin/docker ~/.local/bin/docker
ln -sfn ~/.local/opt/docker-cli/cli-plugins/docker-compose \
  ~/.docker/cli-plugins/docker-compose

hash -r
command -v docker          # 应为 ~/.local/bin/docker
docker version             # 客户端 27.x,服务端以宿主机为准
docker compose version     # 应 >= 2.24.4
```

> Compose 版本要求 ≥2.24.4,因为项目 compose 使用了 `!override` 语法。

## 2. uv

```bash
pip download uv --no-deps -d /tmp/uvwheel \
  -i https://repo.huaweicloud.com/repository/pypi/simple

cd /tmp/uvwheel
unzip -o -q uv-*.whl -d uv-wheel
install -m 755 uv-wheel/uv-*.data/scripts/uv ~/.local/bin/uv

hash -r
uv --version
```

如果 PyPI 直连可用,也可以直接:

```bash
pip install --user uv
```

## 3. Python ≥3.12

harbor 要求 Python ≥3.12(项目 `.python-version` 为 3.13)。
这里使用 python-build-standalone,解压到用户目录。

```bash
mkdir -p ~/.local/opt/python-3.13

# x86_64;如果是 aarch64,把 URL 里的 x86_64 换成 aarch64
curl -L -o /tmp/py313.tar.gz \
  'https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz'

tar xzf /tmp/py313.tar.gz -C ~/.local/opt/python-3.13 --strip-components=1
ln -sfn ~/.local/opt/python-3.13/bin/python3.13 ~/.local/bin/python3.13

hash -r
python3.13 --version
python3.13 -c 'import ssl, sqlite3; print("ssl+sqlite ok")'
```

> 镜像目录可能随时间变化;如果上面的 release 不存在,浏览
> <https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/>
> 选择最新的 `cpython-3.13.*-x86_64-unknown-linux-gnu-install_only.tar.gz`。

## 4. 验证

```bash
type -a docker uv python3.13
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
docker compose version
uv --version
python3.13 --version
```

## 5. git（必需）

git 是环境准备的一部分：clone/更新 `harbor-otel-stack`、`harbor`，以及使用
registry 数据集（`-d`）都需要它。

RPM 系发行版上可以做用户级安装，不需要 sudo：

```bash
mkdir -p ~/.cache/git-rpm ~/.local/opt/git

# 下载 git 及其未安装的依赖
dnf download --resolve git --destdir ~/.cache/git-rpm

# 解包到用户目录
for rpm in ~/.cache/git-rpm/*.rpm; do
  rpm2cpio "$rpm" | (cd ~/.local/opt/git && cpio -idm --no-absolute-filenames)
done

ln -sfn ~/.local/opt/git/usr/bin/git ~/.local/bin/git
export GIT_EXEC_PATH="$HOME/.local/opt/git/usr/libexec/git-core"
export GIT_TEMPLATE_DIR="$HOME/.local/opt/git/usr/share/git-core/templates"

git --version
```

把两个 `export` 写进 `~/.bashrc`，新 shell 才能继续使用完整的 git 子命令。
如果 `dnf` 不可用，请让管理员安装 git，或使用机器上已有的 git。

说明：

- `git` RPM 依赖 `git-core`、`perl-Git` 等包，`dnf download --resolve` 会一起下载。
- `uv.lock` 的备份/恢复只是同步安全措施，不能替代 git。

## 6. 软件源速查

| 用途 | 源 |
|---|---|
| Docker 镜像 | `docker.m.daocloud.io`(备用 `docker.1ms.run`) |
| PyPI | `https://repo.huaweicloud.com/repository/pypi/simple` |
| Python standalone | 南京大学 GitHub release 镜像 |
| Docker CLI / Compose | 官方 `docker:27-cli` 镜像内提取 |

> GitHub 和 Docker Hub 直连在受限网络里可能不可用;上面的组合已实测可用。
