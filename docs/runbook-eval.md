# Harbor 评测手册（与 Langfuse 部署解耦）

评测和 Langfuse 部署是两条独立链路：

- Langfuse 可以用 dind，也可以用宿主机 Docker，见 [quickstart.md](quickstart.md)。
- **评测默认使用宿主机 Docker**，不要设置 `DOCKER_HOST` 指向 dind。
- 评测完成后，把 job 目录交给 `harborstack display`，导入任意一套已运行的 Langfuse。

## 0. 前置条件

- 已按仓库 README 或 [quickstart.md](quickstart.md) 准备 harborstack。
- 已准备 harbor 工作区（`paths.harbor_repo`），并能运行：

  ```bash
  cd /path/to/harbor
  .venv/bin/harbor --version
  ```

- 已设置模型凭据（示例）：

  ```bash
  export DEEPSEEK_API_KEY=...
  ```

- 本地数据集，或可访问的 registry 数据集。
  - `-p /path/to/tasks`：本地 task/数据集目录。
  - `-d org/dataset`：registry 数据集。
- git 是环境前置条件：clone/更新 `harborstack` 和 `harbor`，以及
  使用 registry 数据集都需要它。

## 1. 先 dry-run

```bash
unset DOCKER_HOST
uv run harborstack run --dry-run -- run \
  -p /path/to/tasks/TASK_NAME \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker \
  -n 1 -y
```

只校验参数、路径和任务元数据，不启动评测。

## 2. 运行评测

### 2.1 推荐入口：`harborstack run`

```bash
unset DOCKER_HOST
cd /path/to/harborstack

uv run harborstack run -- run \
  -p /path/to/tasks/TASK_NAME \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker \
  -n 1 -y \
  --job-name my-run-01
```

产物会自动落到：

```text
var/runs/<时间戳>-<随机>/
```

跑多个任务时，先 `-n 1` 验证一个；稳定后再增加并发或任务数。

### 2.2 备选入口：直接调用 harbor

如果不想用包装器：

```bash
unset DOCKER_HOST
cd /path/to/harbor

.venv/bin/harbor run \
  -p /path/to/tasks/TASK_NAME \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker \
  -n 1 -y \
  --job-name my-run-01 \
  -o /path/to/jobs
```

推荐用 `-o` 指定一个明确的 job 目录，后面 `display` 时直接传它。

### 2.3 网络较慢时

如果 agent 安装在 `uv sync` 阶段超时，可以给本次运行指定可达的 PyPI 镜像：

```bash
uv run harborstack run -- run \
  -p /path/to/tasks/TASK_NAME \
  -a jiuwenswarm \
  -m deepseek/deepseek-flash \
  -e docker -n 1 -y \
  --ae UV_DEFAULT_INDEX=https://pypi.example.com/simple  # 换成你的可达镜像
```

镜像地址按你的网络环境选择，不要写死到通用配置里。`--ae` 主要覆盖
agent 安装/运行；如果 verifier 的 `uvx` 也慢，在任务 `task.toml` 的
`[environment.env]` 里设置同名变量，或使用预热缓存/镜像。

```toml
[environment.env]
UV_DEFAULT_INDEX = "https://pypi.example.com/simple"
```

改动任务配置会影响该任务的环境变量；如果你在维护原始数据集，建议在副本上做。

## 3. 导入 Langfuse

### 用包装器运行时

```bash
uv run harborstack display var/runs/RUN_ID  # 换成实际目录名
```

### 直接调用 harbor 时

```bash
uv run harborstack display /path/to/jobs/JOB_NAME  # 换成实际 job 目录
```

`display` 只通过 Langfuse HTTP 端口导入，不依赖 Langfuse 是用 dind 还是
宿主机 Docker 启动的。

重复导入会检测到 trace 已存在并显示 `[skip]`，这是正常幂等行为。

## 4. 看结果

最终结果以 verifier 为准，不以 agent 自述为准：

```text
var/runs/<id>/<trial>/verifier/reward.txt
var/runs/<id>/<trial>/verifier/reward.json
var/runs/<id>/<trial>/verifier/test-stdout.txt
var/runs/<id>/<trial>/verifier/ctrf.json
```

agent 的轨迹：

```text
var/runs/<id>/<trial>/agent/trajectory.json
```

如果 agent 说自己完成、但 reward 为 0，优先看 `test-stdout.txt` 和
`ctrf.json`。

## 5. 高级场景：把评测放进 dind

不推荐作为默认路径。如果确实需要：

1. 把任务镜像加载进 dind；
2. 解决 agent 安装和 verifier 的 PyPI/GitHub 网络；
3. 再设置 `DOCKER_HOST=tcp://127.0.0.1:23750` 运行评测。

这属于高级配置，不在傻瓜指南范围内；遇到问题先看 [faq.md](faq.md)。
