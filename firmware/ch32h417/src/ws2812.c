#include "ws2812.h"

/* WS2812 waveform driver over the CH32H417 PIOC. Carried over from the proven
 * Hurra-v3 driver (itself the WCH RGB1W example): PIOC bringup on PF13/IO1, the
 * busy-drop RAM-mode send, and the GRB wire order. The relay-specific color
 * policy is NOT carried over -- status_led owns color and animation and hands
 * this a finished GRB frame. MMIO-only, so the whole file is behind the target
 * guard. */
#if defined(CH32H417)

#include <string.h>

#include "ch32h417_port.h"
#include "ch32h417_pioc.h"

/* RAM-mode command bit, and the code-RAM slot the waveform reads its 3 GRB
 * bytes from (PIOC_SRAM_BASE + 0x400). The ~0x480-byte program overruns +0x400,
 * so this overlap is safe ONLY for a 3-byte frame -- do not lengthen it without
 * relocating the buffer. */
#define WS2812_CMD_RAM  0x80u
#define WS2812_RAM_ADDR ((uint8_t *)(PIOC_SRAM_BASE + 0x400))

extern const uint8_t ws2812_pioc_code[];
extern const unsigned ws2812_pioc_code_len;

void ws2812_init(void)
{
    GPIO_InitTypeDef gpio = {0};

    /* GPIOF + AFIO, then PIOC. The PIOC RCC enable is a read-modify-write on a
     * register shared with USBHS/DMA1 enables; safe only because PIOC is enabled
     * once on the V5F before the service loop and the V3F never touches it. */
    RCC_HB2PeriphClockCmd(RCC_HB2Periph_AFIO | RCC_HB2Periph_GPIOF, ENABLE);
    RCC_HBPeriphClockCmd(RCC_HBPeriph_PIOC, ENABLE);

    GPIO_PinAFConfig(GPIOF, GPIO_PinSource13, GPIO_AF5);
    gpio.GPIO_Pin = GPIO_Pin_13;
    gpio.GPIO_Mode = GPIO_Mode_AF_PP;
    gpio.GPIO_Speed = GPIO_Speed_Very_High;
    GPIO_Init(GPIOF, &gpio);

    /* Reset PIOC, enable both IO switches, and load the waveform program. */
    PIOC->D8_SYS_CFG = (uint8_t)(RB_MST_RESET | RB_MST_IO_EN0 | RB_MST_IO_EN1);
    memcpy((void *)PIOC_SRAM_BASE, ws2812_pioc_code, ws2812_pioc_code_len);
}

void ws2812_send_grb(const uint8_t grb[3])
{
    /* Drop if PIOC has not yet read the previous master-written command. */
    if ((PIOC->D8_SYS_CFG & RB_DATA_MW_SR) != 0u) {
        return;
    }

    PIOC->D8_SYS_CFG =
        (uint8_t)(RB_MST_RESET | RB_MST_IO_EN0 | RB_MST_IO_EN1); /* halt */
    memcpy(WS2812_RAM_ADDR, grb, 3);                             /* load */
    PIOC->D8_SYS_CFG =
        (uint8_t)(RB_MST_CLK_GATE | RB_MST_IO_EN0 | RB_MST_IO_EN1); /* run */
    PIOC->D16_DATA_REG0_1 = 3u;                                  /* nbytes */
    PIOC->D8_DATA_REG2 = 1u;                                     /* IO1 */
    PIOC->D8_CTRL_WR = (uint8_t)(0x55u | WS2812_CMD_RAM);        /* start */
}

#endif /* CH32H417 */
