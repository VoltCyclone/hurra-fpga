// The two-write policy is portable and host-tested. CMSIS and the linker
// symbol are confined to the single trailing target guard.

#include "stack_limit.h"

void stack_limit_configure(uintptr_t stack_floor,
                           stack_limit_writer_t write_msplim,
                           stack_limit_writer_t write_psplim)
{
    write_msplim(stack_floor);
    write_psplim(stack_floor);
}

#if defined(MCXN947)

#include "fsl_device_registers.h"

extern uint32_t __StackLimit;

static void stack_limit_write_msplim(uintptr_t value)
{
    __set_MSPLIM((uint32_t)value);
}

static void stack_limit_write_psplim(uintptr_t value)
{
    __set_PSPLIM((uint32_t)value);
}

void stack_limit_init(void)
{
    stack_limit_configure((uintptr_t)&__StackLimit,
                          stack_limit_write_msplim,
                          stack_limit_write_psplim);
}

#endif
