import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from settings import settings

# Initialize the OpenAI client
client = OpenAI(
    api_key=settings["openAIToken"],
    default_headers={"OpenAI-Beta": "assistants=v1"},
)

_state_path = Path(settings["assistant_state_file"])
_instructions_path = Path(settings["assistant_instructions_file"])
_schema_path = Path(settings["assistant_schema_file"])
_assistant_model = settings["assistant_model"]
_assistant_name = settings["assistant_name"].strip() or None
_assistant_description = settings["assistant_description"].strip() or None

_assistant_id: Optional[str] = None
_assistant_lock = asyncio.Lock()

def _load_text_file(path: Path) -> Optional[str]:
    try:
        content = path.read_text(encoding="utf-8").strip()
        return content or None
    except FileNotFoundError:
        logging.error("Instructions file not found at %s", path)
    except OSError as exc:
        logging.error("Failed reading instructions file %s: %s", path, exc)
    return None


def _load_json_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logging.error("Schema file %s must contain a JSON object.", path)
            return None
        return data
    except FileNotFoundError:
        logging.error("Schema file not found at %s", path)
    except json.JSONDecodeError as exc:
        logging.error("Invalid JSON schema in %s: %s", path, exc)
    except OSError as exc:
        logging.error("Failed reading schema file %s: %s", path, exc)
    return None


def _read_cached_assistant_id() -> Optional[str]:
    if not _state_path.exists():
        return None
    try:
        data = json.loads(_state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logging.error("Invalid assistant state file %s: %s", _state_path, exc)
        return None
    except OSError as exc:
        logging.error("Unable to read assistant state %s: %s", _state_path, exc)
        return None

    assistant_id = data.get("assistant_id") if isinstance(data, dict) else None
    if assistant_id and isinstance(assistant_id, str):
        return assistant_id
    logging.error("Assistant state file %s missing valid assistant_id.", _state_path)
    return None


def _persist_assistant_id(assistant_id: str) -> None:
    try:
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _state_path.write_text(
            json.dumps({"assistant_id": assistant_id}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logging.error("Unable to persist assistant ID to %s: %s", _state_path, exc)


async def get_assistant_id() -> Optional[str]:
    global _assistant_id

    if _assistant_id:
        return _assistant_id

    async with _assistant_lock:
        if _assistant_id:
            return _assistant_id

        cached = _read_cached_assistant_id()
        if cached:
            _assistant_id = cached
            return _assistant_id

        instructions = await asyncio.to_thread(_load_text_file, _instructions_path)
        if not instructions:
            logging.error("Assistant instructions are required to create an assistant.")
            return None

        schema = await asyncio.to_thread(_load_json_file, _schema_path)
        if not schema:
            logging.error("Assistant JSON schema is required to create an assistant.")
            return None

        request: dict[str, Any] = {
            "model": _assistant_model,
            "instructions": instructions,
        }
        if _assistant_name:
            request["name"] = _assistant_name
        if _assistant_description:
            request["description"] = _assistant_description
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "assistant_response",
                "strict": True,
                "schema": schema
            }
        }

        try:
            assistant = await asyncio.to_thread(client.beta.assistants.create, **request)
        except Exception as exc:
            logging.error("Failed to create assistant: %s", exc)
            return None

        _assistant_id = assistant.id
        _persist_assistant_id(_assistant_id)
        logging.info("Created new assistant with id %s", _assistant_id)
        return _assistant_id

async def update_assistant_model(new_model: str) -> bool:
    global _assistant_model
    assistant_id = await get_assistant_id()
    if not assistant_id:
        return False
    try:
        await asyncio.to_thread(
            client.beta.assistants.update,
            assistant_id=assistant_id,
            model=new_model,
        )
        _assistant_model = new_model
        logging.info("Assistant model updated to %s", new_model)
        return True
    except Exception as exc:
        logging.error("Failed to update assistant model: %s", exc)
        return False

async def create_new_thread():
    """Create a new OpenAI thread."""
    try:
        thread = await asyncio.to_thread(client.beta.threads.create)
        return thread.id
    except Exception as exc:
        logging.error("Error creating thread: %s", exc)
        return None


async def check_run(thread_id, run_id):
    """Wait until an OpenAI run finishes."""
    while True:
        try:
            run = await asyncio.to_thread(
                client.beta.threads.runs.retrieve,
                thread_id=thread_id,
                run_id=run_id,
            )

            if run.status == "completed":
                break
            elif run.status == "failed":
                print(f"\n OPENAI ERROR")
                print(f"Reason: {run.last_error}\n")
                break
            elif run.status in ["expired", "cancelled"]:
                print(f"OPENAI ERROR: Run status is {run.status}")
                break
            await asyncio.sleep(3)
        except Exception as exc:
            print(f"\nError checking run status: {exc}")
            break


async def GPT_response(thread_id, prompt):
    """Send a prompt to the assistant and return the response payload."""
    try:
        await asyncio.to_thread(
            client.beta.threads.messages.create,
            thread_id=thread_id,
            role="user",
            content=prompt,
        )

        assistant_id = await get_assistant_id()
        if not assistant_id:
            return {
                "response": "Assistant configuration is incomplete. Check server logs.",
                "values": {},
            }

        run = await asyncio.to_thread(
            client.beta.threads.runs.create,
            thread_id=thread_id,
            assistant_id=assistant_id,
        )

        await check_run(thread_id, run.id)

        messages = await asyncio.to_thread(
            client.beta.threads.messages.list, thread_id=thread_id
        )
        if not messages.data:
            return {"response": "No response received", "values": {}}

        assistant_message = messages.data[0].content[0].text.value

        try:
            return json.loads(assistant_message)
        except json.JSONDecodeError:
            return {"response": assistant_message, "values": {}}
    except Exception as exc:
        logging.error("Error in GPT response: %s", exc)
        return {"response": "An error occurred while processing your request", "values": {}}


async def transcribe_audio(audio_bytes: bytes) -> str | None:
    """Transcribe WAV audio bytes using the configured OpenAI model."""
    try:
        result = client.audio.transcriptions.create(
            model=settings["transcription_model"],
            file=("speech.wav", audio_bytes, "audio/wav"),
            response_format="text",
            language="en",
        )
        return result if isinstance(result, str) else getattr(result, "text", None)
    except Exception as exc:
        logging.error("Error transcribing audio: %s", exc)
        return None
