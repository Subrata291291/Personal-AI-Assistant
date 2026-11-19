import logging
from types import SimpleNamespace

import pytest

from app.llm.fallback import FallbackManager, LLMFallbackError
from app.llm.schemas import LLMRequest, LLMResponse, LLMMessage


class FakeProvider:
    def __init__(self, name, model, behavior):
        self.provider_name = name
        self.model = model
        self._behavior = behavior
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        result = self._behavior(request)
        if isinstance(result, Exception):
            raise result
        return result

    def stream(self, request):
        self.calls += 1
        result = self._behavior(request)
        if isinstance(result, Exception):
            raise result
        # simple stream: yield chunks from iterable of LLMStreamChunk or strings not needed here
        for item in result:
            yield item


class FakeRegistry:
    def __init__(self, configured, providers):
        self._configured = set(configured)
        self._providers = providers

    def is_configured(self, provider_name):
        return provider_name in self._configured

    def get_provider(self, provider_name):
        return self._providers[provider_name]


def _request():
    return LLMRequest(messages=[LLMMessage(role="user", content="hello")], temperature=0.0, max_tokens=16)


def _ok(provider, model="m"):
    return LLMResponse(content="ok", provider=provider, model=model, latency_ms=1.0, finish_reason="stop")


def _status_error(status_code, code=None):
    err = Exception(f"http {status_code}")
    setattr(err, "status_code", status_code)
    if code is not None:
        setattr(err, "code", code)
    return err


def _make_manager(priority, providers_by_name, configured=None):
    if configured is None:
        configured = list(providers_by_name.keys())
    fm = FallbackManager(registry=FakeRegistry(configured, providers_by_name))
    fm.provider_priority = priority
    return fm


@pytest.mark.parametrize("status_code", [401, 402, 403, 404, 429, 500])
def test_provider_specific_or_transient_http_errors_fall_back_to_next_provider(status_code):
    # Covers: 401, 402, 403, 404/model unavailable, 429, 500 -> next provider succeeds
    calls = []

    def openrouter_fail(_req):
        calls.append("openrouter")
        return _status_error(status_code, code="provider_err")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini", "gemini-model")

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_fail),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fm = _make_manager(["openrouter", "gemini", "openai"], providers, configured=["openrouter", "gemini"])
    resp = fm.generate(_request())
    assert resp.provider == "gemini"
    assert calls == ["openrouter", "gemini"]
    assert providers["openrouter"].calls == 1
    assert providers["gemini"].calls == 1


def test_network_failure_falls_back_to_next_provider():
    calls = []

    def openrouter_fail(_req):
        calls.append("openrouter")
        raise TimeoutError("timed out")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini")

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_fail),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fm = _make_manager(["openrouter", "gemini"], providers)
    resp = fm.generate(_request())
    assert resp.provider == "gemini"
    assert calls == ["openrouter", "gemini"]


def test_invalid_shared_request_does_not_blindly_trigger_provider_fallback():
    calls = []

    def openrouter_bad_request(_req):
        calls.append("openrouter")
        # 400 treated as request/input error (possibly shared) -> do not fallback blindly
        return _status_error(400, code="bad_request")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini")

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_bad_request),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fm = _make_manager(["openrouter", "gemini"], providers)
    with pytest.raises(Exception) as ei:
        fm.generate(_request())
    assert getattr(ei.value, "status_code", None) == 400
    assert calls == ["openrouter"]
    assert providers["gemini"].calls == 0


def test_programming_errors_are_not_silently_swallowed_or_fallback_triggered():
    calls = []

    def openrouter_bug(_req):
        calls.append("openrouter")
        raise AttributeError("application bug")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini")

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_bug),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fm = _make_manager(["openrouter", "gemini"], providers)
    with pytest.raises(AttributeError):
        fm.generate(_request())
    assert calls == ["openrouter"]
    assert providers["gemini"].calls == 0


def test_all_providers_exhausted_produces_llm_fallback_error_with_chain():
    attempts = []

    def fail(name, status):
        def _fn(_req):
            attempts.append(name)
            return _status_error(status)
        return _fn

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", fail("openrouter", 401)),
        "gemini": FakeProvider("gemini", "gemini-model", fail("gemini", 503)),
    }
    fm = _make_manager(["openrouter", "gemini"], providers)
    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())
    err = ei.value
    assert attempts == ["openrouter", "gemini"]
    assert err.attempted_providers == ["openrouter", "gemini"]
    assert isinstance(err.__cause__, Exception)
    assert getattr(err.__cause__, "status_code", None) == 503
    assert len(err.failures) == 2
    assert err.failures[0]["provider"] == "openrouter"
    assert err.failures[0]["category"] in {"provider_auth_error", "provider_access_or_policy_error", "provider_model_or_resource_unavailable", "rate_limit", "provider_server_error", "network_or_timeout"}


def test_provider_ordering_remains_unchanged_and_unconfigured_skipped():
    calls = []

    def ok(name):
        def _fn(_req):
            calls.append(name)
            return _ok(name)
        return _fn

    providers = {
        "groq": FakeProvider("groq", "gq", ok("groq")),
        "openrouter": FakeProvider("openrouter", "or", lambda _r: _status_error(401)),
        "gemini": FakeProvider("gemini", "ge", ok("gemini")),
        "openai": FakeProvider("openai", "oa", ok("openai")),
    }
    # Priority preserved; gemini not configured so should be skipped, openai tried after openrouter fails
    fm = _make_manager(
        ["groq", "openrouter", "gemini", "openai"],
        providers,
        configured=["groq", "openrouter", "openai"],
    )
    resp = fm.generate(_request())
    # groq first and succeeds
    assert resp.provider == "groq"
    assert calls == ["groq"]


def test_provider_ordering_used_for_fallback_sequence():
    calls = []

    def fail(name):
        def _fn(_req):
            calls.append(name)
            return _status_error(401)
        return _fn

    def ok(name):
        def _fn(_req):
            calls.append(name)
            return _ok(name)
        return _fn

    providers = {
        "groq": FakeProvider("groq", "gq", fail("groq")),
        "openrouter": FakeProvider("openrouter", "or", fail("openrouter")),
        "gemini": FakeProvider("gemini", "ge", fail("gemini")),
        "openai": FakeProvider("openai", "oa", ok("openai")),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())
    assert resp.provider == "openai"
    assert calls == ["groq", "openrouter", "gemini", "openai"]


def test_provider_attempt_logs_contain_safe_metadata_and_no_secrets_or_prompts(caplog):
    secret = "sk-secret-APIKEY-12345"
    prompt_text = "USER_PRIVATE_PROMPT_CONTENT"

    def fail(_req):
        err = Exception("authorization failed with bearer " + secret)
        setattr(err, "status_code", 401)
        setattr(err, "code", "invalid_api_key")
        # simulate unsafe body/message that must not be logged
        setattr(err, "body", {"error": {"message": "bad key " + secret + " prompt " + prompt_text, "code": "invalid_api_key"}})
        raise err

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model-sensitive", fail),
        "gemini": FakeProvider("gemini", "gemini-model", lambda _r: _ok("gemini")),
    }
    fm = _make_manager(["openrouter", "gemini"], providers)

    with caplog.at_level(logging.INFO, logger="app.llm.fallback"):
        resp = fm.generate(_request())
    assert resp.provider == "gemini"

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "openrouter" in messages
    assert "or-model-sensitive" in messages or "openrouter" in messages
    assert "attempt=" in messages or "attempt" in messages
    assert "provider_auth_error" in messages or "category=" in messages
    assert "fallback_will_continue" in messages or "fallback_to_next_provider" in messages
    assert secret not in messages
    assert prompt_text not in messages
    assert "USER_PRIVATE_PROMPT_CONTENT" not in messages
    assert "sk-secret" not in messages


def test_openrouter_402_falls_back_to_gemini_then_openai_when_gemini_fails():
    calls = []

    def openrouter_402(_req):
        calls.append("openrouter")
        return _status_error(402, code="insufficient_credits")

    def gemini_fail(_req):
        calls.append("gemini")
        return _status_error(503)

    def openai_ok(_req):
        calls.append("openai")
        return _ok("openai", "openai-model")

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_402),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_fail),
        "openai": FakeProvider("openai", "openai-model", openai_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "openai"
    assert calls == ["openrouter", "gemini", "openai"]
    assert providers["openrouter"].calls == 1
    assert providers["gemini"].calls == 1
    assert providers["openai"].calls == 1


def test_openrouter_402_is_not_retried_and_exhausts_to_structured_error():
    calls = []

    def openrouter_402(_req):
        calls.append("openrouter")
        return _status_error(402, code="insufficient_credits")

    def gemini_fail(_req):
        calls.append("gemini")
        return _status_error(500)

    def openai_fail(_req):
        calls.append("openai")
        return _status_error(503)

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_402),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_fail),
        "openai": FakeProvider("openai", "openai-model", openai_fail),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    assert calls == ["openrouter", "gemini", "openai"]
    assert providers["openrouter"].calls == 1
    assert ei.value.failures[0]["provider"] == "openrouter"
    assert ei.value.failures[0]["category"] == "provider_payment_or_quota_error"


def test_groq_timeout_falls_back_to_openrouter_success():
    calls = []

    def groq_timeout(_req):
        calls.append("groq")
        raise TimeoutError("request timed out")

    def openrouter_ok(_req):
        calls.append("openrouter")
        return _ok("openrouter", "openrouter-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_timeout),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "openrouter"
    assert calls == ["groq", "openrouter"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 1


def test_timeout_chain_falls_back_until_gemini_success():
    calls = []

    def groq_timeout(_req):
        calls.append("groq")
        raise TimeoutError("groq timeout")

    def openrouter_timeout(_req):
        calls.append("openrouter")
        raise TimeoutError("openrouter timeout")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini", "gemini-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_timeout),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_timeout),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
        "openai": FakeProvider("openai", "openai-model", lambda _r: _ok("openai")),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "gemini"
    assert calls == ["groq", "openrouter", "gemini"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 1
    assert providers["gemini"].calls == 1
    assert providers["openai"].calls == 0


def test_timeout_chain_exhausts_all_providers_with_structured_error():
    calls = []

    def fail(name):
        def _fn(_req):
            calls.append(name)
            raise TimeoutError(f"{name} timeout")
        return _fn

    providers = {
        "groq": FakeProvider("groq", "groq-model", fail("groq")),
        "openrouter": FakeProvider("openrouter", "openrouter-model", fail("openrouter")),
        "gemini": FakeProvider("gemini", "gemini-model", fail("gemini")),
        "openai": FakeProvider("openai", "openai-model", fail("openai")),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    assert calls == ["groq", "openrouter", "gemini", "openai"]
    assert ei.value.attempted_providers == ["groq", "openrouter", "gemini", "openai"]
    assert len(ei.value.failures) == 4
    assert all(f["category"] in {"network_or_timeout", "timeout"} for f in ei.value.failures)
    assert all(providers[p].calls == 1 for p in providers)


def test_connection_and_read_timeout_errors_fall_back():
    import httpx

    calls = []

    def connect_timeout(_req):
        calls.append("connect")
        raise httpx.ConnectTimeout("connect timeout")

    def read_timeout(_req):
        calls.append("read")
        raise httpx.ReadTimeout("read timeout")

    def ok(_req):
        calls.append("ok")
        return _ok("gemini", "gemini-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", connect_timeout),
        "openrouter": FakeProvider("openrouter", "openrouter-model", read_timeout),
        "gemini": FakeProvider("gemini", "gemini-model", ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "gemini"
    assert calls == ["connect", "read", "ok"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 1


def test_openrouter_402_is_not_retried_and_exhausts_to_structured_error():
    calls = []

    def openrouter_402(_req):
        calls.append("openrouter")
        return _status_error(402, code="insufficient_credits")

    def gemini_fail(_req):
        calls.append("gemini")
        return _status_error(500)

    def openai_fail(_req):
        calls.append("openai")
        return _status_error(503)

    providers = {
        "openrouter": FakeProvider("openrouter", "or-model", openrouter_402),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_fail),
        "openai": FakeProvider("openai", "openai-model", openai_fail),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)

    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    err = ei.value
    assert calls == ["openrouter", "gemini", "openai"]
    assert providers["openrouter"].calls == 1  # no repeated retry on same 402
    assert err.attempted_providers == ["openrouter", "gemini", "openai"]
    assert len(err.failures) == 3
    assert err.failures[0]["provider"] == "openrouter"
    assert err.failures[0]["status_code"] == 402
    assert err.failures[0]["category"] == "provider_payment_or_quota_error"
    assert err.failures[0]["fallback_to_next_provider"] is True
    assert isinstance(err.__cause__, Exception)
    assert getattr(err.__cause__, "status_code", None) == 503


def test_overall_deadline_exhausted_before_next_provider(monkeypatch):
    import app.llm.fallback as fallback_mod

    calls = []

    def groq_timeout(_req):
        calls.append("groq")
        raise TimeoutError("groq timeout")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_timeout),
        "openrouter": FakeProvider("openrouter", "openrouter-model", lambda _r: _ok("openrouter")),
    }
    fm = _make_manager(["groq", "openrouter"], providers)

    # First perf_counter() sets overall start; subsequent calls simulate elapsed time
    # so remaining budget is exhausted after first failure and before next attempt.
    times = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(fallback_mod, "perf_counter", lambda: next(times, 10.0))
    monkeypatch.setattr(fallback_mod.settings, "LLM_REQUEST_DEADLINE_SECONDS", 5, raising=False)

    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    err = ei.value
    assert calls == ["groq"]
    assert err.attempted_providers == ["groq"]
    assert len(err.failures) == 1
    assert err.failures[0]["category"] in {"network_or_timeout", "timeout"}
    assert providers["openrouter"].calls == 0


def test_successful_final_provider_within_deadline(monkeypatch):
    import app.llm.fallback as fallback_mod

    calls = []

    def groq_timeout(_req):
        calls.append("groq")
        raise TimeoutError("groq timeout")

    def openrouter_ok(req):
        calls.append("openrouter")
        # Fallback should pass remaining budget to provider attempt.
        assert getattr(req, "timeout_seconds", None) is not None
        assert req.timeout_seconds > 0
        return _ok("openrouter", "openrouter-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_timeout),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_ok),
        "gemini": FakeProvider("gemini", "gemini-model", lambda _r: _ok("gemini")),
    }
    fm = _make_manager(["groq", "openrouter", "gemini"], providers)

    times = iter([0.0, 0.0, 1.0, 1.0, 1.2, 1.2, 1.3, 1.3, 1.4])
    monkeypatch.setattr(fallback_mod, "perf_counter", lambda: next(times, 1.5))
    monkeypatch.setattr(fallback_mod.settings, "LLM_REQUEST_DEADLINE_SECONDS", 30, raising=False)

    resp = fm.generate(_request())
    assert resp.provider == "openrouter"
    assert calls == ["groq", "openrouter"]
    assert providers["gemini"].calls == 0


def test_four_provider_timeout_chain_respects_overall_deadline_exhaustion(monkeypatch):
    import app.llm.fallback as fallback_mod

    calls = []

    def fail(name):
        def _fn(_req):
            calls.append(name)
            raise TimeoutError(f"{name} timeout")
        return _fn

    providers = {
        "groq": FakeProvider("groq", "groq-model", fail("groq")),
        "openrouter": FakeProvider("openrouter", "openrouter-model", fail("openrouter")),
        "gemini": FakeProvider("gemini", "gemini-model", fail("gemini")),
        "openai": FakeProvider("openai", "openai-model", fail("openai")),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)

    # Allow first two attempts, then exhaust overall deadline before attempting gemini.
    # Sequence covers: overall start, pre-attempt checks, attempt timing, and post-failure checks.
    times = iter([
        0.0,   # overall_started_at
        0.0,   # before groq remaining check
        0.0,   # groq attempt start
        1.0,   # groq attempt end
        1.0,   # after groq remaining check
        1.0,   # before openrouter remaining check
        1.0,   # openrouter attempt start
        3.0,   # openrouter attempt end
        6.0,   # after openrouter remaining check -> exhausted
        6.0,   # safety
    ])
    monkeypatch.setattr(fallback_mod, "perf_counter", lambda: next(times, 6.0))
    monkeypatch.setattr(fallback_mod.settings, "LLM_REQUEST_DEADLINE_SECONDS", 5, raising=False)

    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    err = ei.value
    assert calls == ["groq", "openrouter"]
    assert err.attempted_providers == ["groq", "openrouter"]
    assert providers["gemini"].calls == 0
    assert providers["openai"].calls == 0
    assert len(err.failures) == 2


def test_groq_rate_limit_429_falls_back_to_openrouter_success():
    calls = []

    def groq_rate_limit(_req):
        calls.append("groq")
        return _status_error(429, code="rate_limit_exceeded")

    def openrouter_ok(_req):
        calls.append("openrouter")
        return _ok("openrouter", "openrouter-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_rate_limit),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "openrouter"
    assert calls == ["groq", "openrouter"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 1


def test_groq_quota_exhaustion_falls_back_to_openrouter_success():
    calls = []

    def groq_quota_exhausted(_req):
        calls.append("groq")
        err = Exception("You exceeded your current quota, please check your plan and billing details.")
        setattr(err, "status_code", 429)
        setattr(err, "code", "insufficient_quota")
        return err

    def openrouter_ok(_req):
        calls.append("openrouter")
        return _ok("openrouter", "openrouter-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_quota_exhausted),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "openrouter"
    assert calls == ["groq", "openrouter"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 1


def test_openrouter_authentication_failure_falls_back_to_gemini_success():
    calls = []

    def openrouter_auth_fail(_req):
        calls.append("openrouter")
        return _status_error(401, code="invalid_api_key")

    def gemini_ok(_req):
        calls.append("gemini")
        return _ok("gemini", "gemini-model")

    providers = {
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_auth_fail),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "gemini"
    assert calls == ["openrouter", "gemini"]
    assert providers["openrouter"].calls == 1
    assert providers["gemini"].calls == 1


def test_gemini_timeout_falls_back_to_openai_success():
    calls = []

    def gemini_timeout(_req):
        calls.append("gemini")
        raise TimeoutError("gemini request timed out")

    def openai_ok(_req):
        calls.append("openai")
        return _ok("openai", "openai-model")

    providers = {
        "gemini": FakeProvider("gemini", "gemini-model", gemini_timeout),
        "openai": FakeProvider("openai", "openai-model", openai_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "openai"
    assert calls == ["gemini", "openai"]
    assert providers["gemini"].calls == 1
    assert providers["openai"].calls == 1


def test_openai_insufficient_credits_classified_separately_and_not_retried():
    calls = []
    secret = "sk-live-secret-key-DO-NOT-LOG"

    def groq_fail(_req):
        calls.append("groq")
        return _status_error(503, code="service_unavailable")

    def openrouter_fail(_req):
        calls.append("openrouter")
        return _status_error(401, code="invalid_api_key")

    def gemini_fail(_req):
        calls.append("gemini")
        raise TimeoutError("gemini timeout")

    def openai_no_credits(_req):
        calls.append("openai")
        err = Exception(f"Error code: 429 - You have no credits remaining. {secret}")
        setattr(err, "status_code", 429)
        setattr(err, "code", "insufficient_quota")
        setattr(err, "body", {"error": {"message": f"You have no credits remaining {secret}", "code": "insufficient_quota"}})
        return err

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_fail),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_fail),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_fail),
        "openai": FakeProvider("openai", "openai-model", openai_no_credits),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    err = ei.value
    assert calls == ["groq", "openrouter", "gemini", "openai"]
    assert providers["openai"].calls == 1
    assert err.attempted_providers == ["groq", "openrouter", "gemini", "openai"]
    assert len(err.failures) == 4
    openai_failure = err.failures[-1]
    assert openai_failure["provider"] == "openai"
    assert openai_failure["status_code"] == 429
    assert openai_failure["category"] == "provider_billing_or_quota_exhausted"
    assert openai_failure["retry_attempted"] is False
    assert openai_failure["fallback_continued"] is False
    assert "no credits remaining" not in str(err).lower()  # sanitized summary has no raw message text
    assert secret not in str(err)
    assert secret not in repr(err)


def test_all_providers_fail_with_different_exception_types():
    import httpx

    calls = []

    def groq_fail(_req):
        calls.append("groq")
        raise httpx.ReadTimeout("read timeout")

    def openrouter_fail(_req):
        calls.append("openrouter")
        return _status_error(401, code="invalid_api_key")

    def gemini_fail(_req):
        calls.append("gemini")
        return _status_error(404, code="model_not_found")

    def openai_fail(_req):
        calls.append("openai")
        err = Exception("You have no credits remaining.")
        setattr(err, "status_code", 429)
        setattr(err, "code", "insufficient_quota")
        return err

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_fail),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_fail),
        "gemini": FakeProvider("gemini", "gemini-model", gemini_fail),
        "openai": FakeProvider("openai", "openai-model", openai_fail),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    with pytest.raises(LLMFallbackError) as ei:
        fm.generate(_request())

    err = ei.value
    assert calls == ["groq", "openrouter", "gemini", "openai"]
    assert [f["provider"] for f in err.failures] == ["groq", "openrouter", "gemini", "openai"]
    assert err.failures[0]["category"] in {"network_or_timeout", "timeout"}
    assert err.failures[1]["category"] == "provider_auth_error"
    assert err.failures[2]["category"] == "provider_model_or_resource_unavailable"
    assert err.failures[3]["category"] == "provider_billing_or_quota_exhausted"
    assert all(p.calls == 1 for p in providers.values())


def test_non_retryable_failures_do_not_enter_retry_loops():
    calls = []

    def auth_fail(_req):
        calls.append("openrouter")
        return _status_error(401, code="invalid_api_key")

    providers = {
        "openrouter": FakeProvider("openrouter", "openrouter-model", auth_fail),
        "gemini": FakeProvider("gemini", "gemini-model", lambda _r: _status_error(401, code="invalid_api_key")),
        "openai": FakeProvider("openai", "openai-model", lambda _r: _status_error(402, code="insufficient_credits")),
    }
    fm = _make_manager(["openrouter", "gemini", "openai"], providers)
    with pytest.raises(LLMFallbackError):
        fm.generate(_request())

    assert providers["openrouter"].calls == 1
    assert providers["gemini"].calls == 1
    assert providers["openai"].calls == 1
    assert len([c for c in calls if c == "openrouter"]) == 1


def test_provider_failure_logs_include_retry_and_fallback_fields_without_secrets(caplog):
    secret = "sk-secret-987654321"
    prompt_text = "PRIVATE_USER_PROMPT"

    def quota_fail(_req):
        err = Exception(f"insufficient credits {secret} {prompt_text}")
        setattr(err, "status_code", 429)
        setattr(err, "code", "insufficient_quota")
        setattr(err, "body", {"error": {"message": f"{secret} {prompt_text}", "code": "insufficient_quota"}})
        raise err

    providers = {
        "openai": FakeProvider("openai", "openai-model", quota_fail),
        "gemini": FakeProvider("gemini", "gemini-model", lambda _r: _ok("gemini")),
    }
    fm = _make_manager(["openai", "gemini"], providers)

    with caplog.at_level(logging.INFO, logger="app.llm.fallback"):
        resp = fm.generate(_request())
    assert resp.provider == "gemini"

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "retry_attempted=" in messages
    assert "fallback_continued=" in messages
    assert "provider_billing_or_quota_exhausted" in messages
    assert secret not in messages
    assert "sk-secret" not in messages
    assert prompt_text not in messages


def test_existing_successful_primary_provider_path_unchanged():
    calls = []

    def groq_ok(_req):
        calls.append("groq")
        return _ok("groq", "groq-model")

    def openrouter_ok(_req):
        calls.append("openrouter")
        return _ok("openrouter", "openrouter-model")

    providers = {
        "groq": FakeProvider("groq", "groq-model", groq_ok),
        "openrouter": FakeProvider("openrouter", "openrouter-model", openrouter_ok),
    }
    fm = _make_manager(["groq", "openrouter", "gemini", "openai"], providers)
    resp = fm.generate(_request())

    assert resp.provider == "groq"
    assert resp.content == "ok"
    assert calls == ["groq"]
    assert providers["groq"].calls == 1
    assert providers["openrouter"].calls == 0

