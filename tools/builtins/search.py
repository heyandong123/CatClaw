"""搜索工具 —— 双通道：Tavily API 优先，未配置或失败时回退 bing（ddgs）

通道选择：
    设置了 TAVILY_API_KEY  → 走 Tavily（1-3s，且返回已抽取的正文，模型通常不用再抓页面）
    未设置 / Tavily 失败   → 回退 ddgs 的 bing 后端（实测 15-30s，慢但免费且无需 key）

两个通道的返回值统一归一化成 ddgs 的字段名 {title, href, body}，
这样模型在两条通道下看到的结构完全一致。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List

from ddgs import DDGS
from dotenv import load_dotenv

load_dotenv()  # 让本模块单独运行时也能读到 .env

TAVILY_ENDPOINT = "https://api.tavily.com/search"
# TAVILY_API_KEY 是 Tavily 官方文档的命名；TAVILY_KEY 是常见简写，一并接受（前者优先）
TAVILY_KEY_ENV = "TAVILY_API_KEY"
TAVILY_KEY_NAMES = ("TAVILY_API_KEY", "TAVILY_KEY")

# ddgs 的 8 个 text 后端里，本机网络下只有 bing 能返回结果：
#   bing                                     ✅ 15-30s
#   duckduckgo / google / yahoo / wikipedia  ❌ No results（被风控）
#   brave / mojeek / yandex                  ❌ 连接超时
# ddgs 默认 backend="auto" 会把这些引擎分批全试一遍，白白多等 20 秒 —— 所以固定单后端。
BING_BACKEND = "bing"
BING_ATTEMPTS = 2       # bing 通道的总尝试次数

# 墙钟硬上限按通道分开设：
#   Tavily 实测 7-10s（本机到 Tavily 服务器的网络往返为主）—— 卡 10s 余量太小会误判失败
#   bing 实测 14-30s（成功案例最慢 25s，失败案例 30.7s）—— 卡 30s 会把成功的搜索砍掉
TAVILY_HARD_TIMEOUT_S = 20
BING_HARD_TIMEOUT_S = 35
CONNECT_TIMEOUT_S = 10  # 传给 HTTP 客户端的连接/读取超时
RETRY_WAIT_S = 1.0

# 模块级线程池：两个通道都是同步阻塞调用，只能用线程来封顶总耗时。
# 故意不用 with（ThreadPoolExecutor.__exit__ 会 shutdown(wait=True)，超时就变成永久阻塞）。
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="search")


class SearchUnavailableError(RuntimeError):
    """搜索不可用。错误信息面向 LLM —— 必须明确告诉它"别再重试"。"""


# 只在首次回退时告警：key 配错会被静默降级成慢速 bing，
# 不提示的话用户只会觉得"搜索怎么这么慢"，永远查不到根因。
_tavily_warned = False


def _warn_tavily_fallback(err: str) -> None:
    global _tavily_warned
    if _tavily_warned:
        return
    _tavily_warned = True
    print(
        f"⚠️  [search] {TAVILY_KEY_ENV} 已配置但 Tavily 调用失败，本次及后续搜索将回退到 bing（慢）：{err}\n"
        f"    检查 key 是否有效 / 额度是否用尽；不需要 Tavily 就把它从 .env 删掉以消除本提示。",
        file=sys.stderr,
    )


# ------------------------------------------------------------------ 通道实现

def _tavily_key() -> str:
    """读取 API key，按 TAVILY_KEY_NAMES 顺序取第一个非空的。

    每次调用时读、不缓存 —— 方便测试和运行时改 env。
    """
    for name in TAVILY_KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _search_tavily(query: str, max_results: int) -> List[Dict[str, Any]]:
    """Tavily API 通道。返回归一化后的结果列表。"""
    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }).encode("utf-8")

    req = urllib.request.Request(
        TAVILY_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {_tavily_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx/5xx 的响应体里通常写着原因（key 无效 / 额度用尽），带出来便于排查
        detail = e.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e

    # Tavily 字段 {title, url, content} → ddgs 字段 {title, href, body}
    return [
        {
            "title": r.get("title", ""),
            "href": r.get("url", ""),
            "body": r.get("content", ""),
        }
        for r in data.get("results", [])
    ]


def _search_bing(query: str, max_results: int) -> List[Dict[str, Any]]:
    """ddgs/bing 通道（回退用）。ddgs 本身返回的就是 {title, href, body}。"""
    with DDGS(timeout=CONNECT_TIMEOUT_S) as ddgs:
        return list(ddgs.text(query, max_results=max_results, backend=BING_BACKEND))


def _attempt(
    fn: Callable[..., List[Dict[str, Any]]],
    query: str,
    max_results: int,
    hard_timeout: float,
) -> List[Dict[str, Any]]:
    """跑一次搜索通道并施加墙钟硬超时。返回空结果视为失败（抛异常）。"""
    results = _POOL.submit(fn, query, max_results).result(hard_timeout)
    if not results:
        raise RuntimeError("返回 0 条结果")
    return results


# ------------------------------------------------------------------ 对外接口

def search(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """搜索网页，返回 [{title, href, body}, ...]。

    先试 Tavily（若配置了 key），失败或无 key 则回退 bing。

    Raises:
        SearchUnavailableError: 两个通道都失败。异常信息包含给 LLM 的行动建议。
    """
    errors: list[str] = []

    if _tavily_key():
        try:
            return _attempt(_search_tavily, query, max_results, TAVILY_HARD_TIMEOUT_S)
        except FutureTimeout:
            err = f"超过 {TAVILY_HARD_TIMEOUT_S}s 墙钟上限"
        except Exception as e:  # noqa: BLE001 - 网络/额度/key 异常统一降级为回退
            err = f"{type(e).__name__}: {e}"
        errors.append(f"Tavily 失败（{err}）")
        _warn_tavily_fallback(err)
    else:
        errors.append(f"未配置 {TAVILY_KEY_ENV}")

    for i in range(1, BING_ATTEMPTS + 1):
        try:
            return _attempt(_search_bing, query, max_results, BING_HARD_TIMEOUT_S)
        except FutureTimeout:
            err = f"超过 {BING_HARD_TIMEOUT_S}s 墙钟上限"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        errors.append(f"bing 第 {i}/{BING_ATTEMPTS} 次（{err}）")
        if i < BING_ATTEMPTS:
            time.sleep(RETRY_WAIT_S)

    raise SearchUnavailableError(
        f"搜索服务不可用（{'；'.join(errors)}）。"
        "不要再次调用 search —— 重试不会成功，只是浪费轮次。"
        "请改用 bash 直接抓取具体网页（如 `curl -sL --max-time 20 <url>`），"
        "或基于已有信息与自身知识作答。"
    )


def backend_name() -> str:
    """当前生效的搜索通道名（供工具描述使用，让模型知道该不该频繁搜索）。"""
    return "Tavily API (fast)" if _tavily_key() else "ddgs/bing (slow, 15-30s per call)"


if __name__ == "__main__":
    print("当前通道:", backend_name())
    for r in search("python programming", max_results=3):
        print(f"- {r['title']}\n  {r['href']}")
