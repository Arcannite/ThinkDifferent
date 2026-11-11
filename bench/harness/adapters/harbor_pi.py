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
STATE_DIR = "/opt/pi-agent-dir"


class Pi(HarborPi):
    """harbor's pi agent, registry-aware and bundle-first."""

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
            return

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

    @override
    def get_version_command(self) -> str | None:
        # No nvm in the bundle.
        return "pi --version"

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

        escaped_instruction = shlex.quote(instruction)
        model_args = f"--provider {shlex.quote(provider)} --model {shlex.quote(model_id)} "

        cli_flags = self.build_cli_flags()
        if cli_flags:
            cli_flags += " "
        resume_flag = "--continue " if self._resume else ""

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command)

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