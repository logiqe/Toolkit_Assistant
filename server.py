from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import asyncio
import json
import os
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish

from settings import settings
from OpenAiClientAssistant import create_new_thread, GPT_response

app = FastAPI()

sessions = {}

def load_logs_from_file():
    global sessions
    if os.path.exists("test_participants_logs.json"):
        try:
            with open("test_participants_logs.json", "r", encoding="utf-8") as f:
                sessions = json.load(f)
            print(f"✅ {len(sessions)} sessions restored")
        except Exception as e:
            print(f"Error loading logs: {e}")

@app.on_event("startup")
async def startup_event():
    load_logs_from_file()

def save_logs_to_file():
    try:
        with open("test_participants_logs.json", "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error while saving logs: {e}")


def get_session(board_id: str):
    """Gets or creates an isolated session for a given board"""
    if board_id not in sessions:
        sessions[board_id] = {
            "thread_id": None, # created at the first message
            "last_sensor": "No data",
            "history": [{"sender": "ai", "text": settings["Welcom_msg"]}],
            "calibrated_sensors": {}
        }
    return sessions[board_id]

# --- CONFIGURATION MQTT ---
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
                # if the board says that the calibration is done
                if data.get("status") == "calibration_finished":

                    # permanent save
                    session["calibrated_sensors"][data["sensor"]] = {
                        "min": data["min"],
                        "max": data["max"]
                    }

                    # temporary message for the AI
                    session["pending_hardware_update"] = (
                        f"SYSTEM NOTIFICATION: Calibration for '{data['sensor']}' is COMPLETE. "
                        f"Measured Min: {data['min']}, Measured Max: {data['max']}. "
                        f"This sensor is now calibrated."
                    )

                    print(f"Calibration saved for {board_id}")
            except Exception as e:
                print(f"Error parsing calibration: {e}") # not a JSON
            # -----------------

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

# --- ROUTES WEB ---
class UserInput(BaseModel):
    text: str

# interface
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
    return {"message": settings["Welcom_msg"]}

@app.post("/reset")
async def reset_conversation(board_id: str = Query(...)):
    session = get_session(board_id)
    session["thread_id"] = await create_new_thread()
    session["history"] = [{"sender": "ai", "text": settings["Welcom_msg"]}]
    session["calibrated_sensors"] = {}
    session.pop("pending_hardware_update", None)
    return {"status": "success"}

# messages
@app.post("/chat")
async def chat_with_ai(user_input: UserInput, board_id: str = Query(...)):
    session = get_session(board_id)
    
    if session["thread_id"] is None:
        session["thread_id"] = await create_new_thread()

    calibration_context = f"""
    {json.dumps(session['calibrated_sensors'])}
    """

    message_to_ai = calibration_context + "\n\n"

    # if a calibration was just done
    if session.get("pending_hardware_update"):
        message_to_ai += session["pending_hardware_update"] + "\n\n"
        del session["pending_hardware_update"]

    message_to_ai += f"User message: {user_input.text}"

    session["history"].append({"sender": "user", "text": user_input.text})

    response = await GPT_response(session["thread_id"], message_to_ai)
    
    texte_ia = response.get("answer", "Action effectuée.")
    valeurs_mqtt = response.get("MQTT_value", {})

    # send MQTT only on "this" board
    if valeurs_mqtt:
        topic_envoi = f"{base_topic}/{board_id}/command"
        mqtt_client.publish(topic_envoi, json.dumps(valeurs_mqtt), qos=1)
        print(f"📤 Order sent to {board_id} : {valeurs_mqtt}")

    session["history"].append({"sender": "ai", "text": texte_ia})

    save_logs_to_file()

    return {
        "reply": texte_ia,
        "MQTT_value": valeurs_mqtt
    }

@app.get("/admin/logs")
async def get_all_logs():
    """Displays all the messages"""
    return sessions