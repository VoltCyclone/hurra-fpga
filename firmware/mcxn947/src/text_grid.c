// Portable model first and only: this module has no target guard or vendor API.

#include "text_grid.h"

#define TEXT_GRID_ALL_DIRTY ((UINT64_C(1) << TEXT_GRID_COLS) - UINT64_C(1))

void text_grid_init(text_grid_t *grid, char fill, uint8_t attr)
{
    const uint8_t character = (uint8_t)fill;

    for (uint8_t row = 0u; row < TEXT_GRID_ROWS; ++row) {
        for (uint8_t col = 0u; col < TEXT_GRID_COLS; ++col) {
            grid->cells[row][col].character = character;
            grid->cells[row][col].attr = attr;
        }
        grid->dirty[row] = UINT64_C(0);
    }
}

bool text_grid_set_cell(text_grid_t *grid, uint8_t row, uint8_t col,
                        char character, uint8_t attr)
{
    if (row >= TEXT_GRID_ROWS || col >= TEXT_GRID_COLS) {
        return false;
    }

    text_grid_cell_t *const cell = &grid->cells[row][col];
    const uint8_t value = (uint8_t)character;
    if (cell->character != value || cell->attr != attr) {
        cell->character = value;
        cell->attr = attr;
        grid->dirty[row] |= UINT64_C(1) << col;
    }
    return true;
}

size_t text_grid_put(text_grid_t *grid, uint8_t row, uint8_t col,
                     const char *text, uint8_t attr)
{
    size_t written = 0u;

    if (row >= TEXT_GRID_ROWS || col >= TEXT_GRID_COLS) {
        return 0u;
    }

    while (*text != '\0' && col < TEXT_GRID_COLS) {
        (void)text_grid_set_cell(grid, row, col, *text, attr);
        ++text;
        ++col;
        ++written;
    }
    return written;
}

void text_grid_fill_row(text_grid_t *grid, uint8_t row, char fill,
                        uint8_t attr)
{
    if (row >= TEXT_GRID_ROWS) {
        return;
    }

    for (uint8_t col = 0u; col < TEXT_GRID_COLS; ++col) {
        (void)text_grid_set_cell(grid, row, col, fill, attr);
    }
}

void text_grid_mark_all_dirty(text_grid_t *grid)
{
    for (uint8_t row = 0u; row < TEXT_GRID_ROWS; ++row) {
        grid->dirty[row] = TEXT_GRID_ALL_DIRTY;
    }
}

bool text_grid_take_dirty_run(text_grid_t *grid, text_grid_run_t *run)
{
    for (uint8_t row = 0u; row < TEXT_GRID_ROWS; ++row) {
        const uint64_t bits = grid->dirty[row];
        if (bits == UINT64_C(0)) {
            continue;
        }

        uint8_t start = 0u;
        while ((bits & (UINT64_C(1) << start)) == UINT64_C(0)) {
            ++start;
        }

        uint8_t end = start;
        while (end < TEXT_GRID_COLS &&
               (bits & (UINT64_C(1) << end)) != UINT64_C(0)) {
            ++end;
        }

        const uint8_t length = (uint8_t)(end - start);
        const uint64_t mask = ((UINT64_C(1) << length) - UINT64_C(1)) << start;
        grid->dirty[row] &= ~mask;
        run->row = row;
        run->start_col = start;
        run->length = length;
        return true;
    }

    return false;
}

const text_grid_cell_t *text_grid_cell_at(const text_grid_t *grid, uint8_t row,
                                          uint8_t col)
{
    if (row >= TEXT_GRID_ROWS || col >= TEXT_GRID_COLS) {
        return NULL;
    }
    return &grid->cells[row][col];
}
