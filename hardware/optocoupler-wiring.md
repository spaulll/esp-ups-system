# Optocoupler wiring — mains sense

## As-built (what is actually fitted)

- **Sensor:** ready-made **2-channel PC817 optocoupler module** (bought online,
  used as-supplied — LED current-limit resistor is the vendor-fitted onboard
  one, value not recorded). **Channel 1 in use; channel 2 left disconnected
  on both sides.**
- **Mains source:** generic **5V/1A USB wall charger**, mains-powered. Its 5V
  output drives the module's channel-1 LED side: adapter live → LED on.
- **ESP side:** module channel-1 transistor output → **GPIO 13 (D13)**,
  configured `INPUT_PULLUP`; return to GND. Adapter ON (mains present) →
  GPIO **LOW**; adapter OFF → GPIO **HIGH** (active-LOW convention, matches
  firmware `cachedMainsUp = (stableLevel == LOW)`).
- **Isolation:** provided by the adapter (mains never touches the low-voltage
  side) **plus** the PC817 optical gap. Never wire mains directly.

> ⚠️ **Module jumper check:** many 2-channel boards have an output-side
> pull-up jumper (5V/3V3). If yours has one, set it to **3V3** (or power the
> output side from the ESP32 3V3 pin) — GPIO 13 must never see 5V. Verified
> working on this build: GPIO reads LOW with the adapter plugged in, HIGH
> with it unplugged (see Verification below).

## Locked design (do not regress)

- Mains is sensed **electrically on GPIO 13 only** — never via network
  reachability (v1's TCP probe was the fake-powercut root cause).
- Optocoupler pulls LOW = mains present; firmware 500ms debounce + **3s
  stability rule** absorbs adapter sag on short blips (<3s dips count as
  `blip`, logged, no countdown).
- Unused channel 2 stays disconnected — do not parallel it with channel 1.

## Schematic (logical)

```
Wall mains ──→ [5V/1A USB adapter] ──5V──→ [PC817 module CH1 LED +] 
                                               CH1 LED - ──→ adapter GND
                                               (onboard R limits LED current)

[PC817 CH1 transistor] ──→ GPIO 13 (D13, INPUT_PULLUP)
                       ──→ GND
```

Behavior truth table (firmware-observed via `/state`):

| Adapter | LED | GPIO 13 | `mainsRaw` | `mainsUp` |
|---|---|---|---|---|
| Plugged (mains present) | on | LOW | 0 | true |
| Unplugged (mains down) | off | HIGH (pull-up) | 1 | false |

## Verification (how to prove it works)

1. Adapter plugged in: `curl http://192.168.0.178/state` →
   `mainsRaw:0`, `mainsUp:true`.
2. Unplug adapter: within ~3s → `mainsRaw:1`, `mainsUp:false`; `/diag`
   "Mains … last change" age resets.
3. Re-plug: clean return, no stuck flags. `/status` TEST MODE banner must be
   **absent** (that banner means a `set_gpio_test` override is active and
   real outages are invisible).

## Calibration notes

- **20/20 controlled unplug cycles: passed** — every real cut detected, every
  restore detected, no stuck state.
- Soak observation to date: zero false detections; the 3s rule absorbs
  sub-3s sags (counted as `blip`, no countdown).
- Cheap-charger caveat: no-name 5V adapters can brown out on sags. A sag that
  resets the adapter looks like a short outage — harmless here (false
  *restore* changes nothing; false *down* only starts a cancelable
  countdown), but if blip counts climb, suspect the adapter before the code.

## Photos

*Pending — no build photos attached yet.* Convention when adding:
`hardware/photos/<date>-<view>.jpg` (e.g. `2026-09-11-module-top.jpg`),
referenced from this section.
