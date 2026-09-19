// Page composition and rasterization stay portable. The one trailing target
// guard owns only the panel-backed asynchronous pipeline.

#include "display.h"

#include "glyphs.h"
#include "hid_report_decoder.h"
#include "injection_wire.h"
// For the verdict and pin NAMES only. display.h mirrors the cross-core block
// as plain bytes so this file still never learns that shared memory exists;
// what it needs from the classifier is the spelling of a pin, not its layout.
#include "link_fault_classify.h"

static char display_hex_digit(uint8_t value)
{
    return value < 10u ? (char)('0' + value)
                       : (char)('A' + (uint8_t)(value - 10u));
}

static void display_put_hex32(text_grid_t *grid, uint8_t row, uint8_t col,
                              uint32_t value, uint8_t digits, uint8_t attr)
{
    char text[11] = {'0', 'x', '0', '0', '0', '0', '0', '0', '0', '0', '\0'};
    for (uint8_t i = 0u; i < digits; ++i) {
        const uint8_t shift = (uint8_t)((digits - i - 1u) * 4u);
        text[2u + i] = display_hex_digit((uint8_t)((value >> shift) & 0x0fu));
    }
    text[2u + digits] = '\0';
    (void)text_grid_put(grid, row, col, text, attr);
}

// Right-aligned unsigned decimal in `width` columns, space padded. Decimal
// because these are quantities a person reads, not register images.
static void display_put_dec(text_grid_t *grid, uint8_t row, uint8_t col,
                            uint32_t value, uint8_t width, uint8_t attr)
{
    char text[11];
    uint8_t digits = 0u;
    do {
        text[digits] = (char)('0' + (value % 10u));
        value /= 10u;
        digits++;
    } while (value != 0u && digits < (uint8_t)sizeof(text));

    char out[12];
    uint8_t pad = (width > digits) ? (uint8_t)(width - digits) : 0u;
    uint8_t n = 0u;
    while (n < pad && n < (uint8_t)(sizeof(out) - 1u)) {
        out[n] = ' ';
        n++;
    }
    while (digits > 0u && n < (uint8_t)(sizeof(out) - 1u)) {
        digits--;
        out[n] = text[digits];
        n++;
    }
    out[n] = '\0';
    (void)text_grid_put(grid, row, col, out, attr);
}

// Reports per second, derived from the slot counter rather than a clock.
//
// CPU1 has no time base -- no SysTick, no timer, and its loop is not paced. But
// the FPGA link delivers exactly one slot every 125 us, so `slot_counter` IS a
// clock at DISPLAY_SLOT_HZ. A ratio against it converts to real time:
//
//     reports/s = delta_reports * DISPLAY_SLOT_HZ / delta_slots
//
// Note this is the only rate that can honestly be shown. "Slots per second"
// derived the same way is tautologically DISPLAY_SLOT_HZ and would be a fake
// number, so link liveness is reported as advancing-or-stalled instead.
//
// Returns 0 when no slots have elapsed, which is also the stalled case.
uint32_t display_report_rate(uint32_t delta_reports, uint32_t delta_slots)
{
    if (delta_slots == 0u) {
        return 0u;
    }
    const uint64_t scaled =
        (uint64_t)delta_reports * (uint64_t)DISPLAY_SLOT_HZ / (uint64_t)delta_slots;
    return (scaled > 0xffffffffu) ? 0xffffffffu : (uint32_t)scaled;
}

static void display_page_row(text_grid_t *grid, uint8_t row,
                             const char *label)
{
    text_grid_fill_row(grid, row, ' ', DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, row, 1u, label, DISPLAY_ATTR_NORMAL);
}

void display_attr_rgb565(uint8_t attr, uint16_t *foreground,
                         uint16_t *background)
{
    *background = 0x0000u;
    switch (attr) {
    case DISPLAY_ATTR_HEADER:
        *foreground = 0xffffu;
        *background = 0x001fu;
        break;
    case DISPLAY_ATTR_SLOT:
        *foreground = 0xffe0u;
        break;
    case DISPLAY_ATTR_GOOD:
        *foreground = 0x07e0u;
        break;
    case DISPLAY_ATTR_FAULT:
        *foreground = 0xf800u;
        break;
    default:
        *foreground = 0xc618u;
        break;
    }
}

// Two columns, split at DISPLAY_SPLIT_COL.
//
// Left is for a person: decimal quantities, decoded states, and colour that
// answers "is it working" at a glance. Right is the raw register image in hex,
// which is what you want when the answer is "no". Neither view is derived from
// the other -- the left side can be read without trusting the decode, and the
// right side is the wire contract verbatim.
// Collect decoded lines into the bottom pane. hid_report_decode() hands each
// line to a callback and the line is only valid for the duration of that call,
// so this writes straight into the grid instead of buffering: CPU1 has a 2 KB
// stack (0x800) and DISPLAY_DESC_LINES x 31 bytes would not fit on it.
typedef struct {
    text_grid_t *grid;
    uint32_t emitted;  // lines seen, including ones past the pane
} descriptor_sink_t;

static void descriptor_line(void *context, const char *line)
{
    descriptor_sink_t *const sink = (descriptor_sink_t *)context;
    const uint32_t index = sink->emitted;
    sink->emitted++;

    if (index >= DISPLAY_DESC_LINES) {
        // Keep counting: the header reports the total so truncation is visible
        // rather than silent. A panel that quietly shows the first ten items of
        // a descriptor looks exactly like a descriptor with ten items.
        return;
    }

    const uint8_t row = (uint8_t)(DISPLAY_DESC_FIRST_ROW + (index / 2u));
    const uint8_t column = ((index % 2u) == 0u) ? DISPLAY_DESC_COL_A : DISPLAY_DESC_COL_B;
    (void)text_grid_put(sink->grid, row, column, line, DISPLAY_ATTR_NORMAL);
}

// A fault is worth showing only once the classifier has actually run.
// sequence == 0 is "not yet classified", which is neither a fault nor a clean
// bill of health, and OK is a link that works -- both leave the pane alone.
static bool display_fault_active(const display_fault_t *fault)
{
    return fault != NULL && fault->sequence != 0u &&
           fault->verdict != (uint8_t)LINK_FAULT_OK;
}

// The fault pane, rendered over the descriptor rows. See display.h: the two
// cannot collide, because a descriptor only ever arrives over a link that is
// working, and this pane only ever appears when one is not.
//
// One suspect per row with its physical location beside it. The location is
// the half that is actually actionable at the bench, so it is never elided
// even when only one pin is named.
static void display_compose_fault(text_grid_t *grid, const display_fault_t *fault)
{
    for (uint8_t row = DISPLAY_DESC_HEADER_ROW; row <= DISPLAY_DESC_LAST_ROW; row++) {
        text_grid_fill_row(grid, row, ' ', DISPLAY_ATTR_NORMAL);
    }

    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 1u, "LINK FAULT",
                        DISPLAY_ATTR_FAULT);
    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 13u,
                        link_fault_verdict_name((link_fault_verdict_t)fault->verdict),
                        DISPLAY_ATTR_FAULT);

    uint8_t row = DISPLAY_DESC_FIRST_ROW;
    for (uint8_t index = 0u;
         index < fault->suspect_count && row <= DISPLAY_DESC_LAST_ROW;
         index++, row++) {
        const link_pin_t pin = (link_pin_t)fault->suspect[index];
        (void)text_grid_put(grid, row, 1u, link_pin_name(pin), DISPLAY_ATTR_NORMAL);
        (void)text_grid_put(grid, row, 12u, link_pin_location(pin), DISPLAY_ATTR_SLOT);
    }
}

static void display_compose_descriptor(text_grid_t *grid,
                                       const display_descriptor_t *descriptor)
{
    for (uint8_t row = DISPLAY_DESC_HEADER_ROW; row <= DISPLAY_DESC_LAST_ROW; row++) {
        text_grid_fill_row(grid, row, ' ', DISPLAY_ATTR_NORMAL);
    }

    if (descriptor == NULL || descriptor->bytes == NULL || descriptor->length == 0u) {
        (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 1u,
                            "HID DESCRIPTOR   (none captured)", DISPLAY_ATTR_NORMAL);
        return;
    }

    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 1u, "HID DESCRIPTOR",
                        DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 17u, "if", DISPLAY_ATTR_NORMAL);
    display_put_dec(grid, DISPLAY_DESC_HEADER_ROW, 20u, descriptor->interface_number, 1u,
                    DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 22u, "gen", DISPLAY_ATTR_NORMAL);
    display_put_dec(grid, DISPLAY_DESC_HEADER_ROW, 26u, descriptor->generation, 3u,
                    DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 30u, "len", DISPLAY_ATTR_NORMAL);
    display_put_dec(grid, DISPLAY_DESC_HEADER_ROW, 34u, descriptor->length, 4u,
                    DISPLAY_ATTR_NORMAL);

    descriptor_sink_t sink = {.grid = grid, .emitted = 0u};
    (void)hid_report_decode(descriptor->bytes, descriptor->length, descriptor_line, &sink);

    if (sink.emitted > DISPLAY_DESC_LINES) {
        (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 44u, "+", DISPLAY_ATTR_SLOT);
        display_put_dec(grid, DISPLAY_DESC_HEADER_ROW, 45u,
                        sink.emitted - DISPLAY_DESC_LINES, 3u, DISPLAY_ATTR_SLOT);
        (void)text_grid_put(grid, DISPLAY_DESC_HEADER_ROW, 49u, "more", DISPLAY_ATTR_SLOT);
    }
}

void display_compose_page(text_grid_t *grid, const link_snapshot_t *snapshot,
                          uint32_t cpu1_heartbeat, uint32_t reports_per_sec,
                          bool link_alive, const display_descriptor_t *descriptor,
                          const display_fault_t *fault)
{
    text_grid_fill_row(grid, 0u, ' ', DISPLAY_ATTR_HEADER);
    (void)text_grid_put(grid, 0u, 2u, "HURRA  MCXN947 LINK",
                        DISPLAY_ATTR_HEADER);

    const bool ready =
        (snapshot->link_flags & INJ_LINK_STATUS_FLAG_RELAY_READY) != 0u;
    const bool healthy = ready && link_alive && snapshot->fault_flags == 0u;
    (void)text_grid_put(grid, 0u, 45u, healthy ? "LINK OK  " : "LINK FAULT",
                        DISPLAY_ATTR_HEADER);

    (void)text_grid_put(grid, 2u, 1u, "HEALTH", DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, 2u, DISPLAY_REG_LABEL_COL, "REGISTERS",
                        DISPLAY_ATTR_NORMAL);

    // ---- left: health -------------------------------------------------
    display_page_row(grid, 3u, "Link");
    (void)text_grid_put(grid, 3u, DISPLAY_HEALTH_VALUE_COL,
                        ready ? "READY" : "DOWN ",
                        ready ? DISPLAY_ATTR_GOOD : DISPLAY_ATTR_FAULT);

    display_page_row(grid, 4u, "Faults");
    if (snapshot->fault_flags == 0u) {
        (void)text_grid_put(grid, 4u, DISPLAY_HEALTH_VALUE_COL, "NONE  ",
                            DISPLAY_ATTR_GOOD);
    } else {
        display_put_hex32(grid, 4u, DISPLAY_HEALTH_VALUE_COL,
                          snapshot->fault_flags, 4u, DISPLAY_ATTR_FAULT);
    }

    display_page_row(grid, 5u, "Reports");
    display_put_dec(grid, 5u, DISPLAY_HEALTH_VALUE_COL,
                    snapshot->native_report_count, 10u, DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 6u, "Rate");
    display_put_dec(grid, 6u, DISPLAY_HEALTH_VALUE_COL, reports_per_sec, 6u,
                    DISPLAY_ATTR_SLOT);
    (void)text_grid_put(grid, 6u, (uint8_t)(DISPLAY_HEALTH_VALUE_COL + 7u),
                        "/s", DISPLAY_ATTR_SLOT);

    // Frame phase is the 125 us alignment the whole injection design turns on.
    display_page_row(grid, 7u, "Frame");
    display_put_dec(grid, 7u, DISPLAY_HEALTH_VALUE_COL, snapshot->usb_frame, 5u,
                    DISPLAY_ATTR_NORMAL);
    (void)text_grid_put(grid, 7u, (uint8_t)(DISPLAY_HEALTH_VALUE_COL + 5u), ".",
                        DISPLAY_ATTR_NORMAL);
    display_put_dec(grid, 7u, (uint8_t)(DISPLAY_HEALTH_VALUE_COL + 6u),
                    snapshot->usb_subframe, 1u, DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 8u, "Desc gen");
    display_put_dec(grid, 8u, DISPLAY_HEALTH_VALUE_COL,
                    snapshot->descriptor_generation, 5u, DISPLAY_ATTR_NORMAL);

    display_page_row(grid, 9u, "Map gen");
    display_put_dec(grid, 9u, DISPLAY_HEALTH_VALUE_COL,
                    snapshot->map_generation, 5u, DISPLAY_ATTR_NORMAL);

    // Advancing slots, not a rate: see display_report_rate().
    display_page_row(grid, 10u, "Activity");
    (void)text_grid_put(grid, 10u, DISPLAY_HEALTH_VALUE_COL,
                        link_alive ? "ALIVE  " : "STALLED",
                        link_alive ? DISPLAY_ATTR_GOOD : DISPLAY_ATTR_FAULT);

    // Column rule, so the two halves read as separate panes.
    for (uint8_t row = 2u; row <= 13u; row++) {
        (void)text_grid_set_cell(grid, row, DISPLAY_SPLIT_COL, '|',
                                 DISPLAY_ATTR_NORMAL);
    }

    // ---- right: raw registers -----------------------------------------
    (void)text_grid_put(grid, 3u, DISPLAY_REG_LABEL_COL, "slot",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 3u, DISPLAY_REG_VALUE_COL, snapshot->slot_counter,
                      8u, DISPLAY_ATTR_SLOT);

    (void)text_grid_put(grid, 4u, DISPLAY_REG_LABEL_COL, "seq",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 4u, DISPLAY_REG_VALUE_COL, snapshot->seq, 8u,
                      DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 5u, DISPLAY_REG_LABEL_COL, "native",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 5u, DISPLAY_REG_VALUE_COL,
                      snapshot->native_report_count, 8u, DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 6u, DISPLAY_REG_LABEL_COL, "flags",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 6u, DISPLAY_REG_VALUE_COL, snapshot->link_flags, 4u,
                      DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 7u, DISPLAY_REG_LABEL_COL, "fault",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 7u, DISPLAY_REG_VALUE_COL, snapshot->fault_flags,
                      4u,
                      snapshot->fault_flags == 0u ? DISPLAY_ATTR_GOOD
                                                  : DISPLAY_ATTR_FAULT);

    (void)text_grid_put(grid, 8u, DISPLAY_REG_LABEL_COL, "rxseq",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 8u, DISPLAY_REG_VALUE_COL,
                      snapshot->last_rx_sequence, 2u, DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 9u, DISPLAY_REG_LABEL_COL, "frame",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 9u, DISPLAY_REG_VALUE_COL, snapshot->usb_frame, 4u,
                      DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 10u, DISPLAY_REG_LABEL_COL, "subfrm",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 10u, DISPLAY_REG_VALUE_COL, snapshot->usb_subframe,
                      4u, DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 11u, DISPLAY_REG_LABEL_COL, "dgen",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 11u, DISPLAY_REG_VALUE_COL,
                      snapshot->descriptor_generation, 4u,
                      DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 12u, DISPLAY_REG_LABEL_COL, "mgen",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 12u, DISPLAY_REG_VALUE_COL,
                      snapshot->map_generation, 4u, DISPLAY_ATTR_NORMAL);

    (void)text_grid_put(grid, 13u, DISPLAY_REG_LABEL_COL, "hbeat",
                        DISPLAY_ATTR_NORMAL);
    display_put_hex32(grid, 13u, DISPLAY_REG_VALUE_COL, cpu1_heartbeat, 8u,
                      DISPLAY_ATTR_NORMAL);

    // ---- bottom: the decoded HID report descriptor --------------------
    // The fault pane wins the rows when there is a fault, and it costs the
    // descriptor nothing to give them up: a link that cannot frame a slot has
    // not delivered a descriptor either, so the pane it takes is already blank.
    if (display_fault_active(fault)) {
        display_compose_fault(grid, fault);
    } else {
        display_compose_descriptor(grid, descriptor);
    }
}

bool display_rasterize_run(const text_grid_t *grid,
                           const text_grid_run_t *run, uint16_t *pixels,
                           size_t pixel_capacity)
{
    if (run->row >= TEXT_GRID_ROWS || run->length == 0u ||
        run->start_col >= TEXT_GRID_COLS ||
        (uint16_t)run->start_col + (uint16_t)run->length > TEXT_GRID_COLS) {
        return false;
    }

    const size_t row_pixels = (size_t)run->length * GLYPH_WIDTH;
    const size_t required = row_pixels * GLYPH_HEIGHT;
    if (pixels == NULL || pixel_capacity < required) {
        return false;
    }

    for (uint8_t glyph_row = 0u; glyph_row < GLYPH_HEIGHT; ++glyph_row) {
        size_t output = (size_t)glyph_row * row_pixels;
        for (uint8_t offset = 0u; offset < run->length; ++offset) {
            const uint8_t col = (uint8_t)(run->start_col + offset);
            const text_grid_cell_t *const cell =
                text_grid_cell_at(grid, run->row, col);
            const uint8_t *const glyph = glyphs_for_char(cell->character);
            uint16_t foreground;
            uint16_t background;
            display_attr_rgb565(cell->attr, &foreground, &background);

            for (uint8_t x = 0u; x < GLYPH_WIDTH; ++x) {
                pixels[output++] =
                    (glyph[glyph_row] & (uint8_t)(0x80u >> x)) != 0u
                        ? foreground
                        : background;
            }
        }
    }
    return true;
}

#if defined(MCXN947)

#include "display_panel.h"
#include "shared_window.h"

// For __DMB() around the descriptor seqlock read. link_snapshot.c defines a
// fallback instead, because it is portable and host-compiled; this is inside
// the target guard, so it can just have the real CMSIS intrinsic.
#include "fsl_device_registers.h"

#define DISPLAY_ROW_BUFFER_PIXELS \
    (DISPLAY_PANEL_WIDTH * GLYPH_HEIGHT)

_Static_assert(TEXT_GRID_COLS * GLYPH_WIDTH == DISPLAY_PANEL_WIDTH,
               "text columns must cover the panel width");
_Static_assert(TEXT_GRID_ROWS * GLYPH_HEIGHT == DISPLAY_PANEL_HEIGHT,
               "text rows must cover the panel height");

typedef struct {
    text_grid_t grid;
    _Alignas(32) uint16_t row_buffers[2][DISPLAY_ROW_BUFFER_PIXELS];
    text_grid_run_t staged_run;
    uint8_t staged_buffer;
    uint8_t write_buffer;
    bool staged;
    bool active;
    bool first_frame;
    bool available;

    // Baseline for the derived report rate and liveness. CPU1 has no clock, so
    // the slot counter is the time base -- see display_report_rate().
    uint32_t prev_slots;
    uint32_t prev_reports;

    // CPU1's private copy of the published descriptor, and the view of it handed
    // to the pure composer. 2 KB of core1's 104 KB, and it buys the decoder a
    // stable buffer that CPU0 cannot rewrite underneath it.
    display_descriptor_t descriptor;
    // Zero-initialised with the rest of s_display, so sequence starts at 0 --
    // "not yet classified" -- and the pane stays the descriptor's until CPU0
    // publishes a first verdict.
    display_fault_t fault;
    uint8_t descriptor_bytes[SHARED_DESCRIPTOR_CAPACITY];
} display_state_t;

static display_state_t s_display;

static bool display_prepare_next(void)
{
    if (s_display.staged) {
        return true;
    }
    if (!text_grid_take_dirty_run(&s_display.grid,
                                  &s_display.staged_run)) {
        return false;
    }

    s_display.staged_buffer = s_display.write_buffer;
    if (!display_rasterize_run(
            &s_display.grid, &s_display.staged_run,
            s_display.row_buffers[s_display.staged_buffer],
            DISPLAY_ROW_BUFFER_PIXELS)) {
        s_display.active = false;
        return false;
    }
    s_display.staged = true;
    return true;
}

static void display_kick_prepared(void)
{
    if (!s_display.staged || display_panel_blit_busy()) {
        return;
    }

    const uint16_t x =
        (uint16_t)((uint16_t)s_display.staged_run.start_col * GLYPH_WIDTH);
    const uint16_t y =
        (uint16_t)((uint16_t)s_display.staged_run.row * GLYPH_HEIGHT);
    const uint16_t width =
        (uint16_t)((uint16_t)s_display.staged_run.length * GLYPH_WIDTH);
    if (!display_panel_blit(
            x, y, width, GLYPH_HEIGHT,
            s_display.row_buffers[s_display.staged_buffer])) {
        s_display.active = false;
        s_display.staged = false;
        return;
    }

    s_display.staged = false;
    s_display.write_buffer ^= 1u;
    // The just-started eDMA owns the other buffer. Rasterizing one following
    // run here is the ping-pong overlap required by section 2.
    (void)display_prepare_next();
}

// Bring-up aid, deliberately kept. Paints the whole panel one colour through
// exactly the same SelectArea + WritePixels path a real run uses.
//
// It exists because "blits succeed and the screen is black" is not a
// diagnosis: it is consistent with a dead bus AND with a renderer that emits
// nothing visible, and those have completely different fixes. A solid fill
// separates them in one look at the board -- colour means the transport works
// and the bug is above it. Every other check (pins, mux alternates, shifter
// and timer indices, baud divider, callback wiring) was read against NXP's own
// LVGL support file for this exact panel and matched, which is precisely why
// reading more code was not going to settle it.
static bool display_fill_screen(uint16_t rgb565)
{
    for (uint32_t i = 0u; i < DISPLAY_ROW_BUFFER_PIXELS; ++i) {
        s_display.row_buffers[0][i] = rgb565;
    }

    const uint16_t band = (uint16_t)(DISPLAY_ROW_BUFFER_PIXELS / DISPLAY_PANEL_WIDTH);
    for (uint16_t y = 0u; y < DISPLAY_PANEL_HEIGHT; y = (uint16_t)(y + band)) {
        uint16_t height = band;
        if ((uint32_t)y + height > DISPLAY_PANEL_HEIGHT) {
            height = (uint16_t)(DISPLAY_PANEL_HEIGHT - y);
        }
        if (!display_panel_blit(0u, y, DISPLAY_PANEL_WIDTH, height,
                                s_display.row_buffers[0])) {
            return false;
        }
        while (display_panel_blit_busy()) {
        }
    }
    return true;
}

bool display_init(void)
{
    text_grid_init(&s_display.grid, ' ', DISPLAY_ATTR_NORMAL);
    s_display.staged = false;
    s_display.active = false;
    s_display.first_frame = true;
    s_display.write_buffer = 0u;
    s_display.prev_slots = 0u;
    s_display.prev_reports = 0u;
    s_display.available = display_panel_init();

    // Paint the panel solid before any text. Two jobs: it proves the transport
    // reaches the glass (see display_fill_screen), and it clears the ST7796S's
    // power-on GRAM, which is undefined -- NXP's own bring-up clears video RAM
    // between ST7796S_Init and EnableDisplay for the same reason.
    if (s_display.available) {
        (void)display_fill_screen(DISPLAY_FILL_ON_INIT);
    }
    return s_display.available;
}

bool display_start_frame(const link_snapshot_t *snapshot,
                         uint32_t cpu1_heartbeat)
{
    if (!s_display.available || !display_panel_ok() || s_display.active) {
        return false;
    }

    // Rate and liveness both come from how far the slot counter moved since the
    // last frame; CPU1 has no other clock. First frame has no baseline, so it
    // reports 0/s and STALLED for one repaint rather than inventing a number.
    const uint32_t delta_slots = snapshot->slot_counter - s_display.prev_slots;
    const uint32_t delta_reports =
        snapshot->native_report_count - s_display.prev_reports;
    const uint32_t rate = s_display.first_frame
                              ? 0u
                              : display_report_rate(delta_reports, delta_slots);
    const bool alive = !s_display.first_frame && delta_slots != 0u;
    s_display.prev_slots = snapshot->slot_counter;
    s_display.prev_reports = snapshot->native_report_count;

    // Snapshot the shared descriptor into a CPU1-private copy, and only when it
    // has actually changed. The copy exists because decoding straight out of
    // shared memory could race a re-publish and put bytes from two devices
    // through the decoder; the change check exists because copying 2 KB at
    // 20 Hz forever to re-decode an unchanged descriptor is pure waste on the
    // core that also drives the panel.
    //
    // Seqlock, same discipline as link_snapshot_read: an odd sequence means a
    // publish is in flight, and a sequence that moved across the copy means the
    // bytes may be spliced. Either way, keep the previous copy and try again
    // next frame -- a descriptor arrives once per enumeration, so there is
    // always a next frame.
    const uint32_t descriptor_seq = g_shared_window.descriptor.sequence;
    if ((descriptor_seq & 1u) == 0u) {
        // Barrier before ANY payload read, including the generation/length
        // pair the skip test below uses -- those are payload too. Without it
        // the reads may be hoisted above the sequence load, and the final
        // comparison would then vouch for bytes fetched before it.
        __DMB();
        const uint16_t generation = g_shared_window.descriptor.generation;
        const uint16_t length = g_shared_window.descriptor.length;

        // A 2 KB copy every frame for a descriptor that changes once per
        // enumeration; the compare keeps it to the frames that need it.
        if (generation != s_display.descriptor.generation ||
            length != s_display.descriptor.length) {
            const uint16_t capped =
                (length > SHARED_DESCRIPTOR_CAPACITY) ? SHARED_DESCRIPTOR_CAPACITY : length;
            for (uint16_t i = 0u; i < capped; i++) {
                s_display.descriptor_bytes[i] = g_shared_window.descriptor.data[i];
            }
            const uint8_t interface_number = g_shared_window.descriptor.interface_number;
            __DMB();
            if (g_shared_window.descriptor.sequence == descriptor_seq) {
                s_display.descriptor.bytes = s_display.descriptor_bytes;
                s_display.descriptor.length = capped;
                s_display.descriptor.generation = generation;
                s_display.descriptor.interface_number = interface_number;
            }
        }
    }

    // Same seqlock discipline, and the whole block is eight bytes so the copy
    // is trivially short. A verdict republished every 50 ms needs no retry
    // beyond "keep last and try next frame": the next one is one frame away.
    const uint32_t fault_seq = g_shared_window.fault.sequence;
    if ((fault_seq & 1u) == 0u) {
        __DMB();
        display_fault_t pending;
        pending.sequence = fault_seq;
        pending.verdict = g_shared_window.fault.verdict;
        pending.suspect_count = g_shared_window.fault.suspect_count;
        for (uint32_t i = 0u; i < SHARED_FAULT_MAX_SUSPECTS; i++) {
            pending.suspect[i] = g_shared_window.fault.suspect[i];
        }
        __DMB();
        if (g_shared_window.fault.sequence == fault_seq) {
            s_display.fault = pending;
        }
    }

    display_compose_page(&s_display.grid, snapshot, cpu1_heartbeat, rate, alive,
                         &s_display.descriptor, &s_display.fault);
    if (s_display.first_frame) {
        text_grid_mark_all_dirty(&s_display.grid);
        s_display.first_frame = false;
    }

    s_display.active = true;
    if (!display_prepare_next()) {
        s_display.active = false;
        return true;
    }
    display_kick_prepared();
    return s_display.active;
}

void display_poll(void)
{
    if (!s_display.active) {
        return;
    }
    if (!display_panel_ok()) {
        s_display.available = false;
        s_display.active = false;
        s_display.staged = false;
        return;
    }
    if (display_panel_blit_busy()) {
        return;
    }
    if (!s_display.staged && !display_prepare_next()) {
        s_display.active = false;
        return;
    }
    display_kick_prepared();
}

bool display_available(void)
{
    return s_display.available && display_panel_ok();
}

bool display_render_active(void)
{
    return s_display.active;
}

#endif  // MCXN947
