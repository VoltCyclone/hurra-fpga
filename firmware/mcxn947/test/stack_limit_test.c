#include <assert.h>
#include <stdint.h>
#include <stdio.h>

#include "stack_limit.h"

typedef struct {
    char reg;
    uintptr_t value;
} write_event_t;

static write_event_t s_events[2];
static uint32_t s_event_count;

static void write_msplim(uintptr_t value)
{
    assert(s_event_count < 2u);
    s_events[s_event_count++] = (write_event_t){.reg = 'M', .value = value};
}

static void write_psplim(uintptr_t value)
{
    assert(s_event_count < 2u);
    s_events[s_event_count++] = (write_event_t){.reg = 'P', .value = value};
}

static void test_configures_both_limits_at_the_linker_stack_floor(void)
{
    const uintptr_t stack_floor = (uintptr_t)0x2004b800u;

    s_event_count = 0u;
    stack_limit_configure(stack_floor, write_msplim, write_psplim);

    assert(s_event_count == 2u);
    assert(s_events[0].reg == 'M');
    assert(s_events[0].value == stack_floor);
    assert(s_events[1].reg == 'P');
    assert(s_events[1].value == stack_floor);
}

int main(void)
{
    test_configures_both_limits_at_the_linker_stack_floor();
    puts("stack_limit_test: ok");
    return 0;
}
