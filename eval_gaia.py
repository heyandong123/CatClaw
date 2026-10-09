"""GAIA 验证集评测 harness —— 驱动 CatClaw agent 跑 GAIA 题并判分。

用法:
    uv run python eval_gaia.py --num 10            # 随机抽 10 个无附件题评测
    uv run python eval_gaia.py --stratified --per-level 10   # 分层抽样: 每 Level 各 10 题（共 30）
    uv run python eval_gaia.py --num 1 --max-steps 10   # 冒烟测试
    uv run python eval_gaia.py --no-goal           # 对比：不用 goal 循环，普通对话模式

设计要点:
- 复用 main.py 的 ChatNode/ToolCallNode/GoalState/goal_message/make_goal_complete_tool，
  按 Flow.run 语义手动展开循环（Flow.run 无步数上限，run_goal 会无限循环）。
- 每题的 memory 用 tempdir 隔离（monkeypatch MEMORY_FILEPATH/LONG_TERM_MEMORY_FILEPATH），
  并把 Memory.compress 打成 no-op，避免隐藏的 LLM 压缩调用与仓库内反斜杠污染文件。
- 判分: 规则归一化匹配（exact/number/contain），不匹配时用 LLM 复核（GAIA 官方做法简化版）。
- 38 个依赖附件的题（文件本体未随 jsonl 提供）跳过，单独标注。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from main import (
    SYSTEM_PROMPT,
    ChatNode,
    GoalState,
    ToolCallNode,
    goal_message,
    make_goal_complete_tool,
)
from core import memory as core_memory
from core.llm import call_llm_simple
from core.memory import Memory
from core.node import shared
from tools.builtins.tool_def import get_builtin_tools
from tools.executor import ToolExecutor

GAIA_PATH = Path("/Users/heyandong/Downloads/gaia_validation.jsonl")
EVAL_DIR = Path("eval_results")
MAX_STEPS = 30              # 每题 LLM 调用数上限
QUESTION_TIMEOUT_S = 300    # 每题墙钟上限


# ---------------------------------------------------------------- 数据加载与抽样

def load_questions(path: Path) -> list[dict]:
    """读取 GAIA jsonl，返回题目 dict 列表。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def split_questions(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按是否依赖附件文件分成 (无附件, 有附件) 两组。"""
    no_file = [r for r in rows if not r.get("file_name")]
    with_file = [r for r in rows if r.get("file_name")]
    return no_file, with_file


def sample_questions(rows: list[dict], num: int, seed: int) -> list[dict]:
    """固定 seed 随机抽样，可复现。"""
    n = min(num, len(rows))
    return random.Random(seed).sample(rows, n)


def sample_stratified(
    rows: list[dict], per_level: int, seed: int
) -> tuple[list[dict], dict[str, int]]:
    """分层抽样: 按 Level 分组，每层独立抽 per_level 题（不足则全抽），可复现。

    返回 (题目列表, 各层抽样数 {level: n})。每层用同一 seed 独立抽样，
    层间互不影响 —— 简单随机抽样里 L3 题少经常抽不到，分层保证每层都有样本。
    """
    by_level: dict[str, list[dict]] = {}
    for r in rows:
        by_level.setdefault(str(r.get("Level", "")), []).append(r)
    sampled: list[dict] = []
    counts: dict[str, int] = {}
    for lv in sorted(by_level):
        n = min(per_level, len(by_level[lv]))
        counts[lv] = n
        sampled += random.Random(seed).sample(by_level[lv], n)
    return sampled, counts


# ---------------------------------------------------------------- 判分（纯函数）

def normalize_answer(s: str) -> str:
    """归一化答案: NFKC 全角转半角 → 小写 → 去冠词 → 去标点 → 去前导零 → 合并空白。"""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.lower().strip()
    s = re.sub(r"\b(a|an|the)\b", " ", s)   # 去冠词
    s = re.sub(r"[^\w\s]", " ", s)          # 去标点（含 $ % , 千分位逗号）
    s = re.sub(r"0+(?=\d)", "", s)          # 去前导零（"007" → "7"，孤立 "0" 保留）
    return re.sub(r"\s+", " ", s).strip()


def _to_number(s: str) -> float | None:
    """取字符串中第一个数字（容忍千分位逗号/小数/负号），失败返回 None。"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(s).replace(",", ""))
    return float(m.group()) if m else None


def rule_match(gold: str, pred: str) -> tuple[bool, str]:
    """规则判分: 返回 (是否匹配, 匹配方式 exact/number/contain/none)。"""
    ng, np = normalize_answer(gold), normalize_answer(pred)
    if not np:
        return False, "none"
    if ng == np:
        return True, "exact"
    # 数字宽容: 两边都能解析成数值时按数值比较（"41" vs "41.0"、"34,689" vs "34689"）
    num_gold, num_pred = _to_number(gold), _to_number(pred)
    if num_gold is not None and num_pred is not None and num_gold == num_pred:
        return True, "number"
    # 包含匹配（"egalitarian" vs "egalitarian society"），长度下限防短词误判
    if len(ng) >= 3 and (ng in np or np in ng):
        return True, "contain"
    return False, "none"


def judge_prompt(question: str, gold: str, pred: str) -> str:
    """构造 LLM 复核 prompt（GAIA 官方宽松匹配判定的简化版）。"""
    return f"""You are an answer evaluator for the GAIA benchmark.
Determine whether the predicted answer matches the reference answer for the question.
Ignore differences in case, punctuation, articles, units, and formatting (e.g. "41" vs "41.0", "34,689" vs "34689").
A predicted answer that is a valid paraphrase or equivalent form of the reference counts as a match.
Reply with exactly one word: CORRECT or INCORRECT.

Example 1:
Question: What is the zip code of the city where Mozart was born?
Reference: 5020
Predicted: Salzburg 5020
Answer: CORRECT

Example 2:
Question: How many legs does an octopus have?
Reference: 8
Predicted: 10
Answer: INCORRECT

Now evaluate:
Question: {question}
Reference answer: {gold}
Predicted answer: {pred}
Answer:"""


def llm_judge(question: str, gold: str, pred: str) -> str:
    """LLM 复核，返回 CORRECT / INCORRECT / ERROR（调用失败时）。"""
    try:
        raw = call_llm_simple(judge_prompt(question, gold, pred)).strip()
    except Exception:
        return "ERROR"
    first = raw.splitlines()[0].strip().upper() if raw else ""
    if first.startswith("CORRECT"):
        return "CORRECT"
    if first.startswith("INCORRECT"):
        return "INCORRECT"
    return "ERROR"


# ---------------------------------------------------------------- 单题运行

def run_one_question(
    row: dict,
    *,
    use_goal: bool = True,
    max_steps: int = MAX_STEPS,
    timeout_s: float = QUESTION_TIMEOUT_S,
) -> dict:
    """跑单题，返回明细 dict。任何异常兜底为 status=error，不中断整批。"""
    record = {
        "task_id": row["task_id"],
        "level": row.get("Level", ""),
        "question": row["Question"],
        "gold": row.get("Final answer", ""),
        "status": "done",
        "predicted": "",
        "correct": False,
        "match_method": "",
        "judge_raw": "",
        "steps": 0,
        "llm_calls": 0,
        "tokens": 0,
        "duration_s": 0.0,
        "reason": "",
        "notes": "",
    }
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core_memory, "MEMORY_FILEPATH", Path(tmp) / "session.jsonl"), \
                patch.object(core_memory, "LONG_TERM_MEMORY_FILEPATH", Path(tmp) / "MEMORY.md"), \
                patch.object(Memory, "compress", lambda self, total_tokens: None):
            # ---- 每题重建状态（goal_complete 闭包绑定上一题状态，必须新建） ----
            memory = Memory()
            goal = GoalState()
            goal.text = row["Question"]
            goal.active = use_goal

            # bash 超时由 tools/builtins/bash.py 的 DEFAULT_TIMEOUT_S 负责，这里不再包补丁
            executor = ToolExecutor()

            goal_tool = make_goal_complete_tool(goal)
            executor.tools.append(goal_tool)
            executor.tool_map[goal_tool.name] = goal_tool

            shared.clear()  # 防上题残留
            shared["memory"] = memory
            shared["goal"] = goal
            shared["tools"] = [t.to_llm_format() for t in get_builtin_tools()] + [goal_tool.to_llm_format()]
            shared["tool_executor"] = executor

            if use_goal:
                memory.add_message(goal_message(goal))
            memory.add_message({"role": "user", "content": row["Question"]})

            # ---- 与 main.py 相同的两条边 ----
            chat_node = ChatNode()
            tool_call_node = ToolCallNode()
            chat_node - "tool_call" >> tool_call_node
            tool_call_node - "chat" >> chat_node

            # ---- 手动展开 flow（Flow.run 无步数上限；run_goal 会无限循环） ----
            node, action, payload = chat_node, "default", None
            last_answer, llm_calls, tokens, tool_steps = "", 0, 0, 0
            reason = ""
            while node is not None and (not use_goal or goal.active) and llm_calls < max_steps:
                if time.monotonic() - start > timeout_s:
                    reason = "timeout"
                    break
                if isinstance(node, ChatNode):
                    llm_calls += 1
                action, payload = node._exec(payload)
                if isinstance(node, ChatNode):
                    # ★ 立即捕获答案（ToolCallNode 返回 ("chat", None) 会覆盖 payload）
                    if payload.get("content"):
                        last_answer = payload["content"]
                    # usage 会被 Memory.add_message 过滤掉，须在这里记账
                    tokens += payload.get("usage", {}).get("total_tokens", 0)
                else:
                    tool_steps += 1
                node = node.successors.get(action)

            if not reason:
                if use_goal and goal.active:
                    reason = "truncated" if llm_calls >= max_steps else "early_stop"
                elif use_goal:
                    reason = "goal_complete"
                else:
                    reason = "truncated" if llm_calls >= max_steps else "done"

            # ---- 判分: 规则匹配，不匹配再 LLM 复核 ----
            predicted = last_answer.strip()
            correct, method = rule_match(record["gold"], predicted)
            judge_raw = ""
            if correct:
                record["match_method"] = method
            else:
                judge_raw = llm_judge(record["question"], record["gold"], predicted)
                if judge_raw == "CORRECT":
                    correct = True
                    record["match_method"] = "llm"
                else:
                    record["match_method"] = "llm-error" if judge_raw == "ERROR" else "rule-none"

            record.update(
                predicted=predicted,
                correct=correct,
                judge_raw=judge_raw,
                steps=tool_steps,
                llm_calls=llm_calls,
                tokens=tokens,
                duration_s=round(time.monotonic() - start, 1),
                reason=reason,
            )
    except Exception as e:
        record["status"] = "error"
        record["notes"] = f"{type(e).__name__}: {e}"
    finally:
        record["duration_s"] = round(time.monotonic() - start, 1)
    return record


# ---------------------------------------------------------------- 批量运行与报告

def run_eval(rows: list[dict], **kwargs) -> list[dict]:
    """串行跑题，每题结束后立即打印一行进度。"""
    records = []
    for i, row in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] Level {row.get('Level', '?')}: {row['Question'][:90]}")
        rec = run_one_question(row, **kwargs)
        records.append(rec)
        mark = "✅" if rec["correct"] else "❌"
        print(
            f"  {mark} 预测: {rec['predicted'][:60]!r} | 期望: {rec['gold'][:60]!r} | "
            f"匹配: {rec['match_method']} | {rec['llm_calls']} 次LLM | {rec['duration_s']}s | {rec['reason']}"
        )
        if rec["status"] == "error":
            print(f"  ⚠️  异常: {rec['notes']}")
        time.sleep(1)  # 题间休息，避免 DDGS 限流
    return records


def write_results(records: list[dict], skipped: list[dict], out_path: Path) -> None:
    """落盘结果 jsonl（含被跳过的附件题记录）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    all_rows = [
        {
            "task_id": r["task_id"],
            "level": r.get("Level", ""),
            "question": r.get("Question", ""),
            "gold": r.get("Final answer", ""),
            "status": "skipped_attachment",
        }
        for r in skipped
    ] + records
    with open(out_path, "w", encoding="utf-8") as f:
        for r in all_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n结果已保存: {out_path}")


def print_report(records: list[dict], *, seed: int, use_goal: bool, total_s: float) -> None:
    """打印汇总报告: 整体准确率 + 按 Level 分。"""
    done = [r for r in records if r["status"] == "done"]
    errored = [r for r in records if r["status"] == "error"]
    total = len(done)
    correct = sum(1 for r in done if r["correct"])

    print("\n" + "=" * 56)
    print("GAIA Eval 报告")
    print("=" * 56)
    print(f"模型: {os.environ.get('OPENAI_MODEL_ID', 'kimi-k2.5')} | 模式: {'goal' if use_goal else 'chat'} | seed={seed}")
    print(f"完成 {total} 题 | 异常 {len(errored)} 题 | 总耗时 {total_s / 60:.1f}m")
    if total:
        print(f"整体准确率: {correct}/{total} = {correct / total * 100:.1f}%")
        levels = sorted({r["level"] for r in done})
        for lv in levels:
            sub = [r for r in done if r["level"] == lv]
            c = sum(1 for r in sub if r["correct"])
            print(f"  Level {lv}: {c}/{len(sub)} ({c / len(sub) * 100:.1f}%)")
        methods = {}
        for r in done:
            methods[r["match_method"]] = methods.get(r["match_method"], 0) + 1
        print("匹配方式分布:", ", ".join(f"{k}={v}" for k, v in sorted(methods.items())))
    print("=" * 56)


def main() -> None:
    parser = argparse.ArgumentParser(description="GAIA 验证集评测 harness")
    parser.add_argument("--num", type=int, default=10, help="抽题数量（默认 10）")
    parser.add_argument("--stratified", action="store_true", help="按 Level 分层抽样（每层抽 --per-level 题）")
    parser.add_argument("--per-level", type=int, default=10, help="分层抽样时每层抽题数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="抽样种子（默认 42，可复现）")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS, help="每题 LLM 调用数上限")
    parser.add_argument("--timeout", type=float, default=QUESTION_TIMEOUT_S, help="每题墙钟上限（秒）")
    parser.add_argument("--no-goal", action="store_true", help="不使用 goal 循环（普通对话模式）")
    parser.add_argument("--path", type=Path, default=GAIA_PATH, help="GAIA jsonl 路径")
    args = parser.parse_args()

    rows = load_questions(args.path)
    no_file, with_file = split_questions(rows)

    strata_desc = ""
    if args.stratified:
        sampled, strata = sample_stratified(no_file, args.per_level, args.seed)
        strata_desc = "（分层: " + ", ".join(f"L{lv}×{n}" for lv, n in sorted(strata.items())) + "）"
    else:
        sampled = sample_questions(no_file, args.num, args.seed)

    print(f"加载 {len(rows)} 题 | 无附件 {len(no_file)} 题（抽样池）| 跳过附件题 {len(with_file)} 题")
    print(f"抽样 {len(sampled)} 题 {strata_desc} | seed={args.seed} | MAX_STEPS={args.max_steps} | 每题超时 {args.timeout:.0f}s")
    print(f"模式: {'goal（目标驱动 + goal_complete）' if not args.no_goal else 'chat（普通对话）'} | 预计耗时 10-30 分钟\n")

    start = time.monotonic()
    records = run_eval(
        sampled,
        use_goal=not args.no_goal,
        max_steps=args.max_steps,
        timeout_s=args.timeout,
    )
    total_s = time.monotonic() - start

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = EVAL_DIR / f"gaia_{stamp}.jsonl"
    write_results(records, with_file, out_path)
    print_report(records, seed=args.seed, use_goal=not args.no_goal, total_s=total_s)


if __name__ == "__main__":
    main()
