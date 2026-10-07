// See hid_pad_layout.h.

#include "hid_pad_layout.h"

#include <stdbool.h>
#include <string.h>

#include "hid_fields.h"
#include "injection_wire.h"

#define PAGE_GENERIC_DESKTOP 0x01u
#define PAGE_SIMULATION 0x02u
#define PAGE_BUTTON 0x09u
#define USAGE_JOYSTICK 0x04u
#define USAGE_GAME_PAD 0x05u
#define USAGE_MULTI_AXIS 0x08u
#define USAGE_X 0x30u
#define USAGE_Y 0x31u
#define USAGE_Z 0x32u
#define USAGE_RX 0x33u
#define USAGE_RY 0x34u
#define USAGE_RZ 0x35u
#define USAGE_HAT 0x39u
#define USAGE_ACCELERATOR 0xC4u
#define USAGE_BRAKE 0xC5u

// The FPGA maps injected BUTTON_STATE bit n to button n+1 for n < 64, one
// entry spans at most 32 bits, and an absolute entry's held value is 16 bits.
#define MAX_BUTTON_NUMBER 64u
#define MAX_ENTRY_BITS 32u
#define MAX_ABSOLUTE_BITS 16u

// The hat is only injectable when 0..7 are the eight directions and 8 is the
// null (centred) value the MCU sends for "released" -- the FPGA writes a held
// value unclamped, so any other convention would need a translation this
// version does not have. The field must also be wide enough to carry the 8:
// the FPGA masks the held value to the field width, so on a 3-bit hat 8 would
// land as 0, "up".
#define HAT_MINIMUM 0
#define HAT_MAXIMUM 7
#define HAT_MIN_BITS 4u

// Every axis and button run of a real pad (adjacent buttons merge as they are
// collected). Unlike the mouse compiler, running out of room here is an error
// (UNSUPPORTED), because a dropped candidate could be a channel.
#define MAX_CANDIDATES 16u
#define MAX_AXIS_RUN 16u

// Simulation-page triggers are a fallback: they only become LT/RT when the
// report has no Rx/Ry, so they are tagged as such and resolved after the walk.
#define KIND_SIM_LT 0x80u
#define KIND_SIM_RT 0x81u

typedef struct {
    uint8_t report_id;
    hid_layout_field_t field;
} candidate_t;

typedef struct {
    candidate_t items[MAX_CANDIDATES];
    uint8_t count;
    bool saw_pad;
    bool overflow;  // a channel or button run had no room: the layout is not whole
} workspace_t;

// Static, not on the stack: core0's whole stack is 2 KiB. These are this
// compiler's own -- hid_mouse_layout.c's walker and workspace are file-local.
static hid_fields_walker_t s_walker;
static workspace_t s_work;

static int axis_kind(uint16_t page, uint16_t usage)
{
    if (page == PAGE_GENERIC_DESKTOP) {
        switch (usage) {
        case USAGE_X:
            return HID_PAD_KIND_LX;
        case USAGE_Y:
            return HID_PAD_KIND_LY;
        case USAGE_Z:
            return HID_PAD_KIND_RX;
        case USAGE_RZ:
            return HID_PAD_KIND_RY;
        case USAGE_RX:
            return HID_PAD_KIND_LT;
        case USAGE_RY:
            return HID_PAD_KIND_RT;
        case USAGE_HAT:
            return HID_PAD_KIND_HAT;
        default:
            return -1;
        }
    }
    if (page == PAGE_SIMULATION) {
        if (usage == USAGE_ACCELERATOR) {
            return (int)KIND_SIM_LT;
        }
        if (usage == USAGE_BRAKE) {
            return (int)KIND_SIM_RT;
        }
    }
    return -1;
}

static void add_candidate(workspace_t *w, uint8_t report_id, const hid_layout_field_t *field)
{
    if (w->count >= MAX_CANDIDATES) {
        w->overflow = true;
        return;
    }
    w->items[w->count].report_id = report_id;
    w->items[w->count].field = *field;
    w->count++;
}

// Same rules as hid_mouse_layout.c: a run must name every element (a range,
// or a single listed usage), adjacent runs fold into one entry.
static void collect_buttons(workspace_t *w, const hid_field_t *f)
{
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
    if (w->count != 0u) {
        candidate_t *last = &w->items[w->count - 1u];
        const bool bits_follow = last->field.bit_offset + last->field.bit_width == f->bit_offset;
        const bool buttons_follow = last->field.usage + last->field.bit_width == first;
        if (last->field.kind == HID_PAD_KIND_BUTTONS && last->report_id == f->report_id &&
            bits_follow && buttons_follow && last->field.bit_width + count <= MAX_ENTRY_BITS) {
            last->field.bit_width = (uint8_t)(last->field.bit_width + count);
            return;
        }
    }
    hid_layout_field_t button = {
        .kind = HID_PAD_KIND_BUTTONS,
        .usage_page = PAGE_BUTTON,
        .usage = (uint16_t)first,
        .bit_offset = f->bit_offset,
        .bit_width = (uint8_t)count,
        .logical_minimum = 0,
        .logical_maximum = 1,
    };
    add_candidate(w, f->report_id, &button);
}

static bool hat_range_ok(const hid_field_t *f)
{
    return f->logical_minimum == HAT_MINIMUM && f->logical_maximum == HAT_MAXIMUM &&
           f->bit_size >= HAT_MIN_BITS;
}

static void collect_axes(workspace_t *w, const hid_field_t *f)
{
    if (f->bit_size > MAX_ABSOLUTE_BITS) {
        return;  // the FPGA holds 16 bits; a wider field cannot be set
    }
    const uint32_t elements = f->count < MAX_AXIS_RUN ? f->count : MAX_AXIS_RUN;
    for (uint32_t i = 0u; i < elements; ++i) {
        const uint16_t usage = f->usage_is_range ? (uint16_t)(f->usage + i) : f->usage;
        const int kind = axis_kind(f->usage_page, usage);
        if (kind >= 0 && (kind != HID_PAD_KIND_HAT || hat_range_ok(f))) {
            hid_layout_field_t axis = {
                .kind = (uint8_t)kind,
                .null_state = (f->flags & HID_FIELD_NULL_STATE) != 0u ? 1u : 0u,
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
    if (f->app_usage_page != PAGE_GENERIC_DESKTOP ||
        (f->app_usage != USAGE_GAME_PAD && f->app_usage != USAGE_JOYSTICK &&
         f->app_usage != USAGE_MULTI_AXIS)) {
        return true;  // mice, keyboards, vendor collections: not this compiler's
    }
    w->saw_pad = true;
    if ((f->flags & HID_FIELD_CONSTANT) != 0u || (f->flags & HID_FIELD_VARIABLE) == 0u) {
        return true;  // padding and arrays
    }
    if ((f->flags & HID_FIELD_RELATIVE) != 0u) {
        return true;  // a relative axis cannot be SET; nothing here is additive
    }
    if (f->usage_page == PAGE_BUTTON) {
        if (f->bit_size == 1u) {
            collect_buttons(w, f);
        }
    } else {
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

// The first report (in declaration order) carrying both X and Y.
static bool pick_report(const workspace_t *w, uint8_t *report_id)
{
    for (uint8_t i = 0u; i < w->count; ++i) {
        const candidate_t *c = &w->items[i];
        if (c->field.kind == HID_PAD_KIND_LX &&
            first_of(w, HID_PAD_KIND_LY, c->report_id) != NULL) {
            *report_id = c->report_id;
            return true;
        }
    }
    return false;
}

// Returns false when the layout is full: the caller reports UNSUPPORTED rather
// than shipping a layout with a channel missing.
static bool append_field(hid_layout_t *out, const hid_layout_field_t *field)
{
    if (out->field_count >= HID_LAYOUT_MAX_FIELDS) {
        return false;
    }
    out->fields[out->field_count++] = *field;
    const uint32_t reach = ((uint32_t)field->bit_offset + field->bit_width + 7u) / 8u;
    if (reach > out->min_report_length) {
        out->min_report_length = (uint8_t)reach;
    }
    return true;
}

// Simulation-page Accelerator/Brake stand in for LT/RT only when the report
// declares no Rx/Ry: a pad with both (a wheel with pedals AND a right stick)
// keeps the Generic Desktop reading.
static const candidate_t *trigger_of(const workspace_t *w, uint8_t kind, uint8_t report_id)
{
    const candidate_t *c = first_of(w, kind, report_id);
    if (c != NULL) {
        return c;
    }
    const bool rx_ry_absent = first_of(w, HID_PAD_KIND_LT, report_id) == NULL &&
                              first_of(w, HID_PAD_KIND_RT, report_id) == NULL;
    if (!rx_ry_absent) {
        return NULL;
    }
    return first_of(w, kind == HID_PAD_KIND_LT ? (uint8_t)KIND_SIM_LT : (uint8_t)KIND_SIM_RT,
                    report_id);
}

hid_pad_status_t hid_pad_compile(const uint8_t *descriptor, size_t length, hid_layout_t *out)
{
    memset(out, 0, sizeof(*out));
    memset(&s_work, 0, sizeof(s_work));

    const hid_fields_status_t walked =
        hid_fields_walk(&s_walker, descriptor, length, collect, &s_work);
    if (walked == HID_FIELDS_LIMIT) {
        return HID_PAD_UNSUPPORTED;
    }
    if (walked != HID_FIELDS_OK) {
        return HID_PAD_MALFORMED;
    }
    if (!s_work.saw_pad) {
        return HID_PAD_NOT_PAD;
    }

    uint8_t report_id = 0u;
    if (!pick_report(&s_work, &report_id)) {
        return HID_PAD_NO_AXES;
    }
    const uint32_t report_bytes = (hid_fields_input_bits(&s_walker, report_id) + 7u) / 8u;
    if (report_bytes > INJ_MAX_REPORT_BYTES || s_work.overflow) {
        return HID_PAD_UNSUPPORTED;
    }

    out->report_id = report_id;
    out->report_length = (uint8_t)report_bytes;
    out->device_class = HID_DEVICE_CLASS_PAD;
    // Channels first, in channel order, then button runs in report order: the
    // upload CRC depends on the order, so it must not depend on the descriptor's.
    for (uint8_t kind = HID_PAD_KIND_LX; kind <= HID_PAD_KIND_HAT; ++kind) {
        const candidate_t *c = (kind == HID_PAD_KIND_LT || kind == HID_PAD_KIND_RT)
                                   ? trigger_of(&s_work, kind, report_id)
                                   : first_of(&s_work, kind, report_id);
        if (c != NULL) {
            hid_layout_field_t field = c->field;
            field.kind = kind;  // a Simulation trigger is renamed to the channel it fills
            if (!append_field(out, &field)) {
                memset(out, 0, sizeof(*out));
                return HID_PAD_UNSUPPORTED;
            }
            out->axes |= HID_LAYOUT_AXIS_BIT(kind);
        }
    }
    for (uint8_t i = 0u; i < s_work.count; ++i) {
        const candidate_t *c = &s_work.items[i];
        if (c->field.kind == HID_PAD_KIND_BUTTONS && c->report_id == report_id &&
            !append_field(out, &c->field)) {
            memset(out, 0, sizeof(*out));
            return HID_PAD_UNSUPPORTED;
        }
    }
    return HID_PAD_OK;
}
