// Entry point for the original ESP32 (classic Bluetooth/BR-EDR). Speaks
// the wire protocol in protocol.h over RFCOMM/SPP: BluetoothSerial
// registers the standard SPP UUID (00001101-0000-1000-8000-00805F9B34FB)
// automatically, which is the one the Android app targets via
// createRfcommSocketToServiceRecord().
//
// Blocking-read design: each command is read with recvExact() (exactly N
// bytes or the connection is gone) - first the fixed-size header, then
// the payload length it announces - and answered before the next one is
// read, one client at a time.
#include <Arduino.h>
#include <BluetoothSerial.h>
#include <freertos/stream_buffer.h>

#include "panel.h"
#include "protocol.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BT_SPP_ENABLED)
#error Bluetooth Classic/SPP is not enabled in the sdkconfig of this build - \
       needed by BluetoothSerial. See the espressif32 platform docs for \
       enabling CONFIG_BT_SPP_ENABLED.
#endif

namespace {

BluetoothSerial gSerialBT;
Panel *gPanel;

// BluetoothSerial's own RX queue is a hardcoded 512 bytes and silently
// drops whatever overflows it - a 6KB image frame sent in one go loses
// bytes and the read below then waits forever. onData() bypasses that
// queue, so incoming bytes land here instead, sized for a whole frame.
constexpr size_t RX_BUFFER_SIZE = 8192;
StreamBufferHandle_t gRxBuffer;

void onBtData(const uint8_t *data, size_t len) {
  size_t sent = xStreamBufferSend(gRxBuffer, data, len, 0);
  if (sent < len) {
    Serial.printf("[-] RX buffer full, dropped %u bytes\n", (unsigned)(len - sent));
  }
}

// Reads exactly n bytes from the connected client, blocking. Returns false
// if the connection drops.
bool recvExact(uint8_t *buf, size_t n) {
  size_t total = 0;
  while (total < n) {
    if (!gSerialBT.hasClient()) {
      return false;
    }
    total += xStreamBufferReceive(gRxBuffer, buf + total, n - total, pdMS_TO_TICKS(50));
  }
  return true;
}

void sendResponse(uint8_t status, const String &message) {
  uint8_t header[protocol::RESPONSE_HEADER_SIZE];
  header[0] = status;
  protocol::writeU32BE(header + 1, message.length());
  gSerialBT.write(header, sizeof(header));
  if (message.length() > 0) {
    gSerialBT.write((const uint8_t *)message.c_str(), message.length());
  }
}

// Reads and dispatches a single command. Returns false once the client
// has disconnected (or the stream is desynced and the connection should
// be dropped).
bool handleOneCommand() {
  uint8_t header[protocol::HEADER_SIZE];
  if (!recvExact(header, sizeof(header))) {
    return false;
  }
  protocol::ParsedHeader parsed = protocol::parseHeader(header);

  if (parsed.length > protocol::MAX_PAYLOAD_SIZE) {
    Serial.printf("[-] Payload length %u exceeds max %u (desynced stream?) - dropping connection\n",
                  (unsigned)parsed.length, (unsigned)protocol::MAX_PAYLOAD_SIZE);
    return false;
  }

  // Static, not stack-allocated: the default Arduino loop task stack is
  // only 8KB, too small to also hold an 8KB payload buffer.
  static uint8_t payload[protocol::MAX_PAYLOAD_SIZE];
  if (parsed.length > 0 && !recvExact(payload, parsed.length)) {
    return false;
  }

  String error;
  bool ok;
  switch (parsed.commandType) {
    case protocol::COMMAND_IMAGE:
      ok = gPanel->handleImage(payload, parsed.length, &error);
      break;
    case protocol::COMMAND_KILL:
      ok = gPanel->handleKill(&error);
      break;
    case protocol::COMMAND_SET_BRIGHTNESS:
      ok = gPanel->handleSetBrightness(payload, parsed.length, &error);
      break;
    default:
      ok = false;
      error = "Unknown command type: " + String(parsed.commandType);
      break;
  }

  if (ok) {
    sendResponse(protocol::STATUS_OK, "");
  } else {
    Serial.println("[-] Command " + String(parsed.commandType) + " failed: " + error);
    sendResponse(protocol::STATUS_ERROR, error);
  }
  return true;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  gRxBuffer = xStreamBufferCreate(RX_BUFFER_SIZE, 1);
  gSerialBT.onData(onBtData);
  // Bluetooth before the panel: the HUB75 DMA framebuffer takes enough
  // internal RAM that Bluedroid's init runs out of heap and crashes (null
  // memset in bta_sys_init) if the panel allocates first.
  // disableBLE: we only speak classic SPP, and the controller's BLE memory
  // is RAM the classic stack needs once a phone connects - without it,
  // Bluedroid hit a null deref under memory pressure on first connection.
  gSerialBT.begin("teslapi-esp32", false, true);
  gPanel = new Panel();
  Serial.printf("[+] Free heap: %u bytes, largest block: %u\n", (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
  Serial.println("[+] Bluetooth SPP server started as \"teslapi-esp32\"");
  // Parsed by the installer - uppercase, colon-separated.
  Serial.println("[+] Bluetooth address: " + gSerialBT.getBtAddressString());
  Serial.println("[+] Ready");
}

void loop() {
  if (gSerialBT.hasClient()) {
    Serial.println("[+] Client connected");
    while (handleOneCommand()) {
    }
    xStreamBufferReset(gRxBuffer);  // don't feed a half-read frame to the next client
    Serial.println("[+] Disconnected");
  }
  delay(10);
}
