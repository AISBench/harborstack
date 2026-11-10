# Harborstack 傻瓜启动

本指南只做两件事：**起 Langfuse 可观测栈**，然后**准备 harbor 工作区**。
评测本身是独立步骤，见 [runbook-eval.md](runbook-eval.md)。

## 0. 前置条件

- Linux 机器，已有 Docker Engine，当前用户在 `docker` 组。
- `docker compose` ≥ 2.24.4、`uv`、Python ≥ 3.12、`git`。
  - git 用于 clone/更新 `harbor-otel-stack` 和 `harbor`，以及 registry 数据集。
  - 缺工具看 [runbook-toolchain.md](runbook-toolchain.md)。
- 磁盘建议 ≥ 30G。
- Harbor 工作区默认 `~/code/harbor`；不在这个路径时，二选一：

  ```toml
  # 方式 A：改 harborstack.toml
  [paths]
  harbor_repo = "/你的实际路径/harbor"
  ```

  ```bash
  # 方式 B：只对当前 shell 生效
  export HARBORSTACK_PATHS__HARBOR_REPO=/你的实际路径/harbor
  ```

  起 Langfuse 栈本身不依赖 harbor；后面的评测和 `setup-harbor.sh` 会用到。

## 1. 选择 Langfuse 部署方式

先进入仓库根目录：

```bash
cd /path/to/harbor-otel-stack
```

| 场景 | 建议 |
| --- | --- |
| 共享机器、没有 root、不能改宿主机防火墙；或宿主机上容器 A 访问容器 B 不通 | **方式 A：dind** |
| 普通宿主机 Docker，容器间通信正常 | **方式 B：宿主机 Docker** |

不确定时先跑网络预检：

```bash
scripts/dind/check-host-network.sh || true
```

- 输出 `RESULT: host bridge OK`：方式 B 可用。
- 输出 `RESULT: host bridge BLOCKED`：用方式 A。

## 2. 方式 A：dind 起 Langfuse

一条命令（会依次准备 venv、启动 dind、导入镜像、起栈、验证）：

```bash
scripts/dind/up.sh
```

成功标志：

- 最后打印 `Langfuse http://localhost:18930`
- 里面 `verify.sh` 的 `doctor` 是 `fail=0 warn=0`
- fixture 能导入，`show-trace.py` 看到 101 个 observation

后续需要重新验证时：

```bash
scripts/dind/verify.sh
```

原理、分步命令、转发和排错见
[runbook-langfuse-dind.md](runbook-langfuse-dind.md)。

## 3. 方式 B：宿主机 Docker 起 Langfuse

```bash
uv sync
uv run harborstack doctor
uv run harborstack init
uv run harborstack display fixtures/atif-fidelity
```

成功标志：

- `doctor` 的 `fail=0`；全新环境有镜像/manifest 的 `warn` 是正常的
- `init` 最后打印 `Langfuse http://localhost:18930`
- `display` 状态 `ok`，并打印一条 trace 链接

详细步骤见 [runbook-langfuse-host.md](runbook-langfuse-host.md)。

## 4. 准备 harbor 工作区（两种方式共用）

```bash
scripts/dind/setup-harbor.sh
```

脚本名带 `dind`，但它**只准备 harbor 的 venv**，不启动 dind，也不跑评测；
方式 A 和方式 B 都可以用。

成功后应打印：

```text
==> harbor repo: /你的实际路径/harbor
==> branch: <当前分支>
==> harbor version: <版本>
```

## 5. 跑一个评测（可选，耗 API 余额）

评测默认使用**宿主机 Docker**，和 Langfuse 用 dind 还是宿主机 Docker 无关。
不要设置 `DOCKER_HOST=tcp://...` 指向 dind。

最短命令：

```bash
unset DOCKER_HOST
cd /path/to/harbor-otel-stack
export DEEPSEEK_API_KEY=...

uv run harborstack run -- run \
  -p /path/to/tasks \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker -n 1 -y \
  --job-name my-run-01

uv run harborstack display var/runs/RUN_ID  # 换成实际目录名
```

- `-p` 是本地 task 或 task 集合目录。
- `-d` 是 registry 数据集，需要 git。
- 先 `-n 1` 跑一个任务；稳定后再扩量。
- 完整前置检查、直接 `harbor run` 的备选命令和结果解释见
  [runbook-eval.md](runbook-eval.md)。

## 6. 停止和清理

方式 A：

```bash
scripts/dind/stop-dind.sh          # 停止但保留数据
scripts/dind/stop-dind.sh --purge  # 彻底删除，Langfuse 数据会丢
```

方式 B：

```bash
uv run harborstack down            # 注意：默认会删除数据卷
```

## 7. 遇到问题

先看 [faq.md](faq.md)。常见问题包括：

- 脚本 `Permission denied`
- `uv sync` / `uvx` 网络超时
- `doctor` 的 `ports` 临时 `warn`
- `show-trace` 一开始 `count 0`
- 评测误用了 dind
- 数据集 `tests/*.sh` 是 CRLF
- `display` 找不到 run 目录
