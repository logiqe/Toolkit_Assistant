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
import copy
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
    # Fix chemins de fichiers
    html = re.sub(r"load\('([^']*)'\)", lambda m: "load('" + m.group(1).replace("\\", "/") + "')", html)
    html = re.sub(r'load\("([^"]*)"\)', lambda m: 'load("' + m.group(1).replace("\\", "/") + '")', html)
    
    # Fix scripts sans defer/DOMContentLoaded
    # Remplacer <script> par <script defer> dans le <head>
    html = re.sub(
        r'<script(?!\s+(?:src|type|defer|async)[^>]*defer)([^>]*)>',
        lambda m: f'<script{m.group(1)} defer>' if 'src=' in m.group(1) else m.group(0),
        html
    )
    
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
    try:
        client = get_client()

        system_prompt = load_system_prompt()
        if hardware_context:
            system_prompt += "\n\n" + format_hardware_context(hardware_context)
            if hardware_context.get("chat_history"):
                system_prompt += (
                    f"\n\n## HARDWARE CONFIGURED IN MAIN CHAT\n"
                    f"The user already set up their hardware in the main assistant chat. "
                    f"Here's the conversation history for context:\n{hardware_context['chat_history']}"
                )

        # Detect vision content BEFORE copying
        has_vision = any(
            isinstance(msg.get("content"), list)
            for msg in conversation_history
        )

        # Always work on a deep copy — never mutate the caller's history
        messages_copy = copy.deepcopy(conversation_history)

        if not has_vision:
            last = messages_copy[-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            last["content"].append({
                "type": "text",
                "text": 'IMPORTANT: Respond ONLY with a valid JSON object {"reply": "...", "world_code": "..."}'
            })

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=8000,
            system=system_prompt,
            messages=convert_messages_for_anthropic(messages_copy),
        )
        raw = response.content[0].text.strip()
        print(f"[world_builder] raw response (first 500):\n{raw[:500]}")

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                except json.JSONDecodeError:
                    print(f"[world_builder] JSON parse failed. Full raw:\n{raw[:2000]}")
                    return {"reply": raw[:500], "world_code": None}
            else:
                print(f"[world_builder] No JSON found. Full raw:\n{raw[:2000]}")
                return {"reply": raw[:500], "world_code": None}

        world_code = result.get("world_code") or ""
        return {
            "reply": result.get("reply", "Here's your world!"),
            "world_code": sanitize_world_code(world_code) or None,
        }

    except Exception as e:
        import traceback
        print(f"[world_builder] Exception:\n{traceback.format_exc()}")
        return {
            "reply": f"Error generating world: {str(e)}",
            "world_code": None,
        }
