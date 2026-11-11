"""Single accessor for the project's model/provider configuration.

Adapters run inside the engine's process (harbor/pier), so they must find the repo root
without relying on the caller's cwd. `BENCH_ROOT` wins; otherwise derive it from this
file's location: harness/adapters/registry.py -> <root>.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


def bench_root() -> Path:
    env = os.environ.get("BENCH_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def providers() -> dict:
    path = bench_root() / "config" / "providers.yaml"
    with path.open() as fh:
        return (yaml.safe_load(fh) or {}).get("providers") or {}


@lru_cache(maxsize=1)
def models() -> dict:
    path = bench_root() / "config" / "models.yaml"
    with path.open() as fh:
        return (yaml.safe_load(fh) or {}).get("models") or {}


def provider_entry(name: str) -> dict:
    entry = providers().get(name)
    if entry is None:
        known = ", ".join(sorted(providers()))
        raise ValueError(f"unknown provider {name!r}; config/providers.yaml has: {known}")
    return entry


def auth_env_for(provider: str) -> str | None:
    return provider_entry(provider).get("auth_env")


def provider_env(provider: str, environ: dict | None = None) -> dict[str, str]:
    """Resolve the provider's credential from the environment, keyed as the provider wants.

    Returns {} when the key is absent, so a missing key surfaces as a provider error
    rather than a silently empty variable.
    """
    src = os.environ if environ is None else environ
    key = auth_env_for(provider)
    if not key:
        return {}
    value = src.get(key)
    return {key: value} if value else {}


def litellm_target(provider: str, model_id: str) -> tuple[str, dict[str, str]]:
    """Map one of our providers onto a litellm model string (used by pier/mini-swe-agent).

    providers.yaml may declare:

        litellm:
          prefix: openai
          api_key_env: OPENAI_API_KEY
          base_url_env: OPENAI_API_BASE

    The returned env carries the credential and base URL under the names litellm expects.
    """
    entry = provider_entry(provider)
    cfg = entry.get("litellm") or {}
    prefix = cfg.get("prefix")
    if not prefix:
        raise ValueError(
            f"provider {provider!r} has no litellm.prefix in config/providers.yaml; "
            "mini-swe-agent cannot address it"
        )
    env: dict[str, str] = {}
    src_key = entry.get("auth_env")
    if src_key and os.environ.get(src_key):
        env[cfg.get("api_key_env", "OPENAI_API_KEY")] = os.environ[src_key]
    if entry.get("base_url"):
        env[cfg.get("base_url_env", "OPENAI_API_BASE")] = entry["base_url"]
    return f"{prefix}/{model_id}", env