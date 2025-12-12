from machine import Pin, SPI, I2C
import time
import network
import socket
from mfrc522 import MFRC522
from machine_i2c_lcd import I2cLcd
from umqtt.simple import MQTTClient
import json
 
# HTTP client for Telegram
try:
    import urequests as requests
except ImportError:
    import requests

# ---------- Wi-Fi ----------
WIFI_SSID = "QualityWIFI"
WIFI_PASSWORD = "12964246"

# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN = "8314612108:AAHiF8LN4gQGacy1CtMl28ORjDalBw5ScCI"
TELEGRAM_CHAT_ID = "809445408"
TELEGRAM_URL = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"

# ---------- MQTT (for Node-RED / Influx) ----------
MQTT_BROKER = "test.mosquitto.org"
MQTT_PORT = 1883
MQTT_TOPIC = "/aupp/esp32/doorlock"
MQTT_CLIENT_ID = "esp32_door_" + str(time.ticks_ms())
mqtt_client = None

# ---------- RFID / LCD / Relay pins ----------
SCK_PIN = 18
MOSI_PIN = 23
MISO_PIN = 19
CS_PIN = 5
RST_PIN = 17

I2C_SCL_PIN = 22
I2C_SDA_PIN = 21
I2C_ADDR = 0x27

RELAY_PIN = 26

# Authorized card/tag UID
AUTHORIZED_UID = [0x37, 0x31, 0x4F, 0x06, 0x4F]

door_locked = True
unlock_deadline = None
failed_attempts = 0

# ---------- Web UI HTML ----------
HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>ESP32 Door Lock</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: Arial, sans-serif; text-align:center; padding:20px; }
    h2 { margin-bottom:10px; }
    #status { font-weight:bold; }
    button { padding:10px 20px; margin:10px; font-size:16px; }
  </style>
</head>
<body>
  <h2>ESP32 RFID Door Lock</h2>
  <p>Door status: <span id="status">...</span></p>
  <button onclick="sendCmd('unlock')">Unlock door</button>
  <button onclick="sendCmd('lock')">Lock door</button>
  <p style="font-size:12px;color:#666;">Physical RFID scan has priority.</p>
  <script>
    function refreshStatus() {
      fetch('/status')
        .then(r => r.text())
        .then(t => { document.getElementById('status').textContent = t; })
        .catch(e => console.log(e));
    }
    function sendCmd(cmd) {
      fetch('/' + cmd)
        .then(r => r.text())
        .then(_ => { setTimeout(refreshStatus, 200); })
        .catch(e => console.log(e));
    }
    setInterval(refreshStatus, 1000);
    refreshStatus();
  </script>
</body>
</html>
"""

# ---------- Init hardware ----------
spi = SPI(
    1,
    baudrate=2000000,  # faster for RFID
    polarity=0,
    phase=0,
    sck=Pin(SCK_PIN),
    mosi=Pin(MOSI_PIN),
    miso=Pin(MISO_PIN),
)
cs = Pin(CS_PIN, Pin.OUT)
rfid = MFRC522(spi, cs)

i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=400000)
lcd = I2cLcd(i2c, I2C_ADDR, 2, 16)

relay = Pin(RELAY_PIN, Pin.OUT)

# ---------- WiFi ----------
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to Wi-Fi...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        while not wlan.isconnected():
            time.sleep_ms(200)
    print("Wi-Fi connected:", wlan.ifconfig())
    return wlan.ifconfig()[0]

# ---------- MQTT ----------
def mqtt_connect():
    global mqtt_client
    try:
        mqtt_client = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER, MQTT_PORT)
        mqtt_client.connect()
        print("MQTT connected to", MQTT_BROKER)
    except Exception as e:
        print("MQTT connect failed:", e)
        mqtt_client = None

def mqtt_publish_event(event, status, source, uid_hex_list=None):
    if mqtt_client is None:
        return
    try:
        if uid_hex_list:
            uid_str = "".join(h[2:] for h in uid_hex_list)  # "0x37" -> "37"
        else:
            uid_str = ""
        payload = {
            "timestamp": time.time(),
            "event": event,    # e.g., ACCESS_GRANTED, ACCESS_DENIED, LOCKED, UNLOCKED
            "status": status,  # e.g., LOCKED / UNLOCKED
            "source": source,  # RFID / WEB / SYSTEM
            "uid": uid_str
        }
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload))
        print("MQTT published:", payload)
    except Exception as e:
        print("MQTT publish error:", e)

# ---------- Telegram ----------
def send_telegram_alert(message):
    try:
        data = "chat_id={}&text={}".format(TELEGRAM_CHAT_ID, message)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        r = requests.post(TELEGRAM_URL, data=data, headers=headers)
        r.close()
        print("Telegram alert sent")
    except Exception as e:
        print("Telegram error:", e)

# ---------- Door control ----------
def lcd_show_state():
    lcd.clear()
    if door_locked:
        lcd.move_to(0, 0)
        lcd.putstr("Door Locked")
    else:
        lcd.move_to(0, 0)
        lcd.putstr("Door Unlocked")

def lock_door():
    global door_locked, unlock_deadline
    relay.value(0)  # bolt OUT = locked
    door_locked = True
    unlock_deadline = None
    lcd_show_state()
    print("Door Locked")
    mqtt_publish_event("LOCKED", "LOCKED", "SYSTEM")

def unlock_door(source="REMOTE"):
    global door_locked, unlock_deadline
    relay.value(1)  # bolt IN = unlocked
    door_locked = False
    unlock_deadline = time.ticks_add(time.ticks_ms(), 15000)  # auto-lock after 15s
    lcd_show_state()
    lcd.move_to(0, 1)
    lcd.putstr("Access Granted ")
    print("Access Granted - Door Unlocked ({})".format(source))
    mqtt_publish_event("UNLOCKED", "UNLOCKED", source)

def auto_lock_check():
    global unlock_deadline
    if not door_locked and unlock_deadline is not None:
        if time.ticks_diff(unlock_deadline, time.ticks_ms()) <= 0:
            print("Auto-lock timeout reached")
            lock_door()

def is_authorized(uid_list):
    if len(uid_list) != len(AUTHORIZED_UID):
        return False
    for i in range(len(AUTHORIZED_UID)):
        if uid_list[i] != AUTHORIZED_UID[i]:
            return False
    return True

# ---------- HTTP server ----------
def start_http_server(ip):
    addr = socket.getaddrinfo(ip, 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    s.settimeout(0.0)
    print("HTTP server listening on http://%s/" % ip)
    return s

def handle_http_request(sock):
    try:
        cl, addr = sock.accept()
    except OSError:
        return
    try:
        req = cl.recv(1024)
        if not req:
            cl.close()
            return
        req = req.decode("utf-8")
        first_line = req.split("\r\n")[0]
        parts = first_line.split(" ")
        if len(parts) < 2:
            cl.close()
            return
        path = parts[1]

        if path == "/":
            cl.send("HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n")
            cl.send(HTML_PAGE)
        elif path == "/unlock":
            unlock_door(source="WEB")
            cl.send("HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nOK")
        elif path == "/lock":
            lock_door()
            cl.send("HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\nOK")
        elif path == "/status":
            status = "LOCKED" if door_locked else "UNLOCKED"
            cl.send("HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n" + status)
        else:
            cl.send("HTTP/1.0 404 NOT FOUND\r\nContent-Type: text/plain\r\n\r\nNot found")
    except Exception as e:
        print("HTTP error:", e)
    finally:
        cl.close()

# ---------- Startup ----------
lcd.clear()
lcd.move_to(0, 0)
lcd.putstr("RFID Door Lock")
print("RFID Door Lock")
time.sleep(1)

lock_door()
lcd.move_to(0, 1)
lcd.putstr("Scan card...")

ip = connect_wifi()
mqtt_connect()
sock = start_http_server(ip)

# ---------- Main loop ----------
while True:
    auto_lock_check()
    handle_http_request(sock)

    (stat, tag_type) = rfid.request(MFRC522.REQIDL)
    if stat == rfid.OK:
        (stat2, uid) = rfid.anticoll()
        if stat2 == rfid.OK:
            uid_hex = ["0x{:02X}".format(b) for b in uid]
            print("Card detected UID:", uid_hex)

            if is_authorized(uid):
                failed_attempts = 0
                lcd.clear()
                lcd.move_to(0, 0)
                lcd.putstr("Access Granted")
                lcd.move_to(0, 1)
                lcd.putstr("Door Unlocked ")
                print("Access Granted (RFID) - UID:", uid_hex)
                mqtt_publish_event("ACCESS_GRANTED", "UNLOCKED", "RFID", uid_hex)
                time.sleep(0.1)
                unlock_door(source="RFID")
            else:
                failed_attempts += 1
                lcd.clear()
                lcd.move_to(0, 0)
                lcd.putstr("Access Denied")
                lcd.move_to(0, 1)
                lcd.putstr("Unknown UID  ")
                print("Access Denied (Unknown UID):", uid_hex)
                mqtt_publish_event("ACCESS_DENIED", "LOCKED", "RFID", uid_hex)

                time.sleep(2)      # keep "Access Denied" visible
                lock_door()

                if failed_attempts >= 3:
                    msg = "ALERT: 3 failed RFID attempts. Last UID: {}".format(" ".join(uid_hex))
                    send_telegram_alert(msg)
                    mqtt_publish_event("ALERT_TRIGGERED", "LOCKED", "RFID", uid_hex)
                    failed_attempts = 0

            lcd.move_to(0, 1)
            lcd.putstr("Scan card...")

    time.sleep_ms(10)
