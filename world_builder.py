"""
world_builder.py
Generates Three.js WebXR scenes from natural language descriptions using OpenAI API.
Reuses the same OPENAI_API_KEY already used for the main assistant.
The generated HTML is self-contained and includes a sensor bridge (postMessage).
"""

import json
import os
import re
import anthropic
from pathlib import Path

_client = None
_system_prompt_cache = None

WORLD_PROMPT_FILE = Path(__file__).parent / "world_builder_instructions.md"


def load_system_prompt() -> str:
    """Load the world builder system prompt from disk (cached)."""
    global _system_prompt_cache
    if _system_prompt_cache is None:
        if not WORLD_PROMPT_FILE.exists():
            raise RuntimeError(f"World prompt file not found: {WORLD_PROMPT_FILE}")
        _system_prompt_cache = WORLD_PROMPT_FILE.read_text(encoding="utf-8")
    return _system_prompt_cache

def reload_system_prompt() -> str:
    """Force reload from disk (useful for admin hot-reload)."""
    global _system_prompt_cache
    _system_prompt_cache = None
    return load_system_prompt()

def get_client():
    global _client
    # Toujours recréer si la clé a changé
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    # Recréer le client si pas encore créé ou si la clé a changé
    if _client is None or _client.api_key != key:
        _client = anthropic.Anthropic(api_key=key)
    return _client

def sanitize_world_code(html: str) -> str:
    """Fix common LLM-generated JS escape issues."""
    # Seulement dans les chemins de fichiers (loader.load('...')), pas partout
    html = re.sub(r"load\('([^']*)'\)", lambda m: "load('" + m.group(1).replace("\\", "/") + "')", html)
    html = re.sub(r'load\("([^"]*)"\)', lambda m: 'load("' + m.group(1).replace("\\", "/") + '")', html)
    return html

def format_hardware_context(ctx: dict) -> str:
    """Render the hardware state as a clear text block for the LLM."""
    inputs = ctx.get("configured_inputs", [])
    outputs = ctx.get("configured_outputs", [])
    calibrated = ctx.get("calibrated_sensors", {})

    lines = ["## CURRENT HARDWARE CONFIGURATION (live from the user's board)"]

    if not inputs and not outputs:
        lines.append("⚠ No hardware components are currently configured on the board.")
        lines.append("If the user asks to bind a sensor that isn't configured, gently mention that the sensor needs to be set up first via the main chat.")
        return "\n".join(lines)

    if inputs:
        lines.append("\n### Available sensors (window.sensorData keys):")
        for inp in inputs:
            name = inp.get("name") or inp.get("type")
            pin = inp.get("pin", "?")
            cal = calibrated.get(name)
            cal_str = f" — calibrated range [{cal['min']}–{cal['max']}]" if cal else ""
            lines.append(f"  • `{name}` (pin {pin}){cal_str}")

    if outputs:
        lines.append("\n### Physical outputs already in use (for reference):")
        for out in outputs:
            lines.append(f"  • {out.get('name', out.get('type'))} on pin {out.get('pin', '?')}")

    lines.append(
        "\n### How to use this in the scene:\n"
        "Your `window.onSensorUpdate(sensors)` callback will receive ONLY the keys "
        "listed above. Build reactions around them. For example, if the user says "
        "'when I press the button, spawn a fish', and `button` is in the list, "
        "write code that detects a 0→1 transition on `sensors.button` and adds a fish mesh to the scene."
    )

    return "\n".join(lines)

def convert_messages_for_anthropic(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format messages to Anthropic format."""
    converted = []
    for msg in messages:
        if msg["role"] == "system":
            continue  # system est géré séparément
        
        content = msg["content"]
        
        if isinstance(content, list):
            new_content = []
            for block in content:
                if block.get("type") == "image_url":
                    # Convertir format OpenAI → Anthropic
                    url = block["image_url"]["url"]
                    if url.startswith("data:"):
                        # base64 data URL
                        match = re.match(r"data:([^;]+);base64,(.+)", url)
                        if match:
                            media_type = match.group(1)
                            data = match.group(2)
                            new_content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": data,
                                }
                            })
                    else:
                        # URL normale
                        new_content.append({
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": url,
                            }
                        })
                else:
                    new_content.append(block)
            converted.append({"role": msg["role"], "content": new_content})
        else:
            converted.append({"role": msg["role"], "content": content})
    
    return converted

def generate_world(conversation_history: list[dict], hardware_context: dict | None = None) -> dict:
    """
    Given a conversation history, generate a 3D world or a chat reply.
    Uses the chat completions API (not the Assistants API) for simplicity.
    Returns: { "reply": str, "world_code": str | None }
    """
    try:
        client = get_client()

        # Build messages: system prompt + conversation history
        system_prompt = load_system_prompt()
        if hardware_context:
            system_prompt += "\n\n" + format_hardware_context(hardware_context)
            if hardware_context.get("chat_history"):
                system_prompt += f"\n\n## HARDWARE CONFIGURED IN MAIN CHAT\nThe user already set up their hardware in the main assistant chat. Here's the conversation history for context:\n{hardware_context['chat_history']}"

        
        messages = [{"role": "system", "content": system_prompt}] + conversation_history

        # Detect if any message contains image content (vision call)
        has_vision = any(
            isinstance(msg.get("content"), list)
            for msg in conversation_history
        )

        create_kwargs = {
            "model": "claude-opus-4-5",
            "max_tokens": 8000,
            "system": system_prompt,
            "messages": convert_messages_for_anthropic(conversation_history),
        }

        # json_object mode is incompatible with vision messages in some API versions
        if not has_vision:
            last_content = conversation_history[-1]["content"]
            if not isinstance(last_content, list):
                last_content = [{"type": "text", "text": last_content}]
            last_content.append({
                "type": "text",
                "text": 'IMPORTANT: Respond ONLY with a valid JSON object {"reply": "...", "world_code": "..."}'
            })
            conversation_history[-1]["content"] = last_content

        response = client.messages.create(**create_kwargs)
        raw = response.content[0].text.strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: extraire le bloc JSON
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    return {"reply": raw[:500], "world_code": None}
            else:
                return {"reply": raw[:500], "world_code": None}

        return {
            "reply": result.get("reply", "Here's your world!"),
            "world_code": sanitize_world_code(result.get("world_code") or "") or None
        }

    except Exception as e:
        return {
            "reply": f"Error generating world: {str(e)}",
            "world_code": None
        }