# Toolkit — Database Schema (by Claude)

## Tables

### `boards`
One row per physical microcontroller.

| Column         | Type        | Notes                                  |
|----------------|-------------|----------------------------------------|
| `id`           | UUID PK     | matches the `board_id` in URLs         |
| `created_at`   | TIMESTAMPTZ | first time this board_id was seen      |
| `last_seen_at` | TIMESTAMPTZ | updated on every incoming MQTT message |
| `mqtt_topic`   | TEXT        | e.g. `toolkit/{board_id}/sensors`      |
| `label`        | TEXT NULL   | friendly name set by admin             |

---

### `users`
One row per verified email address.

| Column          | Type                 |
|-----------------|----------------------|
| `id`            | UUID PK              |
| `email`         | TEXT UNIQUE NOT NULL |
| `first_seen_at` | TIMESTAMPTZ          |
| `last_login_at` | TIMESTAMPTZ          |

---

### `auth_codes`
Temporary OTP state (currently `pending_verifications` dict in RAM).

| Column       | Type             | Notes                    |
|--------------|------------------|--------------------------|
| `id`         | UUID PK          |                          |
| `email`      | TEXT NOT NULL    |                          |
| `code`       | TEXT NOT NULL    | 6-digit string           |
| `created_at` | TIMESTAMPTZ      |                          |
| `expires_at` | TIMESTAMPTZ      | created_at + 10 min      |
| `used_at`    | TIMESTAMPTZ NULL | set on successful verify |

---

### `user_sessions`
One row per browser session created by `/user/verify-code`.  
Replaces the `user_sessions` in-memory set and the `session_id` cookie.

| Column       | Type                  | Notes                                                       |
|--------------|-----------------------|-------------------------------------------------------------|
| `id`         | UUID PK               | the `session_id` cookie value                               |
| `user_id`    | UUID FK → users       |                                                             |
| `board_id`   | UUID FK → boards NULL | board the user was linked to at login                       |
| `token_hash` | TEXT                  | SHA-256 of the `user_token` cookie — never store raw tokens |
| `created_at` | TIMESTAMPTZ           |                                                             |
| `expires_at` | TIMESTAMPTZ NULL      | if you want session expiry                                  |
| `revoked_at` | TIMESTAMPTZ NULL      | set on logout                                               |

---

### `mc_conversations`
One per MC (GPT) conversation lifespan. A new one is created on `/reset` or first chat.  
Maps to the OpenAI thread.

| Column             | Type                         | Notes                              |
|--------------------|------------------------------|------------------------------------|
| `id`               | UUID PK                      |                                    |
| `board_id`         | UUID FK → boards             |                                    |
| `session_id`       | UUID FK → user_sessions NULL | null for legacy / boardonly access |
| `openai_thread_id` | TEXT NOT NULL                | the OpenAI thread id               |
| `started_at`       | TIMESTAMPTZ                  |                                    |
| `ended_at`         | TIMESTAMPTZ NULL             | set on reset / archive             |
| `archived`         | BOOLEAN DEFAULT false        |                                    |

---

### `mc_messages`
Every turn in the MC chat (currently `session["history"]` list).

| Column            | Type                       | Notes                            |
|-------------------|----------------------------|----------------------------------|
| `id`              | UUID PK                    |                                  |
| `conversation_id` | UUID FK → mc_conversations |                                  |
| `seq`             | INTEGER                    | ordering within the conversation |
| `sender`          | TEXT                       | `'user'` or `'ai'`               |
| `text`            | TEXT NOT NULL              | displayed message content        |
| `created_at`      | TIMESTAMPTZ                |                                  |

---

### `mc_mqtt_payloads`
Every JSON config sent to the board via MQTT after a GPT response.  
Currently stored only as `session["last_hardware_config"]`.

| Column            | Type                       | Notes                                                                         |
|-------------------|----------------------------|-------------------------------------------------------------------------------|
| `id`              | UUID PK                    |                                                                               |
| `message_id`      | UUID FK → mc_messages      | the AI turn that triggered this                                               |
| `board_id`        | UUID FK → boards           |                                                                               |
| `conversation_id` | UUID FK → mc_conversations |                                                                               |
| `payload`         | JSONB NOT NULL             | the full MQTT_value object validated against `assistant_response_schema.json` |
| `mqtt_topic`      | TEXT                       | the topic published to                                                        |
| `sent_at`         | TIMESTAMPTZ                |                                                                               |

---

### `sensor_calibrations`
Calibration results received via MQTT (`status: calibration_finished`).  
Currently stored in `session["calibrated_sensors"]`.

| Column            | Type                       | Notes                            |
|-------------------|----------------------------|----------------------------------|
| `id`              | UUID PK                    |                                  |
| `board_id`        | UUID FK → boards           |                                  |
| `conversation_id` | UUID FK → mc_conversations | active convo at calibration time |
| `sensor`          | TEXT                       | e.g. `'light'`, `'distance'`     |
| `min_value`       | NUMERIC                    |                                  |
| `max_value`       | NUMERIC                    |                                  |
| `calibrated_at`   | TIMESTAMPTZ                |                                  |

---

### `world_conversations`
One per World (Claude) conversation lifespan per board.

| Column       | Type                         | Notes                 |
|--------------|------------------------------|-----------------------|
| `id`         | UUID PK                      |                       |
| `board_id`   | UUID FK → boards             |                       |
| `session_id` | UUID FK → user_sessions NULL |                       |
| `started_at` | TIMESTAMPTZ                  |                       |
| `ended_at`   | TIMESTAMPTZ NULL             | set on `/world-clear` |

---

### `world_messages`
Every turn in the World / Claude chat (currently `world_histories[board_id]`).

| Column            | Type                          | Notes                              |
|-------------------|-------------------------------|------------------------------------|
| `id`              | UUID PK                       |                                    |
| `conversation_id` | UUID FK → world_conversations |                                    |
| `seq`             | INTEGER                       | ordering                           |
| `role`            | TEXT                          | `'user'` or `'assistant'`          |
| `text`            | TEXT NULL                     | plain-text portion of the message  |
| `has_images`      | BOOLEAN DEFAULT false         | true when the user sent image(s)   |
| `has_3d_assets`   | BOOLEAN DEFAULT false         | true when the user sent 3D file(s) |
| `created_at`      | TIMESTAMPTZ                   |                                    |

---

### `world_scenes`
Every Three.js / WebXR HTML scene generated by Claude.

| Column            | Type                          | Notes                                                     |
|-------------------|-------------------------------|-----------------------------------------------------------|
| `id`              | UUID PK                       |                                                           |
| `message_id`      | UUID FK → world_messages      | the assistant turn that produced this scene               |
| `conversation_id` | UUID FK → world_conversations |                                                           |
| `board_id`        | UUID FK → boards              |                                                           |
| `html`            | TEXT NOT NULL                 | full generated HTML (injected sensor bridge included)     |
| `reply_text`      | TEXT                          | Claude's `"reply"` field (the human-readable description) |
| `is_latest`       | BOOLEAN DEFAULT true          | false when superseded in same conversation                |
| `created_at`      | TIMESTAMPTZ                   |                                                           |

---

### `world_uploaded_images`
Images sent by users inside the World chat. Currently passed as transient base64; this table lets you persist them.

| Column              | Type                     | Notes                            |
|---------------------|--------------------------|----------------------------------|
| `id`                | UUID PK                  |                                  |
| `message_id`        | UUID FK → world_messages |                                  |
| `original_filename` | TEXT NULL                |                                  |
| `mime_type`         | TEXT                     | `image/png`, `image/jpeg`, etc.  |
| `size_bytes`        | INTEGER                  |                                  |
| `storage_path`      | TEXT                     | path on disk or object-store key |
| `created_at`        | TIMESTAMPTZ              |                                  |

---

### `world_uploaded_assets`
3D files (`.glb`, `.gltf`, `.obj`) saved to the `/uploads/` folder.

| Column              | Type                     | Notes                                |
|---------------------|--------------------------|--------------------------------------|
| `id`                | UUID PK                  |                                      |
| `message_id`        | UUID FK → world_messages |                                      |
| `original_filename` | TEXT                     |                                      |
| `stored_filename`   | TEXT                     | `{uuid}_{original_filename}` on disk |
| `public_url`        | TEXT                     | `/uploads/{stored_filename}`         |
| `size_bytes`        | INTEGER                  |                                      |
| `created_at`        | TIMESTAMPTZ              |                                      |

---

### `mqtt_sensor_events`
Raw inbound MQTT payloads from boards (currently only inspected in RAM).  
Optional but useful for debugging and replay.

| Column        | Type             | Notes                                         |
|---------------|------------------|-----------------------------------------------|
| `id`          | UUID PK          |                                               |
| `board_id`    | UUID FK → boards |                                               |
| `topic`       | TEXT             |                                               |
| `payload`     | JSONB            | parsed payload; fall back to `{"raw": "..."}` |
| `received_at` | TIMESTAMPTZ      |                                               |

---

### `archived_sessions`
What is currently written to `archives.json` on session reset or clear.

| Column                        | Type                            | Notes                             |
|-------------------------------|---------------------------------|-----------------------------------|
| `id`                          | UUID PK                         |                                   |
| `board_id`                    | UUID FK → boards                |                                   |
| `mc_conversation_id`          | UUID FK → mc_conversations NULL |                                   |
| `archived_at`                 | TIMESTAMPTZ                     |                                   |
| `history_snapshot`            | JSONB                           | full message list at archive time |
| `calibrated_sensors_snapshot` | JSONB                           | sensor ranges at archive time     |

---

### `admin_sessions`
Currently an in-memory set; this table makes admin logins persistent across restarts.

| Column       | Type             | Notes                       |
|--------------|------------------|-----------------------------|
| `id`         | UUID PK          | the admin token (hashed)    |
| `token_hash` | TEXT UNIQUE      | SHA-256 of the cookie value |
| `created_at` | TIMESTAMPTZ      |                             |
| `revoked_at` | TIMESTAMPTZ NULL |                             |

---

## Entity-relationship summary

```
boards ──< mc_conversations ──< mc_messages ──< mc_mqtt_payloads
       │                    └──< sensor_calibrations
       │
       └──< world_conversations ──< world_messages ──< world_scenes
                                                   ├──< world_uploaded_images
                                                   └──< world_uploaded_assets

users ──< user_sessions >──── mc_conversations
       └──< auth_codes         └── world_conversations

boards ──< mqtt_sensor_events
boards ──< archived_sessions
```