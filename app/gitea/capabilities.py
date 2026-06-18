from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GiteaCapabilities:
    supports_actions_cache: bool = False
