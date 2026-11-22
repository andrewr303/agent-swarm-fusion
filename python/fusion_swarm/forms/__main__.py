"""JSON CLI for the swarm forms. Use FUSION_TEST_HOOKS=1 with FUSION_FORMS_FAKE_DISPATCH
pointing to a JSON fixture to exercise the full lifecycle offline. The fixture maps
provider names to ordered response lists (strings or dispatch objects with optional delay_s).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import signal
import threading
from collections import deque
from pathlib import Path

from . import ALIASES, FORMS, get_form, list_forms
from . import capacity, journal, planner
from .ledger import ReliabilityLedger
from .runtime import Budget, CancelToken, FormContext, FormError, command_verifier, default_dispatch


class UsageError(ValueError):
    pass


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _csv(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def _bool(value):
    if value.lower() in ("1", "true", "yes"):
        return True
    if value.lower() in ("0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite number")
    return number


def _nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def _json(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON at column {exc.colno}") from None
    except ValueError:
        raise argparse.ArgumentTypeError("invalid JSON value") from None


def _option(parser, key, convert):
    option = "--" + key.replace("_", "-")
    group = parser.add_mutually_exclusive_group()
    if convert is not None:
        group.add_argument(option, dest=key, default=argparse.SUPPRESS, type=convert,
                           **({"nargs": "?", "const": True} if convert is _bool else {}))
    group.add_argument(option + "-json", dest=key, default=argparse.SUPPRESS, type=_json)


# Each entry exposes only data accepted by the form. Python callbacks stay in the library API.
OPTIONS = {
    "refute_panel": dict(authors=_csv, refuters=_csv, k=int, max_k=int, candidates=None, task_class=str),
    "tournament": dict(judges=_csv, candidates=None, seed=int, task_class=str),
    "hedged_race": dict(primary=str, backups=_csv, hedge_delay_s=float, max_hedges=int, task_class=str,
                        isolation_dirs=_csv, read_only=_bool, allow_unverified=_bool),
    "mapreduce": dict(shards=None, mappers=_csv, reducer=str, backup_after=float, max_backup_frac=float,
                      min_done_for_median=int, task_class=str),
    "critic_repair": dict(writer=str, critics=_csv, max_rounds=int, patience=int, task_class=str),
    "pair_swap": dict(a=str, b=str, task_class=str, direction=str, use_peers=_bool),
    "delphi": dict(experts=_csv, rounds=int, numeric=_bool, tol=float, seed=int),
    "auction": dict(bidders=_csv, method=str, explore=float, execute=_bool, role=str, self_bids=None),
    "loop_until_dry": dict(finders=_csv, per_round=int, dry_rounds=int, max_rounds=int, scopes=_csv, task_class=str),
    "blackboard": dict(roles=None, max_cycles=int, channel=str),
    "single": dict(provider=str, role=str, task_class=str),
}


def _parser():
    parser = JsonParser(description="Plan, run and recover Fusion swarm forms.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List runtime forms")
    plan = commands.add_parser("plan", help="Choose a form without dispatching a provider")
    plan.add_argument("--task", required=True)
    plan.add_argument("--providers", type=_csv, required=True)
    plan.add_argument("--kind", choices=planner.KINDS)
    plan.add_argument("--read-only", action="store_true", default=None)
    plan.add_argument("--verifier", dest="has_verifier", action="store_true", default=None)
    plan.add_argument("--items", dest="n_items", type=int)
    plan.add_argument("--difficulty", choices=planner.DIFFICULTIES)
    plan.add_argument("--stakes", choices=planner.STAKES)
    plan.add_argument("--latency", dest="latency_sensitive", action="store_true", default=None)
    plan.add_argument("--isolated", action="store_true", default=None)
    plan.add_argument("--heterogeneous", action="store_true", default=None)
    plan.add_argument("--service-s", type=_positive, default=120.0)
    plan.add_argument("--deadline-s", type=_positive)

    run = commands.add_parser("run", help="Run a form and keep its journal")
    forms = run.add_subparsers(dest="form", required=True)
    for name, options in OPTIONS.items():
        aliases = [alias for alias, exact in ALIASES.items() if exact == name]
        dashed = name.replace("_", "-")
        if dashed != name:
            aliases.append(dashed)
        form = forms.add_parser(name, aliases=aliases)
        form.set_defaults(form=name)
        task_group = form.add_mutually_exclusive_group(required=True)
        task_group.add_argument("--task")
        task_group.add_argument("--task-file", type=Path)
        form.add_argument("--providers", type=_csv, default=[])
        form.add_argument("--gate-cmd", help="An executable verification command supplied by the caller")
        form.add_argument("--cwd")
        form.add_argument("--budget-calls", type=_nonnegative_int)
        form.add_argument("--budget-tokens", type=_nonnegative_int)
        form.add_argument("--deadline-s", type=_positive)
        form.add_argument("--run-dir", type=Path)
        _option(form, "timeout", _positive)
        for key, convert in options.items():
            _option(form, key, convert)

    salvage = commands.add_parser("salvage", help="Recover recorded outputs from a run")
    salvage.add_argument("run_dir", type=Path)
    ledger = commands.add_parser("ledger", help="Inspect or record verified outcomes")
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    show = ledger_commands.add_parser("show")
    show.add_argument("--task-class")
    record = ledger_commands.add_parser("record")
    for key in ("worker", "task-class", "role"):
        record.add_argument("--" + key, required=True)
    outcome = record.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--success", dest="success", type=_bool, nargs="?", const=True)
    outcome.add_argument("--failure", dest="success", action="store_false")
    record.add_argument("--verified", action="store_true", required=True)
    record.add_argument("--latency-s", type=_positive)

    sizing = commands.add_parser("capacity", help="Estimate width, captains and time")
    sizing.add_argument("--width", type=_nonnegative_int, required=True)
    sizing.add_argument("--service-s", type=_positive, required=True)
    sizing.add_argument("--spawn-gap-s", type=_positive, default=capacity.SPAWN_GAP_S)
    sizing.add_argument("--tasks", type=_nonnegative_int)
    sizing.add_argument("--deadline-s", type=_positive)
    return parser


def _form_options(args):
    options = {key: vars(args)[key] for key in (*OPTIONS[args.form], "timeout") if key in vars(args)}
    for key, value in options.items():
        convert = OPTIONS[args.form].get(key, _positive)
        if convert is _csv:
            valid = isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
        elif convert is int:
            valid = type(value) is int
        elif convert in (float, _positive):
            valid = type(value) in (int, float) and math.isfinite(value)
            if convert is _positive:
                valid = valid and value > 0
        elif convert is _bool:
            valid = type(value) is bool
        elif convert is str:
            valid = isinstance(value, str)
        else:
            valid = isinstance(value, dict) if key in ("roles", "self_bids") else isinstance(value, list)
        if not valid:
            raise UsageError(f"invalid value for --{key.replace('_', '-')}")
    return options


def _with_provider_defaults(form, options, providers):
    if form == "refute_panel":
        if "candidates" not in options:
            options.setdefault("authors", providers)
        options.setdefault("refuters", providers)
    elif form == "hedged_race":
        options.setdefault("primary", providers[0] if providers else None)
        options.setdefault("backups", [p for p in providers if p != options["primary"]])
    elif form == "mapreduce":
        options.setdefault("mappers", providers)
        options.setdefault("reducer", providers[0] if providers else None)
    elif form == "critic_repair":
        options.setdefault("writer", providers[0] if providers else None)
        options.setdefault("critics", [p for p in providers if p != options["writer"]])
    elif form == "pair_swap":
        for key, index in (("a", 0), ("b", 1)):
            options.setdefault(key, providers[index] if len(providers) > index else None)
    elif form == "blackboard" and providers:
        options.setdefault("roles", {role: providers[i % len(providers)] for i, role in
                                      enumerate(("resolver", "researcher", "decider", "judge"))})
    elif form == "single":
        options.setdefault("provider", providers[0] if providers else None)
    roster_key = {
        "tournament": "providers", "delphi": "experts", "auction": "bidders", "loop_until_dry": "finders",
    }.get(form)
    if roster_key:
        options.setdefault(roster_key, providers)


def _default_calls(form, options, task):
    if form == "refute_panel":
        n = len(options.get("candidates") or options.get("authors") or options["refuters"])
        return 2 + 3 * options.get("max_k", 3) * max(1, n)
    if form == "tournament":
        candidates = options.get("candidates")
        n = len(candidates or options["providers"])
        return (0 if candidates else n) + 3 * max(0, n - 1)
    if form == "hedged_race":
        return 1 + options.get("max_hedges", 2)
    if form == "mapreduce":
        return math.ceil(len(options.get("shards", [])) * 1.1 + 2)
    if form == "critic_repair":
        return 1 + 3 * options.get("max_rounds", 3)
    if form == "delphi":
        return len(options["experts"]) * options.get("rounds", 3)
    if form in ("auction", "pair_swap", "single"):
        return {"auction": len(task), "pair_swap": 4, "single": 1}[form]
    if form == "loop_until_dry":
        return options.get("max_rounds", 6) * (options.get("per_round") or len(options["finders"]))
    return options.get("max_cycles", 12)


def _fake_dispatch():
    path = os.environ.get("FUSION_FORMS_FAKE_DISPATCH")
    if os.environ.get("FUSION_TEST_HOOKS") != "1" or not path:
        return None
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(fixture, dict) or not all(isinstance(responses, list) for responses in fixture.values()):
        raise UsageError("fake dispatch fixture must map each provider to a response list")
    responses = {provider: deque(items) for provider, items in fixture.items()}
    lock = threading.Lock()

    def dispatch(provider, prompt, *, timeout, cancel, **kwargs):
        with lock:
            queue = responses.get(provider)
            response = queue.popleft() if queue else {"ok": False, "status": "error", "output": "fixture exhausted"}
        if isinstance(response, str):
            response = {"output": response}
        if not isinstance(response, dict):
            return {"ok": False, "status": "error", "output": "invalid fixture response"}
        delay = max(0.0, float(response.get("delay_s", 0)))
        if delay and cancel.wait(min(delay, timeout)):
            return {"ok": False, "status": "cancelled"}
        if delay > timeout:
            return {"ok": False, "status": "timeout"}
        return {"ok": True, **response}

    return dispatch


def _prepare_run(args):
    form = args.form
    task = args.task if args.task_file is None else args.task_file.read_text(encoding="utf-8")
    if not task.strip():
        raise UsageError("task must contain text")
    options = _form_options(args)
    _with_provider_defaults(form, options, args.providers)
    if form == "auction":
        if task.lstrip().startswith("["):
            task = json.loads(task)
        else:
            task = [{"id": "task-1", "text": task, "task_class": "general"}]
    if form == "blackboard" and "roles" not in options:
        raise UsageError("provide --roles-json or --providers")
    if form == "hedged_race" and "isolation_dirs" in options:
        directories = [Path(path).resolve(strict=True) for path in options.pop("isolation_dirs")]
        n_attempts = 1 + min(options.get("max_hedges", 2), len(set(options["backups"])))
        if len(set(directories)) != len(directories) or len(directories) < n_attempts or not all(
            path.is_dir() for path in directories
        ):
            raise UsageError("--isolation-dirs requires a distinct directory per possible attempt")
        options["isolate"] = lambda index: str(directories[index])
    return task, options


def _run(args):
    task, options = _prepare_run(args)
    module = None if args.form == "single" else get_form(args.form)
    runner = module.run if module else planner.run_single
    calls = args.budget_calls
    budget = Budget(max_calls=max(0, _default_calls(args.form, options, task)) if calls is None else calls,
                    max_tokens=args.budget_tokens, deadline_s=args.deadline_s)
    verifier = command_verifier(args.gate_cmd, cwd=args.cwd) if args.gate_cmd else None
    dispatch = _fake_dispatch()
    if args.cwd:
        if args.form in ("auction", "blackboard", "critic_repair", "loop_until_dry", "pair_swap", "single"):
            options["cwd"] = args.cwd
        else:
            invoke = dispatch or default_dispatch()

            def dispatch(provider, prompt, *, cwd=None, **kwargs):
                return invoke(provider, prompt, cwd=cwd or args.cwd, **kwargs)
    if args.run_dir is not None:
        args.run_dir.mkdir(parents=True, exist_ok=True)
        if any(args.run_dir.iterdir()):
            raise UsageError("--run-dir must be empty. Use salvage to recover an existing run.")
        with (args.run_dir / "journal.jsonl").open("x", encoding="utf-8"):
            pass

    cancel = CancelToken()
    signal_thread = None

    def on_signal(signum, frame):
        # Cancel outside the signal frame so interruption cannot re-enter an Event wait lock.
        nonlocal signal_thread
        if signal_thread is None:
            signal_thread = threading.Thread(target=cancel.cancel, args=("signal",), daemon=True)
            signal_thread.start()

    old_handlers = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        ctx = FormContext(args.form, run_dir=args.run_dir, budget=budget, cancel=cancel,
                          dispatch_fn=dispatch, verifier=verifier)
        try:
            result = runner(task, ctx=ctx, **options)
        except Exception:
            cancel.cancel("failure")
            ctx.finish("failed", winner=None, candidates=[], rounds=0, stop_reason="error")
            raise
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if signal_thread is not None:
        signal_thread.join()
        return dataclasses.replace(result, status="cancelled", stop_reason="signal").to_dict(), 130
    code = {"failed": 1, "cancelled": 130, "budget_exhausted": 1}.get(result.status, 0)
    return result.to_dict(), code


def _ledger(args):
    ledger = ReliabilityLedger()
    if args.ledger_command == "record":
        cell = ledger.record(args.worker, args.task_class, args.role, args.success,
                             verified=args.verified, latency_s=args.latency_s)
        ledger.save()
        return cell.to_dict()
    if ledger.path.exists():
        doc = json.loads(ledger.path.read_text(encoding="utf-8"))
    else:
        doc = {"version": 1, "cells": [], "pairs": []}
    if args.task_class is not None:
        for key in ("cells", "pairs"):
            doc[key] = [row for row in doc.get(key, []) if row.get("task_class") == args.task_class]
    return doc


def main(argv=None):
    code = 0
    try:
        args = _parser().parse_args(argv)
        if args.command == "list":
            payload = list_forms()
        elif args.command == "plan":
            hints = {key: getattr(args, key) for key in (
                "kind", "read_only", "has_verifier", "n_items", "difficulty", "stakes",
                "latency_sensitive", "isolated", "heterogeneous",
            )}
            shape = planner.classify(args.task, hints=hints)
            plan = planner.choose(shape, providers=args.providers, ledger=ReliabilityLedger(),
                                  service_s=args.service_s, deadline_s=args.deadline_s)
            payload = dataclasses.asdict(plan)
        elif args.command == "run":
            payload, code = _run(args)
        elif args.command == "salvage":
            payload = journal.salvage(args.run_dir)
        elif args.command == "ledger":
            payload = _ledger(args)
        else:
            n = args.width if args.tasks is None else args.tasks
            if n and not args.width:
                raise UsageError("--width must be positive when --tasks is positive")
            payload = capacity.size_width(n, args.service_s, deadline_s=args.deadline_s,
                                          max_width=args.width, spawn_gap_s=args.spawn_gap_s)
    except UsageError as exc:
        payload, code = {"ok": False, "error": "usage", "message": str(exc)}, 2
    except FormError as exc:
        payload, code = {"ok": False, "error": exc.code, "message": exc.message}, 1
    except (OSError, ValueError) as exc:
        payload, code = {"ok": False, "error": "invalid_input", "message": str(exc)}, 1
    except Exception as exc:
        payload, code = {"ok": False, "error": "internal_error", "message": type(exc).__name__}, 1
    print(json.dumps(payload, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
