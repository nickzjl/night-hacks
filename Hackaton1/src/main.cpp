#include <Arduino.h>
#include <LiquidCrystal.h>

// =====================================================================
//  SERIAL PROTOCOL (end every line with a newline)
//
//  Servos      1S0 .. 6S1      servo number, S, position (0 flat / 1 up)
//  Test        TS              sweep all servos
//  Joystick    JL JR JU JD JP  left, right, up, down, press
//  Screen      L1:text         write text to LCD row 1 (max 16 chars)
//              L2:text         write text to LCD row 2 (max 16 chars)
//              LC              clear the LCD
//              LA              return LCD to automatic (joystick) display
//
//  Mac mode    HN              Lexis backend drives servos + LCD; the joystick
//                              only reports JOY L / JOY R / JOY P / JOY U / JOY D
//              HF              back to the local alphabet mode (also automatic
//                              when the Mac closes the serial port)
//
//  Servo / joystick / TS commands can be chained with commas, spaces,
//  semicolons or newlines.  L1: and L2: take the REST OF THE LINE as
//  their text, so put each one on its own line.
//
//  Replies: OK <cmd> / ERR <cmd>, plus events CHAR, MSG, PLAY START/DONE, JOY
// =====================================================================

// ---------- Servo config ----------
const uint8_t NUM_SERVOS = 6;
const uint8_t SERVO_PINS[NUM_SERVOS] = {5, 6, 9, 10, 11, 12};  // servo 1..6

const uint16_t PULSE_POS0_US = 544;    // horn flat   (0 degrees)
const uint16_t PULSE_POS1_US = 1500;   // horn up     (90 degrees)
const unsigned long FRAME_US = 20000;  // 50 Hz servo frame
const unsigned long TEST_STEP_MS = 600;

// ---------- Joystick config ----------
const uint8_t JOY_X  = A0;   // VRx
const uint8_t JOY_Y  = A1;   // VRy
const uint8_t JOY_SW = A2;   // push button (active LOW)

const bool JOY_ENABLED = true;  // false = ignore the stick (button still works)
// Thresholds are relative to the centre measured at boot (leave the stick
// untouched while powering on): trigger below 50% / above 150% of centre,
// release again inside 65% / 135%.
const bool INVERT_X = false; // flip if left/right feel backwards
const bool INVERT_Y = false; // flip if up/down feel backwards
const unsigned long REPEAT_DELAY_MS = 450;
const unsigned long REPEAT_RATE_MS  = 220;

const unsigned long PLAY_STEP_MS = 1200;    // how long each letter is held
const unsigned long PLAY_GAP_MS  = 600;     // flat pause between letters

// ---------- Mac (host) navigation mode ----------
// Sent "HN" by the Lexis backend. The Mac then owns the message and position, so the
// cell, LCD and dashboard can't disagree; the joystick just reports moves. One report
// per push (the stick must return to centre) and at least HOST_STEP_GAP_MS apart.
const unsigned long HOST_STEP_GAP_MS = 200;
bool hostNav = false;
int hostLastX = 0;
unsigned long hostLastStep = 0;

// ---------- LCD (16x2 HD44780, 4-bit mode) ----------
//                 RS, E, D4, D5,  D6,   D7
LiquidCrystal lcd(A3, A4, A5, SCK, MOSI, MISO);

// ---------- Braille table: bit 0 = dot 1 = servo 1 ... bit 5 = servo 6 ----------
const char CHARS[] = "abcdefghijklmnopqrstuvwxyz ";
const uint8_t NUM_CHARS = 27;
const uint8_t BRAILLE_MASK[NUM_CHARS] = {
  0x01, 0x03, 0x09, 0x19, 0x11, 0x0B, 0x1B, 0x13, 0x0A, 0x1A,  // a-j
  0x05, 0x07, 0x0D, 0x1D, 0x15, 0x0F, 0x1F, 0x17, 0x0E, 0x1E,  // k-t
  0x25, 0x27, 0x3A, 0x2D, 0x3D, 0x35,                          // u-z
  0x00                                                          // space
};

// ---------- State ----------
uint16_t pulseUs[NUM_SERVOS];
unsigned long lastFrame = 0;
char token[8];
uint8_t tokenLen = 0;

uint8_t charIdx = 26;          // start on "space" so all servos stay flat
char msg[33] = "";             // message built with the button / JP command
uint8_t msgLen = 0;

bool lcdManual = false;        // true after L1:/L2:/LC until LA is sent
bool textMode = false;         // currently collecting L1:/L2: text
uint8_t textRow = 0;
char textBuf[17];
uint8_t textLen = 0;

struct Axis { int dir; unsigned long next; int center; int state; };
Axis axisX = {0, 0, 2048, 0};
Axis axisY = {0, 0, 2048, 0};
int lastYDir = 0;
bool joyOk = false;            // true once both axes calibrated sensibly

// =====================================================================
//  Servo driver (software PWM)
// =====================================================================
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

void waitMs(unsigned long ms) {
  unsigned long start = millis();
  while (millis() - start < ms) refreshServos();
}

void setServo(uint8_t index, uint8_t pos) {  // index 0-based
  pulseUs[index] = pos ? PULSE_POS1_US : PULSE_POS0_US;
}

void showMask(uint8_t mask) {
  for (uint8_t i = 0; i < NUM_SERVOS; i++) setServo(i, (mask >> i) & 1);
}

// =====================================================================
//  LCD helpers
// =====================================================================
void printRow(uint8_t row, const char *s) {
  char buf[17];
  snprintf(buf, sizeof(buf), "%-16s", s);  // pad / truncate to 16 chars
  lcd.setCursor(0, row);
  lcd.print(buf);
}

// Only draws while the LCD is in automatic mode
void autoRow(uint8_t row, const char *s) {
  if (!lcdManual) printRow(row, s);
}

void updateLcd() {
  if (lcdManual) return;

  uint8_t mask = BRAILLE_MASK[charIdx];
  char dots[8];
  uint8_t k = 0;
  for (uint8_t i = 0; i < 6; i++) {
    if ((mask >> i) & 1) dots[k++] = '1' + i;
  }
  dots[k] = '\0';

  char ch = CHARS[charIdx];
  char line[17];
  snprintf(line, sizeof(line), "Letter %c  %s",
           ch == ' ' ? '_' : (char)(ch - 32), k ? dots : "-");
  printRow(0, line);

  const char *tail = msgLen > 16 ? msg + (msgLen - 16) : msg;
  printRow(1, tail);
}

// =====================================================================
//  Events reported back over serial
// =====================================================================
void reportChar() {
  Serial.print("CHAR ");
  if (CHARS[charIdx] == ' ') Serial.println("SPACE");
  else Serial.println(CHARS[charIdx]);
}

void reportMsg() {
  Serial.print("MSG ");
  Serial.println(msg);
}

// =====================================================================
//  Joystick actions (used by the real joystick AND the serial commands)
// =====================================================================
void applyCurrentChar() {
  showMask(BRAILLE_MASK[charIdx]);
  updateLcd();
}

void actionStep(int dir) {                 // JL (-1) / JR (+1)
  charIdx = (charIdx + NUM_CHARS + dir) % NUM_CHARS;
  applyCurrentChar();
  reportChar();
}

bool actionPlay() {                        // JU
  if (msgLen == 0) return false;
  Serial.println("PLAY START");
  for (uint8_t i = 0; i < msgLen; i++) {
    const char *p = strchr(CHARS, msg[i]);
    uint8_t idx = p ? (uint8_t)(p - CHARS) : 26;

    char line[17];
    snprintf(line, sizeof(line), "Playing %u/%u", i + 1, msgLen);
    autoRow(0, line);

    showMask(BRAILLE_MASK[idx]);
    waitMs(PLAY_STEP_MS);
    showMask(0);
    waitMs(PLAY_GAP_MS);
  }
  applyCurrentChar();                      // back to the selected letter
  Serial.println("PLAY DONE");
  return true;
}

bool actionDelete() {                      // JD
  if (msgLen == 0) return false;
  msg[--msgLen] = '\0';
  updateLcd();
  reportMsg();
  return true;
}

bool actionPress() {                       // JP
  if (msgLen >= sizeof(msg) - 1) return false;
  msg[msgLen++] = CHARS[charIdx];
  msg[msgLen] = '\0';
  updateLcd();
  reportMsg();
  return true;
}

// =====================================================================
//  Physical joystick
// =====================================================================
bool axisStep(Axis &a, int dir) {
  unsigned long now = millis();
  if (dir == 0) { a.dir = 0; return false; }
  if (dir != a.dir) { a.dir = dir; a.next = now + REPEAT_DELAY_MS; return true; }
  if (now >= a.next) { a.next = now + REPEAT_RATE_MS; return true; }
  return false;
}

// Reads the centre position. Returns false if the readings are unstable
// (floating / unwired pin) or stuck at an extreme, so the stick gets disabled
// instead of scrolling through letters forever.
bool calibrateAxis(Axis &a, uint8_t pin) {
  long sum = 0;
  int lo = 32767, hi = 0;
  for (uint8_t i = 0; i < 32; i++) {
    int v = analogRead(pin);
    sum += v;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
    delay(3);
  }
  a.center = (int)(sum / 32);
  a.state = 0;
  return a.center > 100 && (hi - lo) < a.center * 15 / 100;
}

// Returns -1, 0 or +1 with hysteresis so noise near a threshold can't retrigger
int readDir(Axis &a, uint8_t pin, bool invert) {
  int v = analogRead(pin);
  int lowOn   = a.center * 50 / 100;
  int highOn  = a.center * 150 / 100;
  int lowOff  = a.center * 65 / 100;
  int highOff = a.center * 135 / 100;

  if (a.state == 0) {
    if (v < lowOn) a.state = -1;
    else if (v > highOn) a.state = 1;
  } else if (a.state == -1) {
    if (v > lowOff) a.state = 0;
  } else {
    if (v < highOff) a.state = 0;
  }
  return invert ? -a.state : a.state;
}

bool buttonPressed() {
  static bool last = HIGH;
  static unsigned long t = 0;
  bool now = digitalRead(JOY_SW);
  if (now != last && millis() - t > 30) {
    t = millis();
    last = now;
    if (now == LOW) return true;
  }
  return false;
}

// Mac mode: report moves only, no local action and no auto-repeat
void handleJoystickHost() {
  if (joyOk) {
    int dx = readDir(axisX, JOY_X, INVERT_X);
    if (dx != hostLastX) {
      hostLastX = dx;
      if (dx != 0 && millis() - hostLastStep >= HOST_STEP_GAP_MS) {
        hostLastStep = millis();
        Serial.println(dx < 0 ? "JOY L" : "JOY R");
      }
    }

    int dy = readDir(axisY, JOY_Y, INVERT_Y);
    if (dy != lastYDir) {
      lastYDir = dy;
      if (dy == 1) Serial.println("JOY U");
      else if (dy == -1) Serial.println("JOY D");
    }
  }

  if (buttonPressed()) Serial.println("JOY P");
}

void handleJoystick() {
  if (hostNav) { handleJoystickHost(); return; }

  if (joyOk) {
    int dx = readDir(axisX, JOY_X, INVERT_X);
    if (axisStep(axisX, dx)) actionStep(dx);

    int dy = readDir(axisY, JOY_Y, INVERT_Y);
    if (dy != lastYDir) {
      lastYDir = dy;
      if (dy == 1) actionPlay();
      else if (dy == -1) actionDelete();
    }
  }

  if (buttonPressed()) actionPress();
}

// =====================================================================
//  Serial command handling
// =====================================================================
void runTest() {
  Serial.println("TEST START");
  autoRow(0, "Servo test...");
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
  applyCurrentChar();
}

bool isCmd(const char *t, char a, char b) {
  return toupper(t[0]) == a && toupper(t[1]) == b && t[2] == '\0';
}

void reply(bool ok, const char *t) {
  Serial.print(ok ? "OK " : "ERR ");
  Serial.println(t);
}

void processToken(const char *t) {
  // Test
  if (isCmd(t, 'T', 'S')) { runTest(); return; }

  // Virtual joystick
  if (isCmd(t, 'J', 'L')) { actionStep(-1); reply(true, t); return; }
  if (isCmd(t, 'J', 'R')) { actionStep(1);  reply(true, t); return; }
  if (isCmd(t, 'J', 'U')) { reply(actionPlay(),   t); return; }
  if (isCmd(t, 'J', 'D')) { reply(actionDelete(), t); return; }
  if (isCmd(t, 'J', 'P')) { reply(actionPress(),  t); return; }

  // Joystick diagnostics: JV = live raw values + calibrated centres, JC = re-measure centre
  if (isCmd(t, 'J', 'V')) {
    Serial.print("JOYV X=");  Serial.print(analogRead(JOY_X));
    Serial.print(" Y=");      Serial.print(analogRead(JOY_Y));
    Serial.print(" SW=");     Serial.print(digitalRead(JOY_SW));
    Serial.print(" CX=");     Serial.print(axisX.center);
    Serial.print(" CY=");     Serial.print(axisY.center);
    Serial.print(" OK=");     Serial.println(joyOk ? 1 : 0);
    return;
  }
  if (isCmd(t, 'J', 'C')) {
    joyOk = JOY_ENABLED && calibrateAxis(axisX, JOY_X) && calibrateAxis(axisY, JOY_Y);
    reply(joyOk, t);
    return;
  }

  // Mac navigation mode
  if (isCmd(t, 'H', 'N')) { hostNav = true; hostLastX = 0; reply(true, t); return; }
  if (isCmd(t, 'H', 'F')) {
    hostNav = false;
    lcdManual = false;
    applyCurrentChar();
    reply(true, t);
    return;
  }

  // LCD control
  if (isCmd(t, 'L', 'C')) {
    lcdManual = true;
    lcd.clear();
    reply(true, t);
    return;
  }
  if (isCmd(t, 'L', 'A')) {
    lcdManual = false;
    updateLcd();
    reply(true, t);
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
      reply(true, t);

      char line[17];
      snprintf(line, sizeof(line), "Serial: %s", t);
      autoRow(0, line);
      return;
    }
  }

  reply(false, t);
}

void endToken() {
  if (tokenLen > 0) {
    token[tokenLen] = '\0';
    processToken(token);
    tokenLen = 0;
  }
}

void finishText() {
  textBuf[textLen] = '\0';
  lcdManual = true;
  printRow(textRow, textBuf);
  Serial.print("OK L");
  Serial.println(textRow + 1);
  textMode = false;
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();

    // Collecting LCD text: everything up to the newline belongs to it
    if (textMode) {
      if (c == '\n' || c == '\r') finishText();
      else if (textLen < 16) textBuf[textLen++] = c;
      continue;
    }

    if (c == ',' || c == ' ' || c == ';' || c == '\n' || c == '\r') {
      endToken();
    } else if (tokenLen < sizeof(token) - 1) {
      token[tokenLen++] = c;

      // "L1:" or "L2:" switches to text mode
      if (tokenLen == 3 && toupper(token[0]) == 'L' &&
          (token[1] == '1' || token[1] == '2') && token[2] == ':') {
        textMode = true;
        textRow = token[1] - '1';
        textLen = 0;
        tokenLen = 0;
      }
    } else {
      tokenLen = 0;
      Serial.println("ERR TOO_LONG");
    }
  }
}

// =====================================================================
//  Arduino
// =====================================================================
void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < NUM_SERVOS; i++) {
    pinMode(SERVO_PINS[i], OUTPUT);
    digitalWrite(SERVO_PINS[i], LOW);
    setServo(i, 0);  // start at position 0
  }

  analogReadResolution(12);
  pinMode(JOY_SW, INPUT_PULLUP);

  // Measure the stick centre (keep the stick untouched while booting)
  joyOk = JOY_ENABLED && calibrateAxis(axisX, JOY_X) && calibrateAxis(axisY, JOY_Y);

  lcd.begin(16, 2);
  printRow(0, "Braille ready");
  printRow(1, joyOk ? "Move joystick" : "Joystick off!");
  delay(1200);
  updateLcd();

  Serial.println("READY");
  Serial.print("INFO JOY ");
  Serial.print(joyOk ? "OK" : "OFF");
  Serial.print(" X=");
  Serial.print(axisX.center);
  Serial.print(" Y=");
  Serial.println(axisY.center);
}

void loop() {
  refreshServos();
  handleSerial();
  handleJoystick();

  // Mac went away (serial port closed): fall back to the local alphabet mode
  if (hostNav && !Serial) {
    hostNav = false;
    lcdManual = false;
    applyCurrentChar();
  }
}
