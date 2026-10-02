// Host test for src/inj_session.c -- the injection session state machine.
//
// It drives the FSM through the whole mandated lifecycle with fabricated
// telemetry and REAL compiled layouts (hid_mouse_layout.c over the fixtures in
// hid_fixtures.h), and checks: nothing is uploaded until a mouse layout is
// offered and one of its reports is seen; the map-upload frames come out in
// order (BEGIN, one ENTRY per field, COMMIT) with a monotonic link-level frame
// sequence and the golden entries CRC; RELATIVE motion only flows after a
// COMMIT_ACCEPTED and cites the active generation; keyboards and pads end in
// NO_MOUSE; pacing, rejection retry, re-enumeration and link-drop all behave.

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "hid_fixtures.h"
#include "hid_mouse_layout.h"
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

// A boot-mouse report's first fragment: [buttons, X, Y], generation 3.
static inj_report_fragment_payload_t boot_fragment(void)
{
    inj_report_fragment_payload_t frag;
    memset(&frag, 0, sizeof(frag));
    frag.descriptor_generation = 3u;
    frag.interface_number = 0u;
    frag.endpoint_number = 1u;
    frag.report_id = 0u;  // the FPGA reports 0 for every report until a map commits
    frag.offset = 0u;
    frag.total = 3u;
    return frag;
}

static hid_mouse_layout_t compiled(const uint8_t *descriptor, size_t length)
{
    hid_mouse_layout_t layout;
    assert(hid_mouse_compile(descriptor, length, &layout) == HID_MOUSE_OK);
    return layout;
}

// What link.c does when descriptor-set slot `slot`, holding interface `iface`
// of generation `gen`, completes and the foreground compiles it: one
// completion edge, then the verdict.
static void deliver_in_slot(inj_session_t *s, uint16_t gen, uint8_t slot, uint8_t iface,
                            const hid_mouse_layout_t *l)
{
    inj_session_observe_descriptor(s, gen, slot, true);
    inj_session_offer_layout(s, gen, slot, iface, l);
}

// The common case: interfaces numbered from 0 claim the slot of the same index.
static void deliver(inj_session_t *s, uint16_t gen, uint8_t iface, const hid_mouse_layout_t *l)
{
    deliver_in_slot(s, gen, iface, iface, l);
}

static void offer_boot_mouse(inj_session_t *s)
{
    const hid_mouse_layout_t layout =
        compiled(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE));
    deliver(s, 3u, 0u, &layout);
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

// Run the boot-mouse upload -- BEGIN, three ENTRYs (X, Y, buttons), COMMIT --
// and assert every frame. Sequences start at `first_seq`.
static void run_upload_from(inj_session_t *s, uint8_t first_seq)
{
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];

    assert(next_frame(s, &type, p) == first_seq);
    assert(type == INJ_TYPE_MAP_BEGIN);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(begin.entry_count == 3u);
    assert(begin.layout_count == 1u);
    assert(begin.descriptor_generation == 3u);

    inj_map_entry_payload_t entries[3];
    for (uint8_t i = 0u; i < 3u; ++i) {
        const uint8_t seq = next_frame(s, &type, p);
        assert(seq == (uint8_t)(first_seq + 1u + i));
        assert(type == INJ_TYPE_MAP_ENTRY);
        memcpy(&entries[i], p, sizeof(entries[i]));
        assert(entries[i].entry_index == i);
        assert(entries[i].report_length == 3u);
        assert(entries[i].endpoint_number == 1u);
    }
    // Byte 1 is X (offset 8), byte 2 is Y (offset 16), buttons 1-3 at bit 0.
    assert(entries[0].bit_offset == 8u && entries[0].usage == 0x30u);
    assert(entries[1].bit_offset == 16u && entries[1].usage == 0x31u);
    assert(entries[2].bit_offset == 0u && entries[2].usage == 1u && entries[2].bit_width == 3u);

    // MAP_BEGIN's crc32 must match the entries actually sent. For the first
    // map (generation 1) that is the golden value zlib.crc32 gives for exactly
    // these payloads -- see inj_map_build_test.c.
    assert(begin.entries_crc32 == inj_crc32((const uint8_t *)entries, sizeof(entries)));
    if (begin.map_generation == 1u) {
        assert(begin.entries_crc32 == 0xDD9D11D2u);
    }

    assert(next_frame(s, &type, p) == (uint8_t)(first_seq + 4u));
    assert(type == INJ_TYPE_MAP_COMMIT);
    inj_map_commit_payload_t commit;
    memcpy(&commit, p, sizeof(commit));
    assert(commit.entries_crc32 == begin.entries_crc32);
    assert(commit.map_generation == begin.map_generation);
    assert(commit.entry_count == 3u);
}

static void run_upload(inj_session_t *s)
{
    run_upload_from(s, 1u);
}

static void test_full_lifecycle(void)
{
    inj_session_t s;
    inj_session_init(&s);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_LINK);
    assert_idle(&s);

    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
    assert_idle(&s);

    // A report before any verdict teaches nothing: the session cannot know
    // which bytes of it are a mouse's.
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);

    offer_boot_mouse(&s);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert_idle(&s);

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
    assert(seq == 6u);  // BEGIN, three ENTRYs, COMMIT used 1..5
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
    assert(type == INJ_TYPE_RELATIVE && seq == 7u);
    memcpy(&rel, p, sizeof(rel));
    assert(rel.command_sequence == 2u);
    assert(s.relatives_sent == 2u);
}

static void test_rejection_retries_with_new_generation(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    offer_boot_mouse(&s);
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

// A link drop voids the FPGA-side session but not what was learned about the
// device: the FPGA re-exports the SAME generation's descriptors on link-up,
// and those are not new completions, so nothing would ever re-offer a layout.
static void test_link_drop_resumes_with_the_cached_layout(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    offer_boot_mouse(&s);
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    run_upload(&s);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &ok);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING);

    inj_session_set_link(&s, false);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_LINK);
    assert(s.active_map_generation == 0u);
    assert_idle(&s);
    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    // A live report re-uploads, with a fresh map generation.
    inj_session_observe_report(&s, &frag);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(type == INJ_TYPE_MAP_BEGIN && begin.map_generation == 2u);
}

// Drive a fresh session all the way to INJECTING with a committed boot map.
static void reach_injecting(inj_session_t *s)
{
    inj_session_init(s);
    inj_session_set_link(s, true);
    offer_boot_mouse(s);
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(s, &frag);
    run_upload(s);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(s, &ok);
    assert(inj_session_phase(s) == INJ_PHASE_INJECTING);
}

// The report-ID gaming mouse (ID 2, 16-bit X/Y, wheel, AC Pan, 5 buttons) on
// interface 1, endpoint 2, driven to INJECTING.
static inj_report_fragment_payload_t id_mouse_fragment(uint8_t first_byte)
{
    inj_report_fragment_payload_t frag;
    memset(&frag, 0, sizeof(frag));
    frag.descriptor_generation = 3u;
    frag.interface_number = 1u;
    frag.endpoint_number = 2u;
    frag.total = 8u;
    frag.data[0] = first_byte;  // the report ID, as the device sends it
    return frag;
}

static void drain_upload(inj_session_t *s)
{
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    do {
        (void)next_frame(s, &type, p);
    } while (type != INJ_TYPE_MAP_COMMIT);
}

static void reach_injecting_id_mouse(inj_session_t *s)
{
    inj_session_init(s);
    inj_session_set_link(s, true);
    const hid_mouse_layout_t layout =
        compiled(HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT));
    deliver(s, 3u, 1u, &layout);
    inj_report_fragment_payload_t frag = id_mouse_fragment(2u);
    inj_session_observe_report(s, &frag);
    assert(inj_session_phase(s) == INJ_PHASE_SEND_BEGIN);
    drain_upload(s);
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
    reach_injecting_id_mouse(&s);

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
    assert(!inj_session_request_relative(&s, 1, 1, 0, 0));
    offer_boot_mouse(&s);
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

// --- descriptor-compiled targeting --------------------------------------------

static void tick(inj_session_t *s, uint32_t slots)
{
    for (uint32_t i = 0u; i < slots; ++i) {
        assert_idle(s);
    }
}

// A keyboard alone: its descriptor completes, is judged not-a-mouse, and once
// descriptor traffic has been quiet for the settle window the session says so.
// Its reports never start an upload -- which is the bug this exists to fix: a
// boot map over a keyboard report put injected Y in the first key slot.
static void test_keyboard_only_device_becomes_no_mouse(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);

    inj_report_fragment_payload_t key = boot_fragment();
    key.total = 8u;
    inj_session_observe_report(&s, &key);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);

    tick(&s, INJ_SESSION_SETTLE_SLOTS - 1u);
    assert(!inj_session_no_mouse(&s));
    tick(&s, 1u);
    assert(inj_session_no_mouse(&s));
    assert(!inj_session_request_relative(&s, 5, 0, 0, 0));
    inj_session_observe_report(&s, &key);
    assert(inj_session_no_mouse(&s));
}

// No verdict while a completed descriptor is still being compiled: the
// foreground may lag the timer, and the unjudged one may be the mouse.
static void test_no_verdict_while_a_completed_descriptor_is_unjudged(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    inj_session_observe_descriptor(&s, 3u, 1u, true);  // completed, not yet offered
    tick(&s, INJ_SESSION_SETTLE_SLOTS * 4u);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
}

// Any descriptor fragment restarts the quiet window: an export still in flight
// is not "done".
static void test_descriptor_traffic_restarts_the_settle_window(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    tick(&s, INJ_SESSION_SETTLE_SLOTS - 1u);
    inj_session_observe_descriptor(&s, 3u, 1u, false);  // interface 1 still arriving
    tick(&s, INJ_SESSION_SETTLE_SLOTS - 1u);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
    tick(&s, 1u);
    assert(inj_session_no_mouse(&s));
}

// Composite device: a keyboard on interface 0, the mouse on interface 1. The
// map targets interface 1 and the endpoint seen on ITS reports; the keyboard's
// reports teach nothing.
static void test_composite_device_targets_the_mouse_interface(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    const hid_mouse_layout_t mouse =
        compiled(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE));
    deliver(&s, 3u, 1u, &mouse);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    inj_report_fragment_payload_t key = boot_fragment();
    key.total = 8u;
    inj_session_observe_report(&s, &key);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    inj_report_fragment_payload_t move = boot_fragment();
    move.interface_number = 1u;
    move.endpoint_number = 2u;
    inj_session_observe_report(&s, &move);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);

    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    (void)next_frame(&s, &type, p);
    inj_map_entry_payload_t entry;
    memcpy(&entry, p, sizeof(entry));
    assert(type == INJ_TYPE_MAP_ENTRY);
    assert(entry.interface_number == 1u && entry.endpoint_number == 2u);
}

// Interface numbers are not slot indices: a keyboard numbered 2 claimed slot 0
// and the mouse numbered 5 slot 1. Maps and the report match carry the real
// number, 5.
static void test_a_mouse_numbered_beyond_the_slot_count_is_targeted(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver_in_slot(&s, 3u, 0u, 2u, NULL);
    const hid_mouse_layout_t mouse =
        compiled(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE));
    deliver_in_slot(&s, 3u, 1u, 5u, &mouse);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    inj_report_fragment_payload_t move = boot_fragment();
    move.interface_number = 5u;
    move.endpoint_number = 6u;
    inj_session_observe_report(&s, &move);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);

    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    (void)next_frame(&s, &type, p);
    inj_map_entry_payload_t entry;
    memcpy(&entry, p, sizeof(entry));
    assert(type == INJ_TYPE_MAP_ENTRY);
    assert(entry.interface_number == 5u && entry.endpoint_number == 6u);
}

// A mouse whose descriptor completes after the settle window (a slow composite
// export) still gets its map: NO_MOUSE is a verdict on what has arrived.
static void test_a_late_mouse_overturns_no_mouse(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    tick(&s, INJ_SESSION_SETTLE_SLOTS);
    assert(inj_session_no_mouse(&s));

    const hid_mouse_layout_t mouse =
        compiled(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE));
    deliver(&s, 3u, 1u, &mouse);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
}

// Re-enumeration (a new descriptor generation on a DESCRIPTOR_FRAGMENT) drops
// the layout, the committed map and any queued request; a verdict still in
// flight for the OLD generation is ignored when it lands.
static void test_new_generation_drops_everything(void)
{
    inj_session_t s;
    reach_injecting(&s);
    assert(inj_session_request_relative(&s, 4, 4, 0, 0));

    inj_session_observe_descriptor(&s, 4u, 0u, false);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
    assert(s.active_map_generation == 0u);
    assert(!inj_session_pending_request(&s));
    assert(!s.have_layout);

    const hid_mouse_layout_t mouse =
        compiled(HID_FIXTURE_BOOT_MOUSE, sizeof(HID_FIXTURE_BOOT_MOUSE));
    inj_session_offer_layout(&s, 3u, 0u, 0u, &mouse);  // stale: generation 3
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
    deliver(&s, 4u, 0u, &mouse);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
}

// A report-ID mouse: only reports carrying ITS ID start the upload, and every
// command cites that ID.
static void test_report_id_mouse_is_addressed_by_its_id(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    const hid_mouse_layout_t layout =
        compiled(HID_FIXTURE_ID_MOUSE_16BIT, sizeof(HID_FIXTURE_ID_MOUSE_16BIT));
    deliver(&s, 3u, 1u, &layout);

    inj_report_fragment_payload_t other = id_mouse_fragment(1u);  // another report ID
    inj_session_observe_report(&s, &other);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    inj_report_fragment_payload_t mine = id_mouse_fragment(2u);
    inj_session_observe_report(&s, &mine);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);

    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(begin.entry_count == 5u);
    for (uint8_t i = 0u; i < 5u; ++i) {
        (void)next_frame(&s, &type, p);
        inj_map_entry_payload_t entry;
        memcpy(&entry, p, sizeof(entry));
        assert(entry.report_id == 2u && entry.report_length == 8u);
    }
    (void)next_frame(&s, &type, p);
    assert(type == INJ_TYPE_MAP_COMMIT);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &ok);

    assert(inj_session_request_relative(&s, 3, 0, 0, 0));
    (void)next_frame(&s, &type, p);
    inj_relative_payload_t rel;
    memcpy(&rel, p, sizeof(rel));
    assert(rel.report_id == 2u && rel.interface_number == 1u && rel.endpoint_number == 2u);
}

// The FPGA binds a layout only on an exact report length. A device sending more
// than its descriptor declares still reaches every mapped field, so its real
// length is adopted and counted; one sending too little to reach them is not
// mapped at all.
static void test_report_length_override_and_conflict(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    offer_boot_mouse(&s);
    inj_report_fragment_payload_t longer = boot_fragment();
    longer.total = 4u;
    inj_session_observe_report(&s, &longer);
    assert(inj_session_phase(&s) == INJ_PHASE_SEND_BEGIN);
    assert(s.length_overrides == 1u);
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    (void)next_frame(&s, &type, p);
    inj_map_entry_payload_t entry;
    memcpy(&entry, p, sizeof(entry));
    assert(entry.report_length == 4u);

    inj_session_init(&s);
    inj_session_set_link(&s, true);
    offer_boot_mouse(&s);
    inj_report_fragment_payload_t shorter = boot_fragment();
    shorter.total = 2u;
    inj_session_observe_report(&s, &shorter);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert(s.length_conflicts == 1u);
}

// A wheel request against a mouse with no wheel cannot be honoured; the wheel
// part is dropped (and counted) rather than sent as a command the map does
// not cover, while any X/Y in the same request still goes out.
static void test_wheel_on_a_wheelless_mouse_is_dropped(void)
{
    inj_session_t s;
    reach_injecting(&s);  // boot mouse: X, Y, buttons
    inj_session_set_drift(&s, 0, 0);

    assert(inj_session_request_relative(&s, 0, 0, 1, 0));
    assert_idle(&s);
    assert(s.axis_drops == 1u);

    assert(inj_session_request_relative(&s, 5, 0, 1, 0));
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    inj_relative_payload_t rel;
    memcpy(&rel, p, sizeof(rel));
    assert(rel.flags == INJ_RELATIVE_FLAG_X && rel.x == 5);
    assert(s.axis_drops == 2u);
}

// NO_MOUSE survives a link bounce: the re-exported descriptors are the same
// generation and are never re-judged, so the verdict must be kept.
static void test_no_mouse_survives_a_link_bounce(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    tick(&s, INJ_SESSION_SETTLE_SLOTS);
    assert(inj_session_no_mouse(&s));
    inj_session_set_link(&s, false);
    assert(!inj_session_no_mouse(&s));
    inj_session_set_link(&s, true);
    assert(inj_session_no_mouse(&s));
}

// A NO_MOUSE verdict belongs to one device. Replace the keyboard with a mouse
// (a new generation) and bounce the link before its descriptor arrives: the
// session must be waiting for that descriptor, not repeating the old verdict.
static void test_no_mouse_verdict_dies_with_its_generation(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    tick(&s, INJ_SESSION_SETTLE_SLOTS);
    assert(inj_session_no_mouse(&s));

    inj_session_observe_descriptor(&s, 4u, 0u, false);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
    inj_session_set_link(&s, false);
    inj_session_set_link(&s, true);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);
}

// Reports never move the session's generation; descriptors do. A stale report
// of the previous device must not throw away the current layout -- nothing
// would ever re-offer it, because the descriptor set already holds those
// descriptors complete and the FPGA exports them once. And a new device's
// reports arriving ahead of its descriptors change nothing until they do.
static void test_reports_of_another_generation_are_ignored(void)
{
    inj_session_t s;
    reach_injecting(&s);  // generation 3

    inj_report_fragment_payload_t stale = boot_fragment();
    stale.descriptor_generation = 2u;
    inj_session_observe_report(&s, &stale);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING && s.have_layout);

    inj_report_fragment_payload_t early = boot_fragment();
    early.descriptor_generation = 4u;
    inj_session_observe_report(&s, &early);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING && s.have_layout);

    // Before any descriptor at all, a report teaches nothing either.
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    inj_session_observe_report(&s, &early);
    assert(!s.have_generation);
}

// The set can lose a completed descriptor (a discard) without the FPGA ever
// resending it. link.c reconciles the session with what the set actually holds;
// a withdrawn, never-judged interface must not hold the verdict back forever.
static void test_a_withdrawn_descriptor_does_not_block_the_verdict(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    deliver(&s, 3u, 0u, NULL);
    inj_session_observe_descriptor(&s, 3u, 1u, true);  // complete, never judged
    tick(&s, INJ_SESSION_SETTLE_SLOTS * 2u);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_DESCRIPTOR);

    inj_session_sync_descriptors(&s, 3u, 0x01u);  // interface 1 no longer complete
    tick(&s, INJ_SESSION_SETTLE_SLOTS);
    assert(inj_session_no_mouse(&s));

    // A sync for another generation is not about this device.
    inj_session_t t;
    inj_session_init(&t);
    inj_session_set_link(&t, true);
    deliver(&t, 3u, 0u, NULL);
    inj_session_observe_descriptor(&t, 3u, 1u, true);
    inj_session_sync_descriptors(&t, 9u, 0x01u);
    tick(&t, INJ_SESSION_SETTLE_SLOTS * 2u);
    assert(inj_session_phase(&t) == INJ_PHASE_WAIT_DESCRIPTOR);
}

// A lost MAP_STATUS must not park the session forever: after the commit
// timeout it retries with a fresh map generation, and the late answer for the
// abandoned one is ignored.
static void test_commit_timeout_retries_with_a_new_generation(void)
{
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    offer_boot_mouse(&s);
    inj_report_fragment_payload_t frag = boot_fragment();
    inj_session_observe_report(&s, &frag);
    run_upload(&s);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_COMMIT);

    tick(&s, INJ_SESSION_COMMIT_TIMEOUT_SLOTS - 1u);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_COMMIT);
    tick(&s, 1u);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);
    assert(s.commit_timeouts == 1u);

    inj_map_status_payload_t late = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &late);
    assert(inj_session_phase(&s) == INJ_PHASE_WAIT_REPORT);

    inj_session_observe_report(&s, &frag);
    uint8_t type = 0u;
    uint8_t p[INJ_FRAME_PAYLOAD_SIZE];
    (void)next_frame(&s, &type, p);
    inj_map_begin_payload_t begin;
    memcpy(&begin, p, sizeof(begin));
    assert(type == INJ_TYPE_MAP_BEGIN && begin.map_generation == 2u);
}

// A mouse whose layout maps no buttons has nothing a BUTTON_STATE or
// PHYSICAL_MASK could act on. Such a request is dropped at emission and
// counted, like an unsupported axis -- not refused, because kmcmd treats a
// refusal as "not now" and would retry it forever.
static void test_button_requests_on_a_buttonless_mouse_are_dropped(void)
{
    static const uint8_t axes_only[] = {
        0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
        0x09, 0x30, 0x09, 0x31, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06,
        0xC0,
    };
    inj_session_t s;
    inj_session_init(&s);
    inj_session_set_link(&s, true);
    const hid_mouse_layout_t layout = compiled(axes_only, sizeof(axes_only));
    deliver(&s, 3u, 0u, &layout);
    inj_report_fragment_payload_t frag = boot_fragment();
    frag.total = 2u;
    inj_session_observe_report(&s, &frag);
    drain_upload(&s);
    inj_map_status_payload_t ok = commit_status(1u, INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED);
    inj_session_observe_map_status(&s, &ok);
    assert(inj_session_phase(&s) == INJ_PHASE_INJECTING);
    inj_session_set_drift(&s, 0, 0);

    assert(inj_session_request_buttons(&s, 0x1u, 0u));
    assert_idle(&s);
    assert(inj_session_request_physical_mask(&s, 0x1u));
    assert_idle(&s);
    assert(s.button_drops == 2u);
}

int main(void)
{
    test_full_lifecycle();
    test_rejection_retries_with_new_generation();
    test_link_drop_resumes_with_the_cached_layout();
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
    test_keyboard_only_device_becomes_no_mouse();
    test_no_verdict_while_a_completed_descriptor_is_unjudged();
    test_descriptor_traffic_restarts_the_settle_window();
    test_composite_device_targets_the_mouse_interface();
    test_a_mouse_numbered_beyond_the_slot_count_is_targeted();
    test_a_late_mouse_overturns_no_mouse();
    test_new_generation_drops_everything();
    test_report_id_mouse_is_addressed_by_its_id();
    test_report_length_override_and_conflict();
    test_wheel_on_a_wheelless_mouse_is_dropped();
    test_no_mouse_survives_a_link_bounce();
    test_no_mouse_verdict_dies_with_its_generation();
    test_reports_of_another_generation_are_ignored();
    test_a_withdrawn_descriptor_does_not_block_the_verdict();
    test_commit_timeout_retries_with_a_new_generation();
    test_button_requests_on_a_buttonless_mouse_are_dropped();

    printf("inj_session_test: ok\n");
    return 0;
}
