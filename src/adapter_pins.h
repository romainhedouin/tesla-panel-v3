// HUB75 pin mapping for the seengreat "RGB Matrix Adapter Board (E)"
// (https://www.amazon.fr/dp/B0FVGCF1RW), ESP32-DevKitC V4 side, board
// revision 2.2 (V2.x per https://seengreat.com/wiki/186/rgb-matrix-adapter-board-e).
// Overrides the ESP32-HUB75-MatrixPanel-I2S-DMA library's defaults, which
// don't match this adapter's wiring at all.
#pragma once

#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>

inline HUB75_I2S_CFG::i2s_pins AdapterPinsV2() {
  return {
      /*r1=*/18, /*g1=*/17, /*b1=*/19,
      /*r2=*/21, /*g2=*/23, /*b2=*/27,
      /*a=*/26, /*b=*/16, /*c=*/25, /*d=*/4, /*e=*/22,
      /*lat=*/2, /*oe=*/32, /*clk=*/33,
  };
}
