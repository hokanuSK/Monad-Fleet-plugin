/*
 * ESP32 -> Monad Fleet metrics sender
 *
 * Sends small real-time metrics payloads to:
 *   POST http://<fleet-host>:9108/ingest/v1/metrics
 *
 * Configure Wi-Fi/Fleet constants below, flash, and keep running.
 */

#include <WiFi.h>
#include <HTTPClient.h>

static const char *WIFI_SSID = "YOUR_WIFI_SSID";
static const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

static const char *FLEET_HOST = "192.168.0.70";
static const uint16_t FLEET_PORT = 9108;
static const char *FLEET_TOKEN = "";  // optional x-ingest-token

static const char *DEVICE_ID = "esp32-room-01";
static const char *SOURCE = "arduino-esp32";

static const uint32_t SEND_INTERVAL_MS = 2000;
static uint32_t lastSendMs = 0;

String buildPayload() {
  long rssi = WiFi.RSSI();
  unsigned long uptimeS = millis() / 1000UL;
  uint32_t heapBytes = ESP.getFreeHeap();

  String json = "{";
  json += "\"source\":\"" + String(SOURCE) + "\",";
  json += "\"device_id\":\"" + String(DEVICE_ID) + "\",";
  json += "\"metrics\":[";
  json += "{\"name\":\"arduino_wifi_rssi_dbm\",\"value\":" + String(rssi) + "},";
  json += "{\"name\":\"arduino_uptime_s\",\"value\":" + String(uptimeS) + "},";
  json += "{\"name\":\"arduino_heap_free_bytes\",\"value\":" + String(heapBytes) + "}";
  json += "]}";
  return json;
}

void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] connecting");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; ++i) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] connected ip=");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[wifi] connect failed");
  }
}

void sendMetrics() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }

  HTTPClient http;
  String url = "http://" + String(FLEET_HOST) + ":" + String(FLEET_PORT) + "/ingest/v1/metrics";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  if (strlen(FLEET_TOKEN) > 0) {
    http.addHeader("x-ingest-token", FLEET_TOKEN);
  }

  String payload = buildPayload();
  int status = http.POST(payload);
  if (status > 0) {
    String body = http.getString();
    Serial.print("[fleet] status=");
    Serial.print(status);
    Serial.print(" body=");
    Serial.println(body);
  } else {
    Serial.print("[fleet] post failed err=");
    Serial.println(http.errorToString(status));
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("[boot] esp32 fleet sender starting");
  ensureWifi();
}

void loop() {
  ensureWifi();
  uint32_t now = millis();
  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = now;
    sendMetrics();
  }
  delay(50);
}
