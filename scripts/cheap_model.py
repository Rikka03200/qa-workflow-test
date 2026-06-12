#!/usr/bin/env python
"""
scripts/cheap_model.py

廉价批量子代理的模型客户端（多模型协作：弱模型批量生成 → 主 Claude 终检）。
通过 Anthropic 兼容 Messages API 直连阿里百炼 Token Plan 端点。

配置来源（优先级）：先读进程 env（CHEAP_MODEL_*），缺失再回退到
config/config.local.yaml 里的 ai.cheap_provider 段（经 _load_env.parse_config）。
只有 api_key 是机密；base_url / model 非机密。

用法：
  python scripts/cheap_model.py --smoke        连通自测（验证端点/鉴权/型号名）
  python scripts/cheap_model.py --info         仅打印当前生效配置（不含 key 明文）

作为库使用：
  from cheap_model import generate
  text, msg = generate(system="...", user="...", max_tokens=4096)
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

if sys.platform == "win32":  # 与仓库其它脚本一致，强制 UTF-8 以便中文输出
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_MODEL = "qwen3-max"
DEFAULT_MAX_TOKENS = 8192


def _config() -> dict[str, str]:
    """合并配置：进程 env 优先，缺关键项时回退 config.local.yaml。"""
    env = {k: v for k, v in os.environ.items() if k.startswith("CHEAP_MODEL_")}
    if not env.get("CHEAP_MODEL_API_KEY") or not env.get("CHEAP_MODEL_BASE_URL"):
        try:
            from _load_env import parse_config  # 同目录
            for k, v in parse_config().items():
                env.setdefault(k, v)
        except SystemExit:
            # config.local.yaml 不存在时 parse_config 会 exit；此处吞掉，
            # 让下面的缺失检查给出更友好的提示。
            pass
        except Exception:
            pass
    return env


def _provider(env: dict[str, str]) -> str:
    return (env.get("CHEAP_MODEL_PROVIDER") or "anthropic").strip().lower()


def get_client(env: dict[str, str] | None = None):
    env = env if env is not None else _config()
    base_url = env.get("CHEAP_MODEL_BASE_URL")
    api_key = env.get("CHEAP_MODEL_API_KEY")
    if not base_url or not api_key:
        raise SystemExit(
            "[cheap_model] 廉价模型未配置：请在 config/config.local.yaml 的 "
            "ai.cheap_provider 填好 base_url + api_key 并设 enabled: true。\n"
            "（Token Plan 专属 key，sk-...，与百炼通用 key 不通用。）"
        )
    if _provider(env) == "openai":
        try:
            import openai
        except ImportError:
            raise SystemExit("[cheap_model] 选了 OpenAI 协议(provider=openai)但缺少 openai SDK，运行：pip install openai")
        return openai.OpenAI(base_url=base_url, api_key=api_key, timeout=600.0, max_retries=2)
    try:
        import anthropic
    except ImportError:
        raise SystemExit("[cheap_model] 缺少 anthropic SDK，运行：pip install anthropic")
    return anthropic.Anthropic(base_url=base_url, api_key=api_key)


# ----------------------------- OpenAI 协议（Responses 为主 + chat 兜底） -----------------------------
# 单次（非 agentic）调用：模型只产文本/JSON，不挂工具、不改文件——与 anthropic 分支语义一致。


class _ResponsesUnsupported(Exception):
    """端点未实现 /v1/responses（404/405 等）→ 回退到 /v1/chat/completions。"""


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


def _norm_usage(u):
    """把 Responses(input_tokens/output_tokens) 与 Chat(prompt_tokens/completion_tokens) 归一到
    anthropic 风格字段，让 batch_generate 的 token 统计不改也能用。"""
    if u is None:
        return None
    it = getattr(u, "input_tokens", None)
    ot = getattr(u, "output_tokens", None)
    if it is None:
        it = getattr(u, "prompt_tokens", 0) or 0
    if ot is None:
        ot = getattr(u, "completion_tokens", 0) or 0
    return types.SimpleNamespace(input_tokens=it or 0, output_tokens=ot or 0,
                                 cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _openai_responses(client, model, system, user, max_tokens, temperature):
    base = {"model": model, "input": user}
    if system:
        base["instructions"] = system
    for extra in (dict(max_output_tokens=max_tokens, temperature=temperature),
                  dict(max_output_tokens=max_tokens), {}):
        try:
            r = client.responses.create(**base, **extra)
            return (r.output_text or ""), getattr(r, "usage", None)
        except Exception as e:  # noqa: BLE001
            if _is_unsupported(e) or _is_server_error(e):
                raise _ResponsesUnsupported()  # 端点不实现 /responses 或网关 5xx → chat 兜底
            if _is_param_error(e):
                continue
            raise
    raise _ResponsesUnsupported()  # 参数穷尽仍 400 → 让 chat 兜底（更普适）


def _openai_chat(client, model, system, user, max_tokens, temperature):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": user}]
    last = None
    for extra in (dict(max_tokens=max_tokens, temperature=temperature),
                  dict(max_completion_tokens=max_tokens, temperature=temperature),
                  dict(max_completion_tokens=max_tokens),
                  dict(max_tokens=max_tokens), {}):
        try:
            r = client.chat.completions.create(model=model, messages=msgs, **extra)
            return (r.choices[0].message.content or ""), getattr(r, "usage", None)
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_param_error(e):
                raise
    raise last


def _openai_generate(client, model, system, user, max_tokens, temperature):
    try:
        text, usage = _openai_responses(client, model, system, user, max_tokens, temperature)
    except _ResponsesUnsupported:
        text, usage = _openai_chat(client, model, system, user, max_tokens, temperature)
    return text, types.SimpleNamespace(usage=_norm_usage(usage))


def generate(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.0,
    env: dict[str, str] | None = None,
):
    """单次（非 agentic）生成。返回 (拼接后的文本, 原始 message 对象)。"""
    env = env if env is not None else _config()
    client = get_client(env)
    model = model or env.get("CHEAP_MODEL_NAME") or DEFAULT_MODEL
    if max_tokens is None:
        max_tokens = int(env.get("CHEAP_MODEL_MAX_TOKENS") or DEFAULT_MAX_TOKENS)
    if _provider(env) == "openai":
        return _openai_generate(client, model, system, user, max_tokens, temperature)
    # prompt 缓存：给大 system 块加 cache_control（跨工单/跨轮次复用同一 system 前缀省 token）。
    # 百炼若不支持会忽略；如报错可设 env CHEAP_MODEL_CACHE=false 关闭。
    cache_on = str(env.get("CHEAP_MODEL_CACHE", "true")).lower() not in ("false", "0", "no")
    system_param = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache_on and system else system
    )
    msg_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_param,
        messages=[{"role": "user", "content": user}],
    )
    # 用流式：避免 anthropic SDK 对“可能超 10 分钟”的非流式请求直接报错，
    # 且思考型模型 + 大 max_tokens 生成较慢。text_stream 只产出回答文本（不含思考）。
    text_parts: list[str] = []
    with client.messages.stream(**msg_kwargs) as stream:
        for delta in stream.text_stream:
            text_parts.append(delta)
        msg = stream.get_final_message()
    text = "".join(text_parts)
    if not text:  # 兜底：从最终 message 的 text 块取
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return text, msg


def _info() -> None:
    env = _config()
    base = env.get("CHEAP_MODEL_BASE_URL") or "(未配置)"
    model = env.get("CHEAP_MODEL_NAME") or DEFAULT_MODEL
    key = env.get("CHEAP_MODEL_API_KEY")
    enabled = env.get("CHEAP_MODEL_ENABLED", "(未设置)")
    print(f"[info] enabled  = {enabled}")
    print(f"[info] provider = {_provider(env)}")
    print(f"[info] base_url = {base}")
    print(f"[info] model    = {model}")
    print(f"[info] max_tokens = {env.get('CHEAP_MODEL_MAX_TOKENS') or DEFAULT_MAX_TOKENS}")
    print(f"[info] api_key  = {'已配置 (len=%d)' % len(key) if key else '未配置'}")  # 不打印明文


def _smoke() -> int:
    env = _config()
    base = env.get("CHEAP_MODEL_BASE_URL") or "(未配置)"
    model = env.get("CHEAP_MODEL_NAME") or DEFAULT_MODEL
    print(f"[smoke] provider={_provider(env)}  base_url={base}  model={model}")
    t0 = time.time()
    try:
        text, msg = generate(
            system="You are a connectivity test. Reply with exactly: OK",
            user="Reply with exactly: OK",
            max_tokens=16,
        )
    except SystemExit as e:
        print(str(e))
        return 1
    except Exception as e:  # 网络/鉴权/型号名错误等
        print(f"[smoke] ❌ 调用失败：{type(e).__name__}: {e}")
        return 1
    dt = time.time() - t0
    print(f"[smoke] reply={text!r}  in {dt:.1f}s")
    usage = getattr(msg, "usage", None)
    if usage:
        print(
            f"[smoke] usage: in={getattr(usage, 'input_tokens', '?')} "
            f"out={getattr(usage, 'output_tokens', '?')}"
        )
    if text.strip():
        print("[smoke] ✅ 连通成功")
        return 0
    print("[smoke] ⚠️ 空响应——检查 model 名是否在 Token Plan 支持列表内")
    return 1


def main() -> int:
    if "--smoke" in sys.argv:
        return _smoke()
    if "--info" in sys.argv:
        _info()
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
