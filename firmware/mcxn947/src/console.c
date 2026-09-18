// The CDC console: line editing and command dispatch. Portable; see console.h.

#include <stddef.h>

#include "console.h"

static console_ops_t s_ops;

static char s_line[CONSOLE_LINE_MAX + 1u];
static uint32_t s_length;
static bool s_overflowed;

static bool s_flood;
static uint32_t s_flood_bytes;
static uint32_t s_dropped;

// --- Output ----------------------------------------------------------------

static uint32_t console_strlen(const char *s)
{
    uint32_t n = 0u;
    while (s[n] != '\0') {
        n++;
    }
    return n;
}

// Every byte leaves through here, so the drop accounting has exactly one
// place to be wrong. A short write is normal (see console.h) and is counted,
// not retried: retrying is the blocking behaviour the split exists to avoid.
static void console_emit(const char *data, uint32_t length)
{
    if (s_ops.write == NULL || length == 0u) {
        return;
    }
    const uint32_t taken = s_ops.write(s_ops.ctx, data, length);
    if (taken < length) {
        s_dropped += length - taken;
    }
}

static void console_put(const char *s)
{
    console_emit(s, console_strlen(s));
}

uint32_t console_format_u32(uint32_t value, char *out)
{
    char reversed[10];
    uint32_t n = 0u;

    if (value == 0u) {
        out[0] = '0';
        out[1] = '\0';
        return 1u;
    }
    while (value != 0u && n < sizeof(reversed)) {
        reversed[n++] = (char)('0' + (value % 10u));
        value /= 10u;
    }

    const uint32_t digits = n;
    for (uint32_t i = 0u; i < digits; ++i) {
        out[i] = reversed[digits - 1u - i];
    }
    out[digits] = '\0';
    return digits;
}

// `name=value ` -- the shape the step-3 debug UART report already used, kept
// so that a reader moving from the UART to this console does not have to
// learn a second layout, and so the two can be diffed during the changeover.
static void console_field(const char *name, uint32_t value)
{
    char digits[11];
    console_put(name);
    console_put("=");
    const uint32_t n = console_format_u32(value, digits);
    console_emit(digits, n);
    console_put(" ");
}

// --- Commands --------------------------------------------------------------

static void console_prompt(void)
{
    console_put("hurra> ");
}

static void cmd_help(void)
{
    console_put("commands:\r\n"
                "  help     this list\r\n"
                "  stats    link counters, to be compared against the FPGA's\r\n"
                "  version  firmware identity\r\n"
                "  flood    saturate this pipe until a key is pressed\r\n"
                "  cpu1halt hold CPU1 in reset (the link must not notice)\r\n"
                "  cpu1start release CPU1 again\r\n");
}

static void cmd_version(void)
{
    console_put("hurra-adapter mcxn947 step5 (cpu0: link + console; cpu1: idle)\r\n");
}

static void cmd_stats(void)
{
    console_stats_t s;

    // A zeroed struct is a truthful answer to "no provider bound" and a
    // wrong-looking one to a reader, which is the right way round: every
    // counter reading zero while the link is up is not a plausible sample.
    for (uint32_t i = 0u; i < sizeof(s); ++i) {
        ((uint8_t *)&s)[i] = 0u;
    }
    if (s_ops.stats != NULL) {
        s_ops.stats(s_ops.ctx, &s);
    }

    // Grouped the way the gate reads them: what the FPGA can also see first,
    // then what only this side knows.
    console_put("link ");
    console_put(s.link_ready ? "ready" : "DOWN");
    console_put("\r\n  ");
    console_field("slots", s.slots);
    console_field("idle", s.idle);
    console_field("deliverable", s.deliverable);
    console_put("\r\n  ");
    console_field("sof", s.bad_sof);
    console_field("crc", s.bad_crc);
    console_field("len", s.bad_length);
    console_field("typ", s.bad_type);
    console_put("\r\n  ");
    console_field("dup", s.duplicate);
    console_field("stale", s.stale);
    console_field("gap", s.sequence_gap);
    console_field("ringdrop", s.ring_drop);
    console_put("\r\n  ");
    console_field("isr", s.isr_entries);
    console_field("stall", s.retire_stalls);
    console_field("daddr", s.daddr_out_of_range);
    console_put("\r\n  ");
    console_field("recov", s.recoveries);
    console_field("framing", s.framing_recoveries);
    console_field("gapto", s.gap_wait_timeouts);
    console_put("\r\n  ");
    console_field("flood", s_flood_bytes);
    console_field("txdrop", s_dropped);
    console_field("up_ms", s.uptime_ms);
    console_put("\r\n  snapshot ");
    console_field("snapshot_seq", s.snapshot_seq);
    console_field("snapshot_slots", s.snapshot_slot_counter);
    console_field("snapshot_fail", s.snapshot_read_failures);
    console_put("\r\n  cpu1 ");
    if (s.cpu1_held_in_reset) {
        console_put("HELD-IN-RESET ");
    } else if (!s.cpu1_released) {
        console_put("NOT-RELEASED(no image) ");
    }
    console_put(s.cpu1_alive ? "alive " : "DOWN ");
    console_field("boot", s.cpu1_boot_count);
    console_field("heartbeat", s.cpu1_heartbeat);
    console_field("frames", s.cpu1_frames);
    console_field("blits", s.cpu1_blits);
    console_field("blitrej", s.cpu1_blit_rejects);
    console_put(" panel=");
    if ((s.cpu1_display_flags & 1u) == 0u) {
        console_put("ABSENT");
    } else if ((s.cpu1_display_flags & 2u) != 0u) {
        console_put("ok/FAULT");
    } else {
        console_put("ok");
    }
    console_put(" ");
    console_field("seen_slots", s.cpu1_seen_slot_counter);
    console_put("\r\n");
}

static void cmd_flood(void)
{
    s_flood = true;
    s_flood_bytes = 0u;
    console_put("flooding; send any byte to stop\r\n");
}

static void cmd_cpu1(bool start)
{
    void (*const action)(void *) =
        start ? (s_ops.cpu1_start) : (s_ops.cpu1_halt);
    if (action == NULL) {
        console_put("no CPU1 on this build\r\n");
        return;
    }
    action(s_ops.ctx);
    // Deliberately reports the request, not an outcome. Confirming that CPU1
    // actually stopped or started would mean waiting on CPU1, and the whole
    // point of this split is that CPU0 never does. Read `stats` to see what
    // the heartbeat did.
    console_put(start ? "cpu1 release requested\r\n" : "cpu1 held in reset\r\n");
}

static bool console_equal(const char *a, const char *b)
{
    uint32_t i = 0u;
    while (a[i] != '\0' && b[i] != '\0') {
        if (a[i] != b[i]) {
            return false;
        }
        i++;
    }
    return a[i] == b[i];
}

static void console_dispatch(void)
{
    s_line[s_length] = '\0';

    if (s_overflowed) {
        // Say so rather than acting on a truncated line: acting would run
        // whatever the first CONSOLE_LINE_MAX bytes happened to spell.
        console_put("line too long\r\n");
    } else if (s_length == 0u) {
        // Bare Enter: just re-prompt.
    } else if (console_equal(s_line, "help") || console_equal(s_line, "?")) {
        cmd_help();
    } else if (console_equal(s_line, "stats")) {
        cmd_stats();
    } else if (console_equal(s_line, "version")) {
        cmd_version();
    } else if (console_equal(s_line, "flood")) {
        cmd_flood();
    } else if (console_equal(s_line, "cpu1halt")) {
        cmd_cpu1(false);
    } else if (console_equal(s_line, "cpu1start")) {
        cmd_cpu1(true);
    } else {
        console_put("unknown command; try help\r\n");
    }

    s_length = 0u;
    s_overflowed = false;
    console_prompt();
}

// --- Input -----------------------------------------------------------------

void console_feed(const uint8_t *data, uint32_t length)
{
    for (uint32_t i = 0u; i < length; ++i) {
        const char c = (char)data[i];

        // Any input stops a flood, including the newline that a terminal
        // sends on its own. That is deliberate: the operator's escape hatch
        // must not itself require the pipe to be drainable.
        if (s_flood) {
            s_flood = false;
            console_put("\r\nflood stopped after ");
            char digits[11];
            const uint32_t n = console_format_u32(s_flood_bytes, digits);
            console_emit(digits, n);
            console_put(" bytes\r\n");
            console_prompt();
            continue;
        }

        if (c == '\r' || c == '\n') {
            console_put("\r\n");
            console_dispatch();
            continue;
        }
        if (c == '\b' || c == 0x7F) {
            if (s_length > 0u) {
                s_length--;
                console_put("\b \b");
            }
            continue;
        }
        // Printable ASCII only. Control bytes and anything above 0x7E are
        // dropped silently -- a terminal sending an arrow key must not be
        // able to put escape bytes into a command name.
        if (c < ' ' || c > '~') {
            continue;
        }
        if (s_length >= CONSOLE_LINE_MAX) {
            s_overflowed = true;
            continue;
        }
        s_line[s_length++] = c;
        console_emit(&c, 1u);
    }
}

void console_greet(void)
{
    console_put("\r\n");
    cmd_version();
    console_put("type help\r\n");
    console_prompt();
}

// --- Bulk load generator ---------------------------------------------------

bool console_flood_active(void)
{
    return s_flood;
}

uint32_t console_flood_bytes(void)
{
    return s_flood_bytes;
}

uint32_t console_dropped_bytes(void)
{
    return s_dropped;
}

void console_flood_step(void)
{
    if (!s_flood || s_ops.write == NULL) {
        return;
    }

    // 64 printable columns plus CRLF. Printable so the load can be watched in
    // a terminal, and line-terminated so a host reading it line-wise is not
    // itself the bottleneck.
    static const char chunk[] =
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\r\n";
    const uint32_t chunk_len = (uint32_t)(sizeof(chunk) - 1u);

    // Bounded per call. The foreground loop also runs link_poll(), and an
    // unbounded fill would starve it for as long as the host kept reading --
    // which is precisely the failure mode this whole measurement is meant to
    // rule out, so the instrument must not cause it.
    for (uint32_t i = 0u; i < 16u; ++i) {
        const uint32_t taken = s_ops.write(s_ops.ctx, chunk, chunk_len);
        s_flood_bytes += taken;
        if (taken < chunk_len) {
            // Short write means the FIFO is full. Dropping the remainder is
            // correct here and is NOT counted against txdrop: the flood is a
            // load generator, not a message, so a partial chunk is the pipe
            // being saturated, which is the whole point.
            break;
        }
    }
}

// --- Lifecycle -------------------------------------------------------------

void console_init(const console_ops_t *ops)
{
    s_ops = *ops;
    s_length = 0u;
    s_overflowed = false;
    s_flood = false;
    s_flood_bytes = 0u;
    s_dropped = 0u;
}
