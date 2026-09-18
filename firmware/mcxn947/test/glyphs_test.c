#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "glyphs.h"

static int glyph_has_ink(const uint8_t *glyph)
{
    for (uint8_t row = 0u; row < GLYPH_HEIGHT; ++row) {
        if (glyph[row] != 0u) {
            return 1;
        }
    }
    return 0;
}

int main(void)
{
    const uint8_t *const space = glyphs_for_char(' ');
    for (uint8_t row = 0u; row < GLYPH_HEIGHT; ++row) {
        assert(space[row] == 0u);
    }

    for (uint8_t character = GLYPH_FIRST_CHAR;
         character <= GLYPH_LAST_CHAR; ++character) {
        const uint8_t *const glyph = glyphs_for_char(character);
        assert(glyph != NULL);
        if (character != (uint8_t)' ') {
            assert(glyph_has_ink(glyph));
        }
    }

    assert(glyphs_for_char(0u) == glyphs_for_char((uint8_t)'?'));
    assert(glyphs_for_char(0x7fu) == glyphs_for_char((uint8_t)'?'));
    assert(glyphs_for_char((uint8_t)'0') != glyphs_for_char((uint8_t)'1'));
    printf("glyphs_test: ok\n");
    return 0;
}
