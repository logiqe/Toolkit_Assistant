from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import asyncio
import json
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish

from settings import settings
from OpenAiClientAssistant import create_new_thread, GPT_response

app = FastAPI()

sessions = {}

def get_session(board_id: str):
    """Récupère ou crée une session isolée pour une carte donnée"""
    if board_id not in sessions:
        sessions[board_id] = {
            "thread_id": None, # Sera créé au premier message
            "last_sensor": "Aucune donnée",
            "history": [{"sender": "ai", "text": settings["Welcom_msg"]}]
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
            print(f"📡 Données reçues de la carte {board_id} : {payload}")
    except Exception as e:
        print(f"Erreur MQTT: {e}")

mqtt_client = mqtt.Client()
if mqtt_auth: 
    mqtt_client.username_pw_set(mqtt_auth['username'], mqtt_auth['password'])
mqtt_client.on_message = on_message
mqtt_client.connect(broker, port)
mqtt_client.subscribe(f"{base_topic}/+/sensors")
mqtt_client.loop_start()

# --- ROUTES WEB ---
class UserInput(BaseModel):
    text: str

# 1. Fournir la page web (l'interface)
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
    # On utilise la clé exacte de ton dictionnaire settings.py
    return {"message": settings["Welcom_msg"]}

@app.post("/reset")
async def reset_conversation(board_id: str = Query(...)):
    session = get_session(board_id)
    session["thread_id"] = await create_new_thread()
    session["history"] = [{"sender": "ai", "text": settings["Welcom_msg"]}]
    return {"status": "success"}

# 2. Recevoir les messages du chat de la page web

@app.post("/chat")
async def chat_with_ai(user_input: UserInput, board_id: str = Query(...)):
    session = get_session(board_id)
    
    # 1. Créer le thread si c'est la première fois pour cette carte
    if session["thread_id"] is None:
        session["thread_id"] = await create_new_thread()

    # 2. Sauvegarder le message de l'utilisateur
    session["history"].append({"sender": "user", "text": user_input.text})

    # 3. Demander à l'IA (en utilisant le thread spécifique à cette carte)
    response = await GPT_response(session["thread_id"], user_input.text)
    
    texte_ia = response.get("answer", "Action effectuée.")
    valeurs_mqtt = response.get("MQTT_value", {})

    # 4. Envoyer les ordres MQTT uniquement à CETTE carte
    if valeurs_mqtt:
        topic_envoi = f"{base_topic}/{board_id}/command"
        publish.single(topic_envoi, json.dumps(valeurs_mqtt), hostname=broker, port=port, auth=mqtt_auth)
        print(f"📤 Ordre envoyé à {board_id} : {valeurs_mqtt}")

    # 5. Sauvegarder la réponse de l'IA
    session["history"].append({"sender": "ai", "text": texte_ia})

    return {"reply": texte_ia}