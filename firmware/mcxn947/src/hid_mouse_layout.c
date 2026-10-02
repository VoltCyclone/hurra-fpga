// See hid_mouse_layout.h.

#include "hid_mouse_layout.h"

#include <stdbool.h>
#include <string.h>

#include "hid_fields.h"
#include "injection_wire.h"

#define PAGE_GENERIC_DESKTOP 0x01u
#define PAGE_BUTTON 0x09u
#define PAGE_CONSUMER 0x0Cu
#define USAGE_MOUSE 0x02u
#define USAGE_X 0x30u
#define USAGE_Y 0x31u
#define USAGE_WHEEL 0x38u
#define USAGE_AC_PAN 0x0238u

// The FPGA maps injected BUTTON_STATE bit n to button n+1 for n < 64, and one
// entry spans at most 32 bits.
#define MAX_BUTTON_NUMBER 64u
#define MAX_ENTRY_BITS 32u

// Enough for every axis and button run of the handful of reports a real mouse
// declares (adjacent buttons merge as they are collected, so a run of listed
// buttons is one candidate); anything past it is dropped, never an error.
#define MAX_CANDIDATES 16u

// A range run labels element i with usage + i; bound how far that is followed
// when looking for axes (X..Wheel spans 9 usages).
#define MAX_AXIS_RUN 16u

typedef struct {
    uint8_t report_id;
    hid_mouse_field_t field;
} candidate_t;

typedef struct {
    candidate_t items[MAX_CANDIDATES];
    uint8_t count;
    bool saw_mouse;
} workspace_t;

// Static, not on the stack: core0's whole stack is 2 KiB.
static hid_fields_walker_t s_walker;
static workspace_t s_work;

static int axis_kind(uint16_t page, uint16_t usage)
{
    if (page == PAGE_GENERIC_DESKTOP) {
        switch (usage) {
        case USAGE_X:
            return HID_MOUSE_X;
        case USAGE_Y:
            return HID_MOUSE_Y;
        case USAGE_WHEEL:
            return HID_MOUSE_WHEEL;
        default:
            return -1;
        }
    }
    if (page == PAGE_CONSUMER && usage == USAGE_AC_PAN) {
        return HID_MOUSE_PAN;
    }
    return -1;
}

static bool range_holds_a_step(int kind, int32_t minimum, int32_t maximum)
{
    const int32_t span = (kind == HID_MOUSE_X || kind == HID_MOUSE_Y)
                             ? HID_MOUSE_XY_MINIMUM_SPAN
                             : HID_MOUSE_WHEEL_MINIMUM_SPAN;
    return minimum <= -span && maximum >= span;
}

static void add_candidate(workspace_t *w, uint8_t report_id, const hid_mouse_field_t *field)
{
    if (w->count < MAX_CANDIDATES) {
        w->items[w->count].report_id = report_id;
        w->items[w->count].field = *field;
        w->count++;
    }
}

static void collect_buttons(workspace_t *w, const hid_field_t *f)
{
    // A range names every element. A run sharing one usage is either a single
    // listed button or the tail of a short usage list; the tail "shares the
    // last usage" (HID 1.11 6.2.2.8), but mapping it would make one injected
    // button drive several bits, so only a single element is taken.
    if (!f->usage_is_range && f->count != 1u) {
        return;
    }
    uint32_t count = f->count;
    const uint32_t first = f->usage;
    if (first == 0u || first > MAX_BUTTON_NUMBER) {
        return;
    }
    if (count > MAX_ENTRY_BITS) {
        count = MAX_ENTRY_BITS;
    }
    if (first + count - 1u > MAX_BUTTON_NUMBER) {
        count = MAX_BUTTON_NUMBER - first + 1u;
    }

    // Buttons declared one usage at a time arrive as adjacent width-1 runs.
    // Fold each into the previous run when both its bits and its button number
    // follow on -- the shape the FPGA's button entries take (usage U, width N
    // covers buttons U..U+N-1) -- so a mouse listing sixteen buttons costs one
    // candidate, not sixteen that crowd its axes out of the workspace.
    if (w->count != 0u) {
        candidate_t *last = &w->items[w->count - 1u];
        const bool bits_follow = last->field.bit_offset + last->field.bit_width == f->bit_offset;
        const bool buttons_follow = last->field.usage + last->field.bit_width == first;
        if (last->field.kind == HID_MOUSE_BUTTONS && last->report_id == f->report_id &&
            bits_follow && buttons_follow && last->field.bit_width + count <= MAX_ENTRY_BITS) {
            last->field.bit_width = (uint8_t)(last->field.bit_width + count);
            return;
        }
    }

    hid_mouse_field_t button = {
        .kind = HID_MOUSE_BUTTONS,
        .usage_page = PAGE_BUTTON,
        .usage = (uint16_t)first,
        .bit_offset = f->bit_offset,
        .bit_width = (uint8_t)count,
        .logical_minimum = 0,
        .logical_maximum = 1,
    };
    add_candidate(w, f->report_id, &button);
}

static void collect_axes(workspace_t *w, const hid_field_t *f)
{
    if (f->bit_size > MAX_ENTRY_BITS) {
        return;
    }
    const uint32_t elements = f->count < MAX_AXIS_RUN ? f->count : MAX_AXIS_RUN;
    for (uint32_t i = 0u; i < elements; ++i) {
        const uint16_t usage = f->usage_is_range ? (uint16_t)(f->usage + i) : f->usage;
        const int kind = axis_kind(f->usage_page, usage);
        if (kind >= 0 && range_holds_a_step(kind, f->logical_minimum, f->logical_maximum)) {
            hid_mouse_field_t axis = {
                .kind = (uint8_t)kind,
                .usage_page = f->usage_page,
                .usage = usage,
                .bit_offset = (uint16_t)(f->bit_offset + i * f->bit_size),
                .bit_width = f->bit_size,
                .logical_minimum = f->logical_minimum,
                .logical_maximum = f->logical_maximum,
            };
            add_candidate(w, f->report_id, &axis);
        }
        if (!f->usage_is_range) {
            break;  // a shared usage names only the first element
        }
    }
}

static bool collect(void *context, const hid_field_t *f)
{
    workspace_t *w = context;
    if (f->app_usage_page != PAGE_GENERIC_DESKTOP || f->app_usage != USAGE_MOUSE) {
        return true;  // keyboards, pads, vendor collections: never injectable
    }
    w->saw_mouse = true;
    if ((f->flags & HID_FIELD_CONSTANT) != 0u || (f->flags & HID_FIELD_VARIABLE) == 0u) {
        return true;  // padding and arrays carry nothing to add to
    }
    if (f->usage_page == PAGE_BUTTON) {
        if (f->bit_size == 1u && (f->flags & HID_FIELD_RELATIVE) == 0u) {
            collect_buttons(w, f);
        }
    } else if ((f->flags & HID_FIELD_RELATIVE) != 0u) {
        // Absolute axes are skipped: injection is additive.
        collect_axes(w, f);
    }
    return true;
}

static const candidate_t *first_of(const workspace_t *w, uint8_t kind, uint8_t report_id)
{
    for (uint8_t i = 0u; i < w->count; ++i) {
        if (w->items[i].field.kind == kind && w->items[i].report_id == report_id) {
            return &w->items[i];
        }
    }
    return NULL;
}

// One layout: every command cites exactly one report ID. It is the first report
// (in declaration order) that carries both an injectable X and Y.
static bool pick_report(const workspace_t *w, uint8_t *report_id)
{
    for (uint8_t i = 0u; i < w->count; ++i) {
        const candidate_t *c = &w->items[i];
        if (c->field.kind == HID_MOUSE_X && first_of(w, HID_MOUSE_Y, c->report_id) != NULL) {
            *report_id = c->report_id;
            return true;
        }
    }
    return false;
}

static void append_field(hid_mouse_layout_t *out, const hid_mouse_field_t *field)
{
    if (out->field_count >= HID_MOUSE_MAX_FIELDS) {
        return;
    }
    out->fields[out->field_count++] = *field;
    const uint32_t reach = ((uint32_t)field->bit_offset + field->bit_width + 7u) / 8u;
    if (reach > out->min_report_length) {
        out->min_report_length = (uint8_t)reach;
    }
}

hid_mouse_status_t hid_mouse_compile(const uint8_t *descriptor, size_t length,
                                     hid_mouse_layout_t *out)
{
    memset(out, 0, sizeof(*out));
    memset(&s_work, 0, sizeof(s_work));

    const hid_fields_status_t walked =
        hid_fields_walk(&s_walker, descriptor, length, collect, &s_work);
    if (walked == HID_FIELDS_LIMIT) {
        return HID_MOUSE_UNSUPPORTED;
    }
    if (walked != HID_FIELDS_OK) {
        return HID_MOUSE_MALFORMED;
    }
    if (!s_work.saw_mouse) {
        return HID_MOUSE_NOT_MOUSE;
    }

    uint8_t report_id = 0u;
    if (!pick_report(&s_work, &report_id)) {
        return HID_MOUSE_NO_AXES;
    }
    const uint32_t report_bytes = (hid_fields_input_bits(&s_walker, report_id) + 7u) / 8u;
    if (report_bytes > INJ_MAX_REPORT_BYTES) {
        return HID_MOUSE_UNSUPPORTED;
    }

    out->report_id = report_id;
    out->report_length = (uint8_t)report_bytes;
    // Axes first, in kind order, then button runs in report order: the upload
    // CRC depends on the order, so it must not depend on the descriptor's.
    for (uint8_t kind = HID_MOUSE_X; kind <= HID_MOUSE_PAN; ++kind) {
        const candidate_t *c = first_of(&s_work, kind, report_id);
        if (c != NULL) {
            append_field(out, &c->field);
            out->axes |= HID_MOUSE_AXIS_BIT(kind);
        }
    }
    uint8_t runs = 0u;
    for (uint8_t i = 0u; i < s_work.count && runs < HID_MOUSE_MAX_BUTTON_RUNS; ++i) {
        const candidate_t *c = &s_work.items[i];
        if (c->field.kind == HID_MOUSE_BUTTONS && c->report_id == report_id) {
            append_field(out, &c->field);
            runs++;
        }
    }
    return HID_MOUSE_OK;
}
