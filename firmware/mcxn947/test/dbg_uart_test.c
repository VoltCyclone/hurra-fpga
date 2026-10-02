#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "dbg_uart.h"

static bool g_tx_ready;
static uint8_t g_tx[2048];
static uint32_t g_tx_count;

void dbg_uart_test_hw_init(void)
{
}

bool dbg_uart_test_tx_ready(void)
{
    return g_tx_ready;
}

void dbg_uart_test_tx_write(uint8_t byte)
{
    assert(g_tx_count < sizeof(g_tx));
    g_tx[g_tx_count++] = byte;
}

static void reset(bool ready)
{
    g_tx_ready = ready;
    g_tx_count = 0u;
    memset(g_tx, 0, sizeof(g_tx));
    dbg_uart_init();
}

static void test_boot_output_is_direct_and_blocking(void)
{
    reset(true);
    dbg_puts("boot\n");
    assert(g_tx_count == 6u);
    assert(memcmp(g_tx, "boot\r\n", 6u) == 0);
}

static void test_async_output_is_queued_and_drain_is_bounded(void)
{
    reset(false);
    assert(dbg_uart_dropped_bytes() == 0u);
    dbg_uart_async_enable();
    for (uint32_t i = 0u; i < 39u; ++i) {
        dbg_putc((char)('A' + (i % 26u)));
    }
    dbg_putc('\n');
    assert(g_tx_count == 0u);

    g_tx_ready = true;
    dbg_uart_poll();
    assert(g_tx_count == DBG_UART_TX_DRAIN_BUDGET);
    for (uint32_t i = 0u; i < g_tx_count; ++i) {
        assert(g_tx[i] == (uint8_t)('A' + (i % 26u)));
    }
}

static void test_emergency_output_bypasses_the_ring(void)
{
    reset(true);
    dbg_uart_async_enable();
    dbg_puts("queued\n");
    assert(g_tx_count == 0u);

    dbg_uart_emergency_puts("FAULT\n");
    assert(g_tx_count == 7u);
    assert(memcmp(g_tx, "FAULT\r\n", 7u) == 0);

    dbg_uart_poll();
    assert(g_tx_count == 15u);
    assert(memcmp(&g_tx[7], "queued\r\n", 8u) == 0);
}

static void test_overflow_discards_the_whole_line(void)
{
    reset(false);
    dbg_uart_async_enable();
    for (uint32_t i = 0u; i < 1000u; ++i) {
        dbg_putc('a');
    }
    dbg_puts("\n");
    for (uint32_t i = 0u; i < 30u; ++i) {
        dbg_putc('x');
    }
    dbg_puts("\n");
    dbg_puts("OK\n");
    assert(g_tx_count == 0u);
    assert(dbg_uart_dropped_bytes() == 32u);

    g_tx_ready = true;
    for (uint32_t i = 0u; i < DBG_UART_TX_RING_SIZE; ++i) {
        dbg_uart_poll();
    }
    assert(g_tx_count == 1006u);
    for (uint32_t i = 0u; i < 1000u; ++i) {
        assert(g_tx[i] == (uint8_t)'a');
    }
    assert(memcmp(&g_tx[1000], "\r\nOK\r\n", 6u) == 0);
}

int main(void)
{
    test_boot_output_is_direct_and_blocking();
    test_async_output_is_queued_and_drain_is_bounded();
    test_emergency_output_bypasses_the_ring();
    test_overflow_discards_the_whole_line();
    printf("dbg_uart_test: ok\n");
    return 0;
}
