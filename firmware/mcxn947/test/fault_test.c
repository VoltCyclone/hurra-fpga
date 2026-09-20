#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "fault.h"

typedef struct {
    uint32_t bit;
    const char *name;
} cause_case_t;

static void test_exc_return_selects_the_interrupted_stack(void)
{
    assert(fault_stack_source(0xFFFFFFF1u) == FAULT_STACK_MSP);
    assert(fault_stack_source(0xFFFFFFF9u) == FAULT_STACK_MSP);
    assert(fault_stack_source(0xFFFFFFFDu) == FAULT_STACK_PSP);
    assert(fault_stack_source(0x00000004u) == FAULT_STACK_PSP);
}

static void test_shipped_frame_selection_uses_exact_stack_base(void)
{
    uint32_t msp[26] = {0};
    uint32_t psp[26] = {0};

    // Basic and FP-extended frames both start with the architectural basic
    // frame. Exact pointer equality catches both an inverted MSP/PSP decision
    // and any reintroduced 18-word offset.
    assert(fault_stacked_frame(msp, psp, 0xFFFFFFF9u) == msp);
    assert(fault_stacked_frame(msp, psp, 0xFFFFFFE9u) == msp);
    assert(fault_stacked_frame(msp, psp, 0xFFFFFFFDu) == psp);
    assert(fault_stacked_frame(msp, psp, 0xFFFFFFEDu) == psp);
}

static void test_every_required_cfsr_subcause_has_the_right_name(void)
{
    static const cause_case_t cases[] = {
        {FAULT_CAUSE_IACCVIOL, "IACCVIOL"},
        {FAULT_CAUSE_DACCVIOL, "DACCVIOL"},
        {FAULT_CAUSE_MUNSTKERR, "MUNSTKERR"},
        {FAULT_CAUSE_MSTKERR, "MSTKERR"},
        {FAULT_CAUSE_IBUSERR, "IBUSERR"},
        {FAULT_CAUSE_PRECISERR, "PRECISERR"},
        {FAULT_CAUSE_IMPRECISERR, "IMPRECISERR"},
        {FAULT_CAUSE_UNSTKERR, "UNSTKERR"},
        {FAULT_CAUSE_STKERR, "STKERR"},
        {FAULT_CAUSE_UNDEFINSTR, "UNDEFINSTR"},
        {FAULT_CAUSE_INVSTATE, "INVSTATE"},
        {FAULT_CAUSE_INVPC, "INVPC"},
        {FAULT_CAUSE_NOCP, "NOCP"},
        {FAULT_CAUSE_STKOF, "STKOF"},
        {FAULT_CAUSE_UNALIGNED, "UNALIGNED"},
        {FAULT_CAUSE_DIVBYZERO, "DIVBYZERO"},
    };

    for (uint32_t i = 0u; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        char text[FAULT_CAUSE_TEXT_MAX];
        assert(fault_format_causes(cases[i].bit, text, sizeof(text))
               == strlen(cases[i].name));
        assert(strcmp(text, cases[i].name) == 0);
    }
}

static void test_additional_lazy_state_errors_are_not_hidden(void)
{
    char text[FAULT_CAUSE_TEXT_MAX];

    assert(fault_format_causes(FAULT_CAUSE_MLSPERR | FAULT_CAUSE_LSPERR,
                               text, sizeof(text))
           == strlen("MLSPERR|LSPERR"));
    assert(strcmp(text, "MLSPERR|LSPERR") == 0);
}

static void test_multiple_subcauses_are_joined_in_cfsr_bit_order(void)
{
    char text[FAULT_CAUSE_TEXT_MAX];
    const uint32_t causes = FAULT_CAUSE_DACCVIOL | FAULT_CAUSE_PRECISERR |
                            FAULT_CAUSE_STKOF | FAULT_CAUSE_DIVBYZERO;

    assert(fault_format_causes(causes, text, sizeof(text))
           == strlen("DACCVIOL|PRECISERR|STKOF|DIVBYZERO"));
    assert(strcmp(text, "DACCVIOL|PRECISERR|STKOF|DIVBYZERO") == 0);

    assert(fault_format_causes(0u, text, sizeof(text)) == 4u);
    assert(strcmp(text, "NONE") == 0);
}

static void test_decode_preserves_status_frame_and_valid_addresses(void)
{
    const fault_frame_t frame = {
        .r0 = 0x00000000u,
        .r1 = 0x11111111u,
        .r2 = 0x22222222u,
        .r3 = 0x33333333u,
        .r12 = 0x12121212u,
        .lr = 0xeeeeeeeeu,
        .pc = 0xccccccccu,
        .xpsr = 0x01000000u,
    };
    const uint32_t cfsr = FAULT_CAUSE_DACCVIOL | FAULT_CAUSE_PRECISERR |
                          FAULT_CAUSE_STKOF | FAULT_CFSR_MMARVALID |
                          FAULT_CFSR_BFARVALID;
    const fault_diagnosis_t d = fault_decode(
        FAULT_KIND_HARD, cfsr, 0x40000000u, 0x20001234u, 0x40005678u,
        frame);

    assert(d.kind == FAULT_KIND_HARD);
    assert(strcmp(fault_kind_name(d.kind), "HardFault") == 0);
    assert(d.cfsr == cfsr);
    assert(d.hfsr == 0x40000000u);
    assert(d.cause_flags == (FAULT_CAUSE_DACCVIOL |
                             FAULT_CAUSE_PRECISERR | FAULT_CAUSE_STKOF));
    assert(d.mmfar_valid);
    assert(d.mmfar == 0x20001234u);
    assert(d.bfar_valid);
    assert(d.bfar == 0x40005678u);
    assert(memcmp(&d.frame, &frame, sizeof(frame)) == 0);
}

static void test_decode_rejects_unmarked_fault_addresses(void)
{
    const fault_frame_t frame = {0};
    const fault_diagnosis_t d = fault_decode(
        FAULT_KIND_BUS, FAULT_CAUSE_IMPRECISERR, 0u,
        0xaaaaaaaau, 0xbbbbbbbbu, frame);

    assert(strcmp(fault_kind_name(d.kind), "BusFault") == 0);
    assert(!d.mmfar_valid);
    assert(!d.bfar_valid);
}

int main(void)
{
    test_exc_return_selects_the_interrupted_stack();
    test_shipped_frame_selection_uses_exact_stack_base();
    test_every_required_cfsr_subcause_has_the_right_name();
    test_additional_lazy_state_errors_are_not_hidden();
    test_multiple_subcauses_are_joined_in_cfsr_bit_order();
    test_decode_preserves_status_frame_and_valid_addresses();
    test_decode_rejects_unmarked_fault_addresses();

    puts("fault_test: ok");
    return 0;
}
