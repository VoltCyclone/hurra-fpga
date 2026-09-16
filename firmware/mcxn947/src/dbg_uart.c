// ---- TEMPORARY STEP-2 BRING-UP DIAGNOSTIC -- REMOVE BEFORE COMMIT ----
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
// product feature; this is scaffolding and leaves with the probe.

#if defined(MCXN947)

#include "dbg_uart.h"

#include "fsl_clock.h"
#include "fsl_lpflexcomm.h"
#include "fsl_port.h"
#include "fsl_reset.h"

#define DBG_UART LPUART4
#define DBG_FC_INSTANCE 4u

void dbg_uart_init(void)
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

void dbg_putc(char c)
{
    while ((DBG_UART->STAT & LPUART_STAT_TDRE_MASK) == 0u) {
    }
    DBG_UART->DATA = (uint32_t)(uint8_t)c;
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

void dbg_reg(const char *name, volatile const uint32_t *addr)
{
    dbg_puts("  ");
    dbg_puts(name);
    dbg_puts(" = ");
    dbg_hex32(*addr);
    dbg_puts("\n");
}

#endif  // MCXN947
