// The FPGA injection link. Portable transport predicates first; every MMIO
// access lives in the single trailing `#if defined(MCXN947)` block, so a host
// build of this file sees no vendor header at all.

#include "link.h"
#include "dbg_uart.h"

void link_build_idle_slot(uint8_t slot[INJ_FRAME_SIZE])
{
    // spi_frame_pack() checks type and length before writing a byte, and IDLE
    // with length 0 never dereferences the payload pointer, so NULL is correct
    // rather than merely tolerated. The return can only be SPI_FRAME_OK.
    (void)spi_frame_pack(slot, INJ_TYPE_IDLE, 0u, NULL, 0u);
}

bool link_slot_is_inert_idle(const uint8_t slot[INJ_FRAME_SIZE])
{
    return spi_frame_unpack(slot, NULL, NULL, NULL, NULL) == SPI_FRAME_IDLE;
}

uint8_t link_next_bank(uint8_t bank)
{
    return (uint8_t)((bank + 1u) & (LINK_SLOT_BANKS - 1u));
}

#if defined(MCXN947)

#include <string.h>

#include "fsl_clock.h"
#include "fsl_device_registers.h"
#include "fsl_edma.h"
#include "fsl_gpio.h"
#include "fsl_lpflexcomm.h"
#include "fsl_lpspi.h"
#include "fsl_port.h"

#include "shared_window.h"

// shared_window.h spells its descriptor capacity as a literal so that neither
// it nor its host test has to know the wire contract exists. This file includes
// both, so this is where the two are held to each other -- a contract
// regeneration that changed the bound would fail the build here rather than
// silently truncate a descriptor on the panel.
_Static_assert(SHARED_DESCRIPTOR_CAPACITY == INJ_MAX_DESCRIPTOR_BYTES_PER_INTERFACE,
               "shared descriptor capacity must match the wire contract");

// The step-3 portable halves. Both are MMIO-free and host-tested in their own
// right; they are included inside the guard so that a host build of this file
// still links against nothing but spi_frame.c, as test/link_test.c does.
#include "hid_descriptor_reassembly.h"
#include "link_fault_classify.h"
#include "link_recovery.h"
#include "link_retire.h"

// Same discipline as the descriptor capacity above: shared_window.h spells its
// suspect capacity as a literal so the IPC layer need not know the classifier
// exists, and this file includes both, so this is where they are held to each
// other. Widening the classifier's suspect list without widening the shared
// block would otherwise drop the last pin silently -- on the one report whose
// whole value is naming every candidate wire.
_Static_assert(SHARED_FAULT_MAX_SUSPECTS == LINK_FAULT_MAX_SUSPECTS,
               "shared fault suspect capacity must match the classifier");

// The injection command builders and the session policy above them: the MCU's
// TX side of the wire contract. Both are MMIO-free and host-tested in their own
// right; included inside the guard so a host build of this file still links
// against nothing but spi_frame.c (test/link_test.c).
#include "inj_command.h"
#include "inj_session.h"

// PLATFORM_CORE_HZ, for the FlexComm6 divider static assert below, and
// platform_ticks() for the report cadence. Portable header; nothing in it is
// MMIO.
#include "platform.h"

// --- Board wiring: mikroBUS J6, LP_FLEXCOMM6 -------------------------------
//
// The four data pins and their FlexComm roles, from the FRDM-MCXN947 pin
// tables in the SDK's own board packages (the `pin_signal` strings, e.g.
// `PIO3_23/FC6_P3/CT_INP11/PWM1_X3/FLEXIO0_D31/SAI1_TXD1` carrying
// `label: 'P3_23/J6[3]'`):
//
//   J6[6] MOSI  P3_20  FC6_P0  LPSPI SOUT pad   FPGA -> MCU, so an INPUT here
//   J6[4] SCK   P3_21  FC6_P1  LPSPI SCK
//   J6[5] MISO  P3_22  FC6_P2  LPSPI SIN pad    MCU -> FPGA, so an OUTPUT here
//   J6[3] CS    P3_23  FC6_P3  LPSPI PCS0
//
// FCn_P0 is LPSPI SOUT: the SDK's own LPSPI slave example labels
// `PIO0_24/FC1_P0/...` as `LPSPI1_SOUT`. The mikroBUS socket is wired for the
// *board* to be master, so its MOSI net lands on the SOUT pad. We are the
// slave, which is what CFGR1[PINCFG] below exists to fix.
#define LINK_SPI LPSPI6
#define LINK_FLEXCOMM_INSTANCE 6u
#define LINK_PORT PORT3
#define LINK_PIN_MOSI 20u
#define LINK_PIN_SCK 21u
#define LINK_PIN_MISO 22u
#define LINK_PIN_CS 23u

// The ALT numbers are NOT the position of `FC6_Pn` in the pin_signal string --
// that string also lists functions with no mux slot (WUU0_IN*, TRIG_*, analog)
// and reading it as a dense ALT list gives the wrong answer on three of these
// four pins.
//
// The rule, derived by pairing every `/* Pin is configured as FCn_Pm */`
// comment in every FRDM/EVK pin_mux.c with the `kPORT_MuxAltN` it programs,
// over 787 configured pins with no counterexample in either direction:
//
//     a pin offering ONE FlexComm     -> that FlexComm is ALT2   (136 cases)
//     a pin offering TWO FlexComms    -> the first is ALT2       (643 cases)
//                                        the second is ALT3      (8 cases)
//
// P3_20/21/22 each offer FC8 first and FC6 second, so FC6 is ALT3. P3_23
// offers FC6 *only*, so there FC6 is ALT2. **That one pin is the odd one out,
// and it is the chip select** -- muxing it to ALT3 with its neighbours leaves
// PCS0 unconnected, the slave never frames, and the symptom is not an error
// but total silence with every FPGA counter flat.
#define LINK_ALT_DATA kPORT_MuxAlt3
#define LINK_ALT_CS kPORT_MuxAlt2

// `mcu_ready`, J5 pin 2 (ME_INT). Plain GPIO output, PORT mux ALT0.
//
// PORT5/GPIO5 have no kCLOCK_Port5/kCLOCK_Gpio5 gate: fsl_clock.h's list stops
// at Port4/Gpio4, because this is a VBAT-domain always-on pad (design doc
// section 2 notes the same thing when rejecting P5_7 for timer capture). The
// SDK's own mc_pmsm example writes PORT5->PCR[] with no clock enable, which is
// the evidence that none is needed. Nothing here enables one, and there is no
// enumerator to enable if it turned out to be required.
#define LINK_READY_PORT PORT5
#define LINK_READY_GPIO GPIO5
#define LINK_READY_PIN 7u

// eDMA0 channels. Channel numbers are otherwise free; these two are reserved
// to the link for the life of the project, and design doc section 7 rung 5
// will assert that no CPU1 symbol touches eDMA0 at all.
#define LINK_DMA DMA0
#define LINK_DMA_CHANNEL_TX 0u
#define LINK_DMA_CHANNEL_RX 1u

// FlexComm6 functional clock. **FRO_HF is not an option here.**
// BOARD_BootClockPLL150M leaves FRO_HF at 48 MHz (its own YAML header says
// `{id: FRO_HF_clock.outFreq, value: 48 MHz}`), and LP2 requires
// SCK <= f_periph/4, so 48 MHz caps SCK at 12 MHz against the 15 MHz the FPGA
// clocks -- kFRO_HF_DIV_to_FLEXCOMM6 would be a silent protocol violation.
//
// PLL0 is already at 150 MHz from the same profile, so route it through
// PLLCLKDIV (untouched by the board profile, hence set explicitly here) and
// halve it: 75 MHz, which is SCK x 5.
#define LINK_FLEXCOMM_CLOCK_DIV 2u
#define LINK_FLEXCOMM_CLOCK_HZ (PLATFORM_CORE_HZ / LINK_FLEXCOMM_CLOCK_DIV)
_Static_assert(LINK_FLEXCOMM_CLOCK_HZ >= 4u * 15000000u,
               "LP2 requires the FlexComm6 functional clock to be at least 4x SCK");

// FIFO watermarks. The LPSPI request asserts while the TX FIFO holds TXWATER
// words or fewer, so the DMA tops the FIFO up to TXWATER + 1 and then stops.
// The FIFO is 8 x 32 bits = 256 bits = exactly one slot, so **only TXWATER = 7
// actually stages a whole frame before CS falls**, which is the property design
// doc section 3 rests its "CPU0 has no latency deadline" argument on. A
// half-full FIFO would still work at these rates but would give that argument
// away for nothing, and it is margin against the transmit underrun that
// ERR051588 turns into FIFO-pointer corruption.
//
// RXWATER = 0 requests as soon as one word has landed, which is the least
// overrun-prone setting.
//
// Consequence worth carrying into step 3: with the FIFO kept full, the TX ring
// runs one whole slot ahead of the wire. The bank the DMA is loading is not the
// bank being clocked out, which is exactly why section 4's rule is "only ever
// refill the buffer the DMA is *not* pointing at". At this step both banks hold
// the same inert IDLE slot, so the lookahead is unobservable.
#define LINK_TX_WATERMARK 7u
#define LINK_RX_WATERMARK 0u

// Slot banks. Plain SRAM: this part's Cortex-M33 has no core data cache, and
// PROVENANCE.md records that the vendored linker script has no NonCacheable
// *region* -- the attribute would collect into ordinary .data and buy nothing.
static uint8_t s_tx_bank[LINK_SLOT_BANKS][INJ_FRAME_SIZE] __attribute__((aligned(4)));
static uint8_t s_rx_bank[LINK_SLOT_BANKS][INJ_FRAME_SIZE] __attribute__((aligned(4)));

// In-memory scatter-gather descriptors. The hardware reads these directly at
// major-loop completion and never writes them, so each reload restores that
// bank's SADDR/DADDR verbatim and the ring runs forever untouched by the CPU.
// 32-byte alignment is a hardware requirement, not a preference: a misaligned
// TCD raises an eDMA error instead of transferring.
static edma_tcd_t s_tx_tcd[LINK_SLOT_BANKS] __attribute__((aligned(32)));
static edma_tcd_t s_rx_tcd[LINK_SLOT_BANKS] __attribute__((aligned(32)));

// --- Step-3 state ----------------------------------------------------------
//
// s_rx_cursor is the next RX bank to retire. It is NOT a model of where the
// DMA is -- that is read from the live TCD every time, because a self-loading
// scatter-gather ring never tells the CPU when it reloaded (link_retire.h
// explains why a software `bank ^= 1` would be wrong after a recovery or a
// missed completion). The cursor only records how far retirement has got.
//
// Written by the RX ISR and by link_dma_rings_install(), which only ever runs
// with the channel request off and the IRQ masked. Read nowhere else.
static uint8_t s_rx_cursor;

// ISR-side liveness and shortfall, the pair that separates "the DMA stopped"
// from "we stopped retiring". s_isr_entries counts major-loop completions
// delivered to the CPU; link_retire_counters()->slots counts banks actually
// retired. The two must track 1:1 -- a completion that finds the cursor
// already on the live bank retires nothing and is a lost slot.
static volatile uint32_t s_isr_entries;
static volatile uint32_t s_retire_stalls;
static volatile uint32_t s_daddr_out_of_range;
static volatile uint32_t s_saddr_out_of_range;  // TX refill: source addr outside the bank array.

// ERR051588 accounting. A nonzero s_recoveries in normal operation is a bug,
// not routine -- design doc section 4 says so and this is the counter it means.
static uint32_t s_recoveries;
static uint32_t s_framing_recoveries;
static uint32_t s_recovery_wait_timeouts;
static uint32_t s_recovery_fill_timeouts;
static uint32_t s_recovery_last_ms;
static uint32_t s_last_fault_sr;

static void link_dma_rings_install(void);

// Last value driven onto the line. `mcu_ready` is an output with no readback
// path in this design -- GPIO_PortSet/Clear are write-only register writes --
// so the only way the console can report the link's ready state is for the
// one function that drives it to remember what it drove.
static bool s_ready;

// The injection session: MCU-side policy that learns the target report, uploads
// a boot-mouse field map and then emits RELATIVE motion. Fed from retired RX
// telemetry and drained into the TX ring by link_drain_rx(); see inj_session.h.
static inj_session_t s_inj;

// Reassembly of the captured device's HID report descriptor, for CPU1 to decode
// onto the panel. Written only by the retirement interrupt; copied into the
// shared window by link_poll() with that interrupt masked. One instance, and
// one interface: the FPGA's whole descriptor store is 4 KB for all interfaces
// combined and enumeration only ever commits a HID boot mouse, so four of these
// could never fill. See hid_descriptor_reassembly.h.
static hid_descriptor_reassembly_t s_desc;
static volatile bool s_desc_publish_pending;

// Masks the retirement interrupt for a foreground critical section; defined
// with the injection wrappers further down, declared here because
// link_descriptor_publish() sits beside the RX path that produces its input.
static uint32_t link_retire_irq_mask(void);
static void link_retire_irq_restore(uint32_t was_enabled);

void link_mcu_ready_set(bool ready)
{
    s_ready = ready;
    if (ready) {
        GPIO_PortSet(LINK_READY_GPIO, 1uL << LINK_READY_PIN);
    } else {
        GPIO_PortClear(LINK_READY_GPIO, 1uL << LINK_READY_PIN);
    }
}

static void link_pins_init(void)
{
    CLOCK_EnableClock(kCLOCK_Port3);
    // GPIO3 as well as PORT3: link_wait_for_interframe_gap() reads the chip
    // select out of GPIO3->PDIR while the pin is muxed to LPSPI6, and PDIR
    // reads zero forever without this gate whatever the pad is doing.
    CLOCK_EnableClock(kCLOCK_Gpio3);

    // Fast slew and the input buffer enabled on all four: at 15 MHz SCK the
    // slow-slew pad would not settle, and PCS/SCK/data all need to be readable
    // by the peripheral. No pull on any of them -- the FPGA drives CS, SCK and
    // MOSI actively, and MISO is ours to drive.
    const port_pin_config_t pin = {
        .pullSelect = kPORT_PullDisable,
        .pullValueSelect = kPORT_LowPullResistor,
        .slewRate = kPORT_FastSlewRate,
        .passiveFilterEnable = kPORT_PassiveFilterDisable,
        .openDrainEnable = kPORT_OpenDrainDisable,
        .driveStrength = kPORT_LowDriveStrength,
        .mux = LINK_ALT_DATA,
        .inputBuffer = kPORT_InputBufferEnable,
        .invertInput = kPORT_InputNormal,
        .lockRegister = kPORT_UnlockRegister,
    };
    port_pin_config_t cs = pin;
    cs.mux = LINK_ALT_CS;  // see LINK_ALT_CS: P3_23 is ALT2, not ALT3

    PORT_SetPinConfig(LINK_PORT, LINK_PIN_MOSI, &pin);
    PORT_SetPinConfig(LINK_PORT, LINK_PIN_SCK, &pin);
    PORT_SetPinConfig(LINK_PORT, LINK_PIN_MISO, &pin);
    PORT_SetPinConfig(LINK_PORT, LINK_PIN_CS, &cs);

    // `mcu_ready` starts LOW and stays low until both DMA directions are armed
    // and LPSPI6 is enabled. While it is low the FPGA latches transfer_ready
    // low at every slot start and ignores whatever we return, so a half-built
    // link cannot be validated against.
    PORT_SetPinMux(LINK_READY_PORT, LINK_READY_PIN, kPORT_MuxAlt0);
    const gpio_pin_config_t ready = {
        .pinDirection = kGPIO_DigitalOutput,
        .outputLogic = 0u,
    };
    GPIO_PinInit(LINK_READY_GPIO, LINK_READY_PIN, &ready);
}

static void link_spi_init(void)
{
    CLOCK_AttachClk(kPLL0_to_PLLCLKDIV);
    CLOCK_SetClkDiv(kCLOCK_DivPllClk, 1u);
    CLOCK_SetClkDiv(kCLOCK_DivFlexcom6Clk, LINK_FLEXCOMM_CLOCK_DIV);
    CLOCK_AttachClk(kPLL_DIV_to_FLEXCOMM6);

    // Enables the FlexComm clock, clears its reset, and selects LPSPI. Without
    // this the LPSPI register block reads back as reserved.
    (void)LP_FLEXCOMM_Init(LINK_FLEXCOMM_INSTANCE, LP_FLEXCOMM_PERIPH_LPSPI);

    // Software reset, then flush both FIFOs, then leave the module disabled:
    // CFGR1 and TCR are only writable with CR[MEN] clear.
    LINK_SPI->CR = LPSPI_CR_RST_MASK;
    LINK_SPI->CR = LPSPI_CR_RTF_MASK | LPSPI_CR_RRF_MASK;
    LINK_SPI->CR = 0u;

    LINK_SPI->CFGR1 = LPSPI_CFGR1_MASTER(0u)      // slave; the FPGA drives SCK
                      | LPSPI_CFGR1_PCSPOL(0u)    // PCS0 active low
                      | LPSPI_CFGR1_OUTCFG(0u)    // retain last value off-CS
                      // PINCFG 0b11 = "SOUT is used for input data; SIN is
                      // used for output data". The mikroBUS socket names its
                      // nets for a *master* board, so the FPGA's MOSI arrives
                      // on our SOUT pad (P3_20) and our reply has to leave on
                      // the SIN pad (P3_22). The default 0b00 would drive
                      // P3_20 against the FPGA's own output and listen on the
                      // wire nobody is driving.
                      | LPSPI_CFGR1_PINCFG(3u);

    LINK_SPI->FCR = LPSPI_FCR_TXWATER(LINK_TX_WATERMARK) | LPSPI_FCR_RXWATER(LINK_RX_WATERMARK);

    // TCR[BYSW] = 1. MEASURED, and it agrees with the derivation and with
    // PROVENANCE.md; the earlier `= 0` in this file did not.
    //
    // The derivation: eDMA moves 32-bit words out of a byte array on a
    // little-endian core, so the word handed to the FIFO for slot bytes b0..b3
    // is `b0 | b1<<8 | b2<<16 | b3<<24`; shifted MSB-first that puts b3 on the
    // wire first, and BYSW undoes it. fsl_lpspi.h's kLPSPI_SlaveByteSwap says
    // exactly that, and fsl_lpspi_edma.c sets BYSW from that same flag on the
    // DMA path, where there is no software marshalling either.
    //
    // The bench agrees, and the RECEIVE side is what settles it, because it is
    // read out of memory rather than inferred from a counter. With BYSW=0 the
    // eDMA wrote the FPGA's IDLE slot into s_rx_bank[] as
    // `00 00 00 68 ... 36 fe 00 00` -- every 32-bit word byte-reversed -- and
    // a software-polled read of RDR returned 0x68000000 for slot word 0, i.e.
    // the first wire byte arrives in bits 31..24. Symmetrically the TX word for
    // slot bytes `68 00 00 00` is 0x00000068, which clocks 0x00 out first, and
    // 0x00 is exactly the `rx_sof != 0x68` the FPGA was counting. With BYSW=1
    // the same bank reads back `68 00 00 ... 00 fe 36` and spi_bad_sof stops.
    //
    // An earlier comment here claimed the opposite from a bench run in which
    // "BYSW=1 gave 7,989 bad_sof/s". That measurement cannot separate the two
    // settings: with the byte order wrong in EITHER direction every slot fails
    // its SOF check at the same 1:1 rate, so 1:1 bad_sof is what BOTH values
    // look like if anything else in the chain is also wrong. It is a
    // saturated counter, not a discriminator. The RX bank is the discriminator
    // and it is unambiguous.
    LINK_SPI->TCR = LPSPI_TCR_CPOL(0u)   // mode 0
                    | LPSPI_TCR_CPHA(0u) // mode 0
                    | LPSPI_TCR_LSBF(0u) // MSB first
                    | LPSPI_TCR_BYSW(1u)
                    | LPSPI_TCR_PCS(0u)
                    | LPSPI_TCR_WIDTH(0u)  // single-bit, not dual/quad
                    | LPSPI_TCR_FRAMESZ(LINK_SPI_FRAMESZ);

    LINK_SPI->DER = LPSPI_DER_TDDE_MASK | LPSPI_DER_RDDE_MASK;
}

// Build one direction's two-descriptor ring and install its first descriptor.
// `peripheralAddr` is TDR or RDR; `toPeripheral` picks which side increments.
static void link_dma_ring_init(uint32_t channel,
                               edma_tcd_t tcd[LINK_SLOT_BANKS],
                               uint8_t bank_storage[LINK_SLOT_BANKS][INJ_FRAME_SIZE],
                               // RDR is declared read-only, TDR write-only, so the
                               // one parameter that can name either has to carry const.
                               const volatile uint32_t *peripheral,
                               bool to_peripheral,
                               int32_t request_source,
                               uint16_t interrupt_mask)
{
    for (uint8_t bank = 0u; bank < LINK_SLOT_BANKS; ++bank) {
        edma_transfer_config_t transfer = {
            .srcAddr = to_peripheral ? (uint32_t)bank_storage[bank] : (uint32_t)peripheral,
            .destAddr = to_peripheral ? (uint32_t)peripheral : (uint32_t)bank_storage[bank],
            .srcTransferSize = kEDMA_TransferSize4Bytes,
            .destTransferSize = kEDMA_TransferSize4Bytes,
            .srcOffset = to_peripheral ? (int16_t)LINK_DMA_MINOR_BYTES : (int16_t)0,
            .destOffset = to_peripheral ? (int16_t)0 : (int16_t)LINK_DMA_MINOR_BYTES,
            // One word per peripheral request, eight requests per slot.
            .minorLoopBytes = LINK_DMA_MINOR_BYTES,
            .majorLoopCounts = LINK_DMA_MAJOR_ITER,
            // INTMAJOR on the RX ring and nothing on the TX ring. The rings
            // self-load either way -- the interrupt is not what keeps them
            // turning -- but a completed RX bank has to be retired before the
            // engine comes round to overwrite it, and a bank is 125 us wide.
            .enabledInterruptMask = interrupt_mask,
        };

        // EDMA_TcdSetTransferConfig documents that the caller must reset the
        // descriptor first -- it ORs and ANDs CSR rather than assigning it,
        // and relies on EDMA_TcdReset having set DREQ.
        EDMA_TcdReset(&tcd[bank]);
        // A non-NULL `nextTcd` is what makes this a ring: it sets CSR[ESG] and
        // clears CSR[DREQ], so at major-loop completion the engine loads the
        // next descriptor instead of disarming the channel. Pointing the last
        // descriptor back at the first closes the loop, and nothing else is
        // ever needed to keep it turning. `spi_slots` stuck at 1 on the FPGA
        // is the signature of this not having taken.
        EDMA_TcdSetTransferConfig(&tcd[bank], &transfer, &tcd[link_next_bank(bank)]);
    }

    EDMA_SetChannelMux(LINK_DMA, channel, request_source);
    EDMA_InstallTCD(LINK_DMA, channel, &tcd[0]);
}

static void link_dma_init(void)
{
    CLOCK_EnableClock(kCLOCK_Dma0);

    // Vendor defaults, taken deliberately rather than by omission. Two of them
    // are behaviourally significant here:
    //
    //   enableHaltOnError = true   any channel error sets HALT and stops the
    //                              whole controller until it is cleared, so a
    //                              fault on one direction takes the other down
    //                              with it. That is the right default for a
    //                              link whose two directions are halves of one
    //                              slot -- a half-dead link returning stale
    //                              bytes is worse than a stopped one -- but it
    //                              means step 3's recovery ladder has to clear
    //                              HALT, not just re-arm ERQ.
    //   enableDebugMode = false    the eDMA keeps running while the core is
    //                              halted in the debugger. Worth knowing for
    //                              step 3, which provokes ERR051588 by halting
    //                              the core while the FPGA clocks SCK: the
    //                              rings are self-loading, so a plain halt does
    //                              not by itself starve the TX FIFO.
    edma_config_t config;
    EDMA_GetDefaultConfig(&config);
    EDMA_Init(LINK_DMA, &config);

    link_dma_rings_install();
}

// Build (or rebuild) both rings and install each channel's first descriptor.
// Split out of link_dma_init() because design doc section 4's recovery rung 4
// is "rewrite both TCD rings": after an ERR051588 the descriptors are rebuilt
// from scratch here rather than the channel being nudged, so there is exactly
// one expression of the ring's geometry and the recovery cannot drift from the
// boot path.
static void link_dma_rings_install(void)
{
    link_dma_ring_init(LINK_DMA_CHANNEL_TX, s_tx_tcd, s_tx_bank, &LINK_SPI->TDR, true,
                       (int32_t)kDma0RequestMuxLpFlexcomm6Tx, 0u);
    link_dma_ring_init(LINK_DMA_CHANNEL_RX, s_rx_tcd, s_rx_bank, &LINK_SPI->RDR, false,
                       (int32_t)kDma0RequestMuxLpFlexcomm6Rx,
                       (uint16_t)kEDMA_MajorInterruptEnable);

    // Both channels restart at descriptor 0, so the retirement cursor does
    // too. This is the one place software state about the ring position is
    // set, and it is set from the same call that decides the hardware's.
    s_rx_cursor = 0u;
}


// ---- WIRE PROBE -- retained deliberately ----
// Sample the four FC6 pins as plain GPIO inputs before LPSPI claims them.
// The FPGA drives SCK, CS and MOSI continuously at 8 kHz whatever the byte
// order or the MISO/MOSI orientation, so a pin that never changes state is a
// wire that is not connected. This separates "LPSPI is misconfigured" from
// "the signal never arrives", which no register dump on this side can do.
static void link_wire_probe(void)
{
    CLOCK_EnableClock(kCLOCK_Port3);
    CLOCK_EnableClock(kCLOCK_Gpio3);  // PDIR reads 0 forever without this
    const uint32_t pins[4] = {20u, 21u, 22u, 23u};
    for (uint32_t i = 0u; i < 4u; ++i) {
        PORT_SetPinMux(PORT3, pins[i], kPORT_MuxAlt0);
        PORT3->PCR[pins[i]] |= PORT_PCR_IBE_MASK;
        GPIO3->PDDR &= ~(1uL << pins[i]);  // input
    }

    uint32_t saw_hi = 0u;
    uint32_t saw_lo = 0u;
    for (uint32_t n = 0u; n < 400000u; ++n) {
        uint32_t v = GPIO3->PDIR;
        saw_hi |= v;
        saw_lo |= ~v;
    }

    dbg_puts("-- WIRE PROBE (FPGA drives these at 8 kHz) --\n");
    for (uint32_t i = 0u; i < 4u; ++i) {
        uint32_t m = 1uL << pins[i];
        dbg_puts("  P3_");
        dbg_putc((char)('0' + (pins[i] / 10u)));
        dbg_putc((char)('0' + (pins[i] % 10u)));
        dbg_puts(" : ");
        if (((saw_hi & m) != 0u) && ((saw_lo & m) != 0u)) {
            dbg_puts("TOGGLING  <- wire alive\n");
        } else if ((saw_hi & m) != 0u) {
            dbg_puts("stuck HIGH\n");
        } else {
            dbg_puts("stuck LOW\n");
        }
    }
    dbg_puts("  (SCK=P3_21 and CS=P3_23 MUST toggle)\n");

    // Self-test: does this probe's input path work at all? Drive each pin with
    // the internal pull-up, then the pull-down, and read it back. A pin that
    // follows its own pull is wired to nothing; a pin that IGNORES the pull is
    // being held by something external. A probe that is simply broken reads the
    // same both ways -- which is how we tell "all four dead" apart from "the
    // probe reads zero".
    dbg_puts("-- probe self-test (pull-up / pull-down) --\n");
    for (uint32_t i = 0u; i < 4u; ++i) {
        uint32_t pin = pins[i];
        uint32_t m = 1uL << pin;

        PORT3->PCR[pin] = (PORT3->PCR[pin] & ~(uint32_t)PORT_PCR_PS_MASK)
                          | PORT_PCR_PE_MASK | PORT_PCR_PS_MASK;  // pull-up
        for (volatile uint32_t d = 0u; d < 20000u; ++d) {
        }
        uint32_t up = (GPIO3->PDIR & m) != 0u;

        PORT3->PCR[pin] = (PORT3->PCR[pin] & ~(uint32_t)PORT_PCR_PS_MASK)
                          | PORT_PCR_PE_MASK;  // pull-down
        for (volatile uint32_t d = 0u; d < 20000u; ++d) {
        }
        uint32_t dn = (GPIO3->PDIR & m) != 0u;

        PORT3->PCR[pin] &= ~((uint32_t)PORT_PCR_PE_MASK | (uint32_t)PORT_PCR_PS_MASK);

        dbg_puts("  P3_");
        dbg_putc((char)('0' + (pin / 10u)));
        dbg_putc((char)('0' + (pin % 10u)));
        dbg_puts(" pu=");
        dbg_putc(up ? '1' : '0');
        dbg_puts(" pd=");
        dbg_putc(dn ? '1' : '0');
        if (up && !dn) {
            dbg_puts("  -> input works, pin FLOATING (nothing connected)\n");
        } else if (!up && !dn) {
            dbg_puts("  -> held LOW externally, or input dead\n");
        } else if (up && dn) {
            dbg_puts("  -> held HIGH externally\n");
        } else {
            dbg_puts("  -> inconsistent\n");
        }
    }
}
// ---- END WIRE PROBE ----

// ---- BOOT-TIME REGISTER DUMP -- retained deliberately ----

static void link_delay(uint32_t n)
{
    for (volatile uint32_t d = 0u; d < n; ++d) {
    }
}

static void link_show(const char *name, uint32_t value)
{
    dbg_puts("  ");
    dbg_puts(name);
    dbg_puts(" = ");
    dbg_hex32(value);
    dbg_puts("\n");
}

static void link_dump_channel(const char *tag, uint32_t ch)
{
    dbg_puts(tag);
    link_show("CH_CSR       ", LINK_DMA->CH[ch].CH_CSR);
    link_show("CH_ES        ", LINK_DMA->CH[ch].CH_ES);
    link_show("CH_INT       ", LINK_DMA->CH[ch].CH_INT);
    link_show("CH_MUX       ", LINK_DMA->CH[ch].CH_MUX);
    link_show("TCD_SADDR    ", LINK_DMA->CH[ch].TCD_SADDR);
    link_show("TCD_DADDR    ", LINK_DMA->CH[ch].TCD_DADDR);
    link_show("TCD_SOFF     ", (uint32_t)LINK_DMA->CH[ch].TCD_SOFF);
    link_show("TCD_DOFF     ", (uint32_t)LINK_DMA->CH[ch].TCD_DOFF);
    link_show("TCD_NBYTES   ", LINK_DMA->CH[ch].TCD_NBYTES_MLOFFNO);
    link_show("TCD_ATTR     ", (uint32_t)LINK_DMA->CH[ch].TCD_ATTR);
    link_show("TCD_CITER    ", (uint32_t)LINK_DMA->CH[ch].TCD_CITER_ELINKNO);
    link_show("TCD_BITER    ", (uint32_t)LINK_DMA->CH[ch].TCD_BITER_ELINKNO);
    link_show("TCD_DLAST_SGA", LINK_DMA->CH[ch].TCD_DLAST_SGA);
    link_show("TCD_CSR      ", (uint32_t)LINK_DMA->CH[ch].TCD_CSR);
}

static void link_dump_bytes(const char *tag, const uint8_t *p, uint32_t n)
{
    dbg_puts(tag);
    dbg_puts("\n  ");
    for (uint32_t i = 0u; i < n; ++i) {
        static const char digits[] = "0123456789abcdef";
        dbg_putc(digits[(p[i] >> 4) & 0xFu]);
        dbg_putc(digits[p[i] & 0xFu]);
        dbg_putc(' ');
    }
    dbg_puts("\n");
}

// Watch the live peripheral for a while and report what MOVED, rather than a
// single instantaneous sample. A stuck link and a running one look identical
// in one snapshot of SR.
static void link_watch(void)
{
    uint32_t sr_or = 0u;
    uint32_t rxcount_max = 0u;
    uint32_t txcount_min = 0xFFFFFFFFu;
    uint32_t citer_tx_seen = 0u;
    uint32_t citer_rx_seen = 0u;
    uint32_t saddr_first = LINK_DMA->CH[LINK_DMA_CHANNEL_TX].TCD_SADDR;
    uint32_t saddr_changed = 0u;

    for (uint32_t n = 0u; n < 300000u; ++n) {
        sr_or |= LINK_SPI->SR;
        uint32_t fsr = LINK_SPI->FSR;
        uint32_t rxc = (fsr & LPSPI_FSR_RXCOUNT_MASK) >> LPSPI_FSR_RXCOUNT_SHIFT;
        uint32_t txc = (fsr & LPSPI_FSR_TXCOUNT_MASK) >> LPSPI_FSR_TXCOUNT_SHIFT;
        if (rxc > rxcount_max) {
            rxcount_max = rxc;
        }
        if (txc < txcount_min) {
            txcount_min = txc;
        }
        citer_tx_seen |= 1uL << (LINK_DMA->CH[LINK_DMA_CHANNEL_TX].TCD_CITER_ELINKNO & 0xFu);
        citer_rx_seen |= 1uL << (LINK_DMA->CH[LINK_DMA_CHANNEL_RX].TCD_CITER_ELINKNO & 0xFu);
        if (LINK_DMA->CH[LINK_DMA_CHANNEL_TX].TCD_SADDR != saddr_first) {
            saddr_changed = 1u;
        }
    }

    dbg_puts("-- watch (OR/extremes over ~300k samples) --\n");
    link_show("SR   (or)    ", sr_or);
    link_show("FSR rxcnt max", rxcount_max);
    link_show("FSR txcnt min", txcount_min);
    link_show("tx CITER seen", citer_tx_seen);
    link_show("rx CITER seen", citer_rx_seen);
    link_show("tx SADDR movd", saddr_changed);
    dbg_puts("  (SR bits: 0=TDF 1=RDF 8=WCF 9=FCF 10=TCF 11=TEF 12=REF 24=MBF)\n");
    dbg_puts("  (CITER seen is a BITMASK of observed values; one bit => frozen)\n");
}

// --- RX retirement ---------------------------------------------------------

// Feed retired FPGA->MCU telemetry into the injection session. link_retire_slot
// enqueues every deliverable (SPI_FRAME_OK, non-IDLE) frame; the session only
// acts on REPORT_FRAGMENT (learn the target report's addressing and descriptor
// generation) and MAP_STATUS (the map-commit handshake).
static void link_inject_consume(void)
{
    inj_frame_t frame;
    while (link_retire_receive(&frame)) {
        if (frame.type == INJ_TYPE_REPORT_FRAGMENT) {
            inj_report_fragment_payload_t fragment;
            memcpy(&fragment, frame.payload, sizeof(fragment));
            inj_session_observe_report(&s_inj, &fragment);
        } else if (frame.type == INJ_TYPE_MAP_STATUS) {
            inj_map_status_payload_t status;
            memcpy(&status, frame.payload, sizeof(status));
            inj_session_observe_map_status(&s_inj, &status);
        } else if (frame.type == INJ_TYPE_DESCRIPTOR_FRAGMENT) {
            inj_descriptor_fragment_payload_t fragment;
            memcpy(&fragment, frame.payload, sizeof(fragment));
            if (hid_descriptor_reassembly_push(&s_desc, &fragment) ==
                HID_DESCRIPTOR_FRAGMENT_COMPLETE) {
                // Only a flag here. Publishing means copying up to 2 KB into
                // the shared window, and this runs in the 8 kHz retirement
                // interrupt -- a word-copy of 2 KB is thousands of cycles, and
                // a device cycling descriptor generations could make a
                // completion land every few slots. link_poll() does the copy.
                s_desc_publish_pending = true;
            }
        }
    }
}

// Copy a completed descriptor into the shared window for CPU1 to decode.
//
// Foreground only, and the retirement interrupt is masked for the copy: the
// reassembly buffer is written exclusively by that ISR, so an unmasked copy
// racing a generation change would publish a splice of two devices'
// descriptors -- bytes that decode cleanly into something no device ever sent.
// The mask lasts as long as the copy (a few microseconds against a 125 us slot
// with a two-deep DMA ring), which delays retirement and cannot lose a slot.
//
// `sequence` is the seqlock: odd while writing, even when stable, with the
// stores ordered around it so a reader on the other core cannot see new bytes
// under an old length. Published once per enumeration, so the cost is
// irrelevant and only the correctness of the handshake matters.
static void link_descriptor_publish(void)
{
    const uint32_t was_enabled = link_retire_irq_mask();

    if (!hid_descriptor_reassembly_complete(&s_desc)) {
        // A generation change between the ISR setting the flag and this copy
        // discarded the buffer. Nothing to publish, and the next completion
        // will set the flag again.
        s_desc_publish_pending = false;
        link_retire_irq_restore(was_enabled);
        return;
    }

    const uint8_t *const bytes = hid_descriptor_reassembly_data(&s_desc);
    const size_t length = hid_descriptor_reassembly_length(&s_desc);
    const uint16_t generation = hid_descriptor_reassembly_generation(&s_desc);
    const uint8_t interface_number = hid_descriptor_reassembly_target_interface(&s_desc);

    g_shared_window.descriptor.sequence++;  // -> odd: publish in progress
    __DMB();
    for (size_t i = 0u; i < length && i < SHARED_DESCRIPTOR_CAPACITY; i++) {
        g_shared_window.descriptor.data[i] = bytes[i];
    }
    g_shared_window.descriptor.generation = generation;
    g_shared_window.descriptor.interface_number = interface_number;
    g_shared_window.descriptor.length =
        (uint16_t)((length > SHARED_DESCRIPTOR_CAPACITY) ? SHARED_DESCRIPTOR_CAPACITY : length);
    __DMB();
    g_shared_window.descriptor.sequence++;  // -> even: stable

    s_desc_publish_pending = false;
    link_retire_irq_restore(was_enabled);
}

// Stage the session's next command into the TX bank the DMA does not own, one
// slot ahead of transmission. Mirrors link_drain_rx's ownership rule against the
// live source address; a torn write costs at most one command to a CRC reject,
// which the session re-attempts. With nothing to send the bank is refilled with
// IDLE, so the self-loading ring never re-transmits a stale command.
static void link_inject_refill_tx(void)
{
    uint8_t active;
    if (!link_retire_active_bank(LINK_DMA->CH[LINK_DMA_CHANNEL_TX].TCD_SADDR,
                                 (uint32_t)s_tx_bank[0], INJ_FRAME_SIZE,
                                 (uint8_t)LINK_SLOT_BANKS, &active)) {
        s_saddr_out_of_range++;
        return;
    }
    uint8_t target = link_next_bank(active);
    if (!inj_session_fill_tx(&s_inj, s_tx_bank[target])) {
        link_build_idle_slot(s_tx_bank[target]);
    }
}

// Retire every bank between the cursor and the one the DMA is currently
// writing. Bounded by LINK_SLOT_BANKS iterations by construction: the loop
// stops at `active`, and `active` is always a valid bank index.
//
// This is design doc section 4's "only ever touch the bank the DMA is *not*
// pointing at", implemented rather than assumed. The live TCD destination
// address is the only thing on this part that knows where the engine is; see
// link_retire.h for why a software ping-pong counter is not a substitute.
static void link_drain_rx(void)
{
    uint8_t active;

    // Reflect the transport into the session first. set_link is idempotent: it
    // only arms map upload on the down->up edge and tears all learned state
    // down when the link drops (an ERR051588 recovery holds mcu_ready low for
    // its whole duration, which is exactly when the map must be abandoned).
    inj_session_set_link(&s_inj, s_ready);

    if (!link_retire_active_bank(LINK_DMA->CH[LINK_DMA_CHANNEL_RX].TCD_DADDR,
                                 (uint32_t)s_rx_bank[0], INJ_FRAME_SIZE,
                                 (uint8_t)LINK_SLOT_BANKS, &active)) {
        // The destination address is not inside the bank array at all: a
        // corrupted or never-installed descriptor. Retire nothing -- there is
        // no bank that can be shown to be safe.
        s_daddr_out_of_range++;
        return;
    }

    if (s_rx_cursor == active) {
        // A completion arrived but the only bank we had left to retire is the
        // one now being written. Retirement is a full rotation behind and this
        // slot is lost. Self-healing: the next completion moves `active` on
        // and the cursor is free again.
        s_retire_stalls++;
        return;
    }

    while (s_rx_cursor != active) {
        link_retire_slot(s_rx_bank[s_rx_cursor]);

        // Only touch the injection path while the link is up and steady. During
        // an ERR051588 recovery mcu_ready is low and the ladder is rebuilding
        // the TX banks itself, so retirement and the snapshot below still run
        // but no command is consumed or emitted.
        if (s_ready) {
            link_inject_consume();
        }

        // shared_window_reset() runs after link_init() has enabled this ISR.
        // Its magic-first invalidation prevents the reset from racing this
        // writer; once magic is published, every retired slot gets exactly one
        // snapshot publication at the MCU's own ~8 kHz retirement cadence.
        if (g_shared_window.magic == SHARED_WINDOW_MAGIC) {
            const link_retire_counters_t *counters = link_retire_counters();
            link_snapshot_t snapshot = {0};
            snapshot.slot_counter = counters->slots;
            // Descriptor and map generations now have a real source: what the
            // session last learned from REPORT_FRAGMENT / MAP_STATUS. Each reads
            // zero until the session learns it -- honest rather than invented.
            snapshot.descriptor_generation = s_inj.descriptor_generation;
            snapshot.map_generation = s_inj.active_map_generation;
            snapshot.link_flags = (uint16_t)(
                (s_ready ? INJ_LINK_STATUS_FLAG_RELAY_READY : 0u) |
                (s_inj.active_map_generation != 0u ? INJ_LINK_STATUS_FLAG_MAP_ACTIVE : 0u) |
                (inj_session_phase(&s_inj) == INJ_PHASE_INJECTING
                     ? INJ_LINK_STATUS_FLAG_INJECTION_ENABLED
                     : 0u));
            link_snapshot_publish(&g_shared_window.snapshot, &snapshot);
        }
        s_rx_cursor = link_next_bank(s_rx_cursor);
    }

    // One command per drain, staged a slot ahead of the wire. Kept out of the
    // per-slot loop so a catch-up drain of several banks still emits a single
    // fresh frame rather than racing several writes into one bank.
    if (s_ready) {
        link_inject_refill_tx();
    }
}

// eDMA0 channel 1 major-loop completion: one received slot.
//
// The name is `EDMA_0_CH1_DriverIRQHandler`, not `EDMA_0_CH1_IRQHandler`. The
// vector table entry is a weak trampoline that branches to the Driver name,
// and it is the Driver name that startup_MCXN947_cm33_core0.S `.set`s to
// DefaultISR -- so rooting the vector name would pass the "is it defined?"
// question against the vendor's own trampoline and prove nothing. The
// Makefile's CORE0_RETAIN roots this symbol and `make check` asserts it is a
// strong definition owned by this object.
//
// Retirement runs HERE and not in the foreground. A two-bank ring gives 250 us
// of slack; a foreground that also drives a 115200-baud report spends ~17 ms
// inside one print and would miss seventy rotations. The cost is ~10 us of ISR
// per 125 us slot, nearly all of it the bitwise CRC-16 over 30 bytes.
void EDMA_0_CH1_DriverIRQHandler(void);
void EDMA_0_CH1_DriverIRQHandler(void)
{
    // Clear the channel interrupt request before retiring rather than after.
    // CH_INT is a single W1C bit, so a completion that lands while we are
    // draining coalesces into the next entry either way -- but clearing first
    // means that entry actually happens instead of being cleared away.
    LINK_DMA->CH[LINK_DMA_CHANNEL_RX].CH_INT = DMA_CH_INT_INT_MASK;
    s_isr_entries++;
    link_drain_rx();
}

// --- Frame alignment -------------------------------------------------------
//
// THE MEASUREMENT. A boot can come up with the whole 256-bit slot permanently
// offset, and it is boot-random: two consecutive flashes of a byte-identical
// image, nothing else changed, came up 69 bits out and then perfectly aligned.
// The offset does not decay -- it held for the entire 20 s the board ran.
//
// It is invisible to every status register. No underrun, no overrun, no DMA
// error; SR reads exactly as it does on a healthy link. On the FPGA the only
// symptom is spi_bad_sof at 1:1 with spi_slots, which is the saturated
// non-discriminating signature step 2 already recorded for any whole-slot
// corruption. It was only nameable once retirement made the received bytes
// readable: decoded, the stream was the FPGA's own IDLE keepalive rotated right
// by exactly 69 bits.
//
// THE HYPOTHESIS, which is weaker than the measurement. Enabling the module
// mid-burst leaves the boundary wherever the burst had got to. Waiting for the
// gap between frames before setting CR[MEN] should therefore start it clean.
// Reading the chip select while LPSPI6 is disabled is what that needs, and
// SR[MBF] cannot serve -- a disabled module reports nothing. GPIO3->PDIR can:
// PDIR reflects the pad whatever the PORT mux selects, provided PCR[IBE] is
// set, which link_pins_init() sets on all four link pins for the peripheral's
// own benefit. So the chip select is readable as a plain input at the same time
// as it is wired to PCS0.
//
// EVIDENCE FOR IT: 7 of 7 boots clean with this wait, against 1 of 2 without.
// Suggestive, not proven -- n = 2 on the control side -- and three deliberate
// attempts to reproduce the offset at run time all failed (see
// link_demo_perturb_rx). The mechanism is NOT established, and a claim resting
// on "the boundary is latched at CR[MEN]" should not be built on this.
//
// WHAT THE FRAMING MONITOR DOES AND DOES NOT BUY, corrected against the
// 20-boot experiment that closed step 3 and superseded what stood here:
//
//   * A RECOVERY whose own re-arm lands mis-framed IS repaired. Measured on
//     the bench -- detected and corrected 60 ms later at a cost of 480 bad
//     slots, after which the link ran flat.
//   * A BOOT that comes up mis-framed is NOT. Measured: the ladder ran ~220
//     times against one such boot and never repaired it. It needs a reset.
//     See PROVENANCE.md and design doc section 10.
//
// So the wait is not best-effort scaffolding backed by a converging monitor,
// and the sentence that used to end this block -- "alignment converges rather
// than depending on a lucky boot" -- was measured false. The wait is the only
// mitigation there is for the boot case (20/20 with it, 16/20 without), and
// the monitor's role there is to NAME the condition, not to fix it.
#define LINK_GAP_WAIT_SPINS 4000000u

// Wait for chip select to be ASSERTED and then RELEASED, so what follows is the
// start of an idle window rather than its tail. At 8 kHz with a ~17 us burst
// the idle window is ~108 us, which is an eternity next to the two register
// writes that follow. Returns false if either edge did not arrive inside the
// spin budget -- the caller proceeds anyway, because an unaligned link that the
// monitor will repair beats a link that never comes up at all.
#if !defined(LINK_SKIP_GAP_WAIT)
static bool link_wait_for_interframe_gap(void)
{
    const uint32_t cs = 1uL << LINK_PIN_CS;  // active low
    uint32_t spins = 0u;

    while ((GPIO3->PDIR & cs) != 0u) {
        if (++spins >= LINK_GAP_WAIT_SPINS) {
            return false;
        }
    }
    spins = 0u;
    while ((GPIO3->PDIR & cs) == 0u) {
        if (++spins >= LINK_GAP_WAIT_SPINS) {
            return false;
        }
    }
    return true;
}
#endif  // !LINK_SKIP_GAP_WAIT

static uint32_t s_gap_wait_timeouts;

// Enable LPSPI6 on a frame boundary. The only place CR[MEN] is ever set.
static void link_spi_enable_aligned(void)
{
    // TEMPORARY EXPERIMENT SWITCH -- REMOVE WITH THE SCAFFOLDING.
    // -DLINK_SKIP_GAP_WAIT builds the control arm: enable the module without
    // waiting for the inter-frame gap. The gap wait is a HYPOTHESIS about the
    // boot-random frame offset, measured at 7/7 clean with it and 1/2 without,
    // and n=2 is not a prior. This switch exists to get one -- and it has now
    // given one: 20/20 clean with the wait against 16/20 without.
    //
    // The line that used to stand here called the control arm safe "because
    // link_retire_framing_lost() + the section 4 ladder repair a mis-framed
    // boot within ~60 ms". That was measured false; the ladder does not repair
    // a mis-framed boot at all. A control-arm boot that comes up offset stays
    // offset until it is reset. The switch is safe to build, not to rely on.
#if !defined(LINK_SKIP_GAP_WAIT)
    if (!link_wait_for_interframe_gap()) {
        s_gap_wait_timeouts++;
    }
#endif
    LINK_SPI->CR = LPSPI_CR_MEN_MASK;
}

// --- ERR051588 -------------------------------------------------------------

// Bounded spin for CH_CSR[ACTIVE] to fall. A channel is at most one 4-byte
// minor loop from idle once its request is off, so this is orders of magnitude
// more than needed; it exists so a wedged controller cannot hang the recovery.
#define LINK_RECOVERY_WAIT_SPINS 100000u

// Storm guard: the minimum gap between two recoveries. See link_poll().
#define LINK_RECOVERY_MIN_GAP_MS 20u

static void link_read_fault(link_fault_t *fault)
{
    const uint32_t sr = LINK_SPI->SR;
    const uint32_t tx_es = LINK_DMA->CH[LINK_DMA_CHANNEL_TX].CH_ES;
    const uint32_t rx_es = LINK_DMA->CH[LINK_DMA_CHANNEL_RX].CH_ES;
    const uint32_t tx_csr = LINK_DMA->CH[LINK_DMA_CHANNEL_TX].CH_CSR;
    const uint32_t rx_csr = LINK_DMA->CH[LINK_DMA_CHANNEL_RX].CH_CSR;

    fault->lpspi_transmit_error = (sr & LPSPI_SR_TEF_MASK) != 0u;
    fault->lpspi_receive_error = (sr & LPSPI_SR_REF_MASK) != 0u;
    fault->dma_channel_error = ((tx_es | rx_es) & DMA_CH_ES_ERR_MASK) != 0u;
    fault->dma_controller_halted = (LINK_DMA->MP_CSR & DMA_MP_CSR_HALT_MASK) != 0u;
    // Read, reported, and deliberately not acted on. See link_recovery.h: DONE
    // is sticky status that a scatter-gather reload never clears, so on a
    // healthy self-loading ring it reads set forever.
    fault->dma_channel_done_latched = ((tx_csr | rx_csr) & DMA_CH_CSR_DONE_MASK) != 0u;

    // The one term that comes from the retired content rather than a register.
    // Filled in by link_poll(), which owns the sampling window.
    fault->receive_framing_lost = false;
}

// One rung of design doc section 4's ladder. The ORDER is not decided here --
// link_recovery.c owns it and test/link_recovery_test.c asserts each ordering
// clause the design states. This function only knows how to perform a rung.
static void link_recovery_apply(void *ctx, link_recovery_op_t op)
{
    (void)ctx;

    switch (op) {
    case LINK_RECOVERY_OP_MCU_READY_LOW:
        // Rung 1, and it must be first. `send_message = mcu_ready & tx_queued`
        // on the FPGA, so this stops it consuming what we emit; and all four
        // spi_bad_* counters are gated on `transfer_ready`, latched from this
        // pin at slot start, so the garbage still in flight is ignored rather
        // than counted against us.
        link_mcu_ready_set(false);
        break;

    case LINK_RECOVERY_OP_REQUESTS_OFF:
        EDMA_DisableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_TX);
        EDMA_DisableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_RX);
        // Mask the retirement ISR for the same window. Nothing may retire a
        // bank while the descriptors are being rewritten underneath it.
        NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
        break;

    case LINK_RECOVERY_OP_WAIT_CHANNELS_IDLE: {
        uint32_t spins = 0u;
        while (((LINK_DMA->CH[LINK_DMA_CHANNEL_TX].CH_CSR |
                 LINK_DMA->CH[LINK_DMA_CHANNEL_RX].CH_CSR) &
                DMA_CH_CSR_ACTIVE_MASK) != 0u) {
            if (++spins >= LINK_RECOVERY_WAIT_SPINS) {
                s_recovery_wait_timeouts++;
                break;
            }
        }
        break;
    }

    case LINK_RECOVERY_OP_RESET_FIFOS:
        // Rung 3, and the erratum's own workaround: "reset the transmit FIFO
        // (CR[RTF] = 1) before writing any new data". RRF goes with it because
        // the receive FIFO may hold the tail of a frame that no longer has a
        // matching transmit side. MEN is left alone -- RTF/RRF are
        // self-clearing commands and the vendor's own LPSPI_FlushFifo() writes
        // them with the module enabled.
        LINK_SPI->CR |= LPSPI_CR_RTF_MASK | LPSPI_CR_RRF_MASK;
        // SR's error flags are W1C. TCF is cleared with them so the next
        // frame-complete is unambiguous.
        LINK_SPI->SR = LPSPI_SR_TEF_MASK | LPSPI_SR_REF_MASK | LPSPI_SR_TCF_MASK;
        // ...and then disable the module. This is a DEVIATION from design doc
        // section 4's ladder, which does not mention MEN, and it is what makes
        // the ladder able to repair a mis-framed slave rather than only an
        // underrun one: the frame boundary is latched at MEN and a disabled
        // module ignores SCK entirely, so dropping it here is what lets rung 5
        // choose a new boundary in the inter-frame gap.
        LINK_SPI->CR &= ~(uint32_t)LPSPI_CR_MEN_MASK;
        break;

    case LINK_RECOVERY_OP_CLEAR_CHANNEL_STATE:
        // Rung 4a. DONE is cleared because the design says to, not because it
        // meant anything; ERROR and HALT are cleared because they do.
        // enableHaltOnError is true by default, so an error on either channel
        // stops BOTH, and a TCD installed into a halted controller arms
        // nothing at all.
        EDMA_ClearChannelStatusFlags(LINK_DMA, LINK_DMA_CHANNEL_TX,
                                     (uint32_t)kEDMA_DoneFlag | (uint32_t)kEDMA_ErrorFlag |
                                         (uint32_t)kEDMA_InterruptFlag);
        EDMA_ClearChannelStatusFlags(LINK_DMA, LINK_DMA_CHANNEL_RX,
                                     (uint32_t)kEDMA_DoneFlag | (uint32_t)kEDMA_ErrorFlag |
                                         (uint32_t)kEDMA_InterruptFlag);
        LINK_DMA->MP_CSR &= ~(uint32_t)DMA_MP_CSR_HALT_MASK;
        break;

    case LINK_RECOVERY_OP_REARM_RINGS:
        // Rung 4b. Re-seed both TX banks first, then rewrite the descriptors:
        // the steady-state invariant is that the buffer the DMA will next read
        // is never empty and never half written, and the moment the descriptor
        // is installed is the moment that becomes true again.
        for (uint8_t bank = 0u; bank < LINK_SLOT_BANKS; ++bank) {
            link_build_idle_slot(s_tx_bank[bank]);
        }
        link_dma_rings_install();
        break;

    case LINK_RECOVERY_OP_REQUESTS_ON: {
        NVIC_ClearPendingIRQ(EDMA_0_CH1_IRQn);
        NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
        EDMA_EnableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_TX);
        EDMA_EnableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_RX);

        // The module comes back on a frame boundary, and only here -- the same
        // call the boot path uses, so there is one expression of the rule.
        link_spi_enable_aligned();

        // Do not leave until the FIFO actually holds a whole slot again. The
        // steady-state invariant is that the buffer the DMA will next read is
        // never empty; the FIFO is 8 x 32 bits = exactly one slot, and the eight
        // DMA moves that fill it take well under a microsecond against the
        // ~108 us of idle wire this rung is standing in. Leaving early would
        // hand the next frame a half-full FIFO, which underruns -- the fault,
        // again, manufactured by its own recovery.
        uint32_t spins = 0u;
        while (((LINK_SPI->FSR & LPSPI_FSR_TXCOUNT_MASK) >> LPSPI_FSR_TXCOUNT_SHIFT) <
               (LINK_TX_WATERMARK + 1u)) {
            if (++spins >= LINK_RECOVERY_WAIT_SPINS) {
                s_recovery_fill_timeouts++;
                break;
            }
        }
        break;
    }

    case LINK_RECOVERY_OP_MCU_READY_HIGH:
        // Rung 6, and it must be last: this is the edge that makes the FPGA
        // start scoring us again, so nothing may still be half-armed.
        //
        // SR is cleared once more immediately before the edge. The window
        // between the FIFO reset in rung 3 and the refill in rung 5 is one in
        // which the FPGA is still clocking an empty transmit FIFO, so it sets
        // SR[TEF] by construction -- a fault caused by the recovery rather than
        // found by it. Leaving it latched would have the monitor read it on its
        // very next pass and recover forever, 8,000 times a second.
        LINK_SPI->SR = LPSPI_SR_TEF_MASK | LPSPI_SR_REF_MASK | LPSPI_SR_TCF_MASK;
        link_mcu_ready_set(true);
        break;

    case LINK_RECOVERY_OP_COUNT:
    default:
        break;
    }
}

static void link_recover(const link_fault_t *fault)
{
    s_recoveries++;
    if (fault->receive_framing_lost) {
        s_framing_recoveries++;
    }
    s_last_fault_sr = LINK_SPI->SR;

    dbg_puts("!! ERR051588 recovery #");
    dbg_dec32(s_recoveries);
    dbg_puts("  SR=");
    dbg_hex32(s_last_fault_sr);
    dbg_puts(fault->lpspi_transmit_error ? " TEF" : "");
    dbg_puts(fault->lpspi_receive_error ? " REF" : "");
    dbg_puts(fault->dma_channel_error ? " CH_ES" : "");
    dbg_puts(fault->dma_controller_halted ? " HALT" : "");
    dbg_puts(fault->receive_framing_lost ? " FRAMING" : "");
    dbg_puts("\n");

    (void)link_recovery_run(link_recovery_apply, NULL);

    dbg_puts("   recovered, SR=");
    dbg_hex32(LINK_SPI->SR);
    dbg_puts(" FSR=");
    dbg_hex32(LINK_SPI->FSR);
    dbg_puts(" idle_timeouts=");
    dbg_dec32(s_recovery_wait_timeouts);
    dbg_puts(" fill_timeouts=");
    dbg_dec32(s_recovery_fill_timeouts);
    dbg_puts("\n");
}

// --- The one-shot ERR051588 provocation ------------------------------------
//
// Design doc section 9 step 3: "Includes deliberately provoking ERR051588 by
// halting the core while the FPGA clocks SCK. A recovery path that has never
// run is not a recovery path."
//
// A debugger halt is NOT the mechanism, and step 2 already recorded why:
// EDMA_GetDefaultConfig leaves `enableDebugMode = false`, so the eDMA keeps
// running while the core is stopped. The rings are self-loading, so a plain
// halt does not starve the transmit FIFO and produces no underrun at all.
//
// What does produce one, deterministically and on demand, is taking the
// transmit channel's request away while the FPGA keeps clocking: the FIFO
// holds exactly one slot, so it is empty within 125 us and the next 256 clocks
// underrun. That is ERR051588's own trigger -- "a transmit FIFO underrun
// (SR[TEF]) in slave mode" -- reached by the same route a reset window or an
// error halt would reach it, and unlike a debugger halt it is repeatable.
//
// The schedule is driven by the retirement counter rather than by wall-clock,
// so every phase boundary is a fixed number of slots and the FPGA-side sampling
// windows line up with it. The three phases are arranged to make the evidence
// falsifiable:
//
//   ARMED     ~10 s of untouched steady state, so "flat before" is measured.
//   STARVING  ~1 s with the TX request off. The underrun happens here.
//   BROKEN    ~20 s with the request back ON and NO recovery. If ERR051588 did
//             not really corrupt the FIFO pointers, the link would heal itself
//             here and the FPGA counters would stay flat. They do not, and that
//             is what makes the recovery in the next phase mean something.
//   DONE      recovery has run; the automatic fault monitor is armed from here.
typedef enum {
    LINK_DEMO_ARMED = 0,
    LINK_DEMO_STARVING,
    LINK_DEMO_UNDERRUN_HELD,
    LINK_DEMO_PERTURBED_HELD,
    LINK_DEMO_DONE,
} link_demo_phase_t;

#define LINK_DEMO_ARM_SLOTS 160000u    // ~20 s at 8 kHz
#define LINK_DEMO_STARVE_SLOTS 8000u   // ~1 s
#define LINK_DEMO_HOLD_SLOTS 80000u    // ~10 s per broken window

static link_demo_phase_t s_demo_phase = LINK_DEMO_ARMED;
static uint32_t s_demo_mark;

static const char *link_demo_phase_name(void)
{
    switch (s_demo_phase) {
    case LINK_DEMO_ARMED:
        return "armed";
    case LINK_DEMO_STARVING:
        return "STARVING";
    case LINK_DEMO_UNDERRUN_HELD:
        return "UNDERRUN";
    case LINK_DEMO_PERTURBED_HELD:
        return "PERTURB";
    case LINK_DEMO_DONE:
    default:
        return "steady";
    }
}

// The automatic monitor is suppressed for exactly the windows the provocation
// owns. Everywhere else -- including before the provocation, so a fault
// inherited from the boot or from a flash cycle is caught -- it is armed.
static bool link_fault_monitor_armed(void)
{
    return s_demo_phase == LINK_DEMO_ARMED || s_demo_phase == LINK_DEMO_DONE;
}

// A NEGATIVE CONTROL, and it is labelled one because it is what it measured.
//
// The intent was to reproduce the persistent frame offset seen on a boot, by
// stealing three words out of the receive FIFO before the eDMA could move them.
// The reasoning: the ring writes eight words per bank and the FPGA clocks eight
// words per slot, so taking three away should leave every bank holding the last
// five words of one slot followed by the first three of the next, forever.
//
// IT DOES NOT. Ten seconds after the theft, `sof` is still zero and every FPGA
// counter is still flat. The receive path re-synchronises on its own.
//
// It is the third provocation to fail that way, and together they are the
// finding, which contradicts the obvious reading of the boot symptom:
//
//   * cycling CR[MEN] mid-burst -- no effect, ten seconds, not one bad slot.
//     So the frame boundary is NOT simply latched at the module enable.
//   * starving the transmit FIFO (provocation 1 above) -- sets SR[TEF] exactly
//     as ERR051588 says, and then the transmit path self-heals once the request
//     returns. Measured three times, ten seconds each.
//   * this -- the receive path likewise re-synchronises.
//
// So the link tolerates every mid-run perturbation that has been tried, and the
// persistent 69-bit offset measured once at boot has a cause none of these
// reproduces. link_spi_enable_aligned() is a hypothesis about that cause with
// 7 of 7 clean boots behind it and 1 of 2 before it -- suggestive, not proven --
// and the framing monitor is the backstop that makes it not matter: a boot that
// comes up mis-framed repairs itself whether or not the hypothesis is right.
//
// Kept rather than deleted because "the link survives three words stolen out of
// its receive FIFO" is a robustness property worth re-measuring after any change
// to the ring, and because a provocation nobody records having tried gets tried
// again.
static void link_demo_perturb_rx(void)
{
    // Masked, so the retirement ISR cannot drain the FIFO between the wait and
    // the read and leave us reading an empty RDR (which consumes nothing and
    // would shift nothing).
    NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
    for (uint32_t word = 0u; word < 3u; ++word) {
        uint32_t spins = 0u;
        while (((LINK_SPI->FSR & LPSPI_FSR_RXCOUNT_MASK) >> LPSPI_FSR_RXCOUNT_SHIFT) == 0u) {
            if (++spins >= LINK_GAP_WAIT_SPINS) {
                break;
            }
        }
        (void)LINK_SPI->RDR;
    }
    NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
}

static void link_demo_step(uint32_t slots)
{
    switch (s_demo_phase) {
    case LINK_DEMO_ARMED:
        if (slots >= LINK_DEMO_ARM_SLOTS) {
            dbg_puts("\n-- provocation 1/2: ERR051588, TX DMA request off --\n");
            EDMA_DisableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_TX);
            s_demo_mark = slots;
            s_demo_phase = LINK_DEMO_STARVING;
        }
        break;

    case LINK_DEMO_STARVING:
        if (slots - s_demo_mark >= LINK_DEMO_STARVE_SLOTS) {
            const uint32_t sr = LINK_SPI->SR;
            EDMA_EnableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_TX);
            dbg_puts("-- TX request back on, monitor still OFF. SR=");
            dbg_hex32(sr);
            dbg_puts((sr & LPSPI_SR_TEF_MASK) != 0u ? "  TEF SET (underrun)\n"
                                                    : "  TEF CLEAR (no underrun!)\n");
            s_demo_mark = slots;
            s_demo_phase = LINK_DEMO_UNDERRUN_HELD;
        }
        break;

    case LINK_DEMO_UNDERRUN_HELD:
        if (slots - s_demo_mark >= LINK_DEMO_HOLD_SLOTS) {
            link_fault_t fault;
            link_read_fault(&fault);
            dbg_puts("-- running the section 4 ladder on the latched TEF --\n");
            if (link_recovery_required(&fault)) {
                link_recover(&fault);
            } else {
                dbg_puts("   nothing to recover; fault predicate says clean\n");
            }

            dbg_puts("\n-- provocation 2/2 (negative control): steal 3 RX FIFO words --\n");
            link_demo_perturb_rx();
            s_demo_mark = slots;
            s_demo_phase = LINK_DEMO_PERTURBED_HELD;
        }
        break;

    case LINK_DEMO_PERTURBED_HELD:
        if (slots - s_demo_mark >= LINK_DEMO_HOLD_SLOTS) {
            // Nothing is called here. The monitor is simply armed, and the
            // repair has to happen on its own -- that is the whole point. A
            // ladder someone has to invoke by hand is not a recovery path.
            dbg_puts("-- monitor ARMED; a link still broken must now repair itself --\n");
            s_demo_phase = LINK_DEMO_DONE;
        }
        break;

    case LINK_DEMO_DONE:
    default:
        break;
    }
}

// --- Framing monitor -------------------------------------------------------
//
// Samples the retirement counters on a fixed cadence and asks link_retire.c
// whether anything parsed in the interval. The window has to be long enough to
// clear LINK_FRAMING_MIN_SLOTS (200 slots, ~25 ms) and short enough that a
// mis-framed boot is repaired long before anyone looks at it.
#define LINK_FRAMING_WINDOW_MS 50u

static link_framing_sample_t s_framing_previous;
static uint32_t s_framing_ms;

// Returns the verdict for the window that just closed, or false while one is
// still open. Sampling here rather than inside link_read_fault() keeps the
// window under this function's control: a verdict is only ever computed from
// two samples a whole window apart, never from two taken microseconds apart by
// a fast foreground loop.
// --- Fault classification ---------------------------------------------------
//
// link_retire_framing_lost() answers one bit: is the link mis-framed. That is
// all the recovery ladder needs, and far less than a person standing at the
// bench needs, because every wire in the link fails as "no frames arrive".
// link_fault_classify() turns the same window into a named suspect pin, and
// this is where the window is measured and the verdict published to CPU1.
//
// Deltas, not totals. A cumulative counter would leave a link that was broken
// at boot and healthy since reading as broken forever, which is exactly the
// trap the framing monitor's two-sample window already avoids.
static link_retire_counters_t s_fault_counters_previous;
static uint32_t s_fault_isr_previous;
// Kept so link_report() can print the same verdict CPU1 is rendering. The
// panel is the surface this is FOR, but a verdict that disagreed with the one
// on the UART would be a debugging problem of its own, so both read one value.
static link_fault_report_t s_fault_report;

static void link_fault_publish(void)
{
    const link_retire_counters_t *const c = link_retire_counters();

    link_fault_evidence_t evidence = {
        .slots = c->slots - s_fault_counters_previous.slots,
        .idle = c->idle - s_fault_counters_previous.idle,
        .deliverable = c->deliverable - s_fault_counters_previous.deliverable,
        .bad_sof = c->bad_sof - s_fault_counters_previous.bad_sof,
        .bad_crc = c->bad_crc - s_fault_counters_previous.bad_crc,
        .bad_length = c->bad_length - s_fault_counters_previous.bad_length,
        .bad_type = c->bad_type - s_fault_counters_previous.bad_type,
        .isr_entries = s_isr_entries - s_fault_isr_previous,
        .bank_valid = true,
    };
    s_fault_counters_previous = *c;
    s_fault_isr_previous = s_isr_entries;

    // Masked for the copy, for the reason link_retire_last_slot() spells out:
    // the ISR rewrites that buffer 8,000 times a second, and an unmasked read
    // splices several slots into one bank. Here that would not merely look
    // wrong, it would MANUFACTURE A VERDICT -- a spliced bank is never all
    // 00 or all ff, so a genuinely undriven MOSI would be misreported as a
    // framing fault and send someone to the wrong wire.
    const uint32_t was_enabled = link_retire_irq_mask();
    const uint8_t *const last = link_retire_last_slot();
    for (uint32_t index = 0u; index < INJ_FRAME_SIZE; ++index) {
        evidence.bank[index] = last[index];
    }
    link_retire_irq_restore(was_enabled);

    link_fault_classify(&evidence, &s_fault_report);

    // Same seqlock discipline as the descriptor publish: odd while in
    // progress, even when stable, with a barrier on each side.
    g_shared_window.fault.sequence++;
    __DMB();
    g_shared_window.fault.verdict = (uint8_t)s_fault_report.verdict;
    g_shared_window.fault.suspect_count = s_fault_report.suspect_count;
    for (uint32_t index = 0u; index < SHARED_FAULT_MAX_SUSPECTS; ++index) {
        g_shared_window.fault.suspect[index] = (uint8_t)s_fault_report.suspect[index];
    }
    __DMB();
    g_shared_window.fault.sequence++;
}

static bool link_framing_poll(void)
{
    const uint32_t now = platform_ticks();
    if (now - s_framing_ms < LINK_FRAMING_WINDOW_MS) {
        return false;
    }
    s_framing_ms = now;

    // Classified on the same boundary rather than on its own timer: both want
    // one verdict per closed window, and a second cadence would drift against
    // this one and occasionally classify a window that straddles a recovery.
    link_fault_publish();

    link_framing_sample_t current;
    link_retire_framing_sample(&current);
    const bool lost = link_retire_framing_lost(&s_framing_previous, &current);
    s_framing_previous = current;
    return lost;
}

// --- Snapshots for the console ---------------------------------------------
//
// Both mask the retirement interrupt for the copy. See link.h: the ISR runs
// at 8 kHz, so an unmasked field-by-field read would splice several slots'
// worth of counters into one apparently coherent sample.

void link_counters_read(link_retire_counters_t *out)
{
    NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
    *out = *link_retire_counters();
    NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
}

void link_diagnostics_read(link_diagnostics_t *out)
{
    NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
    out->isr_entries = s_isr_entries;
    out->retire_stalls = s_retire_stalls;
    out->daddr_out_of_range = s_daddr_out_of_range;
    NVIC_EnableIRQ(EDMA_0_CH1_IRQn);

    // The rest are foreground-only: link_poll() is their sole writer and it
    // is the same context the console runs in, so masking would assert
    // nothing.
    out->recoveries = s_recoveries;
    out->framing_recoveries = s_framing_recoveries;
    out->gap_wait_timeouts = s_gap_wait_timeouts;
    out->ready = s_ready;
}

// --- Driving injection from the foreground loop -----------------------------
//
// See link.h. The request is written with the retirement interrupt masked
// because it only becomes meaningful once every field and the pending flag are
// in place, and the next slot is at most 125 us away -- unmasked, the ISR could
// stage a command built from half a request.
//
// Unlike the two readers above, this restores the interrupt's PREVIOUS enable
// state instead of unconditionally enabling it. Both of those are
// foreground-only today, so the difference is latent there rather than live;
// doing it properly here costs one register read and means a caller that is
// already inside a masked region cannot silently re-enable the 8 kHz ISR
// underneath itself.
static uint32_t link_retire_irq_mask(void)
{
    const uint32_t was_enabled = NVIC_GetEnableIRQ(EDMA_0_CH1_IRQn);
    NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
    return was_enabled;
}

static void link_retire_irq_restore(uint32_t was_enabled)
{
    if (was_enabled != 0u) {
        NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
    }
}

bool link_inject_request_relative(int16_t x, int16_t y, int16_t wheel, int16_t pan)
{
    const uint32_t was_enabled = link_retire_irq_mask();
    const bool queued = inj_session_request_relative(&s_inj, x, y, wheel, pan);
    link_retire_irq_restore(was_enabled);
    return queued;
}

bool link_inject_request_buttons(uint64_t mask, uint16_t hold_reports)
{
    const uint32_t was_enabled = link_retire_irq_mask();
    const bool queued = inj_session_request_buttons(&s_inj, mask, hold_reports);
    link_retire_irq_restore(was_enabled);
    return queued;
}

bool link_inject_request_physical_mask(uint64_t button_mask)
{
    const uint32_t was_enabled = link_retire_irq_mask();
    const bool queued = inj_session_request_physical_mask(&s_inj, button_mask);
    link_retire_irq_restore(was_enabled);
    return queued;
}

bool link_inject_ready(void)
{
    // Two aligned single-word reads of state the ISR owns. Masking would not
    // make the pair atomic and there is nothing to tear: a stale answer costs
    // at worst one refused request, which the caller retries on its next step.
    return s_ready && (inj_session_phase(&s_inj) == INJ_PHASE_INJECTING);
}

// --- Periodic report -------------------------------------------------------

#define LINK_REPORT_MS 1000u

static uint32_t s_report_ms;

static void link_report(void)
{
    const link_retire_counters_t *c = link_retire_counters();

    dbg_puts("[");
    dbg_puts(link_demo_phase_name());
    dbg_puts("] slots=");
    dbg_dec32(c->slots);
    dbg_puts(" idle=");
    dbg_dec32(c->idle);
    dbg_puts(" deliv=");
    dbg_dec32(c->deliverable);
    dbg_puts(" sof=");
    dbg_dec32(c->bad_sof);
    dbg_puts(" crc=");
    dbg_dec32(c->bad_crc);
    dbg_puts(" len=");
    dbg_dec32(c->bad_length);
    dbg_puts(" typ=");
    dbg_dec32(c->bad_type);
    dbg_puts(" dup=");
    dbg_dec32(c->duplicate);
    dbg_puts(" stale=");
    dbg_dec32(c->stale);
    dbg_puts(" gap=");
    dbg_dec32(c->sequence_gap);
    dbg_puts("\n      isr=");
    dbg_dec32(s_isr_entries);
    dbg_puts(" stall=");
    dbg_dec32(s_retire_stalls);
    dbg_puts(" daddr_err=");
    dbg_dec32(s_daddr_out_of_range);
    dbg_puts(" recov=");
    dbg_dec32(s_recoveries);
    dbg_puts("/");
    dbg_dec32(s_framing_recoveries);
    dbg_puts("fr gapto=");
    dbg_dec32(s_gap_wait_timeouts);
    dbg_puts(" cursor=");
    dbg_dec32(s_rx_cursor);
    dbg_puts(" SR=");
    dbg_hex32(LINK_SPI->SR);
    dbg_puts(" ms=");
    dbg_dec32(platform_ticks());

    // The classifier's verdict, and the wires it implicates with the physical
    // location of each. This is the same s_fault_report CPU1 renders; printing
    // it here costs one line a second and means a bench session without a
    // panel attached is not blind to it.
    dbg_puts("\n      link=");
    dbg_puts(link_fault_verdict_name(s_fault_report.verdict));
    for (uint8_t suspect = 0u; suspect < s_fault_report.suspect_count; ++suspect) {
        dbg_puts(suspect == 0u ? " check " : ", ");
        dbg_puts(link_pin_name(s_fault_report.suspect[suspect]));
        dbg_puts(" (");
        dbg_puts(link_pin_location(s_fault_report.suspect[suspect]));
        dbg_puts(")");
    }
    // The last bank retired, verbatim. This is the evidence that retirement is
    // reading bytes the FPGA put there: an untouched bank is all zeros, and the
    // keepalive it actually sends is 68 00 00 00 .. 00 fe 36.
    //
    // Snapshotted with the retirement ISR masked, and that is not a nicety. The
    // hex dump takes ~30 ms at 115200 baud and the ISR rewrites the source
    // buffer 240 times in that window, so printing straight from the pointer
    // splices hundreds of slots into one line -- a healthy keepalive acquires a
    // scatter of bytes it never carried, and on the bench that briefly looked
    // like wire corruption. Masking for a 32-byte copy costs tens of
    // nanoseconds against a 125 us slot.
    uint8_t snapshot[INJ_FRAME_SIZE];
    NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
    for (uint32_t index = 0u; index < INJ_FRAME_SIZE; ++index) {
        snapshot[index] = link_retire_last_slot()[index];
    }
    NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
    link_dump_bytes("\n      last slot", snapshot, INJ_FRAME_SIZE);
}

void link_poll(void)
{
    const uint32_t slots = link_retire_counters()->slots;

    // Sampled unconditionally, acted on only when armed. Sampling inside the
    // armed branch instead would leave s_framing_previous stale across a
    // disarmed window, so the first verdict after re-arming would be computed
    // against a sample from minutes earlier.
    const bool framing_lost = link_framing_poll();

    link_demo_step(slots);

    // Once per enumeration, not per pass: the ISR only raises the flag.
    if (s_desc_publish_pending) {
        link_descriptor_publish();
    }

    if (link_fault_monitor_armed()) {
        link_fault_t fault;
        link_read_fault(&fault);
        fault.receive_framing_lost = framing_lost;
        // One recovery per LINK_RECOVERY_MIN_GAP_MS at most. A recovery that
        // re-creates its own trigger would otherwise run at the poll rate and
        // do nothing but flood the UART and drop `mcu_ready` thousands of times
        // a second. Rate-limited, the same bug shows as `recov` climbing
        // steadily in the report, which is a diagnosis rather than a brick.
        const uint32_t since = platform_ticks() - s_recovery_last_ms;
        if (link_recovery_required(&fault) &&
            (s_recoveries == 0u || since >= LINK_RECOVERY_MIN_GAP_MS)) {
            s_recovery_last_ms = platform_ticks();
            link_recover(&fault);
        }
    }

    // Wall-clock rather than slot-driven, deliberately: a link that has stopped
    // delivering slots is the case that most needs a report, and a slot-driven
    // cadence would go silent exactly then.
    const uint32_t now = platform_ticks();
    if (now - s_report_ms >= LINK_REPORT_MS) {
        s_report_ms = now;
        link_report();
    }
}

void link_init(void)
{
    dbg_uart_init();
    link_wire_probe();

    link_pins_init();
    link_mcu_ready_set(false);

    // Retirement state before anything can complete into it.
    link_retire_reset();

    // The injection session, before the RX ISR that feeds and drains it is armed.
    inj_session_init(&s_inj);
    // Interface 0: enumeration only commits a HID boot mouse and that is the
    // interface its report descriptor arrives on. A DESCRIPTOR_FRAGMENT for any
    // other interface is ignored rather than spliced in -- see
    // hid_descriptor_reassembly.h on why splicing is the failure that matters.
    hid_descriptor_reassembly_init(&s_desc, 0u);
    s_desc_publish_pending = false;

    for (uint8_t bank = 0u; bank < LINK_SLOT_BANKS; ++bank) {
        link_build_idle_slot(s_tx_bank[bank]);
        for (uint32_t index = 0u; index < INJ_FRAME_SIZE; ++index) {
            s_rx_bank[bank][index] = 0u;
        }
    }

    link_spi_init();
    link_dma_init();

    EDMA_EnableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_TX);
    EDMA_EnableChannelRequest(LINK_DMA, LINK_DMA_CHANNEL_RX);

    // The RX retirement interrupt, armed before the module is enabled so the
    // first completion is not missed. The RX banks are zeroed above, so if the
    // ISR somehow ran before any transfer it would retire zeros and score them
    // as bad_sof -- which is a visible failure rather than a silent one.
    NVIC_ClearPendingIRQ(EDMA_0_CH1_IRQn);
    NVIC_EnableIRQ(EDMA_0_CH1_IRQn);

    link_spi_enable_aligned();

    link_mcu_ready_set(true);

    dbg_puts("\n==== hurra mcxn947 step4: link + CDC console ====\n");

    dbg_puts("-- clocking --\n");
    link_show("PLLCLKDIVSEL ", SYSCON->PLLCLKDIVSEL);
    link_show("PLLCLKDIV    ", SYSCON->PLLCLKDIV);
    link_show("FCCLKSEL[6]  ", SYSCON->FCCLKSEL[6]);
    link_show("FC6 CLKDIV   ",
              (&(SYSCON->SYSTICKCLKDIV[0]))[(uint32_t)kCLOCK_DivFlexcom6Clk]);
    link_show("AHBCLKCTRL1  ", SYSCON->AHBCLKCTRL1);
    link_show("FC6 PSELID   ",
              ((LP_FLEXCOMM_Type *)LP_FLEXCOMM_GetBaseAddress(LINK_FLEXCOMM_INSTANCE))->PSELID);

    dbg_puts("-- LPSPI6 --\n");
    link_show("VERID        ", LINK_SPI->VERID);
    link_show("PARAM        ", LINK_SPI->PARAM);
    link_show("CR           ", LINK_SPI->CR);
    link_show("SR           ", LINK_SPI->SR);
    link_show("FSR          ", LINK_SPI->FSR);
    link_show("CFGR0        ", LINK_SPI->CFGR0);
    link_show("CFGR1        ", LINK_SPI->CFGR1);
    link_show("TCR          ", LINK_SPI->TCR);
    link_show("FCR          ", LINK_SPI->FCR);
    link_show("DER          ", LINK_SPI->DER);

    dbg_puts("-- FC6 pins --\n");
    link_show("PCR[20] MOSI ", LINK_PORT->PCR[LINK_PIN_MOSI]);
    link_show("PCR[21] SCK  ", LINK_PORT->PCR[LINK_PIN_SCK]);
    link_show("PCR[22] MISO ", LINK_PORT->PCR[LINK_PIN_MISO]);
    link_show("PCR[23] CS   ", LINK_PORT->PCR[LINK_PIN_CS]);

    dbg_puts("-- eDMA0 --\n");
    link_show("MP_CSR       ", LINK_DMA->MP_CSR);
    link_show("MP_ES        ", LINK_DMA->MP_ES);
    link_show("MP_HRS       ", LINK_DMA->MP_HRS);
    link_dump_channel("  ch0 (tx, want CH_MUX=82)\n", LINK_DMA_CHANNEL_TX);
    link_dump_channel("  ch1 (rx, want CH_MUX=81)\n", LINK_DMA_CHANNEL_RX);

    link_show("tcd tx[0] @  ", (uint32_t)&s_tx_tcd[0]);
    link_show("tcd tx[1] @  ", (uint32_t)&s_tx_tcd[1]);
    link_show("tcd rx[0] @  ", (uint32_t)&s_rx_tcd[0]);
    link_show("tcd rx[1] @  ", (uint32_t)&s_rx_tcd[1]);
    link_show("tx bank[0] @ ", (uint32_t)s_tx_bank[0]);
    link_show("rx bank[0] @ ", (uint32_t)s_rx_bank[0]);
    link_show("TDR addr     ", (uint32_t)&LINK_SPI->TDR);
    link_show("RDR addr     ", (uint32_t)&LINK_SPI->RDR);

    link_watch();

    link_delay(2000000u);

    dbg_puts("-- after traffic --\n");
    link_show("SR           ", LINK_SPI->SR);
    link_show("FSR          ", LINK_SPI->FSR);
    link_dump_channel("  ch0 (tx)\n", LINK_DMA_CHANNEL_TX);
    link_dump_channel("  ch1 (rx)\n", LINK_DMA_CHANNEL_RX);
    link_dump_bytes("-- TX bank0 --", s_tx_bank[0], INJ_FRAME_SIZE);
    link_dump_bytes("-- RX bank0 --", s_rx_bank[0], INJ_FRAME_SIZE);
    link_dump_bytes("-- RX bank1 --", s_rx_bank[1], INJ_FRAME_SIZE);

    dbg_puts("-- retirement --\n");
    link_show("isr entries  ", s_isr_entries);
    link_show("slots retired", link_retire_counters()->slots);
    link_show("idle frames  ", link_retire_counters()->idle);
    link_show("bad sof      ", link_retire_counters()->bad_sof);
    link_show("bad crc      ", link_retire_counters()->bad_crc);
    link_show("rx cursor    ", s_rx_cursor);
    {
        uint8_t snapshot[INJ_FRAME_SIZE];
        NVIC_DisableIRQ(EDMA_0_CH1_IRQn);
        for (uint32_t index = 0u; index < INJ_FRAME_SIZE; ++index) {
            snapshot[index] = link_retire_last_slot()[index];
        }
        NVIC_EnableIRQ(EDMA_0_CH1_IRQn);
        link_dump_bytes("-- last retired slot --", snapshot, INJ_FRAME_SIZE);
    }
    dbg_puts("  (a bank nothing filled reads all-zero; the FPGA keepalive is\n"
             "   68 00 00 00 .. 00 fe 36, so a non-zero slot here is wire data)\n");

    // The report cadence starts from whatever the boot dump cost, so the first
    // periodic line is one full interval away rather than immediate.
    s_report_ms = platform_ticks();
}

#endif  // MCXN947
