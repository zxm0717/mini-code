"""Checkpoint management — file backup and message history snapshots for rollback.

Copy-on-Write strategy:
- Creating a checkpoint snapshots only the message history (zero-cost).
- File backups happen lazily, right before a file is first modified after the checkpoint.
- At most one auto-checkpoint per turn (avoids redundant checkpoints).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent

CHECKPOINT_DIR = Path.home() / ".mini-claude" / "checkpoints"

# Files larger than this are skipped during backup
MAX_BACKUP_BYTES = 1_000_000  # 1 MB


def _ensure_dir() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


def _session_checkpoint_dir(session_id: str) -> Path:
    _ensure_dir()
    d = CHECKPOINT_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Create ────────────────────────────────────────────────────

def create_checkpoint(
    agent: "Agent",
    label: str | None = None,
) -> str:
    """Create a checkpoint, snapshotting current message history.

    File backups are lazy (copy-on-write at tool execution time).
    Returns the 8-char hex checkpoint_id.
    """
    checkpoint_id = uuid.uuid4().hex[:8]
    session_dir = _session_checkpoint_dir(agent.session_id)

    data: dict[str, Any] = {
        "id": checkpoint_id,
        "label": label,
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
        "file_backups": {},
    }

    (session_dir / f"{checkpoint_id}.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )

    return checkpoint_id


# ─── CoW backup ─────────────────────────────────────────────────

def backup_file_before_write(
    session_id: str,
    file_path: str,
    checkpoint_id: str,
) -> None:
    """Copy-on-write: read the file and store its current content into the
    checkpoint's file_backups dict.  Only backs up once per file per
    checkpoint (first-write-wins).
    """
    # Nothing to backup if the file doesn't exist yet (it's a new file)
    if not os.path.exists(file_path):
        return

    # Skip large files
    file_size = os.path.getsize(file_path)
    if file_size > MAX_BACKUP_BYTES:
        from .ui import print_info
        print_info(
            f"Checkpoint: skipping backup of {file_path} "
            f"({file_size / 1_000_000:.1f} MB exceeds 1 MB limit)"
        )
        return

    cp_path = _session_checkpoint_dir(session_id) / f"{checkpoint_id}.json"
    if not cp_path.exists():
        return

    try:
        cp_data = json.loads(cp_path.read_text())
    except Exception:
        return

    # first-write-wins
    if file_path in cp_data.get("file_backups", {}):
        return

    try:
        original = Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return

    cp_data["file_backups"][file_path] = original
    cp_path.write_text(json.dumps(cp_data, indent=2, default=str), encoding="utf-8")


# ─── Auto checkpoint ────────────────────────────────────────────

def auto_create_checkpoint(
    agent: "Agent",
    tool_name: str,
    before_destructive: bool = False,
) -> str | None:
    """Auto-create a checkpoint before the first destructive tool in a turn.

    Rate-limited to one per turn to avoid redundant checkpoints.
    Returns checkpoint_id if created, None if skipped.
    """
    if not before_destructive:
        return None
    if agent._last_auto_checkpoint_turn == agent.current_turns:
        return None

    agent._last_auto_checkpoint_turn = agent.current_turns
    return create_checkpoint(agent, label=f"auto-before-{tool_name}")


# ─── List & query ───────────────────────────────────────────────

def list_checkpoints(session_id: str) -> list[dict]:
    """Return sorted list of checkpoint metadata (no file_backup contents)."""
    session_dir = _session_checkpoint_dir(session_id)
    results: list[dict] = []
    for f in sorted(session_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            results.append({
                "id": data["id"],
                "label": data.get("label"),
                "timestamp": data.get("timestamp", ""),
                "turn_number": data.get("turn_number", 0),
                "backup_count": len(data.get("file_backups", {})),
            })
        except Exception:
            pass
    results.sort(key=lambda c: c["turn_number"])
    return results


def get_latest_checkpoint_id(session_id: str) -> str | None:
    checkpoints = list_checkpoints(session_id)
    return checkpoints[-1]["id"] if checkpoints else None


def get_checkpoint(session_id: str, checkpoint_id: str) -> dict | None:
    """Load a single checkpoint's full data (including file_backups)."""
    path = _session_checkpoint_dir(session_id) / f"{checkpoint_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ─── Restore / rollback ─────────────────────────────────────────

def restore_checkpoint(agent: "Agent", checkpoint_id: str) -> None:
    """Rollback to a checkpoint: restore backed-up files, then message history."""
    from .ui import print_info, print_error

    cp = get_checkpoint(agent.session_id, checkpoint_id)
    if cp is None:
        print_error(f"Checkpoint {checkpoint_id} not found.")
        return

    # Step 1: restore files
    file_backups: dict[str, str] = cp.get("file_backups", {})
    restored = 0
    failed = 0
    for file_path, original_content in file_backups.items():
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text(original_content, encoding="utf-8")
            restored += 1
        except Exception:
            failed += 1

    # Step 2: restore message history
    msg_snapshot = cp.get("message_snapshot", {})
    if msg_snapshot.get("anthropic_messages") and not agent.use_openai:
        agent._anthropic_messages = msg_snapshot["anthropic_messages"]
    if msg_snapshot.get("openai_messages") and agent.use_openai:
        agent._openai_messages = msg_snapshot["openai_messages"]
        # Rebuild system prompt for OpenAI path (first message is always system)
        if agent._openai_messages and agent._openai_messages[0].get("role") == "system":
            agent._openai_messages[0]["content"] = agent._system_prompt

    # Step 3: reset tracking state
    agent._checkpoint_file_backups = {}
    agent._last_auto_checkpoint_turn = -1

    label = cp.get("label")
    label_str = f' "{label}"' if label else ""
    parts = [f"Rolled back to checkpoint {checkpoint_id}{label_str}."]
    if restored:
        parts.append(f"Restored {restored} file(s).")
    if failed:
        parts.append(f"({failed} failed)")
    print_info(" ".join(parts))


# ─── Cleanup ────────────────────────────────────────────────────

def _cleanup_session_checkpoints(session_id: str) -> None:
    """Remove all checkpoints for a session (called on /clear)."""
    import shutil
    d = CHECKPOINT_DIR / session_id
    if d.exists():
        shutil.rmtree(d)
