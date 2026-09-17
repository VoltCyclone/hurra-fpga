// Portable 60x20 character/attribute model for the 480x320, 8x16 display.

#ifndef HURRA_MCXN947_TEXT_GRID_H
#define HURRA_MCXN947_TEXT_GRID_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define TEXT_GRID_COLS 60u
#define TEXT_GRID_ROWS 20u

typedef struct {
    uint8_t character;
    uint8_t attr;
} text_grid_cell_t;

typedef struct {
    uint8_t row;
    uint8_t start_col;
    uint8_t length;
} text_grid_run_t;

typedef struct {
    text_grid_cell_t cells[TEXT_GRID_ROWS][TEXT_GRID_COLS];
    uint64_t dirty[TEXT_GRID_ROWS];
} text_grid_t;

void text_grid_init(text_grid_t *grid, char fill, uint8_t attr);
bool text_grid_set_cell(text_grid_t *grid, uint8_t row, uint8_t col,
                        char character, uint8_t attr);
size_t text_grid_put(text_grid_t *grid, uint8_t row, uint8_t col,
                     const char *text, uint8_t attr);
void text_grid_fill_row(text_grid_t *grid, uint8_t row, char fill,
                        uint8_t attr);
void text_grid_mark_all_dirty(text_grid_t *grid);
bool text_grid_take_dirty_run(text_grid_t *grid, text_grid_run_t *run);
const text_grid_cell_t *text_grid_cell_at(const text_grid_t *grid, uint8_t row,
                                          uint8_t col);

#endif  // HURRA_MCXN947_TEXT_GRID_H
