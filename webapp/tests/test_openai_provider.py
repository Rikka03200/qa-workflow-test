"""OpenAI provider 回归：/v1/responses 为主 + /v1/chat/completions 兜底、参数降级、
usage 归一、provider 路由、跨协议不串密钥。全部用假客户端，不打真实网络。"""

import asyncio
import sys
import types

import pytest

from webapp import auth, config, deps
from webapp.strong import runner

# 让测试能直接 import scripts/cheap_model（与其它脚本一致的加载方式）
sys.path.insert(0, str(config.SCRIPTS_DIR))
import cheap_model  # noqa: E402


# ----------------------------- 假客户端 / 假异常 -----------------------------

class _NotFound(Exception):
    status_code = 404


class _BadParam(Exception):
    status_code = 400


class _ServerErr(Exception):
    status_code = 500


def _resp_obj(text, it=0, ot=0):
    return types.SimpleNamespace(
        output_text=text,
        usage=types.SimpleNamespace(input_tokens=it, output_tokens=ot))


def _chat_obj(text, pt=0, ct=0):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))],
        usage=types.SimpleNamespace(prompt_tokens=pt, completion_tokens=ct))


def _fake_client(responses_create, chat_create):
    return types.SimpleNamespace(
        responses=types.SimpleNamespace(create=responses_create),
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=chat_create)),
    )


# ----------------------------- auth：provider 持久化 -----------------------------

def test_ai_view_default_provider_anthropic(tmp_path):
    store = auth.UserStore(tmp_path / "u.json")
    salt, h = auth.hash_password("x")
    store.upsert(auth.User(username="bob", salt=salt, pwd_hash=h))
    assert store.get("bob").ai_view()["weak"]["provider"] == "anthropic"


def test_set_ai_provider_openai_roundtrip(tmp_path):
    store = auth.UserStore(tmp_path / "u.json")
    salt, h = auth.hash_password("x")
    store.upsert(auth.User(username="bob", salt=salt, pwd_hash=h))
    store.set_ai("bob", "weak", "https://x/v1", "gpt-4o", "sk-1", provider="openai")
    v = store.get("bob").ai_view()["weak"]
    assert v["provider"] == "openai" and v["model"] == "gpt-4o" and v["has_key"] is True
    # 非法 provider 不覆盖已有值
    store.set_ai("bob", "weak", "https://x/v1", "gpt-4o", None, provider="garbage")
    assert store.get("bob").ai_view()["weak"]["provider"] == "openai"


# ----------------------------- deps：注入 / 端点合并 -----------------------------

def test_subprocess_env_injects_provider():
    u = auth.User(username="z", display_name="Z",
                  ai={"weak": {"base_url": "https://x/v1", "api_key": "k",
                               "model": "gpt-4o", "provider": "openai"}})
    env = deps.subprocess_env(u)
    assert env["CHEAP_MODEL_PROVIDER"] == "openai"
    assert env["CHEAP_MODEL_BASE_URL"] == "https://x/v1"
    assert env["CHEAP_MODEL_NAME"] == "gpt-4o"


def test_subprocess_env_weak_cross_protocol_guard(monkeypatch):
    # 用户选 openai 但没填 key，全局是 anthropic → 注入用户协议、不串全局 key、不误启用
    monkeypatch.setattr(deps, "_global_cheap_provider", lambda: "anthropic")
    u = auth.User(username="z", ai={"weak": {"base_url": "https://o/v1",
                                             "model": "gpt-4o", "provider": "openai"}})
    env = deps.subprocess_env(u)
    assert env["CHEAP_MODEL_PROVIDER"] == "openai"
    assert env["CHEAP_MODEL_BASE_URL"] == "https://o/v1"
    assert "CHEAP_MODEL_API_KEY" not in env       # 绝不串用全局 anthropic key
    assert env.get("CHEAP_MODEL_ENABLED") != "true"  # 未配全 → 子进程明确报「未配置」


def test_subprocess_env_weak_same_protocol_incomplete_no_inject(monkeypatch):
    # 同协议但未配全 → 不注入用户覆盖（安全回退全局同协议），不误启用
    monkeypatch.setattr(deps, "_global_cheap_provider", lambda: "anthropic")
    u = auth.User(username="z", ai={"weak": {"base_url": "https://a/anthropic",
                                             "provider": "anthropic"}})
    env = deps.subprocess_env(u)
    assert env.get("CHEAP_MODEL_ENABLED") != "true"


def test_user_endpoint_cross_protocol_no_key_bleed(monkeypatch):
    # 全局 anthropic；用户切 openai 但未填 key → 绝不回退串用全局 anthropic 的 key
    monkeypatch.setattr(config, "anthropic_endpoint",
                        lambda: {"base_url": "https://a/anthropic", "api_key": "ANTH",
                                 "model": "claude-x", "provider": "anthropic"})
    u = auth.User(username="z", ai={"strong": {"base_url": "https://o/v1",
                                               "model": "gpt-4o", "provider": "openai"}})
    ep = deps.user_anthropic_endpoint(u)
    assert ep["provider"] == "openai" and ep["base_url"] == "https://o/v1"
    assert ep["api_key"] is None  # 没串用全局 ANTH


def test_user_endpoint_same_protocol_field_fallback(monkeypatch):
    monkeypatch.setattr(config, "anthropic_endpoint",
                        lambda: {"base_url": "https://a/anthropic", "api_key": "ANTH",
                                 "model": "claude-x", "provider": "anthropic"})
    u = auth.User(username="z", ai={"strong": {"base_url": "", "model": "claude-y",
                                               "provider": "anthropic"}})
    ep = deps.user_anthropic_endpoint(u)
    assert ep["api_key"] == "ANTH" and ep["model"] == "claude-y"  # 同协议逐字段回退


# ----------------------------- config / runner：可用性 -----------------------------

def test_anthropic_endpoint_has_provider():
    assert config.anthropic_endpoint().get("provider") in ("anthropic", "openai")


def test_strong_model_available_delegates():
    ok, reason = config.strong_model_available()
    assert isinstance(ok, bool) and isinstance(reason, str)


def test_availability_openai_branch():
    ok, _ = runner.availability_for(
        {"provider": "openai", "base_url": "u", "api_key": "k", "model": "m"})
    assert ok is True
    ok2, _ = runner.availability_for({"provider": "openai", "base_url": "u"})  # 缺 key/model
    assert ok2 is False


# ----------------------------- cheap_model（弱链，同步） -----------------------------

def test_cheap_openai_responses_happy():
    client = _fake_client(lambda **k: _resp_obj("HELLO", 5, 3), None)
    text, msg = cheap_model._openai_generate(client, "gpt-4o", "sys", "u", 100, 0.0)
    assert text == "HELLO"
    assert msg.usage.input_tokens == 5 and msg.usage.output_tokens == 3


def test_cheap_openai_fallback_to_chat():
    def resp_unsup(**k):
        raise _NotFound("/v1/responses not found")
    client = _fake_client(resp_unsup, lambda **k: _chat_obj("VIACHAT", 4, 2))
    text, msg = cheap_model._openai_generate(client, "gpt-4o", "sys", "u", 100, 0.0)
    assert text == "VIACHAT"
    assert msg.usage.input_tokens == 4 and msg.usage.output_tokens == 2  # prompt/completion 归一


def test_cheap_openai_fallback_on_500():
    # 中转对 /v1/responses 报 5xx → 也应回退到 chat
    def resp_500(**k):
        raise _ServerErr("Database error, please contact the administrator")
    client = _fake_client(resp_500, lambda **k: _chat_obj("VIACHAT500"))
    text, _ = cheap_model._openai_generate(client, "gpt-4o", "s", "u", 100, 0.0)
    assert text == "VIACHAT500"


def test_cheap_openai_param_degrade():
    seen = []

    def resp(**k):
        seen.append(dict(k))
        if "temperature" in k:
            raise _BadParam("Unsupported value: temperature")
        return _resp_obj("OK")
    client = _fake_client(resp, None)
    text, _ = cheap_model._openai_generate(client, "m", None, "u", 50, 0.0)
    assert text == "OK"
    assert any("temperature" not in s for s in seen)  # 降级后去掉了 temperature


def test_cheap_generate_routes_openai(monkeypatch):
    client = _fake_client(lambda **k: _resp_obj("ROUTED"), None)
    monkeypatch.setattr(cheap_model, "get_client", lambda env=None: client)
    env = {"CHEAP_MODEL_PROVIDER": "openai", "CHEAP_MODEL_BASE_URL": "u",
           "CHEAP_MODEL_API_KEY": "k", "CHEAP_MODEL_NAME": "gpt-4o"}
    text, _ = cheap_model.generate("sys", "user", env=env)
    assert text == "ROUTED"


# ----------------------------- runner（强链，异步） -----------------------------

def test_runner_openai_responses_async():
    async def rc(**k):
        return _resp_obj("ARESP")
    out = asyncio.run(runner._openai_responses_async(_fake_client(rc, None), "m", "sys", "p"))
    assert out == "ARESP"


def test_runner_query_openai_fallback(monkeypatch):
    async def rc(**k):
        raise _NotFound("/v1/responses not found")

    async def cc(**k):
        return _chat_obj("ACHAT")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda *a, **k: _fake_client(rc, cc))
    out = asyncio.run(runner._query_openai(
        "p", system="s", ep={"base_url": "u", "api_key": "k", "model": "m"}))
    assert out == "ACHAT"


def test_runner_query_openai_fallback_on_500(monkeypatch):
    async def rc(**k):
        raise _ServerErr("Database error, please contact the administrator")

    async def cc(**k):
        return _chat_obj("ACHAT500")
    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda *a, **k: _fake_client(rc, cc))
    out = asyncio.run(runner._query_openai(
        "p", system="s", ep={"base_url": "u", "api_key": "k", "model": "m"}))
    assert out == "ACHAT500"


def test_runner_query_text_routes_openai(monkeypatch):
    async def fake_q(prompt, *, system, ep):
        return "ROUTED-OPENAI"
    monkeypatch.setattr(runner, "_query_openai", fake_q)
    runner.set_endpoint({"provider": "openai", "base_url": "u", "api_key": "k", "model": "m"})
    try:
        out = asyncio.run(runner._query_text("p", system=None, allowed_tools=[],
                                             cwd=config.REPO_ROOT))
        assert out == "ROUTED-OPENAI"
    finally:
        runner.set_endpoint(None)


def test_runner_openai_responses_empty_raises():
    async def rc(**k):
        return _resp_obj("")  # 空输出绝不静默放行
    with pytest.raises(RuntimeError):
        asyncio.run(runner._openai_responses_async(_fake_client(rc, None), "m", None, "p"))


def test_runner_openai_responses_truncated_raises():
    async def rc(**k):
        r = _resp_obj("partial")
        r.status = "incomplete"  # 预算耗尽/截断
        return r
    with pytest.raises(RuntimeError):
        asyncio.run(runner._openai_responses_async(_fake_client(rc, None), "m", None, "p"))


def test_runner_openai_chat_truncated_raises():
    async def cc(**k):
        o = _chat_obj("partial")
        o.choices[0].finish_reason = "length"
        return o
    with pytest.raises(RuntimeError):
        asyncio.run(runner._openai_chat_async(_fake_client(None, cc), "m", None, "p"))
