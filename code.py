import gc
import time
import json
import board
import neopixel
import touchio
import digitalio
import pwmio
import analogio
import busio
import adafruit_vl53l0x
import microcontroller
import binascii
from adafruit_esp32spi import adafruit_esp32spi
import adafruit_connection_manager
from adafruit_motor import servo
from MQTT import Create_MQTT
from settings import settings

time.sleep(2)
gc.collect()

hw_inputs = {}
hw_outputs = {}

board_led = digitalio.DigitalInOut(board.D13)
board_led.direction = digitalio.Direction.OUTPUT

# --- INIT AIRLIFT (Bitsy Expander) ---
print("Init AirLift...")
esp32_cs    = digitalio.DigitalInOut(board.D9)
esp32_ready = digitalio.DigitalInOut(board.D11)
esp32_reset = digitalio.DigitalInOut(board.D12)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)

if esp.status == adafruit_esp32spi.WL_IDLE_STATUS:
    print("ESP32 WiFi Module found.")
    try:
        print("Firmware version:", str(esp.firmware_version, "utf-8"))
    except Exception:
        pass

# --- CONNEXION WIFI ---
ssid = settings.get("ssid")
password = settings.get("password")

while not esp.is_connected:
    try:
        print(f"Connexion à {ssid}...")
        esp.connect_AP(ssid, password)
    except (RuntimeError, ConnectionError) as e:
        print("Échec :", e, "— nouvelle tentative dans 2s")
        time.sleep(2)

print("Connecté ! IP :", esp.pretty_ip(esp.ip_address))

# --- BOARD_ID basé sur la MAC de l'AirLift ---
mac = bytes(esp.MAC_address)
BOARD_ID = binascii.hexlify(mac).decode("utf-8")
print("BOARD_ID :", BOARD_ID)

# --- Socket pool pour MQTT ---
pool = adafruit_connection_manager.get_radio_socketpool(esp)

#LED1_PIN = board.D6
#NUM_LEDS = 1
#TOUCH_PIN = board.D12
#LIGHTSENS_PIN = board.A27
#TEMPSENS_PIN = board.A28
#PIEZO_PIN = board.D14
#SERVO_PIN = board.D8
#TOF_PIN = (board.SCL, board.SDA)
#LED2_PIN = board.D10


# --- HARDWARE ---

#led1 = neopixel.NeoPixel(LED1_PIN, NUM_LEDS, auto_write=False, pixel_order=neopixel.GRBW)
#led2 = neopixel.NeoPixel(LED2_PIN, NUM_LEDS, auto_write=False, pixel_order=neopixel.GRBW)
#touch = touchio.TouchIn(TOUCH_PIN)
#lightsens = analogio.AnalogIn(LIGHTSENS_PIN)
#tempsens = analogio.AnalogIn(TEMPSENS_PIN)
#piezo = pwmio.PWMOut(PIEZO_PIN, variable_frequency=True)

#pwm = pwmio.PWMOut(SERVO_PIN, duty_cycle=2 ** 15, frequency=50)
#servom = servo.Servo(pwm)
#servom.angle = 90

#i2c = busio.I2C(TOF_PIN[0], TOF_PIN[1])
#tof = adafruit_vl53l0x.VL53L0X(i2c)

def get_pin(pin_name):
    pin_str = str(pin_name).upper() # Converting string to object board.PIN
    if hasattr(board, pin_str):
        return getattr(board, pin_str)
    # Fallback for Pico
    if not pin_str.startswith("GP"):
        pin_str = "GP" + pin_str.replace("D", "").replace("A", "")
    return getattr(board, pin_str, None)

def init_hardware(config):
    global hw_inputs, hw_outputs
    print("Initialisation du matériel dynamique...")

    # --- NETTOYAGE DE SÉCURITÉ ---
    for name, comp in hw_inputs.items():
        if "i2c_bus" in comp:
            try: comp["i2c_bus"].deinit()
            except: pass
        # Autres composants
        if hasattr(comp["obj"], "deinit"):
            try:
                comp["obj"].deinit()
            except Exception as e:
                print("Erreur deinit output:", e)

    gc.collect()
    time.sleep(0.2)

    # On force la libération des pins I2C manuellement
#    for pin_id in [board.GP4, board.GP5]:
#        try:
#            with digitalio.DigitalInOut(pin_id) as p: pass
#        except: pass

    for pin_id in [board.SDA, board.SCL]:
        try:
            with digitalio.DigitalInOut(pin_id) as p: pass
        except: pass

    for name, comp in hw_outputs.items():

        if comp["type"] == "servo" and "pwm" in comp:
            try:
                comp["pwm"].deinit()
            except Exception as e:
                print("Erreur deinit servo PWM:", e)

        if hasattr(comp["obj"], "deinit"):
            try:
                comp["obj"].deinit()
            except Exception as e:
                print("Erreur deinit output:", e)

    hw_inputs.clear()
    hw_outputs.clear()

    # INPUTS
    for name, params in config.get("inputs", {}).items():
        if params is None: continue
        ptype = params.get("type")
        try:
            if ptype == "analog":
                hw_inputs[name] = {"type": ptype, "obj": analogio.AnalogIn(get_pin(params["pin"]))}
            elif ptype == "digital_in":
                # tilt switch et button
                pin_obj = digitalio.DigitalInOut(get_pin(params["pin"]))
                pin_obj.direction = digitalio.Direction.INPUT
                pin_obj.pull = digitalio.Pull.DOWN
                hw_inputs[name] = {"type": ptype, "obj": pin_obj}
            elif ptype == "touch":
                hw_inputs[name] = {"type": ptype, "obj": touchio.TouchIn(get_pin(params["pin"]))}
            elif ptype == "vl53l0x":
                try:
                    # Correction ici : on utilise les pins GP5/GP4 directement si port I2C est spécifié
                    i2c = busio.I2C(board.SCL, board.SDA)
                    hw_inputs[name] = {
                        "type": ptype,
                        "obj": adafruit_vl53l0x.VL53L0X(i2c),
                        "i2c_bus": i2c
                    }
                    print(f"Capteur {name} prêt sur I2C")
                except Exception as e:
                    print(f"Erreur capteur distance: {e}")
        except Exception as e:
            print(f"Erreur init input {name}: {e}")

    # OUTPUTS
    for name, params in config.get("outputs", {}).items():
        if params is None: continue
        ptype = params.get("type")
        try:
            if ptype == "neopixel":
                hw_outputs[name] = {
                    "type": ptype,
                    "obj": neopixel.NeoPixel(get_pin(params["pin"]), params.get("num_leds", 1), auto_write=False, pixel_order=neopixel.GRBW)
                }
            elif ptype == "pwm_out":
                hw_outputs[name] = {"type": ptype, "obj": pwmio.PWMOut(get_pin(params["pin"]), frequency=1000)}
            elif ptype == "piezo" or ptype == "pwm":
                hw_outputs[name] = {"type": ptype, "obj": pwmio.PWMOut(get_pin(params["pin"]), variable_frequency=True)}
            elif ptype == "servo":
                pwm = pwmio.PWMOut(
                    get_pin(params["pin"]),
                    duty_cycle=2 ** 15,
                    frequency=50
                )

                hw_outputs[name] = {
                    "type": ptype,
                    "obj": servo.Servo(pwm),
                    "pwm": pwm
                }
        except Exception as e:
            print(f"Erreur init output {name}: {e}")

    print(f"Matériel prêt ! Inputs: {list(hw_inputs.keys())} | Outputs: {list(hw_outputs.keys())}")

# --- STATE ---

inputs = {"touch": 0, "light": 0, "temperature": 0, "distance": 0}
rules = []
mappings = []

input_last_values = {}
input_start_times = {}
input_durations = {}
rule_last_results = {}
toggle_output_states = {}

default_actions = [{"output": "led1", "values": [[0, 0, 0, 0]]},
                   {"output": "led2", "values": [[0, 0, 0, 0]]},
                   {"output": "piezo", "frequencies": [], "volume": 0.0},
                   {"output": "servo", "angle": 90}]

led1_state = [[0, 0, 0, 0]]
led2_state = [[0, 0, 0, 0]]
piezo_state = {"frequencies": [], "volume": 0.0}
servo_state = {"angle": 90}

last_inputs_print = 0

led1_index = 0
led2_index = 0
piezo_index = 0
last_led_update = time.monotonic()
last_piezo_update = time.monotonic()

ANIMATION_SPEED = 0.5
LED1_BRIGHTNESS = 1.0
LED2_BRIGHTNESS = 1.0

last_published_inputs = {}

#piezo.duty_cycle = 0


# -- Calibration --

cal_input = None
cal_min = 65535
cal_max = 0
cal_start = 0
cal_end = 0
CALIBRATION_DURATION = 25.0
ANALOG_INPUTS = ("light", "temperature", "distance")

touch_state = False


# --- FUNCTIONS ---

def apply_single_led(led_object, values, brightness=1.0):
    if not isinstance(values, list) or len(values) != 4:
        return
    r, g, b, w = values
    led_object.brightness = brightness
    led_object.fill((r, g, b, w))
    led_object.show()

def apply_mappings(active_rules_for):
    global LED1_BRIGHTNESS, LED2_BRIGHTNESS, ANIMATION_SPEED
    global servo_state, piezo_state, led1_state, led2_state

    for m in mappings:
        val = inputs.get(m["input"], m["in_min"])
        if m["in_max"] == m["in_min"]:
            continue

        in_range = m["in_max"] - m["in_min"]
        ratio = (val - m["in_min"]) / in_range if in_range != 0 else 0.0
        ratio = max(0.0, min(1.0, ratio))

        if m["output"] == "led1" and not active_rules_for.get("led1", False):
            mapped = int(m["out_min"] + ratio * (m["out_max"] - m["out_min"]))
            ch = m.get("output_channel", 3)
            if ch == 4:
                LED1_BRIGHTNESS = max(0.0, min(1.0, mapped / 255.0))
            elif len(led1_state) > 0 and len(led1_state[0]) > ch:
                led1_state[0][ch] = 0 if mapped <= 8 else (255 if mapped >= 247 else mapped)

        elif m["output"] == "led2" and not active_rules_for.get("led2", False):
            mapped = int(m["out_min"] + ratio * (m["out_max"] - m["out_min"]))
            ch = m.get("output_channel", 3)
            if ch == 4:
                LED2_BRIGHTNESS = max(0.0, min(1.0, mapped / 255.0))
            elif len(led2_state) > 0 and len(led2_state[0]) > ch:
                led2_state[0][ch] = 0 if mapped <= 8 else (255 if mapped >= 247 else mapped)

        elif m["output"] == "piezo_frequency" and not active_rules_for.get("piezo_frequency", False):
            mapped_freq = m["out_min"] + ratio * (m["out_max"] - m["out_min"])
            piezo_state["frequencies"] = [max(10, int(mapped_freq))]

        elif m["output"] == "piezo_volume" and not active_rules_for.get("piezo_volume", False):
            mapped_vol = m["out_min"] + ratio * (m["out_max"] - m["out_min"])
            piezo_state["volume"] = max(0.0, min(1.0, mapped_vol))

        elif m["output"] == "animation_speed":
            mapped_speed = m["out_min"] + ratio * (m["out_max"] - m["out_min"])
            ANIMATION_SPEED = max(0.02, mapped_speed)

        elif m["output"] == "servo" and not active_rules_for.get("servo", False):
            mapped_angle = m["out_min"] + ratio * (m["out_max"] - m["out_min"])
            servo_state["angle"] = max(0, min(180, int(mapped_angle)))

def eval_check(input_name, op, thr, duration_needed=0):
    val = inputs.get(input_name, 0)
    current_duration = input_durations.get(input_name, 0)

    if duration_needed > 0:
        if current_duration < duration_needed:
            return False

    if op == "<":  return val < thr
    if op == ">":  return val > thr
    if op == "<=": return val <= thr
    if op == ">=": return val >= thr
    if op == "==": return val == thr
    if op == "!=": return val != thr
    return False

def rule_matches(rule):
    logic = rule.get("condition_logic", "AND")
    checks = rule.get("checks", [])
    if not checks: return False

    results = []
    for c in checks:
        duration_val = c.get("duration") if c.get("duration") is not None else 0
        res = eval_check(c["input"], c["op"], c["value"], duration_val)
        results.append(res)

    if logic == "AND":
        return all(results)
    else:
        return any(results)

def validate_action(action):
    required_keys = {"output", "values", "volume", "frequencies", "angle", "toggle"}
    if not isinstance(action, dict):
        return False
    if not required_keys.issubset(action.keys()):
        return False
    if action["output"] not in ["led1", "led2", "piezo", "servo", "vibration"]:
        return False
    return True

def validate_mapping(m):
    required = {"label", "input", "in_min", "in_max", "output", "out_min", "out_max", "output_channel"}

    # 1. Base structure
    if not isinstance(m, dict):
        return False
    if not required.issubset(m.keys()):
        return False

    # 2. Types
    if not isinstance(m["label"], str):
        return False
    if not isinstance(m["input"], str):
        return False
    if not isinstance(m["output"], str):
        return False
    if not isinstance(m["output_channel"], int):
        return False

    # Numerical
    for key in ["in_min", "in_max", "out_min", "out_max"]:
        if not isinstance(m[key], (int, float)):
            return False

    # 3. Inputs authorized
    if m["input"] not in ["touch", "light", "temperature", "distance", "potentiometer", "tilt", "button"]:
        return False

    # 4. Outputs authorized
    if m["output"] not in ["led1", "led2", "piezo_frequency", "piezo_volume", "animation_speed", "servo"]:
        return False

    # 5. Range logic (prevents division by 0)
    if m["in_min"] == m["in_max"]:
        return False

    # 6. Specific constraints by outputs

    # LED
    if m["output"] in ["led1", "led2"]:
        if m["output_channel"] not in [0, 1, 2, 3, 4]:
            return False
        if not (0 <= m["out_min"] <= 255 and 0 <= m["out_max"] <= 255):
            return False

    # Piezo frequency
    elif m["output"] == "piezo_frequency":
        if m["output_channel"] != 0:
            return False
        if m["out_min"] < 0 or m["out_max"] < 0:
            return False

    # Piezo volume
    elif m["output"] == "piezo_volume":
        if m["output_channel"] != 0:
            return False
        if not (0.0 <= m["out_min"] <= 1.0 and 0.0 <= m["out_max"] <= 1.0):
            return False

    # Animation speed
    elif m["output"] == "animation_speed":
        if m["output_channel"] != 0:
            return False
        if m["out_min"] <= 0 or m["out_max"] <= 0:
            return False

    # Servo
    elif m["output"] == "servo":
        if m["output_channel"] != 0:
            return False
        if not (0 <= m["out_min"] <= 180 and 0 <= m["out_max"] <= 180):
            return False

    return True

def validate_program(data):
    try:
        if "rules" in data:
            for r in data["rules"]:
                for a in r.get("actions", []):
                    if not validate_action(a):
                        return False

        if "default_actions" in data:
            for a in data["default_actions"]:
                if not validate_action(a):
                    return False

        if "mappings" in data:
            for m in data["mappings"]:
                if not validate_mapping(m):
                    return False

        return True
    except:
        return False

def error_state():
    if "led1" in hw_outputs:
        apply_single_led(hw_outputs["led1"]["obj"], [255, 0, 0, 0])
    if "led2" in hw_outputs:
        apply_single_led(hw_outputs["led2"]["obj"], [0, 0, 0, 0])
    if "piezo" in hw_outputs:
        hw_outputs["piezo"]["obj"].duty_cycle = 0

def smooth(old, new, alpha=0.2):
    return old * (1 - alpha) + new * alpha

# --- MQTT SETUP ---
client_id = f"itsybitsy_{BOARD_ID[:8]}"
mqtt_topic = f"toolkit/{BOARD_ID}/command"
mqtt_publish_topic = f"toolkit/{BOARD_ID}/sensors"

# Test DNS (optionnel mais utile)
print("Test DNS...")
try:
    addr = pool.getaddrinfo("ide-education.cloud.shiftr.io", 1883)
    print("DNS OK:", addr[0][4])
except Exception as e:
    print("DNS FAILED:", e)

# --- MQTT MESSAGE HANDLER ---

def on_message(client, topic, message):
    print("Received message:", message)
    global rules, mappings, default_actions
    global cal_input, cal_min, cal_max, cal_start, cal_end
    global led1_index, led2_index, piezo_index
    global ANIMATION_SPEED, LED1_BRIGHTNESS, LED2_BRIGHTNESS
    global piezo_state, servo_state

    try:
        data = json.loads(message)
        if "version" not in data:
            print("Missing version"); return
        if data["version"] != 1:
            print("Unsupported version:", data["version"]); return
        if not validate_program(data):
            print("INVALID PROGRAM RECEIVED")
            mqtt_client.publish(mqtt_publish_topic, json.dumps({"error": "invalid_program"}))
            error_state()
            return

        print("\n--- NEW MQTT MESSAGE RECEIVED ---")
        LED1_BRIGHTNESS = 1.0
        LED2_BRIGHTNESS = 1.0
        piezo_state = {"frequencies": [], "volume": 0.0}
        servo_state = {"angle": 90}
        gc.collect()

        cmd = data.get("command", "")
        if cmd.startswith("calibrate_"):
            name = cmd[len("calibrate_"):]
            if name in ANALOG_INPUTS:
                cal_input = name
                cal_min = 65535
                cal_max = 0
                cal_start = time.monotonic() + 5.0
                cal_end = cal_start + CALIBRATION_DURATION
                print("Calibrating:", name)
            else:
                print("Unknown calibration input:", name)

        if "animation_speed" in data: ANIMATION_SPEED = float(data["animation_speed"])
        if "hardware_config" in data: init_hardware(data["hardware_config"])
        if "rules" in data: rules = data["rules"]
        if "mappings" in data: mappings = data["mappings"]
        if "default_actions" in data: default_actions = data["default_actions"]

        led1_index = 0
        led2_index = 0
        piezo_index = 0
    except Exception as e:
        print("Erreur de lecture MQTT :", e)
        gc.collect()

# --- CONNEXION MQTT (une seule fois !) ---
mqtt_client, loop_timeout = Create_MQTT(client_id, pool, esp)
mqtt_client.on_message = on_message

gc.collect()

mqtt_client.subscribe(mqtt_topic)
print("Subscribed")
print("Ready on topic:", mqtt_topic)

# --- SETUP ---

for name, comp in hw_outputs.items():
    if comp["type"] == "neopixel":
        apply_single_led(comp["obj"], [0, 0, 0, 0])
    elif comp["type"] == "piezo":
        comp["obj"].duty_cycle = 0
    elif comp["type"] == "digital_in":
        raw_val = 0 if comp["obj"].value else 1  # Pull.UP : False = pressé


# --- MAIN LOOP ---

while True:
    try:
        board_led.value = True

        if not mqtt_client.is_connected():
            print("Connexion MQTT perdue")
            if not esp.is_connected:
                print("WiFi perdu aussi, reconnexion WiFi...")
                try:
                    esp.connect_AP(ssid, password)
                except Exception as e:
                    print("Échec WiFi:", e)
                    microcontroller.reset()
            mqtt_client.reconnect()

        mqtt_client.loop(timeout=loop_timeout)
        now_ms = time.monotonic() * 1000

        for name, comp in hw_inputs.items():
            raw_val = 0
            if comp["type"] == "touch":
                raw_val = 1 if comp["obj"].value else 0
            elif comp["type"] == "analog":
                raw_val = comp["obj"].value
            elif comp["type"] == "vl53l0x":
                try: raw_val = comp["obj"].range
                except: raw_val = inputs.get(name, 0)
            elif comp["type"] == "digital_in":
                raw_val = 1 if comp["obj"].value else 0

            if comp["type"] in ("analog", "vl53l0x"):
                val = smooth(inputs.get(name, raw_val), raw_val)
            else:
                val = raw_val

            if val != input_last_values.get(name, None):
                input_start_times[name] = now_ms
                input_last_values[name] = val
                input_durations[name] = 0
            else:
                input_durations[name] = now_ms - input_start_times.get(name, 0)

            inputs[name] = val

            if cal_input == name:
                now = time.monotonic()
                if cal_start <= now <= cal_end:
                    if val < cal_min: cal_min = val
                    if val > cal_max: cal_max = val

            # --- FIN DE CALIBRATION ---
        if cal_input is not None and time.monotonic() > cal_end:
            print(f"CALIBRATION TERMINÉE pour {cal_input}: Min={cal_min}, Max={cal_max}")
            calibration_result = {
                "status": "calibration_finished",
                "sensor": cal_input,
                "min": cal_min,
                "max": cal_max
            }
            mqtt_client.publish(mqtt_publish_topic, json.dumps(calibration_result))
            cal_input = None

        inputs_changed = any(inputs.get(k) != last_published_inputs.get(k) for k in inputs)
        now_sec = time.monotonic()
        if inputs_changed or (now_sec - last_inputs_print >= 2.0):
            mqtt_client.publish(mqtt_publish_topic, json.dumps(inputs))
            last_published_inputs = dict(inputs)
            last_inputs_print = now_sec

        # --- 3. MOTEUR DE RÈGLES (LOGIC ENGINE) ---
        # 1. Baseline : On commence avec les actions par défaut
        led1_state = [[0, 0, 0, 0]]
        led2_state = [[0, 0, 0, 0]]
        for action in default_actions:
            if action.get("output") == "led1": led1_state = action.get("values") or [[0,0,0,0]]
            if action.get("output") == "led2": led2_state = action.get("values") or [[0,0,0,0]]

        # 2. Persistance : Si un toggle est activé en mémoire, il remplace la baseline
        if toggle_output_states.get("led1"): led1_state = toggle_output_states.get("led1_val")
        if toggle_output_states.get("led2"): led2_state = toggle_output_states.get("led2_val")

        current_piezo_state = {"frequencies": [], "volume": 0.0}
        current_servo_state = {"angle": 90}
        active_rules_for = {"led1": False, "led2": False, "piezo": False, "servo": False}

        # On crée une variable locale pour la vitesse d'animation de cette boucle
        current_anim_speed = ANIMATION_SPEED

        # 3. Évaluer les règles par priorité (le plus petit chiffre d'abord, ex: 1 avant 2)
        sorted_rules = sorted(rules, key=lambda r: r.get("priority", 999))
        for r in sorted_rules:
            rule_id = r.get("label", "unnamed")
            is_match = rule_matches(r)
            was_match = rule_last_results.get(rule_id, False)
            just_triggered = is_match and not was_match
            rule_last_results[rule_id] = is_match

            # Si la règle est active, elle peut modifier la vitesse d'animation
            if is_match and r.get("animation_speed") is not None:
                current_anim_speed = float(r["animation_speed"])

            for action in r.get("actions", []):
                outp = action.get("output")

                # Si une règle de priorité supérieure a déjà pris le contrôle de cette sortie, on ignore
                if active_rules_for.get(outp):
                    continue

                # CAS A : GESTION DU TOGGLE (Interrupteur)
                if action.get("toggle", False):
                    if just_triggered:
                        # On inverse l'état mémorisé
                        new_state = not toggle_output_states.get(outp, False)
                        toggle_output_states[outp] = new_state
                        # On stocke la couleur à utiliser pour cet état ON
                        toggle_output_states[outp + "_val"] = action.get("values") or [[255,255,255,0]]

                    # Si le bouton est actuellement activé par le toggle, on marque la sortie comme occupée
                    # pour que les mappings ne viennent pas interférer (optionnel, selon ton choix)
                    if is_match:
                        active_rules_for[outp] = True

                # CAS B : GESTION MOMENTANÉE (Priorité sur le toggle si is_match)
                elif is_match:
                    active_rules_for[outp] = True
                    if outp == "led1": led1_state = action.get("values") or [[255,255,255,0]]
                    elif outp == "led2": led2_state = action.get("values") or [[255,255,255,0]]
                    elif outp == "piezo":
                        current_piezo_state["frequencies"] = action.get("frequencies") or []
                        current_piezo_state["volume"] = action.get("volume") or 0.0
                    elif outp == "servo":
                        current_servo_state["angle"] = action.get("angle") or 90

        piezo_state = current_piezo_state
        servo_state = current_servo_state

        # On applique les mappings seulement sur les sorties qui n'ont pas été "prises" par une règle
        apply_mappings(active_rules_for)

        # --- 4. EXÉCUTION DES ACTIONS ---
        if "vibration" in hw_outputs:
            # On réutilise le même mécanisme que le piezo
            vib_obj = hw_outputs["vibration"]["obj"]
            freq_list = piezo_state.get("frequencies", [])
            if freq_list and freq_list[0] > 0:
                vib_obj.duty_cycle = int(piezo_state["volume"] * 65535)
            else:
                vib_obj.duty_cycle = 0

        # Servo
        if "servo" in hw_outputs:
            hw_outputs["servo"]["obj"].angle = max(0, min(180, servo_state["angle"]))

        now = time.monotonic()

        # LEDs (Utilise current_anim_speed au lieu de la globale fixe)
        if now - last_led_update >= current_anim_speed:
            last_led_update = now
            if "led1" in hw_outputs and led1_state:
                led1_index = (led1_index + 1) % len(led1_state)
                apply_single_led(hw_outputs["led1"]["obj"], led1_state[led1_index], LED1_BRIGHTNESS)
            if "led2" in hw_outputs and led2_state:
                led2_index = (led2_index + 1) % len(led2_state)
                apply_single_led(hw_outputs["led2"]["obj"], led2_state[led2_index], LED2_BRIGHTNESS)

        # Piezo
        if "piezo" in hw_outputs and now - last_piezo_update >= ANIMATION_SPEED:
            freq_list = piezo_state.get("frequencies", [])
            piezo_obj = hw_outputs["piezo"]["obj"]
            if freq_list:
                piezo_index = (piezo_index + 1) % len(freq_list)
                f = freq_list[piezo_index]
                if f > 0:
                    piezo_obj.frequency = int(f)
                    piezo_obj.duty_cycle = int(piezo_state["volume"] * 32768)
                else: piezo_obj.duty_cycle = 0
            else: piezo_obj.duty_cycle = 0
            last_piezo_update = now

    except Exception as e:
        print("Loop error:", e)
        gc.collect()
        time.sleep(0.5)
