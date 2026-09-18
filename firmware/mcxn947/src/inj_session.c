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
        s->pace_counter++;
        if (s->pace_counter < s->pace_period) {
            return false;
        }
        s->pace_counter = 0u;

        inj_relative_payload_t rel;
        memset(&rel, 0, sizeof(rel));
        rel.lease_generation = s->active_map_generation;
        rel.map_generation = s->active_map_generation;
        rel.command_sequence = (uint16_t)(s->command_sequence + 1u);
        rel.target_frame = 0u;
        rel.interface_number = s->interface_number;
        rel.endpoint_number = s->endpoint_number;
        rel.report_id = s->report_id;
        rel.flags = (uint8_t)(INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y);
        rel.x = s->inject_x;
        rel.y = s->inject_y;
        (void)inj_build_relative(slot, next_frame_seq(s), &rel);
        s->command_sequence = rel.command_sequence;
        s->relatives_sent++;
        return true;
    }
    default:
        // WAIT_LINK / WAIT_REPORT / WAIT_COMMIT: idle.
        return false;
    }
}
