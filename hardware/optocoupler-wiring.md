# Optocoupler wiring — PC817 mains sense

> ⏸ Stub — full schematic, photos and calibration notes land when the part
> arrives (UPGRADE_PLAN Phase 3).

## Locked design (never wire mains directly)

- 5V USB wall adapter (mains-powered) → PC817 LED side via series resistor
- Collector → GPIO 13 (D13) on the ESP32, configured `INPUT_PULLUP`
- Emitter → GND
- Adapter ON (mains present) → GPIO LOW; adapter OFF → GPIO HIGH
- Firmware 3s stability rule absorbs adapter sag on short blips
- Isolation is provided by the adapter — mains never touches the ESP32 side
