#include "usb_time.h"

/* --- Pure timing/sync core (MMIO-free; host-testable). --- */

static uint16_t s_frame;
static uint32_t s_last_capture;
static uint32_t s_period;
static bool s_synchronized;
static bool s_have_last; /* False until the first capture, and again after a loss. */

void usb_time_reset(void)
{
    s_frame = 0u;
    s_last_capture = 0u;
    s_period = 0u;
    s_synchronized = false;
    s_have_last = false;
}

void usb_time_on_capture(uint32_t capture_ticks)
{
    s_frame = (uint16_t)(s_frame + 1u);

    if (s_have_last) {
        uint32_t new_period = capture_ticks - s_last_capture;

        if (!s_synchronized) {
            /* First valid period, or the first after a loss: latch it rather
             * than blend an outage gap into the estimate. */
            s_period = new_period;
        } else {
            /* Steady state: a quarter-weight EMA smooths jitter. */
            s_period = (s_period - (s_period >> 2)) + (new_period >> 2);
        }
        s_synchronized = true;
    }

    s_have_last = true;
    s_last_capture = capture_ticks;
}

void usb_time_poll_at(uint32_t now_ticks)
{
    if (s_synchronized && s_period > 0u) {
        uint32_t elapsed = now_ticks - s_last_capture;

        if (elapsed >= (USB_TIME_SYNC_LOSS_FRAMES * s_period)) {
            /* The FPGA has stopped driving USB_SYNC. Drop sync and force the
             * next capture to re-baseline instead of measuring across the gap. */
            s_synchronized = false;
            s_have_last = false;
        }
    }
}

usb_time_snapshot_t usb_time_snapshot(void)
{
    usb_time_snapshot_t snapshot;

    snapshot.frame = s_frame;
    snapshot.capture_ticks = s_last_capture;
    snapshot.period_ticks = s_period;
    snapshot.synchronized = s_synchronized;
    return snapshot;
}

bool usb_time_reached(uint16_t now, uint16_t target)
{
    /* RFC 1982 serial arithmetic over 16 bits: reached when `now` is within the
     * forward half-window [0, 0x7FFF] ahead of `target`. */
    return (int16_t)(now - target) >= 0;
}

/* --- TIM8_CH1 input capture (target only). ---------------------------------
 *
 * TIM8 free-runs as a 16-bit up counter; s_overflow is its high word, bumped by
 * the update ISR, so (s_overflow << 16 | CNT) is a 32-bit tick base. The CC1
 * ISR timestamps each USB_SYNC edge into that base and feeds the pure core.
 * Neither ISR disciplines the CPU clock or busy-waits, as required. Capture and
 * overflow are separate vectors; the exact ordering at a wrap boundary is a
 * bench-verified detail (Task 6). */
#if defined(CH32H417)

#include "board.h"

static volatile uint32_t s_overflow;

/* Coherent 32-bit read: retry if the update ISR bumped the high word between
 * sampling it and the 16-bit counter. */
static uint32_t usb_time_now(void)
{
    uint32_t hi;
    uint32_t lo;

    do {
        hi = s_overflow;
        lo = (uint32_t)USB_SYNC_TIM->CNT;
    } while (hi != s_overflow);

    return (hi << 16) | lo;
}

void usb_time_init(void)
{
    GPIO_InitTypeDef gpio = {0};
    TIM_TimeBaseInitTypeDef base = {0};
    TIM_ICInitTypeDef ic = {0};

    usb_time_reset();
    s_overflow = 0u;

    RCC_HB2PeriphClockCmd(RCC_HB2Periph_GPIOC | RCC_HB2Periph_TIM8, ENABLE);

    GPIO_PinAFConfig(USB_SYNC_GPIO, USB_SYNC_PINSOURCE, USB_SYNC_AF);
    gpio.GPIO_Pin = USB_SYNC_PIN;
    gpio.GPIO_Mode = GPIO_Mode_IN_FLOATING;
    gpio.GPIO_Speed = GPIO_Speed_Very_High;
    GPIO_Init(USB_SYNC_GPIO, &gpio);

    /* Free-running 16-bit base at the full timer clock; the SOF period is
     * measured in ticks, so the absolute rate does not matter. */
    base.TIM_Prescaler = 0u;
    base.TIM_CounterMode = TIM_CounterMode_Up;
    base.TIM_Period = 0xFFFFu;
    base.TIM_ClockDivision = TIM_CKD_DIV1;
    base.TIM_RepetitionCounter = 0u;
    TIM_TimeBaseInit(USB_SYNC_TIM, &base);

    ic.TIM_Channel = TIM_Channel_1;
    ic.TIM_ICPolarity = TIM_ICPolarity_Rising;
    ic.TIM_ICSelection = TIM_ICSelection_DirectTI;
    ic.TIM_ICPrescaler = TIM_ICPSC_DIV1;
    ic.TIM_ICFilter = 0u;
    TIM_ICInit(USB_SYNC_TIM, &ic);

    TIM_ITConfig(USB_SYNC_TIM, TIM_IT_Update | TIM_IT_CC1, ENABLE);
    NVIC_EnableIRQ(USB_SYNC_TIM_UP_IRQ);
    NVIC_EnableIRQ(USB_SYNC_TIM_CC_IRQ);
    TIM_Cmd(USB_SYNC_TIM, ENABLE);
}

void usb_time_poll(void)
{
    usb_time_poll_at(usb_time_now());
}

void TIM8_UP_IRQHandler(void) WCH_IRQ;
void TIM8_UP_IRQHandler(void)
{
    if (TIM_GetITStatus(USB_SYNC_TIM, TIM_IT_Update) == RESET) {
        return;
    }
    TIM_ClearITPendingBit(USB_SYNC_TIM, TIM_IT_Update);
    s_overflow++;
}

void TIM8_CC_IRQHandler(void) WCH_IRQ;
void TIM8_CC_IRQHandler(void)
{
    uint32_t capture;

    if (TIM_GetITStatus(USB_SYNC_TIM, TIM_IT_CC1) == RESET) {
        return;
    }
    TIM_ClearITPendingBit(USB_SYNC_TIM, TIM_IT_CC1);
    capture = (s_overflow << 16) | (uint32_t)TIM_GetCapture1(USB_SYNC_TIM);
    usb_time_on_capture(capture);
}

#endif /* CH32H417 */
