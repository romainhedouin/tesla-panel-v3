// HUB75 hardware wrapper - a Panel class exposing one handler per command
// in protocol.h (handleImage/handleKill/handleSetBrightness), each
// returning an error message on invalid input rather than crashing.
//
// Pin mapping: see adapter_pins.h - matches the seengreat "RGB Matrix
// Adapter Board (E)" rev 2.2, confirmed against real hardware.
#pragma once

#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

#include "adapter_pins.h"
#include "protocol.h"

class Panel {
 public:
  Panel() {
    HUB75_I2S_CFG cfg(64, 32, 1, AdapterPinsV2());  // width, height, chain length - one 64x32 panel
    // Single-buffered: a second DMA framebuffer doesn't fit in internal RAM
    // alongside the classic Bluetooth stack on Arduino core 3.x. A 64x32
    // frame is drawn fast enough that tearing isn't visible.
    cfg.double_buff = false;
    // This adapter/panel latches data on the other clock edge: with the
    // library's default (true), white pixels fringed into neighbouring
    // columns, worst on the bottom half. Found with main_test_pattern.cpp.
    cfg.clkphase = false;
    display_ = new MatrixPanel_I2S_DMA(cfg);
    if (!display_->begin()) {
      Serial.println("[-] HUB75 panel failed to start (not enough DMA memory?)");
    }
    display_->setBrightness8(255);
    display_->clearScreen();
    display_->flipDMABuffer();
  }

  // Returns true on success. On failure, sets *error to a human-readable
  // reason and leaves the panel showing whatever it last successfully
  // displayed.
  bool handleImage(const uint8_t *payload, size_t length, String *error) {
    protocol::Ppm ppm;
    if (!protocol::parsePpm(payload, length, &ppm, error)) {
      return false;
    }
    for (int y = 0; y < ppm.height; y++) {
      size_t row = size_t(y) * ppm.width * 3;
      for (int x = 0; x < ppm.width; x++) {
        size_t i = row + size_t(x) * 3;
        display_->drawPixelRGB888(x, y, ppm.pixels[i], ppm.pixels[i + 1], ppm.pixels[i + 2]);
      }
    }
    display_->flipDMABuffer();
    return true;
  }

  bool handleKill(String *error) {
    display_->clearScreen();
    display_->flipDMABuffer();
    return true;
  }

  bool handleSetBrightness(const uint8_t *payload, size_t length, String *error) {
    if (length < 1) {
      *error = "brightness payload empty";
      return false;
    }
    int brightness = payload[0];
    if (brightness < 1 || brightness > 100) {
      *error = "brightness must be 1-100, got " + String(brightness);
      return false;
    }
    // Wire protocol carries 1-100 (what the Android app sends); the
    // library wants an 8-bit 0-255 value.
    display_->setBrightness8(uint8_t((brightness * 255 + 50) / 100));
    return true;
  }

 private:
  MatrixPanel_I2S_DMA *display_;
};
