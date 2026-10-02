// See inj_session.h. Pure, MMIO-free; host-compiled by inj_session_test.c.

#include "inj_session.h"

#include <string.h>

#include "inj_command.h"
#include "inj_map_build.h"
#include "spi_frame.h"

// Advance and return the link-level frame sequence, never yielding 0 (which is
// IDLE's sequence and is filtered before the FPGA's RX window). Monotonic +1
// keeps every command a FRESH delta of 1; the 255->1 wrap is delta 2, still
// inside the fresh 1..127 band.
static uint8_t next_frame_seq(inj_session_t *s)
{
    s->tx_frame_seq = (uint8_t)(s->tx_frame_seq + 1u);
    if (s->tx_frame_seq == 0u) {
        s->tx_frame_seq = 1u;
    }
    return s->tx_frame_seq;
}

// Build the map once per attempt, CRC included, and cache it: BEGIN, every
// ENTRY and COMMIT are then plain copies. This runs in the retirement ISR, and
// the CRC is the only part with a real cost (a few tens of microseconds for a
// full eight-entry map, once per attempt).
static void begin_upload(inj_session_t *s, uint8_t endpoint_number, uint8_t report_length)
{
    s->map_generation = (uint16_t)(s->map_generation + 1u);
    if (s->map_generation == 0u) {
        s->map_generation = 1u;
    }
    s->endpoint_number = endpoint_number;

    const inj_map_target_t target = {
        .descriptor_generation = s->descriptor_generation,
        .map_generation = s->map_generation,
        .interface_number = s->interface_number,
        .endpoint_number = endpoint_number,
        .report_length = report_length,
    };
    s->entry_count = inj_map_build_entries(&s->layout, &target, s->entries);
    s->entries_crc32 = inj_map_entries_crc32(s->entries, s->entry_count);
    s->entry_cursor = 0u;
    s->phase = INJ_PHASE_SEND_BEGIN;
}

static void fill_map_meta(const inj_session_t *s, inj_map_begin_payload_t *meta)
{
    memset(meta, 0, sizeof(*meta));
    meta->descriptor_generation = s->descriptor_generation;
    meta->map_generation = s->map_generation;
    meta->entry_count = s->entry_count;
    meta->layout_count = 1u;  // one report layout; every field shares it
    meta->flags = 0u;
    meta->entries_crc32 = s->entries_crc32;
}

// Where a session with the link up belongs when nothing is in flight.
static inj_phase_t resting_phase(const inj_session_t *s)
{
    if (s->have_layout) {
        return INJ_PHASE_WAIT_REPORT;
    }
    return s->no_mouse_verdict ? INJ_PHASE_NO_MOUSE : INJ_PHASE_WAIT_DESCRIPTOR;
}

// Forget the device: a new descriptor generation is a new enumeration. The
// FPGA ties its committed map to the generation, so the map, and any request
// queued against it, are void as well.
static void forget_device(inj_session_t *s, uint16_t generation)
{
    s->descriptor_generation = generation;
    s->have_generation = true;
    memset(&s->layout, 0, sizeof(s->layout));
    s->have_layout = false;
    s->complete_mask = 0u;
    s->judged_mask = 0u;
    s->settle_slots = 0u;
    s->commit_wait_slots = 0u;
    s->no_mouse_verdict = false;
    s->entry_count = 0u;
    s->active_map_generation = 0u;
    s->req_pending = INJ_SESSION_REQ_NONE;
    if (s->phase != INJ_PHASE_WAIT_LINK) {
        s->phase = INJ_PHASE_WAIT_DESCRIPTOR;
    }
}

// Descriptors, and only descriptors, move the session to a new generation.
// Reports carry the generation too, but a stale report of the previous device
// would throw away a layout nothing could re-offer: the descriptor set already
// holds those descriptors complete and the FPGA exports them only once.
static void adopt_generation(inj_session_t *s, uint16_t generation)
{
    // `!=`, not an ordering: generations are u16 and wrap.
    if (!s->have_generation || generation != s->descriptor_generation) {
        forget_device(s, generation);
    }
}

static bool is_current_generation(const inj_session_t *s, uint16_t generation)
{
    return s->have_generation && generation == s->descriptor_generation;
}

void inj_session_init(inj_session_t *s)
{
    memset(s, 0, sizeof(*s));
    s->phase = INJ_PHASE_WAIT_LINK;
    s->map_generation = 0u;  // begin_upload pre-increments to 1 on first attempt
    s->pace_period = INJ_SESSION_DEFAULT_PACE;
    s->inject_x = INJ_SESSION_DEFAULT_X;
    s->inject_y = INJ_SESSION_DEFAULT_Y;
}

void inj_session_set_link(inj_session_t *s, bool up)
{
    if (!up) {
        // The FPGA drops session_active and its RX sequence window with the
        // link, so any in-flight map is void. Keep what was learned about the
        // DEVICE (see inj_session.h), the configured pattern and the
        // monotonic diagnostics.
        s->phase = INJ_PHASE_WAIT_LINK;
        s->tx_frame_seq = 0u;
        s->command_sequence = 0u;
        s->entry_cursor = 0u;
        s->pace_counter = 0u;
        s->settle_slots = 0u;
        s->commit_wait_slots = 0u;
        s->active_map_generation = 0u;
        // A queued request is void too: it was counted against a session and an
        // RX sequence window that no longer exist, so replaying it into a fresh
        // session would land a stale move at an unpredictable moment.
        s->req_pending = INJ_SESSION_REQ_NONE;
        return;
    }
    if (s->phase == INJ_PHASE_WAIT_LINK) {
        s->phase = resting_phase(s);
    }
}

void inj_session_observe_descriptor(inj_session_t *s, uint16_t generation, uint8_t slot,
                                    bool complete_edge)
{
    if (s->phase == INJ_PHASE_WAIT_LINK) {
        return;
    }
    adopt_generation(s, generation);
    s->settle_slots = 0u;  // descriptor traffic: the export is not finished
    if (complete_edge && slot < INJ_MAX_INTERFACES) {
        s->complete_mask = (uint8_t)(s->complete_mask | (1u << slot));
    }
}

void inj_session_sync_descriptors(inj_session_t *s, uint16_t generation, uint8_t complete_now)
{
    if (!is_current_generation(s, generation)) {
        return;
    }
    // A slot can only stop being complete through a discard, which also voids
    // its judgement: if it completes again it is a new descriptor to judge.
    s->complete_mask = complete_now;
    s->judged_mask = (uint8_t)(s->judged_mask & complete_now);
}

void inj_session_offer_layout(inj_session_t *s, uint16_t generation, uint8_t slot,
                              uint8_t interface_number, const hid_mouse_layout_t *layout)
{
    if (!is_current_generation(s, generation) || slot >= INJ_MAX_INTERFACES) {
        return;  // a verdict on a device that is gone
    }
    s->judged_mask = (uint8_t)(s->judged_mask | (1u << slot));

    // The first mouse layout wins; a composite device's second one is not
    // addressable anyway, since every command cites exactly one report.
    if (layout == NULL || s->have_layout) {
        return;
    }
    s->layout = *layout;
    s->have_layout = true;
    s->interface_number = interface_number;
    if (s->phase == INJ_PHASE_WAIT_DESCRIPTOR || s->phase == INJ_PHASE_NO_MOUSE) {
        s->phase = INJ_PHASE_WAIT_REPORT;
    }
}

// Is this fragment the first slice of the layout's own report? A report-ID
// report is recognised by its first byte: until a map commits the FPGA labels
// every report ID 0 (injection.py), so the fragment's report_id says nothing.
static bool is_layout_report(const inj_session_t *s, const inj_report_fragment_payload_t *frag)
{
    if (frag->interface_number != s->interface_number || frag->offset != 0u) {
        return false;
    }
    return s->layout.report_id == 0u || frag->data[0] == s->layout.report_id;
}

void inj_session_observe_report(inj_session_t *s, const inj_report_fragment_payload_t *frag)
{
    // A report of any other generation is stale, or a new device's arriving
    // ahead of its descriptors; either way it is not the layout's report.
    if (s->phase != INJ_PHASE_WAIT_REPORT ||
        !is_current_generation(s, frag->descriptor_generation) || !is_layout_report(s, frag)) {
        return;
    }

    // The FPGA applies a layout only to a report of exactly its length, so the
    // map must carry what the device SENDS. Adopt a different length when it
    // still reaches every mapped field; otherwise the layout is wrong for this
    // device and uploading it would only commit a map that never applies.
    uint16_t length = frag->total;
    if (length != s->layout.report_length) {
        if (length < s->layout.min_report_length || length > INJ_MAX_REPORT_BYTES) {
            s->length_conflicts++;
            return;
        }
        s->length_overrides++;
    }
    begin_upload(s, frag->endpoint_number, (uint8_t)length);
}

void inj_session_observe_map_status(inj_session_t *s, const inj_map_status_payload_t *status)
{
    if (s->phase != INJ_PHASE_WAIT_COMMIT) {
        return;
    }

    if (status->status == INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED &&
        status->error == INJ_MAP_STATUS_ERROR_NONE &&
        status->active_map_generation == s->map_generation) {
        s->active_map_generation = status->active_map_generation;
        s->pace_counter = 0u;
        s->maps_committed++;
        s->phase = INJ_PHASE_INJECTING;
    } else if (status->status == INJ_MAP_STATUS_STATUS_REJECTED ||
               status->status == INJ_MAP_STATUS_STATUS_ABORTED) {
        s->map_rejections++;
        s->phase = INJ_PHASE_WAIT_REPORT;  // retry on the next report, new generation
    }
    // CANDIDATE_ACCEPTED is an intermediate begin/entry ack: keep waiting.
}

// The settle window, clocked by fill_tx (once per retired slot). Every
// completed descriptor must have been judged -- the foreground compile can lag
// the timer, and an unjudged one may be the mouse.
static void settle_step(inj_session_t *s)
{
    if (s->settle_slots < INJ_SESSION_SETTLE_SLOTS) {
        s->settle_slots++;
    }
    // No layout is implied: a layout moves the session out of WAIT_DESCRIPTOR.
    if (s->settle_slots >= INJ_SESSION_SETTLE_SLOTS && s->complete_mask != 0u &&
        (s->complete_mask & (uint8_t)~s->judged_mask) == 0u) {
        s->no_mouse_verdict = true;
        s->phase = INJ_PHASE_NO_MOUSE;
    }
}

// Enable bits for exactly the fields being injected. The engine reads `flags`
// to decide which fields to touch at all, so setting a bit for a zero field
// would be a request to add nothing to it -- harmless arithmetically, but it
// makes a wheel-only scroll look like a motion command in a capture.
static uint8_t relative_flags(int16_t x, int16_t y, int16_t wheel, int16_t pan)
{
    uint8_t flags = 0u;
    if (x != 0) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_X;
    }
    if (y != 0) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_Y;
    }
    if (wheel != 0) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_WHEEL;
    }
    if (pan != 0) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_PAN;
    }
    return flags;
}

// The RELATIVE enable bits this session's map can honour.
static uint8_t layout_relative_flags(const inj_session_t *s)
{
    uint8_t flags = 0u;
    if ((s->layout.axes & HID_MOUSE_AXIS_BIT(HID_MOUSE_X)) != 0u) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_X;
    }
    if ((s->layout.axes & HID_MOUSE_AXIS_BIT(HID_MOUSE_Y)) != 0u) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_Y;
    }
    if ((s->layout.axes & HID_MOUSE_AXIS_BIT(HID_MOUSE_WHEEL)) != 0u) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_WHEEL;
    }
    if ((s->layout.axes & HID_MOUSE_AXIS_BIT(HID_MOUSE_PAN)) != 0u) {
        flags |= (uint8_t)INJ_RELATIVE_FLAG_PAN;
    }
    return flags;
}

static bool layout_has_buttons(const inj_session_t *s)
{
    for (uint8_t i = 0u; i < s->layout.field_count; ++i) {
        if (s->layout.fields[i].kind == HID_MOUSE_BUTTONS) {
            return true;
        }
    }
    return false;
}

// Every command payload opens with the same addressing and identity fields.
// This is returned BY VALUE and its fields assigned into each payload rather
// than filled through pointers: the payloads are __attribute__((packed)), so
// taking the address of a member can produce an unaligned pointer and
// -Waddress-of-packed-member (an error here) rejects it. Assigning to a packed
// member is fine; pointing at one is not.
//
// Citing the ACTIVE generation is the load-bearing part. A command carrying the
// wrong one is discarded by the FPGA's command_fresh with no ack, no counter,
// and nothing to notice it by.
typedef struct {
    uint16_t lease_generation;
    uint16_t map_generation;
    uint16_t command_sequence;
    uint8_t interface_number;
    uint8_t endpoint_number;
    uint8_t report_id;
} command_header_t;

static command_header_t command_header(const inj_session_t *s)
{
    command_header_t header;
    header.lease_generation = s->active_map_generation;
    header.map_generation = s->active_map_generation;
    header.command_sequence = (uint16_t)(s->command_sequence + 1u);
    header.interface_number = s->interface_number;
    header.endpoint_number = s->endpoint_number;
    header.report_id = s->layout.report_id;
    return header;
}

// Build one RELATIVE into `slot` and advance both sequence numbers. Shared by
// the drift and the request path so they cannot drift apart in how they cite
// the active generation -- getting that wrong is the difference between a
// command the FPGA honours and one command_fresh silently discards.
static void emit_relative(inj_session_t *s, uint8_t slot[INJ_FRAME_SIZE], uint8_t flags,
                          int16_t x, int16_t y, int16_t wheel, int16_t pan)
{
    const command_header_t header = command_header(s);
    inj_relative_payload_t rel;
    memset(&rel, 0, sizeof(rel));
    rel.lease_generation = header.lease_generation;
    rel.map_generation = header.map_generation;
    rel.command_sequence = header.command_sequence;
    rel.target_frame = 0u;
    rel.interface_number = header.interface_number;
    rel.endpoint_number = header.endpoint_number;
    rel.report_id = header.report_id;
    rel.flags = flags;
    rel.x = x;
    rel.y = y;
    rel.wheel = wheel;
    rel.pan = pan;
    (void)inj_build_relative(slot, next_frame_seq(s), &rel);
    s->command_sequence = header.command_sequence;
}

// Shared admission for all three request kinds. INJECTING is the MCU-side
// mirror of the FPGA's command_fresh, and a non-empty slot means the one-deep
// queue is still occupied -- either way the answer is a refusal the caller can
// retry, never an overwrite that loses a command silently.
static bool request_slot_free(inj_session_t *s)
{
    if (s->phase != INJ_PHASE_INJECTING || s->req_pending != INJ_SESSION_REQ_NONE) {
        s->requests_refused++;
        return false;
    }
    return true;
}

bool inj_session_request_relative(inj_session_t *s, int16_t x, int16_t y,
                                  int16_t wheel, int16_t pan)
{
    if (!request_slot_free(s)) {
        return false;
    }
    s->req_x = x;
    s->req_y = y;
    s->req_wheel = wheel;
    s->req_pan = pan;
    s->req_pending = INJ_SESSION_REQ_RELATIVE;
    return true;
}

bool inj_session_request_buttons(inj_session_t *s, uint64_t mask, uint16_t hold_reports)
{
    if (!request_slot_free(s)) {
        return false;
    }
    s->req_buttons = mask;
    s->req_hold_reports = hold_reports;
    s->req_pending = INJ_SESSION_REQ_BUTTONS;
    return true;
}

bool inj_session_request_physical_mask(inj_session_t *s, uint64_t button_mask)
{
    if (!request_slot_free(s)) {
        return false;
    }
    s->req_button_mask = button_mask;
    s->req_pending = INJ_SESSION_REQ_PHYSICAL_MASK;
    return true;
}

bool inj_session_pending_request(const inj_session_t *s)
{
    return s->req_pending != INJ_SESSION_REQ_NONE;
}

void inj_session_set_drift(inj_session_t *s, int16_t x, int16_t y)
{
    s->inject_x = x;
    s->inject_y = y;
    s->pace_counter = 0u;
}

bool inj_session_fill_tx(inj_session_t *s, uint8_t slot[INJ_FRAME_SIZE])
{
    switch (s->phase) {
    case INJ_PHASE_WAIT_DESCRIPTOR:
        settle_step(s);
        return false;
    case INJ_PHASE_SEND_BEGIN: {
        inj_map_begin_payload_t meta;
        fill_map_meta(s, &meta);
        (void)inj_build_map_begin(slot, next_frame_seq(s), &meta);
        s->entry_cursor = 0u;
        s->phase = INJ_PHASE_SEND_ENTRY;
        return true;
    }
    case INJ_PHASE_SEND_ENTRY: {
        (void)inj_build_map_entry(slot, next_frame_seq(s), &s->entries[s->entry_cursor]);
        s->entry_cursor++;
        if (s->entry_cursor >= s->entry_count) {
            s->phase = INJ_PHASE_SEND_COMMIT;
        }
        return true;
    }
    case INJ_PHASE_SEND_COMMIT: {
        inj_map_begin_payload_t meta;  // commit shares the begin layout
        fill_map_meta(s, &meta);
        inj_map_commit_payload_t commit;
        memcpy(&commit, &meta, sizeof(commit));
        (void)inj_build_map_commit(slot, next_frame_seq(s), &commit);
        s->commit_wait_slots = 0u;
        s->phase = INJ_PHASE_WAIT_COMMIT;
        return true;
    }
    case INJ_PHASE_WAIT_COMMIT:
        // The FPGA answers within a few slots. A lost MAP_STATUS would park the
        // session here until the link dropped; give up and retry on the next
        // report with a fresh map generation, so a late answer for this one is
        // recognisably stale.
        s->commit_wait_slots++;
        if (s->commit_wait_slots >= INJ_SESSION_COMMIT_TIMEOUT_SLOTS) {
            s->commit_timeouts++;
            s->phase = INJ_PHASE_WAIT_REPORT;
        }
        return false;
    case INJ_PHASE_INJECTING: {
        // A queued one-shot goes out ahead of the drift and without waiting out
        // the pace period: it is a step of a bounded budget whose caller is
        // draining it, not a rate.
        if (s->req_pending != INJ_SESSION_REQ_NONE) {
            const uint8_t kind = s->req_pending;
            s->req_pending = INJ_SESSION_REQ_NONE;

            if (kind != INJ_SESSION_REQ_RELATIVE && !layout_has_buttons(s)) {
                // Nothing in the map for a button state or mask to act on.
                // Dropped here and counted rather than refused at request
                // time: kmcmd reads a refusal as "not now" and would retry it
                // forever.
                s->button_drops++;
                return false;
            }

            if (kind == INJ_SESSION_REQ_BUTTONS) {
                const command_header_t header = command_header(s);
                inj_button_state_payload_t st;
                memset(&st, 0, sizeof(st));
                st.lease_generation = header.lease_generation;
                st.map_generation = header.map_generation;
                st.command_sequence = header.command_sequence;
                st.interface_number = header.interface_number;
                st.endpoint_number = header.endpoint_number;
                st.report_id = header.report_id;
                st.buttons = s->req_buttons;
                st.hold_reports = s->req_hold_reports;
                (void)inj_build_button_state(slot, next_frame_seq(s), &st);
                s->command_sequence = header.command_sequence;
                s->requests_sent++;
                return true;
            }

            if (kind == INJ_SESSION_REQ_PHYSICAL_MASK) {
                const command_header_t header = command_header(s);
                inj_physical_mask_payload_t mask;
                memset(&mask, 0, sizeof(mask));
                mask.lease_generation = header.lease_generation;
                mask.map_generation = header.map_generation;
                mask.command_sequence = header.command_sequence;
                mask.interface_number = header.interface_number;
                mask.endpoint_number = header.endpoint_number;
                mask.report_id = header.report_id;
                mask.button_mask = s->req_button_mask;
                (void)inj_build_physical_mask(slot, next_frame_seq(s), &mask);
                s->command_sequence = header.command_sequence;
                s->requests_sent++;
                return true;
            }

            const uint8_t wanted =
                relative_flags(s->req_x, s->req_y, s->req_wheel, s->req_pan);
            const uint8_t flags = (uint8_t)(wanted & layout_relative_flags(s));
            if (flags != wanted) {
                // A wheel or pan the mouse does not have: the map has no entry
                // to carry it, so it is dropped here, visibly, rather than sent
                // as a command the FPGA can only ignore.
                s->axis_drops++;
            }
            if (flags == 0u) {
                // Every field zero: additive no-op. Drop it rather than spend a
                // slot and a command sequence saying nothing. A zero BUTTON_STATE
                // above is NOT the same case -- that is how a button is released.
                return false;
            }
            emit_relative(s, slot, flags, s->req_x, s->req_y, s->req_wheel, s->req_pan);
            s->requests_sent++;
            return true;
        }

        if (s->inject_x == 0 && s->inject_y == 0) {
            return false;  // drift silenced; see inj_session_set_drift
        }

        s->pace_counter++;
        if (s->pace_counter < s->pace_period) {
            return false;
        }
        s->pace_counter = 0u;

        emit_relative(s, slot, (uint8_t)(INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y),
                      s->inject_x, s->inject_y, 0, 0);
        s->relatives_sent++;
        return true;
    }
    default:
        // WAIT_LINK / NO_MOUSE / WAIT_REPORT: idle.
        return false;
    }
}

