# bench — multi-benchmark runner

One working tree, one model registry, one results directory for:

| Benchmark | Engine | Tasks | Harness |
|---|---|---|---|
| Terminal-Bench 4.0 | `harbor` | 66 | pi (or `oracle`) |
| DeepSWE v1.1 | `pier` | 113 | `mini-swe-agent` (or `oracle`) |
| GDPval-AA | host runner | — | scaffolded, disabled |

Stage 1 harnesses (no custom adapter required for DeepSWE):

| Benchmark | `--harness` | What runs |
|---|---|---|
| TB4 | `pi` | `harness/adapters/harbor_pi.py` + offline bundle (default) |
| TB4 | `pi-builtin` | harbor's stock `-a pi` (network install, hardcoded provider list) |
| TB4 | `oracle` | reference solution, no model |
| DeepSWE | `mini-swe-agent` | pier's built-in agent via litellm (`openai/<id>` + `OPENAI_API_BASE`) |
| DeepSWE | `oracle` | reference solution, no model |

Everything that varies lives in YAML. `bin/bench` contains no model list and no
per-benchmark branching beyond the engine mapping.

## Layout

```
bin/bench                  CLI (Python 3.12, stdlib + PyYAML)
config/models.yaml         THE only place a model is defined
config/providers.yaml      provider base URLs, APIs, auth env vars, builtin flag
config/benchmarks.yaml     which benchmarks are active
config/hosts/*.yaml        machine capacity, paths, proxy, disk policy
benchmarks/<name>/         per-benchmark manifest (engine, dataset path, task count)
datasets/                  USER-SUPPLIED, gitignored (scripts/fetch-datasets.sh)
harness/bundle/            offline pi install, built by scripts/build-bundle.sh, gitignored
harness/adapters/          harbor_pi.py (copy+patch of harbor's pi agent) and registry.py
scripts/                   bundle build/verify, dataset fetch/verify, docker proxy helper
runs/                      ALL results (gitignored)
logs/  work/               scratch (gitignored)
```

## Quick start

```bash
export PATH="$PWD/bin:$PATH"

bench doctor                       # environment + dataset + key + disk status
bench doctor --full                # also install the bundle offline in a container
bench list                         # benchmarks and task counts
bench render                       # models.yaml -> harness/bundle/models.json + ~/.pi/agent/models.json
bench run terminalbench4 --model glm-5.3 --harness oracle --tasks atrx-vep-crispr
bench run deepswe --model glm-5.3 --harness mini-swe-agent --tasks abs-module-cache-flags \
  --ak 'extra_python_packages=["mcp"]'
bench score && column -s, -t runs/_index.csv | head
```

`--harness oracle` needs no `--model` (the directory records `reference`). DeepSWE's
`pi` harness is stage-2 work: use `--harness mini-swe-agent` there.

Typical investigation loop:

```bash
bench run terminalbench4 --model glm-5.3 --harness pi --tasks random:3 --seed 0 --yes
bench score terminalbench4
```

DeepSWE through pier's built-in agent needs one extra kwarg on this litellm version:

```bash
bench run deepswe --model glm-5.3 --harness mini-swe-agent --tasks random:3 --seed 0 --yes \
  --ak 'extra_python_packages=["mcp"]'
```

## Where results land

```
runs/<benchmark>/<model-key>/<harness>__<task>__<UTC timestamp>/
├── run.json          # written before the agent starts, updated when it finishes
├── agent/            # pi.txt (JSON event stream), trajectory.json, ...
└── verifier/         # reward.json or reward.txt, test report
```

harbor writes its job directly into the trial directory. pier adds one job-timestamp
directory of its own (`<trial>/2026-11-04__11-53-33/<task>/...`), so DeepSWE's
`agent/` and `verifier/` are one level deeper. `run.json` is always at the trial root.

`run.json` is the only source of truth for a trial. Anything without one is scratch.
`bench score` flattens every `run.json` into `runs/_index.csv` (reward, duration,
token totals, error summary).

## `bench run` semantics

- `--tasks` is **required**; there is no implicit "all". `random:N` is supported and
  seeded with `--seed`.
- More than one task requires `--yes` — the runbook forbids accidental sweeps.
- `--harness oracle` runs the reference solution: no API key, no model, no pi.
- `--harness pi` and `--harness mini-swe-agent` require the selected provider's key in `.env`.
- Only the selected provider's key is exported to the child process; the rest of the
  environment is not inherited.
- `--dry-run` prints the engine command for each selected task. It writes nothing and
  starts nothing, so nothing appears in `bench score`.
- `--timeout` caps the whole trial process (default 14400 s). The task manifest's
  `agent_timeout_sec` is recorded separately in `run.json.limits`.
- `--ak KEY=VALUE` is forwarded to the engine as `--ak` (repeatable); needed for
  `extra_python_packages=["mcp"]` on mini-swe-agent.
- Before starting, `bench run` checks live free disk on `docker_root` against
  `limits.disk_reserve_gb` and refuses below the reserve.
- Exit codes: `0` all trials ok, `1` at least one trial failed, `2` refused to start.

## `bench doctor` exit codes

| Code | Meaning |
|---|---|
| 0 | everything OK |
| 1 | only credentials missing (`.env` stubs) — `ASK-2` |
| 2 | hard failure (docker, datasets, bundle, adapters, engine, python) |

`bench run` refuses to start on hard failures, but ignores *other* providers' missing
keys; it checks only the key it is about to use.

## Building the offline bundle

```bash
bash scripts/build-bundle.sh      # node/pi/uv/python/wheels -> harness/bundle/
bash scripts/verify-bundle.sh     # installs it in a network-isolated container
```

Sources are the newest local installs: `~/.nvm/versions/node/v*` (must be >= 22.23.4;
v22.22.3 trusts the revoked emSign root), the managed pi install under
`~/.pi/agent/install/releases/`, `~/.local/bin/{uv,uvx}`, and the uv-managed CPython.
Override `NODE_SRC`, `PI_SRC`, `UV_SRC`, `PY_SRC` to reproduce an older bundle.
`verify-bundle.sh` must print `REJECTED CERT_REVOKED`; if it prints `TRUSTED`, stop.

## Adding a benchmark

```bash
cp -r benchmarks/_template benchmarks/mybench
$EDITOR benchmarks/mybench/benchmark.yaml     # name, engine, dataset path, task_count
$EDITOR config/benchmarks.yaml                # add "mybench"
```

No shell change and no Python change are needed. Engines are limited to `harbor`,
`pier`, and `host`; a genuinely new engine is the one case that touches `bin/bench`.

## Adding a model

```yaml
# config/models.yaml
models:
  my-model:
    provider: ollama-cloud     # must exist in config/providers.yaml
    id: vendor/actual-model-id # what the API expects
    reasoning: true
    context: 128000
    max_tokens: 32000
    aliases: [mine]
    cost: {input: 0, output: 0}
```

Then `bench render`. Never add a model anywhere else.

## Disk policy

`config/hosts/linux-box.yaml` sets `disk_policy: batch_and_prune`. TB4 fits in full;
DeepSWE runs as disjoint batches with `docker image prune -a` and
`docker builder prune -af` in between. Each DeepSWE image is used once, so pruning
costs no re-download. `capacity.disk_mb` is a dated measurement; `bench doctor` and
`bench run` read live free space and compare it with `limits.disk_reserve_gb`.

## Network

- Host-side downloads (git / hf / uv) go through `host_proxy` in `config/hosts/linux-box.yaml`
  because `github.com` and `huggingface.co` are MITM'd on this box.
- Docker uses the daemon's registry mirror and reaches `public.ecr.aws` directly. Builds
  that must reach GitHub go through `scripts/docker-proxy.sh on`, which installs/enables a
  `systemd --user` unit for the loopback forwarder (`172.17.0.1:7898`).
- Provider traffic (ollama.com, openrouter.ai, pypi, npm) is direct.