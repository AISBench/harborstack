# 离线运行时 bundle：打包、归档、恢复

本指南面向“镜像源不可达 / 需要重复部署 / 需要在干净机器上复现”的场景：
把一次跑通的 Langfuse 运行时资源打成包，归档到自己的对象存储，之后直接恢复，
不再依赖上游镜像站。

转换代码（ATIF → OTel、`display` 逻辑）**不在** bundle 里。只改转换代码时，
旧的包仍然可用；只有 Langfuse 版本或架构变化时才需要重打。

## 1. 包里有什么

`--mode full`（推荐）的目录结构：

```text
harborstack-aarch64-runtime/
├── compose/            # 4 个部署资产（3 个 compose + skip-checksum.xml）
├── images.tar          # docker save 的 6 个镜像
├── lock.json           # 镜像 digest、资产指纹、补丁元数据
├── patch/
│   └── clickhouse-<digest>-p1.0.0.bin   # 仅 aarch64 无 RCpc 时需要
└── manifest.json       # 逐文件 size + sha256
```

| 模式 | 适用场景 | 额外内容 |
|---|---|---|
| `skeleton` | 目标机器已有 6 个镜像 | 无 |
| `full` | 目标机器没有镜像（**归档用这一档**） | `images.tar` |
| `offline` | 目标机器连 PyPI 都没有 | 再加 `wheels/` |

`full` 与业务代码解耦、体积可控；`offline` 里的 wheel 会随 harborstack 版本变化，
不适合作为长期归档。

## 2. 打包与校验

前提：本机已经成功执行过 `init`（镜像、补丁产物都在本地）。

```bash
cd /path/to/harbor-otel-stack
.venv/bin/harborstack bundle create \
  --out bundles/harborstack-$(uname -m)-runtime --mode full
.venv/bin/harborstack bundle verify bundles/harborstack-$(uname -m)-runtime
```

预期 `verify` 输出 `[ok  ] bundle` 与文件数：aarch64 需要补丁时是 7 个，x86_64 无补丁时为 6 个。
这个数字只统计写进 `manifest.json` 的文件，不含 `manifest.json` 自身，因此比目录里
`ls` 看到的数量少 1。

> **目录名就是解包后的顶层目录名。** `bundle create` 只生成目录、不打 tar；
> 如果你手工打 tar，请让压缩包名与目录名一致，否则解包后需要 `mv` 一次。
>
> 重复打包前建议先清空 `--out` 目录：`create` 会覆盖同名文件，但不会删除上次遗留的其它文件。

## 3. 命名与归档布局

压缩包与目录同名，并生成校验文件：

```bash
cd bundles
tar --zstd -cf harborstack-$(uname -m)-runtime.tar.zst harborstack-$(uname -m)-runtime
sha256sum harborstack-$(uname -m)-runtime.tar.zst > SHA256SUMS
```

对象存储按“架构 / Langfuse 版本”分层，不要带日期（同一版本重复上传时覆盖即可）：

```text
<bucket>/langfuse/<arch>/<langfuse-version>/
├── harborstack-<arch>-runtime.tar.zst
└── SHA256SUMS
```

例如 aarch64 + Langfuse 4.70.1：

```text
obs://aisbench/langfuse/aarch64/4.70.1/
```

这样后续补 x86_64 或其他版本时不会混在一起。上传以华为云 OBS 为例：

```bash
cd /path/to/obsutil
./obsutil cp /path/to/bundles/harborstack-aarch64-runtime.tar.zst \
  obs://<bucket>/langfuse/aarch64/4.70.1/harborstack-aarch64-runtime.tar.zst -f
./obsutil cp /path/to/bundles/SHA256SUMS \
  obs://<bucket>/langfuse/aarch64/4.70.1/SHA256SUMS -f
```

把 `obsutil` 换成 `aws s3 cp`、`gsutil cp` 或内部制品库命令即可。

## 4. 在干净机器上恢复

```bash
mkdir -p ~/restore/4.70.1
obsutil cp obs://<bucket>/langfuse/aarch64/4.70.1/ ~/restore/4.70.1/ -r -f

cd ~/restore/4.70.1
sha256sum -c SHA256SUMS
tar --zstd -xf harborstack-aarch64-runtime.tar.zst
```

解包后应直接得到 `harborstack-aarch64-runtime/`，不需要改名。再校验一次包内文件：

```bash
cd /path/to/harbor-otel-stack
.venv/bin/harborstack bundle verify ~/restore/4.70.1/harborstack-aarch64-runtime
```

## 5. 部署

### 5.1 宿主机 Docker

```bash
unset DOCKER_HOST
.venv/bin/harborstack install --from-bundle ~/restore/4.70.1/harborstack-aarch64-runtime
.venv/bin/harborstack init
```

`install` 会导入 `images.tar`、把补丁二进制放到缓存并按 `lock.json` 补写同名 `.json` sidecar；
`init` 检测到镜像已就绪就不会再联网拉取。

### 5.2 dind

镜像仍由**宿主机** Docker 准备，再流式导入 dind：

```bash
unset DOCKER_HOST
.venv/bin/harborstack install --from-bundle ~/restore/4.70.1/harborstack-aarch64-runtime
scripts/dind/load-images.sh
scripts/dind/run-init.sh
```

后续步骤与 [dind 部署手册](runbook-langfuse-dind.md) 完全一致。

## 6. 什么时候需要重打

| 变化 | 是否需要重打 |
|---|---|
| 修改转换/导入代码（`src/harborstack/convert`、`display`） | 不需要 |
| 改 README、测试、CLI 文案 | 不需要 |
| 升级 Langfuse / ClickHouse 等镜像版本 | 需要，并更新 `lock.json` |
| 换架构（aarch64 ↔ x86_64） | 需要，分目录归档 |
| 镜像站某个 digest 已下线，但本地已有 | 不需要，包里的 `images.tar` 就是兜底 |

## 7. 常见失败

- `bundle verify` 报 `mismatch` / `missing`：重新下载；仍失败说明归档不完整。
- `install --from-bundle` 后 `init` 报补丁相关 KeyError：确认包内有 `patch/*.bin`；
  sidecar（同名 `.json`）不在包里，由 `install --from-bundle` 依据 `lock.json` 自动补写。
- 解包目录名与文档不一致：手工打包时 tar 顶层目录取自源目录名，改名请重打。
- 目标机器架构不符：`images.tar` 里的镜像与架构绑定，x86_64 不能用 aarch64 的包。
