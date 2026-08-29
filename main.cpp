// ESP firmware/ main.cpp

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiUdp.h>
#include <Preferences.h>
#include <ArduinoOTA.h>
#include <ArduinoJson.h>
#include <WebServer.h>

// ===================== CONFIG =====================
const char* WIFI_SSID    = "__WIFI_SSID__";
const char* WIFI_PASS    = "__WIFI_PASS__";
// --- HARDWARE LOCK: Bound strictly to the Main Router's 2.4GHz BSSID ---
const uint8_t MAIN_ROUTER_BSSID[] = __WIFI_BSSID_BYTES__; 

const char* OTA_PASSWORD = "__OTA_PASSWORD__";
const char* FW_VERSION   = "V6.6";

const char* PING_TARGET  = "192.168.0.2"; // Extender IP for checking mains status
const int   PING_PORT    = 80;
const char* WAN_TARGET_1 = "8.8.8.8";
const char* WAN_TARGET_2 = "1.1.1.1";
const int   WAN_PORT     = 53;

const char* SHUTDOWN_URL = "http://192.168.0.50:9999/shutdown";
const char* BROADCAST_IP = "192.168.0.255";
const char* M900_MAC     = "__MAC__";

// Target Pi notification endpoint
const char* PI_NOTIFY_URL = "http://192.168.0.169:9997/notify";

// --- Polling intervals (DECOUPLED: mains checked far more often than WAN) ---
// Previously a single 15s interval was shared by both mains + WAN checks,
// and since checks themselves could take up to ~7-8s under failure, the real
// gap between mains samples could stretch to ~25s+ — long enough for a
// 10-15s power blip to fall entirely between two samples and never be seen.
const unsigned long MAINS_POLL_INTERVAL_MS   = 3000;    // fast mains sampling
const unsigned long WAN_POLL_INTERVAL_MS     = 15000;   // unchanged cadence for WAN
const unsigned long MAINS_FAILURE_TIMEOUT_MS = 300000;   // 5 min (unchanged)
const unsigned long WAN_FAILURE_TIMEOUT_MS   = 600000;   // 10 min (unchanged)

// Mains flap detection
const int           FLAP_THRESHOLD  = 3;
const unsigned long FLAP_WINDOW_MS = 600000;   // 10 min rolling window
// ==================================================

// --- Persisted flags ---
bool shutdownReasonMains       = false;
bool shutdownReasonWAN         = false;
bool shutdownReasonManual      = false;   // manual /off — blocks auto-restore
bool manualOffWhileMainsDown   = false;   // /off while mains was down — allow auto-restore on mains up
bool manualOverride            = false;   // manual /on while mains down — suppresses mains auto-shutdown

// --- Live state ---
bool wakeExecuted = false; 
bool mainsFailureStarted = false;
bool mainsDownNotified   = false;
bool wanFailureStarted   = false;
unsigned long mainsFirstFailTime = 0;
unsigned long wanFirstFailTime   = 0;
unsigned long espBootTime        = 0;
unsigned long shutdownIssuedAt   = 0;   // when shutdown_complete last fired

// --- Settle timing ---
const unsigned long MIN_SHUTDOWN_SETTLE_MS = 45000;  // min wait before wake-eligible

// --- Cached sensor state (updated by background tasks only) ---
bool cachedMainsUp = false;
bool cachedWanUp   = false;

// --- Flap detection ---
unsigned long flapTimestamps[10];
int  flapCount  = 0;
bool flapWarned = false;

// --- Command Deferral Flag ---
String pendingCommand = "";
long pendingCustomDelayMin = -1;

// --- Runtime-adjustable timeouts ---
// Mains failure timeout can be changed at runtime via the Pi (/custom-delay
// -> POST /command). Defaults to MAINS_FAILURE_TIMEOUT_MS, persisted in NVS.
unsigned long mainsFailureTimeoutMs = MAINS_FAILURE_TIMEOUT_MS;

WiFiUDP udp;
WebServer server(80);

// --- Background network-check tasks (run on core 0, parallel to loop()/server on core 1) ---
// Mains and WAN are now checked by TWO SEPARATE tasks on their own schedules,
// so a slow/failing WAN check can never delay how often mains is sampled.
TaskHandle_t mainsCheckTaskHandle = NULL;
TaskHandle_t wanCheckTaskHandle   = NULL;
portMUX_TYPE cacheMux = portMUX_INITIALIZER_UNLOCKED; // guards cachedMainsUp/cachedWanUp

// ==================================================
// PERSIST
// ==================================================

void saveState() {
    Preferences prefs;
    prefs.begin("ups", false);
    prefs.putBool("sdMains",      shutdownReasonMains);
    prefs.putBool("sdWAN",        shutdownReasonWAN);
    prefs.putBool("sdManual",     shutdownReasonManual);
    prefs.putBool("sdManMains",   manualOffWhileMainsDown);
    prefs.putBool("manOvr",       manualOverride);
    prefs.putULong("mainsDelay",  mainsFailureTimeoutMs);
    prefs.end();
}

void loadState() {
    Preferences prefs;
    prefs.begin("ups", true);
    shutdownReasonMains     = prefs.getBool("sdMains",    false);
    shutdownReasonWAN       = prefs.getBool("sdWAN",      false);
    shutdownReasonManual    = prefs.getBool("sdManual",   false);
    manualOffWhileMainsDown = prefs.getBool("sdManMains", false);
    manualOverride          = prefs.getBool("manOvr",     false);
    unsigned long savedDelay = prefs.getULong("mainsDelay", 0);
    if (savedDelay >= 60000UL && savedDelay <= 720UL * 60000UL) {
        mainsFailureTimeoutMs = savedDelay;
    }
    prefs.end();

    // if we're booting up already marked as "shut down" (e.g. ESP32
    // itself rebooted mid-window), restart the settle timer from now rather
    // than trusting a pre-reboot millis() value or defaulting to 0.
    // (A persisted millis() stamp is meaningless across reboots, so the old
    // "sdIssuedAt" NVS key was removed entirely.)
    if (shutdownReasonMains || shutdownReasonWAN || shutdownReasonManual) {
        shutdownIssuedAt = millis();
    }
}

// ==================================================
// HELPERS
// ==================================================

void notifyPi(String eventType, String extra = "") {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClient client;
    HTTPClient http;
    String url = String(PI_NOTIFY_URL) + "?event=" + eventType;
    if (extra.length()) url += "&" + extra;
    http.begin(client, url);
    http.setTimeout(2000);
    http.GET();
    http.end();
}

bool tcpCheck(const char* host, int port, int timeoutMs = 2000) {
    WiFiClient client;
    // Use the connect(host, port, timeout_ms) overload — setTimeout() only
    // affects socket READS, not the TCP connect handshake, so the old code
    // could block for the core's default connect timeout (several seconds)
    // instead of the intended 800ms/2000ms budget.
    bool result = client.connect(host, port, (int64_t)timeoutMs);
    client.stop();
    return result;
}

// --- Mains check: fast single-attempt, no internal retry loop ---
// Previously this retried up to 3x with 500ms delays between attempts
// (up to ~7.5s worst case per call). That made mains sampling slow to run,
// which is exactly what caused the shared 15s task to drift and miss short
// blips. Retry/confidence is now handled by the FAST POLL RATE instead
// (MAINS_POLL_INTERVAL_MS = 3s) — a single missed sample gets corrected by
// another sample 3 seconds later, rather than spending 7.5s trying to be
// sure on any single sample.
bool isMainsUp() {
    return tcpCheck(PING_TARGET, PING_PORT, 800);
}

bool isWANUp() {
    if (tcpCheck(WAN_TARGET_1, WAN_PORT, 2000)) return true;
    if (tcpCheck(WAN_TARGET_2, WAN_PORT, 2000)) return true;
    return false;
}

bool isM900ShutDown() {
    return shutdownReasonMains || shutdownReasonWAN || shutdownReasonManual;
}

// ==================================================
// BACKGROUND NETWORK-CHECK TASKS (core 0)
// ==================================================
// Split into two independent tasks so mains sampling is never delayed by a
// slow/failing WAN check (or vice versa). Each writes its own cached value
// under the same spinlock. Fixed-rate scheduling (vTaskDelayUntil) keeps the
// polling interval accurate even if a given check takes a little time,
// instead of stacking check-time on top of the wait like the old code did.

void mainsCheckTask(void *pvParameters) {
    TickType_t lastWake = xTaskGetTickCount();
    for (;;) {
        bool mainsUp = isMainsUp();

        portENTER_CRITICAL(&cacheMux);
        cachedMainsUp = mainsUp;
        portEXIT_CRITICAL(&cacheMux);

        vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(MAINS_POLL_INTERVAL_MS));
    }
}

void wanCheckTask(void *pvParameters) {
    TickType_t lastWake = xTaskGetTickCount();
    for (;;) {
        bool wanUp = isWANUp();

        portENTER_CRITICAL(&cacheMux);
        cachedWanUp = wanUp;
        portEXIT_CRITICAL(&cacheMux);

        vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(WAN_POLL_INTERVAL_MS));
    }
}

void sendNativeWOL(const char* macStr) {
    byte mac[6];
    int m[6];
    sscanf(macStr, "%x:%x:%x:%x:%x:%x", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]);
    for (int i = 0; i < 6; i++) mac[i] = (byte)m[i];
    byte magicPacket[102];
    for (int i = 0; i < 6; i++) magicPacket[i] = 0xFF;
    for (int i = 1; i <= 16; i++)
        for (int j = 0; j < 6; j++) magicPacket[i * 6 + j] = mac[j];
    udp.beginPacket(BROADCAST_IP, 9);
    udp.write(magicPacket, 102);
    udp.endPacket();
}

// ==================================================
// FLAP DETECTION
// ==================================================

void recordMainsFlap() {
    unsigned long now = millis();

    if (flapCount < 10) {
        flapTimestamps[flapCount++] = now;
    } else {
        for (int i = 0; i < 9; i++) flapTimestamps[i] = flapTimestamps[i + 1];
        flapTimestamps[9] = now;
    }

    int recentFlaps = 0;
    for (int i = 0; i < flapCount; i++) {
        if ((now - flapTimestamps[i]) <= FLAP_WINDOW_MS) recentFlaps++;
    }

    Serial.printf("Mains flap recorded — %d flaps in last 10 min\n", recentFlaps);

    if (recentFlaps >= FLAP_THRESHOLD && !flapWarned) {
        flapWarned = true;
        notifyPi("power_instability");
    }
}

void checkFlapReset() {
    if (flapCount == 0) return;
    unsigned long now = millis();
    int validFlaps = 0;
    
    // Shift unexpired flaps to the front of the array
    for (int i = 0; i < flapCount; i++) {
        if ((now - flapTimestamps[i]) <= FLAP_WINDOW_MS) {
            flapTimestamps[validFlaps] = flapTimestamps[i];
            validFlaps++;
        }
    }
    
    if (flapCount != validFlaps) {
        flapCount = validFlaps;
        if (flapCount < FLAP_THRESHOLD) flapWarned = false; // Clear warning lock if we drop below threshold
        Serial.println("Old flaps expired — array compacted");
    }
}

// ==================================================
// SHUTDOWN & WAKE EXECUTION
// ==================================================

void executeShutdownProxmox(String mode) {
    if (mode != "manual" && isM900ShutDown()) {
        return;
    }

    if (mode == "mains") {
        shutdownReasonMains = true;
        notifyPi("shutdown_mains_start");
    } else if (mode == "wan") {
        shutdownReasonWAN = true;
        notifyPi("shutdown_wan_start");
    } else { // manual
        shutdownReasonManual = true;
        portENTER_CRITICAL(&cacheMux);
        bool mainsUpNow = cachedMainsUp;
        portEXIT_CRITICAL(&cacheMux);
        if (!mainsUpNow) {
            manualOffWhileMainsDown = true;
            notifyPi("shutdown_manual_mains_down");
        } else {
            manualOffWhileMainsDown = false;
            notifyPi("shutdown_manual_normal");
        }
    }

    manualOverride = false;
    saveState();

    // Fire Proxmox Hook — tighter timeout, pump server immediately after
    WiFiClient client;                // explicit client: avoids HTTPClient reuse crash
    HTTPClient http;
    http.begin(client, SHUTDOWN_URL);
    http.setTimeout(1000);
    http.GET();
    http.end();
    ArduinoOTA.handle();
    server.handleClient();

    notifyPi("shutdown_complete");
    shutdownIssuedAt = millis();  
}

void executeWakeProxmox(String reason) {
    notifyPi("restoring_network_stabilization");
    
    // Non-blocking 15-second delay replacement inside main flow
    unsigned long waitStart = millis();
    while (millis() - waitStart < 15000) {
        ArduinoOTA.handle();
        server.handleClient();
        delay(10);
    }

    udp.begin(9);
    sendNativeWOL(M900_MAC);
    udp.stop();

    notifyPi("wol_packet_sent");
    
    wakeExecuted = true;
    shutdownReasonMains     = false;
    shutdownReasonWAN       = false;
    shutdownReasonManual    = false;
    manualOffWhileMainsDown = false;
    saveState();
}

// ==================================================
// HTTP WEB API HANDLERS
// ==================================================

void handleGetState() {
    JsonDocument doc;

    // Read the cached sensor values under the same spinlock the writer
    // tasks use (loop() does the same — keep this consistent).
    portENTER_CRITICAL(&cacheMux);
    bool mainsUpCached = cachedMainsUp;
    bool wanUpCached   = cachedWanUp;
    portEXIT_CRITICAL(&cacheMux);

    doc["mainsUp"] = mainsUpCached;
    doc["wanUp"]   = wanUpCached;
    doc["sdMains"] = shutdownReasonMains;
    doc["sdWAN"] = shutdownReasonWAN;
    doc["sdManual"] = shutdownReasonManual;
    doc["manualOffMainsDown"] = manualOffWhileMainsDown;
    doc["manualOverride"] = manualOverride;
    
    // Calculate current rolling window flaps
    unsigned long now = millis();
    int recentFlaps = 0;
    for (int i = 0; i < flapCount; i++) {
        if ((now - flapTimestamps[i]) <= FLAP_WINDOW_MS) recentFlaps++;
    }
    doc["recentFlaps"] = recentFlaps;
    
    doc["mainsFailSinceMs"] = (mainsFailureStarted && mainsFirstFailTime > 0) ? (now - mainsFirstFailTime) : 0;
    doc["wanFailSinceMs"] = (wanFailureStarted && wanFirstFailTime > 0) ? (now - wanFirstFailTime) : 0;
    doc["espUptimeMs"] = millis() - espBootTime;
    doc["rssi"] = WiFi.RSSI();
    doc["freeHeap"] = ESP.getFreeHeap();
    doc["fw"] = FW_VERSION;
    doc["mainsDelayMs"] = mainsFailureTimeoutMs;

    String response;
    serializeJson(doc, response);
    server.send(200, "application/json", response);
}

void handlePostCommand() {
    if (server.hasArg("plain") == false) {
        server.send(400, "application/json", "{\"error\":\"Body empty\"}");
        return;
    }
    
    JsonDocument doc;
    DeserializationError error = deserializeJson(doc, server.arg("plain"));
    if (error) {
        server.send(400, "application/json", "{\"error\":\"Invalid JSON\"}");
        return;
    }

    String cmd = doc["cmd"] | "";
    if (cmd == "wake" || cmd == "shutdown") {
        pendingCommand = cmd; // Defers processing to main loop, avoiding stack re-entrancy crashes
        server.send(200, "application/json", "{\"status\":\"pending\"}");
    } else if (cmd == "custom_delay") {
        long mins = doc["minutes"] | -1L;
        if (mins >= 1 && mins <= 720) {
            pendingCustomDelayMin = mins;
            pendingCommand = "custom_delay"; // Deferred like the others — NVS write stays out of server context
            server.send(200, "application/json", "{\"status\":\"pending\"}");
        } else {
            server.send(400, "application/json", "{\"error\":\"minutes must be 1-720\"}");
        }
    } else {
        server.send(400, "application/json", "{\"error\":\"Unknown command\"}");
    }
}

// ==================================================
// SETUP
// ==================================================

void setup() {
    Serial.begin(115200);
    loadState();

    // --- UPDATED WI-FI INITIALIZATION LAYER ---
    Serial.println("Connecting explicitly to Main Router BSSID...");
    
    // Pass the SSID, Password, Channel (0/passed over), and the explicit hardware MAC array
    WiFi.begin(WIFI_SSID, WIFI_PASS, 0, MAIN_ROUTER_BSSID);
    
    while (WiFi.status() != WL_CONNECTED) { 
        delay(500); 
        Serial.print("."); 
    }
    Serial.println("\nWiFi locked to Main Router! IP: " + WiFi.localIP().toString());

    // Setup OTA
    ArduinoOTA.setHostname("esp32-ups-monitor"); 
    ArduinoOTA.setPassword(OTA_PASSWORD); 
    ArduinoOTA.begin();

    // API Routes Setup
    server.on("/state", HTTP_GET, handleGetState); 
    server.on("/command", HTTP_POST, handlePostCommand); 
    server.begin();

    // Launch mains + WAN checks as two SEPARATE tasks on core 0, parallel to
    // loop()/server on core 1. Mains polls every 3s; WAN polls every 15s.
    // Splitting them means a slow WAN check can never delay a mains sample.
    xTaskCreatePinnedToCore(
        mainsCheckTask,
        "mainsCheckTask",
        4096,
        NULL,
        2,
        &mainsCheckTaskHandle,
        0
    );

    xTaskCreatePinnedToCore(
        wanCheckTask,
        "wanCheckTask",
        4096,
        NULL,
        1,
        &wanCheckTaskHandle,
        0
    );

    espBootTime = millis(); 
    delay(1000); 
    notifyPi("esp_booted");
}

// ==================================================
// MAIN LOOP
// ==================================================
// Runs its own decision logic on a 3s cadence (matching the new fast mains
// poll rate) instead of the old shared 15s cadence, so a blip that the
// background task now catches isn't sat on for up to 15s before loop()
// even looks at it.
const unsigned long LOOP_DECISION_INTERVAL_MS = 3000;
unsigned long lastDecisionTime = 0;

void loop() {
    delay(2); // Yields core execution to RTOS background tasks

    ArduinoOTA.handle();
    server.handleClient();

    // Non-blocking WiFi reconnect
    if (WiFi.status() != WL_CONNECTED) {
        static unsigned long lastReconnectAttempt = 0;
        unsigned long now2 = millis();
        if (now2 - lastReconnectAttempt > 5000) {
            lastReconnectAttempt = now2;
            WiFi.disconnect();
            WiFi.begin(WIFI_SSID, WIFI_PASS, 0, MAIN_ROUTER_BSSID);
        }
        return;
    }

    // Handle Deferred Commands safely outside Server context
    if (pendingCommand != "") {
        String executeCmd = pendingCommand;
        pendingCommand = ""; // clear flag immediately
        if (executeCmd == "wake") {
            portENTER_CRITICAL(&cacheMux);
            bool mainsUpNow = cachedMainsUp;
            portEXIT_CRITICAL(&cacheMux);
            if (!mainsUpNow) {
                manualOverride = true;
                saveState();
            }
            executeWakeProxmox("Manual request executed via Pi.");
            // Only keep the recovery-suppression latch armed if a mains
            // failure is actually still in progress; otherwise a stale
            // wakeExecuted would swallow the next legitimate flap /
            // mains_false_alarm event after a manual on->off->on cycle.
            if (!mainsFailureStarted) {
                wakeExecuted = false;
            }
        } else if (executeCmd == "shutdown") {
            executeShutdownProxmox("manual");
        } else if (executeCmd == "custom_delay") {
            if (pendingCustomDelayMin > 0) {
                mainsFailureTimeoutMs = (unsigned long)pendingCustomDelayMin * 60000UL;
                saveState();
                Serial.printf("Mains failure timeout set to %ld min\n", pendingCustomDelayMin);
            }
            pendingCustomDelayMin = -1;
        }
    }

    unsigned long now = millis();
    if (now - lastDecisionTime < LOOP_DECISION_INTERVAL_MS) return;
    lastDecisionTime = now;

    // Checks now run on core 0 (mainsCheckTask / wanCheckTask); read the
    // latest cached results here instead of blocking loop()/server directly.
    portENTER_CRITICAL(&cacheMux);
    bool mainsUp = cachedMainsUp;
    bool wanUp   = cachedWanUp;
    portEXIT_CRITICAL(&cacheMux);

    if (mainsUp) checkFlapReset();

    // ==================================================
    // AUTONOMOUS AUTOMATION & FAILSAFE ENGINE
    // ==================================================
    if (isM900ShutDown() && (now - shutdownIssuedAt >= MIN_SHUTDOWN_SETTLE_MS)) {
        // Case 1: Auto-shutdown (mains/WAN) — restore when both back up
        if (!shutdownReasonManual) {
            if (mainsUp && wanUp) {
                executeWakeProxmox("Failsafe triggers: Infrastructure healthy.");
            } else if (wanUp && !mainsUp && shutdownReasonWAN) {
                notifyPi("wan_restored_mains_down_hold");
            }
        // Case 2: Manual /off while mains was DOWN — restore when mains comes back up
        } else if (shutdownReasonManual && manualOffWhileMainsDown) {
            if (mainsUp && wanUp) {
                executeWakeProxmox("Mains restored after deferred manual off.");
            }
        }
    }

    // FAILURE DETECTION — MAINS
    if (!mainsUp) {
        if (!mainsFailureStarted) {
            mainsFailureStarted = true;
            mainsFirstFailTime  = now;
        }
        
        // DEBOUNCE: Only notify Pi if down for > 5 seconds.
        if (!mainsDownNotified && (now - mainsFirstFailTime >= 5000)) {
            mainsDownNotified = true; // <--- Just use the global variable directly
            if (isM900ShutDown()) {
                notifyPi("mains_down_shutdown_suppressed");
            } else if (manualOverride) {
                notifyPi("mains_down_override_active");
            } else {
                notifyPi("mains_down_countdown_start",
                         "mins=" + String(mainsFailureTimeoutMs / 60000UL));
            }
        }

        if (!manualOverride && !isM900ShutDown() && (now - mainsFirstFailTime >= mainsFailureTimeoutMs)) {
            executeShutdownProxmox("mains");
        }
    } else {
        if (mainsFailureStarted) {
            unsigned long failDuration = now - mainsFirstFailTime;
            mainsFailureStarted = false;
            mainsFirstFailTime  = 0;
            
            // Only trigger recovery logic if we actually sent a failure notification
            if (failDuration >= 5000) { 
                if (manualOverride) {
                    manualOverride = false;
                    saveState();
                    notifyPi("mains_restored_override_cleared");
                } else if (!isM900ShutDown() && !wakeExecuted) {
                    recordMainsFlap();
                    notifyPi("mains_false_alarm");
                }
            }
            wakeExecuted = false;
            mainsDownNotified = false; // <--- Reset the global variable cleanly
        }
    }

    // FAILURE DETECTION — WAN
    if (!wanUp) {
        if (!wanFailureStarted) {
            wanFailureStarted = true;
            wanFirstFailTime  = now;
        }
        if (!isM900ShutDown() && (now - wanFirstFailTime >= WAN_FAILURE_TIMEOUT_MS)) {
            executeShutdownProxmox("wan");
        }
    } else {
        if (wanFailureStarted) {
            wanFailureStarted = false;
            wanFirstFailTime  = 0;
        }
    }
}

