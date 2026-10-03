// Bring-up diagnostic UART. Retained deliberately through step 3. See dbg_uart.c.
#ifndef DBG_UART_H
#define DBG_UART_H

#include <stdint.h>

#define DBG_UART_TX_RING_SIZE 1024u
#define DBG_UART_TX_DRAIN_BUDGET 16u

void dbg_uart_init(void);
void dbg_uart_async_enable(void);
void dbg_uart_poll(void);
// Bytes discarded when an asynchronous line cannot fit. The entire line is
// withheld from the UART, including any prefix already staged in the ring.
uint32_t dbg_uart_dropped_bytes(void);
void dbg_putc(char c);
void dbg_puts(const char *s);
void dbg_hex32(uint32_t v);
void dbg_dec32(uint32_t v);
void dbg_reg(const char *name, volatile const uint32_t *addr);

// Architectural faults never return to the foreground loop, so these bypass
// the TX ring and spin on the hardware directly. They deliberately ignore any
// queued normal diagnostics: the fault record is the output that must survive.
void dbg_uart_emergency_puts(const char *s);
void dbg_uart_emergency_hex32(uint32_t v);

#endif  // DBG_UART_H
