"""search 工具可用性测试

用法:
    uv run python tests/test_search.py             # 全部用例（含真实网络调用）
    uv run python tests/test_search.py --offline   # 只跑离线用例（mock，不需要 key 和网络）

分两层:
    离线用例 —— mock 掉两个搜索通道，验证调度逻辑（通道选择、回退、归一化、错误信息）
    在线用例 —— 真实调用搜索通道，验证可用性、返回结构、延迟、中英文查询

不依赖 pytest（本仓无测试框架），直接 python 运行，退出码非 0 表示有用例失败。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# 从 tests/ 子目录运行时，项目根不在 sys.path 上，先补进去
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# tools/builtins/__init__.py 里 `from .search import search` 会把同名子模块遮蔽掉
# （`import tools.builtins.search` 拿到的是函数而非模块），所以用 import_module 绕开。
S = importlib.import_module("tools.builtins.search")
from tools.executor import ToolExecutor  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    """跑一个用例，记录成败，不让异常打断整批。"""
    t0 = time.monotonic()
    try:
        note = fn() or ""
        RESULTS.append((name, True, note))
        print(f"  ✅ {name}" + (f"  [{note}]" if note else "") + f"  {time.monotonic() - t0:.2f}s")
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print(f"  ❌ {name}  → 断言失败: {e}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name}  → {type(e).__name__}: {e}")


# ------------------------------------------------------------------ 公共工具

def _fake_tavily_response(results: list[dict]) -> MagicMock:
    """构造一个可当上下文管理器用的假 HTTP 响应。"""
    resp = MagicMock()
    resp.read.return_value = json.dumps({"results": results}).encode("utf-8")
    resp.__enter__ = lambda s: resp
    resp.__exit__ = lambda *a: None
    return resp


def _assert_shape(rows: list[dict], *, expect: int | None = None) -> str:
    """校验返回结构：{title, href, body} 且都是 str。返回一句摘要。"""
    assert rows, "结果为空"
    if expect is not None:
        assert len(rows) <= expect, f"max_results={expect} 但返回了 {len(rows)} 条"
    for i, r in enumerate(rows):
        assert set(r) == {"title", "href", "body"}, f"第{i}条字段异常: {sorted(r)}"
        for k, v in r.items():
            assert isinstance(v, str), f"第{i}条 {k} 不是 str: {type(v).__name__}"
    return f"{len(rows)} 条, body 均长 {sum(len(r['body']) for r in rows) // len(rows)} 字符"


# ------------------------------------------------------------------ 离线用例

def offline_tests() -> None:
    print("\n【离线】调度逻辑（mock，不联网）")

    def t_key_priority():
        with patch.dict(os.environ, {"TAVILY_KEY": "alias-key"}, clear=False):
            os.environ.pop("TAVILY_API_KEY", None)
            assert S._tavily_key() == "alias-key", "简写 TAVILY_KEY 未被识别"
            os.environ["TAVILY_API_KEY"] = "canonical-key"
            assert S._tavily_key() == "canonical-key", "TAVILY_API_KEY 应优先于 TAVILY_KEY"
            del os.environ["TAVILY_API_KEY"]
            os.environ.pop("TAVILY_KEY", None)
            assert S._tavily_key() == "", "无 key 时应返回空串"
    check("key 读取优先级 TAVILY_API_KEY > TAVILY_KEY", t_key_priority)

    def t_normalize():
        """Tavily 的 {title,url,content} 必须映射成 ddgs 的 {title,href,body}。"""
        fake = _fake_tavily_response([
            {"title": "T1", "url": "https://a.com", "content": "正文一"},
            {"title": "T2", "url": "https://b.com", "content": "正文二"},
        ])
        with patch.object(S.urllib.request, "urlopen", return_value=fake):
            rows = S._search_tavily("q", 2)
        assert rows == [
            {"title": "T1", "href": "https://a.com", "body": "正文一"},
            {"title": "T2", "href": "https://b.com", "body": "正文二"},
        ], f"字段映射错误: {rows}"
    check("Tavily 响应归一化为 {title,href,body}", t_normalize)

    def t_request_shape():
        """请求必须带 Bearer 鉴权头、正确的 URL 和请求体。"""
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = json.loads(req.data.decode())
            return _fake_tavily_response([{"title": "x", "url": "y", "content": "z"}])

        with patch.dict(os.environ, {"TAVILY_API_KEY": "k1"}, clear=False), \
                patch.object(S.urllib.request, "urlopen", fake_urlopen):
            S._search_tavily("hello world", 7)

        assert seen["url"] == S.TAVILY_ENDPOINT, seen["url"]
        assert seen["auth"] == "Bearer k1", seen["auth"]
        assert seen["body"]["query"] == "hello world", seen["body"]
        assert seen["body"]["max_results"] == 7, seen["body"]
    check("Tavily 请求 URL/鉴权头/请求体正确", t_request_shape)

    def t_prefers_tavily():
        """有 key 时应走 Tavily，且不碰 bing。"""
        called = []
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}, clear=False), \
                patch.object(S, "_search_tavily", lambda q, n: called.append("tavily") or [{"title": "t", "href": "h", "body": "b"}]), \
                patch.object(S, "_search_bing", lambda q, n: called.append("bing") or []):
            rows = S.search("q", 1)
        assert called == ["tavily"], f"调用序列异常: {called}"
        assert rows[0]["title"] == "t"
    check("有 key → 走 Tavily 且不调 bing", t_prefers_tavily)

    def t_fallback_on_tavily_error():
        """Tavily 抛错 → 应回退 bing，并返回 bing 的结果。"""
        called = []
        def boom(q, n):
            called.append("tavily")
            raise RuntimeError("HTTP 401: invalid key")
        with patch.dict(os.environ, {"TAVILY_API_KEY": "bad"}, clear=False), \
                patch.object(S, "_search_tavily", boom), \
                patch.object(S, "_search_bing", lambda q, n: called.append("bing") or [{"title": "from-bing", "href": "h", "body": "b"}]), \
                patch("sys.stderr"):  # 吞掉回退告警，避免污染测试输出
            rows = S.search("q", 1)
        assert called == ["tavily", "bing"], f"调用序列异常: {called}"
        assert rows[0]["title"] == "from-bing"
    check("Tavily 失败 → 自动回退 bing", t_fallback_on_tavily_error)

    def t_no_key_skips_tavily():
        """无 key 时不应调用 Tavily。"""
        called = []
        env = {k: v for k, v in os.environ.items() if k not in S.TAVILY_KEY_NAMES}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(S, "_search_tavily", lambda q, n: called.append("tavily") or []), \
                patch.object(S, "_search_bing", lambda q, n: called.append("bing") or [{"title": "b", "href": "h", "body": "x"}]):
            S.search("q", 1)
        assert called == ["bing"], f"调用序列异常: {called}"
    check("无 key → 跳过 Tavily 直接 bing", t_no_key_skips_tavily)

    def t_both_fail_raises():
        """两通道都失败 → 抛 SearchUnavailableError，且信息对模型可行动。"""
        env = {k: v for k, v in os.environ.items() if k not in S.TAVILY_KEY_NAMES}
        with patch.dict(os.environ, {**env, "TAVILY_API_KEY": "k"}, clear=True), \
                patch.object(S, "_search_tavily", lambda q, n: (_ for _ in ()).throw(RuntimeError("401"))), \
                patch.object(S, "_search_bing", lambda q, n: (_ for _ in ()).throw(RuntimeError("timeout"))), \
                patch("sys.stderr"), \
                patch.object(S.time, "sleep"):
            try:
                S.search("q", 1)
            except S.SearchUnavailableError as e:
                msg = str(e)
                assert "Tavily" in msg and "bing" in msg, f"未同时保留两条通道的原因: {msg}"
                assert "不要再次调用 search" in msg, f"缺少行动建议: {msg}"
                return
            raise AssertionError("应当抛出 SearchUnavailableError")
    check("两通道全挂 → 可行动的错误信息", t_both_fail_raises)

    def t_empty_results_is_failure():
        """返回空列表应视为失败（None → 触发回退/重试），而不是当成成功返回 []。"""
        calls = []
        def empty(q, n):
            calls.append(1)
            return []
        env = {k: v for k, v in os.environ.items() if k not in S.TAVILY_KEY_NAMES}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(S, "_search_bing", empty), \
                patch.object(S.time, "sleep"):
            try:
                S.search("q", 1)
            except S.SearchUnavailableError:
                assert len(calls) == S.BING_ATTEMPTS, f"应重试 {S.BING_ATTEMPTS} 次，实际 {len(calls)}"
                return
            raise AssertionError("空结果应当报错")
    check("空结果视为失败并触发重试", t_empty_results_is_failure)

    def t_executor_integration():
        """经 ToolExecutor 走一遍模型实际路径：失败应表现为 is_error=True。"""
        ex = ToolExecutor()
        tc = ex.parse_tool_calls({"tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "search", "arguments": '{"query": "q"}'},
        }]})[0]
        env = {k: v for k, v in os.environ.items() if k not in S.TAVILY_KEY_NAMES}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(S, "_search_bing", lambda q, n: (_ for _ in ()).throw(RuntimeError("boom"))), \
                patch.object(S.time, "sleep"):
            res = ex.execute(tc)
        assert res.is_error is True, "失败时 is_error 应为 True"
        assert res.tool_call_id == "c1", res.tool_call_id
        msg = res.to_message()
        assert msg["role"] == "tool" and msg["tool_call_id"] == "c1", msg
    check("经 ToolExecutor 后 is_error / tool_call_id 正确", t_executor_integration)


# ------------------------------------------------------------------ 在线用例

def online_tests() -> None:
    channel = S.backend_name()
    print(f"\n【在线】真实搜索（当前通道: {channel}）")

    def t_basic():
        t0 = time.monotonic()
        rows = S.search("Python 3.14 release date", max_results=5)
        note = _assert_shape(rows, expect=5)
        return f"{time.monotonic() - t0:.2f}s, {note}"
    check("英文查询返回结构正确", t_basic)

    def t_body_substantive():
        """body 必须有实质内容 —— Tavily 的价值就在于正文已抽取。"""
        rows = S.search("What is the Model Context Protocol", max_results=3)
        _assert_shape(rows)
        avg = sum(len(r["body"]) for r in rows) / len(rows)
        assert avg > 50, f"body 过短（均值 {avg:.0f} 字符），可能是空摘要"
        assert any(r["href"].startswith("http") for r in rows), "href 不是合法 URL"
        return f"body 均值 {avg:.0f} 字符"
    check("body 有实质内容且 href 合法", t_body_substantive)

    def t_chinese_query():
        """GAIA 里含大量非英文查询，中文必须可用。"""
        rows = S.search("日本ハムファイターズ 2023 背番号 玉井大翔", max_results=3)
        return _assert_shape(rows)
    check("中文/日文查询可用", t_chinese_query)

    def t_max_results():
        rows = S.search("linux kernel", max_results=2)
        _assert_shape(rows, expect=2)
        return f"{len(rows)} 条"
    check("max_results 被尊重", t_max_results)

    def t_latency():
        """连续 3 次延迟必须显著低于当前通道的硬超时，否则会误判失败并白白回退。

        这条断言存在的意义：通道变慢时（网络劣化 / 服务降级）应当被测出来，
        而不是等它在生产里悄悄退化成一堆假超时。
        """
        times = []
        for i in range(3):
            t0 = time.monotonic()
            S.search(f"python release notes {i}", max_results=3)
            times.append(time.monotonic() - t0)
        avg = sum(times) / len(times)
        cap = S.TAVILY_HARD_TIMEOUT_S if S._tavily_key() else S.BING_HARD_TIMEOUT_S
        assert avg < cap, f"平均 {avg:.1f}s 已达硬超时 {cap}s —— 会被误判为失败并回退"
        return f"平均 {avg:.2f}s（上限 {cap}s，余量 {cap / avg:.1f}×）"
    check("延迟显著低于硬超时上限", t_latency)


# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="search 工具可用性测试")
    parser.add_argument("--offline", action="store_true", help="只跑离线用例（不联网）")
    args = parser.parse_args()

    has_key = bool(S._tavily_key())
    print("=" * 64)
    print("search 工具可用性测试")
    print("=" * 64)
    print(f"当前通道: {S.backend_name()}")
    print(f"TAVILY_API_KEY: {'已配置' if has_key else '未配置（将走 bing 回退通道，较慢）'}")

    offline_tests()
    if not args.offline:
        online_tests()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"结果: {passed}/{total} 通过")
    for name, ok, note in RESULTS:
        if not ok:
            print(f"  ❌ {name}\n     {note}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
