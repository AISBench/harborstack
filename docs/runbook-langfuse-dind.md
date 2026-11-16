# Harborstack Langfuse 部署手册（dind 路径）

> 只想快速跑起来：看 [quickstart.md](quickstart.md)。
> 本文件保留 dind 的完整原理、参数和排错，供需要时查阅。

本手册只负责**把 Langfuse 可观测栈跑在隔离的 Docker-in-Docker 容器里**。
评测与 Langfuse 部署解耦：评测默认在宿主机 Docker 上运行，见
[runbook-eval.md](runbook-eval.md)。

dind 路径适用于：

- 共享机器、没有 root、不能改宿主机 firewalld/iptables；
- 宿主机上 `容器 A -> 容器 B` 不通（`Host is unreachable`），但 `host -> 容器端口` 正常；
- 希望所有端口只绑 `127.0.0.1`、不影响其他用户。

> 如果宿主机 Docker bridge 转发正常，宿主机 Docker 路径更轻量：
> [runbook-langfuse-host.md](runbook-langfuse-host.md)。
> 工具链缺失时先看 [runbook-toolchain.md](runbook-toolchain.md)。

---

## 0. 选择路径

先跑网络预检：

```bash
cd ~/code/harbor-otel-stack
scripts/dind/check-host-network.sh
```

- 输出 `RESULT: host bridge OK`：宿主机 Docker 路径可用，dind 也可以但更重。
- 输出 `RESULT: host bridge BLOCKED`：**使用本手册的 dind 路径**。

共享机器上如果输出 `RESULT: host bridge BLOCKED`，说明应该使用本 dind 路径。

---

## 1. 前置条件

- 按 [runbook-toolchain.md](runbook-toolchain.md) 准备：
  - Docker CLI + Compose（Compose ≥2.24.4）
  - uv
  - Python ≥3.12（建议 3.13）
- 当前用户在 `docker` 组，能够运行 `--privileged` 容器（dind 需要）。
- 目录：
  - harborstack：`~/code/harbor-otel-stack`
  - harbor：`~/code/harbor`
- 磁盘：harbor 运行时 venv 约 211M，dind 内镜像约 4G，加上数据卷，建议预留 ≥30G。
- 需要 git 来 clone/更新 `harbor-otel-stack` 和 `harbor`；安装见 [runbook-toolchain.md](runbook-toolchain.md) §5。

### 1.1 代码位置

默认路径：

- harborstack：`~/code/harbor-otel-stack`（本仓库）
- harbor：`~/code/harbor`

如果 harbor 在别处：

```bash
export HARBORSTACK_PATHS__HARBOR_REPO=/path/to/harbor
```

如果 harborstack 仓库在别处，所有命令都在该仓库根目录执行；`scripts/dind`
会自动按脚本位置定位仓库根目录。用 git clone/pull 维护代码，安装见
[runbook-toolchain.md](runbook-toolchain.md) §5。

### 1.2 镜像源一致性

以下命令必须使用同一个 `HARBORSTACK_IMAGE__MIRROR`：

- 宿主机 `.venv/bin/harborstack install`
- `scripts/dind/load-images.sh`
- `scripts/dind/run-init.sh`
- `harborstack doctor` / `display`

本手册默认：

```bash
export HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io
```

换源时所有命令一起换，否则 `load-images.sh` 会找不到宿主机镜像。

### 1.3 新机器自检

```bash
docker version
docker compose version      # 必须 >=2.24.4
uv --version
python3.13 --version        # 必须 >=3.12
df -h ~                     # 建议 >=30G 可用
```

如果这些命令缺失，先按 [runbook-toolchain.md](runbook-toolchain.md) 准备。

> **安全提示**：dind 容器是 `--privileged`，权限面比普通容器大。
> 它被限制在独立网络命名空间和独立存储卷里，不改宿主机防火墙；
> 但仍建议只在可信机器、且你确认可以运行 privileged 容器时使用。

---

## 2. 准备 harborstack venv

```bash
cd ~/code/harbor-otel-stack
scripts/dind/setup-stack-venv.sh
```

验证：

```bash
ls ~/code/harbor-otel-stack/.venv/bin/harborstack
```

> `up.sh` 只负责 Langfuse 栈，不准备 harbor；harbor 工作区是下面独立的 2.5 步。

## 2.5 准备 harbor 工作区（评测前置）

先配置 harbor 路径，二选一：

- 修改 `harborstack.toml`：
  ```toml
  [paths]
  harbor_repo = "/你的实际路径/harbor"
  ```
- 或只对当前 shell 生效：
  ```bash
  export HARBORSTACK_PATHS__HARBOR_REPO=/你的实际路径/harbor
  ```

然后准备 venv：

```bash
cd ~/code/harbor-otel-stack
scripts/dind/setup-harbor.sh
# 需要 harbor 开发/云适配器时用完整环境（约 8.2G）：
# scripts/dind/setup-harbor.sh --full
```

> 脚本默认使用一个可达的 PyPI 镜像;要换源时在同一条命令前覆盖
> `UV_DEFAULT_INDEX`。同步完成后会恢复 harbor 的 `uv.lock`,不会把镜像
> URL 写进锁文件。

脚本会打印：

- 解析后的 harbor 路径
- `.git/HEAD` 里的分支
- `harbor --version`

> 只运行评测时 `--no-dev` 足够（约 211M）；路径和 `harborstack` CLI
> 必须使用同一个来源（环境变量或 `harborstack.toml`）。

---

## 3. 启动 dind

```bash
cd ~/code/harbor-otel-stack
scripts/dind/start-dind.sh
```

默认参数：

| 参数 | 默认值 |
|---|---|
| 容器名 | `harborstack-dind` |
| 镜像 | `docker.m.daocloud.io/library/docker:27.5.1-dind` |
| API | `127.0.0.1:23750` |
| 数据卷 | `harborstack-dind-data` |
| CPU / 内存 | `12` / `16g` |

> 默认内存按 compose 的 `mem_limit` 总和（约 10.5G）留了余量。小机器可以调小：
> `DIND_MEMORY=12g DIND_CPUS=8 scripts/dind/start-dind.sh`。
> 修改 `DIND_MEMORY` / `DIND_CPUS` / `DIND_VOLUME` / `DIND_API_PORT` 后，
> 需要先删除旧 dind 容器再启动（数据卷保留）：
> `scripts/dind/stop-dind.sh && docker rm harborstack-dind && scripts/dind/start-dind.sh`。
> 如果用了 `DIND_NAME` 覆盖，把 `docker rm` 的名字换成实际容器名。

脚本会：

1. 必要时拉取 dind 镜像；
2. 创建/复用独立数据卷；
3. 以 `--privileged` 启动 dind；
4. 等待内部 Docker daemon 就绪。

成功后输出：

```text
==> dind ready: client=27.5.1 server=27.5.1
Use:
  export DOCKER_HOST=tcp://127.0.0.1:23750
  export HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io
```

> 如果要复用已有的 dind 容器/卷，在 `var/dind/env`（gitignored）里覆盖
> `DIND_NAME`、`DIND_VOLUME` 等变量。

---

## 4. 准备并导入镜像

镜像来源与宿主机 Docker 路径相同。先让**宿主机** Docker 把 6 个镜像准备好
（这一步只拉镜像，不创建任何容器）：

```bash
cd ~/code/harbor-otel-stack
unset DOCKER_HOST          # 这一步必须操作宿主机 Docker,不能用 dind
export HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io
.venv/bin/harborstack install
```

预期 6 个镜像 `[ok]`，`clickhouse_patch` 显示 `[skip]`（x86_64 无需穿刺）。

> 镜像来自离线 bundle 时，把上面的 `install` 换成
> `.venv/bin/harborstack install --from-bundle /path/to/harborstack-<arch>-runtime`；
> 后面的 `load-images.sh` 不变。打包与恢复流程见 [runbook-bundle.md](runbook-bundle.md)。

然后把宿主机镜像流式导入 dind：

```bash
scripts/dind/load-images.sh
```

预期 6 个 `Loaded image` + 6 个 `ok`。

说明：

- `docker save/load` **不会保留 RepoDigests**；`harborstack init` 会按 digest
  从镜像源补拉 manifest 元数据。镜像层已经在 dind 本地，所以补拉很快。
- 这要求 **dind 容器能访问镜像源**。
  如果 dind 完全不能出网，需要额外的本地 registry 方案；当前手册未覆盖。

---

## 5. 起栈

```bash
scripts/dind/run-init.sh
```

脚本会：

1. 停掉上一轮的宿主机 forwarder / dind 内 sidecar；
2. 移走旧的 `var/ports.json`，强制重新分配；
3. 启动 watcher，等 `var/ports.json` 出现后启动两层端口转发；
4. 用 `DOCKER_HOST=tcp://127.0.0.1:23750` 执行 `harborstack init`。

> **端口转发范围**：watcher 会把 `var/ports.json` 中的全部 8 个端口
> （web、worker、ClickHouse HTTP/native、MinIO API/console、Redis、Postgres）
> 都绑定到宿主机 `127.0.0.1`，不只是 web。它们只监听回环，其他用户不能直接访问；
> 如果需要最小暴露面，需要修改 `watch-and-forward.sh` / `forwarder.py` 的端口列表。

预期输出：

```text
[ok  ] platform          x86_64 | rcpc=no | needs_patch=no
[skip] langfuse_web      已就绪 ...
...
[skip] clickhouse_patch  x86_64 无需穿刺
[ok  ] ports             web=18930，worker=18900，...
[ok  ] render            .../var/.env
[ok  ] compose           config 校验通过
[ok  ] up                项目 harborstack-default 已启动

[ok  ] containers   六容器全部 running/healthy
[ok  ] clickhouse   SELECT 1 通过
[ok  ] web          /api/public/health 200（端口 18930）
[ok  ] events_full  events_full 已建出

Langfuse   http://localhost:18930
登录账号   admin@harborstack.local
端口分配   web=18930，...
==> Langfuse: http://localhost:18930
```

退出码 0 即成功。

---

## 6. 验证

```bash
cd ~/code/harbor-otel-stack
export DOCKER_HOST=tcp://127.0.0.1:23750
export HARBORSTACK_IMAGE__MIRROR=docker.m.daocloud.io

.venv/bin/harborstack doctor
.venv/bin/harborstack display fixtures/atif-fidelity

# 一条命令看 dind、sidecar、转发进程、端口和 web 健康
scripts/dind/status.sh
```

预期：

- `doctor`：**11 项检查，fail=0 warn=0**
- `display`：`trials 1（跳过 0，缺文件 0）`、`状态 ok`，并打印一条 trace 链接

`fixtures/atif-fidelity` 里就是 `path-tracing-reverse__N5FYxHG` 单 case，用于打通链路。

> 刚起栈时 `doctor` 的 `ports` 项可能因为 2 秒健康探测超时而短暂 `warn`。
> `verify.sh` 会先等 `/api/public/health` 返回 200;手动执行时若看到这一条,
> 确认同一轮后面的 web 是 200 即可,或在 web 稳定后重跑 `doctor`。

### 终端查看 trace（不需要浏览器）

```bash
.venv/bin/python scripts/show-trace.py
# 也可以指定 report.json 或 trace id：
# .venv/bin/python scripts/show-trace.py fixtures/atif-fidelity/report.json
# .venv/bin/python scripts/show-trace.py 5a623a1351fc5e17871f22748c503534

# display 刚返回时读路径可能还没可见，最多等 60 秒：
.venv/bin/python scripts/show-trace.py --wait 60
```

输出是树状时间线：type、name、latency、父子关系，例如：

```text
trace  http://127.0.0.1:18930/project/harborstack-project/traces/...
count  101  (generation=47, span=54)
- 12:34:54.579 SPAN       trial                 610.622s
  - 12:34:59.383 GENERATION llm                     0.931s
    - 12:34:59.383 SPAN       tool.bash               0.931s
```

> `display` 返回只代表 ingestion 接受了数据，observations 的读路径可能还有
> 几秒延迟。手动执行如果看到 `count 0`，等几秒重跑，或加 `--wait 60`。
> `verify.sh` 已经默认使用 `--wait 60`。
>
> v4 `events_only` 模式下 observations API 不返回 input/output；终端脚本只展示
> 结构、类型和耗时。完整的消息内容请在 Langfuse UI 或 ClickHouse 里看。

### 打开页面

- 本地浏览器：`http://localhost:18930`
- 账号：`admin@harborstack.local`
- 密码：`ChangeMe-Harborstack1`

从自己电脑访问需要 SSH 隧道：

```bash
ssh -L 18930:127.0.0.1:18930 user@host  # 换成你的用户名和机器地址
```

然后在本地打开 `http://localhost:18930`。

Langfuse v4 如果 Tracing 列表为空，去 **Observations** 页，把
`Is Root Observation` 设为 `true` 查看。

## 6.5 评测（独立文档）

评测**不在 dind 里运行**。dind 只承载 Langfuse；评测默认使用宿主机 Docker，
这样才能让任务镜像、agent 安装和 verifier 网络按预期工作。

完整命令、前置检查和排错见 [runbook-eval.md](runbook-eval.md)。

> 如果你确实要把评测也放进 dind，需要先加载任务镜像，并自行解决
> agent/verifier 的网络访问；这属于高级场景，不在傻瓜指南范围内。

---

## 7. 重跑 / 停止 / 清理

### 重跑 init

```bash
scripts/dind/run-init.sh
```

脚本会自动停掉旧的 forwarder/sidecar，重新分配端口并起栈，幂等。

### 停止但保留数据

```bash
scripts/dind/stop-dind.sh
```

只停止 dind 容器；容器、命名卷和 Langfuse 数据都保留。

### 彻底删除（数据会丢）

```bash
scripts/dind/stop-dind.sh --purge
```

删除 dind 容器和命名卷 `harborstack-dind-data`，**所有 Langfuse 数据丢失**。

> 宿主机 Docker 路径第一次失败时留下的容器/数据卷是另一套对象，和 dind 无关；
> 如需清理，用宿主机的 `harborstack down`（会删宿主机数据卷），不要和 dind 混淆。

---

## 8. 架构与隔离

```text
宿主机 Docker
└── harborstack-dind (privileged, 独立 netns, 16G/12CPU)
    ├── 6 个 Langfuse 栈容器 (healthy)
    └── harborstack-dind-fwd (sidecar, 共享 dind netns)
         监听 dind-ip:189xx  ->  127.0.0.1:189xx

宿主机 loopback 转发
127.0.0.1:189xx  ->  dind-ip:189xx
```

- 宿主机 `189xx` 端口只绑 `127.0.0.1`，不对外暴露。
- 不改宿主机 firewalld/iptables。
- dind 内 compose 仍绑定 `127.0.0.1:189xx`。
- 资源限制：dind 16G / 12 CPU，sidecar 128M / 0.5 CPU。
- dind 数据全部在命名卷里；删除卷会丢数据。
- aarch64 无 RCpc 时，dind 以相同绝对路径只读挂载仓库目录和 patch cache；
  `run-init.sh` 把 `TMPDIR` 指向 `cache/tmp`，使 patch smoke test 和最终
  ClickHouse 二进制对 dind 可见。

---

## 9. 常见问题

dind 相关现象、原因和处理统一放在 [faq.md](faq.md)。

---

## 10. 附录：与宿主机 Docker 路径的关系

- **dind 路径**（本手册）：不依赖宿主机 Docker bridge 转发，适合共享/受限机器。
- **宿主机 Docker 路径**（[runbook-langfuse-host.md](runbook-langfuse-host.md)）：
  直接用宿主机 Docker，更轻量，但要求宿主机 firewalld/iptables 允许 Docker bridge 转发。
- 两条路径共用同一套 `harborstack` / `harbor` 代码和 6 个镜像，只是 Docker daemon
  和端口转发方式不同。

如果以后宿主机网络修复，可以停掉 dind，再按宿主机 Docker 路径执行；不需要改项目代码。
