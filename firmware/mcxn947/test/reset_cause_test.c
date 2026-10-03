#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "reset_cause.h"

typedef struct {
    uint32_t bit;
    const char *name;
} cause_case_t;

static void test_zero_names_no_recorded_cause(void)
{
    char text[RESET_CAUSE_TEXT_MAX];

    assert(reset_cause_format(0u, text, sizeof(text)) == 4u);
    assert(strcmp(text, "NONE") == 0);
}

static void test_every_documented_srs_bit_has_the_right_name(void)
{
    static const cause_case_t cases[] = {
        {0x00000001u, "WAKEUP"}, {0x00000002u, "POR"},
        {0x00000004u, "VD"},     {0x00000010u, "WARM"},
        {0x00000020u, "FATAL"},  {0x00000100u, "PIN"},
        {0x00000200u, "DAP"},    {0x00000400u, "RSTACK"},
        {0x00000800u, "LPACK"},  {0x00001000u, "SCG"},
        {0x00002000u, "WWDT0"},  {0x00004000u, "SW"},
        {0x00008000u, "LOCKUP"}, {0x00010000u, "CPU1"},
        {0x01000000u, "VBAT"},   {0x02000000u, "WWDT1"},
        {0x04000000u, "CDOG0"},  {0x08000000u, "CDOG1"},
        {0x10000000u, "JTAG"},   {0x40000000u, "SECVIO"},
        {0x80000000u, "TAMPER"},
    };

    for (uint32_t i = 0u; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        char text[RESET_CAUSE_TEXT_MAX];
        const uint32_t length = reset_cause_format(cases[i].bit, text,
                                                   sizeof(text));
        assert(length == strlen(cases[i].name));
        assert(strcmp(text, cases[i].name) == 0);
    }
}

static void test_multiple_causes_are_joined_in_bit_order(void)
{
    char text[RESET_CAUSE_TEXT_MAX];

    const uint32_t length = reset_cause_format(
        0x00000002u | 0x00008000u | 0x00010000u | 0x02000000u,
        text, sizeof(text));

    assert(length == strlen("POR|LOCKUP|CPU1|WWDT1"));
    assert(strcmp(text, "POR|LOCKUP|CPU1|WWDT1") == 0);
}

static void test_unknown_bits_are_not_silently_hidden(void)
{
    char text[RESET_CAUSE_TEXT_MAX];

    assert(reset_cause_format(0x00000008u, text, sizeof(text))
           == strlen("UNKNOWN"));
    assert(strcmp(text, "UNKNOWN") == 0);

    assert(reset_cause_format(0x0000000au, text, sizeof(text))
           == strlen("POR|UNKNOWN"));
    assert(strcmp(text, "POR|UNKNOWN") == 0);
}

static void test_small_buffer_is_nul_terminated(void)
{
    char text[5] = {'x', 'x', 'x', 'x', 'x'};

    assert(reset_cause_format(0x00008000u, text, sizeof(text)) == 4u);
    assert(memcmp(text, "LOCK", sizeof(text)) == 0);

    assert(reset_cause_format(0x00008000u, NULL, 0u) == 0u);
}

int main(void)
{
    test_zero_names_no_recorded_cause();
    test_every_documented_srs_bit_has_the_right_name();
    test_multiple_causes_are_joined_in_bit_order();
    test_unknown_bits_are_not_silently_hidden();
    test_small_buffer_is_nul_terminated();

    puts("reset_cause_test: ok");
    return 0;
}
