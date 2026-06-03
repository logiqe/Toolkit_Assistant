import os
from pathlib import Path


def _load_env_file(filename: str = ".env") -> None:
    env_path = Path(__file__).resolve().parent / filename
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        cleaned = value.strip()
        if cleaned and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1]
        cleaned = cleaned.replace("\\n", "\n")
        os.environ[key.strip()] = cleaned

_load_env_file()

_BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = _BASE_DIR / path
    return str(path)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _optional_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {value}") from exc

settings = {
    "broker": _require("MQTT_BROKER"),
    "topic": _require("MQTT_TOPIC"),
    "mqtt_user": _optional("MQTT_USER"),
    "mqtt_password": _optional("MQTT_PASSWORD"),
    "client_id": _optional("MQTT_CLIENT_ID", "windmill-assistant"),
    "mqtt_port": _optional_int("MQTT_PORT", 1883),
    "openAIToken": _optional("OPENAI_API_KEY"),
    "anthropicToken": _optional("ANTHROPIC_API_KEY"),
    "transcription_model": _optional("TRANSCRIPTION_MODEL", "gpt-4o-transcribe"),
    "Welcom_msg": _optional(
        "WELCOME_MESSAGE",
        "Hello! Ask a question to adjust the windmills.",
    ),
    "admin_password": _optional("ADMIN_PASSWORD", ""),
    "assistant_model": _optional("OPENAI_ASSISTANT_MODEL", "gpt-4o-mini"),
    "assistant_name": _optional("OPENAI_ASSISTANT_NAME", "Windmill Assistant"),
    "assistant_description": _optional(
        "OPENAI_ASSISTANT_DESCRIPTION", "Controls windmill presets via MQTT."
    ),
    "assistant_instructions_file": _resolve_path(
        _optional("OPENAI_ASSISTANT_INSTRUCTIONS_FILE", "assistant_instructions.md")
    ),
    "assistant_schema_file": _resolve_path(
        _optional("OPENAI_ASSISTANT_SCHEMA_FILE", "assistant_response_schema.json")
    ),
    "assistant_state_file": _resolve_path(
        _optional("OPENAI_ASSISTANT_STATE_FILE", "assistant_state.json")
    ),
}

def reload_settings():
    """Reload .env and rebuild the settings dict in place."""
    _load_env_file()
    settings["broker"] = _require("MQTT_BROKER")
    settings["topic"] = _require("MQTT_TOPIC")
    settings["mqtt_user"] = _optional("MQTT_USER")
    settings["mqtt_password"] = _optional("MQTT_PASSWORD")
    settings["client_id"] = _optional("MQTT_CLIENT_ID", "windmill-assistant")
    settings["mqtt_port"] = _optional_int("MQTT_PORT", 1883)
    settings["openAIToken"] = _optional("OPENAI_API_KEY")
    settings["anthropicToken"] = _optional("ANTHROPIC_API_KEY")
    settings["transcription_model"] = _optional("TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
    settings["Welcom_msg"] = _optional("WELCOME_MESSAGE", "Hello! Ask a question to adjust the windmills.")
    settings["admin_password"] = _optional("ADMIN_PASSWORD", "")
    settings["assistant_model"] = _optional("OPENAI_ASSISTANT_MODEL", "gpt-4o-mini")
    settings["assistant_name"] = _optional("OPENAI_ASSISTANT_NAME", "Windmill Assistant")
    settings["assistant_description"] = _optional("OPENAI_ASSISTANT_DESCRIPTION", "Controls windmill presets via MQTT.")
    # les *_file ne changent pas en pratique, mais au cas où :
    settings["assistant_instructions_file"] = _resolve_path(_optional("OPENAI_ASSISTANT_INSTRUCTIONS_FILE", "assistant_instructions.md"))
    settings["assistant_schema_file"] = _resolve_path(_optional("OPENAI_ASSISTANT_SCHEMA_FILE", "assistant_response_schema.json"))
    settings["assistant_state_file"] = _resolve_path(_optional("OPENAI_ASSISTANT_STATE_FILE", "assistant_state.json"))