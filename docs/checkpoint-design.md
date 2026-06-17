# MiniClaudeCode Checkpoint 机制改造方案

## 1. 背景

MiniClaudeCode 目前**没有任何撤销/回滚能力**。现有的会话持久化（`session.py`）可以保存对话历史并在下次通过 `--resume` 恢复，但存在以下不足：

- 无法回退到对话中间的某个状态
- 文件被修改后无法恢复原始内容
- 没有快照/回滚的概念，模型出错后只能手动修复

本项目需要加入一个**轻量级 checkpoint 机制**，保持项目 ~3000 行的简洁哲学。

---

## 2. 现有架构分析

### 2.1 相关模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Agent | `agent.py` | 核心 agent 循环，持有所有运行时状态 |
| Session | `session.py` | 会话持久化（`~/.mini-claude/sessions/`） |
| Tools | `tools.py` | 工具定义、权限检查、工具执行 |
| CLI | `__main__.py` | 命令行入口、REPL 命令 |
| UI | `ui.py` | 终端渲染 |

### 2.2 Agent 状态全景

Agent 实例持有以下需要被 checkpoint 覆盖的状态：

```
Agent
├── 消息历史
│   ├── _anthropic_messages: list[dict]
│   └── _openai_messages: list[dict]
├── 会话元数据
│   ├── session_id: str (8 位 hex)
│   └── session_start_time: str (ISO 8601)
├── 计数器
│   ├── total_input_tokens: int
│   ├── total_output_tokens: int
│   ├── current_turns: int
│   └── last_api_call_time: float
├── 权限状态
│   └── _confirmed_paths: set[str]
├── Plan 模式状态
│   ├── _pre_plan_mode: str | None
│   ├── _plan_file_path: str | None
│   └── _context_cleared: bool
├── 文件追踪
│   └── _read_file_state: dict[str, float]  (path → mtime)
└── 记忆召回
    ├── _already_surfaced_memories: set[str]
    └── _session_memory_bytes: int
```

### 2.3 现有工具分类

| 类型 | 工具 | 是否有副作用 |
|------|------|:---:|
| 只读 | `read_file`, `list_files`, `grep_search`, `web_fetch` | ❌ |
| 写入 | `write_file`, `edit_file` | ✅ |
| 执行 | `run_shell` | ✅ |
| 元操作 | `enter_plan_mode`, `exit_plan_mode`, `agent`, `skill` | ✅ |

---

## 3. 设计目标

1. **手动创建**：通过 REPL 命令 `/checkpoint [label]` 或在对话中让模型自行调用 `create_checkpoint` 工具
2. **自动创建**：在 destructive tool（`write_file`、`edit_file`、`run_shell`）首次执行前自动创建
3. **快速回滚**：通过 `/rollback [id]` 恢复到 checkpoint，包括文件内容和对话历史
4. **列表查看**：通过 `/checkpoints` 列出当前 session 所有 checkpoint
5. **持久化**：checkpoint 跟随 session 持久化，`--resume` 后可继续使用
6. **轻量级**：利用 JSON 文件存储消息历史，文件快照用git

---

## 4. 核心设计

### 4.1 双层存储架构

```
消息历史（对话状态）          文件快照（代码状态）
        │                         │
        ▼                         ▼
  JSON 文件存储              隔离式私有 Git 仓库
 ~/.mini-claude/             ~/.mini-claude/
 checkpoints/<sid>/          checkpoints/<sid>/
   ├── xxx.json                └── repo/
   └── yyy.json                    ├── .git/      ← GIT_DIR 指向这里
                                   │   (完全独立，不碰用户 .git)
                                   └── (commit 对象)
                                        ↑
                                   GIT_WORK_TREE = 项目根目录
                                   只 git add Agent 修改过的文件
```

**为什么文件快照用 Git 而不是 JSON？**

| 维度 | JSON 内联 (v1) | Git-native (v2) |
|------|:---:|:---:|
| 大文件 | >1MB 跳过 | git 压缩/ pack 处理 |
| 新建/删除文件 | ✗ 不支持 | ✓ 天然支持 |
| 差异对比 | ✗ 需手动比对 | ✓ `git diff` / `git log` |
| 回滚方式 | 逐文件写回 | `git checkout <commit> -- <files>` |
| 远程备份 | ✗ | ✓ `git push` 到 GitHub |
| 用户 .git | 不碰 | **隔离式私有仓库，同样不碰** |

### 4.2 隔离式私有 Git 仓库

```
用户的仓库 (.git)           Checkpoint 私有仓库
        │                         │
~/project/                  ~/.mini-claude/checkpoints/<sid>/repo/
  ├── .git/    ← 用户自己的        └── .git/    ← 完全独立
  ├── src/                         
  └── ...                        通过 GIT_DIR + GIT_WORK_TREE 环境变量隔离
```

每次 checkpoint 操作通过 `subprocess.run(["git", ...], env={GIT_DIR: ..., GIT_WORK_TREE: ...})` 执行，完全不影响用户的 `.git` 目录。

### 4.3 只跟踪 Agent 修改的文件

Agent 持有一个 `_agent_modified_files: set[str]`，每次 `write_file` / `edit_file` 执行后自动添加文件路径。Checkpoint 只 `git add` 这个集合中的文件。

### 4.4 Checkpoint 数据格式（JSON 元数据）

```json
{
    "id": "a1b2c3d4",
    "label": "before-refactoring-auth",
    "commit": "a1b2c3d",  
    "timestamp": "2026-06-16T10:30:00Z",
    "turn_number": 5,
    "message_snapshot": {
        "anthropic_messages": [...],
        "openai_messages": null
    },
    "tracked_files": [
        "/home/user/project/src/auth.py",
        "/home/user/project/src/config.py"
    ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `str` | 8 位 hex 唯一 ID |
| `label` | `str \| null` | 用户自定义标签 |
| `commit` | `str \| null` | 私有 Git 仓库中的 commit hash |
| `timestamp` | `str` | ISO 8601 UTC |
| `turn_number` | `int` | 所在 turn |
| `message_snapshot` | `dict` | 消息历史快照 |
| `tracked_files` | `list[str]` | Agent 修改过的文件列表 |

### 4.5 原子回滚流程

```
/rollback a1b2c3d4
  │
  ├── 1. _backup_current_state()
  │     ├─ 遍历 _agent_modified_files，copy2 到临时目录
  │     └─ 深拷贝当前 message history
  │
  ├── 2. git checkout <commit> -- <tracked_files>
  │     └─ 失败 → raise RuntimeError
  │
  ├── 3. 从 JSON 恢复消息历史
  │     └─ 失败 → raise
  │
  ├── ✅ 成功 → 清理临时目录
  │
  └── ❌ 任意步骤失败 → _restore_from_backup()
        ├─ 将临时目录的文件 copy2 回原位置
        ├─ 恢复消息历史
        └─ 打印 "Previous state restored. Nothing was changed."
```



---

## 5. 详细实现

### 5.1 新文件：`python/mini_claude/checkpoint.py`（~150 行）

```python
"""Checkpoint management — file backup and message history snapshots for rollback."""

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

# 大文件阈值：超过此大小跳过备份
MAX_BACKUP_BYTES = 1_000_000  # 1 MB


def _ensure_dir() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


def _session_checkpoint_dir(session_id: str) -> Path:
    _ensure_dir()
    d = CHECKPOINT_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_checkpoint(
    agent: "Agent",
    label: str | None = None,
) -> str:
    """创建 checkpoint，快照当前消息历史。返回 checkpoint_id。"""
    checkpoint_id = uuid.uuid4().hex[:8]
    session_dir = _session_checkpoint_dir(agent.session_id)

    data = {
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


def backup_file_before_write(
    session_id: str,
    file_path: str,
    checkpoint_id: str,
) -> None:
    """CoW：在文件被修改前备份原始内容到最新 checkpoint。"""
    # 文件不存在则无需备份
    if not os.path.exists(file_path):
        return

    # 大文件跳过
    file_size = os.path.getsize(file_path)
    if file_size > MAX_BACKUP_BYTES:
        from .ui import print_info
        print_info(
            f"Checkpoint: skipping backup of {file_path} "
            f"({file_size / 1_000_000:.1f} MB exceeds limit)"
        )
        return

    cp_path = _session_checkpoint_dir(session_id) / f"{checkpoint_id}.json"
    if not cp_path.exists():
        return

    try:
        cp_data = json.loads(cp_path.read_text())
    except Exception:
        return

    # first-write-wins：已经备份过的文件不再重复备份
    if file_path in cp_data.get("file_backups", {}):
        return

    try:
        original = Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return

    cp_data["file_backups"][file_path] = original
    cp_path.write_text(json.dumps(cp_data, indent=2, default=str), encoding="utf-8")


def auto_create_checkpoint(
    agent: "Agent",
    tool_name: str,
    before_destructive: bool = False,
) -> str | None:
    """在 destructive tool 前自动创建 checkpoint（每 turn 最多一个）。"""
    if not before_destructive:
        return None
    if agent._last_auto_checkpoint_turn == agent.current_turns:
        return None  # 本 turn 已创建过

    agent._last_auto_checkpoint_turn = agent.current_turns
    return create_checkpoint(agent, label=f"auto-before-{tool_name}")


def list_checkpoints(session_id: str) -> list[dict]:
    """列出 session 下所有 checkpoint（轻量，不返回 file_backups 内容）。"""
    session_dir = _session_checkpoint_dir(session_id)
    results = []
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
    """加载 checkpoint 完整数据。"""
    path = _session_checkpoint_dir(session_id) / f"{checkpoint_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def restore_checkpoint(agent: "Agent", checkpoint_id: str) -> None:
    """回滚到 checkpoint：恢复文件 + 消息历史。"""
    from .ui import print_info, print_error

    cp = get_checkpoint(agent.session_id, checkpoint_id)
    if cp is None:
        print_error(f"Checkpoint {checkpoint_id} not found.")
        return

    # Step 1: 恢复文件
    file_backups = cp.get("file_backups", {})
    restored = 0
    failed = 0
    for file_path, original_content in file_backups.items():
        try:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            Path(file_path).write_text(original_content, encoding="utf-8")
            restored += 1
        except Exception:
            failed += 1

    # Step 2: 恢复消息历史
    msg = cp.get("message_snapshot", {})
    if msg.get("anthropic_messages") and not agent.use_openai:
        agent._anthropic_messages = msg["anthropic_messages"]
    if msg.get("openai_messages") and agent.use_openai:
        agent._openai_messages = msg["openai_messages"]

    # Step 3: 重建 system prompt（OpenAI 路径）
    if agent.use_openai and agent._openai_messages:
        if agent._openai_messages[0].get("role") == "system":
            agent._openai_messages[0]["content"] = agent._system_prompt

    # Step 4: 重置追踪状态
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


def _cleanup_session_checkpoints(session_id: str) -> None:
    """删除 session 所有 checkpoint（/clear 时调用）。"""
    import shutil
    d = CHECKPOINT_DIR / session_id
    if d.exists():
        shutil.rmtree(d)
```

### 5.2 修改：`python/mini_claude/agent.py`

#### 5.2.1 `__init__` 新增状态字段

在 `self._read_file_state: dict[str, float] = {}` 之后（line 209）：

```python
# Checkpoint state
self._checkpoint_file_backups: dict[str, str] = {}  # path → checkpoint_id
self._last_auto_checkpoint_turn: int = -1
```

#### 5.2.2 `_execute_tool_call` 路由新工具

在 `if name == "skill":` 之后（line 658）：

```python
if name == "create_checkpoint":
    return self._execute_create_checkpoint_tool(inp)
if name == "rollback_checkpoint":
    return self._execute_rollback_checkpoint_tool(inp)
```

#### 5.2.3 新增 Agent 方法

在 "REPL commands" 区域（line 373 附近）：

```python
# ─── Checkpoint ──────────────────────────────────────────

def create_manual_checkpoint(self, label: str | None = None) -> str:
    """手动创建 checkpoint（REPL /checkpoint 命令调用）。"""
    from .checkpoint import create_checkpoint
    cid = create_checkpoint(self, label=label)
    label_suffix = f' "{label}"' if label else ""
    print_info(f"Checkpoint {cid} created.{label_suffix}")
    return cid

def rollback_to_checkpoint(self, checkpoint_id: str) -> None:
    """回滚到指定 checkpoint（REPL /rollback 命令调用）。"""
    from .checkpoint import restore_checkpoint
    restore_checkpoint(self, checkpoint_id)

def list_all_checkpoints(self) -> list[dict]:
    """列出当前 session 所有 checkpoint。"""
    from .checkpoint import list_checkpoints
    return list_checkpoints(self.session_id)

def _execute_create_checkpoint_tool(self, inp: dict) -> str:
    """模型调用的 create_checkpoint 工具处理。"""
    from .checkpoint import create_checkpoint
    cid = create_checkpoint(self, label=inp.get("label"))
    label = inp.get("label", "")
    prefix = f'Label: "{label}". ' if label else ""
    return f"{prefix}Checkpoint {cid} created successfully."

def _execute_rollback_checkpoint_tool(self, inp: dict) -> str:
    """模型调用的 rollback_checkpoint 工具处理。"""
    from .checkpoint import restore_checkpoint, get_latest_checkpoint_id
    cid = inp.get("checkpoint_id")
    if not cid:
        cid = get_latest_checkpoint_id(self.session_id)
        if not cid:
            return "Error: No checkpoints exist to rollback to."
    restore_checkpoint(self, cid)
    return (
        f"Successfully rolled back to checkpoint {cid}. "
        "Conversation and files have been restored."
    )
```

#### 5.2.4 自动 checkpoint 注入（Anthropic 路径）

在 `_chat_anthropic` 的 tool loop 内（line 953 `perm = check_permission(...)` 之前）：

```python
# Auto-checkpoint before destructive tools
if tu.name in ("write_file", "edit_file", "run_shell"):
    from .checkpoint import auto_create_checkpoint, backup_file_before_write
    cid = auto_create_checkpoint(self, tu.name, before_destructive=True)
    if cid:
        file_path = inp.get("file_path")
        if file_path and tu.name in ("write_file", "edit_file"):
            backup_file_before_write(
                self.session_id,
                str(Path(file_path).resolve()),
                cid,
            )
```

#### 5.2.5 自动 checkpoint 注入（OpenAI 路径）

在 `_chat_openai` 的 tool loop 内（line 1155 `perm = check_permission(...)` 之前）：

```python
# Auto-checkpoint before destructive tools
if fn_name in ("write_file", "edit_file", "run_shell"):
    from .checkpoint import auto_create_checkpoint, backup_file_before_write
    cid = auto_create_checkpoint(self, fn_name, before_destructive=True)
    if cid:
        file_path = inp.get("file_path")
        if file_path and fn_name in ("write_file", "edit_file"):
            backup_file_before_write(
                self.session_id,
                str(Path(file_path).resolve()),
                cid,
            )
```

#### 5.2.6 `clear_history` 增加 cleanup

```python
def clear_history(self) -> None:
    self._anthropic_messages = []
    self._openai_messages = []
    if self.use_openai:
        self._openai_messages.append({"role": "system", "content": self._system_prompt})
    self.total_input_tokens = 0
    self.total_output_tokens = 0
    self.last_input_token_count = 0
    # Checkpoint cleanup
    self._checkpoint_file_backups = {}
    self._last_auto_checkpoint_turn = -1
    from .checkpoint import _cleanup_session_checkpoints
    _cleanup_session_checkpoints(self.session_id)
    print_info("Conversation cleared.")
```

#### 5.2.7 `restore_session` 增加 checkpoint 发现

```python
def restore_session(self, data: dict) -> None:
    if data.get("anthropicMessages"):
        self._anthropic_messages = data["anthropicMessages"]
    if data.get("openaiMessages"):
        self._openai_messages = data["openaiMessages"]
    # 检查已有 checkpoints
    from .checkpoint import list_checkpoints
    existing = list_checkpoints(self.session_id)
    if existing:
        print_info(
            f"Session restored ({self._get_message_count()} messages, "
            f"{len(existing)} checkpoint(s) available)."
        )
    else:
        print_info(f"Session restored ({self._get_message_count()} messages).")
```

### 5.3 修改：`python/mini_claude/tools.py`

#### 5.3.1 新增工具定义

在 `tool_definitions` 列表末尾（line 170 `]` 之前）：

```python
{
    "name": "create_checkpoint",
    "description": (
        "Create a named checkpoint to save the current conversation state "
        "and all modified files. Use this before attempting risky changes "
        "so you can rollback if needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "description": "Optional label for this checkpoint (e.g. 'before-refactoring-auth')",
            },
        },
    },
},
{
    "name": "rollback_checkpoint",
    "description": (
        "Rollback to a previous checkpoint, restoring all files and "
        "conversation state. Use this to undo changes if an implementation "
        "approach didn't work out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": (
                    "The ID of the checkpoint to rollback to. "
                    "Leave empty to rollback to the most recent checkpoint."
                ),
            },
        },
    },
},
```

#### 5.3.2 权限绕过

在 `check_permission` 函数中（line 593）：

```python
# 原代码
if tool_name in ("enter_plan_mode", "exit_plan_mode"):
    return {"action": "allow"}

# 改为
if tool_name in ("enter_plan_mode", "exit_plan_mode", "create_checkpoint", "rollback_checkpoint"):
    return {"action": "allow"}
```

### 5.4 修改：`python/mini_claude/__main__.py`

在 REPL 循环中（`/skills` 命令处理之后）新增三个命令：

```python
# --- Checkpoint commands ---
if inp == "/checkpoints":
    checkpoints = agent.list_all_checkpoints()
    if not checkpoints:
        print_info("No checkpoints in this session.")
    else:
        print_info(f"{len(checkpoints)} checkpoint(s):")
        for cp in checkpoints:
            label_str = f' - "{cp["label"]}"' if cp.get("label") else ""
            backup_str = (
                f" ({cp['backup_count']} file(s) backed up)"
                if cp.get("backup_count") else ""
            )
            print(
                f"    [{cp['id']}] Turn {cp['turn_number']} "
                f"@ {cp['timestamp']}{label_str}{backup_str}"
            )
    continue

if inp.startswith("/checkpoint"):
    # /checkpoint [label]
    parts = inp.split(" ", 1)
    label = parts[1].strip() if len(parts) > 1 else None
    agent.create_manual_checkpoint(label)
    continue

if inp.startswith("/rollback"):
    # /rollback [checkpoint_id]
    parts = inp.split(" ", 1)
    cid = parts[1].strip() if len(parts) > 1 else None
    if cid:
        agent.rollback_to_checkpoint(cid)
    else:
        from .checkpoint import get_latest_checkpoint_id
        latest = get_latest_checkpoint_id(agent.session_id)
        if latest:
            agent.rollback_to_checkpoint(latest)
        else:
            print_info("No checkpoints to rollback to.")
    continue
```

帮助文本更新：

```
  /checkpoint [label]  Create a session checkpoint
  /checkpoints         List all checkpoints in this session
  /rollback [id]       Rollback to a checkpoint (latest if no ID given)
```

### 5.5 修改：`python/mini_claude/ui.py`

`print_welcome()` 更新命令列表（新增三个）：

```python
console.print(
    "[dim]  Commands: /clear /plan /cost /compact /memory /skills "
    "/checkpoint /checkpoints /rollback[/dim]\n"
)
```

---

## 6. 设计决策

| 决策 | v1 (JSON) | v2 (Git-native) |
|------|-----------|-----------------|
| 文件备份策略 | Copy-on-Write → 内联 JSON | Git commit in private repo |
| 存储位置 | `~/.mini-claude/checkpoints/<sid>/*.json` | 同上 + `repo/.git` |
| 大文件 | >1MB 跳过 | Git 压缩处理，无限制 |
| 回滚方式 | 逐文件读写 | `git checkout <commit> -- <files>` |
| 原子性 | ✗ 部分失败 = 部分回滚 | ✓ backup → restore → verify |
| 隔离性 | 不碰用户 .git | `GIT_DIR` + `GIT_WORK_TREE` 完全隔离 |
| 跟踪范围 | 只备份被修改的文件 | `_agent_modified_files` 精准跟踪 |
| Sub-agent | 不触发 | 不触发（不变） |
| 远程备份 | ✗ | ✓ 可将 checkpoint repo push 到 GitHub |

---

## 7. 文件变更汇总

| 文件 | 操作 | 行数变化 | 说明 |
|------|:--:|:------:|------|
| `python/mini_claude/checkpoint.py` | 重写 | ~230 | git-native：私有仓库、原子回滚 |
| `python/mini_claude/agent.py` | 修改 | ~30 | `_agent_modified_files` 跟踪、简化注入 |
| `python/mini_claude/tools.py` | 修改 | +30 | 工具定义、权限绕过 |
| `python/mini_claude/__main__.py` | 修改 | +35 | REPL 命令 |
| `python/mini_claude/ui.py` | 修改 | +2 | 欢迎信息 |
| **合计** | | **~330 行** | |

---

## 8. 验证方案

### 测试 1：手动 checkpoint + git 文件回滚

```
$ mini-claude

> 请创建 src/hello.py 输出 hello world
(agent 创建文件)

> /checkpoint before-edit
Checkpoint a1b2c3d4 created. "before-edit"

> 把 hello.py 改成输出 goodbye world
(agent 修改文件)

> /rollback
Rolled back to checkpoint a1b2c3d4 "before-edit". Restored 1 file(s).

# 验证：文件已恢复为 hello world
```

### 测试 2：自动 checkpoint

```
$ mini-claude

> 帮我在 /tmp/test-auto.py 写一个快速排序实现
(agent 写入文件)
# 检查 ~/.mini-claude/checkpoints/<session_id>/ 
# 应该有一个 auto-before-write_file 的 checkpoint
```

### 测试 3：多 checkpoint 列表

```
> /checkpoint a
> /checkpoint b
> /checkpoint c
> /checkpoints
3 checkpoint(s):
    [xxx] Turn 0 @ ... - "a"
    [yyy] Turn 0 @ ... - "b"
    [zzz] Turn 0 @ ... - "c"
```

### 测试 4：Session 持久化

```
$ mini-claude
> /checkpoint persist-test
> exit

$ mini-claude --resume
Session restored (2 messages, 1 checkpoint(s) available).
> /rollback
Rolled back to checkpoint xxx "persist-test".
```

### 测试 5：原子回滚失败恢复

```
# 模拟回滚过程中 git checkout 失败
# 验证文件状态不变，消息历史不变
# 输出: "Previous state restored. Nothing was changed."
```

### 测试 6：隔离性验证

```
# 执行 checkpoint 操作后
$ git log  # 用户仓库无任何变化
# 验证 ~/.mini-claude/checkpoints/<sid>/repo/ 下有 git 记录
```

### 测试 7：边界情况

| 场景 | 预期行为 |
|------|---------|
| 无 checkpoint 时 `/rollback` | `No checkpoints to rollback to.` |
| `/rollback invalid-id` | `Checkpoint invalid-id not found.` |
| `/clear` 后 `/checkpoints` | `No checkpoints in this session.` |
| 连续 write_file 同一 turn | 每次写入产生独立 git commit |
| 新文件创建 + 回滚 | 文件被删除（git checkout 到空状态） |
| 已删除文件的回滚 | 文件被恢复 |
