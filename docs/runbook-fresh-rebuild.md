# Harborstack 手工部署手册

从零部署并验证整条链路。除标注「可选」的第 8 节外，按顺序复制执行即可。

所有命令都在 harborstack 仓库根目录执行：

```bash
cd /path/to/harbor-otel-stack                 # 换成你的实际路径
export HARBORSTACK_PATHS__HARBOR_REPO=~/code/harbor   # run 使用的 harbor 仓
```

> 第二行只对当前终端生效，后续命令请在同一终端执行。

---

## 1. 检查机器

```bash
docker --version && docker compose version && uv --version
```

预期：三条命令都能打印版本号。

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
uv sync
```

如果访问 PyPI 慢或超时，把最后一条换成：

```bash
UV_DEFAULT_INDEX=https://repo.huaweicloud.com/repository/pypi/simple uv sync
```

预期：`ls ~/code/harbor/.venv/bin/harbor` 存在。

> 用镜像源安装会把 harbor 的 `uv.lock` 改写成镜像地址（依赖版本不变）。想保持工作区干净，追加：
> `git checkout -- uv.lock`

---

## 4. 装 harborstack 环境

```bash
cd /path/to/harbor-otel-stack
uv sync
```

如果访问 PyPI 慢或超时，把最后一条换成：

```bash
UV_DEFAULT_INDEX=https://repo.huaweicloud.com/repository/pypi/simple uv sync
```

预期：生成 `.venv/`，且 `ls .venv/bin/harborstack` 存在。

---

## 5. 体检

```bash
.venv/bin/harborstack doctor
```

预期：`fail` 必须为 0。全新环境出现 `warn`（镜像未拉取、补丁未缓存、尚未 init）属正常。

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

出现 `FAIL` 就停在这里，按第 10 节处理。初始密码见 `harborstack.toml` 的 `langfuse.admin.password`。

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

## 8. 跑一次真实评测（可选，耗 API 余额）

没有 API Key 或任务目录就跳过本节，第 7 节已经验证完整链路。

```bash
export DEEPSEEK_API_KEY=<你的密钥>

.venv/bin/harborstack run -- run \
  -p /path/to/tasks \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker -n 1 -y \
  --job-name my-run-01 \
  --ae UV_DEFAULT_INDEX=https://repo.huaweicloud.com/repository/pypi/simple
```

`-p` 换成你自己的 task 目录。参数必须写在 `--` 之后。

预期：结束时打印 `run 目录 <绝对路径>/var/runs/<时间戳>-<随机>`。然后导入最新一次 run：

```bash
.venv/bin/harborstack display
```

预期：`状态 ok`。重复导入会检测到 trace 已存在并显示 `[skip]`，不会新增 observation。

---

## 9. 看页面

打开 `http://localhost:18930`：

- **Experiments**：每次 `run` 一个条目，不同 run 自动分组。
- **Observations**：把 `Is Root Observation` 设为 `true`，按 trace 查看每一步。Langfuse v4 的 `Tracing` 列表在 events 模式下可能为空，看不到 trace 时到这里看。

---

## 10. 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `git clone` 卡在连接 `github.com` | 网络无法直连 GitHub | 换成内网镜像地址；或用已有副本 `git clone <本地路径> harbor` |
| `git checkout` 报 `pathspec ... did not match` | 本地副本只有远程跟踪分支 | `git fetch <本地路径> refs/remotes/origin/<分支名>:refs/heads/<分支名>` |
| `uv sync` 报 `timed out` | 默认 PyPI 不可达 | 使用第 3、4 节的镜像源命令 |
| `init` 卡在某个镜像 `Pulling fs layer` | 镜像代理不稳定 | 用第 6 节的 daocloud 命令重跑 |
| `display` 显示 `[skip]` | trace 已存在 | 正常幂等行为；要重新导入请换 run 目录或清空 Langfuse 数据 |
| `init` 提示「已存在 N 个数据卷，但 secrets.json 缺失」 | 删了 `var/` 但卷还在 | `harborstack down` 删卷后重新 `init` |
| `init` 卡在拉镜像 | 网络抖动 | 重新执行 `init`，已就绪的会 `skip` |
| `run` 报缺少 harbor 参数 | 没写 `--` | harbor 参数必须写在 `--` 之后 |
| `run` 报 `No such option '-p'` | harbor 是新版子命令 CLI | `--` 之后以 `run` 开头：`run -- run -p ...` |
| `run` 报 `harbor 工作区不存在` | `paths.harbor_repo` 指向的目录不对 | 改 `harborstack.toml`，或加 `--set paths.harbor_repo=/path/to/harbor` |
| `run` 报没有可用的 harbor 环境 | 该仓没装依赖 | `cd ~/code/harbor && uv sync` |
| `display` 退出码 4 | 栈不健康 | 先 `doctor`，再重新 `init` |
| `display` 报 jobs 目录不存在 | 移动过 run 目录 | 把 run 还原到 `run.json` 里记录的原始路径 |
| 端口被占 | 别的服务抢了区间 | 重新 `init`，会自动换端口 |
| 页面看不到 trace | Langfuse v4 列表页不展示 events 数据 | 去 **Observations** 页，把 `Is Root Observation` 设为 `true` |

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
