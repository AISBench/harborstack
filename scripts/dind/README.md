# dind path scripts

These scripts run the Langfuse stack against an isolated Docker-in-Docker daemon.
They are the implementation of the dind path in
[`docs/runbook-langfuse-dind.md`](../../docs/runbook-langfuse-dind.md).

Evaluation is a separate step and defaults to the host Docker daemon; see
[`docs/runbook-eval.md`](../../docs/runbook-eval.md).

## Quick start

见 [docs/quickstart.md](../../docs/quickstart.md)。

最短路径：

```bash
cd ~/code/harbor-otel-stack
scripts/dind/up.sh
```

分步：`setup-stack-venv.sh` → `start-dind.sh` → `install-images.sh` →
`run-init.sh` → `verify.sh`。

`up.sh` 只负责 Langfuse 栈。harbor 工作区是独立的可选步骤：

```bash
scripts/dind/setup-harbor.sh
~/code/harbor/.venv/bin/harbor --version
```

## Environment overrides

| Variable | Default |
|---|---|
| `DIND_NAME` | `harborstack-dind` |
| `DIND_IMAGE` | `docker.m.daocloud.io/library/docker:27.5.1-dind` |
| `DIND_API_PORT` | `23750` |
| `DIND_VOLUME` | `harborstack-dind-data` |
| `DIND_CPUS` | `12` |
| `DIND_MEMORY` | `16g` |
| `DIND_PY_IMAGE` | `docker.m.daocloud.io/library/python:3.13-alpine` |
| `HARBORSTACK_IMAGE__MIRROR` | `docker.m.daocloud.io` |

## Key points

- `watch-and-forward.sh` **must** unset `DOCKER_HOST`: it uses the host daemon
  to inspect the dind container and to start the sidecar; harborstack itself
  uses `DOCKER_HOST` to talk to dind.
- The host loopback forwarders bind only **after** `var/ports.json` is
  rendered, otherwise harborstack's port allocator would see the ports as
  occupied and choose a different set.
- `run-init.sh` stops old forwarders/sidecar before rerunning, so repeated
  `init` runs reuse the same port allocation.
- `verify.sh` waits for web health before `doctor` (its port probe has a 2s
  timeout) and runs `show-trace.py --wait 60`, because Langfuse observations
  become queryable a few seconds after `display`.
- The dind container is `--privileged`; the named volume holds all Langfuse
  data.  `stop-dind.sh --purge` deletes that volume.
- For aarch64 without RCpc, `start-dind.sh` bind-mounts the repo and patch
  cache at their host absolute paths, and `run-init.sh` sets `TMPDIR` under
  the cache, so the ClickHouse patch smoke test and final binary are visible
  to the inner daemon.
