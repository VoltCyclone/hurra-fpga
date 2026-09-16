// Bring-up diagnostic UART. Retained deliberately through step 3. See dbg_uart.c.
#ifndef DBG_UART_H
#define DBG_UART_H

#include <stdint.h>

void dbg_uart_init(void);
void dbg_putc(char c);
void dbg_puts(const char *s);
void dbg_hex32(uint32_t v);
void dbg_dec32(uint32_t v);
void dbg_reg(const char *name, volatile const uint32_t *addr);

#endif  // DBG_UART_H
