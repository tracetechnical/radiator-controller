#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
import queue
import requests
import paho.mqtt.client as mqtt
from typing import Any, Dict, Callable
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

BOOST_SECONDS = int(os.getenv("BOOST_SECONDS", 500))
SP_DEBOUNCE_SECONDS = int(os.getenv("SP_DEBOUNCE_SECONDS", 5))
WEB_PORT = int(os.getenv("WEB_PORT", 8000))
ALERT_CHECK_INTERVAL = int(os.getenv("ALERT_CHECK_INTERVAL", 60))
ALERT_NO_CHANGE_SECONDS = int(os.getenv("ALERT_NO_CHANGE_SECONDS", 600))
GLOBAL_SLEEP_TOPIC = os.getenv("GLOBAL_SLEEP_TOPIC", "heating/sleep")
ROOM_CONFIG_REFRESH_INTERVAL = int(os.getenv("ROOM_CONFIG_REFRESH_INTERVAL", 300))

LWT_TOPIC = f"units/{ROOM_NAME.replace(' ', '').lower()}-radiator-control"

# =======================
# STATE
# =======================
_ignore_initial_valve_updates: bool = True

radiator_valve_state: Dict[str, Any] = {}
sleep_mode: Any = None
last_published: Dict[str, Any] = {}
last_cfh: Any = None

_ignore_sensor_until: float = 0.0
_ignore_sp_until: float = 0.0

_last_temp: float = None
_last_temp_time: float = None

ROOM_TOPIC: str = None
MQTT_TOPIC_THERMOSTAT: str = None
MQTT_TOPIC_CORRECTION: str = None

mqtt_client_ref: mqtt.Client = None

_next_room_config_fetch: float = None

sse_queue = queue.Queue(maxsize=10)

# =======================
# UTILS
# =======================
def log(msg, *args):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", *args)

def decode_payload(payload):
    try:
        return json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None

def merge_and_diff(update: dict) -> dict:
    global _ignore_initial_valve_updates
    changes = {}
    if not isinstance(update, dict):
        return changes
    for key, new_value in update.items():
        old_value = radiator_valve_state.get(key)
        if _ignore_initial_valve_updates and key in ["eco_temperature", "comfort_temperature", "current_heating_setpoint"]:
            continue
        if old_value != new_value and last_published.get(key) != new_value:
            changes[key] = (old_value, new_value)
            radiator_valve_state[key] = new_value
            log(f"🔄 {key} changed: {old_value} → {new_value}")
    return changes

def calculate_calibration(radiator_temp: float, calibration: float, room_temp: float) -> float:
    return room_temp - (radiator_temp - calibration)

def publish(client: mqtt.Client, payload: dict, topic: str = None):
    if topic is None:
        topic = f"{MQTT_TOPIC_THERMOSTAT}/set"
    to_publish = {}
    for key, value in payload.items():
        # Convert Python bools to lowercase strings before sending
        if isinstance(value, bool):
            value = "true" if value else "false"
        if last_published.get(key) != value:
            to_publish[key] = value
            last_published[key] = value
    if to_publish:
        client.publish(topic, json.dumps(to_publish), qos=0, retain=True)
        log(f"📤 Published to {topic}: {to_publish}")
        broadcast_sse()

# =======================
# PERIODIC CFH UPDATER
# =======================
def periodic_cfh_loop():
    global mqtt_client_ref
    while True:
        time.sleep(20)
        if mqtt_client_ref:
            update_cfh(mqtt_client_ref)

def update_cfh(client: mqtt.Client):
    global last_cfh
    sp = radiator_valve_state.get("current_heating_setpoint")
    boost_time = radiator_valve_state.get("boost_timeset_countdown")
    temp = radiator_valve_state.get("room_sensor_temp")
    if boost_time is None or sp is None or temp is None:
        return
    cfh = temp < sp or boost_time > 0
    if cfh != last_cfh:
        client.publish(f"{ROOM_TOPIC}/cfh", json.dumps(cfh), qos=0)
        last_cfh = cfh
        log(f"🔥 CFH update → {cfh} (room_temp={temp}, setpoint={sp})")
        broadcast_sse()

def broadcast_sse():
    try:
        sse_queue.put_nowait(json.dumps(radiator_valve_state))
    except queue.Full:
        pass

# =======================
# FETCH ROOM CONFIG
# =======================
def fetch_room_config(room_name: str):
    global ROOM_TOPIC, MQTT_TOPIC_THERMOSTAT, MQTT_TOPIC_CORRECTION, _next_room_config_fetch
    url = f"http://json.io.home/heating?name={room_name.replace(' ', '%20')}"
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            log("⚠️ No room data returned")
            return
        room = data[0]
        ROOM_TOPIC = room.get("rootTopic")
        room_name_smol = room_name.replace(" ", "")
        MQTT_TOPIC_THERMOSTAT = f"zigbee2mqtt/{room_name_smol}RV"
        MQTT_TOPIC_CORRECTION = f"zigbee2mqtt/{room_name_smol}TH"
        radiator_valve_state.update({
            "eco_temperature": room.get("sleepTemperature"),
            "comfort_temperature": room.get("standbyTemperature"),
            "current_heating_setpoint": room.get("standbyTemperature")
        })
        log(f"✅ Loaded room config: ROOM_TOPIC={ROOM_TOPIC}, ECO={radiator_valve_state['eco_temperature']}, "
            f"COMFORT={radiator_valve_state['comfort_temperature']}, SP={radiator_valve_state['current_heating_setpoint']}")
        _next_room_config_fetch = time.time() + ROOM_CONFIG_REFRESH_INTERVAL
        radiator_valve_state["_next_room_config_fetch"] = _next_room_config_fetch
        broadcast_sse()
    except Exception as e:
        log("❌ Failed to fetch room config:", e)
        exit(1)

def periodic_room_config_fetch():
    while True:
        time.sleep(5)
        if _next_room_config_fetch is None or time.time() >= _next_room_config_fetch:
            fetch_room_config(ROOM_NAME)

# =======================
# SPECIAL HANDLERS
# =======================
def sleep_special(client, value):
    global _ignore_sp_until
    sp = radiator_valve_state["eco_temperature"] if value else radiator_valve_state["comfort_temperature"]
    # radiator_valve_state["current_heating_setpoint"] = sp
    client.publish(f"{ROOM_TOPIC}/sp", json.dumps(sp), qos=0)
    _ignore_sp_until = time.time() + SP_DEBOUNCE_SECONDS
    client.publish(f"{ROOM_TOPIC}/sleep", "true" if value else "false", qos=0)
    log(f"{'🌙 Sleep ON' if value else '☀️ Sleep OFF'} → SP: {sp}")
    update_cfh(client)

def boost_special(client, value):
    if value in [True, 1, 1.0]:
        publish(client, {"boost_timeset_countdown": BOOST_SECONDS})
        log(f"🔥 Boost triggered → {BOOST_SECONDS} seconds")
    else:
        publish(client, {"boost_timeset_countdown": 1})
        log(f"🔥 Boost OFF")
    update_cfh(client)

def room_sensor_special(client, value: float):
    global _last_temp, _last_temp_time, _ignore_initial_valve_updates
    radiator_temp = radiator_valve_state.get("local_temperature")
    calibration = radiator_valve_state.get("local_temperature_calibration")
    if radiator_temp is not None and calibration is not None:
        corrected = calculate_calibration(radiator_temp, calibration, value)
        publish(client, {"local_temperature_calibration": round(corrected, 1)})
        log(f"🛠 Room sensor calibration applied: {round(corrected,1)}")
    radiator_valve_state["room_sensor_temp"] = value
    client.publish(f"{ROOM_TOPIC}/pv", json.dumps(value), qos=0, retain=True)
    update_cfh(client)
    if _last_temp != value:
        _last_temp = value
        _last_temp_time = time.time()
        log(f"🌡 Room temp updated: {value}")
    if _ignore_initial_valve_updates:
        _ignore_initial_valve_updates = False

# =======================
# GENERIC HANDLER
# =======================
def handle_update(client: mqtt.Client, payload, key: str, parse: Callable = float, special: Callable = None):
    if payload is None:
        return
    try:
        value = parse(payload)
    except (ValueError, TypeError):
        log(f"⚠️ Invalid {key} payload:", payload)
        return
    old_value = radiator_valve_state.get(key)
    if old_value == value:
        return
    log(f"🎯 {key.replace('_',' ').title()} update: {old_value} → {value}")
    radiator_valve_state[key] = value
    if special:
        special(client, value)
    if key in ["room_sensor_temp", "current_heating_setpoint"]:
        update_cfh(client)
    if key != "current_heating_setpoint":
        publish(client, {key: value})
    broadcast_sse()

# =======================
# MQTT CALLBACKS
# =======================
def on_connect(client, userdata, flags, rc):
    if rc != 0:
        log("❌ MQTT connection failed with code", rc)
        sys.exit(1)
    log("✅ Connected to MQTT:", rc)

    client.subscribe(MQTT_TOPIC_THERMOSTAT, qos=0)
    for key in ["eco_temperature", "comfort_temperature", "current_heating_setpoint"]:
        value = radiator_valve_state.get(key)
        if value is not None:
            last_published[key] = None
            publish(client, {key: value})
    client.subscribe(MQTT_TOPIC_CORRECTION, qos=0)
    client.subscribe(f"{ROOM_TOPIC}/#", qos=0)
    client.subscribe(GLOBAL_SLEEP_TOPIC, qos=0)

    # LWT setup
    client.will_set(LWT_TOPIC.lower(), "Dying connection", qos=0, retain=True)
    client.publish(LWT_TOPIC.lower(), "Online", qos=0, retain=True)

def on_disconnect(client, userdata, rc):
    log("🔌 Disconnected:", rc)
    client.publish(LWT_TOPIC.lower(), "Disconnecting", qos=0, retain=True)

def on_message(client, userdata, msg):
    payload = decode_payload(msg.payload)
    if payload is None:
        return

    if msg.topic == GLOBAL_SLEEP_TOPIC:
        log(f"🌐 Global sleep message received: {payload} → republishing to room")
        try:
            val = "true" if bool(payload) else "false"
            client.publish(f"{ROOM_TOPIC}/sleep", val, qos=0, retain=True)
        except Exception as e:
            log("⚠️ Failed to republish global sleep:", e)
        return

    topic_map = {
        MQTT_TOPIC_CORRECTION: lambda p: handle_update(client, p.get("temperature") if isinstance(p, dict) else None,
                                                      "room_sensor_temp", special=room_sensor_special),
        f"{ROOM_TOPIC}/sleep": lambda p: handle_update(client, p, "sleep_mode", special=sleep_special),
        f"{ROOM_TOPIC}/boost": lambda p: handle_update(client, p, "boost", special=boost_special),
        f"{ROOM_TOPIC}/sp": lambda p: handle_update(client, p, "current_heating_setpoint",
                                                    special=lambda c, v: publish(c, {"current_heating_setpoint": v})),
        f"{ROOM_TOPIC}/eco": lambda p: handle_update(client, p, "eco_temperature"),
        f"{ROOM_TOPIC}/comfort": lambda p: handle_update(client, p, "comfort_temperature"),
    }

    if msg.topic == MQTT_TOPIC_THERMOSTAT:
        changes = merge_and_diff(payload)
        for key, (_, new) in changes.items():
            radiator_valve_state[key] = new
            if key in ["eco_temperature", "comfort_temperature"]:
                client.publish(f"{ROOM_TOPIC}/{key.split('_')[0]}", json.dumps(new), qos=0)
            if key == "current_heating_setpoint":
                if time.time() >= _ignore_sp_until:
                    client.publish(f"{ROOM_TOPIC}/sp", json.dumps(new), qos=0, retain=True)
        update_cfh(client)
        broadcast_sse()
    elif msg.topic in topic_map:
        topic_map[msg.topic](payload)
        broadcast_sse()

# =======================
# HEAT ALERT LOOP
# =======================
def heat_alert_loop():
    global _last_temp_time
    while True:
        time.sleep(ALERT_CHECK_INTERVAL)
        sp = radiator_valve_state.get("current_heating_setpoint")
        temp = radiator_valve_state.get("room_sensor_temp")
        if sp is None or temp is None or _last_temp_time is None:
            continue
        if sp > temp and (time.time() - _last_temp_time) > ALERT_NO_CHANGE_SECONDS:
            mqtt_client_ref.publish(HEAT_ALERT_TOPIC, json.dumps({
                "room": ROOM_NAME,
                "current_temp": temp,
                "setpoint": sp,
                "time": time.time()
            }))
            log(f"⚠️ Heat alert! Room temp stuck at {temp}°C for over {ALERT_NO_CHANGE_SECONDS/60:.0f} min")
            _last_temp_time = time.time()
            broadcast_sse()

# =======================
# FLASK APP
# =======================
app = Flask(__name__)

@app.route("/healthz")
def health():
    return "OK"

@app.route("/events")
def sse():
    def gen():
        yield f"data: {json.dumps(radiator_valve_state)}\n\n"
        while True:
            data = sse_queue.get()
            yield f"data: {data}\n\n"
    return Response(gen(), mimetype="text/event-stream")

@app.route("/")
def index():
    html = """
<html>
<head>
<title>{{room_name}} Dashboard</title>
<style>
body { font-family: Arial; background:#f5f5f5; color:#333; margin:20px; }
h1 { color:#007acc; }
h2 { color:#005fa3; }
table { border-collapse: collapse; width:50%; background:#fff; margin-bottom:20px; }
th, td { border:1px solid #ddd; padding:8px; text-align:center; }
th { background-color:#007acc; color:white; }
tr:nth-child(even) {background:#f2f2f2;}
</style>
</head>
<body>
<h1>{{room_name}} - Dashboard</h1>
<h2>Radiator Valve</h2><table id="valve"></table>
<h2>Room Sensor</h2><table id="sensor"></table>
<h2>Internal State</h2><table id="internal"></table>
<h2>Heat Alert</h2><table id="alert"></table>
<h2>Next Room Config Fetch</h2>
<p id="next_fetch"></p>
<script>
const valveKeys=["current_heating_setpoint","eco_temperature","comfort_temperature"];
const sensorKeys=["room_sensor_temp"];
const internalKeys=["sleep_mode","last_cfh","_last_temp","_last_temp_time"];
const valve=document.getElementById("valve"), sensor=document.getElementById("sensor"), internal=document.getElementById("internal"), alert=document.getElementById("alert"), nextFetch=document.getElementById("next_fetch");

let nextFetchTime=null;

function render(data){
  valve.innerHTML="<tr><th>Key</th><th>Value</th></tr>";
  sensor.innerHTML="<tr><th>Key</th><th>Value</th></tr>";
  internal.innerHTML="<tr><th>Key</th><th>Value</th></tr>";
  alert.innerHTML="<tr><th>Metric</th><th>Value</th></tr>";
  for(const [k,v] of Object.entries(data)){
    if(valveKeys.includes(k)) valve.innerHTML+=`<tr><td>${k}</td><td>${v}</td></tr>`;
    else if(sensorKeys.includes(k)) sensor.innerHTML+=`<tr><td>${k}</td><td>${v}</td></tr>`;
    else if(internalKeys.includes(k)) internal.innerHTML+=`<tr><td>${k}</td><td>${v}</td></tr>`;
    else if(k=="_next_room_config_fetch") nextFetchTime=v;
  }
  const sp=data["current_heating_setpoint"]||"-";
  const temp=data["room_sensor_temp"]||"-";
  const ltt=data["_last_temp_time"];
  let elapsed="-";
  if(ltt){ const diff=Math.floor(Date.now()/1000 - ltt); elapsed=Math.floor(diff/60)+"m "+diff%60+"s"; }
  alert.innerHTML+=`<tr><td>Setpoint</td><td>${sp}</td></tr>`;
  alert.innerHTML+=`<tr><td>Temp</td><td>${temp}</td></tr>`;
  alert.innerHTML+=`<tr><td>Time Since Last Temp Change</td><td>${elapsed}</td></tr>`;
}

const evt=new EventSource("/events");
evt.onmessage=e=>{ render(JSON.parse(e.data)); };

setInterval(()=>{
  if(nextFetchTime){
    let now=Math.floor(Date.now()/1000);
    let diff=Math.floor(nextFetchTime - now);
    let target=new Date(nextFetchTime*1000);
    if(diff<0) diff=0;
    nextFetch.innerText=`Next fetch in ${diff}s (${target.toLocaleTimeString()})`;
  }
},1000);
</script>
</body>
</html>
"""
    return render_template_string(html, room_name=ROOM_NAME)

# =======================
# MAIN
# =======================
def main():
    global mqtt_client_ref
    fetch_room_config(ROOM_NAME)

    client = mqtt.Client()
    mqtt_client_ref = client
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    client.connect(MQTT_HOST, MQTT_PORT, KEEPALIVE)
    threading.Thread(target=client.loop_forever, daemon=True).start()
    threading.Thread(target=periodic_cfh_loop, daemon=True).start()
    threading.Thread(target=periodic_room_config_fetch, daemon=True).start()
    threading.Thread(target=heat_alert_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=WEB_PORT)

if __name__ == "__main__":
    main()
