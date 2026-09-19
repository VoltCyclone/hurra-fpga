// Host test for src/kmcmd.c -- the KMBox/MAKCU serial command parser.
//
// Like console_test.c this runs with no board attached, and for the same
// reason: kmcmd.c is portable by construction (no MMIO, no TinyUSB, and
// deliberately no injection_wire.h -- see kmcmd.h for why the last one
// matters). The Makefile recipe for this target passes only -Isrc, so a
// wire-header include creeping in breaks the build rather than the product.
//
// What is asserted here is the GRAMMAR and the REFUSALS, not the prose of any
// reply. The grammar is a third-party contract -- existing KMBox/MAKCU host
// tooling is the caller and cannot be changed to suit us -- so every accepted
// spelling in these cases is one a real host is documented to send. The
// refusals matter just as much: this device physically cannot express an
// absolute cursor move or an axis lock, and a parser that accepted those
// silently would report success for motion that never happens.

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "kmcmd.h"

// --- the sink -----------------------------------------------------------------
//
// Stands in for the one-line adapter over inj_session that the integrator
// wires up (kmcmd.h documents it). Records every emitted step so the tests can
// assert on what actually reached the injection layer rather than on what the
// parser said it would do.

#define SINK_MAX 512

typedef struct {
    int16_t dx, dy, wheel, pan;
} step_t;

static step_t g_steps[SINK_MAX];
static uint32_t g_step_count;

static uint32_t g_mask_writes[SINK_MAX];
static uint32_t g_mask_hold[SINK_MAX];
static uint32_t g_mask_count;

static uint32_t g_physical_masks[SINK_MAX];
static uint32_t g_physical_count;

static bool g_ready;
// Number of further sink calls that will be refused. The sink is allowed to
// refuse, exactly as the console's writer is: the FPGA has a one-deep command
// queue, so "not now" is a normal answer and must not be retried in a spin.
static uint32_t g_refuse_after;

static bool sink_relative(void *ctx, int16_t dx, int16_t dy, int16_t wheel, int16_t pan)
{
    (void)ctx;
    if (g_refuse_after == 0u) {
        return false;
    }
    g_refuse_after--;
    if (g_step_count < SINK_MAX) {
        g_steps[g_step_count++] = (step_t){dx, dy, wheel, pan};
    }
    return true;
}

static bool sink_buttons(void *ctx, uint32_t mask, uint32_t hold_reports)
{
    (void)ctx;
    if (g_refuse_after == 0u) {
        return false;
    }
    g_refuse_after--;
    if (g_mask_count < SINK_MAX) {
        g_mask_hold[g_mask_count] = hold_reports;
        g_mask_writes[g_mask_count++] = mask;
    }
    return true;
}

static bool sink_physical_mask(void *ctx, uint32_t mask)
{
    (void)ctx;
    if (g_physical_count < SINK_MAX) {
        g_physical_masks[g_physical_count++] = mask;
    }
    return true;
}

static bool sink_ready(void *ctx)
{
    (void)ctx;
    return g_ready;
}

static char g_reply[KMCMD_REPLY_MAX];

static void setup(void)
{
    memset(g_steps, 0, sizeof(g_steps));
    g_step_count = 0u;
    memset(g_mask_writes, 0, sizeof(g_mask_writes));
    memset(g_mask_hold, 0, sizeof(g_mask_hold));
    g_mask_count = 0u;
    memset(g_physical_masks, 0, sizeof(g_physical_masks));
    g_physical_count = 0u;
    g_ready = true;
    g_refuse_after = 0xFFFFFFFFu;
    memset(g_reply, 0, sizeof(g_reply));

    const kmcmd_ops_t ops = {
        .relative = sink_relative,
        .buttons = sink_buttons,
        .physical_mask = sink_physical_mask,
        .ready = sink_ready,
        .ctx = NULL,
    };
    kmcmd_init(&ops);
    kmcmd_set_mode(KMCMD_MODE_MAKCU);
}

static kmcmd_verdict_t line(const char *text)
{
    memset(g_reply, 0, sizeof(g_reply));
    return kmcmd_line(text, g_reply, sizeof(g_reply));
}

static bool replied(const char *needle)
{
    return strstr(g_reply, needle) != NULL;
}

// Drain the whole pending budget, with a bound so a parser that never
// finishes fails as a hang here rather than in the foreground loop.
static uint32_t drain(void)
{
    uint32_t iterations = 0u;
    while (kmcmd_pending() && iterations < 4096u) {
        (void)kmcmd_step();
        iterations++;
    }
    assert(!kmcmd_pending());
    return iterations;
}

static int32_t total_dx(void)
{
    int32_t sum = 0;
    for (uint32_t i = 0u; i < g_step_count; ++i) {
        sum += g_steps[i].dx;
    }
    return sum;
}

static int32_t total_dy(void)
{
    int32_t sum = 0;
    for (uint32_t i = 0u; i < g_step_count; ++i) {
        sum += g_steps[i].dy;
    }
    return sum;
}

// --- before anything is wired -------------------------------------------------

// The console lives in the image and is reachable before the injection session
// is brought up, so an operator can enable this parser before kmcmd_init() has
// ever run. Every default therefore has to be correct as ZERO-INITIALISED BSS,
// not merely after init. This case must run before any other -- it is the only
// point at which the pre-init state is observable.
//
// The subtle one is the echo flag. Pre-init every SETTER refuses with `nosink`,
// and a refusal is audible whatever echo says, so the ACK branch looks
// unreachable. A bare GETTER is the exception: a query needs no link and so
// takes the reply path, which means a `true`-by-default flag stored as `s_echo`
// would read false here and answer a MAKCU host with silence -- and a MAKCU
// host waits for `>>>`. The flag is therefore stored inverted.
static void test_defaults_are_correct_before_init(void)
{
    char reply[KMCMD_REPLY_MAX];

    // Mode zero must be OFF, so an unwired parser cannot swallow console lines.
    assert(kmcmd_mode() == KMCMD_MODE_OFF);
    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.move(1,1)", reply, sizeof(reply)) == KMCMD_NOT_MINE);

    kmcmd_set_mode(KMCMD_MODE_MAKCU);

    // Switched on but with nothing bound: the refusal must still be audible.
    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.move(1,1)", reply, sizeof(reply)) == KMCMD_HANDLED);
    assert(strstr(reply, "!") != NULL);

    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.version()", reply, sizeof(reply)) == KMCMD_HANDLED);
    assert(strstr(reply, ">>>") != NULL);

    // The getter. Silence here is a hung host, not a quiet one.
    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.left()", reply, sizeof(reply)) == KMCMD_HANDLED);
    assert(strstr(reply, ">>>") != NULL);

    // And a click with a hold must refuse rather than guess a report rate,
    // because an uninitialised rate is zero and there is no honest conversion.
    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.click(1,1,50)", reply, sizeof(reply)) == KMCMD_HANDLED);
    assert(strstr(reply, "!") != NULL);

    assert(!kmcmd_pending());
    assert(!kmcmd_step());
}

// --- what is and is not ours --------------------------------------------------

// The console still has to work. A line that is not a km command must fall
// through untouched so `stats` keeps reaching its own handler.
static void test_non_km_lines_fall_through(void)
{
    setup();
    assert(line("stats") == KMCMD_NOT_MINE);
    assert(line("help") == KMCMD_NOT_MINE);
    assert(line("") == KMCMD_NOT_MINE);
    assert(line("cpu1halt") == KMCMD_NOT_MINE);
    // Looks like a call but is not a name we know: not ours either, so the
    // console can give its own "unknown command" answer.
    assert(line("frobnicate(1)") == KMCMD_NOT_MINE);
    assert(g_step_count == 0u);
}

// MAKCU documents the `km.` prefix as optional on input ("commands sent to the
// device begin with a dot and close with a parenthesis -- the km. prefix is
// optional"); every KMBox host library sends it. Accepting both is what makes
// one parser serve both.
static void test_prefix_is_optional(void)
{
    setup();
    assert(line("km.move(3,4)") == KMCMD_HANDLED);
    assert(drain() > 0u);
    assert(total_dx() == 3 && total_dy() == 4);

    setup();
    assert(line("move(3,4)") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 3 && total_dy() == 4);

    // MAKCU's own example is `.move(1,1,)` -- leading dot, trailing comma.
    setup();
    assert(line(".move(1,1,)") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 1 && total_dy() == 1);
}

// A km.-prefixed name we do not know is unambiguously meant for us, so it must
// be refused here rather than handed back to the console to call "unknown
// command". The distinction matters to a host: one means "wrong device", the
// other means "this device, unsupported command".
static void test_unknown_km_command_is_refused_not_passed_on(void)
{
    setup();
    assert(line("km.nosuchthing(1)") == KMCMD_HANDLED);
    assert(replied("!"));
    assert(kmcmd_refused() == 1u);
}

static void test_whitespace_and_signs(void)
{
    setup();
    // MAKCU's documented sample has a space after the comma.
    assert(line("km.move(10, -5)") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 10 && total_dy() == -5);

    setup();
    assert(line("  km.move( +7 , -8 )  ") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 7 && total_dy() == -8);
}

static void test_malformed_lines_are_refused(void)
{
    setup();
    assert(line("km.move(1") == KMCMD_HANDLED);  // unterminated
    assert(replied("!"));
    assert(g_step_count == 0u);

    setup();
    assert(line("km.move(1,x)") == KMCMD_HANDLED);  // non-numeric argument
    assert(replied("!"));
    assert(g_step_count == 0u);

    setup();
    assert(line("km.move()") == KMCMD_HANDLED);  // move needs two arguments
    assert(replied("!"));
    assert(g_step_count == 0u);
}

// --- movement -----------------------------------------------------------------

// The whole point. km.move(dx,dy) is a one-shot displacement, and the mapped
// X/Y field is int8 with logical range +/-127 (inj_session.c boot_mouse_entries),
// so a 300-count move cannot ride on one report -- the engine would reject the
// field on overflow and pass the report through unmodified, losing the move
// silently. It has to arrive as several bounded steps that sum to the request.
static void test_large_move_is_split_and_sums_exactly(void)
{
    setup();
    assert(line("km.move(300,-450)") == KMCMD_HANDLED);
    assert(kmcmd_pending());
    drain();

    assert(g_step_count > 1u);
    assert(total_dx() == 300);
    assert(total_dy() == -450);
    for (uint32_t i = 0u; i < g_step_count; ++i) {
        const int16_t dx = g_steps[i].dx;
        const int16_t dy = g_steps[i].dy;
        assert(dx <= (int16_t)KMCMD_STEP_MAX && dx >= -(int16_t)KMCMD_STEP_MAX);
        assert(dy <= (int16_t)KMCMD_STEP_MAX && dy >= -(int16_t)KMCMD_STEP_MAX);
    }
}

// Motion needs `relative`. A sink with buttons but no relative used to pass
// motion_blocked(), which only refused when ALL THREE ops were absent, so the
// host got an ACK for a budget kmcmd_step() would never drain.
static void test_move_is_refused_when_only_buttons_are_wired(void)
{
    char reply[KMCMD_REPLY_MAX];
    const kmcmd_ops_t buttons_only = {
        .relative = NULL,
        .buttons = sink_buttons,
        .physical_mask = NULL,
        .ready = sink_ready,
        .ctx = NULL,
    };

    kmcmd_init(&buttons_only);
    kmcmd_set_mode(KMCMD_MODE_MAKCU);
    kmcmd_set_link(true);

    memset(reply, 0, sizeof(reply));
    assert(kmcmd_line("km.move(10,10)", reply, sizeof(reply)) == KMCMD_HANDLED);
    assert(strstr(reply, "!") != NULL);
    assert(kmcmd_pending_counts() == 0u);
}

// kmcmd_pending() answers yes/no, which cannot distinguish a budget that is
// draining from one that is not draining at all. The magnitude can, so stats
// reports it.
static void test_pending_counts_tracks_the_undrained_budget(void)
{
    setup();
    assert(kmcmd_pending_counts() == 0u);

    line("km.move(300,-450)");
    assert(kmcmd_pending_counts() == 750u);

    (void)kmcmd_step();
    assert(kmcmd_pending_counts() < 750u);

    drain();
    assert(kmcmd_pending_counts() == 0u);

    // Every axis contributes, or a stalled wheel would read as an idle link.
    line("km.wheel(7)");
    assert(kmcmd_pending_counts() == 7u);
}

// One step per call, never a loop that empties the budget in one go. The
// foreground loop also runs link_poll() and ERR051588 recovery; a drain that
// ran to completion inside one call would starve them for the length of the
// move. Same argument console.c makes for bounding console_flood_step().
static void test_step_emits_at_most_one_relative_per_call(void)
{
    setup();
    line("km.move(300,0)");
    (void)kmcmd_step();
    assert(g_step_count == 1u);
    (void)kmcmd_step();
    assert(g_step_count == 2u);
}

// A small move fits in one report and must not be split -- splitting it would
// turn a 5-count flick into several, each on a different report.
static void test_small_move_is_one_step(void)
{
    setup();
    line("km.move(5,-5)");
    assert(kmcmd_step());
    assert(g_step_count == 1u);
    assert(g_steps[0].dx == 5 && g_steps[0].dy == -5);
    assert(!kmcmd_pending());
}

// MAKCU's move() takes an optional segment count (default 1, ceiling 512).
// We honour it as a *minimum* number of steps: it can only ever subdivide
// further, never coarsen past the field bound the split exists to respect.
static void test_segments_subdivide(void)
{
    setup();
    line("km.move(40,0,8)");
    drain();
    assert(g_step_count >= 8u);
    assert(total_dx() == 40);
}

// Bezier control points are accepted (a real MAKCU host sends them) but the
// path is not curved: we have no cursor position to curve around, and the
// contract has no way to express one. Accepting the arguments and walking a
// straight line is the honest reading -- the displacement is exactly right and
// only the shape of the path differs, which nothing downstream can observe.
static void test_bezier_arguments_are_accepted_as_a_straight_line(void)
{
    setup();
    assert(line("km.move(100,50,8,40,25,80,10)") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 100);
    assert(total_dy() == 50);
}

// Constraint that cannot be engineered around: the wire has RELATIVE (additive)
// and PHYSICAL_MASK (buttons only, per protocol/report_injection_wire.json).
// There is no motion mask and no absolute-position command, and nothing on this
// link ever reports where the cursor is -- so moveto() and getpos() are not
// approximable. They must fail loudly.
static void test_absolute_move_is_refused(void)
{
    setup();
    assert(line("km.moveto(640,360)") == KMCMD_HANDLED);
    assert(replied("!"));
    assert(g_step_count == 0u);
    assert(!kmcmd_pending());
    assert(kmcmd_refused() == 1u);

    setup();
    assert(line("km.getpos()") == KMCMD_HANDLED);
    assert(replied("!"));
}

// --- the ready gate -----------------------------------------------------------

// gateware.py's command_fresh gates every RELATIVE on
// link_ready & session_active & map_store.active_valid & matching generation.
// Queueing motion before a map is committed would be motion that is silently
// dropped by the FPGA, so the refusal has to happen here where a host can see
// it.
static void test_motion_before_the_map_is_committed_is_refused(void)
{
    setup();
    g_ready = false;
    assert(line("km.move(10,10)") == KMCMD_HANDLED);
    assert(replied("!"));
    assert(!kmcmd_pending());
    assert(g_step_count == 0u);

    // And it works again once the session is up, without needing a re-init.
    g_ready = true;
    assert(line("km.move(10,10)") == KMCMD_HANDLED);
    drain();
    assert(total_dx() == 10);
}

// A sink that says "not now" is normal -- the FPGA's command queue is one deep.
// The budget must survive so the move completes on later calls, and the step
// must not spin.
static void test_a_refusing_sink_does_not_lose_the_budget(void)
{
    setup();
    line("km.move(100,0)");
    g_refuse_after = 0u;
    assert(!kmcmd_step());
    assert(g_step_count == 0u);
    assert(kmcmd_pending());
    assert(kmcmd_dropped() == 1u);

    g_refuse_after = 0xFFFFFFFFu;
    drain();
    assert(total_dx() == 100);
}

// A link that drops takes the FPGA's session and RX window with it, so every
// queued count is void. Carrying it over would replay a stale move into a
// fresh session.
static void test_link_loss_discards_the_budget(void)
{
    setup();
    line("km.move(400,400)");
    assert(kmcmd_pending());
    kmcmd_set_link(false);
    assert(!kmcmd_pending());
    assert(g_step_count == 0u);
}

// --- buttons ------------------------------------------------------------------

// The form every KMBox host library sends: km.left(1) down, km.left(0) up.
static void test_button_state_commands(void)
{
    setup();
    assert(line("km.left(1)") == KMCMD_HANDLED);
    assert(g_mask_count == 1u);
    assert(g_mask_writes[0] == KMCMD_BUTTON_LEFT);

    assert(line("km.right(1)") == KMCMD_HANDLED);
    assert(g_mask_count == 2u);
    // The mask is state, not an event: pressing right while left is held must
    // leave left held.
    assert(g_mask_writes[1] == (KMCMD_BUTTON_LEFT | KMCMD_BUTTON_RIGHT));

    assert(line("km.left(0)") == KMCMD_HANDLED);
    assert(g_mask_writes[2] == KMCMD_BUTTON_RIGHT);

    assert(line("km.middle(1)") == KMCMD_HANDLED);
    assert(g_mask_writes[3] == (KMCMD_BUTTON_RIGHT | KMCMD_BUTTON_MIDDLE));

    assert(line("km.side1(1)") == KMCMD_HANDLED);
    assert(line("km.side2(1)") == KMCMD_HANDLED);
    assert(g_mask_writes[5] ==
           (KMCMD_BUTTON_RIGHT | KMCMD_BUTTON_MIDDLE | KMCMD_BUTTON_SIDE1 | KMCMD_BUTTON_SIDE2));
}

// MAKCU documents state 2 as "silent_release": zero the state without emitting
// a frame. Our equivalent is to clear the bit locally and send nothing, so the
// next real report carries the change.
static void test_silent_release_does_not_emit(void)
{
    setup();
    line("km.left(1)");
    const uint32_t before = g_mask_count;
    assert(line("km.left(2)") == KMCMD_HANDLED);
    assert(g_mask_count == before);
    // But the state really did clear: pressing right must not resurrect left.
    line("km.right(1)");
    assert(g_mask_writes[g_mask_count - 1u] == KMCMD_BUTTON_RIGHT);
}

// A bare getter. MAKCU returns a lock indicator here; we answer with what we
// actually know -- our own injected state -- and must not emit a frame.
static void test_button_query_does_not_emit(void)
{
    setup();
    line("km.left(1)");
    const uint32_t before = g_mask_count;
    assert(line("km.left()") == KMCMD_HANDLED);
    assert(g_mask_count == before);
    assert(g_reply[0] != '\0');
}

// km.click(button[,count]) -- button 1..5. Each click has to be a press on one
// report and a release on a later one; collapsing them into a single mask write
// would be a press the PC never sees end.
static void test_click_presses_and_releases(void)
{
    setup();
    assert(line("km.click(1)") == KMCMD_HANDLED);
    drain();
    assert(g_mask_count >= 2u);
    assert(g_mask_writes[0] == KMCMD_BUTTON_LEFT);
    assert(g_mask_writes[1] == 0u);

    setup();
    assert(line("km.click(2,3)") == KMCMD_HANDLED);
    drain();
    uint32_t presses = 0u;
    for (uint32_t i = 0u; i < g_mask_count; ++i) {
        if (g_mask_writes[i] == KMCMD_BUTTON_RIGHT) {
            presses++;
        }
    }
    assert(presses == 3u);
    // Ends released. A click that left the button down would be a stuck button
    // on the PC with no further command coming.
    assert(g_mask_writes[g_mask_count - 1u] == 0u);
}

// BUTTON_STATE carries hold_reports (u16), so a requested hold has somewhere
// real to go. The conversion needs a report rate, and this module has no clock,
// so the rate is an explicit setting rather than a guess -- see kmcmd.h.
static void test_click_delay_becomes_hold_reports(void)
{
    setup();
    kmcmd_set_reports_per_ms(8u);  // 8 kHz High Speed, per CLAUDE.md
    assert(line("km.click(1,1,50)") == KMCMD_HANDLED);
    drain();
    assert(g_mask_writes[0] == KMCMD_BUTTON_LEFT);
    assert(g_mask_hold[0] == 400u);  // 50 ms * 8 reports/ms
}

// hold_reports is u16 on the wire. A host asking for a minute of hold must be
// clamped, not wrapped -- a wrap would turn a long hold into a short one.
static void test_click_hold_is_clamped_not_wrapped(void)
{
    setup();
    kmcmd_set_reports_per_ms(8u);
    line("km.click(1,1,60000)");  // 480000 reports, far past u16
    drain();
    assert(g_mask_hold[0] == 65535u);
}

static void test_click_rejects_an_unknown_button(void)
{
    setup();
    assert(line("km.click(9)") == KMCMD_HANDLED);
    assert(replied("!"));
    assert(g_mask_count == 0u);
}

// --- wheel and pan ------------------------------------------------------------

// MAKCU: "delta is clamped to +/-1 step (positive for scroll up, negative for
// scroll down)" because Windows rejects multi-step scrolls in one command --
// so km.wheel(-5) is documented to move one notch down. We do NOT clamp the
// request away: we clamp each STEP to one notch and queue the rest, so the
// total scroll a host asked for actually happens.
static void test_wheel_is_one_notch_per_step_but_keeps_the_total(void)
{
    setup();
    assert(line("km.wheel(-5)") == KMCMD_HANDLED);
    drain();
    int32_t total = 0;
    for (uint32_t i = 0u; i < g_step_count; ++i) {
        assert(g_steps[i].wheel >= -1 && g_steps[i].wheel <= 1);
        total += g_steps[i].wheel;
    }
    assert(total == -5);
    assert(g_step_count == 5u);
}

static void test_pan_is_accepted(void)
{
    setup();
    assert(line("km.pan(3)") == KMCMD_HANDLED);
    drain();
    int32_t total = 0;
    for (uint32_t i = 0u; i < g_step_count; ++i) {
        total += g_steps[i].pan;
    }
    assert(total == 3);
}

// Motion and scroll queued together must not serialise into separate reports
// when they could share one -- the RELATIVE payload carries x, y, wheel and pan
// in the same command.
static void test_move_and_wheel_share_a_step(void)
{
    setup();
    line("km.move(4,4)");
    line("km.wheel(1)");
    assert(kmcmd_step());
    assert(g_step_count == 1u);
    assert(g_steps[0].dx == 4);
    assert(g_steps[0].dy == 4);
    assert(g_steps[0].wheel == 1);
}

// --- locks --------------------------------------------------------------------

// Button locks map onto PHYSICAL_MASK, which the contract defines as
// buttons-only (a u64 button_mask and nothing else).
static void test_button_locks_reach_the_physical_mask(void)
{
    setup();
    assert(line("km.lock_ml(1)") == KMCMD_HANDLED);
    assert(g_physical_count == 1u);
    assert(g_physical_masks[0] == KMCMD_BUTTON_LEFT);

    assert(line("km.lock_mr(1)") == KMCMD_HANDLED);
    assert(g_physical_masks[1] == (KMCMD_BUTTON_LEFT | KMCMD_BUTTON_RIGHT));

    assert(line("km.lock_ml(0)") == KMCMD_HANDLED);
    assert(g_physical_masks[2] == KMCMD_BUTTON_RIGHT);
}

// The other half of constraint 2. PHYSICAL_MASK has no motion field, so an
// axis lock is not implementable and must not report success. This is the case
// most likely to be "fixed" later by someone inventing a wire feature, which
// is exactly why it is pinned.
static void test_axis_locks_are_refused(void)
{
    const char *const axis_locks[] = {
        "km.lock_mx(1)", "km.lock_my(1)",  "km.lock_mw(1)",
        "km.lock_mx+(1)", "km.lock_my-(1)", "km.lock_mw+(1)",
    };
    for (uint32_t i = 0u; i < sizeof(axis_locks) / sizeof(axis_locks[0]); ++i) {
        setup();
        assert(line(axis_locks[i]) == KMCMD_HANDLED);
        assert(replied("!"));
        assert(g_physical_count == 0u);
        assert(kmcmd_refused() == 1u);
    }
}

// catch_<target> requires the device to divert a physical event to the host
// rather than to the PC. There is no FPGA->MCU command for that, so it cannot
// work; the MCU sees reports only as REPORT_FRAGMENT telemetry, which is a
// copy, not an interception.
static void test_catch_is_refused(void)
{
    setup();
    assert(line("km.catch_ml(1)") == KMCMD_HANDLED);
    assert(replied("!"));
}

// --- keyboard -----------------------------------------------------------------

// The FPGA enumerates and clones a HID *boot mouse* (descriptors.py refuses a
// capture without bInterfaceProtocol == 2). There is no keyboard interface to
// inject into, so MAKCU's keyboard half must be refused rather than accepted
// into a void.
static void test_keyboard_commands_are_refused(void)
{
    const char *const keys[] = {
        "km.press('a')", "km.down('shift')", "km.up(\"ctrl\")", "km.string(\"Hello\")",
    };
    for (uint32_t i = 0u; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        setup();
        assert(line(keys[i]) == KMCMD_HANDLED);
        assert(replied("!"));
    }
}

// --- reply framing: the one place the two protocols genuinely conflict --------
//
// MAKCU specifies a reply for every setter: "all responses start with km. and
// end with CRLF followed by the prompt >>>", and echo(0) turns the setter ACK
// off. KMBox host libraries are fire-and-forget and read nothing back. A single
// framing cannot satisfy both, so the mode picks one.

static void test_makcu_mode_acks_setters_with_the_prompt(void)
{
    setup();
    kmcmd_set_mode(KMCMD_MODE_MAKCU);
    line("km.move(1,2)");
    assert(replied("km."));
    assert(replied(">>>"));
}

static void test_kmbox_mode_is_silent_on_accepted_setters(void)
{
    setup();
    kmcmd_set_mode(KMCMD_MODE_KMBOX);
    line("km.move(1,2)");
    assert(g_reply[0] == '\0');
    drain();
    assert(total_dx() == 1);
}

// Silence is only correct for something that worked. A refusal has to be
// audible in every mode, or an unsupported command looks like a successful one.
static void test_refusals_are_reported_even_in_kmbox_mode(void)
{
    setup();
    kmcmd_set_mode(KMCMD_MODE_KMBOX);
    assert(line("km.moveto(1,2)") == KMCMD_HANDLED);
    assert(replied("!"));
}

// MAKCU's echo(0) suppresses setter ACKs. It must not suppress refusals for
// the same reason.
static void test_echo_off_suppresses_acks_only(void)
{
    setup();
    kmcmd_set_mode(KMCMD_MODE_MAKCU);
    assert(line("km.echo(0)") == KMCMD_HANDLED);

    line("km.move(1,2)");
    assert(g_reply[0] == '\0');

    assert(line("km.moveto(1,2)") == KMCMD_HANDLED);
    assert(replied("!"));

    assert(line("km.echo(1)") == KMCMD_HANDLED);
    line("km.move(1,2)");
    assert(replied(">>>"));
}

// With the parser off, km lines must fall through so the console answers them
// itself. This is the escape hatch for an operator who typed something that
// only looks like a km command.
static void test_off_mode_passes_everything_through(void)
{
    setup();
    kmcmd_set_mode(KMCMD_MODE_OFF);
    assert(line("km.move(1,2)") == KMCMD_NOT_MINE);
    assert(g_step_count == 0u);
    assert(!kmcmd_pending());
}

static void test_version_answers_locally(void)
{
    setup();
    assert(line("km.version()") == KMCMD_HANDLED);
    assert(g_reply[0] != '\0');
    assert(replied("km."));
}

// --- no sink bound ------------------------------------------------------------

// The console is brought up before the injection session is wired, and an
// operator can type into it at any time. A parse with no sink must answer
// rather than fault, and must not pretend the motion happened.
static void test_no_sink_is_survivable(void)
{
    const kmcmd_ops_t ops = {
        .relative = NULL,
        .buttons = NULL,
        .physical_mask = NULL,
        .ready = NULL,
        .ctx = NULL,
    };
    kmcmd_init(&ops);
    kmcmd_set_mode(KMCMD_MODE_MAKCU);
    memset(g_reply, 0, sizeof(g_reply));
    assert(kmcmd_line("km.move(10,10)", g_reply, sizeof(g_reply)) == KMCMD_HANDLED);
    // A NULL ready() cannot be assumed ready: it means nothing is wired.
    assert(strstr(g_reply, "!") != NULL);
    (void)kmcmd_step();
}

// A reply buffer too small for the answer must be truncated and NUL terminated,
// never overrun. The console's own writer may take fewer bytes than offered,
// and this is the same bargain one layer up.
static void test_reply_buffer_is_never_overrun(void)
{
    setup();
    char small[8];
    memset(small, 0x5A, sizeof(small));
    (void)kmcmd_line("km.move(123456,123456)", small, sizeof(small));
    bool terminated = false;
    for (uint32_t i = 0u; i < sizeof(small); ++i) {
        if (small[i] == '\0') {
            terminated = true;
            break;
        }
    }
    assert(terminated);

    // And a zero-length buffer must simply write nothing.
    (void)kmcmd_line("km.move(1,1)", small, 0u);
}

// --- counters -----------------------------------------------------------------

static void test_counters_separate_accepted_from_refused(void)
{
    setup();
    line("km.move(1,1)");
    line("km.left(1)");
    assert(kmcmd_accepted() == 2u);
    assert(kmcmd_refused() == 0u);

    line("km.moveto(1,1)");
    line("km.lock_mx(1)");
    assert(kmcmd_refused() == 2u);
    assert(kmcmd_accepted() == 2u);
}

int main(void)
{
    // Must be first: it is the only point at which BSS defaults are visible.
    test_defaults_are_correct_before_init();

    test_non_km_lines_fall_through();
    test_prefix_is_optional();
    test_unknown_km_command_is_refused_not_passed_on();
    test_whitespace_and_signs();
    test_malformed_lines_are_refused();

    test_large_move_is_split_and_sums_exactly();
    test_move_is_refused_when_only_buttons_are_wired();
    test_pending_counts_tracks_the_undrained_budget();
    test_step_emits_at_most_one_relative_per_call();
    test_small_move_is_one_step();
    test_segments_subdivide();
    test_bezier_arguments_are_accepted_as_a_straight_line();
    test_absolute_move_is_refused();

    test_motion_before_the_map_is_committed_is_refused();
    test_a_refusing_sink_does_not_lose_the_budget();
    test_link_loss_discards_the_budget();

    test_button_state_commands();
    test_silent_release_does_not_emit();
    test_button_query_does_not_emit();
    test_click_presses_and_releases();
    test_click_delay_becomes_hold_reports();
    test_click_hold_is_clamped_not_wrapped();
    test_click_rejects_an_unknown_button();

    test_wheel_is_one_notch_per_step_but_keeps_the_total();
    test_pan_is_accepted();
    test_move_and_wheel_share_a_step();

    test_button_locks_reach_the_physical_mask();
    test_axis_locks_are_refused();
    test_catch_is_refused();
    test_keyboard_commands_are_refused();

    test_makcu_mode_acks_setters_with_the_prompt();
    test_kmbox_mode_is_silent_on_accepted_setters();
    test_refusals_are_reported_even_in_kmbox_mode();
    test_echo_off_suppresses_acks_only();
    test_off_mode_passes_everything_through();
    test_version_answers_locally();

    test_no_sink_is_survivable();
    test_reply_buffer_is_never_overrun();
    test_counters_separate_accepted_from_refused();

    printf("kmcmd_test: all cases passed\n");
    return 0;
}
