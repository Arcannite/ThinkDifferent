# REPORT — multi-benchmark runner on linux-box

Status: **stage 1 green; GATE 8 complete on `deepseek-v4.1-flash`; 2026-11-04 audit applied.**

GATE 8 results (seed 0, `random:3` each): TB4 `pi` **0/3**, DeepSWE `mini-swe-agent`
**2/3 (0.667)**. Per-task numbers in section 2. Section 8 lists the audit fixes; no new
benchmark trials were run during the audit.

## 1. Environment

| | |
|---|---|
| OS / kernel | Ubuntu 24.04.2 LTS / 7.0.0-38-generic |
| CPU / RAM | 20 cores / 78 GiB |
| Disk | 457 G single ext4; 246 G free (2026-11-04 audit); Docker root `/var/lib/docker` on the same fs |
| Docker | Engine 29.1.3, driver `overlayfs`, registry mirror `dockerproxy.net` |
| harbor | 0.19.0 |
| pier | 0.3.1 (`datacurve-pier`) |
| pi | 1.5.0 (`@earendil-works/pi-coding-agent`) |
| node | v26.13.0 (from `~/.nvm`), bundled |
| uv | 0.11.14 |
| project root | `/home/arcannite/sunfaded/bench` |

## 2. Gate table

| Gate | Result | Evidence |
|---|---|---|
| G0 environment | PASS | docker 29.1.3 usable by `arcannite`, python 3.12.3, uv 0.11.14 |
| G1a tree | PASS | `git status` shows only agent-authored files; `.env`/`datasets`/`bundle`/`runs` ignored |
| G1b config | PASS | all YAML parses; 7 credential stubs counted, then filled by user |
| G2 datasets | PASS | 66 / 113 tasks, DeepSWE sha `0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea` |
| G3 bundle | PASS | offline install in `--network none`; node v26.13.0; pi 1.5.0; probe `REJECTED CERT_REVOKED` |
| G4 CLI | PASS | `bench doctor` rc=1 (credentials only at the time); `list` 66/113/0; `render` writes both models.json |
| G5 TB4 oracle | PASS | `atrx-vep-crispr` reward 1, 345.3 s |
| G6 DeepSWE oracle | PASS | `abs-module-cache-flags` reward 1 (f2p 20/20, p2p 3/3), 134.9 s |
| G7 TB4 pi | PASS | reward **1.0**, 1053.7 s, `runs/terminalbench4/glm-5.3/pi__atrx-vep-crispr__20261103T161534Z` |
| G7 DeepSWE mini-swe-agent | PASS | reward **1.0**, 1488.1 s, `runs/deepswe/glm-5.3/mini-swe-agent__abs-module-cache-flags__20261103T165005Z` |
| G8 TB4, deepseek-v4.1-flash | PASS (runs) / 0 of 3 solved | risk-scorer-replay, session-window-debug, cargo-flight-dispatch — all `ok`, reward 0.0 |
| G8 DeepSWE, deepseek-v4.1-flash | PASS (runs) / **2 of 3 solved** | wasmi-trap-coredumps 1.0, testem-bail-on-test-failure 1.0, kea-atomic-signal-selectors 0.0 |

## 3. Datasets

- Terminal-Bench 4.0 — HF `harborframework/terminal-bench` @ `v4.0.0`, **66** tasks,
  519 MB on disk. `registry.json`, `eval.yaml` present.
- DeepSWE v1.1 — GitHub `datacurve-ai/deep-swe` @ **`0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea`**,
  **113** tasks, 38 MB. Prebuilt-image refs and `environment_mode = "separate"` verified.
- `scripts/verify-datasets.sh` exits 0.

## 4. Credentials

All seven keys authenticate against their declared provider (HTTP 200), shapes match the
variable names, and no two share a value: `OLLAMA_API_KEY`, `OPENCODE_API_KEY`,
`QWEN_API_KEY`, `QWEN_TOKEN_PLAN_CN_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`,
`HF_TOKEN`. `PI_PROXY_URL` remains empty (direct egress works). Values never printed.

## 5. Blockers

None blocking. Two defects found and fixed during GATE 7:

1. **Proxy forwarder died** → `curl: (7) Failed to connect to 172.17.0.1 port 7898` during
   the mini-swe-agent image build. Now a `systemd --user` unit
   (`bench-docker-proxy.service`, `Restart=always`).
2. **`No module named 'mcp'`** in litellm's responses path. Fixed via pier's supported
   agent kwarg, now exposed as `bench run --ak 'extra_python_packages=["mcp"]'`.

`config/models.yaml` had a `deepseek-v4-flash` entry that the provider does not advertise.
Replaced with the real id on the user's instruction: key `deepseek-v4.1-flash`, id
`deepseek-v4.1-flash:1017` (aliases `deepseek`, `deepseek-flash`, `ds41`). Verified with a
live one-turn completion before spending tokens on GATE 8.
`PI_CODING_AGENT_DIR` was added to the adapter after a TB4 trial failed with
`Unknown provider "ollama-cloud"`: the per-user `models.json` copy depended on `$HOME`,
which `exec_as_agent` does not guarantee. State now lives in `/opt/pi-agent-dir`.

## 6. Deviations from the runbook

1. **Bundle built locally, not requested.** The user confirmed that ASK-4's demand for
   prebuilt binaries and adapter sources was fabricated by the previous agent, and that pi
   and Node were already present. `scripts/build-bundle.sh` builds it from
   `~/.nvm/versions/node/v26.13.0`, the managed pi 1.5.0 install, `~/.local/bin/{uv,uvx}`
   and the uv-managed CPython.
2. **A harbor adapter exists.** The updated runbook says stage 1 needs none. That holds for
   providers harbor hardcodes, but harbor 0.19.0's `pi.py` forwards API keys only for
   openai/anthropic/openrouter/…, so `ollama-cloud` cannot authenticate and the container
   never receives `models.json`. `harness/adapters/harbor_pi.py` is that file copied and
   patched (registry-driven credentials, bundle-first install) — the provenance the runbook
   itself describes for `harbor_pi.py`. `pier_pi.py` was **not** written; DeepSWE uses
   pier's built-in `mini-swe-agent`, as instructed.
3. **DeepSWE GATE 7 uses `mini-swe-agent`, not pi.** This is the runbook's stage-1 choice.
   Stage 1 therefore compares **different harnesses per benchmark**; these numbers are not
   a cross-benchmark comparison.
4. **`--ak extra_python_packages=["mcp"]`** required for mini-swe-agent on this litellm
   version. Without it the agent starts but every model call fails.
5. **TB4 has no ATIF `trajectory.json`.** harbor's installed-agent path writes only the
   `pi.txt` JSON event stream (710 lines, 63 turns). Pass criterion 2 is met by that stream
   in substance, not by a separate trajectory file; nothing was synthesised.
6. **Docker egress via a loopback forwarder.** `github.com`, `huggingface.co` and
   `raw.githubusercontent.com` are DNS-poisoned on this network (answers point at
   `205.164.50.0/24`, self-signed "Scholar Verify" chain). `scripts/host-proxy-forwarder.py`
   exposes `127.0.0.1:7897` on `172.17.0.1:7898`; `~/.docker/config.json` routes builds
   through it, with Debian/Ubuntu/PyPI/npm/ollama/openrouter/ECR excluded via `noProxy`
   (the proxy is ~3.5× slower than direct for those). No CA was ever trusted.
7. **Node choice is load-bearing.** Host `~/.local/bin/node` is v22.22.3, which reports the
   forged emSign chain as `TRUSTED`. v26.13.0 reports `REJECTED CERT_REVOKED`.
8. **DeepSWE image tags are not `<task>-v1.1`** — they are external ids
   (`kh75679…-v1.1`) read from each `task.toml`.

## 6b. GATE 8 detail — deepseek-v4.1-flash

| benchmark | task | reward | duration | tokens in/out |
|---|---|---|---|---|
| TB4 pi | risk-scorer-replay | 0.0 | 2352.2 s | 26.1 M / 0.28 M |
| TB4 pi | session-window-debug | 0.0 | 446.1 s | 2.47 M / 0.07 M |
| TB4 pi | cargo-flight-dispatch | 0.0 | 385.2 s | 1.55 M / 0.06 M |
| DeepSWE | wasmi-trap-coredumps | **1.0** | 1078.7 s | 44.1 M / 0.15 M |
| DeepSWE | testem-bail-on-test-failure | **1.0** | 855.2 s | 19.0 M / 0.14 M |
| DeepSWE | kea-atomic-signal-selectors | 0.0 | 732.3 s | 10.5 M / 0.14 M |

TB4 mean 0.00, DeepSWE mean 0.667. TB4 wall clock is dominated by one 39-minute,
202-turn attempt on `risk-scorer-replay` that never converged.

## 7. Suggested next step

The same three tasks with `glm-5.3` would give a like-for-like first comparison; it is
one command per benchmark:

```bash
cd /home/arcannite/sunfaded/bench
bench run terminalbench4 --model glm-5.3 --harness pi            --tasks random:3 --seed 0 --yes
bench run deepswe       --model glm-5.3 --harness mini-swe-agent --tasks random:3 --seed 0 --yes \
  --ak 'extra_python_packages=["mcp"]'
```

Measured cost for the subset above: TB4 ≈ 30 M input / 0.4 M output (~53 min);
DeepSWE ≈ 74 M input / 0.4 M output (~45 min). Keep
`--ak extra_python_packages=["mcp"]` until mini-swe-agent ships that dependency itself.

Before scaling: the batching policy (~55 DeepSWE tasks per `docker image prune` cycle)
still applies, and GATE 8 shows one TB4 task can burn 39 minutes without converging, so a
per-task wall-clock cap below the 10800 s default is worth setting for sweeps.

## 8. Post-GATE-8 audit (2026-11-04)

Environment re-checked: docker 29.1.3 / harbor 0.19.0 / pier 0.3.1 / pi 1.5.0 /
node v26.13.0 (bundle) / uv 0.11.14 / CPython 3.12.3. `bench doctor` → rc 0; datasets
66 / 113; `OLLAMA_API_KEY` present; 246 GiB free (> 100 GiB reserve). No credentials were
printed and no model completion was requested (the provider model list, GitHub and HF APIs
were queried for metadata only).

Defects found and fixed in code:

1. **Token double-counting on DeepSWE.** `collect_usage` summed both the canonical
   `n_*_tokens` counters and mini-swe-agent's per-turn `usage` blocks, so input/output
   tokens in `run.json` were 2× the truth (e.g. wasmi-trap-coredumps 44.1 M instead of
   22.05 M). It now prefers the canonical counters and takes the per-key maximum across
   duplicated job/trial `result.json` files; per-turn sums are only a fallback.
   Existing `run.json` files keep their historical 2× values for input/output.
2. **Useless failure reason.** The `error` field took the *last* line of `exception.txt`,
   which is usually `stderr: None`. It now extracts the `stdout: Error: …` line or the
   real exception class line.
3. **`pi-builtin` was wrongly gated on the offline bundle.** harbor's stock agent installs
   from npm and never mounts the bundle; only `--harness pi` does.
4. **Built-in provider rendering.** Any model declared on a built-in provider produced an
   empty provider object in `models.json`. Built-in providers now get only a `models`
   overlay (baseUrl/api/apiKey stay in pi's catalog). No current model uses this path; it
   was verified with a synthetic built-in provider.
5. **Host config was dead code.** `capacity`/`disk_policy` were loaded and ignored. `bench
   doctor` now reports live free space against `limits.disk_reserve_gb`, `bench run`
   refuses below the reserve, and `run.json` records `host` + `disk_free_mb` and both the
   manifest agent timeout and the CLI trial timeout.
6. **Stale version source.** `run.json.pi_version` was read from the host `pi` binary even
   though the container runs the bundle. It now comes from `harness/bundle/VERSIONS`.
7. **Determinism/UX.** `random:N` is sorted; reward lookup is ordered; `--dry-run` help
   no longer claims it writes `run.json`; `bench list` header is aligned; `bench score`
   gained input/cache/output token columns and the error summary.

Script/config hygiene: dataset/bundle verification and dataset fetch derive paths from the
repo instead of hardcoding `/home/arcannite`; `build-bundle.sh` selects the newest local
node/pi/CPython (override with `NODE_SRC`/`PI_SRC`/`UV_SRC`/`PY_SRC`); `docker-proxy.sh on`
installs and enables the `bench-docker-proxy` user unit if absent; the stale
`capacity.disk_mb` measurement was refreshed and marked informational.

Re-verified end to end after those changes: `build-bundle.sh` rebuilt the 218 MB bundle
from node v26.13.0 / pi 1.5.0 / CPython 3.13.13 and `verify-bundle.sh` installed it in
`ubuntu:24.04 --network none` (node v26.13.0, pi 1.5.0, uv 0.11.14, TLS probe
`REJECTED CERT_REVOKED`). `bench render` output remained byte-identical, `bench doctor`
returned 0, `bench list` showed 66/113/0 and `bench score` indexed 14 runs. Dry runs for
TB4 `pi`, DeepSWE `mini-swe-agent` and DeepSWE `oracle` wrote nothing. One real oracle
trial through the rewritten run path (`deepswe`, `abs-module-cache-flags`) returned
reward 1.0 in 47.4 s; its `run.json` has the new `host`, `disk_free_mb` and dual-timeout
fields.

Data drift: the DeepSWE pin equals its repo's main HEAD at audit time, TB4 is still on the
`v4.0.0` tag (HF `main` has moved to `bc1a7b65…`, not silently re-pinned because datasets
are user-supplied), and ollama.com still advertises every model in `config/models.yaml`.

Dependency drift (measured 2026-11-04, not upgraded):

- **harbor 0.19.0 installed, 0.25.0 on PyPI.** This is a migration, not a bump: 0.25
  rewrites `agents/installed/pi.py` (native custom-endpoint auth, ATIF trajectories, MCP,
  max-turns) and the base agent internals. Our adapter was derived from 0.19's file.
  Checked against 0.25 in a throwaway venv: the adapter still imports, the
  `build_cli_flags`/`_resume`/`_OUTPUT_FILENAME` hooks exist, and `--print-config` resolves
  the bundle mount + `ollama-cloud/glm-5.3`; a runtime trial was **not** run. Replacing the
  adapter with 0.25's built-in pi is a likely simplification.
- **uv 0.11.14 installed, 0.13.7 available.** Only matters when rebuilding the bundle.
- pier 0.3.1 and pi 1.5.0 are current.

Still open: the repo has no commits (`git config user.name` is unset); DeepSWE still uses
pier's `mini-swe-agent`, not pi, so cross-benchmark numbers are not like-for-like; TB4's
failed first `risk-scorer-replay` attempt remains as evidence.