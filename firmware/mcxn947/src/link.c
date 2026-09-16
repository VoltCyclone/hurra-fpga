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

#include "fsl_clock.h"
#include "fsl_device_registers.h"
#include "fsl_edma.h"
#include "fsl_gpio.h"
#include "fsl_lpflexcomm.h"
#include "fsl_lpspi.h"
#include "fsl_port.h"

// PLATFORM_CORE_HZ, for the FlexComm6 divider static assert below. Portable
// header; nothing in it is MMIO.
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

void link_mcu_ready_set(bool ready)
{
    if (ready) {
        GPIO_PortSet(LINK_READY_GPIO, 1uL << LINK_READY_PIN);
    } else {
        GPIO_PortClear(LINK_READY_GPIO, 1uL << LINK_READY_PIN);
    }
}

static void link_pins_init(void)
{
    CLOCK_EnableClock(kCLOCK_Port3);

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
                               int32_t request_source)
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
            // No interrupt. The ring self-loads, so the CPU has nothing to do
            // per slot at this step; step 3 turns INTMAJOR on for the RX
            // channel to retire completed slots.
            .enabledInterruptMask = 0u,
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

    link_dma_ring_init(LINK_DMA_CHANNEL_TX, s_tx_tcd, s_tx_bank, &LINK_SPI->TDR, true,
                       (int32_t)kDma0RequestMuxLpFlexcomm6Tx);
    link_dma_ring_init(LINK_DMA_CHANNEL_RX, s_rx_tcd, s_rx_bank, &LINK_SPI->RDR, false,
                       (int32_t)kDma0RequestMuxLpFlexcomm6Rx);
}


// ---- TEMPORARY WIRE PROBE -- REMOVE BEFORE COMMIT ----
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
// ---- END TEMPORARY WIRE PROBE ----

// ---- TEMPORARY step-2 bring-up probe -- REMOVE BEFORE COMMIT ----

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

void link_init(void)
{
    dbg_uart_init();
    link_wire_probe();

    link_pins_init();
    link_mcu_ready_set(false);

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

    LINK_SPI->CR = LPSPI_CR_MEN_MASK;

    link_mcu_ready_set(true);

    dbg_puts("\n==== hurra mcxn947 step2 probe v2 ====\n");

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

}

#endif  // MCXN947
