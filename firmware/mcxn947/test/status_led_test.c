// The policy is a two-beat cardiac pulse phase-locked to retired slots. These
// tests pin three separate things: the exact waveform, the sampling margin
// that lets a ~20 Hz caller render it faithfully, and the stall behaviour.

#include <assert.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "status_led.h"

// The caller rewrites the pin once per snapshot read: 720 heartbeats/s divided
// by 36 is about 20 Hz, so about 400 slots pass between updates. The larger
// steps here stand in for a slower loop than step 5 measured.
#define SAMPLE_STEP_20HZ 400u
#define SAMPLE_STEP_MARGIN STATUS_LED_MIN_SEGMENT_SLOTS  // ~5.2 Hz, the floor.

static uint32_t rising_edges_when_sampled(uint32_t step, uint32_t offset,
                                          uint32_t cycles)
{
    const uint32_t span = cycles * STATUS_LED_CYCLE_SLOTS;
    uint32_t edges = 0u;
    // Slot 0 opens a beat, so treating "before the first sample" as dark
    // counts that opening beat exactly once.
    bool previous = false;

    for (uint32_t slot = offset; slot < span; slot += step) {
        const bool now = status_led_for_slot_counter(slot);
        if (now && !previous) {
            edges++;
        }
        previous = now;
    }
    return edges;
}

static void test_waveform_edges(void)
{
    // First beat, then the gap, then the second beat, then the rest.
    assert(status_led_for_slot_counter(0u));
    assert(status_led_for_slot_counter(STATUS_LED_BEAT1_END - 1u));
    assert(!status_led_for_slot_counter(STATUS_LED_BEAT1_END));
    assert(!status_led_for_slot_counter(STATUS_LED_GAP_END - 1u));
    assert(status_led_for_slot_counter(STATUS_LED_GAP_END));
    assert(status_led_for_slot_counter(STATUS_LED_BEAT2_END - 1u));
    assert(!status_led_for_slot_counter(STATUS_LED_BEAT2_END));
    assert(!status_led_for_slot_counter(STATUS_LED_CYCLE_SLOTS - 1u));

    // The cycle repeats, including across the 2^32 wrap: the cycle length is a
    // power of two dividing 2^32, so the rhythm never stutters at rollover.
    assert(status_led_for_slot_counter(STATUS_LED_CYCLE_SLOTS));
    assert(status_led_for_slot_counter(64u * STATUS_LED_CYCLE_SLOTS));
    assert(status_led_for_slot_counter(0xFFFFFFFFu - STATUS_LED_CYCLE_SLOTS + 1u));
    assert(!status_led_for_slot_counter(0xFFFFFFFFu));
}

// Walk one whole cycle and pin every run length. Four runs, in this order and
// no other: 2048 lit, 1536 dark, 1536 lit, 11264 dark.
static void test_run_structure(void)
{
    const uint32_t expected_lengths[4] = {
        STATUS_LED_BEAT1_END,
        STATUS_LED_GAP_END - STATUS_LED_BEAT1_END,
        STATUS_LED_BEAT2_END - STATUS_LED_GAP_END,
        STATUS_LED_CYCLE_SLOTS - STATUS_LED_BEAT2_END,
    };
    const bool expected_levels[4] = {true, false, true, false};

    uint32_t run = 0u;
    uint32_t run_length = 0u;
    bool run_level = status_led_for_slot_counter(0u);
    uint32_t lit = 0u;

    for (uint32_t phase = 0u; phase < STATUS_LED_CYCLE_SLOTS; ++phase) {
        const bool level = status_led_for_slot_counter(phase);
        if (level) {
            lit++;
        }
        if (level == run_level) {
            run_length++;
            continue;
        }
        assert(run < 4u);
        assert(run_level == expected_levels[run]);
        assert(run_length == expected_lengths[run]);
        run++;
        run_level = level;
        run_length = 1u;
    }
    // The final dark run closes at the cycle boundary.
    assert(run == 3u);
    assert(run_level == expected_levels[3]);
    assert(run_length == expected_lengths[3]);

    assert(lit == STATUS_LED_ON_SLOTS_PER_CYCLE);
    // Every run clears the sampling floor, which is what makes the waveform
    // survive a ~20 Hz caller.
    for (uint32_t i = 0u; i < 4u; ++i) {
        assert(expected_lengths[i] >= STATUS_LED_MIN_SEGMENT_SLOTS);
    }
}

// The design claim: sampled at the caller's real rate, from any phase, both
// beats render and neither aliases away. Two rising edges per cycle, exactly.
static void test_survives_coarse_sampling(void)
{
    const uint32_t steps[] = {
        SAMPLE_STEP_20HZ,   // ~20 Hz, what step 5 measured.
        410u,               // Same rate, loop drifted slightly.
        533u,               // ~15 Hz.
        800u,               // ~10 Hz.
        1000u,              // ~8 Hz.
        SAMPLE_STEP_MARGIN, // ~5.2 Hz: an update per shortest segment.
    };
    const uint32_t cycles = 4u;

    for (uint32_t s = 0u; s < sizeof(steps) / sizeof(steps[0]); ++s) {
        const uint32_t step = steps[s];
        // Every step tested is shorter than the opening beat, so sampling
        // always starts inside it and all 2*cycles beats are in the window.
        assert(step < STATUS_LED_BEAT1_END);
        for (uint32_t offset = 0u; offset < step; ++offset) {
            assert(rising_edges_when_sampled(step, offset, cycles)
                   == 2u * cycles);
        }
    }
}

static void test_stall_stops_the_animation(void)
{
    // A stalled slot_counter is a constant argument, so the level holds: the
    // LED stops animating rather than blinking on after the data behind it.
    const uint32_t stalled[] = {0u, 1000u, 3000u, 4000u, 10000u, 65535u};
    for (uint32_t i = 0u; i < sizeof(stalled) / sizeof(stalled[0]); ++i) {
        const bool level = status_led_for_slot_counter(stalled[i]);
        for (uint32_t repeat = 0u; repeat < 64u; ++repeat) {
            assert(status_led_for_slot_counter(stalled[i]) == level);
        }
    }

    // And a stall is far likelier to park it dark than to leave it falsely
    // lit: the rest dominates the cycle at better than three dark slots to
    // one lit.
    const uint32_t dark = STATUS_LED_CYCLE_SLOTS - STATUS_LED_ON_SLOTS_PER_CYCLE;
    assert(dark > 3u * STATUS_LED_ON_SLOTS_PER_CYCLE);
    assert(!status_led_for_slot_counter(10000u));  // Stalled mid-rest: dark.
    assert(status_led_for_slot_counter(1000u));    // Stalled mid-beat: lit.
}

int main(void)
{
    test_waveform_edges();
    test_run_structure();
    test_survives_coarse_sampling();
    test_stall_stops_the_animation();

    printf("status_led_test: ok (cycle %" PRIu32 " slots, %" PRIu32 " lit)\n",
           (uint32_t)STATUS_LED_CYCLE_SLOTS,
           (uint32_t)STATUS_LED_ON_SLOTS_PER_CYCLE);
    return 0;
}
