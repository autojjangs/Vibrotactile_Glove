// ESP32-C3 / Espressif Arduino core 3.3.2, Bluedroid BLE.
// Motor output goes to an existing motor driver/module IN, not a bare motor.
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include "config.h"

static const char* SERVICE_UUID = "e5f90001-730d-4d25-9c25-51ee70b7d820";
static const char* COMMAND_UUID = "e5f90002-730d-4d25-9c25-51ee70b7d820";
static const char* EVENT_UUID = "e5f90003-730d-4d25-9c25-51ee70b7d820";
enum Opcode : uint8_t { STOP = 0, PREVIEW = 1, FEEDBACK = 2, SNAPSHOT = 3, PING = 4 };
enum EventKind : uint8_t { CONTACT = 1, ACK = 2, STATE = 3, HAPTIC = 4, ERROR_EVENT = 5 };

#pragma pack(push, 1)
struct Command {
  uint8_t version;
  uint8_t opcode;
  uint32_t reference;
  uint16_t onMs;
  uint16_t offMs;
  uint16_t count;
};
struct Event {
  uint8_t version;
  uint8_t kind;
  uint8_t side;
  uint8_t value;
  uint32_t sequence;
  uint32_t deviceMs;
  uint32_t reference;
};
#pragma pack(pop)
static_assert(sizeof(Command) == 12, "Command wire size");
static_assert(sizeof(Event) == 16, "Event wire size");

QueueHandle_t commandQueue;
BLECharacteristic* eventCharacteristic = nullptr;
volatile bool connected = false;
volatile bool restartAdvertising = false;
volatile uint8_t callbackError = 0;
bool wasConnected = false;
uint32_t sequence = 0;
uint32_t lastCommandMs = 0;
bool watchdogFired = false;
bool stableContact = false;
bool rawContact = false;
uint32_t rawChangedAt = 0;
uint8_t mode = STOP;
uint32_t patternReference = 0;
uint32_t phaseAt = 0;
uint16_t onMs = 0, offMs = 0, pulseCount = 0, pulsesStarted = 0;
bool motorOn = false;

void emitEvent(uint8_t kind, uint8_t value, uint32_t reference = 0, uint32_t timestamp = 0) {
  if (!connected) return;
  Event event = {1, kind, GLOVE_SIDE, value, ++sequence,
                 timestamp ? timestamp : millis(), reference};
  eventCharacteristic->setValue(reinterpret_cast<uint8_t*>(&event), sizeof(event));
  eventCharacteristic->notify();
  Serial.printf("event=%u side=%u value=%u seq=%lu ms=%lu ref=%lu\n",
                event.kind, event.side, event.value,
                (unsigned long)event.sequence, (unsigned long)event.deviceMs,
                (unsigned long)event.reference);
}

void motor(bool enabled) {
  motorOn = enabled;
  digitalWrite(MOTOR_PIN, enabled ? MOTOR_ACTIVE_LEVEL : MOTOR_INACTIVE_LEVEL);
}

void stopPattern(uint32_t reference, bool report = true) {
  mode = STOP;
  motor(false);
  patternReference = reference;
  if (report) emitEvent(HAPTIC, STOP, reference);
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer*) override { connected = true; }
  void onDisconnect(BLEServer*) override {
    connected = false;
    restartAdvertising = true;
  }
};

class CommandCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    String bytes = characteristic->getValue();
    if (bytes.length() != sizeof(Command)) {
      callbackError = 1;
      return;
    }
    Command command;
    memcpy(&command, bytes.c_str(), sizeof(command));
    if (xQueueSend(commandQueue, &command, 0) != pdTRUE) callbackError = 2;
  }
};

void processCommand(const Command& command) {
  if (command.version != 1 || command.opcode > PING) {
    emitEvent(ERROR_EVENT, 3, command.reference);
    return;
  }
  if ((command.opcode == PREVIEW || command.opcode == FEEDBACK) &&
      (command.onMs == 0 || command.offMs == 0 ||
       command.onMs > 60000 || command.offMs > 60000 ||
       (command.opcode == PREVIEW && (command.count == 0 || command.count > 100)) ||
       (command.opcode == FEEDBACK && command.count != 0))) {
    emitEvent(ERROR_EVENT, 3, command.reference);
    return;
  }
  lastCommandMs = millis();
  watchdogFired = false;
  if (command.opcode == STOP) {
    stopPattern(command.reference);
  } else if (command.opcode == SNAPSHOT) {
    emitEvent(STATE, stableContact, command.reference);
  } else if (command.opcode == PREVIEW || command.opcode == FEEDBACK) {
    // Replacing a pattern immediately cancels all remaining preview pulses.
    mode = command.opcode;
    patternReference = command.reference;
    onMs = command.onMs;
    offMs = command.offMs;
    pulseCount = command.count;
    pulsesStarted = 1;
    motor(true);
    phaseAt = millis();
    emitEvent(HAPTIC, mode, command.reference, phaseAt);
  }
  emitEvent(ACK, command.opcode, command.reference);
}

void updatePattern(uint32_t now) {
  if (mode == STOP) return;
  if (motorOn && uint32_t(now - phaseAt) >= onMs) {
    motor(false);
    phaseAt = now;
    if (mode == PREVIEW && pulsesStarted >= pulseCount) {
      stopPattern(patternReference);
    }
  } else if (!motorOn && uint32_t(now - phaseAt) >= offMs) {
    motor(true);
    ++pulsesStarted;
    phaseAt = now;
  }
}

void updateContact(uint32_t now) {
  bool current = digitalRead(CONTACT_PIN) == LOW;
  if (current != rawContact) {
    rawContact = current;
    rawChangedAt = now;
  }
  if (rawContact != stableContact && uint32_t(now - rawChangedAt) >= DEBOUNCE_MS) {
    stableContact = rawContact;
    // One event per debounced edge. Holding cloth does not repeat presses.
    emitEvent(CONTACT, stableContact, 0, now);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(MOTOR_PIN, OUTPUT);
  motor(false);
  pinMode(CONTACT_PIN, INPUT_PULLUP);
  rawContact = stableContact = digitalRead(CONTACT_PIN) == LOW;
  rawChangedAt = millis();
  commandQueue = xQueueCreate(16, sizeof(Command));
  if (!commandQueue) {
    Serial.println("Failed to allocate command queue");
    while (true) delay(1000);
  }
  BLEDevice::init(GLOVE_SIDE == 0 ? "VibroGlove-Left" : "VibroGlove-Right");
  BLEServer* server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService* service = server->createService(SERVICE_UUID);
  BLECharacteristic* commandCharacteristic = service->createCharacteristic(
      COMMAND_UUID, BLECharacteristic::PROPERTY_WRITE);
  commandCharacteristic->setCallbacks(new CommandCallbacks());
  eventCharacteristic = service->createCharacteristic(
      EVENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  eventCharacteristic->addDescriptor(new BLE2902());
  service->start();
  BLEAdvertising* advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->start();
  Serial.printf("VibroGlove side=%u contactGPIO=%d motorGPIO=%d BLE=%s\n",
                GLOVE_SIDE, CONTACT_PIN, MOTOR_PIN, BLEDevice::getAddress().toString().c_str());
}

void loop() {
  uint32_t now = millis();
  if (connected && !wasConnected) {
    stopPattern(0, false);
    lastCommandMs = now;
    watchdogFired = false;
  }
  if (!connected && wasConnected) {
    stopPattern(0, false);
    xQueueReset(commandQueue);
  }
  wasConnected = connected;
  if (restartAdvertising && !connected) {
    restartAdvertising = false;
    BLEDevice::startAdvertising();
  }
  if (callbackError) {
    uint8_t error = callbackError;
    callbackError = 0;
    emitEvent(ERROR_EVENT, error);
    stopPattern(0);
  }
  Command command;
  while (connected && xQueueReceive(commandQueue, &command, 0) == pdTRUE) {
    processCommand(command);
  }
  now = millis();
  if (connected && !watchdogFired && uint32_t(now - lastCommandMs) > WATCHDOG_MS) {
    watchdogFired = true;
    stopPattern(0);
    emitEvent(ERROR_EVENT, 4);
  }
  updateContact(now);
  updatePattern(now);
  delay(2);
}
