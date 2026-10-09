# 🐱 CatClaw

**从零开始构建的轻量级 AI Agent 框架**

CatClaw 是一个目标驱动的自主 AI Agent，配备 **8 个内置工具**、**对话记忆管理**（持久化 + 自动压缩 + 长期记忆）、**MCP 协议扩展**（可连接外部工具服务器）和 **Goal 目标循环**。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置环境变量：在项目根目录新建 .env，至少填入前两项
#    OPENAI_API_KEY=sk-...
#    OPENAI_BASE_URL=<你的服务地址>    # 任何 OpenAI 兼容协议服务均可
#    OPENAI_MODEL_ID=kimi-k2.5        # 可选，不填则用 kimi-k2.5

# 3. 运行
uv run python main.py
```

## 环境变量

| 变量 | 说明 | 默认值 |
| :----: | :----: | :------: |
| `OPENAI_API_KEY` | API 密钥（**必填**） | 无，缺失则启动时提示退出 |
| `OPENAI_BASE_URL` | API 地址（**必填**） | 无，代码里没有兜底值 |
| `OPENAI_MODEL_ID` | 模型 ID（可选） | `kimi-k2.5` |
| `TAVILY_API_KEY` | 搜索 API key（可选，见「网页搜索」一节） | 无，缺失则回退 bing |
| `CATCLAW_MCP_COMMAND` | MCP Server 启动命令（可选） | - |
| `CATCLAW_MCP_ARGS` | MCP Server 参数，逗号分隔（可选） | - |

> `OPENAI_API_KEY` 和 `OPENAI_BASE_URL` 缺任何一个，[main.py](main.py) 会打印提示后直接退出；`OPENAI_MODEL_ID` 不填则回退到代码内的 `kimi-k2.5`。

## 功能特性

### 🛠 8 个内置工具

| 工具 | 功能 | 典型用法 |
| ------ | ------ | --------- |
| `read` | 读取文件内容 | `read("main.py", offset=1, limit=50)` |
| `write` | 写入文件（自动创建父目录） | `write("out.txt", "hello")` |
| `edit` | 精确文本替换（单次匹配） | `edit("f.py", "old", "new")` |
| `bash` | 执行 Shell 命令（默认 30 秒超时，输出 30KB/2000 行截断） | `bash("ls -la")` |
| `grep` | 正则搜索文件内容 | `grep("class Node", ".", glob="*.py")` |
| `find` | Glob 模式查找文件 | `find("**/*.py")` |
| `ls` | 列出目录内容 | `ls(".")` |
| `search` | 网页搜索（双通道，见下） | `search("Python Agent")` |

#### 🌐 网页搜索：双通道自动回退

`search` 按下面的顺序选通道，两个通道返回的结果结构完全一致（`{title, href, body}`），模型侧无感知：

| 条件 | 通道 | 实测延迟 | 硬超时 |
| ------ | ------ | --------- | -------- |
| 配置了 `TAVILY_API_KEY` | Tavily API（返回已抽取的正文，通常无需再抓页面） | 7-10s | 20s |
| 未配置，或 Tavily 调用失败 | ddgs 的 bing 后端（免费无需 key） | 15-30s，重试 2 次 | 35s |

- 墙钟超时用 `ThreadPoolExecutor` 硬封顶 —— ddgs 自带的 `timeout` 只管连接阶段，慢响应能拖到 30s 以上
- 两个通道都失败时抛 `SearchUnavailableError`，错误信息**面向上层模型**：明确写「不要再次调用 search」并给出替代方案（改用 `bash` + `curl -sL --max-time 20 <url>`）。早期版本抛裸连接异常，模型看不懂就反复重试，是评测超时的主要成因
- key 配错时会往 stderr 打**一次**告警，避免静默降级成慢速通道
- 本机网络实测：ddgs 的 8 个 text 后端里只有 `bing` 能返回结果，所以后端固定为 bing，不用默认的 `auto`（那会把 8 个引擎分批全试一遍，白等 20 秒）

### 🧠 对话记忆管理

| 能力 | 说明 |
| ------ | ------ |
| **对话持久化** | 所有消息以 JSONL 格式实时追加写入 `chat_memory/session.jsonl`，重启后自动恢复 |
| **自动压缩** | 当 token 用量超过 128K × 90% 阈值时，LLM 自动将早期消息压缩为摘要，保留最后 4 条原始消息 |
| **长期记忆** | LLM 从对话中提取关键事实（用户偏好、重要事件等），写入 `chat_memory/MEMORY.md`，后续对话中自动注入 |
| **崩溃恢复** | 检测未完成的 `tool_calls` 序列，自动回滚到稳定状态 |

**相关文件**：

```text
chat_memory/
├── session.jsonl    # 完整对话历史（JSONL 追加写入）
└── MEMORY.md        # 长期记忆（LLM 自动维护）
```

### 🔌 MCP 协议扩展

通过连接外部 MCP (Model Context Protocol) Server，可以动态加载更多工具。

#### 方式一：使用自带的示例 MCP Server

```bash
# .env 中添加：
CATCLAW_MCP_COMMAND="python"
CATCLAW_MCP_ARGS="tools/mcp/server.py"

uv run python main.py
```

启动时会自动连接并加载 `mcp_search`、`mcp_add`、`mcp_multiply` 三个工具。

#### 方式二：独立运行 MCP Server

```bash
# 启动内置 MCP Server（stdio 传输）
uv run python tools/mcp/server.py
```

Server 提供 `search`（网页搜索）、`add`（加法）、`multiply`（乘法）三个工具。

#### 方式三：连接第三方 MCP Server

```bash
# 例如连接 filesystem server
export CATCLAW_MCP_COMMAND="npx"
export CATCLAW_MCP_ARGS="-y,@modelcontextprotocol/server-filesystem,/path/to/allowed/dir"
uv run python main.py
```

**MCP 工具命名规则**：所有 MCP 工具会自动添加 `mcp_` 前缀，避免与内置工具冲突。例如 MCP Server 的 `search` 工具会注册为 `mcp_search`。

**架构**：`MCPBridge` 类在后台线程中维护与 MCP Server 的长连接（基于 FastMCP 3.x 的 `StdioTransport`），对外暴露同步 API，与现有的同步 Agent 循环无缝集成。

### 🎯 Goal 目标驱动

通过 `/goal` 命令启动自主执行模式，Agent 会持续运行直到完成目标：

| 命令 | 说明 |
| ------ | ------ |
| `/goal <描述>` | 启动一个目标，Agent 自主执行直到调用 `goal_complete` |
| `/goal status` | 查看当前目标状态 |
| `/goal clear` | 清除当前目标 |

**工作原理**：`run_goal()` 在每轮对话前注入目标提醒消息，引导 Agent 持续推进，直到 Agent 主动调用 `goal_complete` 工具标记完成。

## 交互示例

```text
🐱 CatClaw — Agent with Goal + Memory + MCP
📝 已恢复 4 条历史消息

👤 You: 帮我搜索最新的 Python 3.14 发布时间
  [Tool] 执行: search({'query': 'Python 3.14 release date 2025'})
  [Tool] 结果: [{'title': 'Python 3.14.0 release...', ...

🐱 CatClaw: Python 3.14 预计在 2025 年 10 月发布...

👤 You: /goal 创建一个 hello.py 文件，输出 "Hello from CatClaw"

🎯 Goal started: 创建一个 hello.py 文件，输出 "Hello from CatClaw"

  [Tool] 执行: write({'path': 'hello.py', 'content': 'print("Hello from CatClaw")'})
  [Tool] 结果: Successfully wrote 30 bytes to hello.py
  [Tool] 执行: bash({'command': 'python hello.py'})
  [Tool] 结果: Hello from CatClaw
  [Tool] 执行: goal_complete({})
  [Tool] 结果: Goal complete

🐱 CatClaw: 目标完成！已创建 hello.py，运行验证通过。
```

## 项目结构

```text
CatClaw/
├── core/
│   ├── node.py          # 工作流引擎 — Node + Flow（55 行）
│   ├── llm.py           # LLM 调用接口（OpenAI 兼容协议）
│   └── memory.py        # 对话记忆管理（持久化 + 压缩 + 长期记忆）
├── tools/
│   ├── executor.py      # 工具解析与执行引擎
│   ├── builtins/        # 8 个内置工具
│   │   ├── read.py      #   文件读取（offset/limit + 截断）
│   │   ├── write.py     #   文件写入（自动创建父目录）
│   │   ├── edit.py      #   精确文本替换（唯一匹配）
│   │   ├── bash.py      #   Shell 命令执行（默认 30s 超时 + 30KB/2000行截断）
│   │   ├── grep.py      #   内容搜索（ripgrep + Python 回退）
│   │   ├── find.py      #   文件查找（fd + Python glob 回退）
│   │   ├── ls.py        #   目录列表（500条限制）
│   │   ├── search.py    #   网页搜索（Tavily API + bing 双通道回退）
│   │   └── tool_def.py  #   Tool 数据类 + LLM 格式转换
│   └── mcp/             # MCP 协议扩展
│       ├── client.py    #   MCP 客户端（后台线程长连接 + 同步桥）
│       └── server.py    #   MCP 示例服务器（search/add/multiply）
├── tests/
│   └── test_search.py   # search 工具测试（9 个离线 mock + 5 个真实网络用例）
├── main.py              # 主程序入口 — ChatNode + ToolCallNode + Goal
├── pyproject.toml
└── README.md
```

## 架构设计

CatClaw 的核心是一个 **55 行的工作流引擎**，在此基础上逐层叠加能力：

```text
                    ┌─────────────────────────┐
                    │      Goal Loop           │
                    │  run_goal() 外层循环       │
                    │  持续注入目标提醒           │
                    └───────────┬─────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                 Agent Loop                     │
        │         ChatNode ←→ ToolCallNode               │
        │    (LLM 决策)      (工具执行 + 结果回传)         │
        └───────┬───────────┬───────────┬───────────────┘
                │           │           │
    ┌───────────┴──┐  ┌─────┴─────┐  ┌─┴──────────────┐
    │   Memory     │  │ 8 Builtins│  │   MCP Bridge    │
    │ 持久化+压缩   │  │ 本地工具   │  │  外部工具扩展    │
    │ +长期记忆    │  │           │  │  (后台线程)      │
    └──────────────┘  └───────────┘  └─────────────────┘
```

核心设计模式：

- **`shared` 全局状态** — 模块级 `dict` 作为 DI 容器，`memory`、`tools`、`executor`、`goal` 都在其中，节点间直接读写
- **`Node` 基类** — `exec(payload) -> (action, next_payload)`，通过 `>>` 运算符串联
- **`Flow` 编排器** — 按 action 名称驱动节点跳转，直到无后继为止
- **`- "action"` 连线模式** — `chat - "tool_call" >> tool_call` 表示当 ChatNode 返回 `"tool_call"` 时跳转到 ToolCallNode
- **Agent 循环** — `ChatNode ↔ ToolCallNode` 构成工具调用的无限循环，直到 LLM 不再返回 `tool_calls`
- **Goal 循环** — `run_goal()` 外层持续注入 `/goal` 消息，直到 Agent 主动调用 `goal_complete` 工具
- **MCP 桥** — `MCPBridge` 后台线程维护长连接，通过 `asyncio.run_coroutine_threadsafe` 实现同步调用

## 依赖

| 包 | 用途 |
| ---- | ------ |
| `openai` | LLM API 客户端（兼容任何 OpenAI 协议服务） |
| `ddgs` | 网页搜索（bing 后端）。Tavily 通道走标准库 `urllib`，不需要额外依赖 |
| `fastmcp` | MCP 服务器框架 + 客户端（FastMCP 3.x） |
| `python-dotenv` | `.env` 环境变量加载 |
