import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def test_provider_presets_cover_the_supported_vendors():
    from app.ai.provider import PROVIDER_PRESETS

    assert {
        code: preset.base_url
        for code, preset in PROVIDER_PRESETS.items()
    } == {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "xai": "https://api.x.ai/v1",
        "deepseek": "https://api.deepseek.com",
        "minimax": "https://api.minimaxi.com/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "glm": "https://open.bigmodel.cn/api/paas/v4/",
        "siliconflow": "https://api.siliconflow.cn/v1",
    }


def test_generic_secret_roundtrip_uses_the_configured_fernet_key(monkeypatch):
    from app.tools.basesec import decrypt_secret, encrypt_secret

    monkeypatch.setenv("OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"))

    ciphertext = encrypt_secret("sk-demo-value")

    assert ciphertext != "sk-demo-value"
    assert ciphertext.startswith("gAAAAA")
    assert decrypt_secret(ciphertext) == "sk-demo-value"


class _FakeCompletions:
    def __init__(self, *, stream_chunks=None, non_stream_response=None, stream_error=None):
        self.stream_chunks = stream_chunks or []
        self.non_stream_response = non_stream_response
        self.stream_error = stream_error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["stream"]:
            if self.stream_error:
                raise self.stream_error
            return iter(self.stream_chunks)
        return self.non_stream_response


def _fake_client(completions):
    return _ns(chat=_ns(completions=completions))


def test_adapter_aggregates_fragmented_stream_tool_calls():
    from app.ai.provider import OpenAICompatibleAdapter

    chunks = [
        _ns(choices=[_ns(delta=_ns(
            content="准备查询",
            reasoning_content="先检查",
            tool_calls=None,
        ))]),
        _ns(choices=[_ns(delta=_ns(
            content=None,
            reasoning_content="资产范围",
            tool_calls=[
                _ns(
                    index=0,
                    id="call_",
                    function=_ns(name="search_", arguments='{"group":'),
                )
            ],
        ))]),
        _ns(choices=[_ns(delta=_ns(
            content=None,
            reasoning_content=None,
            tool_calls=[
                _ns(
                    index=0,
                    id="asset",
                    function=_ns(name="assets", arguments='"web"}'),
                )
            ],
        ))]),
    ]
    completions = _FakeCompletions(stream_chunks=chunks)
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model="demo-model",
        client_factory=lambda **_: _fake_client(completions),
    )

    result = adapter.complete(
        messages=[{"role": "user", "content": "查询 web 组"}],
        tools=[{"type": "function", "function": {"name": "search_assets"}}],
    )

    assert result.content == "准备查询"
    assert result.reasoning_content == "先检查资产范围"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_asset"
    assert result.tool_calls[0].name == "search_assets"
    assert result.tool_calls[0].arguments == {"group": "web"}
    assert result.used_stream is True
    assert completions.calls[0]["stream"] is True


def test_adapter_retries_once_without_stream_when_streaming_is_incompatible():
    from app.ai.provider import OpenAICompatibleAdapter

    message = _ns(
        content=None,
        tool_calls=[
            _ns(
                id="call_1",
                function=_ns(
                    name="search_assets",
                    arguments=json.dumps({"online": True}),
                ),
            )
        ],
    )
    response = _ns(choices=[_ns(message=message)])
    completions = _FakeCompletions(
        stream_error=RuntimeError("provider does not stream tool calls"),
        non_stream_response=response,
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model="demo-model",
        client_factory=lambda **_: _fake_client(completions),
    )

    result = adapter.complete(
        messages=[{"role": "user", "content": "只看在线主机"}],
        tools=[{"type": "function", "function": {"name": "search_assets"}}],
    )

    assert [call["stream"] for call in completions.calls] == [True, False]
    assert result.used_stream is False
    assert result.tool_calls[0].arguments == {"online": True}


def test_adapter_keeps_non_stream_reasoning_content_for_tool_roundtrip():
    from app.ai.provider import OpenAICompatibleAdapter

    message = _ns(
        content=None,
        reasoning_content="需要先读取服务状态",
        tool_calls=[_ns(
            id="call_probe",
            function=_ns(name="run_diagnostic", arguments="{}"),
        )],
    )
    response = _ns(choices=[_ns(message=message)])
    completions = _FakeCompletions(
        stream_error=RuntimeError("stream unsupported"),
        non_stream_response=response,
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test",
        base_url="https://example.test/v1",
        model="deepseek-chat",
        client_factory=lambda **_: _fake_client(completions),
    )

    result = adapter.complete(messages=[{"role": "user", "content": "检查"}])

    assert result.reasoning_content == "需要先读取服务状态"
    assert result.tool_calls[0].name == "run_diagnostic"


def test_public_providers_expose_configured_disabled_and_unavailable_reasons(
    monkeypatch,
):
    from app.ai import provider_config
    from app.ai.provider_config import ProviderConfigService

    rows = [
        _ns(
            id=1,
            provider_code="siliconflow",
            api_key_ciphertext="ciphertext",
            model="Qwen/Qwen3-8B",
            enabled=False,
            is_default=True,
        ),
        _ns(
            id=2,
            provider_code="minimax",
            api_key_ciphertext=None,
            model="MiniMax-M2.1",
            enabled=True,
            is_default=False,
        ),
        _ns(
            id=3,
            provider_code="deepseek",
            api_key_ciphertext="ciphertext",
            model="",
            enabled=True,
            is_default=False,
        ),
        _ns(
            id=4,
            provider_code="openai",
            api_key_ciphertext="ciphertext",
            model="gpt-5-mini",
            enabled=True,
            is_default=False,
        ),
        _ns(
            id=5,
            provider_code="kimi",
            api_key_ciphertext=None,
            model="",
            enabled=False,
            is_default=False,
        ),
    ]
    monkeypatch.setattr(
        provider_config,
        "t_ai_provider",
        _ns(query=_ns(all=lambda: rows)),
    )

    result = ProviderConfigService().public_rows()
    providers = {item["provider_code"]: item for item in result["providers"]}

    assert providers["siliconflow"]["available"] is False
    assert providers["siliconflow"]["reason"] == "disabled"
    assert providers["minimax"]["available"] is False
    assert providers["minimax"]["reason"] == "key_missing"
    assert providers["deepseek"]["available"] is False
    assert providers["deepseek"]["reason"] == "model_missing"
    assert providers["openai"]["available"] is True
    assert providers["openai"]["reason"] is None
    assert "kimi" not in providers
    assert result["default_provider"] == "openai"


def test_discover_models_normalizes_deduplicates_sorts_and_caps(monkeypatch):
    from app.ai import provider_config
    from app.ai.provider_config import ProviderConfigService

    row = _ns(
        provider_code="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key_ciphertext="ciphertext",
        model="",
        enabled=False,
    )
    service = ProviderConfigService()
    monkeypatch.setattr(service, "_row", lambda code: row)
    monkeypatch.setattr(
        provider_config,
        "_assert_public_destination",
        lambda value: None,
    )
    monkeypatch.setattr(
        provider_config,
        "decrypt_secret",
        lambda value: "sk-server-only",
    )

    unique_ids = [
        f"vendor/model-{index:03d}"
        for index in range(provider_config.MAX_DISCOVERED_MODELS + 5)
    ]
    model_ids = unique_ids + [" vendor/model-001 ", "vendor/model-001", "", None]
    response = _ns(data=[_ns(id=model_id) for model_id in model_ids])
    captured = {}

    class FakeModels:
        def list(self):
            return response

    class FakeClient:
        models = FakeModels()

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    result = service.discover_models(
        "siliconflow",
        client_factory=client_factory,
    )

    assert captured["api_key"] == "sk-server-only"
    assert captured["base_url"] == row.base_url
    assert result["provider_code"] == "siliconflow"
    assert result["models"] == sorted(unique_ids)[
        : provider_config.MAX_DISCOVERED_MODELS
    ]
    assert result["total"] == provider_config.MAX_DISCOVERED_MODELS
    assert result["truncated"] is True
    assert "sk-server-only" not in json.dumps(result)


def test_discover_models_requires_a_saved_api_key(monkeypatch):
    from app.ai.provider_config import ProviderConfigError, ProviderConfigService

    service = ProviderConfigService()
    monkeypatch.setattr(
        service,
        "_row",
        lambda code: _ns(
            provider_code=code,
            base_url="https://api.siliconflow.cn/v1",
            api_key_ciphertext=None,
        ),
    )

    with pytest.raises(ProviderConfigError, match="API Key"):
        service.discover_models("siliconflow")


def test_discover_models_rejects_a_non_public_destination(monkeypatch):
    from app.ai import provider_config
    from app.ai.provider_config import ProviderConfigError, ProviderConfigService

    service = ProviderConfigService()
    monkeypatch.setattr(
        service,
        "_row",
        lambda code: _ns(
            provider_code=code,
            base_url="https://provider.example/v1",
            api_key_ciphertext="ciphertext",
        ),
    )
    monkeypatch.setattr(
        provider_config,
        "_assert_public_destination",
        lambda value: (_ for _ in ()).throw(
            ProviderConfigError("模型服务域名解析到私网或保留地址")
        ),
    )
    monkeypatch.setattr(
        provider_config,
        "decrypt_secret",
        lambda value: pytest.fail("destination must be checked before decrypting the key"),
    )

    with pytest.raises(ProviderConfigError, match="私网或保留地址"):
        service.discover_models("siliconflow")


def test_provider_context_capability_defaults_to_256k_and_round_trips_1m(monkeypatch):
    from app.ai import provider_config
    from app.ai.context import DEEP_CONTEXT_TOKENS, STANDARD_CONTEXT_TOKENS
    from app.ai.provider_config import ProviderConfigError, ProviderConfigService

    row = _ns(
        id=1,
        provider_code="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        model="demo-model",
        api_key_ciphertext="ciphertext",
        enabled=False,
        is_default=False,
        extra_body_json=None,
        created_at=None,
        updated_at=None,
    )
    service = ProviderConfigService()
    monkeypatch.setattr(service, "_row", lambda code: row)
    monkeypatch.setattr(
        provider_config,
        "t_ai_provider",
        _ns(query=_ns(all=lambda: [row])),
    )

    legacy = next(
        item
        for item in service.admin_rows()
        if item["provider_code"] == "siliconflow"
    )
    assert legacy["context_window_tokens"] == STANDARD_CONTEXT_TOKENS

    saved = service.save(
        "siliconflow",
        {"context_window_tokens": str(DEEP_CONTEXT_TOKENS)},
    )
    assert saved["context_window_tokens"] == DEEP_CONTEXT_TOKENS

    with pytest.raises(ProviderConfigError, match="上下文"):
        service.save("siliconflow", {"context_window_tokens": 524288})


def test_provider_context_window_schema_is_kept_in_sync():
    from app.core.db.database import t_ai_provider

    backend_root = Path(__file__).resolve().parents[1]
    column = t_ai_provider.__table__.c.context_window_tokens
    assert column.nullable is False
    assert str(column.server_default.arg) == "262144"

    fresh_schema = (backend_root / "mysqldir" / "orange.sql").read_text(
        encoding="utf-8",
    )
    migration = (
        backend_root / "mysqldir" / "rev49_ai_context_window.sql"
    ).read_text(encoding="utf-8")
    assert "`context_window_tokens` int(11) NOT NULL DEFAULT '262144'" in fresh_schema
    assert "information_schema.COLUMNS" in migration
    assert "ADD COLUMN `context_window_tokens` INT NOT NULL DEFAULT 262144" in migration


@pytest.mark.parametrize("plain", ["", b""])
def test_generic_secret_rejects_empty_values(monkeypatch, plain):
    from app.tools.basesec import encrypt_secret

    monkeypatch.setenv("OGS_FERNET_KEYS", Fernet.generate_key().decode("ascii"))

    with pytest.raises(ValueError, match="secret cannot be empty"):
        encrypt_secret(plain)
