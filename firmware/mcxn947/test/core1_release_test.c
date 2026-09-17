#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "core1_release.h"

static void test_assert_reset_sets_key_clock_and_reset_from_one_snapshot(void)
{
    const uint32_t before = 0x00000100u;
    const uint32_t after = core1_cpuctrl_assert_reset(before);

    assert(after == (0xC0C40000u | 0x00000100u | (1u << 3) | (1u << 5)));
}

static void test_release_sets_key_and_clock_but_clears_reset(void)
{
    const uint32_t before = 0x00000120u;
    const uint32_t after = core1_cpuctrl_release(before);

    assert(after == (0xC0C40000u | 0x00000100u | (1u << 3)));
}

// The measured failure this predicate exists for: an erased core1 region
// reads 0xFFFFFFFF for both vector words. Releasing CPU1 onto that locked it
// up and reset CPU0, boot-looping the whole part with the link live.
static void test_erased_region_is_not_a_valid_image(void)
{
    assert(!core1_image_valid(0xFFFFFFFFu, 0xFFFFFFFFu));
}

static void test_all_zero_region_is_not_a_valid_image(void)
{
    assert(!core1_image_valid(0u, 0u));
}

static void test_real_vector_pair_is_accepted(void)
{
    // Top of CPU1's m_data, and a Thumb entry point in its flash window.
    assert(core1_image_valid(0x20068000u, 0x000C0401u));
}

static void test_stack_outside_core1_ram_is_rejected(void)
{
    assert(!core1_image_valid(0x2004C000u, 0x000C0401u));  // shared window
    assert(!core1_image_valid(0x20000000u, 0x000C0401u));  // CPU0's RAM
    assert(!core1_image_valid(0x20068004u, 0x000C0401u));  // past the top
}

static void test_misaligned_stack_is_rejected(void)
{
    assert(!core1_image_valid(0x20068004u - 4u + 2u, 0x000C0401u));
    assert(!core1_image_valid(0x2005000Cu, 0x000C0401u));
}

static void test_reset_vector_outside_core1_flash_is_rejected(void)
{
    assert(!core1_image_valid(0x20068000u, 0x00000401u));  // CPU0's flash
    assert(!core1_image_valid(0x20068000u, 0x00100001u));  // past the window
    assert(!core1_image_valid(0x20068000u, 0x000BFFFFu));  // just below it
}

static void test_arm_state_reset_vector_is_rejected(void)
{
    // Armv8-M is Thumb-only; a clear bit 0 faults on the first fetch.
    assert(!core1_image_valid(0x20068000u, 0x000C0400u));
}

int main(void)
{
    test_erased_region_is_not_a_valid_image();
    test_all_zero_region_is_not_a_valid_image();
    test_real_vector_pair_is_accepted();
    test_stack_outside_core1_ram_is_rejected();
    test_misaligned_stack_is_rejected();
    test_reset_vector_outside_core1_flash_is_rejected();
    test_arm_state_reset_vector_is_rejected();
    test_assert_reset_sets_key_clock_and_reset_from_one_snapshot();
    test_release_sets_key_and_clock_but_clears_reset();

    printf("core1_release_test: ok\n");
    return 0;
}
