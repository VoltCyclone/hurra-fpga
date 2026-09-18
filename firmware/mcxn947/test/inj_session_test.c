// Host test for src/inj_session.c -- the injection session state machine.
//
// It drives the FSM through the whole mandated lifecycle with fabricated
// telemetry and checks: the map-upload frames come out in order (BEGIN, two
// ENTRYs, COMMIT) with a monotonic link-level frame sequence; MAP_BEGIN/COMMIT
// carry an entries_crc32 that matches the actual entries emitted; RELATIVE
// motion only flows after a COMMIT_ACCEPTED and cites the active generation;
// pacing, rejection retry, re-enumeration and link-drop all behave.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "inj_command.h"
#include "inj_session.h"
#include "injection_wire.h"
#include "spi_frame.h"

// Pull the next TX frame; assert the session wants to send and it decodes.
static uint8_t next_frame(inj_session_t *s, uint8_t *type_out, uint8_t payload_out[INJ_FRAME_PAYLOAD_SIZE])
{
    uint8_t slot[INJ_FRAME_SIZE];
    assert(inj_session_fill_tx(s, slot));
    uint8_t type = 0u;
    uint8_t sequence = 0u;
    const uint8_t *payload = NULL;
    uint8_t length = 0u;
    assert(spi_frame_unpack(slot, &type, &sequence, &payload, &length) == SPI_FRAME_OK);
    assert(length == INJ_FRAME_PAYLOAD_SIZE);
    *type_out = type;
    memcpy(payload_out, payload, INJ_FRAME_PAYLOAD_SIZE);
    return sequence;
}

static void assert_idle(inj_session_t *s)
{
    uint8_t slot[INJ_FRAME_SIZE];
    assert(!inj_session_fill_tx(s, slot));
}

static inj_report_fragment_payload_t boot_fragment(void)
{
    inj_report_fragment_payload_t frag;
    memset(&frag, 0, sizeof(frag));
    frag.descriptor_generation = 3u;
    frag.interface_number = 0u;
    frag.endpoint_number = 1u;
    frag.report_id = 0u;
    frag.offset = 0u;
    frag.total = 4u;  // [buttons, X, Y, wheel]
    return frag;
}

static inj_map_status_payload_t commit_status(uint16_t map_generation, uint8_t status)
{
    inj_map_status_payload_t st;
    memset(&st, 0, sizeof(st));
    st.descriptor_generation = 3u;
    st.map_generation = map_generation;
    st.active_map_generation = map_generation;
    st.status = status;
    st.error = INJ_MAP_STATUS_ERROR_NONE;
    return st;
}

// Run the BEGIN/ENTRY/ENTRY/COMMIT upload and assert every frame it emits.
static void run_upload(inj_session_t *s)
{
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];

    assert(next_frame(s, &type, p) == 1u);
    assert(type == INJ_TYPE_MAP_BEGIN);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(begin.entry_count == INJ_SESSION_MAP_ENTRIES);
    assert(begin.layout_count == 1u);
    assert(begin.descriptor_generation == 3u);

    uint8_t entry_blob[INJ_SESSION_MAP_ENTRIES * INJ_FRAME_PAYLOAD_SIZE];
    for (uint8_t i = 0u; i < INJ_SESSION_MAP_ENTRIES; ++i) {
        uint8_t seq = next_frame(s, &type, p);
        assert(seq == (uint8_t)(2u + i));
        assert(type == INJ_TYPE_MAP_ENTRY);
        inj_map_entry_payload_t entry;
        memcpy(&entry, p, sizeof(entry));
        assert(entry.entry_index == i);
        assert(entry.report_length == 4u);
        memcpy(&entry_blob[i * INJ_FRAME_PAYLOAD_SIZE], p, INJ_FRAME_PAYLOAD_SIZE);
    }
    // Byte 1 is X (offset 8), byte 2 is Y (offset 16).
    inj_map_entry_payload_t x_entry;
    inj_map_entry_payload_t y_entry;
    memcpy(&x_entry, &entry_blob[0], sizeof(x_entry));
    memcpy(&y_entry, &entry_blob[INJ_FRAME_PAYLOAD_SIZE], sizeof(y_entry));
    assert(x_entry.bit_offset == 8u && x_entry.usage == 0x30u);
    assert(y_entry.bit_offset == 16u && y_entry.usage == 0x31u);

    // MAP_BEGIN's crc32 must match the entries actually sent.
    assert(begin.entries_crc32 == inj_crc32(entry_blob, sizeof(entry_blob)));

    assert(next_frame(s, &type, p) == 4u);
    assert(type == INJ_TYPE_MAP_COMMIT);
    inj_map_commit_payload_t commit;
    memcpy(&commit, p, sizeof(commit));
    assert(commit.entries_crc32 == begin.entries_crc32);
    assert(commit.map_generation == begin.map_generation);
}

static void test_full_lifecycle(void)
{
    inj_session_t s;
    inj_session_init(&s);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_LINK);
    assert_idle(&s);

    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert_idle(&s);

    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);

    run_upload(&s);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_COMMIT);
    assert_idle(&s);  // nothing to send until the FPGA acks

    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &ok);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING);
    assert(s.maps_committed == 1u);
    assert(s.active_map_generation == 1u);

    // Pacing: pace_period-1 idles, then one RELATIVE.
    for (uint8_t i = 0u; i < INJ_SESSION_DEFAULT_PACE - 1u; ++i) {
        assert_idle(&s);
    }
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    uint8_t seq = next_frame(&s, &type, p);
    assert(type == INJ_TYPE_RELATIVE);
    assert(seq == 5u);  // BEGIN..COMMIT used 1..4
    inj_relative_payload_t rel;
    memcpy(&rel, p, sizeof(rel));
    assert(rel.map_generation == 1u);
    assert(rel.command_sequence == 1u);
    assert(rel.endpoint_number == 1u);
    assert(rel.flags == (INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y));
    assert(rel.x == INJ_SESSION_DEFAULT_X);
    assert(rel.y == INJ_SESSION_DEFAULT_Y);
    assert(s.relatives_sent == 1u);

    // Second RELATIVE increments both the command and frame sequences.
    for (uint8_t i = 0u; i < INJ_SESSION_DEFAULT_PACE - 1u; ++i) {
        assert_idle(&s);
    }
    seq = next_frame(&s, &type, p);
    assert(type == INJ_TYPE_RELATIVE && seq == 6u);
    memcpy(&rel, p, sizeof(rel));
    assert(rel.command_sequence == 2u);
    assert(s.relatives_sent == 2u);
}

static void test_rejection_retries_with_new_generation(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    run_upload(&s);  // map_generation == 1

    inj_map_status_payload_t bad = commit_status(1u, INJ_MAP_STATUS_STATUS_REJECTED);
    inj_session_observe_map_status(&s, &bad);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert(s.map_rejections == 1u);

    // Re-learn and re-upload: the retry must carry a fresh map_generation so the
    // FPGA cannot confuse it with the rejected one.
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(begin.map_generation == 2u);
}

static void test_link_drop_resets_and_non_boot_is_ignored(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);

    // Report-ID-prefixed device: not a boot layout, ignored.
    inj_report_fragment_payload_t frag = boot_fragment();
    frag.report_id = 2u;
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    // A report too short to hold Y is also ignored.
    frag = boot_fragment();
    frag.total = 2u;
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    // Get to INJECTING, then drop the link: everything resets.
    frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    run_upload(&s);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &ok);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING);

    inj_session_set_link(&s, false);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_LINK);
    assert_idle(&s);
    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
}

int main(void)
{
    test_full_lifecycle();
    test_rejection_retries_with_new_generation();
    test_link_drop_resets_and_non_boot_is_ignored();

    printf("inj_session_test: ok\n");
    return 0;
}
