# Toolkit Assistant

A conversational assistant that bridges **natural language → LLM reasoning → physical computing**.
A Raspberry Pi Pico 2W (MicroPython/CircuitPython) talks to a web frontend over MQTT, with two
available modes:

- **Artefact Chat (MC)** — an OpenAI assistant that translates user requests into hardware
  configuration (Grove sensors/actuators) sent to the board via MQTT.
- **World Builder Chat (VR)** — an Anthropic assistant that generates reactive Three.js/WebXR
  scenes, driven by the board's live sensor data.

---

## Architecture

```
Browser ── HTTP/WS ──► FastAPI (server.py) ──► MQTT broker ──► Pico 2W
                            │
                            ├── OpenAiClientAssistant.py  (Logic Engine, MC mode)
                            └── world_builder.py          (World Builder, VR mode)
```

- **`server.py`** — central FastAPI server: page routing (`/`, `/world`, `/admin`, `/login`),
  chat endpoints (`/chat`, `/world-chat`), MQTT ↔ WebSocket bridge (`/ws/{board_id}`), email-based
  user authentication (`/user/*`), and the admin panel (`/admin/*`).
- **`OpenAiClientAssistant.py`** — OpenAI Assistants API client, constrained by
  `assistant_response_schema.json` (strict JSON Schema) to produce valid MQTT payloads.
- **`assistant_instructions.md`** — Logic Engine system prompt (persona, translating vague
  requests into sensor/actuator logic).
- **`world_builder.py`** / **`world_builder_instructions.md`** — WebXR scene generation via the
  Anthropic API, with robust JSON parsing (regex fallback) and live injection of the board's hardware context.
- **`index.html` / `world.html`** — frontends for MC mode and VR mode.
- **`admin.html`** — admin panel (runtime config, logs, sessions, MQTT/WebSocket tests).
- **`login_user.html`** — login via a 6-digit code sent by email.
- **`settings.py`** — config loading (`.env` + Render environment variables).

---

## Hosting

This project is deployed on **Render's free tier** (Render Web Service).

A few important consequences of that choice:

- The free tier **spins the service down after a period of inactivity**, so the first request
  after idle time can take tens of seconds (cold start).
- Application state (sessions, chat histories, generated scenes) is currently persisted to local
  JSON files (`test_participants_logs.json`, `world_states.json`, `world_scenes.json`,
  `archives.json`) rather than a real database. **On Render's free tier, disk storage is not guaranteed to persist across redeploys**, so these files can be reset after a redeploy.
- The admin panel (`/admin`) can remotely update certain environment variables via the Render API
  (`RENDER_API_KEY` + `RENDER_SERVICE_ID`), which triggers an automatic redeploy (~1 min).

### Environment variables to configure on Render

| Variable                                                               | Purpose                                                |
|------------------------------------------------------------------------|--------------------------------------------------------|
| `MQTT_BROKER`, `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USER`, `MQTT_PASSWORD` | MQTT broker connection                                 |
| `OPENAI_API_KEY`, `OPENAI_ASSISTANT_MODEL`                             | Artefact                                               |
| `ANTHROPIC_API_KEY`                                                    | World Builder                                          |
| `RESEND_API_KEY`                                                       | Sending login codes by email                           |
| `ADMIN_PASSWORD`                                                       | Access to the `/admin` panel                           |
| `RENDER_API_KEY`, `RENDER_SERVICE_ID`                                  | Lets `/admin/config` update Render's env vars remotely |

---

## Email authentication — Resend

How it works (`/login`, `login_user.html`):

1. The user enters their email → `POST /user/send-code`.
2. A 6-digit code is generated and kept in memory (`pending_verifications`, 10-minute expiry),
   then sent via the Resend API (default sender address: `onboarding@resend.dev`, Resend's shared
   test domain).
3. The user enters the code → `POST /user/verify-code` → a `user_token` and `session_id`
   (`httponly` cookies) are created, then a redirect to `/?board_id=...` follows.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the keys listed above
uvicorn server:app --reload
```

The server listens on `http://localhost:8000` by default.
The board connects over MQTT on the `toolkit/{board_id}/...` topic.

---

## Modes

| Mode | Frontend | Backend | LLM |
|---|---|---|---|
| MC | `index.html` | `OpenAiClientAssistant.py` | OpenAI Assistants API, output constrained by `assistant_response_schema.json` |
| VR | `world.html` | `world_builder.py` | Anthropic API (Claude), JSON output `{reply, world_code}` |

---

## Technical notes

- **`BOARD_ID`**: derived stably from the AirLift module's MAC address, allowing multiple boards
  to run concurrent sessions without collisions.
- **srcdoc/null-origin bug fix**: WebXR scenes generated by `world_builder.py` are served via
  `/scene/{board_id}` (rather than as iframe `srcdoc`) to avoid the `null`-origin issues that broke
  WebXR/camera APIs in some browsers.
- **Robust JSON parsing**: `world_builder.py` falls back to regex extraction if the model returns
  HTML that isn't properly escaped inside the JSON response (see `_parse_model_response`).