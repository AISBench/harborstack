# Harborstack Langfuse 部署手册（宿主机 Docker 路径）

> 本手册直接使用宿主机 Docker，要求宿主机允许 Docker bridge 转发
> （firewalld/iptables 未阻断容器间通信）。
>
> 共享机器 / 无 root / 宿主机防火墙受限时，请使用
> **[dind 路径](runbook-langfuse-dind.md)**。
>
> 可以用 `scripts/dind/check-host-network.sh` 预检宿主机网络：
> 输出 `host bridge OK` 时本路径可用；`host bridge BLOCKED` 时请改用 dind 路径。

本手册只负责部署 Langfuse 并验证 fixture 导入；**评测与 Langfuse 部署解耦**，
见 [runbook-eval.md](runbook-eval.md)。按顺序复制执行即可。

所有命令都在 harborstack 仓库根目录执行：

```bash
cd /path/to/harbor-otel-stack                 # 换成你的实际路径
export HARBORSTACK_PATHS__HARBOR_REPO=~/code/harbor   # run 使用的 harbor 仓
```

> 第二行只对当前终端生效，后续命令请在同一终端执行。

---

## 1. 检查机器

```bash
docker --version && docker compose version && uv --version && python3.13 --version
```

预期：四条命令都能打印版本号，且 Compose ≥2.24.4、Python ≥3.12。

> 如果命令缺失或版本过低，先按 [无 root 工具链准备](runbook-toolchain.md) 安装。

---

## 2. 清理旧环境（首次部署整节跳过）

```bash
# 停栈并删除数据卷
[ -x .venv/bin/harborstack ] && .venv/bin/harborstack down || true

# 删除本项目镜像（不存在会报错，已忽略）
docker rmi -f \
  docker.1ms.run/langfuse/langfuse:4 \
  docker.1ms.run/langfuse/langfuse-worker:4 \
  docker.1ms.run/library/postgres:16 \
  docker.1ms.run/library/redis:7-alpine \
  docker.1ms.run/clickhouse/clickhouse-server:25.12-alpine \
  docker.1ms.run/bitnamilegacy/minio:latest 2>/dev/null || true

# 删除本地状态与缓存
rm -rf var .venv ~/.cache/harborstack
```

预期：`down` 以 `[ok  ] harborstack-default` 开头；`rm` 之后 `ls` 只剩代码目录。

> 共享机器：先确认这些镜像没有别人的容器在用，再执行 `docker rmi`：
> `docker ps -a --format '{{.Names}}\t{{.Image}}' | grep -E 'langfuse|clickhouse|postgres|redis|minio'`
>
> 如果用过 `docker.m.daocloud.io` 兜底，把上面 6 个 tag 里的 `docker.1ms.run` 换成该前缀再删一遍。

---

## 3. 获取 harbor 代码并装依赖

```bash
mkdir -p ~/code && cd ~/code
[ -d harbor ] || git clone https://github.com/ivanbao9783/harbor.git harbor
cd harbor
git checkout feat/jiuwenswarm_adapter
uv sync --python python3.13
```

> 只运行评测（不需要 harbor 开发/云适配器）时，可以用
> `uv sync --no-dev --python python3.13`，
> venv 从约 8.2G 降到约 211M；需要 pytest/云适配器时再用完整 `uv sync`。
>
> 如果 `python3.13` 不在 `PATH`，把它换成实际解释器路径；缺工具链时看
> [runbook-toolchain.md](runbook-toolchain.md)。

如果访问 PyPI 慢或超时，把最后一条换成：

```bash
UV_DEFAULT_INDEX=https://pypi.example.com/simple \
  uv sync --python python3.13  # 换成你的可达镜像
```

预期：`ls ~/code/harbor/.venv/bin/harbor` 存在。

> 用镜像源安装会把 harbor 的 `uv.lock` 改写成镜像地址（依赖版本不变）。想保持工作区干净，追加：
> `git checkout -- uv.lock`

---

## 4. 装 harborstack 环境

```bash
cd /path/to/harbor-otel-stack
uv sync --python python3.13
```

如果访问 PyPI 慢或超时，把最后一条换成：

```bash
UV_DEFAULT_INDEX=https://pypi.example.com/simple \
  uv sync --python python3.13  # 换成你的可达镜像
```

预期：生成 `.venv/`，且 `ls .venv/bin/harborstack` 存在。

---

## 5. 体检

```bash
.venv/bin/harborstack doctor
```

预期：`fail` 必须为 0。全新环境出现 `warn`（镜像未拉取、补丁未缓存、尚未 init）属正常。

> 如果镜像来自离线 bundle，跳过联网拉取，先执行：
>
> ```bash
> unset DOCKER_HOST
> .venv/bin/harborstack install --from-bundle /path/to/harborstack-<arch>-runtime
> ```
>
> 再继续下一节。打包与恢复流程见 [runbook-bundle.md](runbook-bundle.md)。

---

## 6. 起栈

```bash
.venv/bin/harborstack init
```

这一步会拉镜像、给 ClickHouse 打补丁、起六个容器、跑健康检查。预期每个检查行都是 `[ok  ]`，最后输出：

```
Langfuse   http://localhost:18930
登录账号   admin@harborstack.local
端口分配   web=18930，...
```

出现 `FAIL` 就停在这里，按 [faq.md](faq.md) 排查。初始密码见 `harborstack.toml` 的 `langfuse.admin.password`。

> 如果某个镜像长时间停在 `Pulling fs layer`，换备用源重跑：
> `HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io .venv/bin/harborstack init`

---

## 7. 灌数据（不耗 API）

```bash
.venv/bin/harborstack display fixtures/atif-fidelity --dry-run   # 先看计划
.venv/bin/harborstack display fixtures/atif-fidelity             # 真导入
```

第二条命令预期：`trials 1（跳过 0，缺文件 0）`、`状态 ok`，并打印一条 `trace http://...` 链接。

---

## 8. 评测（独立文档）

Langfuse 部署完成后，评测默认使用**宿主机 Docker**，不要设置
`DOCKER_HOST` 指向 dind。完整前置检查、运行和导入命令见
[runbook-eval.md](runbook-eval.md)。

---

## 9. 看页面

打开 `http://localhost:18930`：

- **Experiments**：每次 `run` 一个条目，不同 run 自动分组。
- **Observations**：把 `Is Root Observation` 设为 `true`，按 trace 查看每一步。Langfuse v4 的 `Tracing` 列表在 events 模式下可能为空，看不到 trace 时到这里看。

---

## 10. 常见问题

Langfuse 部署、评测和结果解释的常见问题统一放在 [faq.md](faq.md)。

---

## 附：生成的目录

| 路径 | 内容 |
| --- | --- |
| `var/.env` | compose 变量（0600，勿手改） |
| `var/secrets.json` | 数据库/API Key 密钥（0600，**删了栈就起不来**） |
| `var/ports.json` | 端口分配 |
| `var/env-manifest.json` | 部署状态，`ready` 才能 `display` |
| `var/runs/<id>/` | 每次评测产物与导入报告 |
| `~/.cache/harborstack/` | ClickHouse 补丁二进制缓存 |
