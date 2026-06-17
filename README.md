# Mini Claude Code

从零构建的轻量级 Coding Agent CLI，~4300 行 Python，零外部框架依赖。

## 快速开始

**需要 Python >= 3.11。**

```bash
cd python
pip install -e .

# 设置 API Key（二选一）
export ANTHROPIC_API_KEY=sk-ant-...          # Anthropic 后端
# 或
export OPENAI_API_KEY=sk-...                  # OpenAI 兼容后端
export OPENAI_BASE_URL=https://api.openai.com/v1

# 运行
mini-claude-py "fix the bug in src/app.py"    # 一次性模式
mini-claude-py                                # 交互式 REPL
mini-claude-py --resume                        # 恢复上次会话
```

**OpenAI 兼容后端**（用于非 Anthropic API，如 azure、local proxy）：
```bash
OPENAI_API_KEY=sk-xxx mini-claude-py \
  --api-base https://your-endpoint/v1 \
  --model gpt-4o \
  "hello"
```

## 功能

### 核心能力

| 功能 | 说明 |
|------|------|
| **双后端** | Anthropic SDK + OpenAI SDK，流式输出 |
| **4 层压缩** | Budget → Snip → Microcompact → Compact，自动管理上下文窗口 |
| **12 个工具** | read_file, write_file, edit_file, list_files, grep_search, run_shell, web_fetch, agent, skill, create_checkpoint, rollback_checkpoint, enter/exit_plan_mode |
| **5 种权限** | default, acceptEdits, bypassPermissions, plan, dontAsk |
| **Plan 模式** | 先规划再执行，4 选项审批流程 |
| **Sub-agent** | explore / plan / general 三个内置类型 + 自定义 Agent |
| **MCP 支持** | 完整的 MCP 协议兼容（JSON-RPC stdio），零外部 SDK 依赖 |
| **记忆系统** | 文件级持久化记忆 + 语义召回（side query） |
| **技能系统** | inline / fork 两种上下文模式，用户级 + 项目级 |
| **Checkpoint** | git-native 文件快照 + 原子回滚（见下文） |

### Checkpoint 机制

```
消息历史（对话状态）          文件快照（代码状态）
        │                         │
        ▼                         ▼
  JSON 文件                隔离式私有 Git 仓库
                             GIT_DIR 隔离，不碰用户 .git
```

- **手动创建**：`/checkpoint [label]`
- **自动创建**：write_file / edit_file / run_shell 执行前自动触发
- **查看列表**：`/checkpoints`
- **原子回滚**：`/rollback [id]` — 先备份当前状态，回滚文件 + 消息，任何步骤失败全量还原
- **精准跟踪**：只追踪 Agent 通过 write_file / edit_file 实际修改过的文件

## 命令行参数

```
mini-claude-py [options] [prompt]

Options:
  --yolo, -y          跳过确认（bypassPermissions）
  --plan              计划模式（只读）
  --accept-edits      自动批准文件编辑
  --dont-ask          自动拒绝确认（CI 模式）
  --thinking          启用 extended thinking（Anthropic）
  --model, -m         指定模型
  --api-base URL      OpenAI 兼容端点
  --resume            恢复上次会话
  --max-cost USD      最大花费上限
  --max-turns N       最大 agent turn 数
  --help, -h          显示帮助
```

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/clear` | 清空对话历史 |
| `/plan` | 切换 plan 模式 |
| `/cost` | 显示 token 用量和成本 |
| `/compact` | 手动压缩对话 |
| `/memory` | 查看记忆列表 |
| `/skills` | 查看可用技能 |
| `/checkpoint [label]` | 创建 checkpoint |
| `/checkpoints` | 列出所有 checkpoint |
| `/rollback [id]` | 回滚到 checkpoint |
| `/<skill-name>` | 调用技能 |

## 文件结构

```
python/mini_claude/
├── __init__.py          (3 行)
├── __main__.py          (344 行)   CLI 入口与 REPL
├── agent.py             (1380 行)  核心循环、双后端、压缩、sub-agent
├── checkpoint.py        (341 行)   Git-native checkpoint 与原子回滚
├── tools.py             (727 行)   工具定义、权限检查、执行
├── ui.py                (207 行)   终端渲染（Rich）
├── prompt.py            (243 行)   系统提示词构建
├── session.py           (49 行)    会话持久化
├── memory.py            (378 行)   文件级 + 语义记忆
├── skills.py            (171 行)   技能发现与执行
├── subagent.py          (171 行)   子 Agent 类型
├── mcp_client.py        (250 行)   MCP JSON-RPC 客户端
└── frontmatter.py       (47 行)    YAML frontmatter 解析
```

## 依赖

```
anthropic>=0.40.0
openai>=1.50.0
rich>=13.0.0
```
