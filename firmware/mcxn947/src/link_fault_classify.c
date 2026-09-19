#include "link_fault_classify.h"

#include <stddef.h>

// An input nobody drives sits at a rail. Anything else -- even bytes that make
// no sense as a frame -- means the wire is carrying traffic, which is what
// separates "MOSI is disconnected" from "MOSI is fine and the boundary is
// wrong". Requiring a rail rather than merely "all bytes equal" keeps a
// legitimately repetitive payload from reading as a dead wire.
static bool bank_is_undriven(const uint8_t bank[INJ_FRAME_SIZE])
{
    const uint8_t first = bank[0];

    if (first != 0x00u && first != 0xffu) {
        return false;
    }
    for (uint32_t index = 1u; index < INJ_FRAME_SIZE; ++index) {
        if (bank[index] != first) {
            return false;
        }
    }
    return true;
}

static void accuse(link_fault_report_t *out, link_pin_t pin)
{
    if (out->suspect_count < LINK_FAULT_MAX_SUSPECTS) {
        out->suspect[out->suspect_count] = pin;
        out->suspect_count++;
    }
}

void link_fault_classify(const link_fault_evidence_t *evidence,
                         link_fault_report_t *out)
{
    if (out == NULL) {
        return;
    }

    // Written before any early return: the display renders whatever is here,
    // and a report left untouched would show the previous window's fault.
    out->verdict = LINK_FAULT_OK;
    out->suspect_count = 0u;
    for (uint32_t index = 0u; index < LINK_FAULT_MAX_SUSPECTS; ++index) {
        out->suspect[index] = LINK_PIN_NONE;
    }

    if (evidence == NULL) {
        return;
    }

    // Nothing retired AND the ISR never entered. The caller samples on a 50 ms
    // cadence, which is ~400 slots at 8 kHz, so zero of both is not a short
    // window -- it is an unclocked shift register.
    //
    // The ISR term is what keeps this honest. A ring that has stopped retiring
    // also shows slots == 0 while the clock is perfectly healthy, and blaming
    // SCK for that would send someone to re-solder a working wire.
    if (evidence->slots == 0u && evidence->isr_entries == 0u) {
        out->verdict = LINK_FAULT_NO_CLOCK;
        accuse(out, LINK_PIN_SCK);
        return;
    }

    // Too little traffic to judge. Stays OK rather than becoming a verdict of
    // its own: a window straddling a recovery is not evidence of a fault, and
    // a diagnostic that cries wolf during every recovery gets ignored.
    if (evidence->slots < LINK_FAULT_MIN_SLOTS) {
        return;
    }

    // Checked BEFORE the framing branches, because reaching the CRC at all
    // proves the SOF matched and therefore that the frame boundary is right.
    // A CRC failure is the one symptom that positively exonerates CS and
    // usb_sync, so it decides first.
    if (evidence->bad_crc > 0u) {
        out->verdict = LINK_FAULT_NOISY;
        accuse(out, LINK_PIN_GND);
        return;
    }

    // Something parsed and nothing failed its CRC: the link works.
    if ((evidence->idle + evidence->deliverable) > 0u) {
        return;
    }

    // Past here, not one slot in the window parsed.
    if (evidence->bank_valid && bank_is_undriven(evidence->bank)) {
        out->verdict = LINK_FAULT_NO_DATA;
        accuse(out, LINK_PIN_MOSI);
        return;
    }

    // Varied bytes arriving, none of them parsing, and no CRC failure to prove
    // the boundary was ever right. That is a framing fault, and the two wires
    // that set the boundary are chip select and the frame-phase strobe.
    //
    // Counters cannot separate them -- CS frames the slot and usb_sync places
    // it -- so both are named rather than one guessed at.
    if (evidence->bad_sof > 0u) {
        out->verdict = LINK_FAULT_MIS_FRAMED;
        accuse(out, LINK_PIN_CS);
        accuse(out, LINK_PIN_USB_SYNC);
        return;
    }

    // Slots retired, nothing parsed, and no counter says why. Named rather
    // than folded into MIS_FRAMED so that a fault this module does not
    // understand is visibly distinct from one it does.
    out->verdict = LINK_FAULT_STALE;
}

const char *link_fault_verdict_name(link_fault_verdict_t verdict)
{
    switch (verdict) {
    case LINK_FAULT_OK:
        return "OK";
    case LINK_FAULT_NO_CLOCK:
        return "NO CLOCK";
    case LINK_FAULT_NO_DATA:
        return "NO DATA";
    case LINK_FAULT_MIS_FRAMED:
        return "MIS-FRAMED";
    case LINK_FAULT_NOISY:
        return "NOISY";
    case LINK_FAULT_STALE:
        return "STALE";
    case LINK_FAULT_COUNT:
    default:
        return "?";
    }
}

const char *link_pin_name(link_pin_t pin)
{
    switch (pin) {
    case LINK_PIN_NONE:
        return "-";
    case LINK_PIN_SCK:
        return "SCK";
    case LINK_PIN_MOSI:
        return "MOSI";
    case LINK_PIN_CS:
        return "CS";
    case LINK_PIN_USB_SYNC:
        return "usb_sync";
    case LINK_PIN_GND:
        return "GND";
    case LINK_PIN_COUNT:
    default:
        return "?";
    }
}

const char *link_pin_location(link_pin_t pin)
{
    switch (pin) {
    case LINK_PIN_NONE:
        return "-";
    case LINK_PIN_SCK:
        return "J6-4 P3_21";
    case LINK_PIN_MOSI:
        return "J6-6 P3_20";
    // The one pin at ALT2 while SCK/MISO/MOSI are ALT3, which makes it the
    // easiest of the four to mux wrong. See docs/MCXN947_CONTROLLER.md.
    case LINK_PIN_CS:
        return "J6-3 P3_23";
    case LINK_PIN_USB_SYNC:
        return "J3-3 P1_22";
    case LINK_PIN_GND:
        return "J6-8 GND";
    case LINK_PIN_COUNT:
    default:
        return "?";
    }
}
