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
const char* FW_VERSION           = "V9.0";

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

// ==================== V9 POWER-STABILITY RULES ====================
// Wake-side flutter guard (never boot into unstable power):
const uint8_t   WK_BLIP_LIMIT        = 2;          // >2 flips -> strict mode
const unsigned long WK_STABILIZE_MS      = 300000; // strict: 5 min continuous mains
const unsigned long WK_FLAP_MEMORY_MS    = 1800000; // stay strict for 30 min after flapping
const unsigned long WK_UNSTABLE_ALERT_MS = 3600000; // alert once after 1h stuck waiting
const unsigned long WK_ABORT_DOWN_MS     = 10000;  // abort wake if mains down >10s while waiting
// Shutdown-side: a return is only "real" after this long, and only then
// cancels/resets the countdown (otherwise the timer pauses and resumes):
const unsigned long MAINS_REAL_RETURN_MS = 60000;
// Adaptive post-wake re-shutdown: if mains dies again within 1h of a wake,
// use the shorter delay instead of mainsDelayMs (battery already spent):
const unsigned long ADAPTIVE_SHORT_DELAY_MS = 180000; // 3 min
const unsigned long ADAPTIVE_WINDOW_MS      = 3600000; // within 1h of a wake
// WAN double-check handshake (ESP first-detect + Pi confirm):
// continuous fail for SUSPECT_MS -> "wan_suspect" event (Pi probes from its
// own vantage point). The 10-min shutdown countdown starts only on confirm:
// Pi "wan_confirm" command, or auto-confirm after PI_WAIT_MS of Pi silence
// (safe default — a dead Pi must not wedge us in suspect forever).
const unsigned long WAN_SUSPECT_MS      = 60000;
const unsigned long WAN_PI_WAIT_MS      = 60000;

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
const char* NVS_MAINS_REMAIN = "mainsRemain";
const char* NVS_WAN_REMAIN   = "wanRemain";

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
bool wanDownNotified = false;   // CONFIRMED wan-down: shutdown countdown running
unsigned long wanFailSince = 0;
bool wanSuspect = false;        // ~1min continuous fail, awaiting Pi confirm/clear
unsigned long wanSuspectSince = 0;
unsigned long wanFirstFailSince = 0;  // first-fail stamp for the suspect window
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
bool bootReArmPending = false;   // auto-flags set at boot: probe before webhook

enum WakePhase { WK_IDLE, WK_SETTLING, WK_POLLING, WK_FAILED };
WakePhase wakePhase = WK_IDLE;
unsigned long wakeTs = 0;
unsigned long lastLiveness = 0;
unsigned long lastReWol = 0;
uint8_t wolAttempts = 0;
bool wakeFailedEmitted = false;
// V9 stability state:
uint8_t wakeBlips = 0;          // mains down->up flips counted during a pre-WOL wait
bool wakeStrict = false;        // requires WK_STABILIZE_MS of continuous mains
unsigned long wakeStabilizeUntil = 0;
unsigned long wakeFlappySince = 0;  // 0 = not flappy; else timestamp of first flap
bool wakeUnstableNotified = false;
unsigned long wakeWaitStartedAt = 0;
// shutdown-side pause/prove (real-return rule):
bool mainsReturnSuspect = false;
unsigned long mainsReturnSince = 0;
unsigned long mainsOutageElapsed = 0;      // accumulated down-time ms
unsigned long mainsLastAccumAt = 0;
// adaptive post-wake re-shutdown:
unsigned long lastWakeEndedAt = 0;

unsigned long mainsEffectiveDelayMs() {
  if (lastWakeEndedAt != 0 &&
      (millis() - lastWakeEndedAt < ADAPTIVE_WINDOW_MS)) {
    return ADAPTIVE_SHORT_DELAY_MS;
  }
  return mainsDelayMs;
}
// wake-wait watchdog helpers (V9):
bool wkWaitDownLatched = false;
unsigned long wkWaitDownSince = 0;

// ==================== DEFERRED COMMANDS ====================
enum Cmd { CMD_NONE, CMD_WAKE, CMD_SHUTDOWN, CMD_MAINSDELAY, CMD_WAN_TIMEOUT, CMD_GPIO_TEST, CMD_WAN_CONFIRM, CMD_WAN_CLEAR };
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
void persistCountdowns();
void notifyPi(const char* evt, uint32_t seq);
void saveState();
void clearFlags();
bool isNodeDown();
bool wanUpNow();
void confirmWan(const char* src);
void clearWan(const char* src);
void executeShutdown(const char* mode);
void startWake();
void resetWakeGuard();
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
  // Resurrect an interrupted countdown from its stored remaining time.
  // (Flags set means the shutdown already fired — countdown is moot.)
  unsigned long mr = p.getULong(NVS_MAINS_REMAIN, 0);
  if (mr > 0 && !isNodeDown()) {
    mainsDownNotified = true;
    if (mr <= mainsDelayMs) {
      mainsOutageElapsed = mainsDelayMs - mr;
    } else {
      mainsOutageElapsed = 0;
    }
    mainsLastAccumAt = millis();
    mainsFailSince = millis();
  }
  unsigned long wr = p.getULong(NVS_WAN_REMAIN, 0);
  if (wr > 0 && !isNodeDown()) {
    wanDownNotified = true;
    wanFailSince = millis() - min(wr, wanTimeoutMs);
  }
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

// Persist the countdown's REMAINING time so an ESP reboot mid-outage does
// not restart the clock from zero. Written on transitions and ~every 60s
// while a countdown is live (bounded NVS wear).
void persistCountdowns() {
  Preferences p; p.begin(NVS_NS, false);
  unsigned long now = millis();
  unsigned long mRemain = 0, wRemain = 0;
  if (mainsDownNotified && !isNodeDown()) {
    unsigned long elapsed = mainsOutageElapsed;   // accumulated, paused during suspect
    unsigned long delay = mainsEffectiveDelayMs();
    mRemain = (elapsed >= delay) ? 0 : delay - elapsed;
  }
  if (wanDownNotified && !isNodeDown()) {
    unsigned long elapsed = now - wanFailSince;
    wRemain = (elapsed >= wanTimeoutMs) ? 0 : wanTimeoutMs - elapsed;
  }
  p.putULong(NVS_MAINS_REMAIN, mRemain);
  p.putULong(NVS_WAN_REMAIN, wRemain);
  p.end();
}

void resetFailureWindows() {
  mainsDownNotified = false;
  mainsFailSince = 0;
  mainsReturnSuspect = false;
  mainsReturnSince = 0;
  mainsOutageElapsed = 0;
  mainsLastAccumAt = 0;
  wanDownNotified = false;
  wanFailSince = 0;
  wanSuspect = false;
  wanSuspectSince = 0;
  wanFirstFailSince = 0;
  persistCountdowns();
}

// Latch a CONFIRMED wan-down and start the wanTimeoutMs shutdown countdown.
// Idempotent: a duplicate confirm (Pi re-send after ESP reboot, or the
// auto-confirm racing a late Pi command) is a no-op. A stale confirm that
// arrives after self-recovery just dissolves the suspect state.
void confirmWan(const char* src) {
  if (wanDownNotified) return;
  wanSuspect = false;
  wanSuspectSince = 0;
  if (!wanUpNow()) {
    wanDownNotified = true;
    wanFailSince = millis();
    addEvent("wan_down", src);
    persistCountdowns();
  }
}

// Pi judged the suspect a false alarm (its own probes succeeded): drop all
// WAN fail state, including a raced confirmed countdown — the next failing
// polls re-suspect on their own if the outage is real. Ledger trace only.
void clearWan(const char* src) {
  wanFirstFailSince = 0;
  wanSuspect = false;
  wanSuspectSince = 0;
  wanDownNotified = false;
  wanFailSince = 0;
  addEvent("wan_cleared", src);
  persistCountdowns();
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
  http.setConnectTimeout(1500);   // don't stall the main loop on a dead Pi
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
  // TCP to the API port — NOT plain HTTP against a TLS port (that relied on
  // pveproxy's redirect behaviour; a tcpCheck is robust and honest).
  return tcpCheck(PROXMOX_IP, PROXMOX_API_PORT, 2000);
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
  // A shutdown command cancels any in-flight wake: the loop returns early
  // for non-idle wake phases, so without this the webhook below would
  // never be sent (and online_confirmed could wipe the new flags).
  wakePhase = WK_IDLE;
  wolAttempts = 0;
  wakeFailedEmitted = false;
  resetWakeGuard();
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
  persistCountdowns();   // countdown is moot now — clear stored remainder
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
    // HTTP 200 from the agent means "shutdown initiated", NOT "server off".
    // Proxmox with many LXCs/services takes minutes to power off, so never
    // claim offline here — the Pi's PVE-API verification is the sole
    // offline authority (it emits "Proxmox Confirmed Offline").
    addEvent("shutdown_webhook_ok");
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
void resetWakeGuard() {
  wakeBlips = 0;
  wakeStrict = false;
  wakeStabilizeUntil = 0;
  wakeUnstableNotified = false;
  wakeWaitStartedAt = 0;
  wkWaitDownLatched = false;
  wkWaitDownSince = 0;
}

void startWake() {
  wakePhase = WK_SETTLING;
  wakeTs = millis();
  wolAttempts = 0;
  wakeFailedEmitted = false;
  // No stale shutdown-webhook retry may hit a server that is being woken.
  sdPhase = SHD_IDLE;
  // V9 flutter guard: re-arm strict mode if we are still inside the flap
  // memory window (30 min of quiet power clears it).
  resetWakeGuard();
  wakeWaitStartedAt = millis();
  if (wakeFlappySince && (millis() - wakeFlappySince < WK_FLAP_MEMORY_MS)) {
    wakeStrict = true;
    wakeStabilizeUntil = millis() + WK_STABILIZE_MS;
  } else {
    wakeFlappySince = 0;
  }
  addEvent("wake_sequence_start");
}

void handleWakeProgress(unsigned long now) {
  switch (wakePhase) {
    case WK_SETTLING: {
      // Flap watching: count confirmed down->up flips while we wait.
      if (wkWaitDownLatched) {
        if (mainsUpNow()) {
          wkWaitDownLatched = false;
          wakeBlips++;
          if (wakeBlips > WK_BLIP_LIMIT) {
            wakeStrict = true;
            wakeStabilizeUntil = now + WK_STABILIZE_MS;
            if (wakeFlappySince == 0) wakeFlappySince = now;
          }
        }
      } else if (!mainsUpNow()) {
        wkWaitDownLatched = true;
        wkWaitDownSince = now;
        if (wakeStrict) wakeStabilizeUntil = now + WK_STABILIZE_MS;
      }
      if (wakeStrict) {
        if (!mainsUpNow()) {
          // Persistent outage during the wait: abort, never boot into it.
          if (now - wkWaitDownSince >= WK_ABORT_DOWN_MS) {
            wakePhase = WK_IDLE;
            resetWakeGuard();
            break;
          }
        } else if (now >= wakeStabilizeUntil) {
          sendWolBurst();
          wakePhase = WK_POLLING;
          wakeTs = now;
          lastLiveness = now;
          lastReWol = now;
        }
      } else if (now - wakeTs >= SETTLE_MS) {
        sendWolBurst();
        wakePhase = WK_POLLING;
        wakeTs = now;
        lastLiveness = now;
        lastReWol = now;
      }
      // One calm "power is unstable" message after 1h stuck in the wait.
      if (!wakeUnstableNotified && (now - wakeWaitStartedAt >= WK_UNSTABLE_ALERT_MS)) {
        wakeUnstableNotified = true;
        addEvent("power_unstable");
      }
      break;
    }
    case WK_POLLING:
      if (now - lastLiveness >= PROX_POLL_MS) {
        lastLiveness = now;
        if (nodeLivenessOk()) {
          addEvent("online_confirmed");
          clearFlags();
          resetFailureWindows();
          wakePhase = WK_IDLE;
          wolAttempts = 0;
          lastWakeEndedAt = now;
          resetWakeGuard();
        } else if (wolAttempts < MAX_WOL && now - lastReWol >= RE_WOL_AFTER_MS) {
          wolAttempts++;
          wolRexmitCount++;
          addEvent("wol_rexmitted", ("attempt=" + String(wolAttempts) + "/" + String(MAX_WOL)).c_str());
          sendWolBurst();
          lastReWol = now;
        } else if (wolAttempts >= MAX_WOL && !wakeFailedEmitted &&
                   now - lastReWol >= RE_WOL_AFTER_MS) {
          // Give the final burst the full 120s window too — declaring
          // failure on the very next 15s poll is far too early.
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
          lastWakeEndedAt = now;
          resetWakeGuard();
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
  portENTER_CRITICAL(&cacheMux);
  mainsStableSince = levelSince;
  portEXIT_CRITICAL(&cacheMux);
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
  static unsigned long last = 0;
  static unsigned long failSince = 0;
  if (WiFi.status() != WL_CONNECTED) {
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
    failSince = 0;            // next drop gets a fresh 30s grace window
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
      sdPhase = SHD_IDLE;   // /on cancels any pending shutdown webhook
      if (isNodeDown()) {
        manualOverride = true;
        clearFlags();
        manualOffWhileMainsDown = false;
        saveState();
        addEvent("manual_on");
        startWake();
      } else if (mainsDownNotified || wanDownNotified) {
        // /on during EITHER countdown suspends the auto-shutdown for this
        // outage (mains or WAN). The node is still up — no wake is needed
        // and none must be started.
        manualOverride = true;
        saveState();
        addEvent("manual_override", "suppress_auto_shutdown");
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
    case CMD_WAN_CONFIRM:
      confirmWan("src=pi");
      break;
    case CMD_WAN_CLEAR:
      clearWan("src=pi");
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
  // NB: cast to signed BEFORE serialize — plain `: -1` in the ternary
  // promotes to unsigned long (0xFFFFFFFF = 1193h 2m on the Pi).
  long stableAgeMs = stableSince ? (long)(now - stableSince) : -1L;
  d["mainsStableSinceMs"] = stableAgeMs;
  d["wanUp"] = wUp;
  d["sdMains"] = sdMains;
  d["sdWAN"] = sdWAN;
  d["sdManual"] = sdManual;
  d["manualOffMainsDown"] = manualOffWhileMainsDown;
  d["manualOverride"] = manualOverride;
  d["mainsDelayMs"] = mainsDelayMs;
  d["mainsDelayEffectiveMs"] = mainsEffectiveDelayMs();
  d["wanTimeoutMs"] = wanTimeoutMs;
  d["mainsFailSinceMs"] = mainsDownNotified ? mainsOutageElapsed : 0;
  d["wanFailSinceMs"] = wanDownNotified ? (now - wanFailSince) : 0;
  d["wanSuspect"] = wanSuspect;
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
  } else if (cmd == "wan_confirm") {
    pendingCmd = CMD_WAN_CONFIRM;
  } else if (cmd == "wan_clear") {
    pendingCmd = CMD_WAN_CLEAR;
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
  // If flags were set before this boot, we must NOT blindly re-fire the
  // shutdown webhook: after a total blackout the node's BIOS may already
  // have booted it (it would then receive a SECOND shutdown command right
  // after coming back up). Defer to a post-WiFi liveness probe in loop()
  // (bootReArmPending): node already up -> adopt reality and clear auto
  // flags; node still down -> start the classic webhook retry loop.
  if (isNodeDown()) {
    bootReArmPending = true;
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
  // Boot re-arm probe: only after WiFi is up can we tell whether the node is
  // already alive (BIOS auto-boot after a blackout) or genuinely still down.
  if (bootReArmPending && WiFi.status() == WL_CONNECTED) {
    if (!sdManual && (sdMains || sdWAN) && nodeLivenessOk()) {
      // The node is up on its own — the shutdown either completed pre-reboot
      // or was never needed; adopt reality, never re-issue a webhook.
      bootReArmPending = false;
      clearFlags();
      resetFailureWindows();
      addEvent("online_confirmed");
    } else {
      bootReArmPending = false;
      sdPhase = SHD_SENDING;
      sdRetry = 0;
      sdLastTry = 0;
    }
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
  // MAINS COUNTDOWN (V9: accumulate down-time; a return only counts as real
  // after MAINS_REAL_RETURN_MS — meanwhile the timer is paused, not reset).
  if (mainsDownNotified && !mainsReturnSuspect) {
    if (mainsLastAccumAt == 0) mainsLastAccumAt = now;
    mainsOutageElapsed += (now - mainsLastAccumAt);
    if (mainsOutageElapsed > mainsDelayMs) mainsOutageElapsed = mainsDelayMs;
    mainsLastAccumAt = now;
  }
  if (!mainsUp) {
    if (mainsReturnSuspect) {
      // The candidate return was a lie — resume the countdown where it paused.
      mainsReturnSuspect = false;
      mainsReturnSince = 0;
      mainsLastAccumAt = now;
    } else if (!mainsDownNotified) {
      mainsDownNotified = true;
      mainsFailSince = now;
      mainsDownCount++;
      mainsOutageElapsed = 0;
      mainsLastAccumAt = now;
      addEvent("mains_down", ("mins=" + String(mainsEffectiveDelayMs() / 60000UL)).c_str());
      persistCountdowns();
    }
    if (!manualOverride && !isNodeDown() && !mainsReturnSuspect &&
        (mainsOutageElapsed >= mainsEffectiveDelayMs())) {
      executeShutdown("mains");
    }
  } else {
    if (mainsDownNotified && !mainsReturnSuspect) {
      // Candidate return: pause the timer and prove it for 60s.
      mainsReturnSuspect = true;
      mainsReturnSince = now;
    } else if (mainsReturnSuspect) {
      if (now - mainsReturnSince >= MAINS_REAL_RETURN_MS) {
        unsigned long dur = mainsOutageElapsed;
        mainsDownNotified = false;
        mainsFailSince = 0;
        mainsReturnSuspect = false;
        mainsReturnSince = 0;
        mainsOutageElapsed = 0;
        mainsLastAccumAt = 0;
        addEvent("mains_restored", ("downtimeMs=" + String(dur)).c_str());
        persistCountdowns();
      }
    }
  }
  // Manual override lasts until BOTH causes have cleared: a /on during a
  // mains OR WAN countdown suspends that outage's auto-shutdown. Restoring
  // only one cause is not enough to re-arm the other.
  if (manualOverride && mainsUp && wanUp) {
    manualOverride = false;
    saveState();
  }
  // WAN HANDSHAKE (only when WiFi is up — otherwise we can't reach the targets).
  // Single failed polls are noise: suspect needs WAN_SUSPECT_MS of continuous
  // failure, and the wanTimeoutMs shutdown countdown starts only on confirm
  // (Pi "wan_confirm", or auto-confirm after WAN_PI_WAIT_MS of Pi silence).
  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  if (!wanUp) {
    if (wifiOk) {
      if (wanFirstFailSince == 0) wanFirstFailSince = now;
      if (!wanSuspect && !wanDownNotified &&
          (now - wanFirstFailSince >= WAN_SUSPECT_MS)) {
        wanSuspect = true;
        wanSuspectSince = now;
        addEvent("wan_suspect");
      }
      if (wanSuspect && !wanDownNotified &&
          (now - wanSuspectSince >= WAN_PI_WAIT_MS)) {
        confirmWan("src=auto");
      }
    } else {
      wanFirstFailSince = 0;  // WiFi down proves nothing — don't age the window
    }
    if (wanDownNotified && wifiOk && !isNodeDown() && !manualOverride &&
        (now - wanFailSince >= wanTimeoutMs)) {
      executeShutdown("wan");
    }
  } else {
    wanFirstFailSince = 0;
    if (wanSuspect && !wanDownNotified) {
      // Recovered while suspect-only (pre-confirm): false alarm, no user
      // noise — the Pi drops the matching wan_cleared silently.
      wanSuspect = false;
      wanSuspectSince = 0;
      addEvent("wan_cleared", "src=self");
    }
    if (wanDownNotified) {
      unsigned long dur = now - wanFailSince;
      wanDownNotified = false;
      wanFailSince = 0;
      wanSuspect = false;
      wanSuspectSince = 0;
      addEvent("wan_restored", ("downtimeMs=" + String(dur)).c_str());
      persistCountdowns();
    }
  }
  // Refresh stored countdown remainders ~every 60s while live (bounded NVS
  // writes) so a reboot mid-outage resumes close to the real elapsed time.
  static unsigned long lastCdPersist = 0;
  if ((mainsDownNotified || wanDownNotified) && (now - lastCdPersist >= 60000UL)) {
    lastCdPersist = now;
    persistCountdowns();
  } else if (!mainsDownNotified && !wanDownNotified) {
    lastCdPersist = 0;
  }
}