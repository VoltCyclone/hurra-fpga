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

// Drive a fresh session all the way to INJECTING with a committed boot map.
static void reach_injecting(inj_session_t *s)
{
    inj_session_init(s);
    inj_session_set_link(s, true);
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(s, &frag);
    run_upload(s);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(s, &ok);
    assert(inj_session_phase(s) == INJ_PHASE_INJECTING);
}

// An externally requested one-shot must go out on the NEXT slot, not wait out
// the drift's pace period. kmcmd drains a move as a budget of bounded steps, so
// a queued step that sat behind 199 idle slots would make a large move take
// seconds for no reason.
static void test_requested_relative_preempts_the_paced_drift(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_relative(&s, 64, -3, 0, 0));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_RELATIVE);

    inj_relative_payload_t rel;
    memcpy(&rel, payload, sizeof(rel));
    assert(rel.x == 64 && rel.y == -3);
    assert(rel.flags == (INJ_RELATIVE_FLAG_X | INJ_RELATIVE_FLAG_Y));

    // Consumed: the slot is free again and the drift resumes its pacing.
    assert(!inj_session_pending_request(&s));
    assert_idle(&s);
}

// Wheel and pan carry their own enable bits, and a field that is not being
// injected must not have its flag set -- the flags are what the engine reads to
// decide which fields to touch at all.
static void test_requested_wheel_sets_only_the_wheel_flag(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_relative(&s, 0, 0, -1, 0));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    inj_relative_payload_t rel;
    memcpy(&rel, payload, sizeof(rel));
    assert(rel.flags == INJ_RELATIVE_FLAG_WHEEL);
    assert(rel.wheel == -1 && rel.x == 0 && rel.y == 0);
}

// The FPGA's command queue is one deep. Refusing a second request rather than
// overwriting the first is what makes that depth visible to the caller, so a
// dropped step can be retried instead of silently vanishing.
static void test_second_request_is_refused_while_one_is_pending(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_relative(&s, 10, 0, 0, 0));
    assert(!inj_session_request_relative(&s, 20, 0, 0, 0));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    inj_relative_payload_t rel;
    memcpy(&rel, payload, sizeof(rel));
    assert(rel.x == 10);  // the first, not the second

    assert(inj_session_request_relative(&s, 20, 0, 0, 0));
}

// Before INJECTING the FPGA's command_fresh is false, so a queued command would
// be discarded at the far end. Refuse locally instead of emitting something that
// cannot be honoured.
static void test_request_is_refused_before_injecting(void)
{
    inj_session_t s;
    inj_session_init(&s);
    assert(!inj_session_request_relative(&s, 1, 1, 0, 0));

    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert(!inj_session_request_relative(&s, 1, 1, 0, 0));
}

// The FPGA tears down its session and RX sequence window with the link, so a
// queued count is void. Replaying it into a fresh session would be a stale move
// landing at an unpredictable time.
static void test_link_drop_discards_a_pending_request(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_relative(&s, 32, 32, 0, 0));
    assert(inj_session_pending_request(&s));

    inj_session_set_link(&s, false);
    assert(!inj_session_pending_request(&s));
    assert_idle(&s);
}

// A zero drift emits nothing. The paced RELATIVE exists to move the cursor; at
// (0,0) it is an additive no-op, and spending a slot and a command sequence on
// it would burn the FPGA's one-deep queue against an externally driven move.
static void test_zero_drift_emits_no_paced_relative(void)
{
    inj_session_t s;
    reach_injecting(&s);

    inj_session_set_drift(&s, 0, 0);
    for (uint32_t i = 0u; i < (INJ_SESSION_DEFAULT_PACE * 3u); i++) {
        assert_idle(&s);
    }

    // A request still goes out: only the drift is silenced, not the session.
    assert(inj_session_request_relative(&s, 5, 0, 0, 0));
    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_RELATIVE);
}

// A click's press half is an injected button mask with a hold; its release half
// is the same mask cleared. hold_reports is the one RELATIVE-adjacent field the
// gateware actually wires (engine.button_hold_reports at gateware.py:371 is the
// only hold_reports wiring in the whole design), so BUTTON_STATE is the only
// command on this link that can time anything for itself.
static void test_requested_buttons_emit_button_state(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_buttons(&s, 0x5u, 400u));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_BUTTON_STATE);

    inj_button_state_payload_t st;
    memcpy(&st, payload, sizeof(st));
    assert(st.buttons == 0x5u);
    assert(st.hold_reports == 400u);
    assert(st.map_generation == s.active_map_generation);
    assert(!inj_session_pending_request(&s));
}

// PHYSICAL_MASK suppresses the REAL device's buttons. It is buttons-only --
// there is no motion mask in the contract -- so this is the whole of what an
// axis lock would have needed and the reason axis locks are refused upstream.
static void test_requested_physical_mask_emits_physical_mask(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_physical_mask(&s, 0x3u));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_PHYSICAL_MASK);

    inj_physical_mask_payload_t mask;
    memcpy(&mask, payload, sizeof(mask));
    assert(mask.button_mask == 0x3u);
    assert(mask.map_generation == s.active_map_generation);
}

// There is ONE request slot, shared by all three kinds, because the FPGA's
// command queue is one deep. A pending motion step must therefore block a
// button request rather than the two coexisting -- otherwise a click and a move
// queued in the same pass would silently discard one of them.
static void test_one_request_slot_is_shared_across_kinds(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_relative(&s, 8, 0, 0, 0));
    assert(!inj_session_request_buttons(&s, 0x1u, 0u));
    assert(!inj_session_request_physical_mask(&s, 0x1u));

    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_RELATIVE);  // the motion step, not either mask

    assert(inj_session_request_buttons(&s, 0x1u, 0u));
}

// Releasing every button is a real command, not an empty one: the mask must go
// to zero on the wire or the button stays held. This is the one place the
// "all fields zero is a no-op, drop it" rule from the RELATIVE path must NOT
// apply, so it is pinned separately.
static void test_zero_button_mask_is_still_emitted(void)
{
    inj_session_t s;
    reach_injecting(&s);

    assert(inj_session_request_buttons(&s, 0x1u, 0u));
    uint8_t type = 0u;
    uint8_t payload[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_BUTTON_STATE);

    assert(inj_session_request_buttons(&s, 0x0u, 0u));
    (void)next_frame(&s, &type, payload);
    assert(type == INJ_TYPE_BUTTON_STATE);
    inj_button_state_payload_t st;
    memcpy(&st, payload, sizeof(st));
    assert(st.buttons == 0u);
}

int main(void)
{
    test_full_lifecycle();
    test_rejection_retries_with_new_generation();
    test_link_drop_resets_and_non_boot_is_ignored();
    test_requested_relative_preempts_the_paced_drift();
    test_requested_wheel_sets_only_the_wheel_flag();
    test_second_request_is_refused_while_one_is_pending();
    test_request_is_refused_before_injecting();
    test_link_drop_discards_a_pending_request();
    test_zero_drift_emits_no_paced_relative();
    test_requested_buttons_emit_button_state();
    test_requested_physical_mask_emits_physical_mask();
    test_one_request_slot_is_shared_across_kinds();
    test_zero_button_mask_is_still_emitted();

    printf("inj_session_test: ok\n");
    return 0;
}
