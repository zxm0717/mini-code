"""Checkpoint management — git-native file snapshots + JSON message snapshots.

Architecture:
- Message history → JSON files in ~/.mini-claude/checkpoints/<session_id>/
- File snapshots → isolated private git repo (does NOT touch user's .git)
- Atomic rollback: backup current state → restore files → restore messages
  Any failure triggers full restore from backup (all-or-nothing).
- Only tracks files that the Agent has actually modified (write_file / edit_file).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent

CHECKPOINT_DIR = Path.home() / ".mini-claude" / "checkpoints"


def _ensure_dir() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


def _session_dir(session_id: str) -> Path:
    _ensure_dir()
    d = CHECKPOINT_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Private git repo ─────────────────────────────────────────

def _git_env(session_id: str) -> dict[str, str]:
    """Return env dict that redirects all git ops to the private checkpoint repo.

    GIT_DIR     → ~/.mini-claude/checkpoints/<session>/repo/.git
    GIT_WORK_TREE → the project root (cwd)

    The user's .git is never read or written by checkpoint operations.
    """
    repo_dir = _session_dir(session_id) / "repo"
    return {
        "GIT_DIR": str(repo_dir / ".git"),
        "GIT_WORK_TREE": str(Path.cwd()),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
    }


def _init_repo(session_id: str) -> None:
    """Initialise the private checkpoint git repo (idempotent)."""
    repo_dir = _session_dir(session_id) / "repo"
    git_dir = repo_dir / ".git"
    if git_dir.exists():
        return

    repo_dir.mkdir(parents=True, exist_ok=True)
    env = _git_env(session_id)
    subprocess.run(["git", "init", "-q"], env=env, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "mini-claude-checkpoint"],
        env=env, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "checkpoint@mini-claude.local"],
        env=env, check=True, capture_output=True,
    )


def _git_commit(
    session_id: str,
    files: set[str],
    message: str,
) -> str | None:
    """Stage tracked files and commit. Returns short commit hash or None."""
    _init_repo(session_id)
    env = _git_env(session_id)

    # Stage only agent-modified files that exist on disk
    for f in sorted(files):
        if os.path.exists(f):
            subprocess.run(
                ["git", "add", "--", f],
                env=env, capture_output=True,
            )

    # Commit (allow-empty handles the case where no tracked files changed yet)
    result = subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", message],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None

    # Get short hash
    hash_result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        env=env, capture_output=True, text=True,
    )
    return hash_result.stdout.strip() if hash_result.returncode == 0 else None


# ─── Create ────────────────────────────────────────────────────

def create_checkpoint(
    agent: "Agent",
    label: str | None = None,
) -> str:
    """Create a checkpoint.

    1. git commit all Agent-modified files in the private checkpoint repo.
    2. Save message snapshot as JSON metadata alongside.

    Returns 8-char hex checkpoint_id.
    """
    checkpoint_id = uuid.uuid4().hex[:8]
    session_dir = _session_dir(agent.session_id)

    # Git commit tracked files
    commit_msg = label or f"checkpoint-{checkpoint_id}"
    commit_hash = _git_commit(
        agent.session_id,
        agent._agent_modified_files,
        commit_msg,
    )

    # Save message snapshot as JSON metadata
    data: dict[str, Any] = {
        "id": checkpoint_id,
        "label": label,
        "commit": commit_hash,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "turn_number": agent.current_turns,
        "message_snapshot": {
            "anthropic_messages": (
                list(agent._anthropic_messages) if not agent.use_openai else None
            ),
            "openai_messages": (
                list(agent._openai_messages) if agent.use_openai else None
            ),
        },
        "tracked_files": sorted(agent._agent_modified_files),
    }

    (session_dir / f"{checkpoint_id}.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )

    return checkpoint_id


# ─── Auto checkpoint ────────────────────────────────────────────

def auto_create_checkpoint(
    agent: "Agent",
    tool_name: str,
    before_destructive: bool = False,
) -> str | None:
    """Auto-checkpoint before first destructive tool in a turn.

    No rate-limit per turn — the git repo naturally deduplicates
    (if no tracked files changed, git commit is empty).
    """
    if not before_destructive:
        return None
    return create_checkpoint(agent, label=f"auto-before-{tool_name}")


# ─── List & query ───────────────────────────────────────────────

def list_checkpoints(session_id: str) -> list[dict]:
    """Return sorted list of checkpoint metadata."""
    session_dir = _session_dir(session_id)
    results: list[dict] = []
    for f in sorted(session_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            results.append({
                "id": data["id"],
                "label": data.get("label"),
                "commit": data.get("commit", "?"),
                "timestamp": data.get("timestamp", ""),
                "turn_number": data.get("turn_number", 0),
                "tracked_count": len(data.get("tracked_files", [])),
            })
        except Exception:
            pass
    results.sort(key=lambda c: c["turn_number"])
    return results


def get_latest_checkpoint_id(session_id: str) -> str | None:
    checkpoints = list_checkpoints(session_id)
    return checkpoints[-1]["id"] if checkpoints else None


def get_checkpoint(session_id: str, checkpoint_id: str) -> dict | None:
    """Load full checkpoint data from JSON."""
    path = _session_dir(session_id) / f"{checkpoint_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ─── Atomic rollback ────────────────────────────────────────────

def _backup_current_state(agent: "Agent") -> dict:
    """Backup current file state to a temp directory + messages in memory.

    Returns a dict that can be passed to _restore_from_backup().
    """
    backup: dict[str, Any] = {
        "temp_dir": tempfile.mkdtemp(prefix="mini-claude-rollback-"),
        "files": {},
        "anthropic_messages": list(agent._anthropic_messages),
        "openai_messages": list(agent._openai_messages),
    }

    for f in agent._agent_modified_files:
        if os.path.exists(f):
            dest = os.path.join(backup["temp_dir"], f.lstrip(os.sep).replace(os.sep, "_"))
            try:
                shutil.copy2(f, dest)
                backup["files"][f] = dest
            except Exception:
                pass  # file might have been deleted, skip

    return backup


def _restore_from_backup(backup: dict) -> None:
    """Restore files and messages from a backup."""
    for f, src in backup["files"].items():
        try:
            Path(f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, f)
        except Exception:
            pass

    # Cleanup temp dir
    try:
        shutil.rmtree(backup["temp_dir"])
    except Exception:
        pass


def restore_checkpoint(agent: "Agent", checkpoint_id: str) -> None:
    """Atomic rollback to a checkpoint.

    1. Backup current state (files → temp dir, messages → memory copy).
    2. Git checkout tracked files from the checkpoint commit.
    3. Restore message history from JSON.
    4. If any step fails → restore everything from backup (all-or-nothing).
    """
    from .ui import print_info, print_error

    cp = get_checkpoint(agent.session_id, checkpoint_id)
    if cp is None:
        print_error(f"Checkpoint {checkpoint_id} not found.")
        return

    commit = cp.get("commit")
    tracked = cp.get("tracked_files", [])

    if not commit:
        print_error("Checkpoint has no git commit — nothing to rollback.")
        return

    # Step 1: backup current state
    backup = _backup_current_state(agent)

    try:
        # Step 2: restore files from git (checkout tracked paths from commit)
        env = _git_env(agent.session_id)
        _init_repo(agent.session_id)

        if tracked:
            result = subprocess.run(
                ["git", "checkout", commit, "--"] + tracked,
                env=env, capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Git checkout failed: {result.stderr.strip()}")

        # Step 3: restore message history from JSON
        msg_snapshot = cp.get("message_snapshot", {})
        if msg_snapshot.get("anthropic_messages") and not agent.use_openai:
            agent._anthropic_messages = msg_snapshot["anthropic_messages"]
        if msg_snapshot.get("openai_messages") and agent.use_openai:
            agent._openai_messages = msg_snapshot["openai_messages"]
            if agent._openai_messages and agent._openai_messages[0].get("role") == "system":
                agent._openai_messages[0]["content"] = agent._system_prompt

        # Clear the modified-files tracker — rollback reverted all agent changes
        agent._agent_modified_files.clear()

        # Success
        label = cp.get("label")
        label_str = f' "{label}"' if label else ""
        print_info(
            f"Rolled back to checkpoint {checkpoint_id}{label_str}. "
            f"Restored {len(tracked)} file(s)."
        )

    except Exception as exc:
        # Step 4: failure → restore from backup
        print_error(f"Rollback failed: {exc}")
        print_info("Restoring previous state from backup...")
        _restore_from_backup(backup)
        # Restore messages too
        agent._anthropic_messages = backup["anthropic_messages"]
        agent._openai_messages = backup["openai_messages"]
        print_info("Previous state restored. Nothing was changed.")

    else:
        # Success — cleanup temp backup
        try:
            shutil.rmtree(backup["temp_dir"])
        except Exception:
            pass


# ─── Cleanup ────────────────────────────────────────────────────

def _cleanup_session_checkpoints(session_id: str) -> None:
    """Remove all checkpoints (JSON + private git repo) for a session."""
    d = CHECKPOINT_DIR / session_id
    if d.exists():
        shutil.rmtree(d)
