# Toolkit Assistant

A FastAPI web application that bridges hardware control and AI-generated 3D virtual worlds. It lets you control a Raspberry Pi Pico 2W (sensors, actuators) via MQTT using natural language, and generate immersive WebXR scenes powered by Three.js — all through a browser-based chat interface.

## Features

- **Hardware control (MC mode)** — Chat with an OpenAI-powered assistant that translates plain language into MQTT commands for connected sensors and actuators
- **3D world generation (VR mode)** — Anthropic Claude generates live Three.js / WebXR scenes from your descriptions
- **Real-time WebSocket** — Live sensor data streamed per board
- **Email authentication** — 6-digit code login via Resend
- **Admin dashboard** — Runtime config management at `/admin`

## Requirements

- Python 3.9 or higher
- An MQTT broker accessible from the server (e.g. Mosquitto)
- API keys for OpenAI and Anthropic
- A Resend account for email-based login

## Installation

```bash
# Clone the repo
git clone https://github.com/logiqe/Toolkit_Assistant.git
cd Toolkit_Assistant

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `MQTT_BROKER` | Yes | IP or hostname of your MQTT broker |
| `MQTT_PORT` | Yes | MQTT port (default `1883`) |
| `MQTT_TOPIC` | Yes | Topic used for sensor/command messages |
| `MQTT_USER` | No | MQTT username |
| `MQTT_PASSWORD` | No | MQTT password |
| `MQTT_CLIENT_ID` | No | Client ID (defaults to `windmill-assistant`) |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `TRANSCRIPTION_MODEL` | No | Audio transcription model (e.g. `gpt-4o-transcribe`) |
| `OPENAI_ASSISTANT_MODEL` | No | Assistant model (e.g. `gpt-4o-mini`) |
| `OPENAI_ASSISTANT_NAME` | No | Display name shown in the UI |
| `OPENAI_ASSISTANT_DESCRIPTION` | No | Short description of the assistant |
| `OPENAI_ASSISTANT_INSTRUCTIONS_FILE` | No | Path to system prompt file (default `assistant_instructions.md`) |
| `OPENAI_ASSISTANT_SCHEMA_FILE` | No | Path to response schema (default `assistant_response_schema.json`) |
| `OPENAI_ASSISTANT_STATE_FILE` | No | Path to assistant state cache (default `assistant_state.json`) |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key (for 3D world generation) |
| `RESEND_API_KEY` | Yes | Resend API key for email verification codes |
| `ADMIN_PASSWORD` | Yes | Password for the admin dashboard |
| `WELCOME_MESSAGE` | No | Custom greeting shown to users |
| `RENDER_API_KEY` | No | Render.com API key (for cloud deploy config updates) |
| `RENDER_SERVICE_ID` | No | Render.com service ID |

## Running the Server

**Development** (with auto-reload):

```bash
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

**Production**:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

The server starts at `http://localhost:8000`.

## Accessing the App

| URL | Description |
|---|---|
| `http://localhost:8000/` | Main hardware control interface (login required) |
| `http://localhost:8000/world` | 3D world builder (login required) |
| `http://localhost:8000/admin` | Admin dashboard |
| `ws://localhost:8000/ws/{board_id}` | WebSocket endpoint for real-time hardware data |

## CLI Assistant (optional)

A standalone command-line assistant with voice input support can be run independently:

```bash
python3 Simple-assistant.py
```

This connects directly to MQTT and supports microphone input for voice commands.

## Project Structure

```
Toolkit_Assistant/
├── server.py                        # Main FastAPI app (33 API routes)
├── settings.py                      # Environment variable loader
├── OpenAiClientAssistant.py         # OpenAI Assistants API integration
├── Simple-assistant.py              # Standalone CLI assistant
├── world_builder.py                 # Anthropic-based 3D scene generator
├── assistant_instructions.md        # System prompt for hardware assistant
├── world_builder_instructions.md    # System prompt for 3D world generation
├── assistant_response_schema.json   # JSON schema for hardware logic programs
├── index.html                       # Hardware control UI
├── world.html                       # 3D world builder UI
├── login_user.html                  # Email login page
├── admin.html                       # Admin dashboard
├── style.css                        # Global stylesheet
├── requirements.txt                 # Python dependencies
└── .env.example                     # Environment variable template
```

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, Uvicorn, Python 3.9+ |
| AI (hardware) | OpenAI Assistants API |
| AI (3D worlds) | Anthropic Claude API |
| Hardware comms | paho-mqtt |
| Frontend | HTML5, CSS3, Three.js, WebXR |
| Real-time | WebSocket, MQTT |
| Email auth | Resend |
| Deployment | Render.com |
