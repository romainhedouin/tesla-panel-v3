// Host-side unit tests for protocol.h - ports the protocol-level cases of
// the old Python suite (tests/test_protocol.py in the Pi repo). Its
// recv_exact/handle_one_command cases exercise socket I/O, which has no
// counterpart here (protocol.h only parses bytes already in memory), so
// they're not ported.
//
//   pio test -e native
#include <unity.h>

#include <cstring>
#include <string>
#include <vector>

#include "protocol.h"

using namespace protocol;

void setUp() {}
void tearDown() {}

// Concatenates a text header and raw pixel bytes into one payload, the
// way the Android app sends a frame.
static std::vector<uint8_t> makePpm(const char *header, const std::vector<uint8_t> &pixels) {
  std::vector<uint8_t> out(header, header + strlen(header));
  out.insert(out.end(), pixels.begin(), pixels.end());
  return out;
}

static bool errorContains(const String &error, const char *needle) {
  return std::string(error.c_str()).find(needle) != std::string::npos;
}

void test_parse_ppm_basic() {
  std::vector<uint8_t> pixels = {255, 0, 0, 0, 255, 0};  // red pixel, green pixel
  std::vector<uint8_t> data = makePpm("P6\n2 1\n255\n", pixels);
  Ppm ppm;
  String error;
  TEST_ASSERT_TRUE(parsePpm(data.data(), data.size(), &ppm, &error));
  TEST_ASSERT_EQUAL_INT(2, ppm.width);
  TEST_ASSERT_EQUAL_INT(1, ppm.height);
  TEST_ASSERT_EQUAL_size_t(pixels.size(), ppm.pixelsLength);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(pixels.data(), ppm.pixels, pixels.size());
}

// GIMP-exported PPMs add a '#'-prefixed comment line in the header, which
// must be skipped like whitespace rather than parsed as a number.
void test_parse_ppm_skips_comment_line() {
  std::vector<uint8_t> pixels = {1, 2, 3, 4, 5, 6};
  std::vector<uint8_t> data = makePpm("P6\n# CREATOR: GIMP\n2 1\n255\n", pixels);
  Ppm ppm;
  String error;
  TEST_ASSERT_TRUE(parsePpm(data.data(), data.size(), &ppm, &error));
  TEST_ASSERT_EQUAL_INT(2, ppm.width);
  TEST_ASSERT_EQUAL_INT(1, ppm.height);
  TEST_ASSERT_EQUAL_UINT8_ARRAY(pixels.data(), ppm.pixels, pixels.size());
}

void test_parse_ppm_rejects_missing_p6_header() {
  const char *text = "not a ppm at all";
  Ppm ppm;
  String error;
  TEST_ASSERT_FALSE(parsePpm((const uint8_t *)text, strlen(text), &ppm, &error));
  TEST_ASSERT_TRUE(errorContains(error, "P6"));
}

void test_parse_ppm_rejects_empty_payload() {
  Ppm ppm;
  String error;
  TEST_ASSERT_FALSE(parsePpm(nullptr, 0, &ppm, &error));
  TEST_ASSERT_TRUE(errorContains(error, "P6"));
}

void test_parse_ppm_rejects_truncated_header() {
  const char *text = "P6\n2 1";
  Ppm ppm;
  String error;
  TEST_ASSERT_FALSE(parsePpm((const uint8_t *)text, strlen(text), &ppm, &error));
  TEST_ASSERT_TRUE(errorContains(error, "truncated header"));
}

// Header claims 2x1 (6 bytes) but only 3 pixel bytes follow - must be
// rejected, not read past the end of the buffer.
void test_parse_ppm_rejects_truncated_pixel_data() {
  std::vector<uint8_t> data = makePpm("P6\n2 1\n255\n", {1, 2, 3});
  Ppm ppm;
  String error;
  TEST_ASSERT_FALSE(parsePpm(data.data(), data.size(), &ppm, &error));
  TEST_ASSERT_TRUE(errorContains(error, "truncated pixel data"));
}

void test_parse_ppm_rejects_zero_dimensions() {
  std::vector<uint8_t> data = makePpm("P6\n0 1\n255\n", {});
  Ppm ppm;
  String error;
  TEST_ASSERT_FALSE(parsePpm(data.data(), data.size(), &ppm, &error));
  TEST_ASSERT_TRUE(errorContains(error, "truncated pixel data"));
}

void test_read_u32_be() {
  const uint8_t buf[] = {0x12, 0x34, 0x56, 0x78};
  TEST_ASSERT_EQUAL_HEX32(0x12345678, readU32BE(buf));
}

void test_write_read_u32_be_round_trip() {
  const uint32_t values[] = {0, 1, 0xFF, 0x1234, 0x12345678, 0xFFFFFFFF};
  for (uint32_t value : values) {
    uint8_t buf[4];
    writeU32BE(buf, value);
    TEST_ASSERT_EQUAL_HEX32(value, readU32BE(buf));
  }
  uint8_t buf[4];
  writeU32BE(buf, 0x01020304);
  const uint8_t expected[] = {0x01, 0x02, 0x03, 0x04};
  TEST_ASSERT_EQUAL_UINT8_ARRAY(expected, buf, 4);
}

void test_parse_header() {
  const uint8_t buf[HEADER_SIZE] = {COMMAND_SET_BRIGHTNESS, 0x00, 0x00, 0x01, 0x02};
  ParsedHeader header = parseHeader(buf);
  TEST_ASSERT_EQUAL_UINT8(COMMAND_SET_BRIGHTNESS, header.commandType);
  TEST_ASSERT_EQUAL_UINT32(0x0102, header.length);
}

// The Python suite's oversized-length case relies on the firmware
// comparing the header's length against MAX_PAYLOAD_SIZE - check that a
// real 64x32 frame fits under it and a desynced 0xFFFFFFFF length doesn't.
void test_max_payload_size_fits_64x32_frame() {
  const size_t frameSize = strlen("P6\n64 32\n255\n") + 64 * 32 * 3;
  TEST_ASSERT_TRUE(frameSize <= MAX_PAYLOAD_SIZE);

  const uint8_t buf[HEADER_SIZE] = {COMMAND_IMAGE, 0xFF, 0xFF, 0xFF, 0xFF};
  TEST_ASSERT_TRUE(parseHeader(buf).length > MAX_PAYLOAD_SIZE);
}

void test_parse_ppm_64x32_frame() {
  std::vector<uint8_t> pixels(64 * 32 * 3, 0x7F);
  std::vector<uint8_t> data = makePpm("P6\n64 32\n255\n", pixels);
  Ppm ppm;
  String error;
  TEST_ASSERT_TRUE(parsePpm(data.data(), data.size(), &ppm, &error));
  TEST_ASSERT_EQUAL_INT(64, ppm.width);
  TEST_ASSERT_EQUAL_INT(32, ppm.height);
  TEST_ASSERT_EQUAL_size_t(pixels.size(), ppm.pixelsLength);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_parse_ppm_basic);
  RUN_TEST(test_parse_ppm_skips_comment_line);
  RUN_TEST(test_parse_ppm_rejects_missing_p6_header);
  RUN_TEST(test_parse_ppm_rejects_empty_payload);
  RUN_TEST(test_parse_ppm_rejects_truncated_header);
  RUN_TEST(test_parse_ppm_rejects_truncated_pixel_data);
  RUN_TEST(test_parse_ppm_rejects_zero_dimensions);
  RUN_TEST(test_read_u32_be);
  RUN_TEST(test_write_read_u32_be_round_trip);
  RUN_TEST(test_parse_header);
  RUN_TEST(test_max_payload_size_fits_64x32_frame);
  RUN_TEST(test_parse_ppm_64x32_frame);
  return UNITY_END();
}
