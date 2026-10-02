// ---- BRING-UP DIAGNOSTIC UART -- retained deliberately ----
//
// Minimal LPUART4 transmitter on the MCU-Link VCOM, so bring-up questions can
// be answered by reading registers out of the running target instead of by
// halting it. The LinkServer debugger stalls the boot ROM on attach, so every
// register read taken that way describes a device the debugger is holding
// rather than firmware that is running -- which cost real time on this step.
//
// LPUART4 / LP_FLEXCOMM4, P1_9 = FC4_P1 (TX), FRO12M at 12 MHz, 115200 8N1.
// From the FRDM board package: BOARD_DEBUG_UART_BASEADDR = LPUART4,
// BOARD_DEBUG_UART_CLK_ATTACH = kFRO12M_to_FLEXCOMM4.
//
// Deliberately NOT the step-4 console. That is TinyUSB CDC over J11 and is a
// product feature; this is scaffolding. It stays until that console exists:
// step 3's whole method is reading counters out of a RUNNING target, and the
// LinkServer debugger cannot do that -- it stalls the boot ROM on attach, so
// every register read taken that way describes a device the debugger is
// holding. The frame-offset finding was only nameable because this existed.

#include "dbg_uart.h"

#include <stdbool.h>

#if defined(MCXN947)

#include "fsl_clock.h"
#include "fsl_lpflexcomm.h"
#include "fsl_port.h"
#include "fsl_reset.h"

#define DBG_UART LPUART4
#define DBG_FC_INSTANCE 4u

static void dbg_uart_hw_init(void)
{
    CLOCK_SetClkDiv(kCLOCK_DivFlexcom4Clk, 1u);
    CLOCK_AttachClk(kFRO12M_to_FLEXCOMM4);
    RESET_ClearPeripheralReset(kFC4_RST_SHIFT_RSTn);

    CLOCK_EnableClock(kCLOCK_Port1);
    PORT_SetPinMux(PORT1, 9u, kPORT_MuxAlt2);  // P1_9 -> FC4_P1

    LP_FLEXCOMM_Init(DBG_FC_INSTANCE, LP_FLEXCOMM_PERIPH_LPUART);

    // 12 MHz / (OSR 13 x SBR 8) = 115385 baud, 0.16% from 115200.
    DBG_UART->BAUD = ((uint32_t)12u << 24) | 8u;
    DBG_UART->CTRL = LPUART_CTRL_TE_MASK;
}

static bool dbg_uart_hw_tx_ready(void)
{
    return (DBG_UART->STAT & LPUART_STAT_TDRE_MASK) != 0u;
}

static void dbg_uart_hw_tx_write(uint8_t byte)
{
    DBG_UART->DATA = (uint32_t)byte;
}

#elif defined(DBG_UART_TESTING)

void dbg_uart_test_hw_init(void);
bool dbg_uart_test_tx_ready(void);
void dbg_uart_test_tx_write(uint8_t byte);

static void dbg_uart_hw_init(void)
{
    dbg_uart_test_hw_init();
}

static bool dbg_uart_hw_tx_ready(void)
{
    return dbg_uart_test_tx_ready();
}

static void dbg_uart_hw_tx_write(uint8_t byte)
{
    dbg_uart_test_tx_write(byte);
}

#endif

#if defined(MCXN947) || defined(DBG_UART_TESTING)

_Static_assert((DBG_UART_TX_RING_SIZE & (DBG_UART_TX_RING_SIZE - 1u)) == 0u,
               "debug UART TX ring size must be a power of two");
_Static_assert(DBG_UART_TX_RING_SIZE <= 65536u,
               "debug UART TX ring indices must hold every offset");

static uint8_t s_tx_ring[DBG_UART_TX_RING_SIZE];
static uint16_t s_tx_head;
static uint16_t s_tx_tail;
static uint16_t s_tx_commit;
static uint32_t s_dropped_bytes;
static bool s_async;
static bool s_dropping_line;

static void dbg_uart_direct_putc(char c)
{
    while (!dbg_uart_hw_tx_ready()) {
    }
    dbg_uart_hw_tx_write((uint8_t)c);
}

static void dbg_uart_enqueue(char c)
{
    if (s_dropping_line) {
        s_dropped_bytes++;
        if (c == '\n') {
            s_dropping_line = false;
        }
        return;
    }

    const uint16_t next = (uint16_t)(
        ((uint32_t)s_tx_head + 1u) & (DBG_UART_TX_RING_SIZE - 1u));
    if (next == s_tx_tail) {
        // Nothing beyond s_tx_commit has reached the UART: poll only drains
        // complete lines. Rewind the partial line, account for every byte in
        // it plus this one, and suppress the remainder through its newline.
        s_dropped_bytes +=
            ((uint32_t)s_tx_head - (uint32_t)s_tx_commit) &
            (DBG_UART_TX_RING_SIZE - 1u);
        s_dropped_bytes++;
        s_tx_head = s_tx_commit;
        s_dropping_line = c != '\n';
        return;
    }
    s_tx_ring[s_tx_head] = (uint8_t)c;
    s_tx_head = next;
    if (c == '\n') {
        s_tx_commit = s_tx_head;
    }
}

void dbg_uart_init(void)
{
    dbg_uart_hw_init();
    s_tx_head = 0u;
    s_tx_tail = 0u;
    s_tx_commit = 0u;
    s_dropped_bytes = 0u;
    s_async = false;
    s_dropping_line = false;
}

void dbg_uart_async_enable(void)
{
    s_async = true;
}

void dbg_uart_poll(void)
{
    uint32_t sent = 0u;
    while (sent < DBG_UART_TX_DRAIN_BUDGET &&
           s_tx_tail != s_tx_commit && dbg_uart_hw_tx_ready()) {
        dbg_uart_hw_tx_write(s_tx_ring[s_tx_tail]);
        s_tx_tail = (uint16_t)(
            ((uint32_t)s_tx_tail + 1u) & (DBG_UART_TX_RING_SIZE - 1u));
        sent++;
    }
}

uint32_t dbg_uart_dropped_bytes(void)
{
    return s_dropped_bytes;
}

void dbg_putc(char c)
{
    if (s_async) {
        dbg_uart_enqueue(c);
    } else {
        dbg_uart_direct_putc(c);
    }
}

void dbg_puts(const char *s)
{
    while (*s != '\0') {
        if (*s == '\n') {
            dbg_putc('\r');
        }
        dbg_putc(*s++);
    }
}

void dbg_hex32(uint32_t v)
{
    static const char digits[] = "0123456789abcdef";
    dbg_puts("0x");
    for (int shift = 28; shift >= 0; shift -= 4) {
        dbg_putc(digits[(v >> shift) & 0xFu]);
    }
}

void dbg_uart_emergency_puts(const char *s)
{
    while (*s != '\0') {
        if (*s == '\n') {
            dbg_uart_direct_putc('\r');
        }
        dbg_uart_direct_putc(*s++);
    }
}

void dbg_uart_emergency_hex32(uint32_t v)
{
    static const char digits[] = "0123456789abcdef";
    dbg_uart_emergency_puts("0x");
    for (int shift = 28; shift >= 0; shift -= 4) {
        dbg_uart_direct_putc(digits[(v >> shift) & 0xFu]);
    }
}

// Decimal, because step 3's counters are compared against the FPGA's decimal
// register dump and against each other as rates; hex would mean arithmetic by
// hand on every sample.
void dbg_dec32(uint32_t v)
{
    char buf[10];
    uint32_t n = 0u;

    if (v == 0u) {
        dbg_putc('0');
        return;
    }
    while (v != 0u && n < sizeof(buf)) {
        buf[n++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (n != 0u) {
        dbg_putc(buf[--n]);
    }
}

void dbg_reg(const char *name, volatile const uint32_t *addr)
{
    dbg_puts("  ");
    dbg_puts(name);
    dbg_puts(" = ");
    dbg_hex32(*addr);
    dbg_puts("\n");
}

#endif  // MCXN947 || DBG_UART_TESTING
