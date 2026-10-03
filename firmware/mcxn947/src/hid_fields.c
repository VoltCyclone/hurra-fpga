// See hid_fields.h.

#include "hid_fields.h"

#include <string.h>

#include "hid_item.h"

#define MAIN_INPUT 0x8u
#define MAIN_OUTPUT 0x9u
#define MAIN_COLLECTION 0xAu
#define MAIN_FEATURE 0xBu
#define MAIN_END_COLLECTION 0xCu

#define GLOBAL_USAGE_PAGE 0x0u
#define GLOBAL_LOGICAL_MINIMUM 0x1u
#define GLOBAL_LOGICAL_MAXIMUM 0x2u
#define GLOBAL_REPORT_SIZE 0x7u
#define GLOBAL_REPORT_ID 0x8u
#define GLOBAL_REPORT_COUNT 0x9u
#define GLOBAL_PUSH 0xAu
#define GLOBAL_POP 0xBu

#define LOCAL_USAGE 0x0u
#define LOCAL_USAGE_MINIMUM 0x1u
#define LOCAL_USAGE_MAXIMUM 0x2u

#define COLLECTION_APPLICATION 0x01u

// Largest bit position a field may reach; bit_offset is 16 bits.
#define MAX_REPORT_BITS 0xFFFFu

static hid_fields_usage_t local_usage(const hid_item_t *item)
{
    const hid_fields_usage_t usage = {
        .value = item->size == 4u ? item->value : (item->value & 0xFFFFu),
        .is_short = item->size != 4u,
    };
    return usage;
}

// NULL is "no usage declared": page and usage 0, which no field policy matches.
static void resolve_usage(const hid_fields_usage_t *stored, uint16_t current_page, uint16_t *page,
                          uint16_t *usage)
{
    if (stored == NULL) {
        *page = 0u;
        *usage = 0u;
        return;
    }
    *usage = (uint16_t)(stored->value & 0xFFFFu);
    *page = stored->is_short ? current_page : (uint16_t)(stored->value >> 16u);
}

// The usage that names a whole item: the first listed one, else the start of a
// range, else none. A Collection's usage and an array's label are both this.
static const hid_fields_usage_t *first_local_usage(const hid_fields_walker_t *w)
{
    if (w->usage_count != 0u) {
        return &w->usages[0];
    }
    return w->have_usage_minimum ? &w->usage_minimum : NULL;
}

static void clear_locals(hid_fields_walker_t *w)
{
    w->usage_count = 0u;
    w->usage_overflow = false;
    w->have_usage_minimum = false;
    w->have_usage_maximum = false;
}

// Elements a Usage Minimum/Maximum pair names; 0 when the pair is malformed --
// a maximum below its minimum, or on another page -- and so names none.
static uint32_t range_span(const hid_fields_walker_t *w, uint16_t current_page)
{
    uint16_t min_page;
    uint16_t min_usage;
    uint16_t max_page;
    uint16_t max_usage;
    resolve_usage(&w->usage_minimum, current_page, &min_page, &min_usage);
    resolve_usage(&w->usage_maximum, current_page, &max_page, &max_usage);
    if (min_page != max_page || max_usage < min_usage) {
        return 0u;
    }
    return (uint32_t)(max_usage - min_usage) + 1u;
}

// A logical maximum is unsigned unless the minimum is negative (HID 1.11
// 6.2.2.7): 0x25 0xFF is 255 against a minimum of 0, -1 against -128.
static int32_t logical_maximum(const hid_fields_globals_t *g)
{
    if (g->logical_minimum >= 0 || g->logical_maximum_size == 4u ||
        g->logical_maximum_size == 0u) {
        return (int32_t)g->logical_maximum_raw;
    }
    const uint8_t bits = (uint8_t)(g->logical_maximum_size * 8u);
    const uint32_t sign = UINT32_C(1) << (bits - 1u);
    if ((g->logical_maximum_raw & sign) == 0u) {
        return (int32_t)g->logical_maximum_raw;
    }
    return (int32_t)g->logical_maximum_raw - (int32_t)(UINT32_C(1) << bits);
}

static hid_fields_cursor_t *cursor_for(hid_fields_walker_t *w, uint8_t report_id)
{
    for (uint8_t i = 0u; i < w->cursor_count; ++i) {
        if (w->cursors[i].id == report_id) {
            return &w->cursors[i];
        }
    }
    if (w->cursor_count >= HID_FIELDS_MAX_REPORT_IDS) {
        return NULL;
    }
    hid_fields_cursor_t *cursor = &w->cursors[w->cursor_count++];
    cursor->id = report_id;
    // A nonzero ID is byte 0 of its report, so its fields start at bit 8.
    cursor->bits = report_id != 0u ? 8u : 0u;
    return cursor;
}

typedef struct {
    hid_field_t base;       // every field of one Input item shares all but these three
    uint16_t current_page;  // resolves short usages
    hid_field_fn emit;
    void *context;
} emission_t;

static bool emit_run(const emission_t *e, uint32_t bit_offset, uint32_t count,
                     const hid_fields_usage_t *usage, bool is_range)
{
    if (e->emit == NULL) {
        return true;
    }
    hid_field_t field = e->base;
    field.bit_offset = (uint16_t)bit_offset;
    field.count = (uint16_t)count;
    field.usage_is_range = is_range;
    resolve_usage(usage, e->current_page, &field.usage_page, &field.usage);
    return e->emit(e->context, &field);
}

static hid_fields_status_t walk_input(hid_fields_walker_t *w, uint32_t flags, hid_field_fn emit,
                                      void *context)
{
    const hid_fields_globals_t *g = &w->globals;
    const uint32_t size = g->report_size;
    const uint32_t count = g->report_count;
    if (size == 0u || count == 0u) {
        return HID_FIELDS_OK;  // describes no bits
    }
    if (size > 0xFFu || count > MAX_REPORT_BITS) {
        return HID_FIELDS_LIMIT;
    }

    hid_fields_cursor_t *cursor = cursor_for(w, g->report_id);
    if (cursor == NULL) {
        return HID_FIELDS_LIMIT;
    }
    const uint32_t start = cursor->bits;
    const uint32_t total = size * count;  // both bounded above: cannot wrap
    if (total > MAX_REPORT_BITS - start) {
        return HID_FIELDS_LIMIT;
    }

    emission_t e;
    memset(&e, 0, sizeof(e));
    e.emit = emit;
    e.context = context;
    e.current_page = g->usage_page;
    e.base.report_id = g->report_id;
    e.base.bit_size = (uint8_t)size;
    e.base.flags = flags;
    e.base.logical_minimum = g->logical_minimum;
    e.base.logical_maximum = logical_maximum(g);
    e.base.app_usage_page = w->app_usage_page;
    e.base.app_usage = w->app_usage;

    bool keep_going = true;
    if ((flags & HID_FIELD_VARIABLE) != 0u && w->usage_count != 0u) {
        // Listed usages name elements in order; any elements beyond the list
        // share its last usage (HID 1.11 6.2.2.8). If the list overflowed, the
        // last usage kept is not the last declared, so the tail is unnamed.
        const uint32_t named = w->usage_count < count ? w->usage_count : count;
        for (uint32_t i = 0u; i < named && keep_going; ++i) {
            keep_going = emit_run(&e, start + i * size, 1u, &w->usages[i], false);
        }
        if (keep_going && count > named) {
            const hid_fields_usage_t *tail =
                w->usage_overflow ? NULL : &w->usages[w->usage_count - 1u];
            keep_going = emit_run(&e, start + named * size, count - named, tail, false);
        }
    } else if ((flags & HID_FIELD_VARIABLE) != 0u && w->have_usage_minimum &&
               w->have_usage_maximum) {
        // A range names element i as minimum + i up to its maximum; elements
        // beyond it share the maximum, as a list's tail shares its last usage.
        const uint32_t span = range_span(w, g->usage_page);
        const uint32_t named = span < count ? span : count;
        if (named == 0u) {
            keep_going = emit_run(&e, start, count, NULL, false);
        } else {
            keep_going = emit_run(&e, start, named, &w->usage_minimum, true);
            if (keep_going && count > named) {
                keep_going = emit_run(&e, start + named * size, count - named,
                                      &w->usage_maximum, false);
            }
        }
    } else {
        // One run: a range with no maximum names element i as minimum + i; an
        // array's usages name the values its elements may hold, not the
        // elements, so it is labelled with its first usage.
        keep_going = emit_run(&e, start, count, first_local_usage(w),
                              w->usage_count == 0u && w->have_usage_minimum);
    }

    cursor->bits = start + total;
    return keep_going ? HID_FIELDS_OK : HID_FIELDS_STOPPED;
}

static hid_fields_status_t walk_main(hid_fields_walker_t *w, const hid_item_t *item,
                                     hid_field_fn emit, void *context)
{
    hid_fields_status_t status = HID_FIELDS_OK;
    switch (item->tag) {
    case MAIN_INPUT:
        status = walk_input(w, item->value, emit, context);
        break;
    case MAIN_OUTPUT:
    case MAIN_FEATURE:
        // Parsed for their globals only: they never occupy INPUT bits.
        break;
    case MAIN_COLLECTION:
        if (w->collection_depth >= HID_FIELDS_MAX_COLLECTION_DEPTH) {
            return HID_FIELDS_LIMIT;
        }
        if (w->collection_depth == 0u) {
            w->app_usage_page = 0u;
            w->app_usage = 0u;
            if ((item->value & 0xFFu) == COLLECTION_APPLICATION) {
                // A collection's usage is the first local usage before it.
                resolve_usage(first_local_usage(w), w->globals.usage_page, &w->app_usage_page,
                              &w->app_usage);
            }
        }
        w->collection_depth++;
        break;
    case MAIN_END_COLLECTION:
        if (w->collection_depth == 0u) {
            return HID_FIELDS_UNBALANCED;
        }
        w->collection_depth--;
        if (w->collection_depth == 0u) {
            w->app_usage_page = 0u;
            w->app_usage = 0u;
        }
        break;
    default:
        break;
    }
    // Locals apply to exactly one main item (HID 1.11 6.2.2.8).
    clear_locals(w);
    return status;
}

static hid_fields_status_t walk_global(hid_fields_walker_t *w, const hid_item_t *item)
{
    hid_fields_globals_t *g = &w->globals;
    switch (item->tag) {
    case GLOBAL_USAGE_PAGE:
        g->usage_page = (uint16_t)(item->value & 0xFFFFu);
        break;
    case GLOBAL_LOGICAL_MINIMUM:
        g->logical_minimum = item->svalue;
        break;
    case GLOBAL_LOGICAL_MAXIMUM:
        g->logical_maximum_raw = item->value;
        g->logical_maximum_size = item->size;
        break;
    case GLOBAL_REPORT_SIZE:
        g->report_size = item->value;
        break;
    case GLOBAL_REPORT_ID:
        // HID 1.11 6.2.2.7 reserves 0; an ID is one byte on the wire.
        if (item->value == 0u || item->value > 0xFFu) {
            return HID_FIELDS_BAD_ID;
        }
        g->report_id = (uint8_t)item->value;
        break;
    case GLOBAL_REPORT_COUNT:
        g->report_count = item->value;
        break;
    case GLOBAL_PUSH:
        if (w->global_depth >= HID_FIELDS_MAX_GLOBAL_STACK) {
            return HID_FIELDS_LIMIT;
        }
        w->global_stack[w->global_depth++] = *g;
        break;
    case GLOBAL_POP:
        if (w->global_depth == 0u) {
            return HID_FIELDS_LIMIT;
        }
        *g = w->global_stack[--w->global_depth];
        break;
    default:
        break;  // physical range, units: not needed to place or name a field
    }
    return HID_FIELDS_OK;
}

static void walk_local(hid_fields_walker_t *w, const hid_item_t *item)
{
    switch (item->tag) {
    case LOCAL_USAGE:
        if (w->usage_count < HID_FIELDS_MAX_USAGES) {
            w->usages[w->usage_count++] = local_usage(item);
        } else {
            w->usage_overflow = true;
        }
        break;
    case LOCAL_USAGE_MINIMUM:
        w->usage_minimum = local_usage(item);
        w->have_usage_minimum = true;
        break;
    case LOCAL_USAGE_MAXIMUM:
        w->usage_maximum = local_usage(item);
        w->have_usage_maximum = true;
        break;
    default:
        break;  // designators, strings
    }
}

hid_fields_status_t hid_fields_walk(hid_fields_walker_t *walker, const uint8_t *descriptor,
                                    size_t length, hid_field_fn emit, void *context)
{
    memset(walker, 0, sizeof(*walker));

    size_t pos = 0u;
    hid_item_t item;
    for (;;) {
        const hid_item_status_t read = hid_item_next(descriptor, length, &pos, &item);
        if (read == HID_ITEM_END) {
            break;
        }
        if (read == HID_ITEM_TRUNCATED) {
            return HID_FIELDS_TRUNCATED;
        }
        if (item.is_long) {
            continue;
        }

        hid_fields_status_t status = HID_FIELDS_OK;
        switch (item.type) {
        case HID_ITEM_TYPE_MAIN:
            status = walk_main(walker, &item, emit, context);
            break;
        case HID_ITEM_TYPE_GLOBAL:
            status = walk_global(walker, &item);
            break;
        case HID_ITEM_TYPE_LOCAL:
            walk_local(walker, &item);
            break;
        default:
            break;
        }
        if (status != HID_FIELDS_OK) {
            return status;
        }
    }

    return walker->collection_depth == 0u ? HID_FIELDS_OK : HID_FIELDS_UNBALANCED;
}

uint32_t hid_fields_input_bits(const hid_fields_walker_t *walker, uint8_t report_id)
{
    for (uint8_t i = 0u; i < walker->cursor_count; ++i) {
        if (walker->cursors[i].id == report_id) {
            return walker->cursors[i].bits;
        }
    }
    return 0u;
}
