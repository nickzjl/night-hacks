#include <Arduino.h>

// ---------- Config ----------
const uint8_t NUM_SERVOS = 6;
const uint8_t SERVO_PINS[NUM_SERVOS] = {5, 6, 9, 10, 11, 12};  // servo 1..6

const uint16_t PULSE_POS0_US = 544;    // horn flat   (0 degrees)
const uint16_t PULSE_POS1_US = 1500;   // horn up     (90 degrees)
const unsigned long FRAME_US = 20000;  // 50 Hz servo frame
const unsigned long TEST_STEP_MS = 600;

// ---------- State ----------
uint16_t pulseUs[NUM_SERVOS];
unsigned long lastFrame = 0;
char token[8];
uint8_t tokenLen = 0;

// ---------- Servo driver ----------
// Sends one pulse to every servo each 20 ms frame. Pulses start together
// and each pin drops when its width elapses (max block ~1.5 ms per frame).
void refreshServos() {
  unsigned long now = micros();
  if (now - lastFrame < FRAME_US) return;
  lastFrame = now;

  bool done[NUM_SERVOS] = {false};
  uint8_t remaining = NUM_SERVOS;

  unsigned long start = micros();
  for (uint8_t i = 0; i < NUM_SERVOS; i++) digitalWrite(SERVO_PINS[i], HIGH);

  while (remaining > 0) {
    unsigned long elapsed = micros() - start;
    for (uint8_t i = 0; i < NUM_SERVOS; i++) {
      if (!done[i] && elapsed >= pulseUs[i]) {
        digitalWrite(SERVO_PINS[i], LOW);
        done[i] = true;
        remaining--;
      }
    }
  }
}

// Delay that keeps servo pulses going
void waitMs(unsigned long ms) {
  unsigned long start = millis();
  while (millis() - start < ms) refreshServos();
}

void setServo(uint8_t index, uint8_t pos) {  // index 0-based
  pulseUs[index] = pos ? PULSE_POS1_US : PULSE_POS0_US;
}

// ---------- Commands ----------
void runTest() {
  Serial.println("TEST START");
  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    setServo(i, 1);
    waitMs(TEST_STEP_MS);
    setServo(i, 0);
    waitMs(TEST_STEP_MS);
    Serial.print("TEST ");
    Serial.print(i + 1);
    Serial.println(" OK");
  }
  Serial.println("TEST DONE");
}

void processToken(const char *t) {
  // Test command
  if ((t[0] == 'T' || t[0] == 't') && (t[1] == 'S' || t[1] == 's') && t[2] == '\0') {
    runTest();
    return;
  }

  // Servo command: <servo number>S<0|1>, e.g. 1S1
  if (t[0] >= '1' && t[0] <= '9' &&
      (t[1] == 'S' || t[1] == 's') &&
      (t[2] == '0' || t[2] == '1') &&
      t[3] == '\0') {
    uint8_t servoNum = t[0] - '0';
    if (servoNum <= NUM_SERVOS) {
      setServo(servoNum - 1, t[2] - '0');
      Serial.print("OK ");
      Serial.println(t);
      return;
    }
  }

  Serial.print("ERR ");
  Serial.println(t);
}

void endToken() {
  if (tokenLen > 0) {
    token[tokenLen] = '\0';
    processToken(token);
    tokenLen = 0;
  }
}

// ---------- Arduino ----------
void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    pinMode(SERVO_PINS[i], OUTPUT);
    digitalWrite(SERVO_PINS[i], LOW);
    setServo(i, 0);  // start at position 0
  }

  Serial.println("READY");
}

void loop() {
  refreshServos();

  while (Serial.available()) {
    char c = Serial.read();

    if (c == ',' || c == ' ' || c == ';' || c == '\n' || c == '\r') {
      endToken();
    } else if (tokenLen < sizeof(token) - 1) {
      token[tokenLen++] = c;
    } else {
      tokenLen = 0;
      Serial.println("ERR TOO_LONG");
    }
  }
}