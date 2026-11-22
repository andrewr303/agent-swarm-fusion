"""Credential-free AndrewCode bindings for external orchestration scripts.

The parent runtime validates aliases, credentials and exact efforts. Every
selected subprocess uses AndrewCode again, which validates its loaded catalog.
No third-party CLI or built-in provider/model fallback is used with a selection.
Errors never echo the env value (it may carry private aliases).
"""
from __future__ import annotations

import json
import os

FUSION_MODEL_SELECTION_VERSION = 1
ENV = "FUSION_ANDREWCODE_SELECTION"
EFFORTS = ("off", "on", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_SEATS = ["fable", "codex", "andrewcode", "opencode", "grok", "agy", "copilot", "kimi"]
_ROLES = {"fable": "architect", "codex": "synthesis", "andrewcode": "worker"}


def selection():
    """Parsed and validated selection dict, or None when the env var is unset."""
    raw = os.environ.get(ENV)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("Invalid AndrewCode model selection JSON") from None
    if not isinstance(data, dict) or data.get("version") != FUSION_MODEL_SELECTION_VERSION:
        raise ValueError("Unsupported AndrewCode model selection")
    participants = data.get("participants")
    roles = data.get("roles", {})
    if not isinstance(participants, list) or not 1 <= len(participants) <= 64 or not isinstance(roles, dict):
        raise ValueError("Invalid AndrewCode model selection roster")
    for binding in participants + list(roles.values()):
        if not isinstance(binding, dict) or not isinstance(binding.get("model"), str) or not binding["model"].strip():
            raise ValueError("Invalid selected model alias")
        if binding.get("effort") not in EFFORTS:
            raise ValueError("Invalid selected model effort")
    if any(binding["model"] not in {p["model"] for p in participants} for binding in roles.values()):
        raise ValueError("Role model is outside the selected roster")
    data["roles"] = roles
    return data


def providers():
    """``selected-N`` seat names in order, or None when no selection is set."""
    data = selection()
    return None if data is None else [f"selected-{i + 1}" for i in range(len(data["participants"]))]


def binding_for(provider):
    """``{"model","effort","role"}`` for a seat, or None when no selection is set."""
    data = selection()
    if data is None:
        return None
    roles = data["roles"]
    role = provider if provider in roles else _ROLES.get(provider, provider)
    binding = roles.get(role)
    if binding is None:
        role = ""
        if provider.startswith("selected-"):
            try:
                index = int(provider.removeprefix("selected-")) - 1
            except ValueError:
                raise ValueError("Unknown selected model seat") from None
            if not 0 <= index < len(data["participants"]):
                raise ValueError("Unknown selected model seat")
        else:
            index = _SEATS.index(provider) if provider in _SEATS else 0
        binding = data["participants"][index % len(data["participants"])]
    return {"model": binding["model"], "effort": binding["effort"], "role": role}
