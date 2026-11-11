"""Adapter package: bridges the benchmark engines (harbor, pier) to pi.

Everything here reads model/provider knowledge from `config/models.yaml` and
`config/providers.yaml`. There is no second model list, and adding a model or provider
never requires touching these files.
"""

from .registry import (  # noqa: F401
    auth_env_for,
    bench_root,
    litellm_target,
    provider_entry,
    providers,
)