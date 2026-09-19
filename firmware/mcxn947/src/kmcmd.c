// KMBox / MAKCU serial command input. Portable; see kmcmd.h.

#include <stddef.h>

#include "kmcmd.h"

// Longest command name accepted. The longest real one is `remap_button` (12);
// the widest shape is a lock target with a direction suffix, `lock_mw+` (8).
#define NAME_MAX 20u

// move() is the widest call: dx, dy, segments and two Bezier control points.
#define ARGS_MAX 8u

static kmcmd_ops_t s_ops;
static kmcmd_mode_t s_mode;
// Stored INVERTED so that zero-initialised BSS means echo ON, which is
// MAKCU's documented default. The console is reachable before kmcmd_init()
// has run, and a bare getter takes the reply path without needing the link --
// so a plain `s_echo` would answer an un-inited parser's query with silence,
// and a MAKCU host reads until `>>>`.
static bool s_echo_off;
static uint32_t s_reports_per_ms;

// Injected button state, and which physical buttons are suppressed. Both are
// STATE, not events: a press that arrives while another button is held must
// leave that one held, so each command edits the mask and re-sends the whole
// thing.
static uint32_t s_buttons;
static uint32_t s_physical;

// Pending displacement. Held as int32 because a host may legitimately ask for
// more counts than one report can carry -- that is the whole reason a move is a
// budget (see kmcmd.h).
static int32_t s_pend_x;
static int32_t s_pend_y;
static int32_t s_pend_wheel;
static int32_t s_pend_pan;

// Per-axis cap for the current motion budget. KMCMD_STEP_MAX unless a segment
// count asked for something finer.
static int32_t s_step_cap;

// Pending click. `s_click_release_pending` is the second half of a click whose
// release this module drives itself; a click given an explicit hold instead
// leaves the release to the engine's hold_reports and queues no second half.
static uint32_t s_click_mask;
static uint32_t s_click_remaining;
static uint32_t s_click_hold;
static bool s_click_release_pending;

static uint32_t s_accepted;
static uint32_t s_refused;
static uint32_t s_dropped;

// --- small helpers ----------------------------------------------------------

static bool streq(const char *a, const char *b)
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

static bool is_digit(char c)
{
    return c >= '0' && c <= '9';
}

static bool is_space(char c)
{
    return c == ' ' || c == '\t';
}

static int32_t abs32(int32_t v)
{
    return (v < 0) ? -v : v;
}

// --- bounded reply builder --------------------------------------------------
//
// Truncates and always NUL terminates, and writes nothing at all when handed a
// zero-length buffer. Same bargain as the console's writer one layer up: the
// caller may not be able to take everything, and that is not an error.

typedef struct {
    char *buf;
    uint32_t size;
    uint32_t len;
} reply_t;

static void reply_init(reply_t *r, char *buf, uint32_t size)
{
    r->buf = buf;
    r->size = size;
    r->len = 0u;
    if (size != 0u) {
        buf[0] = '\0';
    }
}

static void reply_put(reply_t *r, const char *s)
{
    if (r->size == 0u) {
        return;
    }
    for (uint32_t i = 0u; s[i] != '\0'; ++i) {
        if (r->len + 1u >= r->size) {
            break;
        }
        r->buf[r->len++] = s[i];
    }
    r->buf[r->len] = '\0';
}

static void reply_i32(reply_t *r, int32_t value)
{
    char digits[12];
    uint32_t n = 0u;
    uint32_t magnitude;

    if (value < 0) {
        reply_put(r, "-");
        // Negated as unsigned so INT32_MIN does not overflow on the way.
        magnitude = (uint32_t)(-(int64_t)value);
    } else {
        magnitude = (uint32_t)value;
    }
    if (magnitude == 0u) {
        reply_put(r, "0");
        return;
    }
    while (magnitude != 0u && n < sizeof(digits) - 1u) {
        digits[n++] = (char)('0' + (magnitude % 10u));
        magnitude /= 10u;
    }
    char ordered[12];
    for (uint32_t i = 0u; i < n; ++i) {
        ordered[i] = digits[n - 1u - i];
    }
    ordered[n] = '\0';
    reply_put(r, ordered);
}

// --- command classification -------------------------------------------------

typedef enum {
    CMD_UNKNOWN = 0,
    CMD_MOVE,
    CMD_BUTTON,  // left / right / middle / side1 / side2
    CMD_CLICK,
    CMD_WHEEL,
    CMD_PAN,
    CMD_LOCK_BUTTON,
    CMD_VERSION,
    CMD_ECHO,
    // Recognised but not expressible on this link; each carries a reason.
    CMD_REFUSE,
} cmd_kind_t;

typedef struct {
    cmd_kind_t kind;
    uint32_t button;      // CMD_BUTTON / CMD_LOCK_BUTTON: the bit
    const char *reason;   // CMD_REFUSE: why
} classified_t;

// Everything MAKCU documents that this device cannot do, with the reason it
// cannot. Named individually rather than lumped into one "unsupported" so a
// host (or a reader) can tell a missing feature from an impossible one.
static bool classify_refusal(const char *name, classified_t *out)
{
    // No absolute positioning: injection is additive (candidate = real_delta +
    // injected), nothing on this link reports the cursor position, and there is
    // no motion mask with which to override the physical delta.
    if (streq(name, "moveto") || streq(name, "silent")) {
        out->reason = "noabsolute";
        return true;
    }
    if (streq(name, "getpos") || streq(name, "screen")) {
        out->reason = "nopos";
        return true;
    }
    // PHYSICAL_MASK is buttons-only; there is no axis or wheel mask.
    if (streq(name, "lock_mx") || streq(name, "lock_my") || streq(name, "lock_mw") ||
        streq(name, "lock_mx+") || streq(name, "lock_mx-") || streq(name, "lock_my+") ||
        streq(name, "lock_my-") || streq(name, "lock_mw+") || streq(name, "lock_mw-")) {
        out->reason = "noaxismask";
        return true;
    }
    // Catch needs the device to divert a physical event away from the PC. The
    // MCU only ever sees reports as REPORT_FRAGMENT telemetry, which is a copy
    // taken after the fact, not an interception point.
    if (streq(name, "catch_ml") || streq(name, "catch_mr") || streq(name, "catch_mm") ||
        streq(name, "catch_ms1") || streq(name, "catch_ms2")) {
        out->reason = "nocatch";
        return true;
    }
    // The FPGA enumerates and clones a HID boot mouse only (descriptors.py
    // refuses a capture without bInterfaceProtocol == 2), so there is no
    // keyboard interface to inject into.
    if (streq(name, "press") || streq(name, "down") || streq(name, "up") ||
        streq(name, "string") || streq(name, "isdown") || streq(name, "disable") ||
        streq(name, "mask") || streq(name, "remap") || streq(name, "keyboard") ||
        streq(name, "init")) {
        out->reason = "nokeyboard";
        return true;
    }
    // Streaming telemetry back to the host would have to come from the FPGA,
    // which sends only MAP_STATUS, DESCRIPTOR_FRAGMENT and REPORT_FRAGMENT.
    if (streq(name, "buttons") || streq(name, "axis") || streq(name, "mouse") ||
        streq(name, "mo")) {
        out->reason = "nostream";
        return true;
    }
    // Device-level settings that belong to a MAKCU box, not to this adapter.
    if (streq(name, "baud") || streq(name, "bypass") || streq(name, "turbo") ||
        streq(name, "remap_button") || streq(name, "remap_axis") || streq(name, "invert_x") ||
        streq(name, "invert_y") || streq(name, "swap_xy") || streq(name, "led") ||
        streq(name, "serial") || streq(name, "log") || streq(name, "hs") ||
        streq(name, "release") || streq(name, "reboot") || streq(name, "fault") ||
        streq(name, "device") || streq(name, "info") || streq(name, "help")) {
        out->reason = "nodevice";
        return true;
    }
    return false;
}

static classified_t classify(const char *name)
{
    classified_t out = {CMD_UNKNOWN, 0u, NULL};

    if (streq(name, "move")) {
        out.kind = CMD_MOVE;
    } else if (streq(name, "left")) {
        out.kind = CMD_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_LEFT;
    } else if (streq(name, "right")) {
        out.kind = CMD_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_RIGHT;
    } else if (streq(name, "middle")) {
        out.kind = CMD_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_MIDDLE;
    } else if (streq(name, "side1")) {
        out.kind = CMD_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_SIDE1;
    } else if (streq(name, "side2")) {
        out.kind = CMD_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_SIDE2;
    } else if (streq(name, "click")) {
        out.kind = CMD_CLICK;
    } else if (streq(name, "wheel")) {
        out.kind = CMD_WHEEL;
    } else if (streq(name, "pan") || streq(name, "tilt")) {
        out.kind = CMD_PAN;
    } else if (streq(name, "lock_ml")) {
        out.kind = CMD_LOCK_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_LEFT;
    } else if (streq(name, "lock_mr")) {
        out.kind = CMD_LOCK_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_RIGHT;
    } else if (streq(name, "lock_mm")) {
        out.kind = CMD_LOCK_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_MIDDLE;
    } else if (streq(name, "lock_ms1")) {
        out.kind = CMD_LOCK_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_SIDE1;
    } else if (streq(name, "lock_ms2")) {
        out.kind = CMD_LOCK_BUTTON;
        out.button = (uint32_t)KMCMD_BUTTON_SIDE2;
    } else if (streq(name, "version")) {
        out.kind = CMD_VERSION;
    } else if (streq(name, "echo")) {
        out.kind = CMD_ECHO;
    } else if (classify_refusal(name, &out)) {
        out.kind = CMD_REFUSE;
    }
    return out;
}

// --- parsing ----------------------------------------------------------------

// Splits `line` into a name and the offset of its argument text. Returns false
// when the line is not shaped like a call at all. `had_prefix` records whether
// the caller wrote `km.` or a bare leading dot, which decides what an unknown
// name means: with a prefix it is a command for us that we do not support,
// without one it may be an ordinary console command.
static bool split_call(const char *line, char *name, bool *had_prefix, uint32_t *args_at)
{
    uint32_t i = 0u;

    while (is_space(line[i])) {
        i++;
    }

    *had_prefix = false;
    if (line[i] == 'k' && line[i + 1u] == 'm' && line[i + 2u] == '.') {
        i += 3u;
        *had_prefix = true;
    } else if (line[i] == '.') {
        i += 1u;
        *had_prefix = true;
    }

    uint32_t n = 0u;
    while (line[i] != '\0' && line[i] != '(') {
        const char c = line[i];
        // Lock targets carry a direction in the NAME (`lock_mx+`), so '+' and
        // '-' are name characters here, not operators.
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || is_digit(c) ||
                        c == '_' || c == '+' || c == '-';
        if (!ok) {
            return false;
        }
        if (n + 1u >= NAME_MAX) {
            return false;
        }
        name[n++] = c;
        i++;
    }
    name[n] = '\0';
    if (n == 0u || line[i] != '(') {
        return false;
    }
    *args_at = i + 1u;
    return true;
}

// Comma-separated signed decimals up to the closing paren. A trailing comma is
// tolerated because MAKCU's own documented sample is `.move(1,1,)`.
static bool parse_args(const char *text, int32_t *args, uint32_t *count)
{
    uint32_t i = 0u;
    *count = 0u;

    for (;;) {
        while (is_space(text[i])) {
            i++;
        }
        if (text[i] == ')') {
            return true;
        }
        if (text[i] == '\0') {
            return false;  // unterminated
        }

        bool negative = false;
        if (text[i] == '+' || text[i] == '-') {
            negative = (text[i] == '-');
            i++;
        }
        if (!is_digit(text[i])) {
            return false;
        }
        int64_t value = 0;
        while (is_digit(text[i])) {
            value = (value * 10) + (text[i] - '0');
            if (value > 2147483647) {
                value = 2147483647;  // saturate rather than wrap
            }
            i++;
        }
        if (*count >= ARGS_MAX) {
            return false;
        }
        args[(*count)++] = negative ? (int32_t)(-value) : (int32_t)value;

        while (is_space(text[i])) {
            i++;
        }
        if (text[i] == ',') {
            i++;
            continue;
        }
        if (text[i] == ')') {
            return true;
        }
        return false;
    }
}

// --- the sink ---------------------------------------------------------------

static bool sink_ready(void)
{
    // A NULL ready() is not "ready by default": it means nothing is wired.
    return s_ops.ready != NULL && s_ops.ready(s_ops.ctx);
}

// Whether a motion or button command can be honoured at all. Mirrors
// gateware.py's command_fresh, which gates every RELATIVE / BUTTON_STATE /
// PHYSICAL_MASK / CLEAR on link_ready & session_active & active_valid & a
// matching map generation. Refusing here is what makes that gate visible to a
// host; queueing instead would be motion the FPGA silently discards.
// Whether the ONE op this command needs is wired, not whether any op is.
// The distinction matters because kmcmd_step() bails per-op: a sink with
// buttons but no relative would otherwise ACK a move and then never drain the
// budget, reporting success for motion that can never be emitted.
static const char *sink_blocked(bool required_op_present)
{
    if (!required_op_present) {
        return "nosink";
    }
    if (!sink_ready()) {
        return "notready";
    }
    return NULL;
}

static int16_t take(int32_t *budget, int32_t cap)
{
    int32_t step = *budget;
    if (step > cap) {
        step = cap;
    } else if (step < -cap) {
        step = -cap;
    }
    *budget -= step;
    return (int16_t)step;
}

// --- command handlers -------------------------------------------------------

static void note_accepted(void)
{
    s_accepted++;
}

static void emit_ack(reply_t *reply, const char *name, const int32_t *args, uint32_t count)
{
    if (s_mode != KMCMD_MODE_MAKCU || s_echo_off) {
        // KMBox hosts are fire-and-forget and never drain a reply; MAKCU's
        // echo(0) asks for the same silence explicitly.
        return;
    }
    reply_put(reply, "km.");
    reply_put(reply, name);
    reply_put(reply, "(");
    for (uint32_t i = 0u; i < count; ++i) {
        if (i != 0u) {
            reply_put(reply, ",");
        }
        reply_i32(reply, args[i]);
    }
    reply_put(reply, ")\r\n>>>");
}

// Audible in every mode. Silence is only a correct answer for something that
// worked, and `echo(0)` asks to drop acknowledgements, not errors.
static void emit_refusal(reply_t *reply, const char *name, const char *reason)
{
    s_refused++;
    reply_put(reply, "km.");
    reply_put(reply, name);
    reply_put(reply, "(!");
    reply_put(reply, reason);
    reply_put(reply, ")");
    if (s_mode == KMCMD_MODE_MAKCU) {
        reply_put(reply, "\r\n>>>");
    }
}

static void handle_move(reply_t *reply, const char *name, const int32_t *args, uint32_t count)
{
    if (count < 2u) {
        emit_refusal(reply, name, "badargs");
        return;
    }
    const char *blocked = sink_blocked(s_ops.relative != NULL);
    if (blocked != NULL) {
        emit_refusal(reply, name, blocked);
        return;
    }

    s_pend_x += args[0];
    s_pend_y += args[1];

    // MAKCU's optional segment count. Honoured as a MINIMUM number of steps:
    // it may subdivide further than the field bound requires but never
    // coarsen past it, because the bound is what keeps a step from being
    // rejected on overflow. Bezier control points (args 3..6) are parsed and
    // discarded -- the displacement is exactly what was asked for, and the
    // shape of the path between two relative deltas is not observable by
    // anything downstream of here.
    int32_t cap = KMCMD_STEP_MAX;
    if (count >= 3u && args[2] > 1) {
        int32_t segments = args[2];
        if (segments > (int32_t)KMCMD_SEGMENTS_MAX) {
            segments = (int32_t)KMCMD_SEGMENTS_MAX;
        }
        int32_t widest = abs32(args[0]);
        if (abs32(args[1]) > widest) {
            widest = abs32(args[1]);
        }
        const int32_t per_segment = (widest + segments - 1) / segments;
        if (per_segment >= 1 && per_segment < cap) {
            cap = per_segment;
        }
    }
    s_step_cap = cap;
    note_accepted();
    emit_ack(reply, name, args, count);
}

static void handle_scroll(reply_t *reply, const char *name, const int32_t *args, uint32_t count,
                          bool is_pan)
{
    if (count < 1u) {
        // A bare getter. MAKCU's pan()/tilt() query a pending value; ours is
        // the budget still waiting to be spent.
        note_accepted();
        if (s_mode == KMCMD_MODE_MAKCU && !s_echo_off) {
            reply_put(reply, "km.");
            reply_put(reply, name);
            reply_put(reply, "(");
            reply_i32(reply, is_pan ? s_pend_pan : s_pend_wheel);
            reply_put(reply, ")\r\n>>>");
        }
        return;
    }
    const char *blocked = sink_blocked(s_ops.relative != NULL);
    if (blocked != NULL) {
        emit_refusal(reply, name, blocked);
        return;
    }
    if (is_pan) {
        s_pend_pan += args[0];
    } else {
        s_pend_wheel += args[0];
    }
    note_accepted();
    emit_ack(reply, name, args, count);
}

static void handle_button(reply_t *reply, const char *name, uint32_t bit, const int32_t *args,
                          uint32_t count)
{
    if (count < 1u) {
        // Bare getter. MAKCU answers with a lock indicator; we answer with what
        // we actually know, which is our own injected state, and emit nothing.
        note_accepted();
        if (s_mode == KMCMD_MODE_MAKCU && !s_echo_off) {
            reply_put(reply, "km.");
            reply_put(reply, name);
            reply_put(reply, "(");
            reply_i32(reply, ((s_buttons & bit) != 0u) ? 1 : 0);
            reply_put(reply, ")\r\n>>>");
        }
        return;
    }

    // MAKCU state 2 is "silent_release": zero the state without emitting a
    // frame, so the change rides out on the next real report instead of
    // provoking one.
    if (args[0] == 2) {
        s_buttons &= ~bit;
        note_accepted();
        emit_ack(reply, name, args, count);
        return;
    }
    if (args[0] != 0 && args[0] != 1) {
        emit_refusal(reply, name, "badstate");
        return;
    }

    const char *blocked = sink_blocked(s_ops.buttons != NULL);
    if (blocked != NULL) {
        emit_refusal(reply, name, blocked);
        return;
    }

    const uint32_t previous = s_buttons;
    if (args[0] == 1) {
        s_buttons |= bit;
    } else {
        s_buttons &= ~bit;
    }
    if (s_ops.buttons == NULL || !s_ops.buttons(s_ops.ctx, s_buttons, 0u)) {
        // Roll back rather than leave our idea of the mask ahead of the FPGA's.
        // A refusal here is the one-deep command queue being busy, which the
        // host can retry; silently diverging would make every later edit wrong.
        s_buttons = previous;
        s_dropped++;
        emit_refusal(reply, name, "busy");
        return;
    }
    note_accepted();
    emit_ack(reply, name, args, count);
}

static void handle_click(reply_t *reply, const char *name, const int32_t *args, uint32_t count)
{
    if (count < 1u) {
        emit_refusal(reply, name, "badargs");
        return;
    }
    if (args[0] < 1 || args[0] > 5) {
        emit_refusal(reply, name, "nobutton");
        return;
    }
    const uint32_t bit = 1u << (uint32_t)(args[0] - 1);
    uint32_t repeats = 1u;
    if (count >= 2u) {
        if (args[1] < 1) {
            emit_refusal(reply, name, "badargs");
            return;
        }
        repeats = (uint32_t)args[1];
    }

    uint32_t hold = 0u;
    if (count >= 3u) {
        if (args[2] < 0) {
            emit_refusal(reply, name, "badargs");
            return;
        }
        if (s_reports_per_ms == 0u) {
            // No declared report rate, so there is no honest conversion from
            // milliseconds to hold_reports. Refuse rather than invent one.
            emit_refusal(reply, name, "noclock");
            return;
        }
        if (repeats > 1u) {
            // A held click's release is timed by the engine's hold_reports, not
            // by us, so we cannot tell when one click ends and the next may
            // begin. Repeating them would merge presses at an interval this
            // module has no clock to measure.
            emit_refusal(reply, name, "noclock");
            return;
        }
        uint64_t reports = (uint64_t)args[2] * (uint64_t)s_reports_per_ms;
        if (reports > 65535u) {
            reports = 65535u;  // hold_reports is u16 on the wire; clamp, never wrap
        }
        hold = (uint32_t)reports;
    }

    const char *blocked = sink_blocked(s_ops.buttons != NULL);
    if (blocked != NULL) {
        emit_refusal(reply, name, blocked);
        return;
    }

    s_click_mask = bit;
    s_click_remaining = repeats;
    s_click_hold = hold;
    s_click_release_pending = false;
    note_accepted();
    emit_ack(reply, name, args, count);
}

static void handle_lock_button(reply_t *reply, const char *name, uint32_t bit, const int32_t *args,
                               uint32_t count)
{
    if (count < 1u) {
        note_accepted();
        if (s_mode == KMCMD_MODE_MAKCU && !s_echo_off) {
            reply_put(reply, "km.");
            reply_put(reply, name);
            reply_put(reply, "(");
            reply_i32(reply, ((s_physical & bit) != 0u) ? 1 : 0);
            reply_put(reply, ")\r\n>>>");
        }
        return;
    }
    if (args[0] != 0 && args[0] != 1) {
        emit_refusal(reply, name, "badstate");
        return;
    }
    const char *blocked = sink_blocked(s_ops.physical_mask != NULL);
    if (blocked != NULL) {
        emit_refusal(reply, name, blocked);
        return;
    }

    const uint32_t previous = s_physical;
    if (args[0] == 1) {
        s_physical |= bit;
    } else {
        s_physical &= ~bit;
    }
    if (s_ops.physical_mask == NULL || !s_ops.physical_mask(s_ops.ctx, s_physical)) {
        s_physical = previous;
        s_dropped++;
        emit_refusal(reply, name, "busy");
        return;
    }
    note_accepted();
    emit_ack(reply, name, args, count);
}

// --- entry points -----------------------------------------------------------

kmcmd_verdict_t kmcmd_line(const char *line, char *reply, uint32_t reply_size)
{
    reply_t out;
    reply_init(&out, reply, reply_size);

    if (s_mode == KMCMD_MODE_OFF) {
        return KMCMD_NOT_MINE;
    }

    char name[NAME_MAX];
    bool had_prefix = false;
    uint32_t args_at = 0u;
    if (!split_call(line, name, &had_prefix, &args_at)) {
        return KMCMD_NOT_MINE;
    }

    const classified_t command = classify(name);
    if (command.kind == CMD_UNKNOWN) {
        if (!had_prefix) {
            // Shaped like a call but not a name we know and not addressed to
            // us. The console's own dispatch gets it, so a future command
            // there is not shadowed by this parser.
            return KMCMD_NOT_MINE;
        }
        emit_refusal(&out, name, "unknown");
        return KMCMD_HANDLED;
    }
    if (command.kind == CMD_REFUSE) {
        emit_refusal(&out, name, command.reason);
        return KMCMD_HANDLED;
    }

    int32_t args[ARGS_MAX];
    uint32_t count = 0u;
    if (!parse_args(&line[args_at], args, &count)) {
        emit_refusal(&out, name, "badargs");
        return KMCMD_HANDLED;
    }

    switch (command.kind) {
        case CMD_MOVE:
            handle_move(&out, name, args, count);
            break;
        case CMD_BUTTON:
            handle_button(&out, name, command.button, args, count);
            break;
        case CMD_CLICK:
            handle_click(&out, name, args, count);
            break;
        case CMD_WHEEL:
            handle_scroll(&out, name, args, count, false);
            break;
        case CMD_PAN:
            handle_scroll(&out, name, args, count, true);
            break;
        case CMD_LOCK_BUTTON:
            handle_lock_button(&out, name, command.button, args, count);
            break;
        case CMD_VERSION:
            note_accepted();
            reply_put(&out, "km.version(hurra-mcxn947 kmcmd 1)");
            if (s_mode == KMCMD_MODE_MAKCU) {
                reply_put(&out, "\r\n>>>");
            }
            break;
        case CMD_ECHO:
            if (count >= 1u) {
                s_echo_off = (args[0] == 0);
            }
            note_accepted();
            // Reported before the new setting takes hold for an enable, and
            // suppressed by it for a disable -- which is what a host toggling
            // echo off expects to see (nothing).
            if (!s_echo_off && s_mode == KMCMD_MODE_MAKCU) {
                reply_put(&out, "km.echo(");
                reply_i32(&out, 1);
                reply_put(&out, ")\r\n>>>");
            }
            break;
        default:
            emit_refusal(&out, name, "unknown");
            break;
    }
    return KMCMD_HANDLED;
}

bool kmcmd_step(void)
{
    // Clicks first. A press/release pair is latency-sensitive in a way a
    // displacement is not: the move still lands wherever it lands, but a click
    // queued behind a long move would arrive long after the host asked for it.
    if (s_click_remaining != 0u || s_click_release_pending) {
        if (s_ops.buttons == NULL) {
            return false;
        }
        if (s_click_release_pending) {
            const uint32_t mask = s_buttons & ~s_click_mask;
            if (!s_ops.buttons(s_ops.ctx, mask, 0u)) {
                s_dropped++;
                return false;
            }
            s_buttons = mask;
            s_click_release_pending = false;
            return true;
        }
        const uint32_t mask = s_buttons | s_click_mask;
        if (!s_ops.buttons(s_ops.ctx, mask, s_click_hold)) {
            s_dropped++;
            return false;
        }
        s_buttons = mask;
        s_click_remaining--;
        // With an explicit hold the engine's hold_reports drives the release
        // (injection.py arms a click-release target from it), so queueing one
        // here would cut the press short. Without one we owe the release.
        s_click_release_pending = (s_click_hold == 0u);
        if (s_click_hold != 0u) {
            s_buttons &= ~s_click_mask;
        }
        return true;
    }

    if (s_pend_x == 0 && s_pend_y == 0 && s_pend_wheel == 0 && s_pend_pan == 0) {
        return false;
    }
    if (s_ops.relative == NULL) {
        return false;
    }

    int32_t cap = s_step_cap;
    if (cap < 1) {
        cap = KMCMD_STEP_MAX;
    }

    // Snapshot the budgets, because a refusing sink must leave every one of
    // them exactly as it found them -- the FPGA's command queue is one deep, so
    // "not now" is routine and must cost nothing.
    const int32_t saved_x = s_pend_x;
    const int32_t saved_y = s_pend_y;
    const int32_t saved_wheel = s_pend_wheel;
    const int32_t saved_pan = s_pend_pan;

    const int16_t dx = take(&s_pend_x, cap);
    const int16_t dy = take(&s_pend_y, cap);
    // One notch per step. MAKCU documents the wheel as clamped to a single step
    // per command because Windows rejects a multi-step scroll, so the clamp is
    // on the STEP; the remainder stays queued and the total a host asked for
    // still happens.
    const int16_t wheel = take(&s_pend_wheel, 1);
    const int16_t pan = take(&s_pend_pan, 1);

    if (!s_ops.relative(s_ops.ctx, dx, dy, wheel, pan)) {
        s_pend_x = saved_x;
        s_pend_y = saved_y;
        s_pend_wheel = saved_wheel;
        s_pend_pan = saved_pan;
        s_dropped++;
        return false;
    }
    return true;
}

bool kmcmd_pending(void)
{
    return s_pend_x != 0 || s_pend_y != 0 || s_pend_wheel != 0 || s_pend_pan != 0 ||
           s_click_remaining != 0u || s_click_release_pending;
}

uint32_t kmcmd_pending_counts(void)
{
    const int32_t axes[4] = {s_pend_x, s_pend_y, s_pend_wheel, s_pend_pan};
    uint32_t total = 0u;

    for (uint32_t index = 0u; index < 4u; ++index) {
        const int32_t value = axes[index];
        total += (uint32_t)(value < 0 ? -value : value);
    }
    return total;
}

void kmcmd_set_link(bool up)
{
    if (up) {
        return;
    }
    // The FPGA drops session_active and its RX sequence window with the link,
    // so every queued count and every mask we think we set is void. Replaying
    // them into a fresh session would be a stale move and a stuck button.
    s_pend_x = 0;
    s_pend_y = 0;
    s_pend_wheel = 0;
    s_pend_pan = 0;
    s_click_mask = 0u;
    s_click_remaining = 0u;
    s_click_hold = 0u;
    s_click_release_pending = false;
    s_buttons = 0u;
    s_physical = 0u;
}

void kmcmd_set_mode(kmcmd_mode_t mode)
{
    s_mode = mode;
}

kmcmd_mode_t kmcmd_mode(void)
{
    return s_mode;
}

void kmcmd_set_reports_per_ms(uint32_t reports_per_ms)
{
    s_reports_per_ms = reports_per_ms;
}

void kmcmd_init(const kmcmd_ops_t *ops)
{
    s_ops = *ops;
    s_mode = KMCMD_MODE_OFF;
    s_echo_off = false;
    // 8 kHz High Speed, the rate the injection plane is designed around. An
    // integrator on a Full Speed link should set 1.
    s_reports_per_ms = 8u;
    s_buttons = 0u;
    s_physical = 0u;
    s_pend_x = 0;
    s_pend_y = 0;
    s_pend_wheel = 0;
    s_pend_pan = 0;
    s_step_cap = KMCMD_STEP_MAX;
    s_click_mask = 0u;
    s_click_remaining = 0u;
    s_click_hold = 0u;
    s_click_release_pending = false;
    s_accepted = 0u;
    s_refused = 0u;
    s_dropped = 0u;
}

uint32_t kmcmd_accepted(void)
{
    return s_accepted;
}

uint32_t kmcmd_refused(void)
{
    return s_refused;
}

uint32_t kmcmd_dropped(void)
{
    return s_dropped;
}
