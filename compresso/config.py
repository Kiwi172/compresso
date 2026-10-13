"""Optional config-file support: TOML or JSON.

Search order: --config path, ~/.config/compresso/config.toml,
~/.compresso/config.json. Config values act as defaults; command-line
flags always win.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def config_path(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    cands = []
    if xx := os.environ.get("COMPRESSO_CONFIG"):
        cands.append(Path(xx))
    cands += [Path.home() / ".config/compresso/config.toml",
              Path.home() / ".compresso/config.json"]
    return next((c for c in cands if c.exists()), None)


def load_config(explicit: str | None = None) -> dict:
    p = config_path(explicit)
    if not p:
        return {}
    try:
        if p.suffix == ".json":
            return json.loads(p.read_text())
        import tomllib
        return tomllib.loads(p.read_text())
    except Exception:
        return {}
