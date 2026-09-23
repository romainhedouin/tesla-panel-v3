// BLE entry point (esp32-ble) - the alternative to main_classic.cpp's
// Bluetooth Classic SPP on the same ESP32. Speaks
// the same logical wire protocol as the classic-ESP32 variant
// (protocol.h), but framed differently: BLE GATT writes/notifies are
// capped by the negotiated MTU (a handful of hundred bytes at best), while
// a 64x32 PPM image is ~6.2KB - the phone must split a command across
// several characteristic writes, and this reassembles them here before
// dispatching.
//
// Custom GATT service (no standard profile fits this - unlike classic SPP,
// BLE has no generic "serial port" analog):
//   Service:               c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c00
//   Command characteristic c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c01 (write)  - phone -> board
//   Response characteristic c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c02 (notify) - board -> phone
// The TeslaLED app's BLE transport ("ESP32 BLE") implements this service;
// never tested end to end on hardware.
#include <Arduino.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>

#include <vector>

#include "panel.h"
#include "protocol.h"

namespace {

constexpr char kServiceUuid[] = "c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c00";
constexpr char kCommandCharUuid[] = "c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c01";
constexpr char kResponseCharUuid[] = "c9af0000-1a1a-4e7e-9a1e-4b1e2f6a9c02";

Panel *gPanel;
BLEServer *gServer;
BLECharacteristic *gResponseChar;

// Reassembly buffer for the command currently being received - BLE writes
// arrive in MTU-sized chunks (a fraction of a 6KB image frame), so a full
// command is spread across several onWrite() calls. Fixed-size and static
// rather than a growable vector: it can never hold more than one command
// (header + the largest payload parseHeader() lets through), and it's
// carved out at link time instead of competing with the BLE stack and the
// DMA framebuffer for heap. Reset once a full command has been dispatched,
// or when a connection begins/ends.
uint8_t gBuffer[protocol::HEADER_SIZE + protocol::MAX_PAYLOAD_SIZE];
size_t gBufferLength = 0;
// Set once the stream is known to be desynced: everything the client
// writes is ignored until the disconnect we requested takes effect, so
// leftover payload bytes aren't misread as new command headers.
bool gDesynced = false;

void sendResponse(uint8_t status, const String &message) {
  uint8_t header[protocol::RESPONSE_HEADER_SIZE];
  header[0] = status;
  protocol::writeU32BE(header + 1, message.length());
  // Response is always small (a short status message) - well under any
  // negotiated MTU, so unlike the command direction this never needs to be
  // split across multiple notifications.
  std::vector<uint8_t> response(header, header + sizeof(header));
  response.insert(response.end(), message.begin(), message.end());
  gResponseChar->setValue(response.data(), response.size());
  gResponseChar->notify();
}

// Dispatches the fully-reassembled command in gBuffer (header already
// validated by the caller), then clears it for the next one.
void dispatchBufferedCommand() {
  protocol::ParsedHeader parsed = protocol::parseHeader(gBuffer);
  const uint8_t *payload = gBuffer + protocol::HEADER_SIZE;

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
  gBufferLength = 0;
}

class CommandCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    const uint8_t *data = characteristic->getData();
    size_t length = characteristic->getLength();
    if (gDesynced) {
      return;
    }

    // A write can end one command and start the next (the phone isn't
    // required to align chunks to command boundaries), so only copy what
    // the current command still needs, dispatch it, and loop over the rest
    // - never append past one command's worth of bytes.
    while (length > 0) {
      // How many bytes the command in progress needs in total: just the
      // header until we have it, then header + its payload.
      size_t needed = protocol::HEADER_SIZE;
      if (gBufferLength >= protocol::HEADER_SIZE) {
        needed += protocol::parseHeader(gBuffer).length;
      }
      size_t chunk = min(length, needed - gBufferLength);
      memcpy(gBuffer + gBufferLength, data, chunk);
      gBufferLength += chunk;
      data += chunk;
      length -= chunk;

      if (gBufferLength == protocol::HEADER_SIZE) {
        protocol::ParsedHeader parsed = protocol::parseHeader(gBuffer);
        if (parsed.length > protocol::MAX_PAYLOAD_SIZE) {
          // No way to find the next command boundary in a desynced stream -
          // drop the connection like the classic variant does, so the app
          // reconnects and starts over from a clean state.
          Serial.printf("[-] Payload length %u exceeds max %u (desynced stream?) - dropping connection\n",
                        (unsigned)parsed.length, (unsigned)protocol::MAX_PAYLOAD_SIZE);
          gBufferLength = 0;
          gDesynced = true;
          gServer->disconnect(gServer->getConnId());
          return;
        }
      }
      if (gBufferLength >= protocol::HEADER_SIZE &&
          gBufferLength == protocol::HEADER_SIZE + protocol::parseHeader(gBuffer).length) {
        dispatchBufferedCommand();
      }
    }
  }
};

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *server) override {
    Serial.println("[+] BLE client connected");
    gBufferLength = 0;
    gDesynced = false;
  }

  void onDisconnect(BLEServer *server) override {
    Serial.println("[+] BLE client disconnected");
    gBufferLength = 0;
    gDesynced = false;
    // Arduino BLE doesn't resume advertising on its own after a disconnect.
    server->startAdvertising();
  }
};

}  // namespace

void setup() {
  Serial.begin(115200);
  // Bluetooth before the panel: the HUB75 DMA framebuffer takes enough
  // internal RAM that the Bluetooth stack's init can run out of heap and
  // crash if the panel allocates first.
  BLEDevice::init("teslapi-esp32-ble");
  BLEDevice::setMTU(517);  // request the max - fewer chunks per image frame

  gServer = BLEDevice::createServer();
  gServer->setCallbacks(new ServerCallbacks());

  BLEService *service = gServer->createService(kServiceUuid);

  BLECharacteristic *commandChar = service->createCharacteristic(
      kCommandCharUuid, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  commandChar->setCallbacks(new CommandCallbacks());

  gResponseChar = service->createCharacteristic(kResponseCharUuid, BLECharacteristic::PROPERTY_NOTIFY);
  // CCCD - required for the client to enable notifications.
  gResponseChar->addDescriptor(new BLE2902());

  service->start();

  gPanel = new Panel();
  // Advertise only once the panel exists - a client that connected and
  // wrote a command any earlier would be dispatched to a null gPanel.
  gServer->getAdvertising()->addServiceUUID(kServiceUuid);
  gServer->getAdvertising()->start();
  Serial.printf("[+] Free heap: %u bytes, largest block: %u\n", (unsigned)ESP.getFreeHeap(),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
  Serial.println("[+] BLE GATT server started as \"teslapi-esp32-ble\"");
  // Uppercase to match the classic variant's format - BLEAddress prints
  // lowercase hex.
  String address = BLEDevice::getAddress().toString();
  address.toUpperCase();
  Serial.println("[+] Bluetooth address: " + address);
  Serial.println("[+] Ready");
}

void loop() {
  delay(100);  // everything happens in BLE callbacks - nothing to poll here
}
