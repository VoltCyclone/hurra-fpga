// Reset-cause formatting is portable. The one CMC read and debug-UART report
// live in the single trailing target guard.

#include "reset_cause.h"

#include <stdbool.h>
#include <stddef.h>

typedef struct {
    uint32_t mask;
    const char *name;
} reset_cause_name_t;

static const reset_cause_name_t s_causes[] = {
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

#define RESET_CAUSE_KNOWN_MASK 0xDF01FF37u

static uint32_t reset_cause_append(char *out, uint32_t capacity,
                                   uint32_t length, const char *text)
{
    while (*text != '\0') {
        if (out != NULL && capacity != 0u && length + 1u < capacity) {
            out[length] = *text;
        }
        length++;
        text++;
    }
    return length;
}

uint32_t reset_cause_format(uint32_t srs, char *out, uint32_t capacity)
{
    uint32_t length = 0u;
    bool first = true;

    if (srs == 0u) {
        length = reset_cause_append(out, capacity, length, "NONE");
    } else {
        for (uint32_t i = 0u; i < sizeof(s_causes) / sizeof(s_causes[0]); ++i) {
            if ((srs & s_causes[i].mask) == 0u) {
                continue;
            }
            if (!first) {
                length = reset_cause_append(out, capacity, length, "|");
            }
            length = reset_cause_append(out, capacity, length, s_causes[i].name);
            first = false;
        }
        if ((srs & ~RESET_CAUSE_KNOWN_MASK) != 0u) {
            if (!first) {
                length = reset_cause_append(out, capacity, length, "|");
            }
            length = reset_cause_append(out, capacity, length, "UNKNOWN");
        }
    }

    if (out == NULL || capacity == 0u) {
        return 0u;
    }
    const uint32_t stored = (length < capacity) ? length : capacity - 1u;
    out[stored] = '\0';
    return stored;
}

#if defined(MCXN947)

#include "dbg_uart.h"
#include "fsl_device_registers.h"

static uint32_t s_latched_srs;

void reset_cause_latch(void)
{
    s_latched_srs = CMC0->SRS;
}

uint32_t reset_cause_latched(void)
{
    return s_latched_srs;
}

void reset_cause_report_debug(void)
{
    char text[RESET_CAUSE_TEXT_MAX];
    (void)reset_cause_format(s_latched_srs, text, sizeof(text));

    dbg_puts("*** RESET CAUSE: ");
    dbg_puts(text);
    dbg_puts(" SRS=");
    dbg_hex32(s_latched_srs);
    dbg_puts(" ***\n");
}

#endif
