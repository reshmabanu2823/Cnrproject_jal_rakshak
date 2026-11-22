// JalRakshak flow sensor node: ESP32 + YF-S201 hall-effect flow sensor.
//
// Wiring: YF-S201 red -> 5V, black -> GND, yellow (signal) -> GPIO 27 via a
// 10k/20k divider (the sensor outputs 5V; ESP32 pins are 3.3V).
//
// Every SEND_INTERVAL_MS it POSTs the pulses counted since the last send to
// /api/ingest. Each batch has a sequence number, so a retry after a Wi-Fi drop is
// ignored by the server instead of double-counted. Readings that could not be sent
// are buffered (up to BUFFER_SIZE) and flushed when the network is back.
//
// Board: "ESP32 Dev Module". Libraries: WiFi, HTTPClient (bundled), ArduinoJson 7.

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <time.h>

// ---- configure ----
const char* WIFI_SSID   = "your-wifi";
const char* WIFI_PASS   = "your-password";
const char* SERVER_URL  = "http://192.168.1.10:8000/api/ingest";
const char* DEVICE_KEY  = "jr_paste_key_from_POST_/api/devices";
const char* FIRMWARE    = "1.0.0";
const int   SENSOR_PIN  = 27;
const unsigned long SEND_INTERVAL_MS = 60UL * 1000;  // one reading per minute
const int   BUFFER_SIZE = 120;                         // 2 hours of offline buffering
// -------------------

volatile uint32_t pulses = 0;
void IRAM_ATTR onPulse() { pulses++; }

struct Reading { uint64_t seq; time_t ts; uint32_t pulses; };
Reading buffer[BUFFER_SIZE];
int buffered = 0;
// seq = (boot count << 32) | readings this boot. Strictly increasing across reboots,
// with or without NTP, so the server's (device, seq) dedupe never drops real data.
uint64_t seq = 0;
unsigned long lastSend = 0;

void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 20 && WiFi.status() != WL_CONNECTED; i++) delay(500);
}

bool flush() {
  connectWifi();
  if (WiFi.status() != WL_CONNECTED || buffered == 0) return false;

  JsonDocument doc;
  doc["firmware"] = FIRMWARE;
  JsonArray arr = doc["readings"].to<JsonArray>();
  for (int i = 0; i < buffered; i++) {
    JsonObject r = arr.add<JsonObject>();
    r["seq"] = buffer[i].seq;
    r["pulses"] = buffer[i].pulses;
    if (buffer[i].ts > 1700000000) {  // only send a timestamp once NTP has synced
      char iso[25];
      strftime(iso, sizeof iso, "%Y-%m-%dT%H:%M:%SZ", gmtime(&buffer[i].ts));
      r["ts"] = iso;
    }
  }
  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code = http.POST(body);
  http.end();
  if (code == 200) { buffered = 0; return true; }
  return false;  // keep the buffer; same seq numbers are retried next time
}

void setup() {
  Serial.begin(115200);
  pinMode(SENSOR_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(SENSOR_PIN), onPulse, FALLING);
  connectWifi();
  configTime(0, 0, "pool.ntp.org");
  Preferences prefs;
  prefs.begin("jalrakshak", false);
  uint32_t boots = prefs.getUInt("boots", 0) + 1;  // one flash write per boot
  prefs.putUInt("boots", boots);
  prefs.end();
  seq = (uint64_t)boots << 32;
}

void loop() {
  if (millis() - lastSend < SEND_INTERVAL_MS) return;
  lastSend = millis();

  noInterrupts();
  uint32_t count = pulses;
  pulses = 0;
  interrupts();

  if (buffered == BUFFER_SIZE) {  // full: drop oldest
    memmove(buffer, buffer + 1, sizeof(Reading) * (BUFFER_SIZE - 1));
    buffered--;
  }
  buffer[buffered++] = { ++seq, time(nullptr), count };  // 0 pulses still acts as a heartbeat
  bool ok = flush();
  Serial.printf("pulses=%lu buffered=%d sent=%d\n", (unsigned long)count, buffered, ok);
}
