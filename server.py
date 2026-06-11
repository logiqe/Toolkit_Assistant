from fastapi import FastAPI, Query, Request, Response, Cookie, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import uuid
from pydantic import BaseModel
from datetime import datetime
import asyncio
import json
import os
import sys
import secrets
import base64
import httpx
import paho.mqtt.client as mqtt
import smtplib, random, time
import resend

from email.mime.text import MIMEText

from pathlib import Path
from settings import settings
from OpenAiClientAssistant import create_new_thread, GPT_response, update_assistant_model

pending_verifications: dict[str, dict] = {}
user_sessions: set[str] = set() 

# ── World Builder import ──
try:
    from world_builder import generate_world
    WORLD_BUILDER_AVAILABLE = True
except ImportError:
    WORLD_BUILDER_AVAILABLE = False
    print("world_builder.py not found — /world-chat will be unavailable")

app = FastAPI()

Path("uploads").mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# --- Global state ---
sessions = {}
admin_sessions: set[str] = set()
RUNTIME_CONFIG = dict(settings)

WORLD_STATE_FILE = "world_states.json"

# ── WebSocket connections: board_id → list of connected WebSocket clients ──
ws_connections: dict[str, list[WebSocket]] = {}

# ── World conversation histories: board_id → list of messages ──
world_histories: dict[str, list[dict]] = {}
# Stores the latest generated HTML scene per board_id — served via /scene/<board_id>
world_scenes: dict[str, str] = {}     # HTML with bridge injected (for serving)
world_scenes_raw: dict[str, str] = {}  # HTML without bridge (for LLM context)

# --- Render API config ---
RENDER_API_KEY = os.environ.get("RENDER_API_KEY")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID")


async def update_render_env_var(key: str, value: str) -> tuple[bool, str]:
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        return False, "RENDER_API_KEY or RENDER_SERVICE_ID not configured"

    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars"
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return False, f"GET failed: {r.status_code} {r.text}"
        env_vars = r.json()

        existing = {item["envVar"]["key"]: item["envVar"]["value"] for item in env_vars}
        existing[key] = value

        payload = [{"key": k, "value": v} for k, v in existing.items()]
        r = await client.put(url, headers=headers, json=payload)
        if r.status_code not in (200, 201):
            return False, f"PUT failed: {r.status_code} {r.text}"
        return True, "OK"


def reload_runtime_config():
    global RUNTIME_CONFIG
    RUNTIME_CONFIG["assistant_model"] = os.environ.get(
        "OPENAI_ASSISTANT_MODEL", RUNTIME_CONFIG["assistant_model"]
    )
    RUNTIME_CONFIG["Welcom_msg"] = os.environ.get(
        "WELCOME_MESSAGE", RUNTIME_CONFIG["Welcom_msg"]
    )


def load_logs_from_file():
    global sessions
    if os.path.exists("test_participants_logs.json"):
        try:
            with open("test_participants_logs.json", "r", encoding="utf-8") as f:
                sessions = json.load(f)
            print(f"✅ {len(sessions)} sessions restored")
        except Exception as e:
            print(f"Error loading logs: {e}")


def save_logs_to_file():
    try:
        with open("test_participants_logs.json", "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error while saving logs: {e}")

def save_world_states():
    with open(WORLD_STATE_FILE, "w") as f:
        json.dump(world_histories, f)

def load_world_states():
    global world_histories
    if os.path.exists(WORLD_STATE_FILE):
        with open(WORLD_STATE_FILE, "r") as f:
            world_histories = json.load(f)

# Au démarrage du serveur
load_world_states()

@app.on_event("startup")
async def startup_event():
    load_logs_from_file()
    load_world_states()
    print(f"🚀 Server started — {len(world_histories)} world states loaded")


def get_session(board_id: str):
    if board_id not in sessions:
        sessions[board_id] = {
            "thread_id": None,
            "last_sensor": "No data",
            "history": [{"sender": "ai", "text": RUNTIME_CONFIG["Welcom_msg"]}],
            "calibrated_sensors": {}
        }
    return sessions[board_id]


# ──────────────────────────────────────────────────────────────────────────────
# MQTT
# ──────────────────────────────────────────────────────────────────────────────

broker = settings["broker"]
port = settings.get("mqtt_port", 1883)
base_topic = settings["topic"].replace("/{BOARD_ID}", "")
mqtt_auth = {'username': settings["mqtt_user"], 'password': settings["mqtt_password"]} if settings.get("mqtt_user") else None


def on_message(client, userdata, msg):
    print(f"MQTT reçu à : {datetime.now().isoformat()}")
    try:
        payload = msg.payload.decode("utf-8")
        parts = msg.topic.split("/")
        if len(parts) >= 3:
            board_id = parts[1]
            session = get_session(board_id)
            session["last_sensor"] = payload
            try:
                data = json.loads(payload)
                if data.get("status") == "calibration_finished":
                    session["calibrated_sensors"][data["sensor"]] = {
                        "min": data["min"],
                        "max": data["max"]
                    }
                    session["pending_hardware_update"] = (
                        f"SYSTEM NOTIFICATION: Calibration for '{data['sensor']}' is COMPLETE. "
                        f"Measured Min: {data['min']}, Measured Max: {data['max']}. "
                        f"This sensor is now calibrated."
                    )
                    print(f"Calibration saved for {board_id}")
            except Exception as e:
                print(f"Error parsing calibration: {e}")

            print(f"📡 Data received from the board {board_id} : {payload}")

            # ── Bridge MQTT → WebSocket clients ──
            # Run in the event loop to broadcast to all WS clients for this board
            if main_loop:
                main_loop.call_soon_threadsafe(
                asyncio.ensure_future,
                broadcast_sensor_data(board_id, payload)
                )

    except Exception as e:
        print(f"Error MQTT: {e}")


async def broadcast_sensor_data(board_id: str, raw_payload: str):
    """Forward raw MQTT sensor payload to all WebSocket clients watching this board."""
    clients = ws_connections.get(board_id, [])
    if not clients:
        return

    # Parse and re-wrap for the frontend
    try:
        data = json.loads(raw_payload)
        message = json.dumps({"board_id": board_id, "data": data})
    except Exception:
        message = json.dumps({"board_id": board_id, "data": {"raw": raw_payload}})

    dead = []
    for ws in clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)

    # Clean up disconnected clients
    for ws in dead:
        clients.remove(ws)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ MQTT connected")
        client.subscribe(f"{base_topic}/+/sensors")
    else:
        print(f"❌ MQTT connection failed: {rc}")


def on_disconnect(client, userdata, rc):
    print(f"MQTT disconnected ({rc}), auto-reconnect...")


mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message
mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
if mqtt_auth:
    mqtt_client.username_pw_set(**mqtt_auth)
mqtt_client.connect_async(broker, port)
mqtt_client.loop_start()


# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────

class UserInput(BaseModel):
    text: str


class LoginInput(BaseModel):
    password: str


class ConfigUpdate(BaseModel):
    key: str
    value: str


class EmailInput(BaseModel):
    email: str

class CodeInput(BaseModel):
    email: str
    code: str

# --- Helpers ---
def is_admin(admin_token: str = Cookie(default=None)) -> bool:
    return admin_token in admin_sessions

def is_user(user_token: str | None) -> bool:
    return user_token in user_sessions

def _send_email(to: str, code: str):
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
    with httpx.Client() as client:
        r = client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": "Toolkit <onboarding@resend.dev>",
                "to": [to],
                "subject": "Your access code",
                "text": f"Your access code: {code}\n\nValid for 10 minutes."
            }
        )
    if r.status_code != 200:
        raise Exception(f"Resend error: {r.status_code} {r.text}")

# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC ROUTES
# ──────────────────────────────────────────────────────────────────────────────

# Au démarrage de l'app
main_loop = None

@app.on_event("startup")
async def startup():
    global main_loop
    main_loop = asyncio.get_event_loop()


@app.get("/")
async def index(request: Request, board_id: str = Query(default=None), user_token: str = Cookie(default=None)):
    if not is_user(user_token):
        redirect_url = f"/login?board_id={board_id}" if board_id else "/login"
        return RedirectResponse(redirect_url)
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/style.css")
async def get_style():
    return FileResponse("style.css")


@app.get("/world")
async def get_world_page(board_id: str = Query(default=None), user_token: str = Cookie(default=None)):
    if not is_user(user_token):
        redirect_url = f"/login?board_id={board_id}" if board_id else "/login"
        return RedirectResponse(redirect_url)
    with open("world.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/history")
async def get_history(board_id: str = Query(...)):
    session = get_session(board_id)
    return {"history": session["history"]}


@app.get("/welcome")
async def get_welcome():
    return {"message": RUNTIME_CONFIG["Welcom_msg"]}


@app.post("/reset")
async def reset_conversation(board_id: str = Query(...)):
    session = get_session(board_id)

    if len(session.get("history", [])) > 1:
        archive_session(board_id, session)

    session["thread_id"] = await create_new_thread()
    session["history"] = [{"sender": "ai", "text": RUNTIME_CONFIG["Welcom_msg"]}]
    session["calibrated_sensors"] = {}
    session.pop("pending_hardware_update", None)

    # Also reset world history for this board
    world_histories.pop(board_id, None)

    return {"status": "success"}


@app.post("/chat")
async def chat_with_ai(
    user_input: UserInput,
    board_id: str = Query(...),
    session_id: str = Cookie(default=None),
    user_token: str = Cookie(default=None)
):
    # Utilise session_id si dispo, sinon fallback board_id (rétrocompat)
    key = session_id if session_id else board_id
    session = get_session(key)
    if session["thread_id"] is None:
        session["thread_id"] = await create_new_thread()

    calibration_context = json.dumps(session['calibrated_sensors'])
    message_to_ai = calibration_context + "\n\n"

    if session.get("pending_hardware_update"):
        message_to_ai += session["pending_hardware_update"] + "\n\n"
        del session["pending_hardware_update"]

    message_to_ai += f"User message: {user_input.text}"
    session["history"].append({"sender": "user", "text": user_input.text})

    response = await GPT_response(session["thread_id"], message_to_ai)
    texte_ia = response.get("answer", "Action done.")
    valeurs_mqtt = response.get("MQTT_value", {})

    if valeurs_mqtt:
        topic_envoi = f"{base_topic}/{board_id}/command"
        mqtt_client.publish(topic_envoi, json.dumps(valeurs_mqtt), qos=1)
        print(f"📤 Order sent to {board_id} : {valeurs_mqtt}")
        session["last_hardware_config"] = valeurs_mqtt
        session["last_hardware_config_at"] = datetime.now().isoformat()


    session["history"].append({"sender": "ai", "text": texte_ia})
    save_logs_to_file()

    return {"reply": texte_ia, "MQTT_value": valeurs_mqtt}


# ──────────────────────────────────────────────────────────────────────────────
# WORLD BUILDER ENDPOINT
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/world-chat")
async def world_chat(user_input: UserInput, board_id: str = Query(...)):
    """
    Chat endpoint for the World Builder.
    Uses Claude (Anthropic API) to generate Three.js scenes.
    Maintains per-board conversation history.
    """
    if not WORLD_BUILDER_AVAILABLE:
        return JSONResponse(
            {"reply": "World Builder is not available. Make sure world_builder.py and ANTHROPIC_API_KEY are configured.", "world_code": None},
            status_code=503
        )

    # Maintain world conversation history per board
    if board_id not in world_histories:
        world_histories[board_id] = []

    # Add user message to history
    world_histories[board_id].append({
        "role": "user",
        "content": user_input.text
    })
    save_world_states()

    # Build hardware context from the main session
    hardware_context = build_hardware_context(board_id)

    # Generate response in a thread to avoid blocking
    result = await asyncio.to_thread(generate_world, world_histories[board_id], hardware_context)

    # Add assistant response to history — store summary ONLY, never the full HTML
    # (storing world_code in history multiplies input tokens with every turn)
    world_histories[board_id].append({
        "role": "assistant",
        "content": result.get("reply", "Scene generated.")
        + (" [scene updated]" if result.get("world_code") else "")
    })
    save_world_states()

    # Keep history bounded (last 20 exchanges = 40 messages)
    if len(world_histories[board_id]) > 40:
        world_histories[board_id] = world_histories[board_id][-40:]

    # Store latest scene for /scene/<board_id> — served as a real URL (no srcdoc)
    if result.get("world_code"):
        world_scenes[board_id] = _inject_sensor_bridge(result["world_code"])
        world_scenes_raw[board_id] = result["world_code"]

    print(f"🌍 World generated for {board_id}: {result['reply'][:60]}...")

    return {
        "reply": result.get("reply", "Here's your world!"),
        "world_code": result.get("world_code", None),
        "scene_url": f"/scene/{board_id}" if result.get("world_code") else None
    }


@app.post("/world-chat-with-files")
async def world_chat_with_files(
    board_id: str = Query(...),
    text: str = Form(""),
    files: list[UploadFile] = File(default=[])
):
    """
    World Builder chat with file uploads.
    Images → GPT-4o vision. 3D files → context notes.
    """
    if not WORLD_BUILDER_AVAILABLE:
        return JSONResponse({"reply": "World Builder is not available.", "world_code": None}, status_code=503)

    SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
    SUPPORTED_3D_EXTENSIONS = {".glb", ".gltf", ".obj"}
    MAX_IMAGE_SIZE = 4 * 1024 * 1024
    MAX_FILE_COUNT = 4

    image_parts = []
    asset_notes = []

    for upload in files[:MAX_FILE_COUNT]:
        ext = os.path.splitext(upload.filename or "")[1].lower()
        content_type = upload.content_type or ""

        if content_type in SUPPORTED_IMAGE_TYPES or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            data = await upload.read()
            if len(data) > MAX_IMAGE_SIZE:
                asset_notes.append(f"Image {upload.filename!r} skipped (>4 MB).")
                continue
            mime = content_type if content_type in SUPPORTED_IMAGE_TYPES else "image/png"
            b64 = base64.b64encode(data).decode("utf-8")
            image_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}
            })
        elif ext in SUPPORTED_3D_EXTENSIONS:
            data = await upload.read()
            # Sauvegarder le fichier
            upload_dir = Path("uploads")
            upload_dir.mkdir(exist_ok=True)
            unique_name = f"{uuid.uuid4().hex}_{upload.filename}"
            (upload_dir / unique_name).write_bytes(data)
            public_url = f"/uploads/{unique_name}"

            # URL absolue pour résoudre le problème du Blob URL dans l'iframe
            asset_notes.append(
                f"3D asset '{upload.filename}' is available at absolute URL: "
                f"http://localhost:8000{public_url} — "
                f"IMPORTANT: use this EXACT full URL in loader.load(), never a relative path."
            )
        else:
            asset_notes.append(f"File {upload.filename!r} ignored (unsupported).")

    user_text = text.strip()
    if asset_notes:
        user_text += "\n\n[Files: " + " | ".join(asset_notes) + "]"
    if not user_text:
        user_text = "(User uploaded files without a text message)"

    if board_id not in world_histories:
        world_histories[board_id] = []

    content_parts = [{"type": "text", "text": user_text}] + image_parts
    world_histories[board_id].append({
        "role": "user",
        "content": content_parts if image_parts else user_text
    })
    save_world_states()

    hardware_context = build_hardware_context(board_id)
    result = await asyncio.to_thread(generate_world, world_histories[board_id], hardware_context)

    world_histories[board_id].append({"role": "assistant", "content": result.get("reply", "Scene generated.") + (" [scene updated]" if result.get("world_code") else "")})
    save_world_states()

    if len(world_histories[board_id]) > 40:
        world_histories[board_id] = world_histories[board_id][-40:]

    if result.get("world_code"):
        world_scenes[board_id] = _inject_sensor_bridge(result["world_code"])
        world_scenes_raw[board_id] = result["world_code"]

    print(f"🌍 World+files for {board_id}: {result['reply'][:60]}...")
    return {
        "reply": result.get("reply", "Here's your world!"),
        "world_code": result.get("world_code", None),
        "scene_url": f"/scene/{board_id}" if result.get("world_code") else None
    }


def _inject_sensor_bridge(html: str, standalone: bool = False) -> str:
    """
    Inject the sensor + passthrough bridge into the generated HTML.
    standalone=True adds a WebSocket client so the scene can receive sensor
    data when opened directly (not inside an iframe).
    The bridge is injected before </body> so Three.js is already loaded.
    """
    ws_client = ""
    if standalone:
        ws_client = """
// ── Standalone WebSocket sensor client ──
// Reads board_id from URL params and connects directly to /ws/<board_id>
(function() {
  const params = new URLSearchParams(location.search);
  const bid = params.get('board_id');
  if (!bid || bid === 'none') return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  function connect() {
    const ws = new WebSocket(proto + '://' + location.host + '/ws/' + bid);
    ws.onmessage = function(e) {
      try {
        const msg = JSON.parse(e.data);
        const data = msg.data || msg;
        if (data && typeof window.onSensorUpdate === 'function') {
          window.onSensorUpdate(data);
        }
      } catch(_) {}
    };
    ws.onclose = function() { setTimeout(connect, 3000); };
  }
  connect();
})();
"""

    bridge = f"""
<script>
// ── Toolkit Sensor & Passthrough Bridge ──
window.sensorData = {{}};
window._passthroughActive = false;

window.addEventListener('message', function(e) {{
  if (!e.data) return;
  if (e.data.type === 'sensor') {{
    window.sensorData = e.data.data;
    if (typeof window.onSensorUpdate === 'function') window.onSensorUpdate(e.data.data);
  }}
  if (e.data.type === 'passthrough') {{
    window._passthroughActive = e.data.enabled;
    document.documentElement.style.background = e.data.enabled ? 'transparent' : '';
    document.body.style.background = e.data.enabled ? 'transparent' : '';
    if (window._renderer) window._renderer.setClearColor(0x000000, e.data.enabled ? 0 : 1);
    if (window._scene) window._scene.background = e.data.enabled ? null : (window._sceneBg || null);
  }}
}});

// Maintain passthrough every frame via setAnimationLoop patch
(function() {{
  const _origSet = THREE && THREE.WebGLRenderer && THREE.WebGLRenderer.prototype.setAnimationLoop;
  if (!_origSet) return;
  THREE.WebGLRenderer.prototype.setAnimationLoop = function(cb) {{
    _origSet.call(this, cb ? function(t, frame) {{
      if (window._passthroughActive) {{
        this.setClearColor(0x000000, 0);
        if (window._scene) window._scene.background = null;
      }}
      cb(t, frame);
    }}.bind(this) : null);
  }};
}})();

{ws_client}
</script>
"""
    if '</body>' in html:
        return html.replace('</body>', bridge + '</body>', 1)
    return html + bridge


@app.get("/scene/{board_id}")
async def get_scene(board_id: str, vr: str = "0"):
    """
    Serve the latest generated 3D scene as a standalone HTML page.
    This is a top-level document — required for WebXR on Meta Quest.
    ?vr=1 adds a standalone WebSocket client for live sensor data.
    """
    standalone = vr == "1"
    raw_html = world_scenes.get(board_id)

    if not raw_html:
        # Try to reconstruct from history
        history = world_histories.get(board_id, [])
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                try:
                    data = json.loads(msg["content"])
                    if data.get("world_code"):
                        raw_html = data["world_code"]
                        break
                except Exception:
                    pass

    if not raw_html:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='background:#111;color:#fff;"
            "font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh'>"
            "<p>No scene generated yet. Go back and describe your world.</p>"
            "</body></html>",
            status_code=404
        )

    # Always inject bridge; standalone adds WS client for sensor data
    html = _inject_sensor_bridge(raw_html, standalone=standalone)
    return HTMLResponse(html)



def _get_raw_world_code(board_id: str) -> str | None:
    """Return the raw world HTML (without bridge) for LLM context. Capped at 6000 chars."""
    raw = world_scenes_raw.get(board_id)
    if raw:
        return raw[:6000]
    # Fallback: try to reconstruct from history
    for msg in reversed(world_histories.get(board_id, [])):
        if msg.get("role") == "assistant":
            try:
                data = json.loads(msg["content"])
                if isinstance(data, dict) and data.get("world_code"):
                    return data["world_code"][:6000]
            except Exception:
                pass
    return None


def build_hardware_context(board_id: str) -> dict:
    session = sessions.get(board_id)
    if not session:
        return {}

    chat_history = session.get("history", [])
    history_text = "\n".join(
        f"{msg['sender'].upper()}: {msg['text']}" 
        for msg in chat_history[-20:]
    )

    inputs = []
    outputs = []

    last_hw = session.get("last_hardware_config", {})
    hw_config = last_hw.get("hardware_config", {})

    # Inputs
    for name, info in hw_config.get("inputs", {}).items():
        if info:  # ignorer les null
            inputs.append({
                "name": name,
                "type": info.get("type", ""),
                "pin": info.get("pin", "?")
            })

    # Outputs
    for name, info in hw_config.get("outputs", {}).items():
        if info:  # ignorer les null
            outputs.append({
                "name": name,
                "type": info.get("type", ""),
                "pin": info.get("pin", "?")
            })

    # Get raw world code (without bridge injection) for context
    # We store raw HTML in world_scenes; strip bridge if needed
    raw_scene = None
    for msg in reversed(world_histories.get(board_id, [])):
        # world_histories now only stores text summaries — get raw HTML from world_scenes
        break
    raw_scene = None  # raw HTML available via world_scenes[board_id] if needed

    return {
        "configured_inputs": inputs,
        "configured_outputs": outputs,
        "last_sensor_value": session.get("last_sensor"),
        "calibrated_sensors": session.get("calibrated_sensors", {}),
        "chat_history": history_text,
        "last_world_code": _get_raw_world_code(board_id)
    }

# ──────────────────────────────────────────────────────────────────────────────
# WEBSOCKET — MQTT → Virtual World bridge
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/debug/hardware")
async def debug_hardware(board_id: str = Query(...)):
    session = sessions.get(board_id, {})
    return {
        "last_hardware_config": session.get("last_hardware_config"),
        "hardware_context": build_hardware_context(board_id)
    }


@app.websocket("/ws/{board_id}")
async def websocket_endpoint(websocket: WebSocket, board_id: str):
    """
    WebSocket endpoint that bridges MQTT sensor data to the virtual world.
    Each board_id can have multiple WebSocket clients (e.g. multiple browser tabs).
    """
    await websocket.accept()

    if board_id not in ws_connections:
        ws_connections[board_id] = []
    ws_connections[board_id].append(websocket)

    print(f"🔌 WebSocket connected for board {board_id} ({len(ws_connections[board_id])} client(s))")

    try:
        # Send last known sensor state immediately on connect
        try:
            session = get_session(board_id)
            last_sensor = session.get("last_sensor", "No data")
            if last_sensor != "No data":
                data = json.loads(last_sensor)
                await websocket.send_text(json.dumps({"board_id": board_id, "data": data}))
        except Exception as e:
            print(f"⚠️ Error sending initial state: {e}")
            # Ne pas fermer la connexion, continuer quand même

        # Keep connection alive
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping"}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"❌ WebSocket error for {board_id}: {e}")  # <-- regarde les logs Render
    finally:
        if board_id in ws_connections and websocket in ws_connections[board_id]:
            ws_connections[board_id].remove(websocket)
        print(f"🔌 WebSocket disconnected for board {board_id}")


# ──────────────────────────────────────────────────────────────────────────────
# ADMIN ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/admin/login")
async def admin_login_page():
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/admin/login")
async def admin_login(data: LoginInput, response: Response):
    admin_password = settings.get("admin_password", os.environ.get("ADMIN_PASSWORD", ""))
    if not admin_password or data.password != admin_password:
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    token = secrets.token_hex(32)
    admin_sessions.add(token)
    response.set_cookie("admin_token", token, httponly=True, samesite="strict")
    return {"status": "ok"}


@app.post("/admin/logout")
async def admin_logout(response: Response, admin_token: str = Cookie(default=None)):
    if admin_token in admin_sessions:
        admin_sessions.discard(admin_token)
    response.delete_cookie("admin_token")
    return {"status": "ok"}


@app.get("/admin")
async def admin_page(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return RedirectResponse("/admin/login")
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/admin/config")
async def get_config(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    keys = [
        "OPENAI_ASSISTANT_MODEL", "OPENAI_API_KEY", "WELCOME_MESSAGE",
        "MQTT_BROKER", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_TOPIC",
        "TRANSCRIPTION_MODEL", "OPENAI_ASSISTANT_NAME",
    ]
    sensitive = {"OPENAI_API_KEY", "MQTT_PASSWORD", "ADMIN_PASSWORD"}

    safe = {}
    for k in keys:
        v = os.environ.get(k, "")
        safe[k] = ("*" * 8) if (k in sensitive and v) else v
    return safe


@app.post("/admin/config")
async def update_config(data: ConfigUpdate, admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    ok, msg = await update_render_env_var(data.key, data.value)
    if not ok:
        return JSONResponse({"error": f"Render update failed: {msg}"}, status_code=500)

    os.environ[data.key] = data.value
    reload_runtime_config()

    if data.key == "OPENAI_ASSISTANT_MODEL":
        if os.environ.get("OPENAI_API_KEY"):
            ok_ai = await update_assistant_model(data.value)
            if not ok_ai:
                return JSONResponse({"error": "Failed to update assistant model on OpenAI"}, status_code=500)

    return {"status": "saved — Render will auto-redeploy in ~1min"}


@app.post("/admin/restart")
async def restart_server(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    save_logs_to_file()

    async def do_restart():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(do_restart())
    return {"status": "restarting"}


def archive_session(board_id: str, session: dict):
    try:
        archives = {}
        if os.path.exists("archives.json"):
            with open("archives.json", "r", encoding="utf-8") as f:
                archives = json.load(f)

        if board_id not in archives:
            archives[board_id] = []

        archives[board_id].append({
            "archived_at": datetime.now().isoformat(),
            "history": session.get("history", []),
            "calibrated_sensors": session.get("calibrated_sensors", {})
        })

        with open("archives.json", "w", encoding="utf-8") as f:
            json.dump(archives, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error archiving session: {e}")


@app.post("/admin/clear-session")
async def clear_session(board_id: str = Query(...), admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if board_id in sessions:
        archive_session(board_id, sessions[board_id])
        del sessions[board_id]
        save_logs_to_file()
    world_histories.pop(board_id, None)
    return {"status": "cleared"}


@app.get("/admin/archives")
async def get_archives(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not os.path.exists("archives.json"):
        return {}
    with open("archives.json", "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/admin/clear-all")
async def clear_all_sessions(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    sessions.clear()
    world_histories.clear()
    save_logs_to_file()
    return {"status": "cleared"}


@app.get("/admin/logs")
async def get_all_logs(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return sessions


@app.get("/admin/mqtt-test")
async def test_mqtt(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    connected = mqtt_client.is_connected()
    return {"connected": connected, "broker": broker, "port": port}


@app.get("/admin/ws-status")
async def ws_status(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return {
        board_id: len(clients)
        for board_id, clients in ws_connections.items()
    }


@app.post("/admin/reset-assistant")
async def reset_assistant(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from pathlib import Path
    state_file = Path(settings["assistant_state_file"])
    if state_file.exists():
        state_file.unlink()
        return {"status": "deleted", "file": str(state_file)}
    return {"status": "file not found"}

@app.get("/world-state")
async def get_world_state(board_id: str = Query(...)):
    """Return the current world state (chat + latest world code) for a board."""
    history = world_histories.get(board_id, [])
    
    # Reconstruct chat messages for display
    chat_messages = []
    latest_world_code = None
    
    for msg in history:
        if msg["role"] == "user":
            content = msg["content"]
            # content peut être string ou list de content blocks
            if isinstance(content, list):
                text = " ".join(
                    block["text"] for block in content 
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = content or ""
            chat_messages.append({"side": "user", "text": text})
        elif msg["role"] == "assistant":
            try:
                data = json.loads(msg["content"])
                chat_messages.append({"side": "ai", "text": data.get("reply", "")})
                if data.get("world_code"):
                    latest_world_code = data["world_code"]
            except:
                pass
    
    return {
        "chat_messages": chat_messages,
        "world_code": latest_world_code
    }

@app.post("/world-clear")
async def clear_world_history(board_id: str = Query(...)):
    world_histories.pop(board_id, None)
    save_world_states()
    return {"status": "cleared"}

@app.get("/login")
async def user_login_page():
    with open("login_user.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.post("/user/send-code")
async def send_code(data: EmailInput):
    code = str(random.randint(100000, 999999))
    pending_verifications[data.email] = {
        "code": code,
        "expires": time.time() + 600
    }
    await asyncio.to_thread(_send_email, data.email, code)  # ← non-bloquant
    return {"status": "sent"}


class CodeInput(BaseModel):
    email: str
    code: str
    board_id: str | None = None  # ← ajouter

@app.post("/user/verify-code")
async def verify_code(data: CodeInput, response: Response):
    entry = pending_verifications.get(data.email)
    if not entry or time.time() > entry["expires"] or entry["code"] != data.code:
        return JSONResponse({"error": "Invalid or expired code"}, status_code=401)
    
    token = secrets.token_hex(32)
    user_sessions.add(token)
    del pending_verifications[data.email]
    
    # Créer un session_id unique par utilisateur (email + board)
    session_id = f"{data.board_id or 'unknown'}_{secrets.token_hex(8)}"
    
    response.set_cookie("user_token", token, httponly=True, samesite="strict")
    response.set_cookie("session_id", session_id, httponly=True, samesite="strict")
    
    return {"status": "ok", "session_id": session_id}