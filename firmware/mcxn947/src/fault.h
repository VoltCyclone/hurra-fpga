#ifndef HURRA_MCXN947_FAULT_H
#define HURRA_MCXN947_FAULT_H

#include <stdbool.h>
#include <stdint.h>

#define FAULT_CAUSE_TEXT_MAX 192u

typedef enum {
    FAULT_KIND_HARD = 0,
    FAULT_KIND_MEMMANAGE = 1,
    FAULT_KIND_BUS = 2,
    FAULT_KIND_USAGE = 3,
} fault_kind_t;

typedef enum {
    FAULT_STACK_MSP = 0,
    FAULT_STACK_PSP = 1,
} fault_stack_source_t;

typedef enum {
    FAULT_INJECTION_USAGE = 0,
    FAULT_INJECTION_BUS = 1,
    FAULT_INJECTION_HARD = 2,
    FAULT_INJECTION_STACK = 3,
    FAULT_INJECTION_FP = 4,
} fault_injection_t;

// CFSR cause bits. Kept as architectural literals so the portable decoder has
// no CMSIS dependency; target-only static assertions below hold them to CMSIS.
#define FAULT_CAUSE_IACCVIOL   0x00000001u
#define FAULT_CAUSE_DACCVIOL   0x00000002u
#define FAULT_CAUSE_MUNSTKERR  0x00000008u
#define FAULT_CAUSE_MSTKERR    0x00000010u
#define FAULT_CAUSE_MLSPERR    0x00000020u
#define FAULT_CAUSE_IBUSERR    0x00000100u
#define FAULT_CAUSE_PRECISERR  0x00000200u
#define FAULT_CAUSE_IMPRECISERR 0x00000400u
#define FAULT_CAUSE_UNSTKERR   0x00000800u
#define FAULT_CAUSE_STKERR     0x00001000u
#define FAULT_CAUSE_LSPERR     0x00002000u
#define FAULT_CAUSE_UNDEFINSTR 0x00010000u
#define FAULT_CAUSE_INVSTATE   0x00020000u
#define FAULT_CAUSE_INVPC      0x00040000u
#define FAULT_CAUSE_NOCP       0x00080000u
#define FAULT_CAUSE_STKOF      0x00100000u
#define FAULT_CAUSE_UNALIGNED  0x01000000u
#define FAULT_CAUSE_DIVBYZERO  0x02000000u
#define FAULT_CAUSE_ALL        0x031F3F3Bu

#define FAULT_CFSR_MMARVALID   0x00000080u
#define FAULT_CFSR_BFARVALID   0x00008000u

typedef struct {
    uint32_t r0;
    uint32_t r1;
    uint32_t r2;
    uint32_t r3;
    uint32_t r12;
    uint32_t lr;
    uint32_t pc;
    uint32_t xpsr;
} fault_frame_t;

typedef struct {
    fault_kind_t kind;
    uint32_t cfsr;
    uint32_t hfsr;
    uint32_t cause_flags;
    uint32_t mmfar;
    uint32_t bfar;
    bool mmfar_valid;
    bool bfar_valid;
    fault_frame_t frame;
} fault_diagnosis_t;

fault_stack_source_t fault_stack_source(uint32_t exc_return);
bool fault_frame_is_extended(uint32_t exc_return);
const uint32_t *fault_stacked_frame(const uint32_t *msp, const uint32_t *psp,
                                    uint32_t exc_return);
fault_diagnosis_t fault_decode(fault_kind_t kind, uint32_t cfsr,
                               uint32_t hfsr, uint32_t mmfar, uint32_t bfar,
                               fault_frame_t frame);
const char *fault_kind_name(fault_kind_t kind);
uint32_t fault_format_causes(uint32_t cause_flags, char *out,
                             uint32_t capacity);

void fault_handlers_init(void);

// Deliberately provoke one of the faults above. Supported injections do not
// return. False means either that this core cannot perform the request (FP on
// core1) or that a fallible provocation unexpectedly returned (the reserved
// bus address responded); the console makes either failure visible.
bool fault_inject(fault_injection_t injection);

void HardFault_Handler(void);
void MemManage_Handler(void);
void BusFault_Handler(void);
void UsageFault_Handler(void);

#endif  // HURRA_MCXN947_FAULT_H
