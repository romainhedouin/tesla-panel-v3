// Test-only stand-in for the Arduino core, used by the native env (see
// [env:native] in platformio.ini) so src/protocol.h compiles on the host.
// Only covers what protocol.h actually uses - String as an error-message
// holder - never shipped to the board.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

class String {
 public:
  String() = default;
  String(const char *s) : value_(s) {}

  String &operator=(const char *s) {
    value_ = s;
    return *this;
  }

  size_t length() const { return value_.length(); }
  const char *c_str() const { return value_.c_str(); }

  bool operator==(const String &other) const { return value_ == other.value_; }
  bool operator==(const char *other) const { return value_ == other; }

 private:
  std::string value_;
};
