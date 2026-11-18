# harborstack 设计方案

> 仓库名：`harborstack`　包名 / CLI：`harborstack`
> 本文描述当前实现的设计与契约。与代码冲突时以代码为准，并同步修订本文。

---

## 1. 背景

Harbor 的评测产物是 ATIF（`agent/trajectory.json` + `verifier/reward.json`），需要用 Langfuse 做可视化。
落地时需要处理四类问题：

| 问题 | 现象 | 采用的做法 |
| --- | --- | --- |
| CPU 指令集 | ClickHouse 官方 aarch64 镜像在无 RCpc 的机器上启动即 SIGILL | LDAPR* → LDAR* 二进制穿刺 + 关闭自校验 |
| 时间轴失真 | 末步 span 长达约 70 小时 | 时间语义投票 + 窗口预计算 + 毫秒级 clamp |
| 版本漂移 | `langfuse:4`、`minio:latest` 是移动 tag | 用 digest 冻结并校验 |
| 多实例共存 | 端口冲突导致其他服务不可用 | 端口分配器 + 只绑 127.0.0.1 + 项目级隔离 |

---

## 2. 目标与非目标

### 目标

1. 一条命令完成部署，用户不需要知道 RCpc、不需要知道我们打过补丁。
2. 版本冻结：上游更新不会让已打通的路径失效。
3. 失败即拒绝：宁可报错退出，也不产出"能跑但数据错"的结果。
4. 跨机器可复现：同一套输入在另一台机器上得到同样的部署与同样的数字。
5. 不干扰共享机器上的其他用户。

### 非目标

- 不做任务调度、多机分布式、K8s 部署
- 不做 dataset / task 生命周期管理（`run` 只透传 harbor CLI）
- 不追 Langfuse unstable dashboards API（模板让用户在 UI 内复制）
- 不做 LiteLLM proxy 的性能指标回填（当前数据无 proxy）
- 不做 Langfuse 源码 fork（见 §15 ADR-4）

---

## 3. 核心原则

1. **冻结优先于追新**：`lock.json` 冻结所有外部依赖的 digest；compose 按 tag 渲染，拉取与就绪判定都以 digest 为准。
2. **契约优先于脚本**：环境、数据、服务三份契约，命令只负责建立与校验契约。
3. **拒绝优先于降级**：契约不满足时退出码非 0，不静默继续。
4. **幂等优先于"能跑"**：重复执行结果收敛。
5. **隔离优先于方便**：端口只绑回环、项目名带前缀、清理只按 label。

---

## 4. 三层冻结

### 4.1 第一层：镜像 digest 锁（`lock.json`）

```json
{
  "schema": 1,
  "frozen_at": "2026-10-28",
  "pins": {
    "langfuse_web": {
      "ref": "langfuse/langfuse:4",
      "version": "4.70.1",
      "digest": "sha256:71b04842ecff…",
      "origin": "upstream"
    },
    "langfuse_worker": { "ref": "langfuse/langfuse-worker:4", "digest": "sha256:3976a73de84a…", "origin": "upstream" },
    "clickhouse": { "ref": "clickhouse/clickhouse-server:25.12-alpine", "version": "25.12.11.4",
                    "digest": "sha256:7d93fb39bb2b…", "origin": "upstream" },
    "minio": { "ref": "bitnamilegacy/minio:latest", "digest": "sha256:451fe6858cb7…", "origin": "upstream" },
    "postgres": { "ref": "library/postgres:16", "digest": "sha256:ca0bd484cb98…", "origin": "upstream" },
    "redis": { "ref": "library/redis:7-alpine", "digest": "sha256:858f009f9709…", "origin": "upstream" }
  },
  "compose": {
    "upstream": "github.com/langfuse/langfuse",
    "upstream_commit": "7526ede92399…",
    "license": "MIT",
    "files": {
      "docker-compose.yml": "9885a197…",
      "docker-compose.isolation.yml": "f0e075bb…",
      "docker-compose.patch.yml": "affafc5f…",
      "clickhouse-skip-checksum.xml": "984dfd65…"
    }
  },
  "artifacts": {
    "clickhouse_patch": {
      "source_sha256": "16c6f4c6a6e5…",
      "output_sha256": "6f8872fc20d8…",
      "size_bytes": 706962096,
      "patcher": "1.0.0",
      "sites": { "ldapr_x": 211821, "ldapr_w": 1013, "ldaprb": 14260, "ldaprh": 1, "total": 227095 },
      "requires": { "machine": "aarch64", "rcpc": false }
    }
  }
}
```

规则：

- `pins.<name>.ref` 是 compose 渲染用的镜像引用（可加 `image.mirror` 前缀）；`digest` 是唯一判等依据。
- `install` 先按 `ref@digest` 拉取并回标 tag；该路径失败时退回按 tag 拉取，拿到镜像后再核对 digest，不符即拒绝。
- `compose.files` 记录四份 compose 资产的 sha256，`doctor` 的 `assets` 检查逐份比对。
- `version` 字段用于人类阅读与问题定位。

### 4.2 第二层：补丁锁

补丁不是"一个二进制"，而是映射：

```
(源镜像 digest, patcher 版本) → 产物 sha256 + 命中明细
```

镜像 digest 变化 → 补丁锁失效 → `init` 以退出码 3 拒绝执行。
**绝不允许**用旧补丁跑新镜像。

### 4.3 第三层：行为锁

转换器的语义行为由测试固定：

- 时间语义投票（`end` / `start` 判定）
- 相邻 LLM 窗口推算工具 span
- 毫秒级时间戳抖动吸收（clamp 负时长为 0）
- `atif.timing_source` 如实标注来源：`measured:<字段>`、`measured_window`、`inferred_window`、`unknown`

落地方式：`tests/test_invariants.py` + fixture `fixtures/atif-fidelity/`（另附 `fixtures/atif-error/` 覆盖失败路径）。

---

## 5. 三份契约

### 契约 A：环境契约 `var/env-manifest.json`

```json
{
  "schema": 1,
  "platform": { "machine": "aarch64", "rcpc": false, "needs_patch": true },
  "images": { "clickhouse": { "digest": "sha256:..." } },
  "binaries": { "clickhouse": { "sha256": "...", "patched_sites": 227095, "patcher": "1.0.0" } },
  "ports": { "web": 18930, "worker": 18900, "clickhouse_http": 18901 },
  "compose": { "docker-compose.yml": "9885a197…", "docker-compose.isolation.yml": "f0e075bb…" },
  "state": "ready"
}
```

`init` 每次执行都做三元组校验：镜像 digest、补丁产物 sha256、平台探测结果。任一不符即拒绝。

### 契约 B：数据契约（ATIF → OTLP 映射）

| ATIF 来源 | OTel 目标 | 必带属性 |
| --- | --- | --- |
| `steps[].source=agent` | span `llm`（GENERATION） | `gen_ai.*`、usage、`atif.timing_source` |
| `tool_calls[]` | span `tool.<name>` | `atif.timing_source` |
| `metrics.extra.total_latency_ms` | span duration | `atif.duration_ms` |
| trial 目录 | trace + root span `trial` | `atif.timing_semantics`、`atif.timing_votes` |

原则：**有值必须说明来源**。禁止"有时长但不说从哪来"。

多 run 区分靠 Langfuse v4 的 experiment 属性组：

| 属性 | 取值 | 说明 |
| --- | --- | --- |
| `langfuse.experiment.id` | trial 的 `run_id` | 一次评测一个实验 |
| `langfuse.experiment.name` | `--job-name`，回落 run 目录名 | 列表展示名 |
| `langfuse.experiment.dataset.id` | harbor `-p` 目录名，回落 job 目录名 | 同一数据集跨 run 可比 |
| `langfuse.experiment.item.id` | `task_id` | 跨 run 对齐同一任务 |
| `langfuse.experiment.item.root_observation_id` | root span id 的 16 位十六进制 | 整型超过 int64 会被丢弃 |

缺 `item.id` / `item.root_observation_id` 时 UI 不会聚合成实验；
`dataset.id` 取 harbor 参数 `-p` / `--path` / `--dataset` 的目录名，缺省回落到 job 目录名。

同一 trial 的所有 span 都带这组属性；trace 级另带 `langfuse.trace.metadata.run_id` / `job_name` 供搜索过滤。

判分回填的 score 必须显式带上 trace 的起始时间作为 `timestamp`。缺省时 Langfuse 记为写入时刻，
与 trace（用 ATIF 里的真实执行时间）落在不同时间窗口，按时间筛选会出现「有 score 无 trace」。

### 契约 C：服务契约

- 端点、端口、健康判据全部由 manifest 声明，`display` 只读不猜。
- 绑定一律 `127.0.0.1`；`restart: "no"`；每容器 `mem_limit` + `cpus`。
- 清理只按 `com.docker.compose.project` + 自有 label 过滤，禁止全局 `prune`。

---

## 6. CLI 设计

`--config / --set / --json` 注册在每个子命令上（根 parser 只有 `--version`）；`--lock` 同样按子命令注册，
但 `run` / `display` 不使用锁文件。`--dry-run` 只存在于 `install / init / run / display / down`
（`doctor` 本身只读，不需要）。重复执行结果收敛。

| 命令 | 职责 | 幂等语义 |
| --- | --- | --- |
| `install` | 拉取 / 导入镜像、准备补丁产物 | 同 digest / 同 sha256 则跳过 |
| `init` | 平台探测 → 卷/密钥校验 → 镜像与补丁 → 端口分配 → 渲染 → 起栈 → 健康门禁 | 复用端口与已就绪镜像，重走门禁 |
| `run` | 调用本地 harbor 仓 CLI，产出 run 目录 | 透传参数，不持有评测配置 |
| `display` | 健康校验 → 导入 ATIF → 输出链接与对账 | trace 已存在则跳过，不新增 observation |
| `doctor` | 只诊断不修改 | 无副作用 |
| `bundle create\|verify` | 生成 / 校验离线包 | `create` 覆盖同名文件；`verify` 只读比对 |
| `down` | 按 label 清理本项目 | 只清自己 |
| `version` | 打印版本 | — |

关于 harbor 仓的边界（已定）：

- 不 clone、不 fetch、不切分支、不冻结版本、**不安装依赖**；只读 `paths.harbor_repo` 指向的现成工作区
- 可执行环境按序发现：`<repo>/.venv/bin/harbor` → `$UV_PROJECT_ENVIRONMENT/bin/harbor` → `PATH` 上的 `harbor`；都没有则退出 3 并提示先准备环境（`uv sync`），绝不代跑 `uv sync` / `uv run`
- `run` 的 cwd 保持**调用方目录**，保证 `-c config.yaml`、`-t ./task` 这类相对路径照常有效；用户没写 `-o/--out` 时自动补上 `<run_dir>`，产物因此不落在该仓
- 该仓的 `HEAD` / 分支 / 脏状态 / 实际命令只记入 `var/runs/<id>/run.json` 与 `report.json` 供追溯，**不做校验、不阻塞**
- 退出码：harbor 的 0 / 2 原样传递，其余一律映射为 5（数据或运行失败），原始码保留在 `run.json`
- `display` 默认取 `var/runs/` 下最新的 run 目录；若 `run.json` 记录了 `jobs_dir`（用户自己指定了 `-o`），以它为准扫描 Trial

因此「可复现」由调用方保证：同一份产物要复现，须自己固定该工作区的分支与提交。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 2 | 参数或调用错误（含 `run` 缺 harbor 参数、`down` 拒绝执行） |
| 3 | 配置或环境不满足（`doctor` 有 `fail`、`install` 失败、`init` 任一环节失败） |
| 4 | 服务不可用（`display` 健康门禁失败、`down` 失败） |
| 5 | 数据异常（`display` 对账失败、`bundle verify` 不符、`run` 映射 harbor 非 0/2 退出码） |

### 6.1 端口分配器

从 `ports.range` 起逐个尝试 `bind("127.0.0.1", p)`，成功即占用并写回 `var/ports.json` 与 manifest。

必须实现的原因：共享机器上端口可能被其他实例或 IDE 中继占用，且占用方未必给出提示。分配器提前避让，`doctor` 明确报告冲突方。

### 6.2 健康门禁

`init` 成功 = 六容器 running（声明了 healthcheck 的须 healthy）+ ClickHouse `SELECT 1` 可执行
+ web `/api/public/health` 200 + `events_full` 已由迁移建出。

`display` 使用更轻的两段门禁（manifest `ready` + `/api/public/health` 200），**不提供跳过开关**：
一旦允许绕过，用户会在栈未就绪时导入，得到的是一批静默丢失的 trace。

通过门禁后，每个 Trial 上报前先查 `v2/observations?traceId=…&limit=1`，已有数据则标记
`skipped` 并跳过，避免 v4 events 模式下的重复 observation（见 §10.2）。

---

## 7. 配置

仓库根 `harborstack.toml`，带默认值；优先级 `CLI > 环境变量 HARBORSTACK_* > toml > 内置默认`。

```toml
[stack]
env = "default"             # compose 项目名 = harborstack-<env>

[platform]
allow_patch = true          # aarch64 且无 rcpc 时是否允许穿刺

[paths]
harbor_repo = "~/code/harbor"
cache_dir   = "~/.cache/harborstack"
var_dir     = "./var"

[ports]
range     = [18900, 18999]
preferred = { web = 18930 }

[langfuse]
image         = "langfuse/langfuse:4"
worker_image  = "langfuse/langfuse-worker:4"
memory        = "2g"
cpus          = 2.0
security_mode = "default"   # default | generated
admin = { email = "admin@harborstack.local", name = "admin", password = "ChangeMe-Harborstack1" }

[clickhouse]
image     = "clickhouse/clickhouse-server:25.12-alpine"
bin_cache = true            # 预留项，当前未读取；缓存命中由产物 sha256 决定

[minio]
image = "bitnamilegacy/minio:latest"

[postgres]
image = "library/postgres:16"

[redis]
image = "library/redis:7-alpine"

[image]
mirror = "docker.1ms.run"   # 为空则直连；镜像 ref 统一加此前缀

[export]
mode       = "full"         # full | structural
idempotent = true           # 预留项，当前未读取；幂等由导入前存在性检查保证

[bundle]
dir = "/share/harborstack-bundle"   # 默认输出目录
mode = "docker-save"                # 预留项（docker-save | oci），不参与档位选择
include_patched_bin = true

[report]
path = "./var/report.json"

[display]
wait_seconds = 60           # 导入后等待 trace 可查的上限（秒）；0 = 不等，dry-run 不等待
```

注意 `[bundle]` 段只描述默认输出位置与是否带入补丁产物，**打包档位由 CLI 的 `--mode` 决定**
（`skeleton | full | offline`，见 §8），与 toml 里的 `bundle.mode` 不是一回事。

`security_mode = "default"` 使用固定口令（仅适合只绑回环的调试形态）；
`generated` 首次生成随机密钥写入 `var/secrets.json`（0600、gitignore），`var/.env` 由它派生。

---

## 8. bundle：跨机器少依赖外网

六个镜像解压后合计约 5.5GB，因此**单个 675MB 的补丁二进制解决不了网络问题**，
正确形态是可搬运的离线包：

```
bundle/
├── manifest.json      # 每个文件的 size + sha256，KB 级
├── images.tar         # docker save 的六个镜像（仅 full / offline）
├── patch/             # 已穿刺的 ClickHouse 二进制
├── compose/           # 三个 compose 资产 + skip-checksum.xml
├── lock.json          # 冻结的 digest 与资产指纹
└── wheels/            # harborstack 及其依赖的 wheel（仅 offline）
```

三档由 CLI 的 `--mode` 选择，差异在于「目标机器缺什么」而非打包体积：

| 模式 | 内容 | 目标机器缺什么 |
| --- | --- | --- |
| `skeleton` | compose + lock + 穿刺产物 | 已有镜像 |
| `full` | skeleton + `images.tar` | 无镜像 |
| `offline` | full + `wheels/` | 无镜像且无 PyPI |

命令与边界：

- `bundle create --out <dir> --mode <档位>`：有网机器生成一次；`--out` 不存在时会创建，同名文件被覆盖（不检查目录是否为空）
- `bundle verify <dir>`：逐文件比对 size + sha256，缺失或被改动返回退出码 5
- `install --from-bundle <dir>`：先 verify，再 `docker load`，最后核对补丁 sha256
- git 内**不存** `images.tar`、补丁二进制、`wheels/`，只存生成它们的代码

`install --from-bundle` 导入后，`ensure_images` 用 digest 判等，镜像已齐备时不会发起 `pull`。

已知限制：`offline` 档用 `pip wheel --no-deps` 打包本包，并只 `pip download tomli`，
不解析 `opentelemetry-sdk` / `opentelemetry-exporter-otlp-proto-http` 的传递依赖，
目标机执行 `pip install --no-index` 可能失败。

---

## 9. ClickHouse 穿刺子系统

### 9.1 判定

平台探测读取 `/proc/cpuinfo` 的 `rcpc` 标志：

- 有 `rcpc` 或 `x86_64` → `needs_patch = false`，直接使用官方镜像
- `aarch64` 且无 `rcpc` → `needs_patch = true`，进入穿刺流水线

### 9.2 流水线

```
digest → 提取二进制 → 纯 Python 解析 ELF program header，取全部可执行 PT_LOAD 段
       → 扫描 4 类 RCpc 编码 → 记录命中明细
       → 原地替换（保留 Rt/Rn/size 位）
       → chmod 755（必须，否则容器内 Permission denied）
       → 同镜像内冒烟（--version + SELECT sum(number)）
       → 计算 sha256，与 lock 记录比对后写入缓存与 manifest
```

缓存文件名 = `clickhouse-<源镜像 digest 前 12 位>-p<patcher 版本>`；命中条件不是文件存在，
而是文件 sha256 与 `lock.json` 记录一致，否则重建。

### 9.3 编码映射

| 原指令 | 编码基址 | 替换为 | 编码基址 |
| --- | --- | --- | --- |
| `ldaprb Wt,[Xn]` | `0x38BFC000` | `ldarb Wt,[Xn]` | `0x08DFFC00` |
| `ldaprh Wt,[Xn]` | `0x78BFC000` | `ldarh Wt,[Xn]` | `0x48DFFC00` |
| `ldapr  Wt,[Xn]` | `0xB8BFC000` | `ldar  Wt,[Xn]` | `0x88DFFC00` |
| `ldapr  Xt,[Xn]` | `0xF8BFC000` | `ldar  Xt,[Xn]` | `0xC8DFFC00` |

匹配 `word & 0xFFFFFC00`，替换 `新基址 | (word & 0x3FF)`。
安全性依据：LDAR 的 RCsc 语义强于 LDAPR 的 RCpc，替换只加强不放宽。

### 9.4 接入方式

- 关闭二进制自校验：`skip_binary_checksum_checks=1`（挂载 `config.d/zz-skip-checksum.xml`）
- compose 只读挂载 `patch/clickhouse → /usr/bin/clickhouse`（镜像内 client/keeper 均为软链，覆盖一个文件即可）
- 换二进制后必须 `up -d --force-recreate`（bind mount 绑定 inode，`restart` 无效）

### 9.5 已知边界

若新版本穿刺后仍 SIGILL，说明出现其他 ARMv8.3+ 指令，用 `src/harborstack/patch/ptrace_sigill.c` 抓 PC 与指令字，按同思路扩展映射表。

---

## 10. ATIF → OTel 转换器

内嵌在 `src/harborstack/convert/`，不依赖任何外部转换器，语义行为由测试固定。

| 模块 | 职责 |
| --- | --- |
| `scan.py` | 发现 Trial、解析 trajectory.json / reward.txt / reward.json / reward-details.json |
| `plan.py` | 纯 Python span 计划：语义投票、窗口预计算、时间收敛、幂等 id |
| `export.py` | 计划 → OTel span 树 → OTLP/HTTP 上报（`PlanIds` 回放计划 id） |
| `sanitize.py` | `structural`（保结构弃内容）/ `full`（仅截断） |
| `scores.py` | 判分回填，走 Public API，仅标准库 |

### 10.1 关键行为

- **时间语义投票**：step 级时间戳是 start 还是 end，由整条轨迹投票决定并记录票数与比例
- **窗口预计算**：工具 span 区间取 `[本轮 LLM 结束, 下一次 LLM 起点]`，没有下一次时退化为零长度
- **clamp**：窗口翻转（毫秒级抖动）时夹为零长度，避免负时长
- **不存在 `end_ts = None`**：三个分支（measured / inferred_window / unknown）全部收敛为非空

### 10.2 幂等导入

- `trace_id = uuid5(NS, "harbor:{run_id}/{task_id}/{trial_dir}")` → 128 bit
- `span_id = uuid5(NS, "{trace_id:032x}:{step_index}:{kind}")` 取前 16 hex → 64 bit（结果为 0 时取 1）
- **`start_time` 必须由 ATIF 时间戳推导，禁止使用「导入时刻」**

依据：v4 的 `events_full` 是 `ReplacingMergeTree(event_ts, is_deleted)`，
去重键为 `(project_id, toStartOfMinute(start_time), xxHash32(trace_id), span_id, start_time)`。
三元组完全一致时重复摄入最终收敛为一行；**`start_time` 一旦变化，重复行永久保留**。

写入侧可收敛不代表读取侧去重：`events_only` 模式下重复上报同一份 Trial，
`v2/observations` 返回的 observation 数会翻倍。因此不能只靠 ReplacingMergeTree，
`display` 在每批上报前先做存在性检查（`v2/observations?traceId=…&limit=1`），已有数据则跳过，
并在 `report.json` 中记为 `skipped`。

---

## 11. 部署与隔离

- compose 项目名 `harborstack-<env>`，所有容器打 `io.harborstack=1` 标签
- 端口全部 `127.0.0.1`；`restart: "no"`；`mem_limit` + `cpus`
- MinIO 使用 bitnami 镜像并以 root 启动创建桶目录（官方 cgr.dev 镜像拉取受限）
- ClickHouse 健康检查用 busybox 的 `wget -q -O /dev/null`；MinIO 用 `curl -sf`
- `down` 只删本项目容器与卷，不触碰其他用户

资产与命令：

| 文件 | 作用 |
| --- | --- |
| `src/harborstack/deploy/langfuse/docker-compose.yml` | 上游快照（只读基线） |
| `src/harborstack/deploy/docker-compose.isolation.yml` | 命名空间、端口、资源、隔离 |
| `src/harborstack/deploy/docker-compose.patch.yml` | 仅需穿刺时叠加 |
| `src/harborstack/deploy/clickhouse-skip-checksum.xml` | 关闭穿刺后自校验 |
| `src/harborstack/commands/init.py` | 探测 → 卷/密钥校验 → 端口 → 渲染 → 起栈 → 门禁 → manifest |
| `src/harborstack/commands/down.py` | 项目名白名单 + 按 label 清理 |
| `src/harborstack/commands/bundle.py` | 三档离线包 create / verify |
| `src/harborstack/commands/display.py` | 门禁 → 扫描 → 上报 → 判分 → report |
| `src/harborstack/deploy/ports.py` | 端口分配器 |
| `src/harborstack/deploy/render.py` | `.env` / secrets / 端口持久化 / compose 命令 |
| `src/harborstack/deploy/health.py` | 四段健康门禁 + 本项目已占端口识别 |
| `src/harborstack/manifest.py` | 环境契约 `var/env-manifest.json` |

---

## 12. run.json 与 report.json

`run` 固定产出 `var/runs/<id>/run.json`（一次调用的追溯记录）：

```json
{
  "schema": 1,
  "created_at": "2026-10-28T15:30:12+08:00",
  "harbor": { "repo": "/path/to/harbor", "runner": "repo-venv",
              "head": "bf869895...", "branch": "main", "dirty": false },
  "argv": ["-c", "config.yaml", "-t", "./task"],
  "command": ["/path/to/harbor/.venv/bin/harbor", "-c", "config.yaml", "-t", "./task",
              "-o", "/path/to/var/runs/20261028-153012-ab12cd"],
  "jobs_dir": ".../runs/20261028-153012-ab12cd",
  "exit_code": 0,
  "mapped_exit": 0,
  "elapsed_s": 1832.4
}
```

`display` 固定产出 `<run_dir>/report.json`，同时写一份到 `report.path`（默认 `var/report.json`）作为「最近一次」：

```json
{
  "schema": 1,
  "generated_at": "2026-10-28T00:00:00Z",
  "dry_run": false,
  "run": { "dir": ".../runs/20261028-153012-ab12cd", "jobs_dir": "...",
           "trials": 1, "skipped": 0, "missing": 1 },
  "harbor": { "repo": "...", "head": "...", "branch": "...", "dirty": false, "runner": "repo-venv" },
  "env": { "web": "http://127.0.0.1:18930",
           "clickhouse_digest": "sha256:...", "clickhouse_bin_sha256": "..." },
  "trials": [{
    "name": "datacurve/abs-module-cache-flags",
    "trial_id": "task-abs-module-cache-flags__8ukCeGz",
    "trace": { "id": "aee8c80ab0c6d45d0f9b7270b7954cc4", "url": "http://localhost:18930/project/.../traces/..." },
    "counts": { "events": 144, "generation": 72, "span": 72 },
    "timing": { "semantics": "end", "votes": 72, "end_ratio": 0.917,
                "root_s": 365.746, "llm_s": 247.948, "tool_s": 111.883, "clamped": 4 },
    "reconcile": { "reward": 1, "reward_match": true, "llm_sum_match": true,
                   "orphan_spans": 0, "negative": 0, "outside_root": 0 },
    "scores": [{ "name": "reward", "status": 200, "detail": "ok" }],
    "exception": null,
    "status": "ok",
    "error": null
  }],
  "settle": { "trace_id": "aee8c80ab0c6d45d0f9b7270b7954cc4", "visible": true, "waited_s": 3.2 },
  "skipped": [{ "trial": "task__agent__model__1", "path": "/abs/path",
                "kind": "parse_error", "reason": "trajectory.json 解析失败：..." }],
  "missing": [{ "trial": "task__agent__model__2", "path": "/abs/path",
                "kind": "missing_atif", "reason": "缺少 agent/trajectory.json（agent 异常：NonZeroAgentExitCodeError）" }],
  "status": "ok"
}
```

字段口径：

- `counts.events` = span 总数；`generation` = GENERATION 数；`span` = 其余（含 root）
- `timing.llm_s / tool_s` 为同角色 span 时长之和；`clamped` 是上界早于下界、被夹为零长度的条数
- `llm_sum_match` 只在全部 generation span 都是 `measured` 时才有意义，否则为 `null`（合并窗口不是实测值）
- `reward_match` 由判分回填的 HTTP 结果决定；Langfuse v4 的 Public API 若返回 404/405，记为 `null`（不适用），不判失败
- `outside_root` 不统计 `subagent` span（其时间来自独立轨迹）
- `exception` 取自 Trial `result.json` 的 `exception_info`，无异常为 `null`
- `skipped` 明细只记「有 `agent/trajectory.json` 但读不出来」的目录；`skipped > 0` 计入失败
- `missing` 明细记「case 目录在、却没有 `agent/trajectory.json`」的目录（容器没起来、超时被杀、agent 未落盘）；只留 `trial` / `path` / `kind` / `reason` 四个定位字段，详细原因回原始目录查；**不重建 trace、不中断流程、不参与失败判定**，只进报告，避免静默消失
- 发现口径：不看目录名，只看产物文件（`agent/trajectory.json`、`agent/events.jsonl.gz`、`verifier/reward.txt|json`）与 `result.json` 的 schema。`result.json` 含 `n_total_trials`/`stats` 视为 job 级并排除；含 `trial_name`/`task_id`/`exception_info`/`verifier` 视为 trial 级。根目录若仅凭 `result.json` 命中且其下还有 case，按容器目录处理，避免吞掉子 trial
- `settle`：真实导入且存在成功导入的 trace 时才有值（`dry_run` 或无需等待为 `null`）；`visible=false` 表示超时，仅提示、不参与 `status` 与退出码
- `status`：任一 Trial 上报失败、`skipped > 0`、`negative > 0`、`outside_root > 0`、或没有任何 Trial，整体为 `failed`（退出码 5）
- Trial 级 `status` 为 `agent_error` 表示 agent 异常（如模型 402 余额不足）但已有轨迹仍完成导入；此时 `error` 记录异常类型与消息，整体 `status` 仍为 `failed`，避免把异常 run 当成干净评测
- `dry_run = true` 时不发网络请求、不写 report.json，但 trace_id 与真实导入一致，可用于预检

---

## 13. 测试与验收

### 13.1 不变量（可自动断言）

- `sum(llm.duration) == sum(metrics.extra.total_latency_ms)`，容差 1ms
- 所有子 span 区间 ⊆ root 区间
- 不存在 `end_ts = None`，且 `end >= start`
- 不存在时长超过 root 时长 1.5 倍的 span（70 小时问题的直接探针）

落地位置：llm 合计由 `display` 对账（`llm_sum_match`，且只在全部 generation 都是 `measured` 时判定），
其余由 `tests/test_invariants.py` 断言。

### 13.2 其它测试

- 重放确定性：同输入连续导出两次，逐 span 的 `(trace_id, span_id, start_time)` 完全一致

### 13.3 端到端自检（验收标准）

基线取两条真实产物的 ATIF，期望值固化在 `fixtures/expected.json`：

| fixture | 来源 | 断言 |
| --- | --- | --- |
| `atif-fidelity` | 成功 trial（path-tracing-reverse，reward=1） | 101 span / 47 generation / 53 tool / root 610.621s / llm 合计 605.818s / 负时长 0 / clamped 0 / 无 ERROR |
| `atif-error` | 402 失败 trial | 2 span / reward=0 / root 与最后一个 llm 标 ERROR，`error_scope=trial`、`error_source=harbor_result` |

`tests/test_invariants.py` 共 6 组用例，按两条 fixture 参数化后收集 12 条：
冻结值快照、时间不变量（含 root 覆盖）、1.5 倍时长探针、构建确定性、
工具输出按 `source_call_id` 匹配、错误标记口径。

---

## 14. 失败模式

| 场景 | 检测点 | 行为 |
| --- | --- | --- |
| CPU 无 rcpc 且未开启穿刺 | `doctor` | 退出 3，提示 `platform.allow_patch` |
| 镜像 digest 与 lock 不符 | `init` 前置校验 | 退出 3，提示重新冻结 `lock.json` |
| 补丁产物 sha256 不符 | `init` / `install` | 退出 3，重跑流水线 |
| 数据卷存在但 `var/secrets.json` 缺失/损坏/mode 变更 | `init` 前置校验（`docker volume ls` 按 project label 过滤） | 退出 3，提示恢复 secrets 或 `down` 删卷重来 |
| 端口被占 | 分配器 | 自动换端口并写回 |
| 容器未 running / healthy | 健康门禁 | 退出 3，列出未就绪的容器与检查项 |
| 导入批次部分失败 | `display` | 退出 5，输出失败批次明细 |
| 对账不等式 | `display` | 退出 5，写 `report.json` 并标 `status=failed` |
| 重跑产生重复 trace | 幂等校验 | 导入前查 `v2/observations`，已存在则 `skipped` |

---

## 15. 决策记录（ADR）

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| ADR-1 | 用 uv 管理的 Python CLI，不做容器化 CLI | 复用 harbor Python 生态；避免共享机器上的 docker-in-docker 权限问题 |
| ADR-2 | 转换器内嵌，不依赖外部转换器包（只把 OTel SDK 当上报依赖） | 上游缺我们需要的修复；避免被上游节奏卡住 |
| ADR-3 | `run` 只封装本地 harbor 仓 CLI | 避免出现第二套评测入口 |
| ADR-4 | 现在不 fork Langfuse，但保留轻量 fork 通道 | fork 的真实成本在构建链与迁移维护；当前扩展诉求均可走 API |
| ADR-5 | 不用 overlay 替代 fork | Langfuse 镜像内是编译产物，overlay 只能换文件，改逻辑必须从源码构建 |
| ADR-6 | 冻结用 digest，不用 tag | `langfuse:4`、`minio:latest` 均为移动 tag，是"路径失效"的直接来源 |
| ADR-7 | 二进制不进 git，只进 bundle | 单个二进制只占总量 12%，对网络无实质帮助，且会让仓库永久变重 |
| ADR-8 | 端口用分配器，不硬编码 | 共享机器上端口占用不可预测，硬编码不可靠 |

---

## 16. 外部标准（第三方仲裁）

| 断言 | 依据 |
| --- | --- |
| span 合规 | OTel GenAI 语义约定 |
| 摄入成功 | Langfuse OTLP 契约（2xx + 入库反查） |
| 转换无损 | ATIF schema 校验 + 对账等式 |
| 补丁安全 | ARM 架构手册：LDAR（RCsc）强于 LDAPR（RCpc） |
| 补丁必要 | ClickHouse 官方 aarch64 的 RCpc 要求 + 平台探测到 cpuinfo 无 rcpc |
| 可复现 | 两次穿刺 sha256 一致；两次导出 payload 哈希一致 |

---

## 17. 附录：当前冻结指纹

| 项 | 值 |
| --- | --- |
| ClickHouse 镜像 | `clickhouse/clickhouse-server:25.12-alpine` / `sha256:7d93fb39…` |
| ClickHouse 版本 | `25.12.11.4` |
| 原始二进制 sha256 | `16c6f4c6a6e577adbe3cfae10eea643b1702bfa21414f9982d30fe24ac16d7ab` |
| 穿刺后 sha256 | `6f8872fc20d8829045d050f3b4947f34bb2f9f671a39ce36f8bae3b47df60291` |
| 命中明细 | x 211821 / w 1013 / b 14260 / h 1 = 227095 |
| Langfuse | `4.70.1` / `sha256:71b04842…` |

穿刺实现与诊断辅助见 `src/harborstack/patch/`。
