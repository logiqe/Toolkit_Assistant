"""
world_builder.py
Generates Three.js WebXR scenes from natural language descriptions using Anthropic API.
"""

import json
import os
import re
import anthropic
import copy
from pathlib import Path

_client = None
_system_prompt_cache = None

WORLD_PROMPT_FILE = Path(__file__).parent / "world_builder_instructions.md"

JSON_REMINDER = (
    '\n\nCRITICAL: You MUST respond with ONLY a raw JSON object — no markdown, '
    'no code fences, no preamble. Format: {"reply": "...", "world_code": "<!DOCTYPE html>..."} '
    'The world_code value must be a JSON-escaped string (escape all backslashes, quotes, newlines).'
)


def load_system_prompt() -> str:
    global _system_prompt_cache
    if _system_prompt_cache is None:
        if not WORLD_PROMPT_FILE.exists():
            raise RuntimeError(f"World prompt file not found: {WORLD_PROMPT_FILE}")
        _system_prompt_cache = WORLD_PROMPT_FILE.read_text(encoding="utf-8")
    return _system_prompt_cache


def reload_system_prompt() -> str:
    global _system_prompt_cache
    _system_prompt_cache = None
    return load_system_prompt()


def get_client():
    global _client
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    if _client is None or getattr(_client, '_api_key_used', None) != key:
        _client = anthropic.Anthropic(api_key=key)
        _client._api_key_used = key
    return _client


def sanitize_world_code(html: str) -> str:
    """
    Fix the only safe thing: ensure Three.js CDN script tag is NOT deferred.
    Do NOT touch any other script tags — the LLM writes correct JS.
    """
    # Remove any accidental 'defer' on the Three.js CDN script tag only
    html = re.sub(
        r'(<script\s[^>]*cdnjs\.cloudflare\.com[^>]*)\s+defer([^>]*>)',
        r'\1\2',
        html
    )
    html = re.sub(
        r'(<script\s[^>]*three\.min\.js[^>]*)\s+defer([^>]*>)',
        r'\1\2',
        html
    )
    return html


def format_hardware_context(ctx: dict) -> str:
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
        "listed above. Build reactions around them."
    )
    return "\n".join(lines)


def convert_messages_for_anthropic(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format messages (with image_url) to Anthropic format."""
    converted = []
    for msg in messages:
        if msg["role"] == "system":
            continue  # handled via system= param

        content = msg["content"]

        if isinstance(content, list):
            new_content = []
            for block in content:
                if block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    if url.startswith("data:"):
                        match = re.match(r"data:([^;]+);base64,(.+)", url, re.DOTALL)
                        if match:
                            new_content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": match.group(1),
                                    "data": match.group(2),
                                }
                            })
                    else:
                        new_content.append({
                            "type": "image",
                            "source": {"type": "url", "url": url}
                        })
                elif block.get("type") == "text":
                    new_content.append(block)
                # skip unknown block types
            converted.append({"role": msg["role"], "content": new_content})
        else:
            converted.append({"role": msg["role"], "content": str(content)})

    return converted


def _inject_json_reminder(messages_copy: list[dict]) -> list[dict]:
    """Append the JSON format reminder to the last user message."""
    for i in range(len(messages_copy) - 1, -1, -1):
        msg = messages_copy[i]
        if msg["role"] == "user":
            if isinstance(msg["content"], str):
                msg["content"] = msg["content"] + JSON_REMINDER
            elif isinstance(msg["content"], list):
                # Find last text block and append, or add a new text block
                appended = False
                for block in reversed(msg["content"]):
                    if block.get("type") == "text":
                        block["text"] = block["text"] + JSON_REMINDER
                        appended = True
                        break
                if not appended:
                    msg["content"].append({"type": "text", "text": JSON_REMINDER})
            break
    return messages_copy


def generate_world(conversation_history: list[dict], hardware_context: dict | None = None) -> dict:
    try:
        client = get_client()

        system_prompt = load_system_prompt()
        if hardware_context:
            system_prompt += "\n\n" + format_hardware_context(hardware_context)
            if hardware_context.get("chat_history"):
                system_prompt += (
                    f"\n\n## HARDWARE CONFIGURED IN MAIN CHAT\n"
                    f"The user already set up their hardware. Context:\n{hardware_context['chat_history']}"
                )

        # Deep copy — never mutate caller's history
        messages_copy = copy.deepcopy(conversation_history)

        # Always inject JSON reminder into the last user message
        messages_copy = _inject_json_reminder(messages_copy)

        # Convert to Anthropic format
        anthropic_messages = convert_messages_for_anthropic(messages_copy)

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=8000,
            system=system_prompt,
            messages=anthropic_messages,
        )
        raw = response.content[0].text.strip()
        print(f"[world_builder] raw (first 300):\n{raw[:300]}")

        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract the outermost {...} 
            json_match = re.search(r'^\{.*\}$', raw, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError as e:
                    print(f"[world_builder] JSON parse failed: {e}\nRaw:\n{raw[:1000]}")
                    return {"reply": "I had trouble formatting the scene. Try rephrasing your request.", "world_code": None}
            else:
                print(f"[world_builder] No JSON found. Raw:\n{raw[:1000]}")
                return {"reply": raw[:400] if raw else "No response.", "world_code": None}

        world_code = result.get("world_code") or ""
        if world_code:
            world_code = sanitize_world_code(world_code)

        return {
            "reply": result.get("reply", "Here's your world!"),
            "world_code": world_code or None,
        }

    except Exception as e:
        import traceback
        print(f"[world_builder] Exception:\n{traceback.format_exc()}")
        return {
            "reply": f"Error generating world: {str(e)}",
            "world_code": None,
        }