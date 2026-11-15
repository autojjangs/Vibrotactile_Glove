#pragma once

// Each glove needs a separate build: LEFT=0, RIGHT=1.
// You can edit these values or provide -D flags at compile time.
#ifndef GLOVE_SIDE
#define GLOVE_SIDE 1
#endif

// ESP32-C3 SuperMini: GPIO3 selected for the cloth contact input.
// Cloth A -> this GPIO; cloth B -> GND. INPUT_PULLUP, active LOW.
// On the 5V/GND/3V3 row, GPIO3 is fifth from the USB end. Verify the printed 3 label.
#ifndef CONTACT_PIN
#define CONTACT_PIN 3
#endif
#ifndef MOTOR_PIN
#define MOTOR_PIN 4
#endif

#define DEBOUNCE_MS 30
#define WATCHDOG_MS 4000
#define MOTOR_ACTIVE_LEVEL HIGH
#define MOTOR_INACTIVE_LEVEL LOW

static_assert(GLOVE_SIDE == 0 || GLOVE_SIDE == 1, "GLOVE_SIDE: LEFT=0, RIGHT=1");
static_assert(CONTACT_PIN >= 0 && CONTACT_PIN <= 21, "Set CONTACT_PIN to your wired ESP32-C3 GPIO");
static_assert(CONTACT_PIN != MOTOR_PIN, "Contact and motor must use different GPIOs");
