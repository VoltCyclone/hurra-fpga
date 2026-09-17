#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "text_grid.h"

static text_grid_t clean_grid(void)
{
    text_grid_t grid;
    text_grid_init(&grid, ' ', 0u);
    return grid;
}

static void test_same_text_twice_has_no_second_run(void)
{
    text_grid_t grid = clean_grid();
    text_grid_run_t run;

    assert(text_grid_put(&grid, 3u, 4u, "slot", 1u) == 4u);
    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 3u);
    assert(run.start_col == 4u);
    assert(run.length == 4u);
    assert(!text_grid_take_dirty_run(&grid, &run));

    assert(text_grid_put(&grid, 3u, 4u, "slot", 1u) == 4u);
    assert(!text_grid_take_dirty_run(&grid, &run));
}

static void test_single_changed_cell_is_one_cell_run(void)
{
    text_grid_t grid = clean_grid();
    text_grid_run_t run;

    assert(text_grid_set_cell(&grid, 7u, 19u, 'X', 2u));
    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 7u);
    assert(run.start_col == 19u);
    assert(run.length == 1u);
    assert(!text_grid_take_dirty_run(&grid, &run));
}

static void test_adjacent_changed_cells_coalesce(void)
{
    text_grid_t grid = clean_grid();
    text_grid_run_t run;

    assert(text_grid_set_cell(&grid, 9u, 20u, 'A', 3u));
    assert(text_grid_set_cell(&grid, 9u, 21u, 'B', 3u));
    assert(text_grid_set_cell(&grid, 9u, 22u, 'C', 3u));
    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 9u);
    assert(run.start_col == 20u);
    assert(run.length == 3u);
    assert(!text_grid_take_dirty_run(&grid, &run));
}

static void test_clean_gap_splits_runs(void)
{
    text_grid_t grid = clean_grid();
    text_grid_run_t run;

    assert(text_grid_set_cell(&grid, 11u, 10u, 'L', 4u));
    assert(text_grid_set_cell(&grid, 11u, 12u, 'R', 4u));

    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 11u);
    assert(run.start_col == 10u);
    assert(run.length == 1u);

    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 11u);
    assert(run.start_col == 12u);
    assert(run.length == 1u);
    assert(!text_grid_take_dirty_run(&grid, &run));
}

static void test_full_width_change_is_one_run(void)
{
    text_grid_t grid = clean_grid();
    text_grid_run_t run;

    text_grid_fill_row(&grid, 19u, '#', 5u);
    assert(text_grid_take_dirty_run(&grid, &run));
    assert(run.row == 19u);
    assert(run.start_col == 0u);
    assert(run.length == TEXT_GRID_COLS);
    assert(!text_grid_take_dirty_run(&grid, &run));
}

int main(void)
{
    test_same_text_twice_has_no_second_run();
    test_single_changed_cell_is_one_cell_run();
    test_adjacent_changed_cells_coalesce();
    test_clean_gap_splits_runs();
    test_full_width_change_is_one_run();
    printf("text_grid_test: ok\n");
    return 0;
}
