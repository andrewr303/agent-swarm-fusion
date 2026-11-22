"""Observed workspace baselines; cooperative scope checks, not a sandbox."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .mailbox import BoardError
from .sidekick import scoped_path

_DARWIN_LIBC = None


def _get_darwin_libc():
    global _DARWIN_LIBC
    if _DARWIN_LIBC is None and sys.platform == "darwin":
        try:
            libc = ctypes.CDLL("libSystem.B.dylib", use_errno=True)
            libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
            libc.clonefile.restype = ctypes.c_int
            _DARWIN_LIBC = libc
        except Exception:
            _DARWIN_LIBC = False
    return _DARWIN_LIBC or None


def _clonefile_native(source: Path, target: Path) -> bool:
    """Attempt macOS APFS clonefile(2) on a file or directory."""
    libc = _get_darwin_libc()
    if libc is None:
        return False
    try:
        rc = libc.clonefile(os.fsencode(str(source)), os.fsencode(str(target)), 0)
        return rc == 0
    except Exception:
        return False


def _copy_or_clone_file(source: Path, target: Path) -> bool:
    """Copy a single file using APFS clonefile(2) when available, else shutil.copy2."""
    if _clonefile_native(source, target):
        return True
    shutil.copy2(source, target)
    return False


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def within(path: str, scope: str) -> bool:
    return scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")


def git_identity(root: Path) -> dict:
    if not (root / ".git").exists() or not shutil.which("git"):
        return {}
    def output(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.stdout.strip() if result.returncode == 0 else ""
    index_name = output("rev-parse", "--git-path", "index")
    index = Path(index_name) if Path(index_name).is_absolute() else root / index_name
    return {"head": output("rev-parse", "--verify", "HEAD"), "branch": output("symbolic-ref", "--quiet", "HEAD"),
            "index": file_digest(index) if index.is_file() else ""}


def snapshot(workspace: str, exclusions: list[str] | None = None) -> dict:
    root = scoped_path(workspace, ".")
    excluded = [Path(item).resolve() for item in exclusions or []]
    def ignored(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == item or item in resolved.parents for item in excluded)
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if name != ".git" and not ignored(parent / name))
        for name in list(dirs) + sorted(names):
            path = parent / name
            if name == ".git" or ignored(path):
                continue
            relative = path.relative_to(root).as_posix()
            is_link = path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
            if is_link:
                files[relative] = {"kind": "link", "target": str(path.resolve())}
                if name in dirs:
                    dirs.remove(name)
            elif path.is_file():
                files[relative] = {"kind": "file", "sha256": file_digest(path)}
            elif path.is_dir():
                files[relative] = {"kind": "directory"}
    return {"schema_version": 1, "workspace": str(root), "files": files,
            "git": git_identity(root), "exclusions": [str(path) for path in excluded]}


def inspect_changes(baseline: dict, allowed_writes: list[str], *, other_owned_scopes: list[str] | None = None) -> dict:
    if baseline.get("schema_version") != 1:
        raise BoardError("unsupported workspace baseline")
    root = scoped_path(baseline["workspace"], ".")
    permitted = [scoped_path(str(root), item).relative_to(root).as_posix() for item in allowed_writes]
    others = [scoped_path(str(root), item).relative_to(root).as_posix() for item in other_owned_scopes or []]
    current = snapshot(str(root), baseline.get("exclusions", []))
    changed = sorted(path for path in set(baseline["files"]) | set(current["files"])
                     if baseline["files"].get(path) != current["files"].get(path))
    own, shared, violations = [], [], []
    for path in changed:
        record = current["files"].get(path, {})
        if record.get("kind") == "link":
            try:
                scoped_path(str(root), path)
            except BoardError:
                violations.append(path)
                continue
        if any(within(path, scope) for scope in permitted):
            own.append(path)
        elif any(within(path, scope) for scope in others):
            shared.append(path)
        else:
            # A newly created ancestor directory of a permitted file is expected.
            if record.get("kind") == "directory" and any(within(scope, path) for scope in permitted + others):
                continue
            violations.append(path)
    if baseline.get("git", {}) != current["git"]:
        violations.append(".git: HEAD/branch/index changed")
    return {"changed_paths": own, "shared_unattributed": shared, "violations": violations,
            "clean_scope": not violations, "current": current}


def provision_directory(workspace: str, destination: str, *, authorized: bool = False,
                        exclusions: list[str] | None = None) -> dict:
    if not authorized:
        raise BoardError("isolated directory provisioning needs explicit authorization")
    source = scoped_path(workspace, ".")
    target = Path(destination).resolve(strict=False)
    if target.exists() or source == target or source in target.parents or target in source.parents:
        raise BoardError("isolation destination must be new and separate from its source")
    before = snapshot(str(source), exclusions)
    if any(metadata["kind"] == "link" for metadata in before["files"].values()):
        raise BoardError("linked workspace entries need an explicit provisioning policy")
    target.mkdir(parents=True)
    cloned_any = False
    for name, metadata in before["files"].items():
        original, copied = source / name, target / name
        if metadata["kind"] == "link":
            raise BoardError("linked workspace entries need an explicit provisioning policy")
        if metadata["kind"] == "directory":
            copied.mkdir(parents=True, exist_ok=True)
        else:
            copied.parent.mkdir(parents=True, exist_ok=True)
            if _copy_or_clone_file(original, copied):
                cloned_any = True
    return {"mode": "directory", "clone_mechanism": "apfs-clone" if cloned_any else "copy",
            "source": str(source), "workspace": str(target), "source_baseline": before,
            "provisioned_baseline": snapshot(str(target)), "git_publication": False}


def provision_clone(workspace: str, destination: str, *, authorized: bool = False,
                    exclusions: list[str] | None = None) -> dict:
    """Provision an APFS Copy-on-Write clone workspace (clonefile(2) / cp -cR with copy fallback)."""
    if not authorized:
        raise BoardError("APFS clone provisioning needs explicit authorization")
    source = scoped_path(workspace, ".")
    target = Path(destination).resolve(strict=False)
    if target.exists() or source == target or source in target.parents or target in source.parents:
        raise BoardError("isolation destination must be new and separate from its source")
    before = snapshot(str(source), exclusions)
    if any(metadata["kind"] == "link" for metadata in before["files"].values()):
        raise BoardError("linked workspace entries need an explicit provisioning policy")
    target.mkdir(parents=True)
    cloned_any = False
    for name, metadata in before["files"].items():
        original, copied = source / name, target / name
        if metadata["kind"] == "link":
            raise BoardError("linked workspace entries need an explicit provisioning policy")
        if metadata["kind"] == "directory":
            copied.mkdir(parents=True, exist_ok=True)
        else:
            copied.parent.mkdir(parents=True, exist_ok=True)
            if _copy_or_clone_file(original, copied):
                cloned_any = True
    return {
        "mode": "apfs-clone",
        "clone_mechanism": "apfs-clone" if cloned_any else "copy",
        "source": str(source),
        "workspace": str(target),
        "source_baseline": before,
        "provisioned_baseline": snapshot(str(target)),
        "git_publication": False,
    }


def integrate_directory(provision: dict, changed_paths: list[str], *, accepted: bool = False) -> dict:
    if not accepted:
        raise BoardError("only accepted artifacts may be integrated")
    source = scoped_path(provision["source"], ".")
    worker = scoped_path(provision["workspace"], ".")
    baseline = provision["source_baseline"]
    current = snapshot(str(source), baseline.get("exclusions", []))
    paths = [scoped_path(str(source), item).relative_to(source).as_posix() for item in changed_paths]
    conflicts = [name for name in paths if current["files"].get(name) != baseline["files"].get(name)]
    if conflicts:
        raise BoardError("integration conflicts with changes since provisioning: " + ", ".join(conflicts))
    if any(scoped_path(str(worker), name).is_dir() or (source / name).is_dir() for name in paths):
        raise BoardError("integration accepts concrete files, not directory replacement")
    for name in paths:
        original, candidate = source / name, scoped_path(str(worker), name)
        if candidate.is_dir() or original.is_dir():
            raise BoardError("integration accepts concrete files, not directory replacement")
        if candidate.exists():
            original.parent.mkdir(parents=True, exist_ok=True)
            _copy_or_clone_file(candidate, original)
        elif original.exists():
            original.unlink()
    return {"integrated": paths, "workspace": str(source)}


def provision_worktree(workspace: str, destination: str, ref: str, *, authorized: bool = False,
                       provision_paths: list[str] | None = None) -> dict:
    """Explicit detached worktree; no branch, commit, staging or publication."""
    if not authorized or not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise BoardError("worktree provisioning needs explicit authorization and a selected ref")
    source, target = scoped_path(workspace, "."), Path(destination).resolve(strict=False)
    if not (source / ".git").exists() or not shutil.which("git"):
        raise BoardError("Git worktree capability is unavailable for this workspace")
    if target.exists() or source == target or source in target.parents or target in source.parents:
        raise BoardError("worktree destination must be new and separate")
    selected = [scoped_path(str(source), path) for path in provision_paths or []]
    before = snapshot(str(source))
    if any(record["kind"] == "link" for path, record in before["files"].items()
           if any(within(path, item.relative_to(source).as_posix()) for item in selected)):
        raise BoardError("linked provisioning paths are unsupported")
    result = subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach", str(target), ref],
                            capture_output=True, text=True, timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise BoardError("worktree provisioning failed: " + result.stderr.strip())
    for item in selected:
        copied = target / item.relative_to(source)
        if item.is_dir():
            shutil.copytree(item, copied, dirs_exist_ok=True)
        elif item.is_file():
            copied.parent.mkdir(parents=True, exist_ok=True)
            _copy_or_clone_file(item, copied)
    return {"mode": "git-worktree", "source": str(source), "workspace": str(target), "ref": ref,
            "source_baseline": before, "provisioned_baseline": snapshot(str(target)), "git_publication": False}


def check_merge_tree(workspace: str, base_ref: str, ref_a: str, ref_b: str) -> dict:
    """Non-destructive 3-way conflict preflight before synthesis.

    Supports both Git refs via ``git merge-tree --write-tree --name-only --merge-base``
    (never mutating ``.git/info/attributes`` or the working tree) and directory paths
    for APFS-cloned or directory-isolated candidate workspaces.
    """
    root = scoped_path(workspace, ".")
    path_a = Path(ref_a).resolve(strict=False) if os.path.sep in ref_a or Path(ref_a).is_dir() else None
    path_b = Path(ref_b).resolve(strict=False) if os.path.sep in ref_b or Path(ref_b).is_dir() else None
    if path_a and path_b and path_a.is_dir() and path_b.is_dir():
        base_dir = Path(base_ref).resolve(strict=False) if (os.path.sep in base_ref or Path(base_ref).is_dir()) else root
        snap_base = snapshot(str(base_dir))["files"]
        snap_a = snapshot(str(path_a))["files"]
        snap_b = snapshot(str(path_b))["files"]
        changed_a = {p for p in (set(snap_base) | set(snap_a)) if snap_base.get(p) != snap_a.get(p)}
        changed_b = {p for p in (set(snap_base) | set(snap_b)) if snap_base.get(p) != snap_b.get(p)}
        conflicts = sorted(p for p in (changed_a & changed_b) if snap_a.get(p) != snap_b.get(p))
        return {
            "clean": not conflicts,
            "conflicts": conflicts,
            "changed_a": sorted(changed_a),
            "changed_b": sorted(changed_b),
            "mechanism": "snapshot-3way",
        }
    if not (root / ".git").exists() or not shutil.which("git"):
        raise BoardError("Git merge-tree preflight requires a git repository or candidate directories")
    for ref in (base_ref, ref_a, ref_b):
        if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
            raise BoardError("merge-tree refs must be non-empty ref strings")
    result = subprocess.run(
        ["git", "-C", str(root), "merge-tree", "--write-tree", "--name-only", f"--merge-base={base_ref}", ref_a, ref_b],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    tree_oid = lines[0] if lines and len(lines[0]) >= 40 and " " not in lines[0] else ""
    if result.returncode == 0:
        return {
            "clean": True,
            "tree_oid": tree_oid,
            "conflicts": [],
            "mechanism": "git-merge-tree",
        }
    conflicts = []
    for line in lines[1:]:
        if line.startswith("Auto-merging ") or line.startswith("CONFLICT "):
            continue
        conflicts.append(line)
    return {
        "clean": False,
        "tree_oid": tree_oid,
        "conflicts": sorted(dict.fromkeys(conflicts)),
        "mechanism": "git-merge-tree",
    }
