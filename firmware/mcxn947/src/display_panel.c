// Geometry is portable. The single trailing guard owns all clocks, pins,
// FlexIO0, eDMA1, DBI and ST7796S state.

#include "display_panel.h"

bool display_panel_rect(uint16_t x, uint16_t y, uint16_t width,
                        uint16_t height, uint16_t *end_x, uint16_t *end_y,
                        uint32_t *pixel_count)
{
    if (width == 0u || height == 0u || x >= DISPLAY_PANEL_WIDTH ||
        y >= DISPLAY_PANEL_HEIGHT) {
        return false;
    }

    const uint32_t x_end = (uint32_t)x + (uint32_t)width - 1u;
    const uint32_t y_end = (uint32_t)y + (uint32_t)height - 1u;
    if (x_end >= DISPLAY_PANEL_WIDTH || y_end >= DISPLAY_PANEL_HEIGHT) {
        return false;
    }

    *end_x = (uint16_t)x_end;
    *end_y = (uint16_t)y_end;
    *pixel_count = (uint32_t)width * (uint32_t)height;
    return true;
}

#if defined(MCXN947)

#include <stddef.h>

#include "fsl_clock.h"
#include "fsl_dbi.h"
#include "fsl_dbi_flexio_edma.h"
#include "fsl_edma.h"
#include "fsl_flexio_mculcd.h"
#include "fsl_gpio.h"
#include "fsl_port.h"
#include "fsl_st7796s.h"

#define DISPLAY_FLEXIO_CLOCK_HZ 150000000u
#define DISPLAY_FLEXIO_AGGREGATE_BAUD \
    ((DISPLAY_FLEXIO_CLOCK_HZ / (2u * DISPLAY_FLEXIO_BAUD_DIV)) * \
     DISPLAY_PANEL_BUS_WIDTH)
#define DISPLAY_DMA_CHANNEL 0u

#define DISPLAY_CS_GPIO GPIO0
#define DISPLAY_CS_PIN 12u
#define DISPLAY_DC_GPIO GPIO0
#define DISPLAY_DC_PIN 7u
#define DISPLAY_RST_GPIO GPIO4
#define DISPLAY_RST_PIN 7u

_Static_assert(DISPLAY_FLEXIO_CLOCK_HZ % (2u * DISPLAY_FLEXIO_BAUD_DIV) == 0u,
               "FlexIO clock must divide exactly to the requested WR rate");

static void display_cs_set(bool high);
static void display_dc_set(bool high);

static FLEXIO_MCULCD_Type s_flexio_lcd = {
    .flexioBase = FLEXIO0,
    .busType = kFLEXIO_MCULCD_8080,
    .dataPinStartIndex = 16u,
    .ENWRPinIndex = 1u,
    .RDPinIndex = 0u,
    .txShifterStartIndex = 0u,
    .txShifterEndIndex = 7u,
    .rxShifterStartIndex = 0u,
    .rxShifterEndIndex = 7u,
    .timerIndex = 0u,
    .setCSPin = display_cs_set,
    .setRSPin = display_dc_set,
    .setRDWRPin = NULL,
};

static edma_handle_t s_tx_dma;
static flexio_mculcd_edma_handle_t s_flexio_handle;
static dbi_iface_t s_dbi_iface;
static st7796s_handle_t s_panel;
static volatile bool s_blit_busy;
static volatile bool s_panel_failed;
static bool s_panel_ready;

// Normally fsl_edma_soc.c supplies the IRQ wrappers and privately declares
// this transactional dispatcher. Step 7 intentionally does not vendor that
// broad SoC wrapper file, so declare the one SDK entry point our owned wrapper
// needs instead of importing handlers for DMA channels owned by neither core.
extern void EDMA_DriverIRQHandler(uint32_t instance, uint32_t channel);

static void display_cs_set(bool high)
{
    if (high) {
        GPIO_PortSet(DISPLAY_CS_GPIO, 1u << DISPLAY_CS_PIN);
    } else {
        GPIO_PortClear(DISPLAY_CS_GPIO, 1u << DISPLAY_CS_PIN);
    }
}

static void display_dc_set(bool high)
{
    if (high) {
        GPIO_PortSet(DISPLAY_DC_GPIO, 1u << DISPLAY_DC_PIN);
    } else {
        GPIO_PortClear(DISPLAY_DC_GPIO, 1u << DISPLAY_DC_PIN);
    }
}

// fsl_st7796s.c needs this SDK primitive, but step 7 deliberately did not
// vendor fsl_common_arm.c. A conservative cycle-count loop is sufficient for
// reset/sleep minimums; running long is harmless and cannot involve CPU0.
void SDK_DelayAtLeastUs(uint32_t delay_time_us, uint32_t core_clock_hz)
{
    uint64_t cycles = ((uint64_t)delay_time_us * (uint64_t)core_clock_hz) /
                      UINT64_C(1000000);
    while (cycles != UINT64_C(0)) {
        __asm__ volatile("nop");
        --cycles;
    }
}

static void display_memory_done(status_t status, void *user_data)
{
    (void)user_data;
    if (status != kStatus_Success) {
        s_panel_failed = true;
    }
    s_blit_busy = false;
}

void EDMA_1_CH0_DriverIRQHandler(void)
{
    // fsl_edma_soc.c is deliberately absent, so the one channel this image
    // owns gets the exact strong Driver handler required by section 7.
    EDMA_DriverIRQHandler(1u, DISPLAY_DMA_CHANNEL);
}

static void display_pins_init(void)
{
    CLOCK_EnableClock(kCLOCK_Port0);
    CLOCK_EnableClock(kCLOCK_Port2);
    CLOCK_EnableClock(kCLOCK_Port4);
    CLOCK_EnableClock(kCLOCK_Gpio0);
    CLOCK_EnableClock(kCLOCK_Gpio4);

    // CS, D/C, WR and RD share Port 0 with CPU0's green LED on P0_27. These
    // PinInit/PCR operations are RMW and are safe only here: section 4(a)
    // releases CPU1 after CPU0 has completed every pin/clock setup. Never
    // remux or reinitialize a Port 0 display pin after this boot-only block.
    PORT_SetPinMux(PORT0, 7u, kPORT_MuxAlt0);
    PORT_SetPinMux(PORT0, 8u, kPORT_MuxAlt6);
    PORT_SetPinMux(PORT0, 9u, kPORT_MuxAlt6);
    PORT_SetPinMux(PORT0, 12u, kPORT_MuxAlt0);
    PORT_SetPinMux(PORT2, 8u, kPORT_MuxAlt6);
    PORT_SetPinMux(PORT2, 9u, kPORT_MuxAlt6);
    PORT_SetPinMux(PORT2, 10u, kPORT_MuxAlt6);
    PORT_SetPinMux(PORT2, 11u, kPORT_MuxAlt6);
    for (uint32_t pin = 12u; pin <= 23u; ++pin) {
        PORT_SetPinMux(PORT4, pin, kPORT_MuxAlt6);
    }
    PORT_SetPinMux(PORT4, 7u, kPORT_MuxAlt0);

    const gpio_pin_config_t output_high = {
        .pinDirection = kGPIO_DigitalOutput,
        .outputLogic = 1u,
    };
    GPIO_PinInit(DISPLAY_CS_GPIO, DISPLAY_CS_PIN, &output_high);
    GPIO_PinInit(DISPLAY_DC_GPIO, DISPLAY_DC_PIN, &output_high);
    GPIO_PinInit(DISPLAY_RST_GPIO, DISPLAY_RST_PIN, &output_high);
}

static bool display_transport_init(void)
{
    CLOCK_SetClkDiv(kCLOCK_DivFlexioClk, 1u);
    CLOCK_AttachClk(kPLL0_to_FLEXIO);
    if (CLOCK_GetFlexioClkFreq() != DISPLAY_FLEXIO_CLOCK_HZ) {
        return false;
    }

    flexio_mculcd_config_t flexio_config;
    FLEXIO_MCULCD_GetDefaultConfig(&flexio_config);
    flexio_config.baudRate_Bps = DISPLAY_FLEXIO_AGGREGATE_BAUD;
    if (FLEXIO_MCULCD_Init(&s_flexio_lcd, &flexio_config,
                           DISPLAY_FLEXIO_CLOCK_HZ) != kStatus_Success) {
        return false;
    }

    edma_config_t dma_config;
    EDMA_GetDefaultConfig(&dma_config);
    EDMA_Init(DMA1, &dma_config);
    EDMA_CreateHandle(&s_tx_dma, DMA1, DISPLAY_DMA_CHANNEL);
    EDMA_SetChannelMux(DMA1, DISPLAY_DMA_CHANNEL,
                       kDma1RequestMuxFlexIO0ShiftRegister0Request);

    if (DBI_FLEXIO_EDMA_CreateHandle(&s_dbi_iface, &s_flexio_lcd,
                                     &s_flexio_handle, &s_tx_dma,
                                     NULL) != kStatus_Success) {
        return false;
    }
    DBI_IFACE_SetMemoryDoneCallback(&s_dbi_iface, display_memory_done, NULL);
    return true;
}

bool display_panel_init(void)
{
    s_panel_ready = false;
    s_panel_failed = false;
    s_blit_busy = false;

    display_pins_init();
    GPIO_PortClear(DISPLAY_RST_GPIO, 1u << DISPLAY_RST_PIN);
    SDK_DelayAtLeastUs(1u, DISPLAY_FLEXIO_CLOCK_HZ);
    GPIO_PortSet(DISPLAY_RST_GPIO, 1u << DISPLAY_RST_PIN);
    SDK_DelayAtLeastUs(5000u, DISPLAY_FLEXIO_CLOCK_HZ);

    if (!display_transport_init()) {
        s_panel_failed = true;
        return false;
    }

    const st7796s_config_t panel_config = {
        .driverPreset = kST7796S_DriverPresetLCDPARS035,
        .pixelFormat = kST7796S_PixelFormatRGB565,
        .orientationMode = kST7796S_Orientation270,
        .teConfig = kST7796S_TEDisabled,
        .invertDisplay = true,
        .flipDisplay = true,
        .bgrFilter = true,
    };
    if (ST7796S_Init(&s_panel, &panel_config, &s_dbi_iface) !=
            kStatus_Success ||
        ST7796S_EnableDisplay(&s_panel, true) != kStatus_Success) {
        s_panel_failed = true;
        return false;
    }

    s_panel_ready = true;
    return true;
}

bool display_panel_blit(uint16_t x, uint16_t y, uint16_t width,
                        uint16_t height, const uint16_t *rgb565)
{
    uint16_t end_x;
    uint16_t end_y;
    uint32_t pixel_count;
    if (!s_panel_ready || s_panel_failed || s_blit_busy || rgb565 == NULL ||
        !display_panel_rect(x, y, width, height, &end_x, &end_y,
                            &pixel_count)) {
        return false;
    }

    if (ST7796S_SelectArea(&s_panel, x, y, end_x, end_y) != kStatus_Success) {
        s_panel_failed = true;
        return false;
    }

    s_blit_busy = true;
    if (ST7796S_WritePixels(&s_panel, (uint16_t *)(uintptr_t)rgb565,
                            pixel_count) != kStatus_Success) {
        s_blit_busy = false;
        s_panel_failed = true;
        return false;
    }
    return true;
}

bool display_panel_blit_busy(void)
{
    return s_blit_busy;
}

bool display_panel_ok(void)
{
    return s_panel_ready && !s_panel_failed;
}

#endif  // MCXN947
