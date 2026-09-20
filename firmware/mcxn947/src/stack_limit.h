#ifndef HURRA_MCXN947_STACK_LIMIT_H
#define HURRA_MCXN947_STACK_LIMIT_H

#include <stdint.h>

typedef void (*stack_limit_writer_t)(uintptr_t value);

// Portable policy: arm both stack-limit registers at the same linker-defined
// floor, before either stack can silently grow into ordinary RAM.
void stack_limit_configure(uintptr_t stack_floor,
                           stack_limit_writer_t write_msplim,
                           stack_limit_writer_t write_psplim);

// Target entry point. Both core mains call this as their first statement.
void stack_limit_init(void);

#endif  // HURRA_MCXN947_STACK_LIMIT_H
