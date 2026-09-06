// v2 firmware core — Phase 1 (UPGRADE_PLAN)
// Mains = optocoupler on D13 (GPIO 13), active-LOW: LOW = mains present.
// Network is for actuation + reporting only — never for mains detection.
// All secrets are placeholder tokens filled by deploy/ota-esp32.sh at build time.

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include <ArduinoOTA.h>
#include <ArduinoJson.h>
#include <ESP32Ping.h>
#include <esp_task_wdt.h>

// ==================== CONFIG (injected at build time) ====================
const char* WIFI_SSID            = "__WIFI_SSID__";
const char* WIFI_PASS            = "__WIFI_PASS__";
const uint8_t MAIN_ROUTER_BSSID[] = __WIFI_BSSID_BYTES__;
const char* OTA_PASSWORD         = "__OTA_PASSWORD__";
const char* FW_VERSION           = "V7.1";

const char* PROXMOX_IP           = "__PROXMOX_IP__";
const char* SHUTDOWN_URL         = "__PROXMOX_SHUTDOWN_URL__";
const char* NODE_MAC             = "__MAC__";
const char* WOL_BCAST            = "__WOL_BROADCAST__";
const char* PI_NOTIFY_URL        = "__PI_NOTIFY_URL__";
const char* NOTIFY_TOKEN         = "__NOTIFY_TOKEN__";
const int   PROXMOX_API_PORT     = 8006;

// ==================== GPIO & WAN ====================
const uint8_t MAINS_SENSE_PIN    = 13;
const unsigned long GPIO_STABLE_MS   = 3000;
const unsigned long MAINS_SAMPLE_MS  = 50;
const unsigned long WAN_POLL_MS      = 15000;
const unsigned long WAN_TCP_TIMEOUT  = 2000;

const char* WAN_TARGET_1 = "8.8.8.8";
const char* WAN_TARGET_2 = "1.1.1.1";
const int   WAN_PORT     = 53;

// ==================== TIMING DEFAULTS ====================
const unsigned long DEFAULT_MAINS_DELAY_MS = 300000;
const unsigned long DEFAULT_WAN_TIMEOUT_MS = 600000;
const unsigned long SETTLE_MS          = 15000;
const unsigned long PROX_POLL_MS       = 15000;
const unsigned long RE_WOL_AFTER_MS    = 120000;
const uint8_t       MAX_WOL            = 5;
const uint8_t       MAX_WEBHOOK_RETRY  = 6;
const unsigned long WEBHOOK_RETRY_MS   = 10000;
const unsigned long WAN_SUSTAIN_MS     = 30000;

// ==================== NVS KEYS ====================
const char* NVS_NS = "ups";
const char* NVS_SD_MAINS  = "sdMains";
const char* NVS_SD_WAN    = "sdWAN";
const char* NVS_SD_MANUAL = "sdManual";
const char* NVS_OFF_DOWN  = "sdManMains";
const char* NVS_MAN_OVR   = "manOvr";
const char* NVS_MAINS_DELAY = "mainsDelay";
const char* NVS_WAN_TIMEOUT = "wanTimeout";
const char* NVS_SEQ         = "seq";

// ==================== STATE ====================
bool sdMains      = false;
bool sdWAN        = false;
bool sdManual     = false;
bool manualOffWhileMainsDown = false;
bool manualOverride          = false;

unsigned long mainsDelayMs = DEFAULT_MAINS_DELAY_MS;
unsigned long wanTimeoutMs = DEFAULT_WAN_TIMEOUT_MS;

bool mainsDownNotified = false;
unsigned long mainsFailSince = 0;
bool wanDownNotified = false;
unsigned long wanFailSince = 0;
unsigned long wanUpSince = 0;
unsigned long espBootTime = 0;

// counters
uint32_t blipCount = 0;
uint32_t mainsDownCount = 0;
uint32_t shutdownCount = 0;
uint32_t wolRexmitCount = 0;
uint32_t wakeFailedCount = 0;

// ==================== CACHED SENSOR STATE (core 0 → core 1) ====================
volatile bool cachedMainsUp = true;
volatile bool cachedWanUp   = true;
volatile int  cachedMainsRaw = HIGH;
volatile unsigned long mainsStableSince = 0;
volatile int  pendingBlips  = 0;
portMUX_TYPE  cacheMux      = portMUX_INITIALIZER_UNLOCKED;

// ==================== GPIO TEST OVERRIDE ====================
int gpioTestOverride = -1;

// ==================== EVENT LEDGER ====================
const uint8_t RING_SIZE = 32;
struct LedgerEvent {
  uint32_t seq;
  uint32_t uptimeMs;
  char event[24];
  char data[48];
};
LedgerEvent ring[RING_SIZE];
uint8_t ringHead = 0;
bool ringFull = false;
uint32_t evSeq = 0;

// ==================== SHUTDOWN / WAKE STATE ====================
enum ShdPhase { SHD_IDLE, SHD_SENDING, SHD_GIVEUP };
ShdPhase sdPhase = SHD_IDLE;
uint8_t sdRetry = 0;
unsigned long sdLastTry = 0;

enum WakePhase { WK_IDLE, WK_SETTLING, WK_POLLING, WK_FAILED };
WakePhase wakePhase = WK_IDLE;
unsigned long wakeTs = 0;
unsigned long lastLiveness = 0;
unsigned long lastReWol = 0;
uint8_t wolAttempts = 0;
bool wakeFailedEmitted = false;

// ==================== DEFERRED COMMANDS ====================
enum Cmd { CMD_NONE, CMD_WAKE, CMD_SHUTDOWN, CMD_MAINSDELAY, CMD_WAN_TIMEOUT, CMD_GPIO_TEST };
Cmd pendingCmd = CMD_NONE;
long pendingMinutes = 0;
int  pendingGpioVal = -1;

// ==================== OBJECTS ====================
WiFiUDP udp;
WebServer server(80);
TaskHandle_t mainsTaskH = NULL;
TaskHandle_t wanTaskH   = NULL;

// ==================== FORWARD DECLARATIONS ====================
uint32_t addEvent(const char* evt, const char* data);
void notifyPi(const char* evt, uint32_t seq);
void saveState();
void clearFlags();
void executeShutdown(const char* mode);
void startWake();
void sendWolBurst();
void handleShutdownProgress(unsigned long now);
void handleWakeProgress(unsigned long now);
bool restoreConditionsMet();
bool nodeLivenessOk();
bool tcpCheck(const char* host, int port, unsigned long timeoutMs);
void handleWifiReconnect();
void handlePendingCommand();
void handleGetState();
void handleGetEvents();
void handlePostCommand();

// ==================== NVS PERSISTENCE ====================
void saveState() {
  Preferences p; p.begin(NVS_NS, false);
  p.putBool(NVS_SD_MAINS,  sdMains);
  p.putBool(NVS_SD_WAN,    sdWAN);
  p.putBool(NVS_SD_MANUAL, sdManual);
  p.putBool(NVS_OFF_DOWN,  manualOffWhileMainsDown);
  p.putBool(NVS_MAN_OVR,   manualOverride);
  p.putULong(NVS_MAINS_DELAY, mainsDelayMs);
  p.putULong(NVS_WAN_TIMEOUT, wanTimeoutMs);
  p.end();
}

void loadState() {
  Preferences p; p.begin(NVS_NS, true);
  sdMains      = p.getBool(NVS_SD_MAINS,  false);
  sdWAN        = p.getBool(NVS_SD_WAN,    false);
  sdManual     = p.getBool(NVS_SD_MANUAL, false);
  manualOffWhileMainsDown = p.getBool(NVS_OFF_DOWN, false);
  manualOverride          = p.getBool(NVS_MAN_OVR,  false);
  unsigned long saved = p.getULong(NVS_MAINS_DELAY, 0);
  if (saved >= 60000UL && saved <= 720UL * 60000UL) mainsDelayMs = saved;
  saved = p.getULong(NVS_WAN_TIMEOUT, 0);
  if (saved >= 300000UL && saved <= 120UL * 60000UL) wanTimeoutMs = saved;
  evSeq = p.getULong(NVS_SEQ, 0);
  p.end();
}

void persistSeq() {
  Preferences p; p.begin(NVS_NS, false);
  p.putULong(NVS_SEQ, evSeq);
  p.end();
}

void clearFlags() {
  sdMains = false;
  sdWAN   = false;
  sdManual = false;
  manualOffWhileMainsDown = false;
  saveState();
}

void resetFailureWindows() {
  mainsDownNotified = false;
  mainsFailSince = 0;
  wanDownNotified = false;
  wanFailSince = 0;
}

// ==================== EVENT LEDGER ====================
uint32_t addEvent(const char* evt, const char* data) {
  evSeq++;
  uint8_t i = ringHead;
  ring[i].seq = evSeq;
  ring[i].uptimeMs = millis();
  snprintf(ring[i].event, sizeof(ring[i].event), "%s", evt);
  snprintf(ring[i].data,  sizeof(ring[i].data),  "%s", data ? data : "");
  ringHead = (ringHead + 1) % RING_SIZE;
  if (ringHead == 0) ringFull = true;
  persistSeq();
  notifyPi(evt, evSeq);
  return evSeq;
}

void addEvent(const char* evt) {
  addEvent(evt, "");
}

// ==================== NOTIFY PI (fast-path nudge) ====================
void notifyPi(const char* evt, uint32_t seq) {
  if (WiFi.status() != WL_CONNECTED) return;
  WiFiClient c;
  HTTPClient http;
  String url = String(PI_NOTIFY_URL) + "?event=" + evt + "&seq=" + String(seq) + "&token=" + NOTIFY_TOKEN;
  http.begin(c, url);
  http.setTimeout(1500);
  http.GET();
  http.end();
}

// ==================== HELPERS ====================
bool tcpCheck(const char* host, int port, unsigned long timeoutMs) {
  WiFiClient c;
  bool ok = c.connect(host, port, (int64_t)timeoutMs);
  c.stop();
  return ok;
}

bool isNodeDown() {
  return sdMains || sdWAN || sdManual;
}

bool mainsUpNow() {
  bool v;
  portENTER_CRITICAL(&cacheMux); v = cachedMainsUp; portEXIT_CRITICAL(&cacheMux);
  return v;
}

bool wanUpNow() {
  bool v;
  portENTER_CRITICAL(&cacheMux); v = cachedWanUp; portEXIT_CRITICAL(&cacheMux);
  return v;
}

String resetReasonStr() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "poweron";
    case ESP_RST_SW:       return "software";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:  return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT:      return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO:     return "sdio";
    default:               return "unknown";
  }
}

// ==================== WOL ====================
void sendMagicPacket(WiFiUDP& u) {
  byte mac[6];
  int m[6];
  sscanf(NODE_MAC, "%x:%x:%x:%x:%x:%x", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]);
  for (int i = 0; i < 6; i++) mac[i] = (byte)m[i];
  byte pkt[102];
  for (int i = 0; i < 6; i++) pkt[i] = 0xFF;
  for (int i = 1; i <= 16; i++)
    for (int j = 0; j < 6; j++) pkt[i * 6 + j] = mac[j];
  u.beginPacket(WOL_BCAST, 9);
  u.write(pkt, 102);
  u.endPacket();
}

void sendWolBurst() {
  udp.begin(9);
  for (int i = 0; i < 3; i++) {
    sendMagicPacket(udp);
    if (i < 2) {
      unsigned long t = millis();
      while (millis() - t < 1000) {
        ArduinoOTA.handle();
        server.handleClient();
        delay(10);
      }
    }
  }
  udp.stop();
}

// ==================== LIVENESS ====================
bool nodeLivenessOk() {
  IPAddress ip;
  ip.fromString(PROXMOX_IP);
  if (!Ping.ping(ip, 1)) return false;
  WiFiClient c;
  HTTPClient http;
  http.begin(c, "http://" + String(PROXMOX_IP) + ":" + String(PROXMOX_API_PORT) + "/");
  http.setTimeout(2000);
  int code = http.GET();
  http.end();
  return code > 0;
}

// ==================== RESTORE CONDITIONS ====================
bool restoreConditionsMet() {
  if (sdManual && !manualOffWhileMainsDown) return false;
  if (sdMains && !mainsUpNow()) return false;
  if (sdWAN && !(wanUpNow() && wanUpSince != 0 && (millis() - wanUpSince >= WAN_SUSTAIN_MS))) return false;
  if (manualOffWhileMainsDown && !mainsUpNow()) return false;
  return true;
}

// ==================== SHUTDOWN ====================
void executeShutdown(const char* mode) {
  manualOverride = false;
  if (strcmp(mode, "mains") == 0) {
    sdMains = true;
    if (!wanUpNow()) sdWAN = true;
    addEvent("shutdown_mains_start");
  } else if (strcmp(mode, "wan") == 0) {
    sdWAN = true;
    if (!mainsUpNow()) sdMains = true;
    addEvent("shutdown_wan_start");
  } else {
    sdManual = true;
    manualOffWhileMainsDown = !mainsUpNow();
    addEvent("shutdown_manual_start");
  }
  shutdownCount++;
  saveState();
  sdPhase = SHD_SENDING;
  sdRetry = 0;
  sdLastTry = 0;
}

void handleShutdownProgress(unsigned long now) {
  if (sdPhase == SHD_IDLE || sdPhase == SHD_GIVEUP) return;
  if (WiFi.status() != WL_CONNECTED) return;
  if (now - sdLastTry < WEBHOOK_RETRY_MS) return;
  sdLastTry = now;
  WiFiClient c;
  HTTPClient http;
  http.begin(c, SHUTDOWN_URL);
  http.setTimeout(2000);
  int code = http.GET();
  http.end();
  if (code == 200) {
    addEvent("shutdown_webhook_ok");
    addEvent("shutdown_complete");
    sdPhase = SHD_IDLE;
  } else {
    sdRetry++;
    addEvent("shutdown_webhook_failed", ("attempt=" + String(sdRetry)).c_str());
    if (sdRetry >= MAX_WEBHOOK_RETRY) {
      sdPhase = SHD_GIVEUP;
      addEvent("webhook_gave_up");
    }
  }
}

// ==================== WAKE ====================
void startWake() {
  wakePhase = WK_SETTLING;
  wakeTs = millis();
  wolAttempts = 0;
  wakeFailedEmitted = false;
  addEvent("wake_sequence_start");
}

void handleWakeProgress(unsigned long now) {
  switch (wakePhase) {
    case WK_SETTLING:
      if (now - wakeTs >= SETTLE_MS) {
        sendWolBurst();
        wakePhase = WK_POLLING;
        wakeTs = now;
        lastLiveness = now;
        lastReWol = now;
      }
      break;
    case WK_POLLING:
      if (now - lastLiveness >= PROX_POLL_MS) {
        lastLiveness = now;
        if (nodeLivenessOk()) {
          addEvent("online_confirmed");
          clearFlags();
          resetFailureWindows();
          wakePhase = WK_IDLE;
          wolAttempts = 0;
        } else if (wolAttempts < MAX_WOL && now - lastReWol >= RE_WOL_AFTER_MS) {
          wolAttempts++;
          wolRexmitCount++;
          addEvent("wol_rexmitted", ("attempt=" + String(wolAttempts) + "/" + String(MAX_WOL)).c_str());
          sendWolBurst();
          lastReWol = now;
        } else if (wolAttempts >= MAX_WOL && !wakeFailedEmitted) {
          wakeFailedEmitted = true;
          wakeFailedCount++;
          addEvent("wake_failed");
          wakePhase = WK_FAILED;
        }
      }
      break;
    case WK_FAILED:
      if (now - lastLiveness >= PROX_POLL_MS) {
        lastLiveness = now;
        if (nodeLivenessOk()) {
          addEvent("online_confirmed");
          clearFlags();
          resetFailureWindows();
          wakePhase = WK_IDLE;
        }
      }
      break;
    default:
      break;
  }
}

// ==================== BACKGROUND TASK: MAINS GPIO (core 0) ====================
void mainsCheckTask(void* pv) {
  esp_task_wdt_add(NULL);
  TickType_t lastWake = xTaskGetTickCount();
  int raw = digitalRead(MAINS_SENSE_PIN);
  int stableLevel = raw;
  unsigned long levelSince = millis();
  bool highActive = (raw == HIGH);
  unsigned long highAt = highActive ? levelSince : 0;
  for (;;) {
    esp_task_wdt_reset();
    unsigned long now = millis();
    int r;
    portENTER_CRITICAL(&cacheMux);
    r = (gpioTestOverride >= 0) ? gpioTestOverride : digitalRead(MAINS_SENSE_PIN);
    cachedMainsRaw = r;
    portEXIT_CRITICAL(&cacheMux);
    if (r != raw) {
      raw = r;
      levelSince = now;
      if (raw == LOW) {
        if (stableLevel == LOW && highActive && now - highAt < GPIO_STABLE_MS) {
          portENTER_CRITICAL(&cacheMux); pendingBlips++; portEXIT_CRITICAL(&cacheMux);
        }
        highActive = false;
      } else {
        if (!highActive) { highActive = true; highAt = now; }
      }
    }
    if (raw != stableLevel && now - levelSince >= GPIO_STABLE_MS) {
      stableLevel = raw;
      portENTER_CRITICAL(&cacheMux);
      mainsStableSince = now;
      portEXIT_CRITICAL(&cacheMux);
    }
    portENTER_CRITICAL(&cacheMux);
    cachedMainsUp = (stableLevel == LOW);
    portEXIT_CRITICAL(&cacheMux);
    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(MAINS_SAMPLE_MS));
  }
}

// ==================== BACKGROUND TASK: WAN CHECK (core 0) ====================
void wanCheckTask(void* pv) {
  esp_task_wdt_add(NULL);
  TickType_t lastWake = xTaskGetTickCount();
  for (;;) {
    esp_task_wdt_reset();
    bool up = false;
    // Only touch the TCP stack when associated — a blocking connect() while
    // WiFi is down starves the core-0 idle task and panics the interrupt WDT.
    if (WiFi.status() == WL_CONNECTED) {
      up = tcpCheck(WAN_TARGET_1, WAN_PORT, WAN_TCP_TIMEOUT) ||
           tcpCheck(WAN_TARGET_2, WAN_PORT, WAN_TCP_TIMEOUT);
    }
    portENTER_CRITICAL(&cacheMux);
    cachedWanUp = up;
    portEXIT_CRITICAL(&cacheMux);
    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(WAN_POLL_MS));
  }
}

// ==================== WIFI ====================
bool bssidLockActive = true;   // drop the lock if locked connect keeps failing

void wifiConnectOnce(bool locked) {
  if (locked) {
    WiFi.begin(WIFI_SSID, WIFI_PASS, 0, MAIN_ROUTER_BSSID);
  } else {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

void dumpWifiScan() {
  int n = WiFi.scanNetworks();
  Serial.printf("[wifi] scan: %d networks\n", n);
  for (int i = 0; i < n && i < 15; i++) {
    Serial.printf("[wifi]   %-20s rssi=%d ch=%d\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i));
  }
  WiFi.scanDelete();
}

void handleWifiReconnect() {
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long last = 0;
    static unsigned long failSince = 0;
    unsigned long now = millis();
    if (now - last > 5000) {
      last = now;
      if (failSince == 0) failSince = now;
      WiFi.disconnect();
      wifiConnectOnce(bssidLockActive);
      // If locked connect can't associate after ~30s total, the .env BSSID is
      // stale — fall back to plain SSID connect so the sensor still works.
      if (bssidLockActive && (now - failSince >= 30000)) {
        Serial.println("[wifi] locked connect failing — dropping BSSID lock");
        bssidLockActive = false;
        failSince = 0;
      }
    }
  } else {
    bssidLockActive = true;   // re-arm lock once we have a working link
  }
}

// ==================== DEFERRED COMMAND HANDLER ====================
void handlePendingCommand() {
  if (pendingCmd == CMD_NONE) return;
  Cmd cmd = pendingCmd;
  pendingCmd = CMD_NONE;
  switch (cmd) {
    case CMD_WAKE:
      if (wakePhase == WK_SETTLING || wakePhase == WK_POLLING) break;  // already waking — don't reset attempts
      if (isNodeDown()) {
        manualOverride = true;
        clearFlags();
        manualOffWhileMainsDown = false;
        saveState();
        addEvent("manual_on");
        startWake();
      } else if (mainsDownNotified) {
        manualOverride = true;
        saveState();
        addEvent("manual_override", "suppress_mains_shutdown");
      } else {
        // Manual wake with no flags set (e.g. node shut down externally,
        // outside the ESP's shutdown path). Was a silent no-op, so /on
        // appeared to do nothing while the node stayed off.
        addEvent("manual_on");
        startWake();
      }
      break;
    case CMD_SHUTDOWN:
      if (!isNodeDown()) executeShutdown("manual");
      else {
        // Retry path: flags say down but the node is still up (shutdown
        // webhook never acked). Re-arm the webhook without touching flags.
        wakePhase = WK_IDLE;
        sdPhase = SHD_SENDING;
        sdRetry = 0;
        sdLastTry = 0;
      }
      break;
    case CMD_MAINSDELAY: {
      unsigned long newMs = (unsigned long)pendingMinutes * 60000UL;
      if (newMs >= 60000UL && newMs <= 720UL * 60000UL) {
        mainsDelayMs = newMs;
        saveState();
        addEvent("mains_delay_set", (String(pendingMinutes) + "min").c_str());
      }
      break;
    }
    case CMD_WAN_TIMEOUT: {
      unsigned long newMs = (unsigned long)pendingMinutes * 60000UL;
      if (newMs >= 300000UL && newMs <= 120UL * 60000UL) {
        wanTimeoutMs = newMs;
        saveState();
        addEvent("wan_timeout_set", (String(pendingMinutes) + "min").c_str());
      }
      break;
    }
    case CMD_GPIO_TEST:
      gpioTestOverride = pendingGpioVal;
      addEvent("gpio_test", (String("value=") + String(pendingGpioVal)).c_str());
      break;
    default:
      break;
  }
}

// ==================== HTTP HANDLERS ====================
void handleGetState() {
  portENTER_CRITICAL(&cacheMux);
  bool mUp = cachedMainsUp;
  bool wUp = cachedWanUp;
  int raw = cachedMainsRaw;
  int blips = pendingBlips;
  unsigned long stableSince = mainsStableSince;
  portEXIT_CRITICAL(&cacheMux);
  // RSSI is read here (server context, core 1) — never from a background task.
  int rssi = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : -127;
  unsigned long now = millis();
  JsonDocument d;
  d["mainsRaw"] = raw;
  d["mainsUp"] = mUp;
  d["mainsStableSinceMs"] = stableSince ? (now - stableSince) : -1;
  d["wanUp"] = wUp;
  d["sdMains"] = sdMains;
  d["sdWAN"] = sdWAN;
  d["sdManual"] = sdManual;
  d["manualOffMainsDown"] = manualOffWhileMainsDown;
  d["manualOverride"] = manualOverride;
  d["mainsDelayMs"] = mainsDelayMs;
  d["wanTimeoutMs"] = wanTimeoutMs;
  d["mainsFailSinceMs"] = mainsDownNotified ? (now - mainsFailSince) : 0;
  d["wanFailSinceMs"] = wanDownNotified ? (now - wanFailSince) : 0;
  d["espUptimeMs"] = now - espBootTime;
  d["espResetReason"] = resetReasonStr();
  d["freeHeap"] = ESP.getFreeHeap();
  d["rssi"] = rssi;
  JsonObject cnt = d["counters"].to<JsonObject>();
  cnt["blips"] = blipCount + blips;
  cnt["mainsDown"] = mainsDownCount;
  cnt["shutdowns"] = shutdownCount;
  cnt["wolRexmit"] = wolRexmitCount;
  cnt["wakeFailed"] = wakeFailedCount;
  d["wolAttempts"] = wolAttempts;
  d["seq"] = evSeq;
  d["fw"] = FW_VERSION;
  d["wakePhase"] = (int)wakePhase;
  d["sdPhase"] = (int)sdPhase;
  d["gpioTestOverride"] = gpioTestOverride;
  String out;
  serializeJson(d, out);
  server.send(200, "application/json", out);
}

void handleGetEvents() {
  String sinceStr = server.arg("since");
  uint32_t since = 0;
  if (sinceStr.length()) since = (uint32_t)sinceStr.toInt();
  uint8_t count = ringFull ? RING_SIZE : ringHead;
  uint8_t start = ringFull ? ringHead : 0;
  JsonDocument d;
  JsonArray arr = d.to<JsonArray>();
  for (uint8_t i = 0; i < count; i++) {
    uint8_t idx = (start + i) % RING_SIZE;
    if (ring[idx].seq > since) {
      JsonObject e = arr.add<JsonObject>();
      e["seq"] = ring[idx].seq;
      e["event"] = ring[idx].event;
      e["uptimeMs"] = ring[idx].uptimeMs;
      e["data"] = ring[idx].data;
    }
  }
  String out;
  serializeJson(arr, out);
  server.send(200, "application/json", out);
}

void handlePostCommand() {
  if (!server.hasArg("plain")) {
    server.send(400, "application/json", "{\"error\":\"body empty\"}");
    return;
  }
  JsonDocument d;
  DeserializationError err = deserializeJson(d, server.arg("plain"));
  if (err) {
    server.send(400, "application/json", "{\"error\":\"invalid JSON\"}");
    return;
  }
  String cmd = d["cmd"] | "";
  if (cmd == "wake") {
    pendingCmd = CMD_WAKE;
  } else if (cmd == "shutdown") {
    pendingCmd = CMD_SHUTDOWN;
  } else if (cmd == "mainsdelay") {
    long mins = d["minutes"] | -1L;
    if (mins >= 1 && mins <= 720) { pendingCmd = CMD_MAINSDELAY; pendingMinutes = mins; }
    else { server.send(400, "application/json", "{\"error\":\"minutes must be 1-720\"}"); return; }
  } else if (cmd == "wantimeout") {
    long mins = d["minutes"] | -1L;
    if (mins >= 5 && mins <= 120) { pendingCmd = CMD_WAN_TIMEOUT; pendingMinutes = mins; }
    else { server.send(400, "application/json", "{\"error\":\"minutes must be 5-120\"}"); return; }
  } else if (cmd == "set_gpio_test") {
    int val = d["value"] | -2;
    if (val >= -1 && val <= 1) { pendingCmd = CMD_GPIO_TEST; pendingGpioVal = val; }
    else { server.send(400, "application/json", "{\"error\":\"value must be -1, 0, or 1\"}"); return; }
  } else {
    server.send(400, "application/json", "{\"error\":\"unknown command\"}");
    return;
  }
  server.send(200, "application/json", "{\"status\":\"pending\"}");
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);
  Serial.println("[boot] setup start");
  pinMode(MAINS_SENSE_PIN, INPUT_PULLUP);
  loadState();
  Serial.printf("[boot] flags s=%d w=%d m=%d off=%d ovr=%d\n",
                sdMains, sdWAN, sdManual, manualOffWhileMainsDown, manualOverride);
  // if flags set at boot, re-issue shutdown webhook (we can't trust it completed
  // before a reboot). Webhook retries will fail harmlessly if node is already off.
  if (isNodeDown()) {
    sdPhase = SHD_SENDING;
    sdRetry = 0;
    sdLastTry = 0;
  }
  esp_task_wdt_init(30, true);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  wifiConnectOnce(bssidLockActive);
  Serial.println("[boot] wifi begin");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(100);
  }
  Serial.printf("[boot] wifi status=%d\n", WiFi.status());
  if (WiFi.status() != WL_CONNECTED) {
    dumpWifiScan();
    bssidLockActive = false;
    wifiConnectOnce(false);
    Serial.println("[boot] retrying without BSSID lock...");
    unsigned long t2 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t2 < 10000) {
      delay(100);
    }
    Serial.printf("[boot] wifi status=%d\n", WiFi.status());
  }
  ArduinoOTA.setHostname("esp32-ups-monitor");
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.begin();
  server.on("/state", HTTP_GET, handleGetState);
  server.on("/events", HTTP_GET, handleGetEvents);
  server.on("/command", HTTP_POST, handlePostCommand);
  server.begin();
  xTaskCreatePinnedToCore(mainsCheckTask, "mainsCheck", 4096, NULL, 2, &mainsTaskH, 0);
  xTaskCreatePinnedToCore(wanCheckTask,   "wanCheck",   4096, NULL, 1, &wanTaskH,   0);
  espBootTime = millis();
  Serial.println("[boot] setup done");
  delay(500);
  addEvent("esp_booted", resetReasonStr().c_str());
  Serial.printf("[boot] events seq=%lu\n", evSeq);
}

// ==================== MAIN LOOP ====================
const unsigned long DECISION_MS = 1000;
unsigned long lastDecision = 0;

void loop() {
  delay(2);
  ArduinoOTA.handle();
  server.handleClient();
  handleWifiReconnect();
  handlePendingCommand();
  unsigned long now = millis();
  if (now - lastDecision < DECISION_MS) return;
  lastDecision = now;
  // read cached state
  portENTER_CRITICAL(&cacheMux);
  bool mainsUp = cachedMainsUp;
  bool wanUp   = cachedWanUp;
  int blips    = pendingBlips;
  pendingBlips = 0;
  portEXIT_CRITICAL(&cacheMux);
  if (wanUp) {
    if (wanUpSince == 0) wanUpSince = now;
  } else {
    wanUpSince = 0;
  }
  // handle ongoing shutdown webhook
  handleShutdownProgress(now);
  // handle ongoing wake sequence
  if (wakePhase != WK_IDLE) {
    handleWakeProgress(now);
    return;
  }
  // if node is down, check for restore conditions
  if (isNodeDown()) {
    if (sdPhase != SHD_SENDING && restoreConditionsMet()) {
      startWake();
    }
    return;
  }
  // drain blips (only when not in outage — single event per batch)
  if (blips > 0) {
    blipCount += blips;
    addEvent("mains_blip", (String(blips) + "x").c_str());
  }
  // MAINS COUNTDOWN
  if (!mainsUp) {
    if (!mainsDownNotified) {
      mainsDownNotified = true;
      mainsFailSince = now;
      mainsDownCount++;
      addEvent("mains_down", ("mins=" + String(mainsDelayMs / 60000UL)).c_str());
    }
    if (!manualOverride && !isNodeDown() && (now - mainsFailSince >= mainsDelayMs)) {
      executeShutdown("mains");
    }
  } else {
    if (mainsDownNotified) {
      unsigned long dur = now - mainsFailSince;
      mainsDownNotified = false;
      mainsFailSince = 0;
      addEvent("mains_restored", ("downtimeMs=" + String(dur)).c_str());
    }
    if (manualOverride) {
      manualOverride = false;
      saveState();
    }
  }
  // WAN COUNTDOWN (only when WiFi is up — otherwise we can't reach the targets)
  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  if (!wanUp) {
    if (wifiOk && !wanDownNotified) {
      wanDownNotified = true;
      wanFailSince = now;
    }
    if (wifiOk && !isNodeDown() && (now - wanFailSince >= wanTimeoutMs)) {
      executeShutdown("wan");
    }
  } else {
    if (wanDownNotified) {
      wanDownNotified = false;
      wanFailSince = 0;
    }
  }
}