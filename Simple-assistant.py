import asyncio
import collections
import json
import select
import sys
import time
import wave
from io import BytesIO

import audioop
import paho.mqtt.client as mqtt
import sounddevice as sd
import webrtcvad

from settings import settings
from OpenAiClientAssistant import create_new_thread, GPT_response, transcribe_audio

broker = settings["broker"]
port = settings.get("mqtt_port", 1883)

topic = settings["topic"]

mqtt_user = settings["mqtt_user"]
mqtt_password = settings["mqtt_password"]

current_thread_id = None
input_mode = "text"
voice_prompt_displayed = False
dev_mode = False

SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)
SILENCE_FRAMES_LIMIT = int(0.9 / (FRAME_DURATION_MS / 1000))
MAX_FRAMES = int(10 / (FRAME_DURATION_MS / 1000))
PRE_SPEECH_FRAMES = int(0.18 / (FRAME_DURATION_MS / 1000))
VOICE_WAIT_TIMEOUT = 8
ENERGY_THRESHOLD = 300
MIN_VOICE_FRAMES = int(0.35 / (FRAME_DURATION_MS / 1000))
POST_SPEECH_FRAMES = int(0.4 / (FRAME_DURATION_MS / 1000))

vad = webrtcvad.Vad(3)
noise_floor = ENERGY_THRESHOLD


class MQTTClient:
    def __init__(self, loop):
        self.loop = loop
        self.client = mqtt.Client(
            client_id=settings["client_id"],
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, password=mqtt_password)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.connected = asyncio.Event()

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code == 0:
            print("\nConnected to MQTT broker.")
            self.client.subscribe(topic + "/sensors")
            self.connected.set()
        else:
            print(f"\nMQTT connection failed with code {code}.")
            self.connected.clear()

    def on_message(self, client, userdata, message):
        try:
            payload = message.payload.decode("utf-8")
            if payload:
                print(f"\nBoard -> Assistant - Info received: {payload}")
                prompt = f"This is an automatic system event, sensors status: {payload}"
                asyncio.run_coroutine_threadsafe(
                    process_user_message(prompt, self, dev_mode=dev_mode), self.loop
                )
        except Exception as e:
            print(f"Error processing MQTT message: {e}")

    def on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        code = getattr(reason_code, "value", reason_code)
        if code not in (0, None):
            print(f"\nMQTT disconnected unexpectedly (code {code}).")
        self.connected.clear()

    async def connect(self):
        try:
            self.client.connect(broker, port, keepalive=60)
            self.client.loop_start()
            try:
                await asyncio.wait_for(self.connected.wait(), timeout=5)
                return True
            except asyncio.TimeoutError:
                print("MQTT connection timed out.")
                return False
        except Exception as exc:
            print(f"Unable to connect to MQTT broker: {exc}")
            return False

    async def disconnect(self):
        if self.client.is_connected():
            self.client.loop_stop()
            self.client.disconnect()

    async def publish(self, payload: str):
        if not self.connected.is_set():
            print("Skipping MQTT publish; not connected.")
            return False
        try:
            self.client.publish(topic + "/command", payload)
            print("\n")
            return True
        except Exception as exc:
            print(f"Failed to publish MQTT message: {exc}")
            return False


async def restart_thread():
    global current_thread_id
    new_thread_id = await create_new_thread()
    if not new_thread_id:
        print("Could not start a new conversation thread.")
        return False

    current_thread_id = new_thread_id
    print(f"\nStarted new conversation thread: {current_thread_id}")
    return True


def calibrate_noise_floor(duration: float = 1.0) -> int | None:
    """Measure ambient RMS level to improve speech gating."""
    frames_needed = max(1, int(duration * SAMPLE_RATE / FRAME_SIZE))
    energies: list[int] = []

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SIZE,
            dtype="int16",
            channels=1,
        ) as stream:
            for _ in range(frames_needed):
                frame, _ = stream.read(FRAME_SIZE)
                if not frame:
                    continue
                energy = audioop.rms(frame, 2)
                energies.append(energy)
    except sd.PortAudioError as exc:
        print(f"Calibration audio error: {exc}")
        return None

    if not energies:
        return None

    avg_energy = sum(energies) / len(energies)
    return max(int(avg_energy), ENERGY_THRESHOLD // 2)


def record_voice_once() -> bytes | None:
    """Capture a single utterance using VAD-controlled recording."""
    ring_buffer = collections.deque(maxlen=PRE_SPEECH_FRAMES)
    post_buffer = collections.deque(maxlen=POST_SPEECH_FRAMES)
    voiced_frames: list[bytes] = []
    voiced_energies: list[int] = []
    silence_frames = 0
    triggered = False
    start_time = time.time()
    dynamic_threshold = max(ENERGY_THRESHOLD, int(noise_floor * 2))

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SIZE,
            dtype="int16",
            channels=1,
        ) as stream:
            while True:
                frame, _ = stream.read(FRAME_SIZE)
                if not frame:
                    continue

                energy = audioop.rms(frame, 2)
                is_voiced = energy >= dynamic_threshold and vad.is_speech(frame, SAMPLE_RATE)

                if not triggered:
                    ring_buffer.append((frame, energy))
                    if is_voiced:
                        triggered = True
                        for buffered_frame, buffered_energy in ring_buffer:
                            voiced_frames.append(buffered_frame)
                            voiced_energies.append(buffered_energy)
                        ring_buffer.clear()
                        silence_frames = 0
                    elif time.time() - start_time > VOICE_WAIT_TIMEOUT:
                        return None
                    continue

                if is_voiced:
                    voiced_frames.append(frame)
                    voiced_energies.append(energy)
                    silence_frames = 0
                    post_buffer.clear()
                else:
                    post_buffer.append(frame)
                    silence_frames += 1
                    if silence_frames > SILENCE_FRAMES_LIMIT:
                        voiced_frames.extend(post_buffer)
                        post_buffer.clear()
                        break

                if len(voiced_frames) > MAX_FRAMES:
                    voiced_frames.extend(post_buffer)
                    post_buffer.clear()
                    break

    except sd.PortAudioError as exc:
        print(f"Audio input error: {exc}")
        return None

    if not voiced_frames or len(voiced_frames) < MIN_VOICE_FRAMES:
        return None

    if not voiced_energies:
        return None

    avg_voiced_energy = sum(voiced_energies) / len(voiced_energies)
    if avg_voiced_energy < dynamic_threshold * 1.1:
        return None

    return b"".join(voiced_frames)


def pcm_to_wav(pcm_data: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM data in a WAV container."""
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buffer.getvalue()


async def handle_command(command, mqtt_client):
    global input_mode, voice_prompt_displayed, dev_mode

    if command == "/help":
        print(
            "\nCommands:\n"
            "/help    Show this message\n"
            "/restart Start a new assistant thread\n"
            "/voice   Switch to voice mode\n"
            "/text    Switch to text mode\n"
            "/dev     Enable dev mode (text + MQTT preview)\n"
            "/quit    Exit the program"
        )
        return True

    if command == "/restart":
        await restart_thread()
        return True

    if command == "/voice":
        input_mode = "voice"
        dev_mode = False
        voice_prompt_displayed = False
        print("\nVoice mode enabled. Speak after the prompt.")
        return True

    if command == "/text":
        input_mode = "text"
        dev_mode = False
        voice_prompt_displayed = False
        print("\nText mode enabled. Type your message.")
        return True

    if command == "/dev":
        input_mode = "text"
        dev_mode = True
        voice_prompt_displayed = False
        print("\nDev mode enabled. Text replies will show MQTT payloads without sending. Type /text to exit dev mode.")
        return True

    if command == "/quit":
        print("Goodbye!")
        if mqtt_client:
            await mqtt_client.disconnect()
        sys.exit(0)

    return False


async def process_user_message(message: str, mqtt_client, *, dev_mode: bool = False):
    global current_thread_id

    if not current_thread_id:
        if not await restart_thread():
            return

    response = await GPT_response(current_thread_id, message)
    text = response.get("answer", "")
    values = response.get("MQTT_value", {})

    print(f"\nAssistant: {text}")

    if values:
        payload = json.dumps(values, indent=2 if dev_mode else None)
        if mqtt_client and not dev_mode:
            await mqtt_client.publish(payload)
        elif not dev_mode:
            print("To see preset commands type /help")
        if dev_mode:
            print("\n[DEV] MQTT payload preview:")
            print(payload)


async def chat_loop(mqtt_client):
    global voice_prompt_displayed, input_mode, dev_mode

    loop = asyncio.get_event_loop()

    while True:
        try:
            if input_mode == "text":
                voice_prompt_displayed = False
                user_input = await loop.run_in_executor(
                    None, lambda: input("\nYou: ").strip()
                )
                if not user_input:
                    continue

                if user_input.startswith("/"):
                    if await handle_command(user_input, mqtt_client):
                        continue

                await process_user_message(user_input, mqtt_client, dev_mode=dev_mode)
                continue

            if input_mode == "voice":
                # Allow typed commands/messages even while in voice mode.
                readable, _, _ = select.select([sys.stdin], [], [], 0)
                if sys.stdin in readable:
                    typed = sys.stdin.readline().strip()
                    if not typed:
                        continue
                    print(f"\nYou (text): {typed}")
                    if typed.startswith("/"):
                        if await handle_command(typed, mqtt_client):
                            continue
                    else:
                        input_mode = "text"
                        voice_prompt_displayed = False
                        print("\nSwitched to text mode based on typed input.")
                        dev_mode = False
                    await process_user_message(typed, mqtt_client, dev_mode=dev_mode)
                    continue

            if input_mode == "voice" and not voice_prompt_displayed:
                print("\n🎤 Voice mode active. Speak clearly and pause to send.")
                voice_prompt_displayed = True

            if input_mode == "voice":
                pcm_data = await asyncio.to_thread(record_voice_once)
            else:
                # Should not reach here in text mode due to continue above.
                pcm_data = None

            if not pcm_data:
                continue

            wav_bytes = pcm_to_wav(pcm_data)
            transcript = await transcribe_audio(wav_bytes)
            if not transcript:
                print("Didn't catch that. Try again.")
                continue

            message = transcript.strip()
            if not message:
                continue

            print(f"\nYou (voice): {message}")

            if message.startswith("/"):
                if await handle_command(message, mqtt_client):
                    continue

            await process_user_message(message, mqtt_client, dev_mode=dev_mode)
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as exc:
            print(f"\nError: {exc}")


async def main():
    global current_thread_id, noise_floor

    print(settings["Welcom_msg"])

    try:
        print("Calibrating microphone... please remain silent.")
        measured = await asyncio.to_thread(calibrate_noise_floor)
        if measured:
            noise_floor = measured
            print(f"Calibrated ambient noise level: {noise_floor:.0f}")
    except Exception as exc:
        print(f"Microphone calibration failed: {exc}")

    current_thread_id = await create_new_thread()
    if not current_thread_id:
        print("Failed to create an assistant thread. Exiting.")
        return

    loop = asyncio.get_event_loop()
    mqtt_client = MQTTClient(loop)
    if not await mqtt_client.connect():
        print("Continuing without MQTT publishing.")
        mqtt_client = None

    await chat_loop(mqtt_client)

    if mqtt_client:
        await mqtt_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram terminated by user.")
    except Exception as exc:
        print(f"\nProgram terminated due to error: {exc}")
