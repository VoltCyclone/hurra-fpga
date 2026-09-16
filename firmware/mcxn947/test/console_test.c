// Host test for src/console.c.
//
// The console is one of the two modules design doc section 7 calls "100%
// portable by construction", and this file is what that claim buys: every
// command, the line editor, the overflow path and the bulk load generator run
// here with no board attached.
//
// What is deliberately NOT asserted is the exact prose of any message. The
// assertions are on the counter NAMES and VALUES in `stats`, because those are
// what step 4's gate compares against the FPGA over JTAG; the wording around
// them is free to change.

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "console.h"

#define CAPTURE_MAX 16384

typedef struct {
    char text[CAPTURE_MAX];
    uint32_t length;
    // Bytes the writer will accept before refusing. UINT32_MAX = unlimited.
    uint32_t budget;
} capture_t;

static uint32_t capture_write(void *ctx, const char *data, uint32_t length)
{
    capture_t *c = (capture_t *)ctx;
    uint32_t allowed = length;

    if (c->budget != 0xFFFFFFFFu) {
        allowed = (length < c->budget) ? length : c->budget;
        c->budget -= allowed;
    }
    if (c->length + allowed > CAPTURE_MAX) {
        allowed = CAPTURE_MAX - c->length;
    }
    memcpy(&c->text[c->length], data, allowed);
    c->length += allowed;
    return allowed;
}

static console_stats_t g_stats;
static uint32_t g_stats_calls;

static void capture_stats(void *ctx, console_stats_t *out)
{
    (void)ctx;
    g_stats_calls++;
    *out = g_stats;
}

static capture_t g_capture;

static void setup(uint32_t budget)
{
    memset(&g_capture, 0, sizeof(g_capture));
    g_capture.budget = budget;
    g_stats_calls = 0u;

    const console_ops_t ops = {
        .write = capture_write,
        .stats = capture_stats,
        .ctx = &g_capture,
    };
    console_init(&ops);
}

static void feed(const char *s)
{
    console_feed((const uint8_t *)s, (uint32_t)strlen(s));
}

static bool captured(const char *needle)
{
    g_capture.text[g_capture.length] = '\0';
    return strstr(g_capture.text, needle) != NULL;
}

// --- formatting -------------------------------------------------------------

static void test_format_u32(void)
{
    char buffer[11];

    assert(console_format_u32(0u, buffer) == 1u);
    assert(strcmp(buffer, "0") == 0);

    assert(console_format_u32(1u, buffer) == 1u);
    assert(strcmp(buffer, "1") == 0);

    // The counters this formats are cumulative and 32-bit, and link_retire.h
    // says spi_slots wraps mod 2^32 rather than saturating -- so the maximum
    // is a value the console will really be asked to print, not a corner.
    assert(console_format_u32(4294967295u, buffer) == 10u);
    assert(strcmp(buffer, "4294967295") == 0);

    assert(console_format_u32(8049u, buffer) == 4u);
    assert(strcmp(buffer, "8049") == 0);
}

// --- line editing -----------------------------------------------------------

static void test_echo_and_prompt(void)
{
    setup(0xFFFFFFFFu);
    feed("hi");
    assert(captured("hi"));
    // No newline yet, so nothing was dispatched.
    assert(g_stats_calls == 0u);

    feed("\r");
    assert(captured("unknown command"));
    assert(captured("hurra> "));
}

static void test_backspace(void)
{
    setup(0xFFFFFFFFu);
    // "statX", rubbed out, then "s" -> "stats".
    feed("statX\b s\r");
    // A space was typed after the backspace in the line above, so this is
    // "stat s", not "stats" -- assert the *unknown* path, then do it cleanly.
    assert(captured("unknown command"));

    setup(0xFFFFFFFFu);
    feed("statX\bs\r");
    assert(g_stats_calls == 1u);

    // Backspace on an empty line must not underflow the cursor.
    setup(0xFFFFFFFFu);
    feed("\b\b\b\bstats\r");
    assert(g_stats_calls == 1u);

    // DEL (0x7F) is what most terminals actually send.
    setup(0xFFFFFFFFu);
    feed("statsX\x7f\r");
    assert(g_stats_calls == 1u);
}

static void test_crlf_is_one_line(void)
{
    // A terminal sending CRLF must not dispatch twice; the second byte lands
    // on an empty line and re-prompts only.
    setup(0xFFFFFFFFu);
    feed("stats\r\n");
    assert(g_stats_calls == 1u);
}

static void test_control_bytes_are_dropped(void)
{
    // An arrow key is ESC '[' 'A'. The ESC must not become part of a command
    // name; the printable remainder is allowed to, which is why this asserts
    // the unknown path rather than success.
    setup(0xFFFFFFFFu);
    feed("\x1b[Astats\r");
    assert(captured("unknown command"));
    assert(g_stats_calls == 0u);
}

static void test_overflow_is_reported_not_truncated(void)
{
    setup(0xFFFFFFFFu);

    char line[CONSOLE_LINE_MAX + 40u];
    memcpy(line, "stats", 5u);
    memset(&line[5], 'z', sizeof(line) - 6u);
    line[sizeof(line) - 1u] = '\0';
    feed(line);
    feed("\r");

    // The first five characters spell a real command. Acting on the truncated
    // prefix would run it; the console must refuse instead.
    assert(captured("line too long"));
    assert(g_stats_calls == 0u);

    // And it must recover on the next line rather than staying wedged.
    feed("stats\r");
    assert(g_stats_calls == 1u);
}

// --- commands ---------------------------------------------------------------

static void test_stats_reports_every_counter(void)
{
    setup(0xFFFFFFFFu);

    // Distinct values so a mis-wired field shows up as a wrong number rather
    // than as a plausible one.
    g_stats = (console_stats_t){
        .slots = 100u,
        .idle = 101u,
        .deliverable = 102u,
        .bad_sof = 103u,
        .bad_crc = 104u,
        .bad_length = 105u,
        .bad_type = 106u,
        .duplicate = 107u,
        .stale = 108u,
        .sequence_gap = 109u,
        .ring_drop = 110u,
        .isr_entries = 111u,
        .retire_stalls = 112u,
        .daddr_out_of_range = 113u,
        .recoveries = 114u,
        .framing_recoveries = 115u,
        .gap_wait_timeouts = 116u,
        .uptime_ms = 117u,
        .link_ready = true,
    };
    feed("stats\r");

    // The gate for this step is comparing these against the FPGA's registers,
    // so every one of them has to actually reach the wire.
    assert(captured("slots=100"));
    assert(captured("idle=101"));
    assert(captured("deliverable=102"));
    assert(captured("sof=103"));
    assert(captured("crc=104"));
    assert(captured("len=105"));
    assert(captured("typ=106"));
    assert(captured("dup=107"));
    assert(captured("stale=108"));
    assert(captured("gap=109"));
    assert(captured("ringdrop=110"));
    assert(captured("isr=111"));
    assert(captured("stall=112"));
    assert(captured("daddr=113"));
    assert(captured("recov=114"));
    assert(captured("framing=115"));
    assert(captured("gapto=116"));
    assert(captured("up_ms=117"));
    assert(captured("link ready"));
}

static void test_stats_reports_link_down(void)
{
    setup(0xFFFFFFFFu);
    memset(&g_stats, 0, sizeof(g_stats));
    g_stats.link_ready = false;
    feed("stats\r");
    assert(captured("link DOWN"));
}

static void test_help_and_version(void)
{
    setup(0xFFFFFFFFu);
    feed("help\r");
    assert(captured("stats"));
    assert(captured("flood"));

    setup(0xFFFFFFFFu);
    feed("?\r");
    assert(captured("stats"));

    setup(0xFFFFFFFFu);
    feed("version\r");
    assert(captured("mcxn947"));
}

static void test_greet(void)
{
    setup(0xFFFFFFFFu);
    console_greet();
    assert(captured("hurra> "));
}

// --- the bulk load generator ------------------------------------------------

static void test_flood_runs_and_stops(void)
{
    setup(0xFFFFFFFFu);
    assert(!console_flood_active());

    feed("flood\r");
    assert(console_flood_active());
    assert(console_flood_bytes() == 0u);

    console_flood_step();
    const uint32_t after_one = console_flood_bytes();
    assert(after_one > 0u);

    console_flood_step();
    assert(console_flood_bytes() > after_one);

    // Any byte stops it. This is the operator's escape hatch and it must not
    // itself depend on the pipe being drainable.
    feed("x");
    assert(!console_flood_active());
    assert(captured("flood stopped after"));

    // And the console is usable again straight away.
    feed("stats\r");
    assert(g_stats_calls == 1u);
}

static void test_flood_yields_when_the_writer_is_full(void)
{
    // A writer that only ever accepts 10 bytes: the generator must give up
    // within one call rather than spin. An unbounded fill here would starve
    // link_poll(), which is the exact failure the step-4 gate exists to rule
    // out -- so the instrument must not cause it.
    setup(10u);
    feed("flood\r");
    console_flood_step();
    assert(console_flood_bytes() <= 10u);
}

// --- drop accounting --------------------------------------------------------

static void test_short_writes_are_counted(void)
{
    setup(4u);
    assert(console_dropped_bytes() == 0u);

    // `stats` prints far more than four bytes, so the remainder is refused.
    feed("stats\r");
    assert(console_dropped_bytes() > 0u);
}

static void test_flood_shortfall_is_not_a_drop(void)
{
    // A saturated pipe is the flood doing its job, not a lost message. If
    // these were counted, `txdrop` would stop meaning "the host was too slow
    // for something that mattered".
    setup(0xFFFFFFFFu);
    feed("flood\r");
    const uint32_t before = console_dropped_bytes();
    g_capture.budget = 3u;  // refuse almost everything from here on
    console_flood_step();
    assert(console_dropped_bytes() == before);
}

static void test_no_provider_is_survivable(void)
{
    // A console bound with no stats provider must answer rather than fault.
    // The zeroed reading is a visibly implausible sample, which is the right
    // failure: every counter at zero on a live link never happens.
    const console_ops_t ops = {
        .write = capture_write,
        .stats = NULL,
        .ctx = &g_capture,
    };
    memset(&g_capture, 0, sizeof(g_capture));
    g_capture.budget = 0xFFFFFFFFu;
    console_init(&ops);
    feed("stats\r");
    assert(captured("slots=0"));
}

int main(void)
{
    test_format_u32();
    test_echo_and_prompt();
    test_backspace();
    test_crlf_is_one_line();
    test_control_bytes_are_dropped();
    test_overflow_is_reported_not_truncated();
    test_stats_reports_every_counter();
    test_stats_reports_link_down();
    test_help_and_version();
    test_greet();
    test_flood_runs_and_stops();
    test_flood_yields_when_the_writer_is_full();
    test_short_writes_are_counted();
    test_flood_shortfall_is_not_a_drop();
    test_no_provider_is_survivable();

    printf("console_test: all cases passed\n");
    return 0;
}
