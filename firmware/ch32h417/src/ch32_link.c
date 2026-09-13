#include "ch32_link.h"

#include <string.h>

#include "spi_frame.h"

/* --- Pure transport state (MMIO-free; host-testable). --- */

static inj_frame_t s_rx_ring[CH32_LINK_RING_SIZE];
static inj_frame_t s_tx_ring[CH32_LINK_RING_SIZE];
static uint8_t s_rx_head; /* Producer: retire writes here. */
static uint8_t s_rx_tail; /* Consumer: receive reads here. */
static uint8_t s_tx_head; /* Producer: submit writes here. */
static uint8_t s_tx_tail; /* Consumer: fill reads here. */

static uint8_t s_rx_last_seq; /* Last accepted RX sequence. */
static bool s_rx_have_last;   /* False until the first deliverable frame. */
static uint8_t s_tx_seq;      /* Monotonic sequence stamped onto sent frames. */

static ch32_link_counters_t s_counters;

static uint32_t s_health_last_slots;
static uint32_t s_health_last_ms;
static bool s_health_seen;

static bool ring_full(uint8_t head, uint8_t tail)
{
    return (uint8_t)((head + 1u) & CH32_LINK_RING_MASK) == tail;
}

void ch32_link_reset(void)
{
    s_rx_head = 0u;
    s_rx_tail = 0u;
    s_tx_head = 0u;
    s_tx_tail = 0u;
    s_rx_last_seq = 0u;
    s_rx_have_last = false;
    s_tx_seq = 0u;
    s_health_last_slots = 0u;
    s_health_last_ms = 0u;
    s_health_seen = false;
    memset(&s_counters, 0, sizeof(s_counters));
}

void ch32_link_retire_slot(const uint8_t slot[INJ_FRAME_SIZE])
{
    uint8_t type = 0u;
    uint8_t sequence = 0u;
    uint8_t length = 0u;
    const uint8_t *payload = NULL;
    spi_frame_seq_class_t disposition;
    inj_frame_t frame;

    s_counters.slots++;

    switch (spi_frame_unpack(slot, &type, &sequence, &payload, &length)) {
    case SPI_FRAME_ERR_SOF:
        s_counters.bad_sof++;
        return;
    case SPI_FRAME_ERR_LEN:
        s_counters.bad_length++;
        return;
    case SPI_FRAME_ERR_CRC:
        s_counters.bad_crc++;
        return;
    case SPI_FRAME_ERR_TYPE:
        s_counters.bad_type++;
        return;
    case SPI_FRAME_IDLE:
        /* A well-formed keepalive: it is a slot, but it is not sequenced and not
         * delivered. Feeding its hardwired sequence 0 into the classifier would
         * make sparse real traffic read as almost entirely stale. */
        return;
    case SPI_FRAME_OK:
        break;
    }

    /* The first deliverable frame of a session is accepted unconditionally; only
     * once a window exists does the classifier decide. */
    if (!s_rx_have_last) {
        disposition = SPI_FRAME_SEQ_NEXT;
    } else {
        disposition = spi_frame_seq_classify(s_rx_last_seq, sequence, NULL);
    }

    switch (disposition) {
    case SPI_FRAME_SEQ_DUPLICATE:
        s_counters.duplicate++;
        return; /* Drain, do not act, do not advance. */
    case SPI_FRAME_SEQ_STALE:
        s_counters.stale++;
        return; /* Drain, never move the window backward. */
    case SPI_FRAME_SEQ_GAP:
        s_counters.sequence_gap++;
        break; /* Act and advance. */
    case SPI_FRAME_SEQ_NEXT:
        break; /* Act and advance. */
    }

    s_rx_last_seq = sequence;
    s_rx_have_last = true;

    frame.type = type;
    frame.sequence = sequence;
    frame.length = length;
    memcpy(frame.payload, payload, INJ_FRAME_PAYLOAD_SIZE);

    /* A full RX ring drops this frame rather than overwrite an unread one; the
     * window has already advanced, so the loss is bounded to one slot and the
     * next in-order frame still classifies as NEXT. */
    if (!ring_full(s_rx_head, s_rx_tail)) {
        s_rx_ring[s_rx_head] = frame;
        s_rx_head = (uint8_t)((s_rx_head + 1u) & CH32_LINK_RING_MASK);
    }
}

bool ch32_link_receive(inj_frame_t *out)
{
    if (s_rx_head == s_rx_tail) {
        return false;
    }
    *out = s_rx_ring[s_rx_tail];
    s_rx_tail = (uint8_t)((s_rx_tail + 1u) & CH32_LINK_RING_MASK);
    return true;
}

bool ch32_link_submit(uint8_t type, const void *payload, uint8_t length)
{
    inj_frame_t frame;

    if (!inj_type_is_known(type)) {
        return false;
    }
    if (length != spi_frame_expected_payload_length(type)) {
        return false;
    }
    if (length != 0u && payload == NULL) {
        return false;
    }
    if (ring_full(s_tx_head, s_tx_tail)) {
        return false;
    }

    frame.type = type;
    frame.sequence = 0u; /* Assigned at fill time so on-wire order is send order. */
    frame.length = length;
    memset(frame.payload, 0, sizeof(frame.payload));
    if (length != 0u) {
        memcpy(frame.payload, payload, length);
    }

    s_tx_ring[s_tx_head] = frame;
    s_tx_head = (uint8_t)((s_tx_head + 1u) & CH32_LINK_RING_MASK);
    return true;
}

void ch32_link_fill_tx_slot(uint8_t slot[INJ_FRAME_SIZE])
{
    inj_frame_t frame;
    const uint8_t *payload;
    uint8_t sequence;

    if (s_tx_head == s_tx_tail) {
        (void)spi_frame_pack(slot, INJ_TYPE_IDLE, 0u, NULL, 0u);
        return;
    }

    frame = s_tx_ring[s_tx_tail];
    s_tx_tail = (uint8_t)((s_tx_tail + 1u) & CH32_LINK_RING_MASK);

    sequence = s_tx_seq;
    s_tx_seq = (uint8_t)(s_tx_seq + 1u);

    payload = (frame.length != 0u) ? frame.payload : NULL;
    (void)spi_frame_pack(slot, frame.type, sequence, payload, frame.length);
}

bool ch32_link_healthy(uint32_t now_ms)
{
    if (!s_health_seen || s_counters.slots != s_health_last_slots) {
        s_health_last_slots = s_counters.slots;
        s_health_last_ms = now_ms;
        s_health_seen = true;
        return true;
    }
    return (uint32_t)(now_ms - s_health_last_ms) < CH32_LINK_HEALTH_TIMEOUT_MS;
}

void ch32_link_clear_queues(void)
{
    s_rx_head = 0u;
    s_rx_tail = 0u;
    s_tx_head = 0u;
    s_tx_tail = 0u;
    /* Resynchronize the RX window: after a lease release the next frame from the
     * free-running master is accepted as the new first. Counters are cumulative
     * diagnostics and are deliberately left intact. */
    s_rx_have_last = false;
}

const ch32_link_counters_t *ch32_link_counters(void)
{
    return &s_counters;
}

/* --- SPI1 slave DMA ping-pong (target only). -------------------------------
 *
 * MMIO lives entirely behind this guard, which the host test build does not
 * define, so the transport above stays host-compilable. The FPGA master clocks
 * one 32-byte full-duplex slot per 125 us; two RX and two TX banks alternate so
 * the foreground always retires a bank the DMA is no longer touching.
 *
 * Single-producer/single-consumer discipline: the RX transfer-complete ISR is
 * the sole writer of s_completed_bank/s_slot_pending, ch32_link_poll() the sole
 * reader. Because a slot is 125 us apart and poll() runs every foreground pass,
 * the flag is always consumed before the next completion; a sustained inability
 * to keep up is exactly what dma_overrun records. */
#if defined(CH32H417)

#include "board.h"

static uint8_t s_rx_bank[2][INJ_FRAME_SIZE] __attribute__((aligned(4)));
static uint8_t s_tx_bank[2][INJ_FRAME_SIZE] __attribute__((aligned(4)));
static DMA_InitTypeDef s_rx_dma;
static DMA_InitTypeDef s_tx_dma;
static volatile uint8_t s_active_bank;    /* Bank the DMA is currently filling. */
static volatile uint8_t s_completed_bank; /* Last RX bank the ISR retired. */
static volatile bool s_slot_pending;      /* A completed slot awaits the foreground. */

static void spi_link_gpio_init(void)
{
    GPIO_InitTypeDef gpio = {0};

    GPIO_PinAFConfig(SPI_LINK_GPIO, SPI_LINK_PINSOURCE_NSS, SPI_LINK_AF);
    GPIO_PinAFConfig(SPI_LINK_GPIO, SPI_LINK_PINSOURCE_SCK, SPI_LINK_AF);
    GPIO_PinAFConfig(SPI_LINK_GPIO, SPI_LINK_PINSOURCE_MISO, SPI_LINK_AF);
    GPIO_PinAFConfig(SPI_LINK_GPIO, SPI_LINK_PINSOURCE_MOSI, SPI_LINK_AF);

    gpio.GPIO_Pin = SPI_LINK_PIN_NSS | SPI_LINK_PIN_SCK | SPI_LINK_PIN_MISO |
                    SPI_LINK_PIN_MOSI;
    gpio.GPIO_Mode = GPIO_Mode_AF_PP;
    gpio.GPIO_Speed = GPIO_Speed_Very_High;
    GPIO_Init(SPI_LINK_GPIO, &gpio);

    gpio.GPIO_Pin = SPI_LINK_PIN_MCU_READY;
    gpio.GPIO_Mode = GPIO_Mode_Out_PP;
    GPIO_Init(SPI_LINK_GPIO, &gpio);
    GPIO_ResetBits(SPI_LINK_GPIO, SPI_LINK_PIN_MCU_READY);
}

static void spi_link_dma_configure(void)
{
    s_rx_dma.DMA_PeripheralBaseAddr = (uint32_t)&SPI_LINK_SPI->DATAR;
    s_rx_dma.DMA_Memory0BaseAddr = (uint32_t)s_rx_bank[0];
    s_rx_dma.DMA_DIR = DMA_DIR_PeripheralSRC;
    s_rx_dma.DMA_BufferSize = INJ_FRAME_SIZE;
    s_rx_dma.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    s_rx_dma.DMA_MemoryInc = DMA_MemoryInc_Enable;
    s_rx_dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    s_rx_dma.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    s_rx_dma.DMA_Mode = DMA_Mode_Normal;
    s_rx_dma.DMA_Priority = DMA_Priority_VeryHigh;
    s_rx_dma.DMA_M2M = DMA_M2M_Disable;

    s_tx_dma = s_rx_dma;
    s_tx_dma.DMA_Memory0BaseAddr = (uint32_t)s_tx_bank[0];
    s_tx_dma.DMA_DIR = DMA_DIR_PeripheralDST;
    s_tx_dma.DMA_Priority = DMA_Priority_High;

    DMA_MuxChannelConfig(SPI_LINK_RX_DMA_MUX_CHANNEL, SPI_LINK_RX_DMA_REQUEST);
    DMA_MuxChannelConfig(SPI_LINK_TX_DMA_MUX_CHANNEL, SPI_LINK_TX_DMA_REQUEST);

    DMA_Init(SPI_LINK_RX_DMA_CHANNEL, &s_rx_dma);
    DMA_Init(SPI_LINK_TX_DMA_CHANNEL, &s_tx_dma);
    DMA_ITConfig(SPI_LINK_RX_DMA_CHANNEL, DMA_IT_TC, ENABLE);
}

/* Re-point both DMA channels at `bank` and re-enable them for the next slot. In
 * DMA_Mode_Normal a channel disables itself at transfer complete, so each slot
 * is re-armed here. O(1); called only from init and the RX ISR. */
static void spi_link_arm_bank(uint8_t bank)
{
    DMA_Cmd(SPI_LINK_RX_DMA_CHANNEL, DISABLE);
    DMA_Cmd(SPI_LINK_TX_DMA_CHANNEL, DISABLE);
    s_rx_dma.DMA_Memory0BaseAddr = (uint32_t)s_rx_bank[bank];
    s_tx_dma.DMA_Memory0BaseAddr = (uint32_t)s_tx_bank[bank];
    DMA_Init(SPI_LINK_RX_DMA_CHANNEL, &s_rx_dma);
    DMA_Init(SPI_LINK_TX_DMA_CHANNEL, &s_tx_dma);
    DMA_Cmd(SPI_LINK_RX_DMA_CHANNEL, ENABLE);
    DMA_Cmd(SPI_LINK_TX_DMA_CHANNEL, ENABLE);
}

static void spi_link_spi_init(void)
{
    SPI_InitTypeDef spi = {0};

    spi.SPI_Direction = SPI_Direction_2Lines_FullDuplex;
    spi.SPI_Mode = SPI_Mode_Slave;
    spi.SPI_DataSize = SPI_DataSize_8b;
    spi.SPI_CPOL = SPI_CPOL_Low;  /* Mode 0. */
    spi.SPI_CPHA = SPI_CPHA_1Edge; /* Mode 0. */
    spi.SPI_NSS = SPI_NSS_Hard;
    spi.SPI_BaudRatePrescaler = SPI_BaudRatePrescaler_Mode0; /* Ignored in slave. */
    spi.SPI_FirstBit = SPI_FirstBit_MSB;
    spi.SPI_CRCPolynomial = 7u;
    SPI_Init(SPI_LINK_SPI, &spi);
    SPI_I2S_DMACmd(SPI_LINK_SPI, SPI_I2S_DMAReq_Rx | SPI_I2S_DMAReq_Tx, ENABLE);
    SPI_Cmd(SPI_LINK_SPI, ENABLE);
}

void ch32_link_init(void)
{
    ch32_link_reset();

    RCC_HBPeriphClockCmd(RCC_HBPeriph_DMA1, ENABLE);
    RCC_HB2PeriphClockCmd(RCC_HB2Periph_GPIOA | RCC_HB2Periph_SPI1, ENABLE);

    spi_link_gpio_init();

    /* Seed both TX banks with IDLE keepalives so the first slots on the wire are
     * well formed before any foreground fill runs. */
    ch32_link_fill_tx_slot(s_tx_bank[0]);
    ch32_link_fill_tx_slot(s_tx_bank[1]);

    spi_link_dma_configure();

    s_active_bank = 0u;
    s_completed_bank = 0u;
    s_slot_pending = false;
    spi_link_arm_bank(0u);

    spi_link_spi_init();

    /* DMA1_Channel2_IRQn is IRQ 38 (>31), core-allocated via NVIC->IALLOCR with a
     * reset default of Core_ID_V3F. The RX-complete handler runs on V5F, so route
     * the IRQ to V5F BEFORE enabling it -- otherwise completions are delivered to
     * V3F (which has no handler), the ping-pong never re-arms, and slots/dma_rearm
     * freeze after the first slot; with the watchdog armed that resets the board
     * ~1 s. Mirrors the USBFS routing in usb_cdc_fs.c. */
    NVIC_SetAllocateIRQ(SPI_LINK_RX_DMA_IRQ, Core_ID_V5F);
    NVIC_EnableIRQ(SPI_LINK_RX_DMA_IRQ);

    /* Both directions armed: release MCU_READY. */
    GPIO_SetBits(SPI_LINK_GPIO, SPI_LINK_PIN_MCU_READY);
}

void ch32_link_poll(void)
{
    uint8_t bank;

    /* Fast unmasked pre-check. The common case on this busy loop is "no new
     * completion", and we do not want to mask the ISR (and pay its fence.i) on
     * every iteration -- only when there is actually a slot to retire. */
    if (!s_slot_pending) {
        return;
    }

    /* A completion is pending: mask the RX-complete ISR across the handoff. This
     * closes two races in the two-bank ping-pong: (1) a lost wakeup, if the ISR
     * sets s_slot_pending in the gap between test and clear (the new completion
     * would be discarded); and (2) a torn read, if the ISR re-arms DMA onto the
     * bank we are mid-retire. The completion is latched in the NVIC while masked
     * and serviced on unmask -- none is lost -- and this masks once per ~125 us
     * slot (not per loop), so the ~1 us of masked work and the DMA re-arm delay
     * are immaterial: the next slot is ~124 us away when the ISR runs. */
    NVIC_DisableIRQ(SPI_LINK_RX_DMA_IRQ);
    if (!s_slot_pending) { /* re-check under mask */
        NVIC_EnableIRQ(SPI_LINK_RX_DMA_IRQ);
        return;
    }
    bank = s_completed_bank;
    s_slot_pending = false;

    ch32_link_retire_slot(s_rx_bank[bank]);
    /* Refill the TX bank that just drained; it is reused two slots from now. */
    ch32_link_fill_tx_slot(s_tx_bank[bank]);
    NVIC_EnableIRQ(SPI_LINK_RX_DMA_IRQ);
}

void DMA1_Channel2_IRQHandler(void) WCH_IRQ;
void DMA1_Channel2_IRQHandler(void)
{
    uint8_t done;

    if (DMA_GetITStatus(DMA1, SPI_LINK_RX_DMA_IT_TC) == RESET) {
        return;
    }
    DMA_ClearITPendingBit(DMA1, SPI_LINK_RX_DMA_IT_TC);

    done = s_active_bank;
    if (s_slot_pending) {
        /* The foreground has not retired the previous slot, so the bank about to
         * be reused still holds an unread one. */
        s_counters.dma_overrun++;
    }
    s_active_bank = (uint8_t)(s_active_bank ^ 1u);
    spi_link_arm_bank(s_active_bank);
    /* Count the re-arm so the watchdog can tell a live DMA/ISR from a wedged one
     * independently of foreground retire progress (see ch32_link_counters_t). */
    s_counters.dma_rearm++;
    s_completed_bank = done;
    s_slot_pending = true;
}

#endif /* CH32H417 */
