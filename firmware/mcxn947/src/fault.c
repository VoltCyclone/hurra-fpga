// Fault interpretation is portable and host-tested. Exception capture, SCB
// reads, GPIO and cross-core publication live in one trailing target guard.

#include "fault.h"

#include <stddef.h>

typedef struct {
    uint32_t mask;
    const char *name;
} fault_cause_name_t;

static const fault_cause_name_t s_cause_names[] = {
    {FAULT_CAUSE_IACCVIOL, "IACCVIOL"},
    {FAULT_CAUSE_DACCVIOL, "DACCVIOL"},
    {FAULT_CAUSE_MUNSTKERR, "MUNSTKERR"},
    {FAULT_CAUSE_MSTKERR, "MSTKERR"},
    {FAULT_CAUSE_MLSPERR, "MLSPERR"},
    {FAULT_CAUSE_IBUSERR, "IBUSERR"},
    {FAULT_CAUSE_PRECISERR, "PRECISERR"},
    {FAULT_CAUSE_IMPRECISERR, "IMPRECISERR"},
    {FAULT_CAUSE_UNSTKERR, "UNSTKERR"},
    {FAULT_CAUSE_STKERR, "STKERR"},
    {FAULT_CAUSE_LSPERR, "LSPERR"},
    {FAULT_CAUSE_UNDEFINSTR, "UNDEFINSTR"},
    {FAULT_CAUSE_INVSTATE, "INVSTATE"},
    {FAULT_CAUSE_INVPC, "INVPC"},
    {FAULT_CAUSE_NOCP, "NOCP"},
    {FAULT_CAUSE_STKOF, "STKOF"},
    {FAULT_CAUSE_UNALIGNED, "UNALIGNED"},
    {FAULT_CAUSE_DIVBYZERO, "DIVBYZERO"},
};

__attribute__((noinline))
fault_stack_source_t fault_stack_source(uint32_t exc_return)
{
    return ((exc_return & 0x4u) != 0u) ? FAULT_STACK_PSP : FAULT_STACK_MSP;
}

bool fault_frame_is_extended(uint32_t exc_return)
{
    // EXC_RETURN[4] is zero when floating-point state belongs to the frame.
    // With lazy stacking, the space may only be reserved; the architectural
    // basic frame still begins at offset zero in either case.
    return (exc_return & 0x10u) == 0u;
}

__attribute__((noinline))
const uint32_t *fault_stacked_frame(const uint32_t *msp, const uint32_t *psp,
                                    uint32_t exc_return)
{
    return fault_stack_source(exc_return) == FAULT_STACK_PSP ? psp : msp;
}

fault_diagnosis_t fault_decode(fault_kind_t kind, uint32_t cfsr,
                               uint32_t hfsr, uint32_t mmfar, uint32_t bfar,
                               fault_frame_t frame)
{
    const fault_diagnosis_t diagnosis = {
        .kind = kind,
        .cfsr = cfsr,
        .hfsr = hfsr,
        .cause_flags = cfsr & FAULT_CAUSE_ALL,
        .mmfar = mmfar,
        .bfar = bfar,
        .mmfar_valid = (cfsr & FAULT_CFSR_MMARVALID) != 0u,
        .bfar_valid = (cfsr & FAULT_CFSR_BFARVALID) != 0u,
        .frame = frame,
    };
    return diagnosis;
}

const char *fault_kind_name(fault_kind_t kind)
{
    switch (kind) {
    case FAULT_KIND_HARD:
        return "HardFault";
    case FAULT_KIND_MEMMANAGE:
        return "MemManage";
    case FAULT_KIND_BUS:
        return "BusFault";
    case FAULT_KIND_USAGE:
        return "UsageFault";
    default:
        return "UnknownFault";
    }
}

static uint32_t fault_append(char *out, uint32_t capacity, uint32_t length,
                             const char *text)
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

uint32_t fault_format_causes(uint32_t cause_flags, char *out,
                             uint32_t capacity)
{
    uint32_t length = 0u;
    bool first = true;

    for (uint32_t i = 0u;
         i < sizeof(s_cause_names) / sizeof(s_cause_names[0]); ++i) {
        if ((cause_flags & s_cause_names[i].mask) == 0u) {
            continue;
        }
        if (!first) {
            length = fault_append(out, capacity, length, "|");
        }
        length = fault_append(out, capacity, length, s_cause_names[i].name);
        first = false;
    }
    if (first) {
        length = fault_append(out, capacity, length, "NONE");
    }

    if (out == NULL || capacity == 0u) {
        return 0u;
    }
    const uint32_t stored = (length < capacity) ? length : capacity - 1u;
    out[stored] = '\0';
    return stored;
}

#if defined(MCXN947)

#include "fsl_device_registers.h"
#include "shared_window.h"

#define FAULT_EMERGENCY_STACK_WORDS 256u
static uint32_t s_fault_stack[FAULT_EMERGENCY_STACK_WORDS]
    __attribute__((used, aligned(8)));

_Static_assert(FAULT_CAUSE_IACCVIOL == SCB_CFSR_IACCVIOL_Msk,
               "IACCVIOL mask must match CMSIS");
_Static_assert(FAULT_CAUSE_DACCVIOL == SCB_CFSR_DACCVIOL_Msk,
               "DACCVIOL mask must match CMSIS");
_Static_assert(FAULT_CAUSE_MUNSTKERR == SCB_CFSR_MUNSTKERR_Msk,
               "MUNSTKERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_MSTKERR == SCB_CFSR_MSTKERR_Msk,
               "MSTKERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_MLSPERR == SCB_CFSR_MLSPERR_Msk,
               "MLSPERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_IBUSERR == SCB_CFSR_IBUSERR_Msk,
               "IBUSERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_PRECISERR == SCB_CFSR_PRECISERR_Msk,
               "PRECISERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_IMPRECISERR == SCB_CFSR_IMPRECISERR_Msk,
               "IMPRECISERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_UNSTKERR == SCB_CFSR_UNSTKERR_Msk,
               "UNSTKERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_STKERR == SCB_CFSR_STKERR_Msk,
               "STKERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_LSPERR == SCB_CFSR_LSPERR_Msk,
               "LSPERR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_UNDEFINSTR == SCB_CFSR_UNDEFINSTR_Msk,
               "UNDEFINSTR mask must match CMSIS");
_Static_assert(FAULT_CAUSE_INVSTATE == SCB_CFSR_INVSTATE_Msk,
               "INVSTATE mask must match CMSIS");
_Static_assert(FAULT_CAUSE_INVPC == SCB_CFSR_INVPC_Msk,
               "INVPC mask must match CMSIS");
_Static_assert(FAULT_CAUSE_NOCP == SCB_CFSR_NOCP_Msk,
               "NOCP mask must match CMSIS");
_Static_assert(FAULT_CAUSE_STKOF == SCB_CFSR_STKOF_Msk,
               "STKOF mask must match CMSIS");
_Static_assert(FAULT_CAUSE_UNALIGNED == SCB_CFSR_UNALIGNED_Msk,
               "UNALIGNED mask must match CMSIS");
_Static_assert(FAULT_CAUSE_DIVBYZERO == SCB_CFSR_DIVBYZERO_Msk,
               "DIVBYZERO mask must match CMSIS");
_Static_assert(FAULT_CFSR_MMARVALID == SCB_CFSR_MMARVALID_Msk,
               "MMARVALID mask must match CMSIS");
_Static_assert(FAULT_CFSR_BFARVALID == SCB_CFSR_BFARVALID_Msk,
               "BFARVALID mask must match CMSIS");

// Architecturally reserved System-space address. The MCXN947 headers describe
// no responder here, but only the bench can prove that the interconnect returns
// a precise BusFault rather than a value on this silicon revision.
#define FAULT_INJECTION_BUS_ADDRESS ((uintptr_t)0xFFFFFFF0u)

__attribute__((noinline, noreturn))
static void fault_inject_undefined_instruction(void)
{
    __asm volatile("udf #0");
    __builtin_unreachable();
}

__attribute__((noinline))
static void fault_inject_reserved_read(void)
{
    const volatile uint32_t *const address =
        (const volatile uint32_t *)FAULT_INJECTION_BUS_ADDRESS;
    (void)*address;
}

typedef void (*fault_recursion_fn_t)(uint32_t depth);

__attribute__((noinline))
static void fault_inject_recurse(uint32_t depth)
{
    // Volatile storage plus an indirect recursive call prevents tail-call and
    // loop conversion: every level must consume MSP until MSPLIM objects.
    volatile uint32_t frame[16];
    fault_recursion_fn_t volatile recurse = fault_inject_recurse;
    frame[0] = depth;
    frame[15] = depth ^ 0x5A5A5A5Au;
    recurse(depth + 1u);
    __asm volatile("" : : "r"(frame[0]), "r"(frame[15]) : "memory");
}

#if defined(CPU_MCXN947VDF_cm33_core0)

#include "dbg_uart.h"
#include "fsl_clock.h"
#include "fsl_gpio.h"
#include "fsl_port.h"
#include "link.h"

#define FAULT_RED_LED_PORT PORT0
#define FAULT_RED_LED_GPIO GPIO0
#define FAULT_RED_LED_PIN 10u

static void fault_red_led_on(void)
{
    CLOCK_EnableClock(kCLOCK_Port0);
    CLOCK_EnableClock(kCLOCK_Gpio0);
    PORT_SetPinMux(FAULT_RED_LED_PORT, FAULT_RED_LED_PIN, kPORT_MuxAlt0);
    const gpio_pin_config_t red = {
        .pinDirection = kGPIO_DigitalOutput,
        .outputLogic = 0u,  // Active low: initialise already lit.
    };
    GPIO_PinInit(FAULT_RED_LED_GPIO, FAULT_RED_LED_PIN, &red);
    GPIO_PortClear(FAULT_RED_LED_GPIO, 1u << FAULT_RED_LED_PIN);
}

static void fault_debug_print(const fault_diagnosis_t *diagnosis,
                              uint32_t exc_return)
{
    char causes[FAULT_CAUSE_TEXT_MAX];
    (void)fault_format_causes(diagnosis->cause_flags, causes, sizeof(causes));

    dbg_uart_emergency_puts("\n*** CPU0 ");
    dbg_uart_emergency_puts(fault_kind_name(diagnosis->kind));
    dbg_uart_emergency_puts(" ***\ncauses=");
    dbg_uart_emergency_puts(causes);
    dbg_uart_emergency_puts("\nCFSR=");
    dbg_uart_emergency_hex32(diagnosis->cfsr);
    dbg_uart_emergency_puts(" HFSR=");
    dbg_uart_emergency_hex32(diagnosis->hfsr);
    dbg_uart_emergency_puts("\nEXC_RETURN=");
    dbg_uart_emergency_hex32(exc_return);
    dbg_uart_emergency_puts(" frame=");
    dbg_uart_emergency_puts(fault_frame_is_extended(exc_return)
                                ? "extended" : "basic");
    dbg_uart_emergency_puts(" PC=");
    dbg_uart_emergency_hex32(diagnosis->frame.pc);
    dbg_uart_emergency_puts(" LR=");
    dbg_uart_emergency_hex32(diagnosis->frame.lr);
    dbg_uart_emergency_puts(" xPSR=");
    dbg_uart_emergency_hex32(diagnosis->frame.xpsr);
    if (diagnosis->mmfar_valid) {
        dbg_uart_emergency_puts("\nMMFAR=");
        dbg_uart_emergency_hex32(diagnosis->mmfar);
    }
    if (diagnosis->bfar_valid) {
        dbg_uart_emergency_puts("\nBFAR=");
        dbg_uart_emergency_hex32(diagnosis->bfar);
    }
    dbg_uart_emergency_puts("\n*** HALTED ***\n");
}

static void fault_inject_log(fault_injection_t injection)
{
    dbg_uart_emergency_puts("\n[fault-inject] provoking ");
    switch (injection) {
    case FAULT_INJECTION_USAGE:
        dbg_uart_emergency_puts("UsageFault with UDF\n");
        break;
    case FAULT_INJECTION_BUS:
        dbg_uart_emergency_puts("BusFault by reading 0xFFFFFFF0\n");
        break;
    case FAULT_INJECTION_HARD:
        dbg_uart_emergency_puts("HardFault by forced BusFault escalation\n");
        break;
    case FAULT_INJECTION_STACK:
        dbg_uart_emergency_puts("MSPLIM stack overflow\n");
        break;
    case FAULT_INJECTION_FP:
        dbg_uart_emergency_puts(
            "FP extended frame with volatile VMUL then UDF (lazy stacking enabled)\n");
        break;
    default:
        dbg_uart_emergency_puts("unknown fault\n");
        break;
    }
}

__attribute__((noinline, noreturn))
static void fault_inject_fp_context(void)
{
    volatile float left = 3.25f;
    volatile float right = -1.5f;
    volatile float product = left * right;

    // Keep the volatile result live through the synchronous exception. FPCCR
    // retains its reset-default lazy policy; EXC_RETURN[4], printed by the
    // handler, is the assertion that this operation set FPCA.
    __asm volatile("" : : "m"(product) : "memory");
    __asm volatile("udf #0");
    __builtin_unreachable();
}

#else

static void fault_cpu1_publish(const fault_diagnosis_t *diagnosis)
{
    // The normal link classifier keeps owning sequence/verdict/suspect above.
    // CPU1 writes only its appended one-shot record, and publishes flags last.
    g_shared_window.fault.cfsr = diagnosis->cfsr;
    g_shared_window.fault.hfsr = diagnosis->hfsr;
    g_shared_window.fault.mmfar = diagnosis->mmfar;
    g_shared_window.fault.bfar = diagnosis->bfar;
    g_shared_window.fault.pc = diagnosis->frame.pc;
    g_shared_window.fault.lr = diagnosis->frame.lr;
    __DMB();
    g_shared_window.fault.fault_flags =
        SHARED_CPU1_FAULT_VALID |
        ((uint32_t)diagnosis->kind << SHARED_CPU1_FAULT_KIND_SHIFT) |
        (diagnosis->cause_flags & SHARED_CPU1_FAULT_CAUSE_MASK);
}

#endif

bool fault_inject(fault_injection_t injection)
{
#if defined(CPU_MCXN947VDF_cm33_core0)
    fault_inject_log(injection);
#else
    // This image is built for the no-FPU core with -mfloat-abi=soft. Never
    // disguise that limitation by turning `fp` into an ordinary UsageFault.
    if (injection == FAULT_INJECTION_FP) {
        return false;
    }
#endif

    switch (injection) {
    case FAULT_INJECTION_USAGE:
        fault_inject_undefined_instruction();
    case FAULT_INJECTION_BUS:
        fault_inject_reserved_read();
        break;
    case FAULT_INJECTION_HARD: {
        const bool bus_fault_was_enabled =
            (SCB->SHCSR & SCB_SHCSR_BUSFAULTENA_Msk) != 0u;
        SCB->SHCSR &= ~SCB_SHCSR_BUSFAULTENA_Msk;
        __DSB();
        __ISB();
        fault_inject_reserved_read();
        if (bus_fault_was_enabled) {
            SCB->SHCSR |= SCB_SHCSR_BUSFAULTENA_Msk;
            __DSB();
            __ISB();
        }
        break;
    }
    case FAULT_INJECTION_STACK:
        fault_inject_recurse(0u);
        break;
    case FAULT_INJECTION_FP:
#if defined(CPU_MCXN947VDF_cm33_core0)
        fault_inject_fp_context();
#else
        return false;
#endif
    default:
        return false;
    }

#if defined(CPU_MCXN947VDF_cm33_core0)
    dbg_uart_emergency_puts(
        "[fault-inject] ERROR: provocation returned without a fault\n");
#endif
    return false;
}

void fault_handlers_init(void)
{
    SCB->SHCSR |= SCB_SHCSR_MEMFAULTENA_Msk |
                  SCB_SHCSR_BUSFAULTENA_Msk |
                  SCB_SHCSR_USGFAULTENA_Msk;
    __DSB();
    __ISB();
}

__attribute__((used, noinline, noreturn))
static void fault_dispatch(const uint32_t *msp, const uint32_t *psp,
                           uint32_t exc_return, uint32_t raw_kind)
{
    __disable_irq();

    const uint32_t *stacked = fault_stacked_frame(msp, psp, exc_return);

#if defined(CPU_MCXN947VDF_cm33_core0)
    // Safety actions are deliberately first and ordered. Nothing diagnostic
    // is allowed to fault or spin while the FPGA still trusts this CPU.
    link_mcu_ready_set(false);
    fault_red_led_on();
#endif

    const fault_frame_t frame = {
        .r0 = stacked[0],
        .r1 = stacked[1],
        .r2 = stacked[2],
        .r3 = stacked[3],
        .r12 = stacked[4],
        .lr = stacked[5],
        .pc = stacked[6],
        .xpsr = stacked[7],
    };
    const fault_diagnosis_t diagnosis = fault_decode(
        (fault_kind_t)raw_kind, SCB->CFSR, SCB->HFSR,
        SCB->MMFAR, SCB->BFAR, frame);

#if defined(CPU_MCXN947VDF_cm33_core0)
    fault_debug_print(&diagnosis, exc_return);
#else
    fault_cpu1_publish(&diagnosis);
#endif

    for (;;) {
        __asm volatile("nop");
    }
}

#define FAULT_HANDLER(handler_name, kind_number)                              \
    __attribute__((naked)) void handler_name(void)                            \
    {                                                                          \
        __asm volatile(                                                        \
            "mrs r0, msp\n"                                                   \
            "mrs r1, psp\n"                                                   \
            "mov r2, lr\n"                                                    \
            "movs r3, #" #kind_number "\n"                                    \
            "ldr r12, =s_fault_stack\n"                                       \
            "msr msplim, r12\n"                                               \
            "add.w r12, r12, #1024\n"                                         \
            "msr msp, r12\n"                                                  \
            "b fault_dispatch\n");                                             \
    }

FAULT_HANDLER(HardFault_Handler, 0)
FAULT_HANDLER(MemManage_Handler, 1)
FAULT_HANDLER(BusFault_Handler, 2)
FAULT_HANDLER(UsageFault_Handler, 3)

#endif  // MCXN947
