// CPU1-only LCD-PAR-S035 transport plus portable rectangle validation.

#ifndef HURRA_MCXN947_DISPLAY_PANEL_H
#define HURRA_MCXN947_DISPLAY_PANEL_H

#include <stdbool.h>
#include <stdint.h>

#define DISPLAY_PANEL_WIDTH 480u
#define DISPLAY_PANEL_HEIGHT 320u
#define DISPLAY_PANEL_BUS_WIDTH 16u

// R34 / R28 (USB1_OTG_PWR / OC on P4_16 / P4_17) must stay DNP. Populating
// either resistor steals FLEXIO0_D24/D25 and makes this 16-bit bus invalid.

// Section 10: 6 is inherited from the 66 ns ILI9341 tWC calculation, yielding
// a 12.5 MHz WR at the 150 MHz FlexIO clock. It is NOT confirmed against the
// LCD-PAR-S035 ST7796S datasheet; raise this one constant if the panel needs a
// slower write cycle.
#define DISPLAY_FLEXIO_BAUD_DIV 6u

bool display_panel_rect(uint16_t x, uint16_t y, uint16_t width,
                        uint16_t height, uint16_t *end_x, uint16_t *end_y,
                        uint32_t *pixel_count);

bool display_panel_init(void);
bool display_panel_blit(uint16_t x, uint16_t y, uint16_t width,
                        uint16_t height, const uint16_t *rgb565);
bool display_panel_blit_busy(void);
bool display_panel_ok(void);

#endif  // HURRA_MCXN947_DISPLAY_PANEL_H
