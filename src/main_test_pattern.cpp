// Standalone HUB75 diagnostic, not part of the real firmware - no
// Bluetooth. Shows a static pattern that makes signal-timing problems
// obvious: single-pixel white lines (colour fringing = channels shifted a
// column) and isolated red/green/blue pixels (drops/shifts per channel).
#include <Arduino.h>
#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

#include "adapter_pins.h"

// false matches panel.h; override with -DTEST_CLKPHASE=true to compare.
#ifndef TEST_CLKPHASE
#define TEST_CLKPHASE false
#endif

namespace {
MatrixPanel_I2S_DMA *display;
}  // namespace

void setup() {
  Serial.begin(115200);
  HUB75_I2S_CFG cfg(64, 32, 1, AdapterPinsV2());  // width, height, chain length - matches panel.h
  cfg.clkphase = TEST_CLKPHASE;
  display = new MatrixPanel_I2S_DMA(cfg);
  display->begin();
  display->setBrightness8(128);  // half brightness - don't blind yourself/stress the PSU
  display->clearScreen();

  // Left half: white 1px vertical lines every 4 columns, plus a white border.
  for (int y = 0; y < 32; y++) {
    for (int x = 0; x < 32; x += 4) display->drawPixelRGB888(x, y, 255, 255, 255);
    display->drawPixelRGB888(63, y, 255, 255, 255);
  }
  for (int x = 0; x < 64; x++) {
    display->drawPixelRGB888(x, 0, 255, 255, 255);
    display->drawPixelRGB888(x, 31, 255, 255, 255);
  }
  // Right half: rows of isolated pure red, green, blue pixels, 2px apart.
  const uint8_t rgb[3][3] = {{255, 0, 0}, {0, 255, 0}, {0, 0, 255}};
  for (int c = 0; c < 3; c++) {
    for (int x = 36; x < 62; x += 2) {
      display->drawPixelRGB888(x, 6 + c * 8, rgb[c][0], rgb[c][1], rgb[c][2]);
    }
  }
  Serial.printf("[+] Diagnostic pattern, clkphase=%d\n", (int)cfg.clkphase);
  Serial.println("[+] Ready");
}

void loop() { delay(1000); }
