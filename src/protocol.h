// Wire protocol for the panel's command channel - the reference
// definition for what the Android app sends and expects back. Kept
// transport-agnostic: this file only deals with parsing bytes already in
// memory, never touches BluetoothSerial/BLE directly, so the same code
// serves both firmware variants (see main_classic.cpp/main_ble.cpp for how
// each one gets bytes into a buffer in the first place).
//
// [1 byte command type][4 bytes big-endian payload length][payload]
//
// followed by a response in the same shape, mirrored:
// [1 byte status][4 bytes big-endian message length][message, UTF-8]
#pragma once

#include <Arduino.h>
#include <cstdint>

namespace protocol {

constexpr uint8_t COMMAND_IMAGE = 0;
constexpr uint8_t COMMAND_KILL = 2;
constexpr uint8_t COMMAND_SET_BRIGHTNESS = 3;

constexpr uint8_t STATUS_OK = 0x00;
constexpr uint8_t STATUS_ERROR = 0x01;

constexpr size_t HEADER_SIZE = 5;           // 1 byte type + 4 bytes length
constexpr size_t RESPONSE_HEADER_SIZE = 5;  // 1 byte status + 4 bytes length

// Comfortably above the ~6.2KB a 64x32 PPM frame needs - a length past
// this means a desynced/corrupted stream, not a legitimately large command.
// Kept tight rather than generous because the receive buffer is statically
// allocated in internal RAM, which the Bluetooth stack and the HUB75 DMA
// framebuffer also need: a 64KB buffer starved the Bluetooth stack into
// crashing on connect.
constexpr uint32_t MAX_PAYLOAD_SIZE = 8192;

// Reads a big-endian uint32 starting at buf[offset].
inline uint32_t readU32BE(const uint8_t *buf) {
  return (uint32_t(buf[0]) << 24) | (uint32_t(buf[1]) << 16) |
         (uint32_t(buf[2]) << 8) | uint32_t(buf[3]);
}

// Writes a big-endian uint32 to buf[0..3].
inline void writeU32BE(uint8_t *buf, uint32_t value) {
  buf[0] = uint8_t(value >> 24);
  buf[1] = uint8_t(value >> 16);
  buf[2] = uint8_t(value >> 8);
  buf[3] = uint8_t(value);
}

struct ParsedHeader {
  uint8_t commandType;
  uint32_t length;
};

inline ParsedHeader parseHeader(const uint8_t *buf) {
  return ParsedHeader{buf[0], readU32BE(buf + 1)};
}

// Parsed P6 PPM image - width/height plus a pointer+length into the
// original payload buffer (no copy) for the raw RGB triplets.
struct Ppm {
  int width;
  int height;
  const uint8_t *pixels;
  size_t pixelsLength;
};

// Minimal P6 PPM parser matching exactly what the Android app's
// PpmCodec.encode() produces: 'P6\n<width> <height>\n<maxval>\n' then raw
// width*height RGB triplets. Also skips a '#'-prefixed comment line (some
// bundled assets were exported from GIMP, which adds one) like whitespace
// rather than trying to parse it as a number.
//
// Returns false (with `error` set to a human-readable reason) on anything
// malformed, rather than reading out of bounds - a corrupted/truncated
// frame must never crash the firmware.
inline bool parsePpm(const uint8_t *data, size_t dataLength, Ppm *out, String *error) {
  if (dataLength < 2 || data[0] != 'P' || data[1] != '6') {
    *error = "Malformed image data (missing P6 header)";
    return false;
  }
  size_t pos = 2;
  int values[3];
  int valueCount = 0;
  while (valueCount < 3) {
    while (pos < dataLength &&
           (data[pos] == ' ' || data[pos] == '\t' || data[pos] == '\r' ||
            data[pos] == '\n' || data[pos] == '#')) {
      if (data[pos] == '#') {
        while (pos < dataLength && data[pos] != '\r' && data[pos] != '\n') {
          pos++;
        }
      } else {
        pos++;
      }
    }
    if (pos >= dataLength) {
      *error = "Malformed image data (truncated header)";
      return false;
    }
    size_t start = pos;
    while (pos < dataLength && data[pos] != ' ' && data[pos] != '\t' &&
           data[pos] != '\r' && data[pos] != '\n') {
      pos++;
    }
    if (pos >= dataLength) {
      *error = "Malformed image data (truncated header)";
      return false;
    }
    int value = 0;
    for (size_t i = start; i < pos; i++) {
      value = value * 10 + (data[i] - '0');
    }
    values[valueCount++] = value;
  }
  pos += 1;  // single whitespace byte separating header from pixel data
  int width = values[0];
  int height = values[1];
  size_t pixelBytes = size_t(width) * size_t(height) * 3;
  if (width <= 0 || height <= 0 || pos + pixelBytes > dataLength) {
    *error = "Malformed image data (truncated pixel data)";
    return false;
  }
  out->width = width;
  out->height = height;
  out->pixels = data + pos;
  out->pixelsLength = pixelBytes;
  return true;
}

}  // namespace protocol
