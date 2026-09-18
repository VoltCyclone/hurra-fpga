// Portable, fixed-size 8x16 printable ASCII glyphs.

#ifndef HURRA_MCXN947_GLYPHS_H
#define HURRA_MCXN947_GLYPHS_H

#include <stdint.h>

#define GLYPH_WIDTH 8u
#define GLYPH_HEIGHT 16u
#define GLYPH_FIRST_CHAR ((uint8_t)' ')
#define GLYPH_LAST_CHAR ((uint8_t)'~')

extern const uint8_t g_glyphs_ascii[95][GLYPH_HEIGHT];
const uint8_t *glyphs_for_char(uint8_t character);

#endif  // HURRA_MCXN947_GLYPHS_H
