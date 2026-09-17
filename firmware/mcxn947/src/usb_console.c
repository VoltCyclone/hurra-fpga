// The CDC console's hardware half. See usb_console.h.
//
// Portable half first, per the guard-polarity rule -- except that there is
// none: every line of this module is either MMIO or a TinyUSB call, which is
// exactly why the console's logic lives in console.c and not here. This file
// is the ~17 lines of glue design doc section 7 predicted, plus the clock and
// PHY sequence the board needs.

#include "usb_console.h"

#if defined(MCXN947)

#include "console.h"
#include "dbg_uart.h"
#include "link.h"
#include "link_retire.h"
#include "platform.h"
#include "core1_release.h"
#include "shared_window.h"

#include "fsl_clock.h"
#include "fsl_device_registers.h"

#include "board.h"         // BOARD_USB_PHY_D_CAL / TXCAL45DP / TXCAL45DM
#include "clock_config.h"  // BOARD_XTAL0_CLK_HZ

#include "tusb.h"

// Port 1 is the ChipIdea High Speed controller. Resolved from the pinned
// TinyUSB tree rather than assumed; the argument is in src/tusb_config.h.
#define USB_CONSOLE_RHPORT 1u

// The USB interrupt must never preempt the link's retirement ISR. It cannot
// today -- retirement is short and the CPU has no deadline, only design doc
// section 3's 107.93 us staging window -- but the two ISRs both default to
// priority 0, and at equal priority the one that arrives first runs to
// completion. Demoting USB makes the ordering a property of the configuration
// instead of an accident of arrival, and it costs the console nothing: the
// ChipIdea controller buffers a whole bulk transfer in SRAM.
//
// Set here rather than in link.c deliberately. link.c's boot ladder is
// measured and step 4 has no business editing it; lowering this side achieves
// the same ordering without touching the path the link comes up on.
#define USB_CONSOLE_IRQ_PRIORITY 3u

// The vector table names USB1_HS_IRQHandler, which
// startup_MCXN947_cm33_core0.S defines as a WEAK TRAMPOLINE that branches to
// USB1_HS_DriverIRQHandler -- and it is the Driver name that is `.set` to
// DefaultISR. So the Driver name is the one to define and the one to root
// (design doc section 7 rung 1). Defining USB1_HS_IRQHandler here instead
// would compile, link, and silently never run, because the strong trampoline
// would still branch to the vendor's DefaultISR stub.
void USB1_HS_DriverIRQHandler(void);
void USB1_HS_DriverIRQHandler(void)
{
    tusb_int_handler(USB_CONSOLE_RHPORT, true);
}

// Required by the pinned TinyUSB tree: common/tusb_common.h declares it
// `extern`, not weak, so the application owns the time base.
uint32_t tusb_time_millis_api(void)
{
    return platform_ticks();
}

// --- Console transport ------------------------------------------------------

static uint32_t usb_console_write(void *ctx, const char *data, uint32_t length)
{
    (void)ctx;

    if (!tud_cdc_connected()) {
        // Not an error and not a drop worth counting against the link: with no
        // terminal attached there is nowhere for the bytes to go. Reporting
        // them as taken keeps `txdrop` meaning "the host was too slow", which
        // is the only reading of it that is actionable.
        return length;
    }

    const uint32_t taken = tud_cdc_write(data, length);
    // Flush immediately rather than at the end of the line. A console whose
    // last partial packet sat in the FIFO until the next command would look
    // like it had hung, and at High Speed the cost of a short packet is
    // irrelevant.
    (void)tud_cdc_write_flush();
    return taken;
}

static void usb_console_stats(void *ctx, console_stats_t *out)
{
    static uint32_t previous_cpu1_heartbeat;
    static link_snapshot_t last_snapshot;
    static uint32_t snapshot_read_failures;

    (void)ctx;

    link_retire_counters_t counters;
    link_diagnostics_t diagnostics;

    // Both of these mask the retirement interrupt internally; see link.h.
    link_counters_read(&counters);
    link_diagnostics_read(&diagnostics);

    out->slots = counters.slots;
    out->idle = counters.idle;
    out->deliverable = counters.deliverable;
    out->bad_sof = counters.bad_sof;
    out->bad_crc = counters.bad_crc;
    out->bad_length = counters.bad_length;
    out->bad_type = counters.bad_type;
    out->duplicate = counters.duplicate;
    out->stale = counters.stale;
    out->sequence_gap = counters.sequence_gap;
    out->ring_drop = counters.ring_drop;

    out->isr_entries = diagnostics.isr_entries;
    out->retire_stalls = diagnostics.retire_stalls;
    out->daddr_out_of_range = diagnostics.daddr_out_of_range;
    out->recoveries = diagnostics.recoveries;
    out->framing_recoveries = diagnostics.framing_recoveries;
    out->gap_wait_timeouts = diagnostics.gap_wait_timeouts;
    out->link_ready = diagnostics.ready;

    out->uptime_ms = platform_ticks();

    // CPU0 deliberately exercises the same bounded reader CPU1 uses. If the
    // ISR wins all four attempts, keep reporting the previous complete copy
    // and make the collision visible rather than formatting a torn sample.
    if (!link_snapshot_read(&g_shared_window.snapshot, &last_snapshot)) {
        snapshot_read_failures++;
    }
    out->snapshot_seq = last_snapshot.seq;
    out->snapshot_slot_counter = last_snapshot.slot_counter;
    out->snapshot_read_failures = snapshot_read_failures;

    // CPU1 is observed, never waited on and never fed into a health decision.
    // The words are naturally aligned 32-bit accesses; step 6 adds the seqlock
    // only for the larger multiword link snapshot.
    const core1_release_status_t release_status = core1_release_last_status();
    out->cpu1_released = release_status == CORE1_RELEASED;
    out->cpu1_held_in_reset = release_status == CORE1_HELD_IN_RESET;

    if (g_shared_window.magic == SHARED_WINDOW_MAGIC) {
        const uint32_t heartbeat = g_shared_window.cpu1_heartbeat;
        out->cpu1_boot_count = g_shared_window.cpu1_boot_count;
        out->cpu1_frames = g_shared_window.cpu1_frames;
        out->cpu1_display_flags = g_shared_window.cpu1_display_flags;
        out->cpu1_heartbeat = heartbeat;
        out->cpu1_seen_slot_counter = g_shared_window.cpu1_seen_slot_counter;
        out->cpu1_alive = heartbeat != previous_cpu1_heartbeat;
        previous_cpu1_heartbeat = heartbeat;
    } else {
        out->cpu1_boot_count = 0u;
        out->cpu1_frames = 0u;
        out->cpu1_display_flags = 0u;
        out->cpu1_heartbeat = 0u;
        out->cpu1_seen_slot_counter = 0u;
        out->cpu1_alive = false;
    }
}

// --- Clock and PHY ----------------------------------------------------------

// The board's own USB HS bring-up. Two independent sources agree on this
// sequence register for register, which is why it is written out here rather
// than vendored:
//
//   * the SDK's USB_DeviceClockInit() in
//     boards/frdmmcxn947/usb_examples/usb_device_cdc_vcom/bm/cm33_core0/,
//   * hw/bsp/mcx/family.c in the pinned TinyUSB tree, under
//     `BOARD_TUD_RHPORT == 1 && CFG_TUSB_MCU == OPT_MCU_MCXN9`.
//
// PROVENANCE.md records why usb_phy.c itself is not vendored: it needs
// middleware/usb/include/usb.h for three enumerators, and that header pulls in
// fsl_os_abstraction.h -- so taking "the only file from middleware/usb/" would
// in fact take the NXP USB stack's common headers and the OSA layer, both of
// which design doc section 7 excludes.
static void usb_console_hardware_init(void)
{
    // Core rail. platform_init() has already taken the part to overdrive for
    // LPSPI6's 30 MHz slave ceiling (design doc section 2), and the levels
    // below are the same overdrive levels -- DCDC_VDD_LVL(3), CORELDO_VDD_LVL(3)
    // -- so this neither raises nor lowers what the link depends on. What it
    // adds is SYSLDO_VDD_DS and the ACTIVE_VDELAY the USB PHY's analogue
    // supply needs.
    SPC0->ACTIVE_VDELAY = 0x0500;
    SPC0->ACTIVE_CFG &= ~SPC_ACTIVE_CFG_CORELDO_VDD_DS_MASK;
    SPC0->ACTIVE_CFG |= SPC_ACTIVE_CFG_DCDC_VDD_LVL(0x3) | SPC_ACTIVE_CFG_CORELDO_VDD_LVL(0x3) |
                        SPC_ACTIVE_CFG_SYSLDO_VDD_DS_MASK | SPC_ACTIVE_CFG_DCDC_VDD_DS(0x2u);
    while ((SPC0->SC & SPC_SC_BUSY_MASK) != 0u) {
    }

    if ((SCG0->LDOCSR & SCG_LDOCSR_LDOEN_MASK) == 0u) {
        SCG0->TRIM_LOCK = 0x5a5a0001U;
        SCG0->LDOCSR |= SCG_LDOCSR_LDOEN_MASK;
        while ((SCG0->LDOCSR & SCG_LDOCSR_VOUT_OK_MASK) == 0u) {
        }
    }

    SYSCON->AHBCLKCTRLSET[2] |=
        SYSCON_AHBCLKCTRL2_USB_HS_MASK | SYSCON_AHBCLKCTRL2_USB_HS_PHY_MASK;

    // The 24 MHz crystal. BOARD_BootClockPLL150M() runs the core from PLL0 off
    // the FRO, so SOSC is not otherwise started -- the USB PHY PLL is the only
    // consumer of it in this image.
    SCG0->SOSCCFG &= ~(SCG_SOSCCFG_RANGE_MASK | SCG_SOSCCFG_EREFS_MASK);
    SCG0->SOSCCFG = (1U << SCG_SOSCCFG_RANGE_SHIFT) | (1U << SCG_SOSCCFG_EREFS_SHIFT);
    SCG0->SOSCCSR |= SCG_SOSCCSR_SOSCEN_MASK;
    while ((SCG0->SOSCCSR & SCG_SOSCCSR_SOSCVLD_MASK) == 0u) {
    }

    SYSCON->CLOCK_CTRL |=
        SYSCON_CLOCK_CTRL_CLKIN_ENA_MASK | SYSCON_CLOCK_CTRL_CLKIN_ENA_FM_USBH_LPT_MASK;
    CLOCK_EnableClock(kCLOCK_UsbHs);
    CLOCK_EnableClock(kCLOCK_UsbHsPhy);
    (void)CLOCK_EnableUsbhsPhyPllClock(kCLOCK_Usbphy480M, BOARD_XTAL0_CLK_HZ);
    (void)CLOCK_EnableUsbhsClock();

    // PHY. This is USB_EhciPhyInit()'s body for a part with neither
    // FSL_FEATURE_SOC_CCM_ANALOG_COUNT nor FSL_FEATURE_SOC_ANATOP_COUNT, which
    // MCXN947 is -- the ANATOP and USB_ANALOG arms of that function compile
    // out entirely here.
    USBPHY->TRIM_OVERRIDE_EN = 0x001fU;
    USBPHY->CTRL |= USBPHY_CTRL_SET_ENUTMILEVEL2_MASK | USBPHY_CTRL_SET_ENUTMILEVEL3_MASK;
    USBPHY->PWD = 0u;

    uint32_t tx = USBPHY->TX;
    tx &= ~(USBPHY_TX_D_CAL_MASK | USBPHY_TX_TXCAL45DM_MASK | USBPHY_TX_TXCAL45DP_MASK);
    tx |= USBPHY_TX_D_CAL(BOARD_USB_PHY_D_CAL) | USBPHY_TX_TXCAL45DP(BOARD_USB_PHY_TXCAL45DP) |
          USBPHY_TX_TXCAL45DM(BOARD_USB_PHY_TXCAL45DM);
    USBPHY->TX = tx;
}

// --- Bring-up report on the debug UART --------------------------------------
//
// The console cannot report its own absence. If J11 is unplugged, if the PHY
// PLL never locks, or if the host never issues SET_CONFIGURATION, there is by
// construction no CDC pipe to say so on -- so this one line goes out the
// MCU-Link VCOM alongside link.c's counter report, on the same 1 Hz cadence.
//
// The field that answers the first question is OTGSC[BSV], B-session valid:
// it is the controller's own view of whether VBUS is present on J11. BSV = 0
// means no cable, which is a completely different diagnosis from BSV = 1 with
// no configuration -- the first is a bench problem and the second is a
// firmware problem, and without this line they look identical from the host
// (nothing appears in /dev).
#define USB_CONSOLE_REPORT_MS 1000u

#define USB_CONSOLE_REGS ((USBHS_Type *)USBHS1__USBC_BASE)

static uint32_t s_report_ms;

// tusb_init()'s return, latched. Reported rather than acted on: the link was
// already live when the stack came up, and design doc section 4 forbids a
// console fault from touching it -- so there is nothing to do on failure but
// say so.
static bool s_stack_started;

static void usb_console_report(void)
{
    const uint32_t otgsc = USB_CONSOLE_REGS->OTGSC;

    dbg_puts("[usb] vbus=");
    dbg_puts((otgsc & USBHS_OTGSC_BSV_MASK) != 0u ? "yes" : "NO ");
    dbg_puts(" mounted=");
    dbg_puts(tud_mounted() ? "yes" : "no ");
    dbg_puts(" cdc=");
    dbg_puts(tud_cdc_connected() ? "open" : "shut");
    dbg_puts(" speed=");
    switch (tud_speed_get()) {
        case TUSB_SPEED_HIGH:
            dbg_puts("HS");
            break;
        case TUSB_SPEED_FULL:
            dbg_puts("FS");
            break;
        default:
            dbg_puts("--");
            break;
    }
    dbg_puts(" otgsc=");
    dbg_hex32(otgsc);
    dbg_puts(" portsc=");
    dbg_hex32(USB_CONSOLE_REGS->PORTSC1);
    dbg_puts(" usbcmd=");
    dbg_hex32(USB_CONSOLE_REGS->USBCMD);
    dbg_puts(" usbsts=");
    dbg_hex32(USB_CONSOLE_REGS->USBSTS);
    dbg_puts(" init=");
    dbg_puts(s_stack_started ? "ok" : "FAIL");
    dbg_puts(" flood=");
    dbg_dec32(console_flood_bytes());
    dbg_puts(" txdrop=");
    dbg_dec32(console_dropped_bytes());
    dbg_puts("\n");
}

// --- Entry points -----------------------------------------------------------

static void usb_console_cpu1_halt(void *ctx)
{
    (void)ctx;
    core1_halt();
}

static void usb_console_cpu1_start(void *ctx)
{
    (void)ctx;
    (void)core1_release();
}

static const console_ops_t s_console_ops = {
    .write = usb_console_write,
    .stats = usb_console_stats,
    .cpu1_halt = usb_console_cpu1_halt,
    .cpu1_start = usb_console_cpu1_start,
    .ctx = NULL,
};

void usb_console_init(void)
{
    console_init(&s_console_ops);

    usb_console_hardware_init();

    NVIC_SetPriority(USB1_HS_IRQn, USB_CONSOLE_IRQ_PRIORITY);

    // tusb_init() enables the controller interrupt itself, via ci_hs_mcx.h's
    // CI_DCD_INT_ENABLE. A failure here is reported and then ignored on
    // purpose: the link is already live and section 4 forbids a console fault
    // from affecting it, so there is nothing to do but carry on without a
    // console.
    // Explicit role and speed rather than the NULL shorthand. Passing NULL
    // makes tusb_rhport_init() ignore the rhport argument entirely and fall
    // back to TUD_OPT_RHPORT -- the same value here, but silently, which is
    // the wrong property for the one number design doc section 10 flagged as
    // never having been re-read.
    const tusb_rhport_init_t device_init = {
        .role = TUSB_ROLE_DEVICE,
        .speed = TUSB_SPEED_HIGH,
    };
    s_stack_started = tusb_init(USB_CONSOLE_RHPORT, &device_init);
}

void usb_console_poll(void)
{
    tud_task();

    static bool s_was_connected;
    const bool connected = tud_cdc_connected();
    if (connected && !s_was_connected) {
        console_greet();
    }
    s_was_connected = connected;

    // Drain in bounded bites. `available` can be a whole 2 KiB FIFO after a
    // host burst, and echoing all of it in one pass would hold the foreground
    // loop away from link_poll() for as long as the burst lasted.
    uint8_t buffer[64];
    uint32_t budget = 4u;
    while (budget-- > 0u && tud_cdc_available() > 0u) {
        const uint32_t count = tud_cdc_read(buffer, sizeof(buffer));
        if (count == 0u) {
            break;
        }
        console_feed(buffer, count);
    }

    console_flood_step();

    const uint32_t now = platform_ticks();
    if (now - s_report_ms >= USB_CONSOLE_REPORT_MS) {
        s_report_ms = now;
        usb_console_report();
    }
}

#endif  // MCXN947
