// See inj_session.h. Pure, MMIO-free; host-compiled by inj_session_test.c.

#include "inj_session.h"

#include <string.h>

#include "inj_command.h"
#include "spi_frame.h"

// Standard HID boot-mouse report is [buttons, X, Y, wheel]; X lives at byte 1
// and Y at byte 2, so the map needs at least three bytes to address Y.
#define INJ_SESSION_MIN_REPORT_LENGTH 3u
#define INJ_USAGE_PAGE_GENERIC_DESKTOP 0x01u
#define INJ_USAGE_X 0x30u
#define INJ_USAGE_Y 0x31u

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

// Fill the fixed boot-mouse field map from learned addressing. Deterministic
// from session state, so recomputing it for BEGIN, each ENTRY and COMMIT (to
// derive entries_crc32) always yields identical bytes.
static void boot_mouse_entries(const inj_session_t *s,
                               inj_map_entry_payload_t out[INJ_SESSION_MAP_ENTRIES])
{
    memset(out, 0, INJ_SESSION_MAP_ENTRIES * sizeof(out[0]));

    out[0].descriptor_generation = s->descriptor_generation;
    out[0].map_generation = s->map_generation;
    out[0].entry_index = 0u;
    out[0].interface_number = s->interface_number;
    out[0].endpoint_number = s->endpoint_number;
    out[0].report_id = s->report_id;
    out[0].usage_page = INJ_USAGE_PAGE_GENERIC_DESKTOP;
    out[0].usage = INJ_USAGE_X;
    out[0].bit_offset = 8u;  // byte 1
    out[0].bit_width = 8u;
    out[0].flags = (uint8_t)(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE |
                             INJ_MAP_ENTRY_FLAG_X);
    out[0].logical_minimum = -127;
    out[0].logical_maximum = 127;
    out[0].report_length = s->report_length;

    out[1] = out[0];
    out[1].entry_index = 1u;
    out[1].usage = INJ_USAGE_Y;
    out[1].bit_offset = 16u;  // byte 2
    out[1].flags = (uint8_t)(INJ_MAP_ENTRY_FLAG_SIGNED | INJ_MAP_ENTRY_FLAG_RELATIVE |
                             INJ_MAP_ENTRY_FLAG_Y);
}

static uint32_t entries_crc32(const inj_map_entry_payload_t entries[INJ_SESSION_MAP_ENTRIES])
{
    uint8_t blob[INJ_SESSION_MAP_ENTRIES * INJ_FRAME_PAYLOAD_SIZE];
    for (uint8_t index = 0u; index < INJ_SESSION_MAP_ENTRIES; ++index) {
        memcpy(&blob[index * INJ_FRAME_PAYLOAD_SIZE], &entries[index], INJ_FRAME_PAYLOAD_SIZE);
    }
    return inj_crc32(blob, sizeof(blob));
}

static void fill_map_meta(const inj_session_t *s, inj_map_begin_payload_t *meta)
{
    inj_map_entry_payload_t entries[INJ_SESSION_MAP_ENTRIES];
    boot_mouse_entries(s, entries);

    memset(meta, 0, sizeof(*meta));
    meta->descriptor_generation = s->descriptor_generation;
    meta->map_generation = s->map_generation;
    meta->entry_count = INJ_SESSION_MAP_ENTRIES;
    meta->layout_count = 1u;  // one report layout; both axes share it
    meta->flags = 0u;
    meta->entries_crc32 = entries_crc32(entries);
}

static void begin_upload(inj_session_t *s)
{
    s->map_generation = (uint16_t)(s->map_generation + 1u);
    if (s->map_generation == 0u) {
        s->map_generation = 1u;
    }
    s->entry_cursor = 0u;
    s->phase = INJ_PHASE_SEND_BEGIN;
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
        // link, so any in-flight map is void. Return to the start; keep the
        // configured pattern and the monotonic diagnostics.
        s->phase = INJ_PHASE_WAIT_LINK;
        s->tx_frame_seq = 0u;
        s->command_sequence = 0u;
        s->entry_cursor = 0u;
        s->pace_counter = 0u;
        s->active_map_generation = 0u;
        // A queued request is void too: it was counted against a session and an
        // RX sequence window that no longer exist, so replaying it into a fresh
        // session would land a stale move at an unpredictable moment.
        s->req_pending = 0u;
        return;
    }
    if (s->phase == INJ_PHASE_WAIT_LINK) {
        s->phase = INJ_PHASE_WAIT_REPORT;
    }
}

void inj_session_observe_report(inj_session_t *s, const inj_report_fragment_payload_t *frag)
{
    // Only boot-protocol reports (no report-ID prefix) with room for X and Y
    // are handled; anything else is left for a later, descriptor-compiled map.
    if (frag->report_id != 0u || frag->total < INJ_SESSION_MIN_REPORT_LENGTH ||
        frag->total > INJ_MAX_REPORT_BYTES) {
        return;
    }

    if (s->phase == INJ_PHASE_WAIT_REPORT) {
        s->descriptor_generation = frag->descriptor_generation;
        s->interface_number = frag->interface_number;
        s->endpoint_number = frag->endpoint_number;
        s->report_id = frag->report_id;
        s->report_length = (uint8_t)frag->total;
        begin_upload(s);
        return;
    }

    // A descriptor-generation change means the device re-enumerated and the
    // committed map is stale (the FPGA ties the map to the generation), so
    // re-learn and re-upload against the new device.
    if (s->phase == INJ_PHASE_INJECTING && frag->descriptor_generation != s->descriptor_generation) {
        s->phase = INJ_PHASE_WAIT_REPORT;
    }
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
        s->phase = INJ_PHASE_WAIT_REPORT;  // re-learn and retry with a new generation
    }
    // CANDIDATE_ACCEPTED is an intermediate begin/entry ack: keep waiting.
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
    header.report_id = s->report_id;
    return header;
}

// Build one RELATIVE into `slot` and advance both sequence numbers. Shared by
// the drift and the request path so they cannot drift apart in how they cite
// the active generation -- getting that wrong is the difference between a
// command the FPGA honours and one command_fresh silently discards.
static void emit_relative(inj_session_t *s, uint8_t slot[INJ_FRAME_SIZE], uint8_t flags,
                          int16_t x, int16_t y, int16_t wheel, int16_t pan)
{
    inj_relative_payload_t rel;
    memset(&rel, 0, sizeof(rel));
    rel.lease_generation = s->active_map_generation;
    rel.map_generation = s->active_map_generation;
    rel.command_sequence = (uint16_t)(s->command_sequence + 1u);
    rel.target_frame = 0u;
    rel.interface_number = s->interface_number;
    rel.endpoint_number = s->endpoint_number;
    rel.report_id = s->report_id;
    rel.flags = flags;
    rel.x = x;
    rel.y = y;
    rel.wheel = wheel;
    rel.pan = pan;
    (void)inj_build_relative(slot, next_frame_seq(s), &rel);
    s->command_sequence = rel.command_sequence;
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
    case INJ_PHASE_SEND_BEGIN: {
        inj_map_begin_payload_t meta;
        fill_map_meta(s, &meta);
        (void)inj_build_map_begin(slot, next_frame_seq(s), &meta);
        s->entry_cursor = 0u;
        s->phase = INJ_PHASE_SEND_ENTRY;
        return true;
    }
    case INJ_PHASE_SEND_ENTRY: {
        inj_map_entry_payload_t entries[INJ_SESSION_MAP_ENTRIES];
        boot_mouse_entries(s, entries);
        (void)inj_build_map_entry(slot, next_frame_seq(s), &entries[s->entry_cursor]);
        s->entry_cursor++;
        if (s->entry_cursor >= INJ_SESSION_MAP_ENTRIES) {
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
        s->phase = INJ_PHASE_WAIT_COMMIT;
        return true;
    }
    case INJ_PHASE_INJECTING: {
        // A queued one-shot goes out ahead of the drift and without waiting out
        // the pace period: it is a step of a bounded budget whose caller is
        // draining it, not a rate.
        if (s->req_pending != INJ_SESSION_REQ_NONE) {
            const uint8_t kind = s->req_pending;
            s->req_pending = INJ_SESSION_REQ_NONE;

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

            const uint8_t flags = relative_flags(s->req_x, s->req_y, s->req_wheel, s->req_pan);
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
        // WAIT_LINK / WAIT_REPORT / WAIT_COMMIT: idle.
        return false;
    }
}
