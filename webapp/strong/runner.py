"""Claude Agent SDK 调用抽象（可选依赖，全部 import 守卫）。

用户使用兼容 ANTHROPIC API 的第三方端点（base_url + api_key + model 自配于
config.local.yaml 的 ai.anthropic）。SDK 在无头模式下复用同一 agent loop，自动加载
工作目录的 CLAUDE.md / .claude / MCP。

【结构化输出策略】跨 SDK 版本稳健起见：指令模型只输出匹配目标形状的 JSON，再用容错
解析提取（与弱链 batch_generate 一致），不依赖特定版本的 output-schema 特性。

⚠️ SDK 调用点（_query_text）需在真实安装的 SDK 版本上 shadow-run 校准消息/内容块形状
（用户已同意上线前做平价评审）。未安装/未配置时 availability() 返回 False，路由降级为
「复制命令贴回 Claude Code」。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import shutil
import threading
import weakref
from pathlib import Path
from typing import Optional

from .. import config

_log = logging.getLogger("webapp.strong.runner")

# 强模型审计的并发上限（与 workflow 默认相近）。Semaphore 绑定事件循环，需按 loop/thread 隔离。
_SEMAPHORES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_SEMAPHORES_LOCK = threading.Lock()
# 按用户强模型端点（在强模型作业的 asyncio task 内 set；contextvar 每 task 隔离，并发安全）
_ENDPOINT: contextvars.ContextVar = contextvars.ContextVar("qa_anthropic_ep", default=None)

_PLACEHOLDER_KEYS = ("REPLACE_ME", "REPLACE_ME_OR_LEAVE_BLANK")
_LOCAL = threading.local()


class StrongModelOutputError(RuntimeError):
    """Raised when the strong model call finishes but does not produce parseable JSON."""


def set_endpoint(ep: Optional[dict]):
    """在强模型作业 task/thread 内调用：设定本次使用的端点（base_url/api_key/model）。"""
    _LOCAL.endpoint = ep
    return _ENDPOINT.set(ep)


def endpoint() -> dict:
    return getattr(_LOCAL, "endpoint", None) or _ENDPOINT.get() or config.anthropic_endpoint()


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _SEMAPHORES_LOCK:
        sem = _SEMAPHORES.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(6)
            _SEMAPHORES[loop] = sem
        return sem


def availability() -> tuple[bool, str]:
    return availability_for(config.anthropic_endpoint())


def claude_cli_present() -> bool:
    return shutil.which("claude") is not None


def _explicit(ep: dict) -> bool:
    """是否显式配全了自定义端点（base_url + api_key 都真）。"""
    return bool((ep or {}).get("base_url") and (ep or {}).get("api_key"))


def _provider(ep: dict) -> str:
    return ((ep or {}).get("provider") or "anthropic").strip().lower()


def availability_for(ep: dict) -> tuple[bool, str]:
    """可跑强模型：
    - provider=openai：装了 openai 包 ∧ 配全 base_url + api_key + model。
    - provider=anthropic：装了 claude-agent-sdk ∧（显式配全端点 或 本机有 claude CLI 可借其登录态）。
    """
    if _provider(ep) == "openai":
        try:
            import openai  # noqa: F401
        except Exception:
            return False, "选了 OpenAI 协议但未安装 openai 包（pip install openai）"
        if (ep or {}).get("base_url") and (ep or {}).get("api_key") and (ep or {}).get("model"):
            return True, "已配置（OpenAI 兼容）"
        return False, "OpenAI 复核未配全：需要接口地址 + API Key + 模型名"
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:
        return False, "未安装 claude-agent-sdk（pip install claude-agent-sdk）"
    if _explicit(ep):
        if not ep.get("model"):
            return False, "自定义端点缺少模型名"
        return True, "已配置（自定义端点）"
    if claude_cli_present():
        return True, "已就绪（复用 Claude Code 登录态）"
    return False, "未配置：在设置里填 复核模型 的端点 + API Key，或确保本机已装并登录 Claude Code"


def _extract_json(text: str):
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    # 回退：截取第一个 {...} 或 [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:  # noqa: BLE001
                continue
    return None


async def _query_text(prompt: str, *, system: Optional[str], allowed_tools: list[str],
                      cwd: Path) -> str:
    """跑一次 SDK query，拼接返回的文本块。SDK 未装则抛 RuntimeError。"""
    ep = endpoint()
    if _provider(ep) == "openai":
        return await _query_openai(prompt, system=system, ep=ep)
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"claude-agent-sdk 不可用：{e}")
    # headless 关键：① 不加载本仓库 .mcp.json 的 atlassian MCP（否则启动时去连 Jira 会挂起）；
    # ② 权限放行避免无 TTY 时等待审批；③ 禁写/禁 Bash（复核只读）；④ 限轮次防跑飞。
    opts_kwargs = dict(
        allowed_tools=allowed_tools,
        disallowed_tools=["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"],
        permission_mode="bypassPermissions",
        mcp_servers={},
        strict_mcp_config=True,
        max_turns=14,
        cwd=str(cwd),
    )
    if system:
        opts_kwargs["system_prompt"] = system
    if _explicit(ep):
        # 凭证只注入到 SDK 起的 CLI 子进程 env（per-call、随作业结束消失），绝不写本进程
        # 全局 os.environ —— 杜绝多用户/多作业间的凭证残留与并发竞态。
        opts_kwargs["env"] = {**os.environ,
                              "ANTHROPIC_BASE_URL": ep["base_url"],
                              "ANTHROPIC_API_KEY": ep["api_key"]}
        if ep.get("model"):
            opts_kwargs["model"] = ep["model"]
    # 不同 SDK 版本字段略有差异：逐步降级到可用集合，保证可构造 options（最后兜底去掉 env）
    options = None
    for drop in ([], ["strict_mcp_config"], ["strict_mcp_config", "mcp_servers"],
                 ["strict_mcp_config", "mcp_servers", "disallowed_tools", "max_turns"],
                 ["strict_mcp_config", "mcp_servers", "disallowed_tools", "max_turns", "env"]):
        try:
            options = ClaudeAgentOptions(**{k: v for k, v in opts_kwargs.items() if k not in drop})
            break
        except TypeError:
            continue
    if options is None:
        options = ClaudeAgentOptions(allowed_tools=allowed_tools, cwd=str(cwd))

    # ResultMessage.result 是完整最终答案；AssistantMessage.content 是分块文本。
    # 优先用 result（避免与分块重复拼接导致 JSON 跨两份副本而解析失败）；无 result 才回退拼块。
    block_parts: list[str] = []
    final_result: Optional[str] = None
    async with _semaphore():
        async for message in query(prompt=prompt, options=options):
            res = getattr(message, "result", None)
            if isinstance(res, str) and res.strip():
                final_result = res
                continue
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        block_parts.append(t)
            elif isinstance(content, str):
                block_parts.append(content)
    return final_result if final_result is not None else "".join(block_parts)


async def query_json(prompt: str, *, shape_hint: str, system: Optional[str] = None,
                     allowed_tools: Optional[list[str]] = None,
                     cwd: Optional[Path] = None) -> Optional[dict]:
    """跑一次 SDK query 并解析为 JSON 对象（容错）。失败返回 None。"""
    full = (
        prompt
        + "\n\n【输出格式】只输出一个 JSON 对象，匹配如下形状（不要任何解释/代码围栏）：\n"
        + shape_hint
    )
    text = await _query_text(full, system=system,
                             allowed_tools=allowed_tools or ["Read", "Grep", "Glob"],
                             cwd=cwd or config.REPO_ROOT)
    obj = _extract_json(text)
    if obj is None:
        # 解析失败不再静默当"空发现/干净"——否则复核会落成「已复核 0 条」。
        preview = (text or "")[:160]
        _log.warning("query_json 无法从模型输出解析 JSON（前 160 字符）：%s", preview)
        raise StrongModelOutputError(f"强模型输出不是可解析 JSON（前 160 字符：{preview!r}）")
    return obj


# ----------------------------- OpenAI 协议（Responses 为主 + chat 兜底，单轮只读） -----------------------------
# 复核/消解只让模型读嵌入工件→出 JSON，不挂工具、不改文件（写盘交确定性 Python）。
# 故无需 agent loop，直接打 /v1/responses（端点不认就回退 /v1/chat/completions），最大兼容。


class _ResponsesUnsupported(Exception):
    """端点未实现 /v1/responses（404/405 等）→ 回退 /v1/chat/completions。"""


def _status(e):
    c = getattr(e, "status_code", None)
    if c is None:
        c = getattr(getattr(e, "response", None), "status_code", None)
    return c


def _is_unsupported(e) -> bool:
    if _status(e) in (404, 405):
        return True
    if type(e).__name__ == "NotFoundError":
        return True
    m = str(e).lower()
    return ("not found" in m or "no such" in m or "does not exist" in m) and "response" in m


def _is_param_error(e) -> bool:
    if _status(e) not in (400, 422) and "badrequest" not in type(e).__name__.lower():
        return False
    m = str(e).lower()
    return any(k in m for k in ("max_tokens", "max_output_tokens", "max_completion_tokens",
                                "temperature", "unsupported parameter", "unsupported value",
                                "is not supported"))


def _is_server_error(e) -> bool:
    """网关 5xx：很多中转对未实现的 /v1/responses 用 5xx 而非 404 表达 → 也回退 chat。"""
    return _status(e) in (500, 501, 502, 503, 504)


_OPENAI_MAX_OUT = 16384


async def _openai_responses_async(client, model, system, prompt):
    responses = getattr(client, "responses", None)
    create = getattr(responses, "create", None)
    if create is None:
        raise _ResponsesUnsupported()
    base = {"model": model, "input": prompt}
    if system:
        base["instructions"] = system
    for extra in (dict(max_output_tokens=_OPENAI_MAX_OUT, temperature=0),
                  dict(max_output_tokens=_OPENAI_MAX_OUT), {}):
        try:
            r = await create(**base, **extra)
        except Exception as e:  # noqa: BLE001
            if _is_unsupported(e) or _is_server_error(e):
                raise _ResponsesUnsupported()  # 端点不实现 /responses 或网关 5xx → chat 兜底
            if _is_param_error(e):
                continue
            raise
        txt = r.output_text or ""
        # 空/截断绝不静默当“无结果”——显式抛错，让上层记入作业日志、不被当成 0 消解放行
        if not txt.strip() or getattr(r, "status", None) == "incomplete":
            raise RuntimeError(
                f"OpenAI(/v1/responses) 返回空或被截断（status={getattr(r, 'status', None)}；"
                f"max_output_tokens={_OPENAI_MAX_OUT} 可能不足，或推理模型耗尽预算）")
        return txt
    raise _ResponsesUnsupported()  # 参数穷尽仍 400 → chat 兜底


async def _openai_chat_async(client, model, system, prompt):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    last = None
    for extra in (dict(max_tokens=_OPENAI_MAX_OUT, temperature=0),
                  dict(max_completion_tokens=_OPENAI_MAX_OUT, temperature=0),
                  dict(max_completion_tokens=_OPENAI_MAX_OUT),
                  dict(max_tokens=_OPENAI_MAX_OUT), {}):
        try:
            r = await client.chat.completions.create(model=model, messages=msgs, **extra)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_param_error(e):
                raise
            continue
        ch = r.choices[0]
        txt = (getattr(ch, "message", None) and ch.message.content) or ""
        if not txt.strip() or getattr(ch, "finish_reason", None) == "length":
            raise RuntimeError(
                f"OpenAI(/v1/chat/completions) 返回空或被截断（finish_reason={getattr(ch, 'finish_reason', None)}；"
                f"max_tokens={_OPENAI_MAX_OUT} 可能不足，或推理模型耗尽预算）")
        return txt
    raise last


async def _query_openai(prompt: str, *, system, ep: dict, client_factory=None) -> str:
    if client_factory is None:
        try:
            from openai import AsyncOpenAI  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"openai SDK 不可用：{e}")
        client_factory = AsyncOpenAI
    client = client_factory(base_url=ep["base_url"], api_key=ep["api_key"],
                            timeout=600.0, max_retries=2)
    async with _semaphore():
        try:
            return await _openai_responses_async(client, ep["model"], system, prompt)
        except _ResponsesUnsupported:
            return await _openai_chat_async(client, ep["model"], system, prompt)
