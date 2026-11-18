# FAQ / 排错

每条按「现象 → 原因 → 处理」组织。示例里的路径、镜像地址都请按自己的环境替换。

## 安装和脚本

### 脚本报 `Permission denied`

**原因**：从 zip 解压或手工复制时丢了可执行位。

**处理**：

```bash
chmod +x scripts/dind/*.sh
chmod +x scripts/show-trace.py
```

不想改权限时，也可以临时用 `bash scripts/dind/<script>.sh`。

### `git: command not found`

**原因**：机器没有安装 git。git 是环境准备的一部分：clone/更新
`harborstack`、`harbor`，以及使用 registry 数据集都需要它。

**处理**：按 [runbook-toolchain.md](runbook-toolchain.md) §5 做用户级安装，
或让管理员安装 git。

### `~/code/harbor` 被解析成 `$HOME/~/code/harbor`

**原因**：旧脚本里 `${VAR#~/}` 的 `~` 被 bash 再次展开。

**处理**：更新仓库脚本；或显式指定路径：

```bash
export HARBORSTACK_PATHS__HARBOR_REPO=/绝对路径/harbor
```

### `uv sync` 很慢或超时

**原因**：默认 PyPI 网络不可达或很慢，常见于 agent 安装和 verifier 的
`uv sync` / `uvx`。

**处理**：agent 安装可以用 `--ae` 指定可达的 PyPI 镜像；verifier 若也慢，
在任务 `task.toml` 的 `[environment.env]` 里设置 `UV_DEFAULT_INDEX`，
或使用预热缓存/镜像。

```bash
uv run harborstack run -- run \
  -p /path/to/tasks/TASK_NAME \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker -n 1 -y \
  --ae UV_DEFAULT_INDEX=https://pypi.example.com/simple  # 换成你的可达镜像
```

不要把一个固定镜像写进通用配置；按你的网络环境选择。

### 中断同步后 `uv.lock` 被改写

**原因**：镜像解析可能重写锁文件里的下载地址。

**处理**：当前脚本会在退出/中断时恢复 `uv.lock`；旧版本请从备份恢复，
或重新检出该文件。

## Langfuse 和 dind

### `doctor` 的 `ports` 项短暂 `warn`

**原因**：刚起栈时 web 还在预热，`doctor` 的 2 秒健康探测可能超时。

**处理**：确认随后 web 是 200；`verify.sh` 会先等 web 健康。稳定后重跑
`doctor` 即可。

### `show-trace.py` 一开始 `count 0`

**原因**：Langfuse 的读路径有最终一致性延迟，`display` 返回不代表
observations 立即可查。

**处理**：

```bash
.venv/bin/python scripts/show-trace.py --wait 60
```

如果长时间仍为 0，再检查 Langfuse worker/ClickHouse 日志。

### 导入刚结束，score 数量比预期少

**现象**：`display` 返回后立刻查 ClickHouse `scores` 表或页面，分数条数偏少（例如
预期 84 条，首次只看到 72 条），过十几秒再查才补齐。

**原因**：score 走 Langfuse 的异步写入链路，HTTP 2xx 只代表已接收；写入
`events_full`/`scores` 与可查询之间存在最终一致性延迟。

**处理**：等待 10–30 秒后重查，不要据此判定导入失败或重复执行 `display`。
`display` 默认会在结束前对最后一条成功导入的 trace 做有界等待（约 60s），可用
`--wait N` / `--no-wait` 调整；超时只给 `[warn]`，不影响退出码。

### `dind_ip not found`

**原因**：watcher 继承了 `DOCKER_HOST`，跑去 dind 内部找 dind 容器。

**处理**：脚本会自动 `unset DOCKER_HOST`；手工执行 watcher 时不要漏掉。

### 端口转发/forwarder 异常

```bash
scripts/dind/status.sh
tail -n 50 var/dind/logs/init.log
tail -n 50 var/dind/logs/forwarders.log
```

### `display` 退出码 4

**原因**：Langfuse 栈不健康。

**处理**：先跑 `harborstack doctor`，确认 web 200；必要时重新 `init`。

### `doctor` 显示 `images 0/6`

**原因**：检查镜像时使用的 mirror 前缀，和加载进 dind 的镜像前缀不一致。

**处理**：让 `HARBORSTACK_IMAGE__MIRROR`、`harborstack.toml` 的
`[image].mirror`、以及 dind 脚本使用同一个前缀。dind 脚本现在会优先读取
`harborstack.toml`，但环境变量仍然优先级最高。

### 页面看不到 trace

Langfuse v4 的 **Tracing** 列表在 events 模式下可能为空。去
**Observations** 页，把 `Is Root Observation` 设为 `true`。

### `start-dind.sh` 报 privileged / permission

**原因**：当前用户不在 `docker` 组，或 daemon 禁止 privileged 容器。

**处理**：让管理员确认权限；不要用 sudo 改系统配置。

### dind 容器启动后立刻退出 / OOM

**原因**：宿主机内存不足，或 `DIND_MEMORY` 太小。compose 的
`mem_limit` 总和约 10.5G，建议 `DIND_MEMORY>=12g`。

**处理**：看 `docker logs harborstack-dind  # 自定义名字时替换`；调整 `DIND_MEMORY`。

### `load-images.sh` 报宿主机缺镜像

**原因**：宿主机没有先执行 `harborstack install`，或者 host install 与
`load-images.sh` 使用了不同的镜像源。

**处理**：先在宿主机跑 `.venv/bin/harborstack install`，并确认两边的
`HARBORSTACK_IMAGE__MIRROR` 一致。

### `init` 在补拉 manifest 时网络失败

**原因**：dind 容器出网受限。镜像层已在本地，但 `RepoDigests` 需要向镜像源
补拉。

**处理**：让 dind 能访问镜像源，或改用本地 registry 方案。

### `web` 健康检查超时

**处理**：

```bash
scripts/dind/status.sh
tail -n 50 var/dind/logs/forwarders.log
ss -tln | grep 18930
```

确认宿主机 loopback 端口存在，且没有旧 forwarder 占着端口。

### 改了 `DIND_MEMORY` / `DIND_CPUS` 没生效

**原因**：`start-dind.sh` 复用已有容器，不会更新资源限制。

**处理**：

```bash
scripts/dind/stop-dind.sh
docker rm harborstack-dind  # 自定义名字时替换
scripts/dind/start-dind.sh
```

数据卷会保留。

### 宿主机重启后页面打不开

dind 容器是 `restart: no`。重新执行：

```bash
scripts/dind/start-dind.sh
scripts/dind/run-init.sh
```

### `doctor` 提示 18930 已被健康 Langfuse 占用

这是宿主机 forwarder，正常；`init` 只有在重跑且未停旧转发时才可能改选端口，
`run-init.sh` 会先停旧转发。

### `display` 之后出现两份 `report.json`

**现象**：run 目录下和 `var/report.json` 各有一份，内容相同。

**原因**：`display` 同时写「随产物归档的那份」和配置项 `report.path`（默认 `./var/report.json`）指向的「最新一份」。

**处理**：两份都是预期产物。判断某次导入请以 run 目录下的那份为准；`var/report.json` 只表示最近一次导入。

### 完全重置 Langfuse 时的顺序

**现象**：只删镜像不删卷，`init` 报 secrets 与卷内凭证不匹配而拒绝启动。

**原因**：`init` 的一致性门禁按设计生效，卷里保留着旧环境的密钥。

**处理**：按顺序执行，缺一不可：

```bash
harborstack down            # 停栈并删本项目的容器与卷
docker rmi <六个镜像>        # 镜像按 lock 里的 ref 删除
rm -rf ~/.cache/harborstack/bins/* var/*
```

不要使用 `docker system prune` 或批量删除，共享机器上会误伤他人容器。

## 评测

### 评测跑进了 dind

**原因**：`DOCKER_HOST` 被设置为 dind，评测会去 dind 里找任务镜像。

**处理**：

```bash
unset DOCKER_HOST
```

dind 只负责 Langfuse；评测默认用宿主机 Docker。见
[runbook-eval.md](runbook-eval.md)。

### `tests/*.sh` 是 CRLF

**原因**：Windows 解压/检出的数据集，verifier 直接在容器里执行脚本会报
shell 语法错误。

**处理**：在数据集副本上转换：

```bash
find /path/to/tasks -path '*/tests/*.sh' -type f -exec sed -i 's/\r$//' {} +
bash -n /path/to/tasks/TASK_NAME/tests/test.sh
```

不要直接改原始数据，除非你确定要保留转换后的版本。

### 任务镜像不存在

**原因**：镜像加载到了另一个 Docker daemon，或任务镜像尚未准备。

**处理**：

```bash
unset DOCKER_HOST
TASK_IMAGE=example/image:tag  # 从 task.toml 的 docker_image 取
docker image inspect "$TASK_IMAGE"
```

不存在就在当前评测 daemon 里加载/构建；不要在评测时依赖不可达的 registry。

### Docker Hub 直连失败

**原因**：网络限制。

**处理**：提前把镜像加载到评测 daemon，或使用可达的内部镜像站。不要假设
评测运行时会自动拉取成功。

### `run` 报缺少 harbor 参数，或 `No such option '-p'`

**原因**：`harborstack run` 的 harbor 参数必须放在 `--` 之后。

**处理**：

```bash
uv run harborstack run -- run -p /path/to/tasks ...
```

### `harbor 工作区不存在` / 没有可用的 harbor 环境

**原因**：`paths.harbor_repo` 指向不对，或该仓没有 `.venv/bin/harbor`。

**处理**：

```bash
export HARBORSTACK_PATHS__HARBOR_REPO=/绝对路径/harbor
cd /绝对路径/harbor && uv sync
```

### `DEEPSEEK_API_KEY` 缺失或模型不可用

**原因**：没有设置 provider 凭据，或当前账号没有该模型。

**处理**：

```bash
export DEEPSEEK_API_KEY=...
```

确认 `-m` 的模型名在账号里可用。不要把 key 提交到仓库或写进文档。

### API key 出现在进程命令行

**原因**：Harbor 通过 `docker compose exec -e API_KEY=...` 传递模型凭据，
同一台机器上的用户可能在 `ps` 中看到。

**处理**：共享机器上跑完后轮换该 key；不要长期复用。

### agent 说完成了，但 reward 是 0

agent 的最终回答不是判分依据。看：

```text
verifier/reward.txt
verifier/reward.json
verifier/test-stdout.txt
verifier/ctrf.json
```

### `display` 找不到 run 目录

- 用包装器：默认从 `var/runs` 取最新；也可以显式传 `var/runs/<id>`。
- 直接 `harbor run`：把 `-o` 指定的 job 目录传给 `display`。

## 离线 bundle

### 解包后目录名和文档里的不一致

**原因**：tar 记录的是打包时的顶层目录名；如果压缩包名改过而目录没改，解包仍用旧名。

**处理**：`mv` 成期望的名字即可；或按
[runbook-bundle.md](runbook-bundle.md) 重打，让包名、目录名一致。

### `bundle verify` 报 `missing` / `mismatch`

**原因**：下载不完整、归档时漏文件，或包内文件被改动。

**处理**：重新下载并核对 `SHA256SUMS`；仍不一致就重新打包，不要带着损坏的包部署。

### `install --from-bundle` 后 `init` 报补丁字段 KeyError

**原因**：缓存里只有 `patch/*.bin`，缺少同名 `.json` sidecar，`init` 读不到站点统计。

**处理**：用当前版本重新 `install --from-bundle`（会自动补写 sidecar），或重新生成 bundle。
旧包不要手工拼 sidecar，直接重打更可靠。

### 换架构或换 Langfuse 版本后还能用旧包吗

不能。`images.tar` 与架构、版本绑定；按
[runbook-bundle.md](runbook-bundle.md) 的目录规范分别归档，不要混放。

## 范围和限制

### DeepSWE / sidecar / 独立 verifier

本指南的验证范围是常规 Harbor 评测和 Langfuse 部署。DeepSWE 的
`agent.network_mode=no-network`、egress sidecar、独立 verifier 镜像和
buildx 属于高级场景，不在傻瓜指南范围内。

### ARM ClickHouse patch

x86_64 不会触发 ClickHouse patch；aarch64 无 RCpc 的真实机器需要单独验证。
