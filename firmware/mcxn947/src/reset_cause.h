#ifndef HURRA_MCXN947_RESET_CAUSE_H
#define HURRA_MCXN947_RESET_CAUSE_H

#include <stdint.h>

#define RESET_CAUSE_TEXT_MAX 128u

// Format the set SRS causes in ascending bit order, separated by '|'. Zero is
// "NONE" and any unrecognised set bit appends "UNKNOWN". The return value is
// the number of characters stored, excluding the trailing NUL.
uint32_t reset_cause_format(uint32_t srs, char *out, uint32_t capacity);

// CPU0 target hooks. reset_cause_latch() reads CMC0->SRS exactly once; callers
// use the saved word thereafter so the boot report and console agree.
void reset_cause_latch(void);
uint32_t reset_cause_latched(void);
void reset_cause_report_debug(void);

#endif  // HURRA_MCXN947_RESET_CAUSE_H
