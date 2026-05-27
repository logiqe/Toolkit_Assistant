from fastapi import FastAPI, Query, Request, Response, Cookie
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from datetime import datetime
import asyncio
import json
import os
import sys
import secrets
import httpx
import paho.mqtt.client as mqtt

from settings import settings
from OpenAiClientAssistant import create_new_thread, GPT_response, update_assistant_model

app = FastAPI()

# --- Global state ---
sessions = {}
admin_sessions: set[str] = set()
RUNTIME_CONFIG = dict(settings)

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


@app.on_event("startup")
async def startup_event():
    load_logs_from_file()


def get_session(board_id: str):
    if board_id not in sessions:
        sessions[board_id] = {
            "thread_id": None,
            "last_sensor": "No data",
            "history": [{"sender": "ai", "text": RUNTIME_CONFIG["Welcom_msg"]}],
            "calibrated_sensors": {}
        }
    return sessions[board_id]


# --- MQTT ---
broker = settings["broker"]
port = settings.get("mqtt_port", 1883)
base_topic = settings["topic"].replace("/{BOARD_ID}", "")
mqtt_auth = {'username': settings["mqtt_user"], 'password': settings["mqtt_password"]} if settings.get("mqtt_user") else None


def on_message(client, userdata, msg):
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
    except Exception as e:
        print(f"Error MQTT: {e}")


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


# --- Models ---
class UserInput(BaseModel):
    text: str


class LoginInput(BaseModel):
    password: str


class ConfigUpdate(BaseModel):
    key: str
    value: str


# --- Helpers ---
def is_admin(admin_token: str = Cookie(default=None)) -> bool:
    return admin_token in admin_sessions


# --- PUBLIC ROUTES ---
@app.get("/")
def get_webpage():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/style.css")
async def get_style():
    return FileResponse("style.css")


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
    session["thread_id"] = await create_new_thread()
    session["history"] = [{"sender": "ai", "text": RUNTIME_CONFIG["Welcom_msg"]}]
    session["calibrated_sensors"] = {}
    session.pop("pending_hardware_update", None)
    return {"status": "success"}


@app.post("/chat")
async def chat_with_ai(user_input: UserInput, board_id: str = Query(...)):
    session = get_session(board_id)
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

    session["history"].append({"sender": "ai", "text": texte_ia})
    save_logs_to_file()

    return {"reply": texte_ia, "MQTT_value": valeurs_mqtt}


# --- ADMIN ROUTES ---
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

    # 1. Persist on Render (will trigger auto-redeploy)
    ok, msg = await update_render_env_var(data.key, data.value)
    if not ok:
        return JSONResponse({"error": f"Render update failed: {msg}"}, status_code=500)

    # 2. Update runtime immediately
    os.environ[data.key] = data.value
    reload_runtime_config()

    # 3. Side-effect OpenAI Assistant
    if data.key == "OPENAI_ASSISTANT_MODEL":
        if not os.environ.get("OPENAI_API_KEY"):
            # API key not set yet, model will be applied on next restart
            pass
        else:
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
        archive_session(board_id, sessions[board_id])  # ← archive avant de supprimer
        del sessions[board_id]
        save_logs_to_file()
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

@app.post("/admin/reset-assistant")
async def reset_assistant(admin_token: str = Cookie(default=None)):
    if not is_admin(admin_token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    import os
    from pathlib import Path
    state_file = Path(settings["assistant_state_file"])
    if state_file.exists():
        state_file.unlink()
        return {"status": "deleted", "file": str(state_file)}
    return {"status": "file not found"}