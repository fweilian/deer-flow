"""Unit tests for the Setup Wizard (scripts/wizard/).

Run from repo root:
    cd backend && uv run pytest tests/test_setup_wizard.py -v
"""

from __future__ import annotations

import yaml
from wizard import ui as wizard_ui
from wizard.providers import LLM_PROVIDERS, LLMProvider, with_thinking_support
from wizard.steps import llm as llm_step
from wizard.writer import (
    build_minimal_config,
    read_env_file,
    write_config_yaml,
    write_env_file,
)


class TestProviders:
    def test_llm_providers_not_empty(self):
        assert len(LLM_PROVIDERS) >= 8

    def test_llm_providers_cover_config_example_families(self):
        providers = {provider.name: provider for provider in LLM_PROVIDERS}

        expected = {
            "volcengine",
            "zai",
            "openai",
            "openai_responses",
            "ollama_qwen",
            "ollama_gemma",
            "anthropic",
            "google",
            "gemini_openai_gateway",
            "mimo",
            "deepseek",
            "kimi",
            "novita",
            "minimax",
            "minimax_cn",
            "openrouter",
            "vllm",
            "mindie",
            "codex",
            "claude_code",
        }
        assert expected.issubset(providers)

        assert providers["openai_responses"].extra_config["use_responses_api"] is True
        assert providers["gemini_openai_gateway"].use == "deerflow.models.patched_openai:PatchedChatOpenAI"
        assert providers["mimo"].use == "deerflow.models.patched_mimo:PatchedChatMiMo"
        assert providers["deepseek"].use == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"
        assert providers["volcengine"].extra_config["api_base"] == "https://ark.cn-beijing.volces.com/api/v3"

    def test_zai_glm_flash_uses_required_thinking_workaround(self):
        provider = next(p for p in LLM_PROVIDERS if p.name == "zai")
        config = provider.extra_config_for("glm-5.3-flash")

        assert provider.use == "deerflow.models.patched_deepseek:PatchedChatDeepSeek"
        assert provider.env_var == "ZAI_API_KEY"
        assert config["api_base"] == "https://api.z.ai/api/paas/v4"
        assert config["supports_thinking"] is True
        assert config["supports_reasoning_effort"] is False
        assert "when_thinking_enabled" not in config
        assert "when_thinking_disabled" not in config
        assert config["extra_body"] == {
            "thinking": {"type": "enabled", "clear_thinking": True},
            "tool_stream": True,
        }

    def test_zai_glm_flash_workaround_is_preserved_in_generated_config(self):
        provider = next(p for p in LLM_PROVIDERS if p.name == "zai")
        content = build_minimal_config(
            provider_use=provider.use,
            model_name=provider.default_model,
            display_name=provider.display_name,
            api_key_field=provider.api_key_field,
            env_var=provider.env_var,
            extra_model_config=provider.extra_config,
        )

        model = yaml.safe_load(content)["models"][0]
        assert model["model"] == "glm-5.3-flash"
        assert model["api_key"] == "$ZAI_API_KEY"
        assert model["supports_reasoning_effort"] is False
        assert model["extra_body"]["thinking"] == {
            "type": "enabled",
            "clear_thinking": True,
        }

    def test_minimax_vision_is_per_model(self):
        """M3 supports vision; M2.7 variants are text-only.

        The provider-level extra_config carries the default (M3) capability, but
        extra_config_for() must drop vision when an M2.7 model is selected.
        """
        providers = {provider.name: provider for provider in LLM_PROVIDERS}

        for name in ("minimax", "minimax_cn"):
            provider = providers[name]
            assert provider.extra_config["supports_vision"] is True
            assert provider.extra_config_for("MiniMax-M3")["supports_vision"] is True
            assert provider.extra_config_for("MiniMax-M2.7")["supports_vision"] is False
            assert provider.extra_config_for("MiniMax-M2.7-highspeed")["supports_vision"] is False
            # Override must not mutate the shared provider-level config.
            assert provider.extra_config["supports_vision"] is True

    def test_extra_config_for_returns_provider_config_without_override(self):
        """Providers without per-model overrides return their config unchanged."""
        providers = {provider.name: provider for provider in LLM_PROVIDERS}
        openai = providers["openai"]
        assert openai.extra_config_for("gpt-5") == openai.extra_config

    def test_llm_providers_have_required_fields(self):
        for p in LLM_PROVIDERS:
            assert p.name
            assert p.display_name
            assert p.use
            assert ":" in p.use, f"Provider '{p.name}' use path must contain ':'"
            assert p.models
            assert p.default_model in p.models


class TestBuildMinimalConfig:
    def test_produces_valid_yaml(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI / gpt-4o",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        data = yaml.safe_load(content)
        assert data is not None
        assert "models" in data
        assert len(data["models"]) == 1
        model = data["models"][0]
        assert model["name"] == "gpt-4o"
        assert model["use"] == "langchain_openai:ChatOpenAI"
        assert model["model"] == "gpt-4o"
        assert model["api_key"] == "$OPENAI_API_KEY"

    def test_gemini_uses_gemini_api_key_field(self):
        content = build_minimal_config(
            provider_use="langchain_google_genai:ChatGoogleGenerativeAI",
            model_name="gemini-2.0-flash",
            display_name="Gemini",
            api_key_field="gemini_api_key",
            env_var="GEMINI_API_KEY",
        )
        data = yaml.safe_load(content)
        model = data["models"][0]
        assert "gemini_api_key" in model
        assert model["gemini_api_key"] == "$GEMINI_API_KEY"
        assert "api_key" not in model

    def test_openrouter_defaults_are_preserved(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="google/gemini-2.5-flash-preview",
            display_name="OpenRouter",
            api_key_field="api_key",
            env_var="OPENROUTER_API_KEY",
            extra_model_config={
                "base_url": "https://openrouter.ai/api/v1",
                "request_timeout": 600.0,
                "max_retries": 2,
                "max_tokens": 8192,
                "temperature": 0.7,
            },
        )
        data = yaml.safe_load(content)
        model = data["models"][0]
        assert model["base_url"] == "https://openrouter.ai/api/v1"
        assert model["request_timeout"] == 600.0
        assert model["max_retries"] == 2
        assert model["max_tokens"] == 8192
        assert model["temperature"] == 0.7

    def test_no_search_tool_when_not_configured(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "web_search" not in tool_names
        assert "web_fetch" not in tool_names

    def test_sandbox_included(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        data = yaml.safe_load(content)
        assert "sandbox" in data
        assert "use" in data["sandbox"]
        assert data["sandbox"]["use"] == "deerflow.sandbox.local:LocalSandboxProvider"
        assert data["sandbox"]["allow_host_bash"] is False

    def test_bash_tool_disabled_by_default(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "bash" not in tool_names

    def test_can_enable_container_sandbox_and_bash(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
            sandbox_use="deerflow.community.aio_sandbox:AioSandboxProvider",
            include_bash_tool=True,
        )
        data = yaml.safe_load(content)
        assert data["sandbox"]["use"] == "deerflow.community.aio_sandbox:AioSandboxProvider"
        assert "allow_host_bash" not in data["sandbox"]
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "bash" in tool_names

    def test_can_disable_write_tools(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
            include_write_tools=False,
        )
        data = yaml.safe_load(content)
        tool_names = [t["name"] for t in data.get("tools", [])]
        assert "write_file" not in tool_names
        assert "str_replace" not in tool_names

    def test_config_version_present(self):
        content = build_minimal_config(
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
            config_version=5,
        )
        data = yaml.safe_load(content)
        assert data["config_version"] == 5

    def test_cli_provider_does_not_emit_fake_api_key(self):
        content = build_minimal_config(
            provider_use="deerflow.models.openai_codex_provider:CodexChatModel",
            model_name="gpt-5.4",
            display_name="Codex CLI",
            api_key_field="api_key",
            env_var=None,
        )
        data = yaml.safe_load(content)
        model = data["models"][0]
        assert "api_key" not in model

    def test_responses_api_provider_defaults_are_preserved(self):
        provider = next(p for p in LLM_PROVIDERS if p.name == "openai_responses")
        content = build_minimal_config(
            provider_use=provider.use,
            model_name=provider.default_model,
            display_name=provider.display_name,
            api_key_field=provider.api_key_field,
            env_var=provider.env_var,
            extra_model_config=provider.extra_config,
        )
        data = yaml.safe_load(content)
        model = data["models"][0]
        assert model["use_responses_api"] is True
        assert model["output_version"] == "responses/v1"
        assert model["supports_vision"] is True

    def test_patched_thinking_provider_defaults_are_preserved(self):
        provider = next(p for p in LLM_PROVIDERS if p.name == "mimo")
        content = build_minimal_config(
            provider_use=provider.use,
            model_name=provider.default_model,
            display_name=provider.display_name,
            api_key_field=provider.api_key_field,
            env_var=provider.env_var,
            extra_model_config=provider.extra_config,
        )
        data = yaml.safe_load(content)
        model = data["models"][0]
        assert model["use"] == "deerflow.models.patched_mimo:PatchedChatMiMo"
        assert model["base_url"] == "https://api.xiaomimimo.com/v1"
        assert model["api_key"] == "$MIMO_API_KEY"
        assert model["supports_thinking"] is True
        assert model["when_thinking_enabled"]["extra_body"]["thinking"]["type"] == "enabled"
        assert model["when_thinking_disabled"]["extra_body"]["thinking"]["type"] == "disabled"


class TestThinkingSupport:
    def test_other_provider_requests_thinking_prompt(self):
        other = next(p for p in LLM_PROVIDERS if p.name == "other")
        assert other.ask_thinking_support is True

    def test_with_thinking_support_enabled_wires_toggles(self):
        other = next(p for p in LLM_PROVIDERS if p.name == "other")
        original = dict(other.extra_config)

        updated = with_thinking_support(other, True)

        assert updated.extra_config["supports_thinking"] is True
        assert updated.extra_config["when_thinking_enabled"]["extra_body"]["thinking"]["type"] == "enabled"
        assert updated.extra_config["when_thinking_disabled"]["extra_body"]["thinking"]["type"] == "disabled"
        # The shared provider singleton must not be mutated.
        assert other.extra_config == original

    def test_with_thinking_support_disabled_marks_unsupported(self):
        other = next(p for p in LLM_PROVIDERS if p.name == "other")

        updated = with_thinking_support(other, False)

        assert updated.extra_config["supports_thinking"] is False
        assert "when_thinking_enabled" not in updated.extra_config


class TestLLMStep:
    def test_model_selection_defaults_to_provider_default_model(self, monkeypatch):
        provider = LLMProvider(
            name="test",
            display_name="Test",
            description="provider",
            use="langchain_openai:ChatOpenAI",
            models=["first-model", "default-model"],
            default_model="default-model",
            env_var="TEST_API_KEY",
            package="langchain-openai",
        )
        prompts: list[tuple[str, int | None]] = []

        def fake_choice(prompt, options, default=None):
            prompts.append((prompt, default))
            return default if default is not None else 0

        monkeypatch.setattr(llm_step, "LLM_PROVIDERS", [provider])
        monkeypatch.setattr(llm_step, "ask_choice", fake_choice)
        monkeypatch.setattr(llm_step, "ask_secret", lambda _prompt: "key")
        monkeypatch.setattr(llm_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_success", lambda *_args, **_kwargs: None)

        result = llm_step.run_llm_step()

        assert result.model_name == "default-model"
        assert prompts == [("Enter choice", None), ("Select model", 1)]

    def test_base_url_prompt_is_used_for_custom_gateway(self, monkeypatch):
        provider = LLMProvider(
            name="gateway",
            display_name="Gateway",
            description="provider",
            use="langchain_openai:ChatOpenAI",
            models=["gateway/model"],
            default_model="gateway/model",
            env_var="GATEWAY_API_KEY",
            package="langchain-openai",
            base_url_prompt="Gateway URL",
        )

        monkeypatch.setattr(llm_step, "LLM_PROVIDERS", [provider])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(llm_step, "ask_text", lambda *_args, **_kwargs: "https://gateway.example/v1")
        monkeypatch.setattr(llm_step, "ask_secret", lambda _prompt: "key")
        monkeypatch.setattr(llm_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_success", lambda *_args, **_kwargs: None)

        result = llm_step.run_llm_step()

        assert result.base_url == "https://gateway.example/v1"

    def test_other_gateway_prompts_and_enables_thinking(self, monkeypatch):
        provider = LLMProvider(
            name="other",
            display_name="Other OpenAI-compatible",
            description="Custom gateway",
            use="langchain_openai:ChatOpenAI",
            models=["gpt-4o"],
            default_model="gpt-4o",
            env_var="OPENAI_API_KEY",
            package="langchain-openai",
            base_url_prompt="Base URL",
            model_prompt="Model name",
            ask_thinking_support=True,
        )

        monkeypatch.setattr(llm_step, "LLM_PROVIDERS", [provider])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(llm_step, "ask_text", lambda *_args, **_kwargs: "custom-thinking-model")
        monkeypatch.setattr(llm_step, "ask_secret", lambda _prompt: "key")
        monkeypatch.setattr(llm_step, "ask_yes_no", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(llm_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_success", lambda *_args, **_kwargs: None)

        result = llm_step.run_llm_step()

        assert result.model_name == "custom-thinking-model"
        assert result.provider.extra_config["supports_thinking"] is True
        assert result.provider.extra_config["when_thinking_enabled"]["extra_body"]["thinking"]["type"] == "enabled"

    def test_other_gateway_declined_thinking_marks_unsupported(self, monkeypatch):
        provider = LLMProvider(
            name="other",
            display_name="Other OpenAI-compatible",
            description="Custom gateway",
            use="langchain_openai:ChatOpenAI",
            models=["gpt-4o"],
            default_model="gpt-4o",
            env_var="OPENAI_API_KEY",
            package="langchain-openai",
            base_url_prompt="Base URL",
            model_prompt="Model name",
            ask_thinking_support=True,
        )

        monkeypatch.setattr(llm_step, "LLM_PROVIDERS", [provider])
        monkeypatch.setattr(llm_step, "ask_choice", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(llm_step, "ask_text", lambda *_args, **_kwargs: "plain-model")
        monkeypatch.setattr(llm_step, "ask_secret", lambda _prompt: "key")
        monkeypatch.setattr(llm_step, "ask_yes_no", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(llm_step, "print_header", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_info", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(llm_step, "print_success", lambda *_args, **_kwargs: None)

        result = llm_step.run_llm_step()

        assert result.provider.extra_config["supports_thinking"] is False
        assert "when_thinking_enabled" not in result.provider.extra_config


class TestWizardUi:
    def test_multi_choice_blank_requires_input_without_default(self, monkeypatch):
        answers = iter(["", "2"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        assert wizard_ui.ask_multi_choice("Pick", ["First", "Second"], default=None) == [1]

    def test_multi_choice_blank_accepts_empty_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "")

        assert wizard_ui.ask_multi_choice("Pick", ["First", "Second"], default=[]) == []


# ---------------------------------------------------------------------------
# writer.py — env file helpers
# ---------------------------------------------------------------------------


class TestEnvFileHelpers:
    def test_write_and_read_new_file(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_file(env_file, {"OPENAI_API_KEY": "sk-test123"})
        pairs = read_env_file(env_file)
        assert pairs["OPENAI_API_KEY"] == "sk-test123"

    def test_update_existing_key(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=old-key\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "new-key"})
        pairs = read_env_file(env_file)
        assert pairs["OPENAI_API_KEY"] == "new-key"
        # Should not duplicate
        content = env_file.read_text()
        assert content.count("OPENAI_API_KEY") == 1

    def test_preserve_existing_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("TAVILY_API_KEY=tavily-val\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "sk-new"})
        pairs = read_env_file(env_file)
        assert pairs["TAVILY_API_KEY"] == "tavily-val"
        assert pairs["OPENAI_API_KEY"] == "sk-new"

    def test_preserve_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# My .env file\nOPENAI_API_KEY=old\n")
        write_env_file(env_file, {"OPENAI_API_KEY": "new"})
        content = env_file.read_text()
        assert "# My .env file" in content

    def test_read_ignores_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nKEY=value\n")
        pairs = read_env_file(env_file)
        assert "# comment" not in pairs
        assert pairs["KEY"] == "value"


# ---------------------------------------------------------------------------
# writer.py — write_config_yaml
# ---------------------------------------------------------------------------


class TestWriteConfigYaml:
    def test_generated_config_loadable_by_appconfig(self, tmp_path):
        """The generated config.yaml must be parseable (basic YAML validity)."""

        config_path = tmp_path / "config.yaml"
        write_config_yaml(
            config_path,
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI / gpt-4o",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        assert config_path.exists()
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "models" in data

    def test_copies_example_defaults_for_unconfigured_sections(self, tmp_path):
        example_path = tmp_path / "config.example.yaml"
        example_path.write_text(
            yaml.safe_dump(
                {
                    "config_version": 5,
                    "log_level": "info",
                    "token_usage": {"enabled": True},
                    "tool_groups": [{"name": "file:read"}, {"name": "file:write"}, {"name": "bash"}],
                    "tools": [
                        {"name": "ls", "group": "file:read", "use": "deerflow.sandbox.tools:ls_tool"},
                        {"name": "write_file", "group": "file:write", "use": "deerflow.sandbox.tools:write_file_tool"},
                        {"name": "bash", "group": "bash", "use": "deerflow.sandbox.tools:bash_tool"},
                    ],
                    "sandbox": {
                        "use": "deerflow.sandbox.local:LocalSandboxProvider",
                        "allow_host_bash": False,
                    },
                    "summarization": {"max_tokens": 2048},
                },
                sort_keys=False,
            )
        )

        config_path = tmp_path / "config.yaml"
        write_config_yaml(
            config_path,
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI / gpt-4o",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        with open(config_path) as f:
            data = yaml.safe_load(f)

        assert data["log_level"] == "info"
        assert data["token_usage"]["enabled"] is True
        assert data["tool_groups"][0]["name"] == "file:read"
        assert data["summarization"]["max_tokens"] == 2048
        assert all(tool["name"] not in {"web_search", "web_fetch", "image_search"} for tool in data["tools"])

    def test_config_version_read_from_example(self, tmp_path):
        """write_config_yaml should read config_version from config.example.yaml if present."""

        example_path = tmp_path / "config.example.yaml"
        example_path.write_text("config_version: 99\n")

        config_path = tmp_path / "config.yaml"
        write_config_yaml(
            config_path,
            provider_use="langchain_openai:ChatOpenAI",
            model_name="gpt-4o",
            display_name="OpenAI",
            api_key_field="api_key",
            env_var="OPENAI_API_KEY",
        )
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert data["config_version"] == 99

    def test_model_base_url_from_extra_config(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        write_config_yaml(
            config_path,
            provider_use="langchain_openai:ChatOpenAI",
            model_name="google/gemini-2.5-flash-preview",
            display_name="OpenRouter",
            api_key_field="api_key",
            env_var="OPENROUTER_API_KEY",
            extra_model_config={"base_url": "https://openrouter.ai/api/v1"},
        )
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert data["models"][0]["base_url"] == "https://openrouter.ai/api/v1"
