"""Swarm forms registry (spec 8.1). fm-a / fm-b / fm-c add form modules; they never edit this file.

Each form module exposes ``FORM: FormSpec`` and ``run(...) -> FormResult``. Modules are imported lazily so a missing or
broken module never breaks listing the others.
"""
from __future__ import annotations

import dataclasses
import importlib
from types import ModuleType

FORMS: dict[str, str] = {   # name -> module (lazy import); order = listing order
    "refute_panel": "refute_panel",
    "tournament": "tournament",
    "hedged_race": "hedged_race",
    "mapreduce": "mapreduce",
    "critic_repair": "critic_repair",
    "pair_swap": "pair_swap",
    "delphi": "delphi",
    "auction": "auction",
    "loop_until_dry": "loop_until_dry",
    "blackboard": "blackboard",
}
ALIASES: dict[str, str] = {
    "refute": "refute_panel", "hedge": "hedged_race", "race": "hedged_race", "map": "mapreduce",
    "critic": "critic_repair", "pair": "pair_swap", "dry": "loop_until_dry", "bb": "blackboard",
}

__all__ = ["FORMS", "ALIASES", "resolve", "get_form", "list_forms"]


def resolve(name: str) -> str:
    """Canonical form name for ``name`` or an alias (case/dash insensitive). Raises FormError("invalid_input")."""
    key = str(name or "").strip().lower().replace("-", "_")
    key = ALIASES.get(key, key)
    if key not in FORMS:
        from .runtime import FormError
        known = ", ".join(sorted(FORMS))
        raise FormError("invalid_input", f"unknown form {name!r}; known forms: {known}")
    return key


def get_form(name: str) -> ModuleType:
    """Lazily import ``fusion_swarm.forms.<module>``; ImportError propagates when the module is missing."""
    return importlib.import_module(f"{__name__}.{FORMS[resolve(name)]}")


def list_forms() -> list[dict]:
    """[{"name","summary","inputs","stop_rule","aggregation","available"}]; a module that fails to import (or lacks
    ``FORM``) is listed with ``"available": False`` and an ``"error"`` string. Never raises."""
    out: list[dict] = []
    for name in FORMS:
        try:
            module = get_form(name)
            spec = module.FORM
            data = dataclasses.asdict(spec) if dataclasses.is_dataclass(spec) else dict(spec)
            out.append({"name": name, "summary": data.get("summary", ""), "inputs": data.get("inputs", {}),
                        "stop_rule": data.get("stop_rule", ""), "aggregation": data.get("aggregation", ""),
                        "default_budget": data.get("default_budget", {}), "available": True})
        except BaseException as exc:  # noqa: BLE001 - SyntaxError etc. in a half-written form must not break listing
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            out.append({"name": name, "summary": "", "inputs": {}, "stop_rule": "", "aggregation": "",
                        "default_budget": {}, "available": False, "error": f"{type(exc).__name__}: {exc}"})
    return out
