// Host test for src/link_fault_classify.c.
//
// The centrepiece is test_observed_misframing_names_cs_and_usb_sync(), whose
// numbers are NOT invented. They are the delta between two consecutive
// link_report() lines captured from the debug UART on 2026-09-19 while the
// bench link was genuinely broken:
//
//   [PERTURB] slots=269424 idle=0 deliv=0 sof=269442 crc=0 len=0 typ=0
//   [PERTURB] slots=277439 idle=0 deliv=0 sof=277457 crc=0 len=0 typ=0
//
// and the bank is the `last slot` line printed beside them. A classifier that
// cannot name the fault the bench actually had is not worth shipping, so that
// window is pinned here rather than paraphrased into round numbers.
//
// The counters in that capture are why bad_crc is load-bearing: crc=0 across
// 8,015 consecutive failing slots is not the absence of corruption, it is
// proof the CRC check was never reached.

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "link_fault_classify.h"

// --- helpers ----------------------------------------------------------------

static void fill(uint8_t bank[INJ_FRAME_SIZE], uint8_t value)
{
    memset(bank, value, INJ_FRAME_SIZE);
}

static bool names(const link_fault_report_t *report, link_pin_t pin)
{
    for (uint8_t i = 0u; i < report->suspect_count; ++i) {
        if (report->suspect[i] == pin) {
            return true;
        }
    }
    return false;
}

// --- cases ------------------------------------------------------------------

// A link delivering frames is not a fault, and must name no pin at all --
// a report that always has a suspect trains the reader to ignore it.
static void test_healthy_link_reports_ok(void)
{
    link_fault_evidence_t evidence = {
        .slots = 8000u,
        .idle = 7000u,
        .deliverable = 1000u,
        .isr_entries = 8000u,
        .bank_valid = true,
    };
    evidence.bank[0] = INJ_FRAME_SOF;

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_OK);
    assert(report.suspect_count == 0u);
}

// No slots AND no ISR entries: nothing is clocking the shift register.
static void test_no_clock_names_sck(void)
{
    const link_fault_evidence_t evidence = {0};

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_NO_CLOCK);
    assert(names(&report, LINK_PIN_SCK));
}

// The ISR firing while no slot retires is a DIFFERENT fault -- the clock is
// there and the ring is not retiring -- so it must not be blamed on SCK.
static void test_retiring_nothing_while_isr_fires_is_not_no_clock(void)
{
    const link_fault_evidence_t evidence = {
        .slots = 0u,
        .isr_entries = 8000u,
    };

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict != LINK_FAULT_NO_CLOCK);
    assert(!names(&report, LINK_PIN_SCK));
}

// An undriven input reads as a constant. Both rails mean the same thing.
static void test_all_zero_bank_names_mosi(void)
{
    link_fault_evidence_t evidence = {
        .slots = 8000u,
        .bad_sof = 8000u,
        .isr_entries = 8000u,
        .bank_valid = true,
    };
    fill(evidence.bank, 0x00u);

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_NO_DATA);
    assert(names(&report, LINK_PIN_MOSI));
}

static void test_all_ones_bank_names_mosi(void)
{
    link_fault_evidence_t evidence = {
        .slots = 8000u,
        .bad_sof = 8000u,
        .isr_entries = 8000u,
        .bank_valid = true,
    };
    fill(evidence.bank, 0xffu);

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_NO_DATA);
    assert(names(&report, LINK_PIN_MOSI));
}

// THE BENCH FAULT OF 2026-09-19. See the file header for provenance.
static void test_observed_misframing_names_cs_and_usb_sync(void)
{
    link_fault_evidence_t evidence = {
        .slots = 277439u - 269424u,   // 8015
        .idle = 0u,
        .deliverable = 0u,
        .bad_sof = 277457u - 269442u, // 8015
        .bad_crc = 0u,
        .bad_length = 0u,
        .bad_type = 0u,
        .isr_entries = 8015u,
        .bank_valid = true,
    };
    // The `last slot` line printed beside those counters: varied bytes, so the
    // wire is carrying real traffic, and no 0x68 at offset 0.
    const uint8_t observed[INJ_FRAME_SIZE] = {
        0x00, 0x1a, 0x31, 0xcd, 0x00, 0x61, 0x03, 0x40,
        0x20, 0x00, 0x00, 0x20, 0x00, 0x00, 0x00, 0x80,
    };
    memcpy(evidence.bank, observed, sizeof(observed));

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_MIS_FRAMED);
    assert(names(&report, LINK_PIN_CS));
    assert(names(&report, LINK_PIN_USB_SYNC));
    // MOSI is carrying data; blaming it here would send someone to the wrong
    // wire on the one fault we have actually seen.
    assert(!names(&report, LINK_PIN_MOSI));
}

// A CRC error proves SOF matched, so the boundary is right and the bits are
// not. That is the ground bridge, not the framing wires.
static void test_crc_failures_name_ground(void)
{
    link_fault_evidence_t evidence = {
        .slots = 8000u,
        .idle = 200u,
        .deliverable = 50u,
        .bad_crc = 400u,
        .isr_entries = 8000u,
        .bank_valid = true,
    };
    evidence.bank[0] = INJ_FRAME_SOF;
    evidence.bank[1] = 0x03u;

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_NOISY);
    assert(names(&report, LINK_PIN_GND));
    assert(!names(&report, LINK_PIN_CS));
}

// Below the slot threshold nothing is trustworthy. Reporting a fault from a
// window that straddles a recovery is how a diagnostic loses its credibility.
static void test_short_window_reports_no_fault(void)
{
    link_fault_evidence_t evidence = {
        .slots = LINK_FAULT_MIN_SLOTS - 1u,
        .bad_sof = LINK_FAULT_MIN_SLOTS - 1u,
        .isr_entries = LINK_FAULT_MIN_SLOTS - 1u,
        .bank_valid = true,
    };
    fill(evidence.bank, 0x5au);

    link_fault_report_t report;
    link_fault_classify(&evidence, &report);

    assert(report.verdict == LINK_FAULT_OK);
    assert(report.suspect_count == 0u);
}

static void test_null_evidence_is_written_not_left_stale(void)
{
    link_fault_report_t report;
    memset(&report, 0xa5, sizeof(report));

    link_fault_classify(NULL, &report);

    assert(report.verdict == LINK_FAULT_OK);
    assert(report.suspect_count == 0u);
}

// The display calls these on whatever is in the shared window, which CPU0 may
// be rewriting; a NULL would fault CPU1.
static void test_names_are_never_null(void)
{
    for (int i = -1; i <= (int)LINK_FAULT_COUNT; ++i) {
        assert(link_fault_verdict_name((link_fault_verdict_t)i) != NULL);
    }
    for (int i = -1; i <= (int)LINK_PIN_COUNT; ++i) {
        assert(link_pin_name((link_pin_t)i) != NULL);
        assert(link_pin_location((link_pin_t)i) != NULL);
    }
}

int main(void)
{
    test_healthy_link_reports_ok();
    test_no_clock_names_sck();
    test_retiring_nothing_while_isr_fires_is_not_no_clock();
    test_all_zero_bank_names_mosi();
    test_all_ones_bank_names_mosi();
    test_observed_misframing_names_cs_and_usb_sync();
    test_crc_failures_name_ground();
    test_short_window_reports_no_fault();
    test_null_evidence_is_written_not_left_stale();
    test_names_are_never_null();

    printf("link_fault_classify_test: ok\n");
    return 0;
}
