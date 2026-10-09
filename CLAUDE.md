# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

CatClaw 是一个教学用（"零基础学Agent"）的轻量级 AI Agent 框架：目标驱动、带 8 个内置工具、对话记忆管理（持久化 + 自动压缩 + 长期记忆）、MCP 协议扩展和 Goal 循环。代码注释、README 与终端 UI 均为中文 — 新增/修改代码时保持中文注释风格。

全仓约 2860 行 Python，无 pytest、无 lint 配置。最大的文件是评测 harness [eval_gaia.py](eval_gaia.py)（406 行）。

## Commands

```bash
uv sync                            # 安装依赖（uv，Python >= 3.13）
uv run python main.py              # 运行交互式 Agent REPL
uv run catclaw                     # 等价于 main.py（pyproject.toml 的 console script: main:main）
uv run python tools/mcp/server.py  # 独立运行示例 MCP Server（stdio）
```

```bash
uv run python tests/test_search.py             # search 工具测试（14 用例，含真实网络调用）
uv run python tests/test_search.py --offline   # 只跑离线用例（mock，不联网、不需要 key）
```

- **无测试框架**（无 pytest，唯一测试是 [tests/test_search.py](tests/test_search.py)）。各模块通过 `if __name__ == "__main__"` 提供演示入口：`uv run python tools/executor.py`（工具执行器 demo，内置模拟 tool_calls）、`uv run python core/llm.py`（LLM 调用自测，会真实调用 API）、`uv run python tools/builtins/search.py`（搜索自测）；read/write/edit/bash/grep/find/ls 各自也有命令行入口。
- 环境变量在 `.env`（gitignored）：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL_ID`；`TAVILY_API_KEY`（可选，搜索 API key，见下）；`CATCLAW_MCP_COMMAND` / `CATCLAW_MCP_ARGS`（可选，MCP server 启动命令，args 逗号分隔）。任何 OpenAI 兼容协议服务均可。注意 [main.py:247](main.py#L247) 只校验 KEY 和 BASE_URL，`OPENAI_MODEL_ID` 缺失时回退到代码内默认值 `kimi-k2.5`（[core/llm.py:22](core/llm.py#L22)、[core/llm.py:43](core/llm.py#L43)）。

## Architecture

核心是一个 55 行的工作流引擎（[core/node.py](core/node.py)），其余能力全部叠加其上：

- **`Node`** — `exec(payload) -> (action, next_payload)`。`>>` 串联节点，`- "action"` 标记下一条边的 action 名；`max_retries`/`wait` 支持失败重试（[_exec](core/node.py#L19-L28) 是重试包装层，`Flow` 调的是它）。
- **`Flow`** — 编排器：执行当前节点，按返回的 action 查 `successors[action]` 跳转，直到无后继。`Flow.run` **无步数上限**。
- **`shared`** — [core/node.py:4](core/node.py#L4) 的模块级 dict，充当 DI 容器。`main.py` 启动时填充 `memory`、`goal`、`tools`（LLM 格式）、`tool_executor`、`mcp_bridge`（[main.py:187-195](main.py#L187-L195)），节点内直接读写，无依赖注入框架。

两层循环组合：

- **Agent 循环**：`chat - "tool_call" >> tool_call; tool_call - "chat" >> chat`（[main.py:201-202](main.py#L201-L202)）。[ChatNode.exec](main.py#L53-L74) 调 LLM，消息含 `tool_calls` 时返回 `"tool_call"`；[ToolCallNode.exec](main.py#L80-L93) 解析执行后返回 `"chat"`，直到 LLM 不再要工具。ToolCallNode 是**批量**执行：`execute_all` 先跑完全部工具，再逐条把结果写回记忆（[main.py:86-91](main.py#L86-L91)）。
- **Goal 循环**：`run_goal()`（[main.py:121-126](main.py#L121-L126)）每轮向 memory 注入 goal 提醒消息并 `flow.run(None)`，直到 LLM 调用 `goal_complete` 工具把 `GoalState.active` 置 False。`GoalState` 只有 `text` / `active` 两个字段（[main.py:26-31](main.py#L26-L31)）。`/goal <描述>`、`/goal status`、`/goal clear` 三个 REPL 命令驱动（[main.py:220-240](main.py#L220-L240)）。**无轮数上限、无监督层**——LLM 不调 `goal_complete` 就会一直循环下去。

### 消息流与记忆（[core/memory.py](core/memory.py)）

- 所有消息都是 dict，经 `Memory.add_message` 追加写入 session.jsonl；持久化前只保留 `MESSAGE_KEYS`（role/content/tool_calls/tool_call_id/reasoning_content，[memory.py:14](core/memory.py#L14)）。
- `Memory.build_context` 把 `SYSTEM_PROMPT` + 长期记忆（MEMORY.md）+ 此前压缩产生的 system 摘要消息合并进发给 LLM 的 messages。
- **自动压缩**：`add_message` 读 `usage.total_tokens`，超过 `128K × 0.9`（`MAX_CONTEXT_LENGTH` / `COMPRESS_THRESHOLD`）时调 LLM 把旧消息压成一条 system 摘要并抽取新事实追加进 MEMORY.md，保留最近 4 条（`KEEP_MESSAGES_ON_COMPRESS`）。只在**无 tool_calls 的消息**上触发；压缩分界会回退，避免把 assistant 的 `tool_calls` 与其后的 tool 结果拆开（[memory.py:97-109](core/memory.py#L97-L109)）。
- **崩溃恢复**：[memory.py:38-57](core/memory.py#L38-L57) 扫描尾部，若存在没有 tool 结果跟随的 `tool_calls` 消息，则回滚该轮未完成消息并重写文件。

### 工具

- `Tool` 数据类（[tool_def.py:8-36](tools/builtins/tool_def.py#L8-L36)）：`name` / `description` / `parameters`(JSON Schema) / `fn`，`to_llm_format()` 转成 OpenAI function calling 格式。8 个内置工具在 [tool_def.py:40-157](tools/builtins/tool_def.py#L40-L157) 的 `get_builtin_tools()` 中注册。
- 内置工具实现散在 [tools/builtins/](tools/builtins/) 各模块：`read`（offset/limit + 30KB/2000 行截断）、`write`（自动建父目录）、`edit`（`old_text` 必须**唯一**匹配，0 次或 >1 次都报错）、`bash`（`shell=True`，返回 `{stdout, stderr, exit_code}`，30KB/2000 行截断）、`grep`（优先 ripgrep，`FileNotFoundError` 时回退纯 Python，100 条上限）、`find`（优先 fd，回退 Python glob，1000 条上限）、`ls`（500 条上限）、`search`（双通道，见下）。`bash` 的 `timeout` 默认 **30 秒**（[bash.py:12-13](tools/builtins/bash.py#L12-L13) 的 `DEFAULT_TIMEOUT_S`），不要改回 `None`——`subprocess.run(timeout=None)` 遇到挂死的命令会让整个 agent 永久卡住。
- `ToolExecutor`（[tools/executor.py](tools/executor.py)）解析 OpenAI 风格 `tool_calls`：[ToolCall.from_openai_item](tools/executor.py#L20-L33) 把 `function.arguments`（JSON **字符串**）解析成 dict，失败兜底为 `{}`；[execute](tools/executor.py#L73-L96) 把「工具不存在」和「执行异常」都归一化成 `is_error=True` 的 `ToolResult`（不向上抛）；`ToolResult.to_message()` 产出 `{"role": "tool", "tool_call_id": ..., "content": ...}` 回填记忆。
- ⚠️ [`tools/__init__.py`](tools/__init__.py) 里的 `chat_with_tools()` 是**按关键词硬编码的模拟函数**，不调用真实 LLM——只作教学演示，不要当成可用接口。
- **MCP 扩展**：`MCPBridge`（[tools/mcp/client.py](tools/mcp/client.py)）在后台线程跑自己的 asyncio event loop，用 FastMCP 3.x `StdioTransport` + `Client` 维持长连接，经 `asyncio.run_coroutine_threadsafe` 对外暴露同步 API（`connect` 等 30s、`call_tool` 等 60s）。`load_mcp_tools` 把远程工具包装成本地 `Tool`，加 `mcp_` 前缀避免与内置工具冲突。仅当设置 `CATCLAW_MCP_COMMAND` 时才加载（[main.py:129-149](main.py#L129-L149)，调用点 [main.py:165](main.py#L165)）。示例 server 提供 `search` / `add` / `multiply` 三个工具（[tools/mcp/server.py](tools/mcp/server.py)）。

### search 的双通道（[tools/builtins/search.py](tools/builtins/search.py)）

`search()` 是唯一入口，按顺序选通道：

| 条件 | 通道 | 实测延迟 | 硬超时 |
| --- | --- | --- | --- |
| 有 `TAVILY_API_KEY` 或 `TAVILY_KEY` | Tavily API | 7-10s | `TAVILY_HARD_TIMEOUT_S = 20` |
| 无 key，或 Tavily 调用失败 | ddgs / bing | 15-30s，重试 2 次 | `BING_HARD_TIMEOUT_S = 35` |

- **两条通道的返回值统一归一化成 ddgs 的字段名 `{title, href, body}`**（Tavily 原生是 `{title, url, content}`）。改动任一通道时必须保持这个映射，否则模型看到的结构会不一致。
- **墙钟超时按通道分开设**，且是靠 `ThreadPoolExecutor` 硬封顶的——ddgs 自己的 `timeout` 只管连接阶段，bing 慢响应能拖到 30s+。注意**不要**给 `_POOL` 加 `with`（`ThreadPoolExecutor.__exit__` 会 `shutdown(wait=True)`，超时就退化成永久阻塞）。
- **失败时抛 `SearchUnavailableError`**，错误信息面向 LLM：明确写"不要再次调用 search"，并给出替代方案（`curl -sL --max-time 20`）。这是刻意的——早期版本抛裸 `ConnectError`，模型看不懂就反复重试，是 GAIA 评测超时的主要成因之一。
- **key 已配置但 Tavily 调用失败时往 stderr 打一次告警**（只打一次）。否则 key 配错会静默降级成慢速 bing，用户只会觉得"搜索怎么这么慢"。
- **工具描述是动态的**：[tool_def.py](tools/builtins/tool_def.py) 调 `backend_name()`，把当前通道和延迟写进 description，让模型知道该不该频繁搜索。
- 本机网络实测：ddgs 的 8 个 text 后端里**只有 bing 能返回结果**（duckduckgo/google/yahoo/wikipedia 被风控，brave/mojeek/yandex 连接超时）。所以 `BING_BACKEND` 固定为 `"bing"`——ddgs 默认的 `backend="auto"` 会把 8 个引擎分批全试一遍，白白多等 20 秒。

### 评测 harness（[eval_gaia.py](eval_gaia.py)）

用 GAIA 验证集驱动真实 agent 循环打分，是仓库里唯一的大规模验证手段：

```bash
uv run python eval_gaia.py --stratified --per-level 10   # 分层抽样：L1/L2/L3 各 10 题（共 30）
uv run python eval_gaia.py --num 10 --seed 42            # 简单随机抽 10 题
uv run python eval_gaia.py --num 1 --max-steps 10        # 冒烟测试
uv run python eval_gaia.py --no-goal                     # 对比：不走 goal 循环
```

- **复用的是真框架**：import main.py 的 `ChatNode` / `ToolCallNode` / `GoalState` / `goal_message` / `make_goal_complete_tool`，但**不用** `Flow.run`（无步数上限）和 `run_goal`（会无限循环），而是按 Flow 语义手动展开循环（[eval_gaia.py:240-259](eval_gaia.py#L240-L259)）。
- **每题隔离**：`tempfile.TemporaryDirectory()` + `patch.object` 替换 `MEMORY_FILEPATH` / `LONG_TERM_MEMORY_FILEPATH`，并把 `Memory.compress` patch 成 no-op（避免隐藏的 LLM 压缩调用产生额外成本）；填充 `shared` 前先 `shared.clear()`。
- **判分**：`rule_match` 规则归一化匹配（`exact` / `number` / `contain`），不匹配再用 `llm_judge` 复核。
- GAIA jsonl 路径默认 `/Users/heyandong/Downloads/gaia_validation.jsonl`；38 个依赖附件的题跳过并标注 `skipped_attachment`；结果落盘 `eval_results/gaia_<ts>.jsonl`（该目录已 gitignore）。

## 已知问题

- [core/memory.py:8-9](core/memory.py#L8-L9) 用 Windows 风格反斜杠路径（`.\chat_memory\session.jsonl`）。在 macOS/Linux 上 `Path` 不解析反斜杠，会在仓库根目录创建字面名为 `.\chat_memory\session.jsonl` 的文件（git status 显示为 `".\\chat_memory\\..."`），而不是写进 `chat_memory/` 目录。`.gitignore` 只忽略 `chat_memory/`，所以这些反斜杠命名文件会显示为 untracked。修改记忆路径时应改用 `Path("chat_memory") / "session.jsonl"` 或正斜杠。
- **ToolCallNode 返回 `("chat", None)` 会覆盖 payload**（[main.py:93](main.py#L93)）。任何复用/包装这条循环的代码，若想拿到最终答案，必须在每次 ChatNode 返回后**立即捕获**，不能等循环结束再取——`goal_complete` 往往正是最后一步，此时 payload 已是 `None`。
- **README 与代码脱节**：README 的「监督层」章节、项目结构树里的 `core/hooks.py` / `core/goal.py`、以及 `cp .env.example .env` 的指引都已失效——这些文件不存在，`.env.example` 也已删除。README 声称默认模型 `deepseek-v4-pro`、`OPENAI_BASE_URL` 有默认值，实际代码默认模型是 `kimi-k2.5` 且 BASE_URL 无默认（必填）。
- **GAIA 数据集缺失**：`eval_gaia.py` 默认路径 `/Users/heyandong/Downloads/gaia_validation.jsonl` 已不存在（2026-09 时还在），重新评测前需先恢复数据集。
- **搜索延迟仍是大头**：即使走 Tavily 单次也要 7-10s，无 key 走 bing 则 15-30s。一道需要 5-10 次检索的 GAIA 题仍要 50-300s，`QUESTION_TIMEOUT_S = 300` 的墙钟上限依然容易被吃满。历史评测（2026-09，修复前）30 题 24 题超时，其中 `goal_complete` 的 11/11 全对、`timeout` 的 0/28 全错——**成败与终止原因完全一致，说明瓶颈在联网而非模型能力**。
- `Memory.compress` 内部有**隐藏的 LLM 调用**（[memory.py:120-130](core/memory.py#L120-L130)），长对话或批量评测会产生额外 token 成本。
- `shared` 是模块级可变 dict，同一进程内多次运行/多题之间必须显式 `clear()`，否则会残留上一轮的 memory、goal、tools。
