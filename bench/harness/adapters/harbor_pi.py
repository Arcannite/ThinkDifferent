"""harbor agent: pi, driven by this project's model registry and offline bundle.

Provenance (per the runbook, Phase 4): this is a *copy* of harbor's own
`harbor/agents/installed/pi.py` with two patches, not new integration code:

1. **Credentials.** Upstream forwards API keys only for a hardcoded provider list
   (openai/anthropic/openrouter/...). Our registry addresses providers such as
   `ollama-cloud` and `qianwen`, which upstream silently drops, so every model call fails
   with an auth error. Here the credential name comes from `config/providers.yaml` via
   `harness.adapters.registry` — there is no second list of models or providers.

2. **Install.** Upstream installs Node via nvm (from raw.githubusercontent.com) and pi via
   `npm install -g`. Here the offline bundle at `/opt/pi-bundle` is used when it is
   mounted, which needs no network and pins the Node version that rejects the revoked
   emSign root. Falls back to upstream behaviour when the bundle is absent.

Everything else — the command line, the JSON event stream, the `pi.txt` output file, and
harbor's post-run token accounting — is upstream's, unchanged.

Usage:
    harbor run ... -a harness.adapters.harbor_pi:Pi -m ollama-cloud/glm-5.3
    (mounted with: --mounts '[{"type":"bind","source":"<root>/harness/bundle",
                               "target":"/opt/pi-bundle","read_only":true}]')
"""

from __future__ import annotations

import os
import shlex
from typing import override

from harbor.agents.installed.base import with_prompt_template
from harbor.agents.installed.pi import Pi as HarborPi
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harness.adapters.registry import auth_env_for

BUNDLE_MOUNT = os.environ.get("BENCH_BUNDLE_MOUNT", "/opt/pi-bundle")
FSTRACK_MOUNT = os.environ.get("BENCH_FSTRACK_MOUNT", "/opt/fstrack")
PI_EXT_MOUNT = os.environ.get("BENCH_PI_EXT_MOUNT", "/opt/pi-extensions")
FSTRACK_OUT = os.environ.get("BENCH_FSTRACK_OUT", "/logs/artifacts/fstrack")
STATE_DIR = "/opt/pi-agent-dir"


class Pi(HarborPi):
    """harbor's pi agent, registry-aware, bundle-first, fstrack-instrumented."""

    # ---------------------------------------------------------------- install

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        probe = await self.exec_as_root(
            environment,
            command=f"test -f {shlex.quote(BUNDLE_MOUNT)}/install.sh && echo BUNDLE || echo NOBUNDLE",
        )
        if "BUNDLE" not in (getattr(probe, "stdout", "") or ""):
            # No bundle mounted: upstream behaviour (needs egress at build time).
            await super().install(environment)
        else:
            # Offline: node + pi + uv from the bundle. Runs with the network untouched.
            await self.exec_as_root(
                environment,
                command=f"bash {shlex.quote(BUNDLE_MOUNT)}/install.sh",
            )
            # Pi state. Do not rely on $HOME here: exec_as_agent does not always give the same
            # HOME that the agent process later sees, and a task whose user differs from the
            # install user then silently loses models.json ("Unknown provider").
            # PI_CODING_AGENT_DIR pins it for both, and is exported in run().
            await self.exec_as_root(
                environment,
                command=(
                    f"mkdir -p {STATE_DIR} && "
                    f"cp {shlex.quote(BUNDLE_MOUNT)}/models.json {STATE_DIR}/models.json && "
                    f"cp {shlex.quote(BUNDLE_MOUNT)}/settings.json {STATE_DIR}/settings.json && "
                    f"chmod -R 0777 {STATE_DIR}"
                ),
            )
            # Best-effort per-user copy as well, for anything that ignores the override.
            await self.exec_as_agent(
                environment,
                command=(
                    'mkdir -p "$HOME/.pi/agent" && '
                    f'cp {STATE_DIR}/models.json "$HOME/.pi/agent/models.json" 2>/dev/null || true; '
                    f'cp {STATE_DIR}/settings.json "$HOME/.pi/agent/settings.json" 2>/dev/null || true; '
                    "pi --version"
                ),
            )

        # Filesystem journal: git + python + the pi event extension. Runs in both
        # bundle and non-bundle modes; failures are non-fatal (tracking is
        # observability, not a precondition for the task).
        await self._install_fstrack(environment)

    async def _install_fstrack(self, environment: BaseEnvironment) -> None:
        probe = await self.exec_as_root(
            environment,
            command=(
                f"test -f {shlex.quote(FSTRACK_MOUNT)}/install.sh && echo YES || echo NO"
            ),
        )
        if "YES" not in (getattr(probe, "stdout", "") or ""):
            return
        try:
            await self.exec_as_root(
                environment,
                command=f"bash {shlex.quote(FSTRACK_MOUNT)}/install.sh",
                env={"FSTRACK_OUT": FSTRACK_OUT, "DEBIAN_FRONTEND": "noninteractive"},
                timeout_sec=600,
            )
            await self.exec_as_root(
                environment,
                command=(
                    f"mkdir -p {STATE_DIR}/extensions && "
                    f"if [ -f {shlex.quote(PI_EXT_MOUNT)}/fstrack.ts ]; then "
                    f"cp {shlex.quote(PI_EXT_MOUNT)}/fstrack.ts {STATE_DIR}/extensions/fstrack.ts; "
                    f"fi; "
                    f"chmod -R 0777 {STATE_DIR} {FSTRACK_OUT} 2>/dev/null || true"
                ),
                timeout_sec=60,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the trial for tracking
            print(f"fstrack: install skipped ({type(exc).__name__}: {exc})", flush=True)

    # ---------------------------------------------------------------- fstrack

    @override
    def get_version_command(self) -> str | None:
        # No nvm in the bundle.
        return "pi --version"

    async def _start_fstrack(self, environment: BaseEnvironment) -> bool:
        probe = await self.exec_as_root(
            environment,
            command=(
                "command -v fstrack-python >/dev/null 2>&1 && "
                f"test -f {shlex.quote(FSTRACK_MOUNT)}/fstrackd.py && echo YES || echo NO"
            ),
        )
        if "YES" not in (getattr(probe, "stdout", "") or ""):
            print("fstrack: not available in this image; continuing without it", flush=True)
            return False

        # Kill a previous daemon (resume path) so the base commit is fresh.
        await self.exec_as_root(
            environment,
            command=(
                f"if [ -f {FSTRACK_OUT}/daemon.pid ]; then "
                f"kill -TERM $(cat {FSTRACK_OUT}/daemon.pid) 2>/dev/null || true; "
                f"sleep 0.5; fi"
            ),
            timeout_sec=30,
        )
        cmd = (
            f"mkdir -p {FSTRACK_OUT} && chmod 0777 {FSTRACK_OUT} && "
            f": > {FSTRACK_OUT}/events.jsonl && chmod 0666 {FSTRACK_OUT}/events.jsonl && "
            f"rm -f {FSTRACK_OUT}/daemon.pid && "
            f"if command -v setsid >/dev/null 2>&1; then "
            f"nohup setsid fstrack-python {shlex.quote(FSTRACK_MOUNT)}/fstrackd.py "
            f">{FSTRACK_OUT}/daemon.log 2>&1 </dev/null & "
            f"else "
            f"nohup fstrack-python {shlex.quote(FSTRACK_MOUNT)}/fstrackd.py "
            f">{FSTRACK_OUT}/daemon.log 2>&1 </dev/null & "
            f"fi; "
            f"echo $! > {FSTRACK_OUT}/daemon.pid"
        )
        await self.exec_as_root(
            environment,
            command=cmd,
            env={
                "FSTRACK_OUT": FSTRACK_OUT,
                "FSTRACK_ROOTS": os.environ.get("BENCH_FSTRACK_ROOTS", "/app"),
                "FSTRACK_SCAN_INTERVAL": os.environ.get(
                    "BENCH_FSTRACK_SCAN_INTERVAL", "1.0"
                ),
                "FSTRACK_EXCLUDE": os.environ.get("BENCH_FSTRACK_EXCLUDE", ""),
                "FSTRACK_MAX_FILE_MB": os.environ.get(
                    "BENCH_FSTRACK_MAX_FILE_MB", "8"
                ),
                "FSTRACK_LARGE_MODE": os.environ.get(
                    "BENCH_FSTRACK_LARGE_MODE", "manifest"
                ),
                "FSTRACK_MAX_REPO_MB": os.environ.get(
                    "BENCH_FSTRACK_MAX_REPO_MB", "256"
                ),
                "FSTRACK_HASH_LARGE": os.environ.get(
                    "BENCH_FSTRACK_HASH_LARGE", "0"
                ),
                "FSTRACK_GIT_TIMEOUT": os.environ.get(
                    "BENCH_FSTRACK_GIT_TIMEOUT", "120"
                ),
            },
            timeout_sec=60,
        )
        # Wait for the base commit so the journal exists before the agent can write.
        await self.exec_as_root(
            environment,
            command=(
                f"for i in $(seq 1 100); do "
                f"test -f {FSTRACK_OUT}/summary.json && break; sleep 0.1; done; "
                f"cat {FSTRACK_OUT}/daemon.pid 2>/dev/null || true"
            ),
            timeout_sec=30,
        )
        return True

    async def _stop_fstrack(self, environment: BaseEnvironment) -> None:
        cmd = (
            f"if [ -f {FSTRACK_OUT}/daemon.pid ]; then "
            f"pid=$(cat {FSTRACK_OUT}/daemon.pid); "
            f"kill -TERM \"$pid\" 2>/dev/null || true; "
            f"for i in $(seq 1 100); do kill -0 \"$pid\" 2>/dev/null || break; sleep 0.1; done; "
            f"fi"
        )
        try:
            await self.exec_as_root(environment, command=cmd, timeout_sec=30)
        except Exception as exc:  # noqa: BLE001
            print(f"fstrack: stop skipped ({type(exc).__name__}: {exc})", flush=True)

    # ---------------------------------------------------------------- run

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")

        provider, model_id = self.model_name.split("/", 1)

        env: dict[str, str] = {}
        key_name = auth_env_for(provider)  # raises for unknown providers
        if key_name and os.environ.get(key_name):
            env[key_name] = os.environ[key_name]
        # The bundle installs a fixed toolchain; keep pi offline.
        env["PI_OFFLINE"] = "1"
        env["PI_TELEMETRY"] = "0"
        env["PI_CODING_AGENT_DIR"] = STATE_DIR
        env["FSTRACK_OUT"] = FSTRACK_OUT

        escaped_instruction = shlex.quote(instruction)
        model_args = f"--provider {shlex.quote(provider)} --model {shlex.quote(model_id)} "

        cli_flags = self.build_cli_flags()
        if cli_flags:
            cli_flags += " "
        resume_flag = "--continue " if self._resume else ""

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command)

        fstrack = await self._start_fstrack(environment)
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    "pi --print --mode json --session-dir /logs/agent/pi/sessions "
                    f"{resume_flag}"
                    f"{model_args}"
                    f"{cli_flags}"
                    f"{escaped_instruction} "
                    f'2>&1 </dev/null | grep -v \'"type":"message_update"\' | stdbuf -oL tee /logs/agent/{self._OUTPUT_FILENAME}'
                ),
                env=env,
            )
        finally:
            if fstrack:
                await self._stop_fstrack(environment)