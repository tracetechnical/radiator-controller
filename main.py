#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
import queue
import requests
import paho.mqtt.client as mqtt
from typing import Any, Dict, Optional
from flask import Flask, Response, render_template_string

# =======================
# CONFIG
# =======================
MQTT_HOST = os.getenv("MQTT_HOST", "mqtt.io.home")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
KEEPALIVE = int(os.getenv("KEEPALIVE", 30))

ROOM_NAME = os.getenv("ROOM_NAME")
if not ROOM_NAME:
    print("❌ ROOM_NAME environment variable is required")
    sys.exit(1)

HEAT_ALERT_TOPIC = os.getenv("HEAT_ALERT_TOPIC")
if not HEAT_ALERT_TOPIC:
    print("❌ HEAT_ALERT_TOPIC environment variable is required")
    sys.exit(1)

# Global sleep topic (default heating/sleep)
HEATING_SLEEP_GLOBAL_TOPIC = os.getenv(
    "HEATING_SLEEP_GLOBAL_TOPIC", "heating/sleep"
)

BOOST_SECONDS = int(os.getenv("BOOST_SECONDS", 500))
SP_DEBOUNCE_SECONDS = int(os.getenv("SP_DEBOUNCE_SECONDS", 5))
WEB_PORT = int(os.getenv("WEB_PORT", 8000))
ALERT_CHECK_INTERVAL = int(os.getenv("ALERT_CHECK_INTERVAL", 60))
ALERT_NO_CHANGE_SECONDS = int(os.getenv("ALERT_NO_CHANGE_SECONDS", 600))
ROOM_CONFIG_REFRESH_SECONDS = 300  # 5 minutes

# =======================
# STATE
# =======================
_ignore_initial_valve_updates = True

radiator_valve_state: Dict[str, Any] = {
    "cfh": None
}

sleep_mode: Optional[bool] = None
last_published: Dict[str, Any] = {}
last_cfh = None

_ignore_sensor_until = 0.0
_ignore_sp_until = 0.0

_last_temp = None
_last_temp_time = None

ROOM_TOPIC = None
MQTT_TOPIC_THERMOSTAT = None
MQTT_TOPIC_CORRECTION = None

mqtt_client_ref: mqtt.Client = None

# SSE
sse_queue = queue.Queue(maxsize=10)

# =======================
# UTILS
# =======================
def log(msg, *args):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", *args)

def decode_payload(payload):
    try:
        return json.loads(payload.decode())
    except Exception:
        return None

def broadcast_sse():
    try:
        sse_queue.put_nowait(json.dumps(radiator_valve_state))
    except queue.Full:
        pass

def publish(client: mqtt.Client, payload: dict, topic: str = None):
    if topic is None:
        topic = f"{MQTT_TOPIC_THERMOSTAT}/set"
    to_publish = {}
    for k, v in payload.items():
        if last_published.get(k) != v:
            to_publish[k] = v
            last_published[k] = v
    if to_publish:
        client.publish(topic, json.dumps(to_publish), qos=0)
        log(f"📤 Published to {topic}: {to_publish}")
        broadcast_sse()

# =======================
# CFH
# =======================
def update_cfh(client: mqtt.Client):
    global last_cfh
    sp = radiator_valve_state.get("current_heating_setpoint")
    boost = radiator_valve_state.get("boost_timeset_countdown", 0)
    temp = radiator_valve_state.get("room_sensor_temp")

    if sp is None or temp is None:
        return

    cfh = temp < sp or boost > 0
    radiator_valve_state["cfh"] = cfh

    if cfh != last_cfh:
        client.publish(f"{ROOM_TOPIC}/cfh", json.dumps(cfh), qos=0)
        last_cfh = cfh
        log(f"🔥 CFH → {cfh}")
        broadcast_sse()

def periodic_cfh_loop():
    while True:
        time.sleep(20)
        if mqtt_client_ref:
            update_cfh(mqtt_client_ref)

# =======================
# ROOM CONFIG
# =======================
def fetch_room_config(room_name: str):
    global ROOM_TOPIC, MQTT_TOPIC_THERMOSTAT, MQTT_TOPIC_CORRECTION
    url = f"http://json.io.home/heating?name={room_name.replace(' ', '%20')}"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return
        room = data[0]

        ROOM_TOPIC = room["rootTopic"]
        room_short = room_name.replace(" ", "")
        MQTT_TOPIC_THERMOSTAT = f"zigbee2mqtt/{room_short}RV"
        MQTT_TOPIC_CORRECTION = f"zigbee2mqtt/{room_short}TH"

        updates = {
            "eco_temperature": room["sleepTemperature"],
            "comfort_temperature": room["standbyTemperature"],
            "current_heating_setpoint": room["standbyTemperature"]
        }

        for k, v in updates.items():
            if radiator_valve_state.get(k) != v:
                radiator_valve_state[k] = v
                if mqtt_client_ref:
                    publish(mqtt_client_ref, {k: v})

        log("🔄 Room config refreshed")
    except Exception as e:
        log("❌ Room config refresh failed:", e)

def room_config_loop():
    while True:
        time.sleep(ROOM_CONFIG_REFRESH_SECONDS)
        fetch_room_config(ROOM_NAME)
        broadcast_sse()

# =======================
# SLEEP HANDLING
# =======================
def apply_sleep_mode(client: mqtt.Client, new_sleep: bool):
    global sleep_mode

    if sleep_mode == new_sleep:
        return

    sleep_mode = new_sleep
    radiator_valve_state["sleep"] = sleep_mode

    if sleep_mode:
        sp = radiator_valve_state.get("eco_temperature")
        log("🌙 Global sleep ON")
    else:
        sp = radiator_valve_state.get("comfort_temperature")
        log("☀️ Global sleep OFF")

    if sp is not None:
        radiator_valve_state["current_heating_setpoint"] = sp
        publish(client, {"current_heating_setpoint": sp})

    update_cfh(client)
    broadcast_sse()

def parse_sleep_payload(payload: Any) -> Optional[bool]:
    if isinstance(payload, bool):
        return payload
    if isinstance(payload, (int, float)):
        return bool(payload)
    if isinstance(payload, str):
        p = payload.lower()
        if p in ("1", "true", "on", "yes"):
            return True
        if p in ("0", "false", "off", "no"):
            return False
    return None

# =======================
# MQTT CALLBACKS
# =======================
def on_connect(client, userdata, flags, rc):
    log("✅ MQTT connected", rc)
    client.subscribe(MQTT_TOPIC_THERMOSTAT, qos=0)
    client.subscribe(MQTT_TOPIC_CORRECTION, qos=0)
    client.subscribe(f"{ROOM_TOPIC}/#", qos=0)
    client.subscribe(HEATING_SLEEP_GLOBAL_TOPIC, qos=0)

def on_message(client, userdata, msg):
    global _last_temp, _last_temp_time

    payload = decode_payload(msg.payload)

    # Global sleep
    if msg.topic == HEATING_SLEEP_GLOBAL_TOPIC:
        sleep_val = parse_sleep_payload(payload)
        if sleep_val is not None:
            apply_sleep_mode(client, sleep_val)
        return

    if not isinstance(payload, dict):
        return

    if msg.topic == MQTT_TOPIC_THERMOSTAT:
        for k, v in payload.items():
            radiator_valve_state[k] = v
        update_cfh(client)
        broadcast_sse()

    elif msg.topic == MQTT_TOPIC_CORRECTION:
        temp = payload.get("temperature")
        if temp is not None:
            radiator_valve_state["room_sensor_temp"] = temp
            if _last_temp != temp:
                _last_temp = temp
                _last_temp_time = time.time()
            update_cfh(client)
            broadcast_sse()

# =======================
# HEAT ALERT
# =======================
def heat_alert_loop():
    global _last_temp_time
    while True:
        time.sleep(ALERT_CHECK_INTERVAL)
        sp = radiator_valve_state.get("current_heating_setpoint")
        temp = radiator_valve_state.get("room_sensor_temp")
        if sp and temp and _last_temp_time:
            if sp > temp and time.time() - _last_temp_time > ALERT_NO_CHANGE_SECONDS:
                mqtt_client_ref.publish(
                    HEAT_ALERT_TOPIC,
                    json.dumps({"room": ROOM_NAME, "temp": temp, "sp": sp})
                )
                log("⚠️ Heat alert sent")
                _last_temp_time = time.time()

# =======================
# FLASK
# =======================
app = Flask(__name__)

@app.route("/events")
def sse():
    def gen():
        yield "data: %s\n\n" % json.dumps(radiator_valve_state)
        while True:
            yield "data: %s\n\n" % sse_queue.get()
    return Response(gen(), mimetype="text/event-stream")

@app.route("/")
def index():
    return render_template_string("""
<html>
<head><title>{{room}}</title></head>
<body>
<h1>{{room}}</h1>
<pre id="out"></pre>
<script>
const es = new EventSource("/events");
es.onmessage = e => {
  document.getElementById("out").innerText =
    JSON.stringify(JSON.parse(e.data), null, 2);
};
</script>
</body>
</html>
""", room=ROOM_NAME)

# =======================
# MAIN
# =======================
def main():
    global mqtt_client_ref, _last_temp_time
    fetch_room_config(ROOM_NAME)

    client = mqtt.Client()
    mqtt_client_ref = client
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, KEEPALIVE)
    client.loop_start()

    _last_temp_time = time.time()

    threading.Thread(target=heat_alert_loop, daemon=True).start()
    threading.Thread(target=periodic_cfh_loop, daemon=True).start()
    threading.Thread(target=room_config_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)

if __name__ == "__main__":
    main()
