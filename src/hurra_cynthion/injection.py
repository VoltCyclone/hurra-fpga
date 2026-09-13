"""Transactional, descriptor-generic mutation of complete native HID reports."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

from amaranth import Cat, Const, Elaboratable, Module, Mux, Signal, signed
from amaranth.lib.data import StructLayout, unsigned
from amaranth.lib.memory import Memory

from .injection_wire import (
    INJ_CLEAR_FLAG_BUTTONS,
    INJ_CLEAR_FLAG_MOTION,
    INJ_CLEAR_FLAG_PHYSICAL_MASKS,
    INJ_MAP_ENTRY_FLAG_BUTTON,
    INJ_MAP_ENTRY_FLAG_RELATIVE,
    INJ_MAP_ENTRY_FLAG_SIGNED,
    INJ_MAP_ENTRY_FLAG_WHEEL,
    INJ_MAP_ENTRY_FLAG_X,
    INJ_MAP_ENTRY_FLAG_Y,
    INJ_RELATIVE_FLAG_PAN,
    INJ_RELATIVE_FLAG_WHEEL,
    INJ_RELATIVE_FLAG_X,
    INJ_RELATIVE_FLAG_Y,
    LIMITS,
)

__all__ = ["ReportInjectionEngine"]

STATE_LAYOUT = StructLayout(
    {
        "interface_number": unsigned(8),
        "endpoint_number": unsigned(8),
        "report_id": unsigned(8),
        "descriptor_generation": unsigned(16),
        "map_generation": unsigned(16),
        "x": signed(32),
        "y": signed(32),
        "wheel": signed(32),
        "pan": signed(32),
        "buttons": unsigned(64),
        "masks": unsigned(64),
        "sequence": unsigned(16),
        "report_length": unsigned(7),
        "click_active": unsigned(1),
        "click_release_target": unsigned(32),
    }
)


class ReportInjectionEngine(Elaboratable):
    """Buffer and transactionally mutate one complete tagged HID report.

    Field offsets use the complete report's wire coordinates. In particular,
    byte zero remains part of the template when a Report ID is present; no
    offset adjustment is made in this engine.

    Decoded command inputs use one valid/ready channel per command type. A
    command that targets a report is held pending until the complete resulting
    report is accepted. This makes ``*_ready`` a transactional acknowledgement:
    residual, button, mask, and command-sequence state all advance on the same
    final-byte handshake.
    """

    def __init__(self, map_store) -> None:
        self.map_store = map_store

        # Tagged native-report input from ReportMergeMux.
        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_interface = Signal(8)
        self.report_endpoint = Signal(4)

        # Buffered report output to ReportRelay.
        self.output_valid = Signal()
        self.output_ready = Signal()
        self.output_data = Signal(8)
        self.output_first = Signal()
        self.output_last = Signal()
        self.output_interface = Signal(8)
        self.output_endpoint = Signal(4)
        self.output_report_id = Signal(8)

        # USB-time scheduling inputs. The accepted-report count is supplied by
        # the downstream integration so click duration follows successful
        # report admission rather than elapsed SOFs.
        self.sof_tick = Signal()
        self.accepted_report_count = Signal(32)

        # Decoded relative command.
        self.relative_valid = Signal()
        self.relative_ready = Signal()
        self.relative_interface = Signal(8)
        self.relative_endpoint = Signal(8)
        self.relative_report_id = Signal(8)
        self.relative_flags = Signal(4)
        self.relative_x = Signal(signed(32))
        self.relative_y = Signal(signed(32))
        self.relative_wheel = Signal(signed(32))
        self.relative_pan = Signal(signed(32))
        self.relative_command_sequence = Signal(16)

        # Decoded injected-button state command.
        self.button_valid = Signal()
        self.button_ready = Signal()
        self.button_interface = Signal(8)
        self.button_endpoint = Signal(8)
        self.button_report_id = Signal(8)
        self.button_buttons = Signal(64)
        self.button_hold_reports = Signal(16)
        self.button_command_sequence = Signal(16)

        # Decoded physical-button suppression command.
        self.mask_valid = Signal()
        self.mask_ready = Signal()
        self.mask_interface = Signal(8)
        self.mask_endpoint = Signal(8)
        self.mask_report_id = Signal(8)
        self.mask_buttons = Signal(64)
        self.mask_command_sequence = Signal(16)

        # Decoded clear command. Clear is global and is committed with the next
        # mapped report transaction; later integration can fan it out across
        # cached layouts when stationary scheduling is added.
        self.clear_valid = Signal()
        self.clear_ready = Signal()
        self.clear_flags = Signal(16)
        self.clear_command_sequence = Signal(16)

        # Observable state for the most recently committed target.
        self.pending_x = Signal(signed(32))
        self.pending_y = Signal(signed(32))
        self.pending_wheel = Signal(signed(32))
        self.pending_pan = Signal(signed(32))
        self.injected_buttons = Signal(64)
        self.physical_mask = Signal(64)
        self.last_committed_command_sequence = Signal(16)
        self.command_committed = Signal()
        self.command_overflow = Signal(32)

        # Simulation-visible scan instrumentation; these are combinational
        # aliases of the stationary scan FSM state and current slot.
        self._stationary_scan_active = Signal()
        self._stationary_scan_index = Signal(range(LIMITS["layouts"]))

        # Simulation-visible field-pipeline instrumentation. The metadata and
        # total signals are the actual registered stage boundaries.
        self._entry_snapshot_active = Signal()
        self._entry_dispatch_active = Signal()
        self._field_total_capture_active = Signal()
        self._field_commit_active = Signal()
        self._entry_matches_q = Signal()
        self._entry_class_q = Signal(2)
        self._entry_bit_offset_q = Signal(16)
        self._entry_bit_width_q = Signal(8)
        self._entry_usage_q = Signal(16)
        self._entry_signed_q = Signal()
        self._entry_axis_q = Signal(2)
        self._entry_logical_minimum_q = Signal(signed(32))
        self._entry_logical_maximum_q = Signal(signed(32))
        self._field_total_q = Signal(signed(34))

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        store = self.map_store

        report_buffer = Memory(
            shape=16,
            depth=LIMITS["report_bytes"],
            init=[],
            attrs={"ram_style": "distributed"},
        )
        m.submodules.report_buffer = report_buffer
        report_buffer_read = report_buffer.read_port(domain="usb")
        report_buffer_write = report_buffer.write_port(domain="usb")
        buffer_read_address = Signal(range(LIMITS["report_bytes"]))
        buffer_read_enable = Signal()
        buffer_write_address = Signal(range(LIMITS["report_bytes"]))
        buffer_write_data = Signal(16)
        buffer_write_enable = Signal()
        buffer_native_byte = report_buffer_read.data[:8]
        buffer_working_byte = report_buffer_read.data[8:]

        input_index = Signal(range(64))
        output_index = Signal(range(64))
        stationary_load_index = Signal(range(64))
        # Byte address presented to the template cache. It runs one ahead of
        # ``stationary_load_index`` because the cache is now a byte-wide
        # synchronous memory: the data valid this cycle is whatever address was
        # presented last cycle.
        stationary_fetch_index = Signal(range(64))
        cache_copy_index = Signal(range(64))
        report_length = Signal(7)
        captured_first_byte = Signal(8)
        captured_interface = Signal(8)
        captured_endpoint = Signal(4)
        selected_report_id = Signal(8)
        layout_query_report_id = Signal(8)
        snapshot_descriptor_generation = Signal(16)
        snapshot_map_generation = Signal(16)
        snapshot_bank = Signal()
        snapshot_entry_count = Signal(7)

        # One canonical state record is retained at the same bounded slot as
        # its active map layout. Validity and per-category live masks stay in
        # registers so invalidation and global CLEAR remain atomic.
        layout_count = LIMITS["layouts"]
        state_valid = Signal(layout_count, name="state_valid")
        state_motion_live = Signal(layout_count, name="state_motion_live")
        state_buttons_live = Signal(layout_count, name="state_buttons_live")
        state_masks_live = Signal(layout_count, name="state_masks_live")
        selected_state = Signal(range(layout_count))
        selected_state_available = Signal()
        stationary_prefetch_index = Signal(range(layout_count))
        stationary_scan_index = Signal(range(layout_count))
        stationary_decision_valid = Signal()
        stationary_decision_pending = Signal()
        stationary_decision_last = Signal()
        stationary_decision_index = Signal(range(layout_count))
        stationary_decision_interface = Signal(8)
        stationary_decision_endpoint = Signal(4)
        stationary_decision_report_id = Signal(8)
        stationary_decision_report_length = Signal(7)
        stationary_predicate_valid_q = Signal()
        stationary_predicate_motion_q = Signal()
        stationary_predicate_relative_q = Signal()
        stationary_predicate_button_q = Signal()
        stationary_predicate_mask_q = Signal()
        stationary_predicate_click_q = Signal()
        stationary_predicate_last_q = Signal()
        stationary_predicate_index_q = Signal(range(layout_count))
        stationary_predicate_interface_q = Signal(8)
        stationary_predicate_endpoint_q = Signal(4)
        stationary_predicate_report_id_q = Signal(8)
        stationary_predicate_report_length_q = Signal(7)

        state_memory = Memory(
            shape=STATE_LAYOUT,
            depth=layout_count,
            init=[],
            attrs={"ram_style": "distributed"},
        )
        m.submodules.layout_state = state_memory
        state_memory_read = state_memory.read_port(domain="usb")
        state_memory_write = state_memory.write_port(domain="usb")
        state_memory_read_address = Signal(range(layout_count))
        state_memory_write_enable = Signal()
        state_record = state_memory_read.data
        state_record_matches = (
            state_valid.bit_select(selected_state, 1)
            & (state_record.interface_number == captured_interface)
            & (state_record.endpoint_number == captured_endpoint)
            & (state_record.report_id == selected_report_id)
            & (state_record.descriptor_generation == snapshot_descriptor_generation)
            & (state_record.map_generation == snapshot_map_generation)
        )

        # One byte per address rather than a whole report per address.
        #
        # As 512 bits x 16 this was the single largest LUTRAM in the design:
        # ECP5 LUTRAM costs ceil(width/4) x ceil(depth/16) TRELLIS_DPR16X4, so
        # it scales with total bits and a wide-shallow shape spends hundreds of
        # LUTs. As 8 bits x 1024 it is the same 8,192 bits but lands in a single
        # 18 kb block RAM, of which only 27 of 56 were in use.
        #
        # The cost is that reads are now one byte per cycle from a synchronous
        # port, where before the whole report arrived in one read and bytes were
        # selected combinationally. ``stationary_fetch_index`` covers that by
        # running one address ahead of the byte being written.
        template_cache = Memory(
            shape=8,
            depth=layout_count * LIMITS["report_bytes"],
            init=[],
        )
        m.submodules.stationary_template_cache = template_cache
        template_cache_read = template_cache.read_port(domain="usb")
        template_cache_write = template_cache.write_port(domain="usb")
        template_cache_write_enable = Signal()
        m.d.comb += [
            # report_bytes is a power of two, so layout*64 + byte is a
            # concatenation rather than a multiply.
            template_cache_read.addr.eq(Cat(stationary_fetch_index, stationary_prefetch_index)),
            template_cache_write.addr.eq(Cat(cache_copy_index, selected_state)),
            template_cache_write.data.eq(buffer_native_byte),
            template_cache_write.en.eq(template_cache_write_enable),
        ]

        scan_record_matches = (
            store.active_valid
            & state_valid.bit_select(stationary_scan_index, 1)
            & (state_record.descriptor_generation == store.active_descriptor_generation)
            & (state_record.map_generation == store.active_generation)
        )
        scan_motion_live = state_motion_live.bit_select(stationary_scan_index, 1)
        scan_buttons_live = state_buttons_live.bit_select(stationary_scan_index, 1)
        scan_masks_live = state_masks_live.bit_select(stationary_scan_index, 1)
        scan_x = Mux(scan_record_matches & scan_motion_live, state_record.x, 0)
        scan_y = Mux(scan_record_matches & scan_motion_live, state_record.y, 0)
        scan_wheel = Mux(scan_record_matches & scan_motion_live, state_record.wheel, 0)
        scan_pan = Mux(scan_record_matches & scan_motion_live, state_record.pan, 0)
        scan_buttons = Mux(scan_record_matches & scan_buttons_live, state_record.buttons, 0)
        scan_masks = Mux(scan_record_matches & scan_masks_live, state_record.masks, 0)
        scan_click_active = scan_record_matches & scan_buttons_live & state_record.click_active
        scan_release_delta = Signal(32)
        m.d.comb += scan_release_delta.eq(
            self.accepted_report_count - state_record.click_release_target
        )
        scan_incoming_relative = (
            self.relative_valid
            & (self.relative_interface == state_record.interface_number)
            & (self.relative_endpoint == state_record.endpoint_number)
            & (self.relative_report_id == state_record.report_id)
            & (
                (((self.relative_flags & INJ_RELATIVE_FLAG_X) != 0) & (self.relative_x != 0))
                | (((self.relative_flags & INJ_RELATIVE_FLAG_Y) != 0) & (self.relative_y != 0))
                | (
                    ((self.relative_flags & INJ_RELATIVE_FLAG_WHEEL) != 0)
                    & (self.relative_wheel != 0)
                )
                | (((self.relative_flags & INJ_RELATIVE_FLAG_PAN) != 0) & (self.relative_pan != 0))
            )
        )
        scan_incoming_button_transition = (
            self.button_valid
            & (self.button_interface == state_record.interface_number)
            & (self.button_endpoint == state_record.endpoint_number)
            & (self.button_report_id == state_record.report_id)
            & (self.button_buttons != scan_buttons)
        )
        scan_incoming_mask_transition = (
            self.mask_valid
            & (self.mask_interface == state_record.interface_number)
            & (self.mask_endpoint == state_record.endpoint_number)
            & (self.mask_report_id == state_record.report_id)
            & (self.mask_buttons != scan_masks)
        )
        scan_motion_pending = scan_record_matches & (
            (scan_x != 0) | (scan_y != 0) | (scan_wheel != 0) | (scan_pan != 0)
        )
        scan_relative_pending = scan_record_matches & scan_incoming_relative
        scan_button_pending = scan_record_matches & scan_incoming_button_transition
        scan_mask_pending = scan_record_matches & scan_incoming_mask_transition
        scan_click_pending = scan_click_active & ~scan_release_delta[-1]

        relative_targets_report = (
            self.relative_valid
            & (self.relative_interface == captured_interface)
            & (self.relative_endpoint == captured_endpoint)
            & (self.relative_report_id == selected_report_id)
        )
        button_targets_report = (
            self.button_valid
            & (self.button_interface == captured_interface)
            & (self.button_endpoint == captured_endpoint)
            & (self.button_report_id == selected_report_id)
        )
        mask_targets_report = (
            self.mask_valid
            & (self.mask_interface == captured_interface)
            & (self.mask_endpoint == captured_endpoint)
            & (self.mask_report_id == selected_report_id)
        )

        working_x = Signal(signed(32))
        working_y = Signal(signed(32))
        working_wheel = Signal(signed(32))
        working_pan = Signal(signed(32))
        working_buttons = Signal(64)
        working_mask = Signal(64)
        captured_raw_x = Signal(signed(32))
        captured_raw_y = Signal(signed(32))
        captured_raw_wheel = Signal(signed(32))
        captured_raw_pan = Signal(signed(32))
        captured_raw_buttons = Signal(64)
        captured_raw_mask = Signal(64)
        captured_raw_sequence = Signal(16)
        captured_raw_click_active = Signal()
        captured_raw_click_release_target = Signal(32)
        transaction_relative = Signal()
        transaction_button = Signal()
        transaction_mask = Signal()
        transaction_clear = Signal()
        transaction_relative_admitted = Signal()
        transaction_relative_overflow = Signal()
        transaction_click_release = Signal()
        transaction_stationary = Signal()
        transaction_mapped = Signal()
        transaction_invalidated = Signal()
        transaction_sequence = Signal(16)
        output_started = Signal()
        emit_native = Signal()
        deferred_sof = Signal()
        state_write_click_release_target = Signal(32)

        captured_state_matches = transaction_relative
        captured_motion_live = transaction_button
        captured_buttons_live = transaction_mask
        captured_masks_live = transaction_clear
        captured_x = Mux(captured_state_matches & captured_motion_live, captured_raw_x, 0)
        captured_y = Mux(captured_state_matches & captured_motion_live, captured_raw_y, 0)
        captured_wheel = Mux(
            captured_state_matches & captured_motion_live,
            captured_raw_wheel,
            0,
        )
        captured_pan = Mux(captured_state_matches & captured_motion_live, captured_raw_pan, 0)
        captured_buttons = Mux(
            captured_state_matches & captured_buttons_live,
            captured_raw_buttons,
            0,
        )
        captured_mask = Mux(
            captured_state_matches & captured_masks_live,
            captured_raw_mask,
            0,
        )
        captured_sequence = Mux(captured_state_matches, captured_raw_sequence, 0)
        captured_click_active = (
            captured_state_matches & captured_buttons_live & captured_raw_click_active
        )
        captured_click_release_target = Mux(
            captured_state_matches & captured_buttons_live,
            captured_raw_click_release_target,
            0,
        )

        clear_motion = self.clear_valid & ((self.clear_flags & INJ_CLEAR_FLAG_MOTION) != 0)
        clear_buttons = self.clear_valid & ((self.clear_flags & INJ_CLEAR_FLAG_BUTTONS) != 0)
        clear_masks = self.clear_valid & ((self.clear_flags & INJ_CLEAR_FLAG_PHYSICAL_MASKS) != 0)

        command_x = Mux(
            relative_targets_report & ((self.relative_flags & INJ_RELATIVE_FLAG_X) != 0),
            self.relative_x,
            0,
        )
        command_y = Mux(
            relative_targets_report & ((self.relative_flags & INJ_RELATIVE_FLAG_Y) != 0),
            self.relative_y,
            0,
        )
        command_wheel = Mux(
            relative_targets_report & ((self.relative_flags & INJ_RELATIVE_FLAG_WHEEL) != 0),
            self.relative_wheel,
            0,
        )
        command_pan = Mux(
            relative_targets_report & ((self.relative_flags & INJ_RELATIVE_FLAG_PAN) != 0),
            self.relative_pan,
            0,
        )

        captured_click_release_delta = Signal(32)
        captured_click_release_due = Signal()
        m.d.comb += [
            captured_click_release_delta.eq(
                self.accepted_report_count - captured_click_release_target
            ),
            captured_click_release_due.eq(
                captured_click_active & ~captured_click_release_delta[-1]
            ),
        ]

        candidate_x = Signal(signed(33))
        candidate_y = Signal(signed(33))
        candidate_wheel = Signal(signed(33))
        candidate_pan = Signal(signed(33))
        candidate_buttons = Signal(64)
        candidate_mask = Signal(64)
        minimum_residual = Const(-(1 << 31), signed(33))
        maximum_residual = Const((1 << 31) - 1, signed(33))
        m.d.comb += [
            candidate_x.eq(Mux(clear_motion, command_x, captured_x + command_x)),
            candidate_y.eq(Mux(clear_motion, command_y, captured_y + command_y)),
            candidate_wheel.eq(Mux(clear_motion, command_wheel, captured_wheel + command_wheel)),
            candidate_pan.eq(Mux(clear_motion, command_pan, captured_pan + command_pan)),
            candidate_buttons.eq(
                Mux(
                    clear_buttons,
                    Mux(button_targets_report, self.button_buttons, 0),
                    Mux(
                        button_targets_report,
                        self.button_buttons,
                        Mux(captured_click_release_due, 0, captured_buttons),
                    ),
                )
            ),
            candidate_mask.eq(
                Mux(
                    clear_masks,
                    Mux(mask_targets_report, self.mask_buttons, 0),
                    Mux(mask_targets_report, self.mask_buttons, captured_mask),
                )
            ),
        ]
        relative_overflow = relative_targets_report & (
            (
                ((self.relative_flags & INJ_RELATIVE_FLAG_X) != 0)
                & ((candidate_x < minimum_residual) | (candidate_x > maximum_residual))
            )
            | (
                ((self.relative_flags & INJ_RELATIVE_FLAG_Y) != 0)
                & ((candidate_y < minimum_residual) | (candidate_y > maximum_residual))
            )
            | (
                ((self.relative_flags & INJ_RELATIVE_FLAG_WHEEL) != 0)
                & ((candidate_wheel < minimum_residual) | (candidate_wheel > maximum_residual))
            )
            | (
                ((self.relative_flags & INJ_RELATIVE_FLAG_PAN) != 0)
                & ((candidate_pan < minimum_residual) | (candidate_pan > maximum_residual))
            )
        )
        relative_admitted = relative_targets_report & ~relative_overflow

        state_write_click_active = Signal()
        selected_state_mask = Const(1, layout_count) << selected_state
        m.d.comb += [
            state_write_click_active.eq(transaction_click_release),
            state_memory_read.addr.eq(state_memory_read_address),
            state_memory_write.addr.eq(selected_state),
            state_memory_write.en.eq(state_memory_write_enable),
            state_memory_write.data.interface_number.eq(captured_interface),
            state_memory_write.data.endpoint_number.eq(captured_endpoint),
            state_memory_write.data.report_id.eq(selected_report_id),
            state_memory_write.data.descriptor_generation.eq(snapshot_descriptor_generation),
            state_memory_write.data.map_generation.eq(snapshot_map_generation),
            state_memory_write.data.x.eq(working_x),
            state_memory_write.data.y.eq(working_y),
            state_memory_write.data.wheel.eq(working_wheel),
            state_memory_write.data.pan.eq(working_pan),
            state_memory_write.data.buttons.eq(working_buttons),
            state_memory_write.data.masks.eq(working_mask),
            state_memory_write.data.sequence.eq(transaction_sequence),
            state_memory_write.data.report_length.eq(report_length),
            state_memory_write.data.click_active.eq(state_write_click_active),
            state_memory_write.data.click_release_target.eq(state_write_click_release_target),
        ]

        entry_index = Signal(range(LIMITS["fields"]))
        bit_index = Signal(range(33))
        raw_field = Signal(32)
        emitted_field = Signal(signed(32))
        pending_buffer_address = Signal(range(LIMITS["report_bytes"]))
        pending_buffer_word = Signal(16)

        entry = store.lookup_entry
        entry_matches_q = self._entry_matches_q
        entry_class_q = self._entry_class_q
        entry_bit_offset_q = self._entry_bit_offset_q
        entry_bit_width_q = self._entry_bit_width_q
        entry_usage_q = self._entry_usage_q
        entry_signed_q = self._entry_signed_q
        entry_axis_q = self._entry_axis_q
        entry_logical_minimum_q = self._entry_logical_minimum_q
        entry_logical_maximum_q = self._entry_logical_maximum_q
        field_total_q = self._field_total_q

        entry_snapshot_matches = (
            store.lookup_valid
            & (entry.interface_number == captured_interface)
            & (entry.endpoint_number == captured_endpoint)
            & (entry.report_id == selected_report_id)
            & (entry.report_length == report_length)
        )
        entry_snapshot_is_button = (entry.flags & INJ_MAP_ENTRY_FLAG_BUTTON) != 0
        entry_snapshot_is_relative = (entry.flags & INJ_MAP_ENTRY_FLAG_RELATIVE) != 0
        entry_snapshot_class = Mux(
            entry_snapshot_is_button,
            1,
            Mux(entry_snapshot_is_relative, 2, 0),
        )
        entry_snapshot_axis = Mux(
            (entry.flags & INJ_MAP_ENTRY_FLAG_X) != 0,
            0,
            Mux(
                (entry.flags & INJ_MAP_ENTRY_FLAG_Y) != 0,
                1,
                Mux((entry.flags & INJ_MAP_ENTRY_FLAG_WHEEL) != 0, 2, 3),
            ),
        )

        absolute_bit = entry_bit_offset_q + bit_index
        template_bit = buffer_native_byte.bit_select(absolute_bit[:3], 1)

        entry_sign_index = (entry_bit_width_q - 1).as_unsigned()
        entry_sign_bit = raw_field.bit_select(entry_sign_index, 1)
        physical_wide = Signal(signed(34))
        axis_injection = Signal(signed(32))
        total_wide = Signal(signed(34))
        minimum_wide = Signal(signed(34))
        maximum_wide = Signal(signed(34))
        clamped_wide = Signal(signed(34))
        residual_wide = Signal(signed(34))
        m.d.comb += [
            physical_wide.eq(
                Mux(
                    transaction_stationary,
                    0,
                    Mux(
                        entry_signed_q,
                        Mux(
                            entry_sign_bit,
                            raw_field - (Const(1, 34) << entry_bit_width_q),
                            raw_field,
                        ),
                        raw_field,
                    ),
                )
            ),
            axis_injection.eq(
                Mux(
                    entry_axis_q == 0,
                    working_x,
                    Mux(
                        entry_axis_q == 1,
                        working_y,
                        Mux(
                            entry_axis_q == 2,
                            working_wheel,
                            working_pan,
                        ),
                    ),
                )
            ),
            total_wide.eq(physical_wide + axis_injection),
            minimum_wide.eq(entry_logical_minimum_q),
            maximum_wide.eq(entry_logical_maximum_q),
            clamped_wide.eq(
                Mux(
                    field_total_q < minimum_wide,
                    minimum_wide,
                    Mux(
                        field_total_q > maximum_wide,
                        maximum_wide,
                        field_total_q,
                    ),
                )
            ),
            residual_wide.eq(field_total_q - clamped_wide),
        ]

        button_usage_index = (entry_usage_q + bit_index - 1).as_unsigned()
        physical_button = template_bit
        injected_button = working_buttons.bit_select(button_usage_index, 1)
        masked_button = working_mask.bit_select(button_usage_index, 1)
        merged_button = (physical_button & ~masked_button) | injected_button
        field_bit_mask = Const(1, 8) << absolute_bit[:3]
        inserted_working_byte = Mux(
            emitted_field.bit_select(bit_index, 1),
            buffer_working_byte | field_bit_mask,
            buffer_working_byte & ~field_bit_mask,
        )
        merged_working_byte = Mux(
            merged_button,
            buffer_working_byte | field_bit_mask,
            buffer_working_byte & ~field_bit_mask,
        )

        snapshot_matches = (
            store.active_valid
            & (store.active_descriptor_generation == snapshot_descriptor_generation)
            & (store.active_generation == snapshot_map_generation)
            & (store.active_bank == snapshot_bank)
        )

        m.d.comb += [
            store.layout_interface.eq(captured_interface),
            store.layout_endpoint.eq(captured_endpoint),
            store.layout_report_id.eq(layout_query_report_id),
            store.query_valid.eq(0),
            store.lookup_index.eq(entry_index),
            self.report_ready.eq(0),
            self.output_valid.eq(0),
            self.output_data.eq(Mux(emit_native, buffer_native_byte, buffer_working_byte)),
            self.output_first.eq(output_index == 0),
            self.output_last.eq(output_index == report_length - 1),
            self.output_interface.eq(captured_interface),
            self.output_endpoint.eq(captured_endpoint),
            self.output_report_id.eq(Mux(transaction_mapped, selected_report_id, 0)),
            self.relative_ready.eq(0),
            self.button_ready.eq(0),
            self.mask_ready.eq(0),
            self.clear_ready.eq(0),
            template_cache_write_enable.eq(0),
            state_memory_read_address.eq(selected_state),
            state_memory_write_enable.eq(0),
            self._stationary_scan_active.eq(0),
            self._stationary_scan_index.eq(stationary_scan_index),
            self._entry_snapshot_active.eq(0),
            self._entry_dispatch_active.eq(0),
            self._field_total_capture_active.eq(0),
            self._field_commit_active.eq(0),
            buffer_read_address.eq(0),
            buffer_read_enable.eq(0),
            buffer_write_address.eq(0),
            buffer_write_data.eq(0),
            buffer_write_enable.eq(0),
            report_buffer_read.addr.eq(buffer_read_address),
            report_buffer_read.en.eq(buffer_read_enable),
            report_buffer_write.addr.eq(buffer_write_address),
            report_buffer_write.data.eq(buffer_write_data),
            report_buffer_write.en.eq(buffer_write_enable),
        ]

        m.d.usb += self.command_committed.eq(0)

        with m.FSM(domain="usb"):
            with m.State("CAPTURE"):
                m.d.comb += self.report_ready.eq(self.report_valid & self.report_first)
                with m.If(self.report_valid & self.report_first):
                    m.d.comb += [
                        buffer_write_address.eq(0),
                        buffer_write_data.eq(Cat(self.report_data, self.report_data)),
                        buffer_write_enable.eq(1),
                    ]
                    m.d.usb += [
                        input_index.eq(0),
                        captured_first_byte.eq(self.report_data),
                        captured_interface.eq(self.report_interface),
                        captured_endpoint.eq(self.report_endpoint),
                        transaction_stationary.eq(0),
                    ]
                    with m.If(self.report_last):
                        m.d.usb += [
                            report_length.eq(1),
                            layout_query_report_id.eq(self.report_data),
                        ]
                        m.next = "QUERY_REPORT_ID_ISSUE"
                    with m.Else():
                        m.next = "CAPTURE_REST"
                with m.Elif(
                    (self.sof_tick | deferred_sof)
                    & store.active_valid
                    & (state_valid != 0)
                    # A zero-layout map has nothing to scan, and the scan's
                    # termination depends on there being at least one slot.
                    & (store.active_layout_count != 0)
                ):
                    m.d.comb += state_memory_read_address.eq(0)
                    m.d.usb += [
                        deferred_sof.eq(0),
                        stationary_scan_index.eq(0),
                        stationary_predicate_valid_q.eq(0),
                        stationary_decision_valid.eq(0),
                        snapshot_descriptor_generation.eq(store.active_descriptor_generation),
                        snapshot_map_generation.eq(store.active_generation),
                        snapshot_bank.eq(store.active_bank),
                        snapshot_entry_count.eq(store.active_entry_count),
                    ]
                    m.next = "STATIONARY_SCAN"

            with m.State("STATIONARY_SCAN"):
                # A zero-layout active map has no slot index that can equal
                # active_layout_count - 1, so the scan must terminate explicitly.
                # (active_layout_count is unsigned(5), so the subtraction widens to
                # signed(6) and the comparison against unsigned(4) is never true.)
                scan_last = (store.active_layout_count == 0) | (
                    stationary_scan_index == store.active_layout_count - 1
                )
                scan_next = Mux(scan_last, 0, stationary_scan_index + 1)
                m.d.comb += [
                    self._stationary_scan_active.eq(1),
                    state_memory_read_address.eq(scan_next),
                ]
                m.d.usb += [
                    stationary_scan_index.eq(scan_next),
                    stationary_predicate_valid_q.eq(1),
                    stationary_predicate_motion_q.eq(scan_motion_pending),
                    stationary_predicate_relative_q.eq(scan_relative_pending),
                    stationary_predicate_button_q.eq(scan_button_pending),
                    stationary_predicate_mask_q.eq(scan_mask_pending),
                    stationary_predicate_click_q.eq(scan_click_pending),
                    stationary_predicate_last_q.eq(scan_last),
                    stationary_predicate_index_q.eq(stationary_scan_index),
                    stationary_predicate_interface_q.eq(state_record.interface_number),
                    stationary_predicate_endpoint_q.eq(state_record.endpoint_number),
                    stationary_predicate_report_id_q.eq(state_record.report_id),
                    stationary_predicate_report_length_q.eq(state_record.report_length),
                    stationary_decision_valid.eq(stationary_predicate_valid_q),
                    stationary_decision_pending.eq(
                        stationary_predicate_motion_q
                        | stationary_predicate_relative_q
                        | stationary_predicate_button_q
                        | stationary_predicate_mask_q
                        | stationary_predicate_click_q
                    ),
                    stationary_decision_last.eq(stationary_predicate_last_q),
                    stationary_decision_index.eq(stationary_predicate_index_q),
                    stationary_decision_interface.eq(stationary_predicate_interface_q),
                    stationary_decision_endpoint.eq(stationary_predicate_endpoint_q),
                    stationary_decision_report_id.eq(stationary_predicate_report_id_q),
                    stationary_decision_report_length.eq(stationary_predicate_report_length_q),
                ]
                with m.If(~snapshot_matches):
                    m.next = "CAPTURE"
                with m.Elif(stationary_decision_valid & stationary_decision_pending):
                    m.d.usb += [
                        stationary_prefetch_index.eq(stationary_decision_index),
                        selected_state.eq(stationary_decision_index),
                        selected_state_available.eq(1),
                        captured_interface.eq(stationary_decision_interface),
                        captured_endpoint.eq(stationary_decision_endpoint),
                        selected_report_id.eq(stationary_decision_report_id),
                        layout_query_report_id.eq(stationary_decision_report_id),
                        report_length.eq(stationary_decision_report_length),
                        transaction_mapped.eq(1),
                        transaction_stationary.eq(1),
                        stationary_load_index.eq(0),
                        stationary_fetch_index.eq(0),
                    ]
                    m.next = "STATIONARY_PREFETCH"
                with m.Elif(stationary_decision_valid & stationary_decision_last):
                    m.next = "CAPTURE"
                with m.Else():
                    m.next = "STATIONARY_SCAN"

            with m.State("STATIONARY_PREFETCH"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    # Byte 0's address was presented on entry to this state, so
                    # its data lands with the first LOAD cycle. Step the address
                    # on so LOAD always has the next byte in flight.
                    m.d.usb += stationary_fetch_index.eq(1)
                    m.next = "STATIONARY_LOAD"

            with m.State("STATIONARY_LOAD"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    cached_byte = template_cache_read.data
                    m.d.comb += [
                        buffer_write_address.eq(stationary_load_index),
                        buffer_write_data.eq(Cat(cached_byte, cached_byte)),
                        buffer_write_enable.eq(1),
                    ]
                    with m.If(stationary_load_index == report_length - 1):
                        m.next = "BASE_CAPTURE"
                    with m.Else():
                        m.d.usb += [
                            stationary_load_index.eq(stationary_load_index + 1),
                            stationary_fetch_index.eq(stationary_fetch_index + 1),
                        ]

            with m.State("CAPTURE_REST"):
                m.d.comb += self.report_ready.eq(1)
                with m.If(self.report_valid):
                    m.d.comb += [
                        buffer_write_address.eq(input_index + 1),
                        buffer_write_data.eq(Cat(self.report_data, self.report_data)),
                        buffer_write_enable.eq(1),
                    ]
                    m.d.usb += [
                        input_index.eq(input_index + 1),
                    ]
                    with m.If(self.report_last):
                        m.d.usb += [
                            report_length.eq(input_index + 2),
                            layout_query_report_id.eq(captured_first_byte),
                        ]
                        m.next = "QUERY_REPORT_ID_ISSUE"

            with m.State("QUERY_REPORT_ID_ISSUE"):
                m.d.comb += store.query_valid.eq(1)
                with m.If(store.query_ready):
                    m.d.usb += [
                        snapshot_descriptor_generation.eq(store.active_descriptor_generation),
                        snapshot_map_generation.eq(store.active_generation),
                        snapshot_bank.eq(store.active_bank),
                        snapshot_entry_count.eq(store.active_entry_count),
                    ]
                    m.next = "QUERY_REPORT_ID_WAIT"

            with m.State("QUERY_REPORT_ID_WAIT"):
                with m.If(~snapshot_matches):
                    m.d.usb += [
                        output_index.eq(0),
                        output_started.eq(0),
                        emit_native.eq(1),
                        transaction_mapped.eq(0),
                        selected_state_available.eq(0),
                    ]
                    m.next = "OUTPUT_PRIME"
                with m.Elif(store.result_valid):
                    with m.If(store.layout_found & (store.layout_report_length == report_length)):
                        m.d.usb += [
                            selected_report_id.eq(layout_query_report_id),
                            selected_state.eq(store.layout_index),
                            selected_state_available.eq(1),
                            transaction_mapped.eq(1),
                        ]
                        m.next = "STATE_READ_WAIT"
                    with m.Elif(layout_query_report_id != 0):
                        m.d.usb += layout_query_report_id.eq(0)
                        m.next = "QUERY_NO_ID_ISSUE"
                    with m.Else():
                        m.d.usb += [
                            output_index.eq(0),
                            output_started.eq(0),
                            emit_native.eq(1),
                            transaction_mapped.eq(0),
                            selected_state_available.eq(0),
                        ]
                        m.next = "OUTPUT_PRIME"

            with m.State("QUERY_NO_ID_ISSUE"):
                with m.If(~snapshot_matches):
                    m.d.usb += [
                        output_index.eq(0),
                        output_started.eq(0),
                        emit_native.eq(1),
                        transaction_mapped.eq(0),
                        selected_state_available.eq(0),
                    ]
                    m.next = "OUTPUT_PRIME"
                with m.Else():
                    m.d.comb += store.query_valid.eq(1)
                    with m.If(store.query_ready):
                        m.next = "QUERY_NO_ID_WAIT"

            with m.State("QUERY_NO_ID_WAIT"):
                with m.If(~snapshot_matches):
                    m.d.usb += [
                        output_index.eq(0),
                        output_started.eq(0),
                        emit_native.eq(1),
                        transaction_mapped.eq(0),
                        selected_state_available.eq(0),
                    ]
                    m.next = "OUTPUT_PRIME"
                with m.Elif(store.result_valid):
                    with m.If(store.layout_found & (store.layout_report_length == report_length)):
                        m.d.usb += [
                            selected_report_id.eq(0),
                            selected_state.eq(store.layout_index),
                            selected_state_available.eq(1),
                            transaction_mapped.eq(1),
                        ]
                        m.next = "STATE_READ_WAIT"
                    with m.Else():
                        m.d.usb += [
                            output_index.eq(0),
                            output_started.eq(0),
                            emit_native.eq(1),
                            transaction_mapped.eq(0),
                            selected_state_available.eq(0),
                        ]
                        m.next = "OUTPUT_PRIME"

            with m.State("STATE_READ_WAIT"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.next = "BASE_CAPTURE"

            with m.State("BASE_CAPTURE"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += [
                        captured_raw_x.eq(state_record.x),
                        captured_raw_y.eq(state_record.y),
                        captured_raw_wheel.eq(state_record.wheel),
                        captured_raw_pan.eq(state_record.pan),
                        captured_raw_buttons.eq(state_record.buttons),
                        captured_raw_mask.eq(state_record.masks),
                        captured_raw_sequence.eq(state_record.sequence),
                        captured_raw_click_active.eq(state_record.click_active),
                        captured_raw_click_release_target.eq(state_record.click_release_target),
                        transaction_relative.eq(state_record_matches),
                        transaction_button.eq(state_motion_live.bit_select(selected_state, 1)),
                        transaction_mask.eq(state_buttons_live.bit_select(selected_state, 1)),
                        transaction_clear.eq(state_masks_live.bit_select(selected_state, 1)),
                    ]
                    m.next = "PREPARE"

            with m.State("PREPARE"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += [
                        working_x.eq(Mux(relative_overflow, captured_x, candidate_x)),
                        working_y.eq(Mux(relative_overflow, captured_y, candidate_y)),
                        working_wheel.eq(Mux(relative_overflow, captured_wheel, candidate_wheel)),
                        working_pan.eq(Mux(relative_overflow, captured_pan, candidate_pan)),
                        working_buttons.eq(candidate_buttons),
                        working_mask.eq(candidate_mask),
                        transaction_relative.eq(relative_targets_report),
                        transaction_relative_admitted.eq(relative_admitted),
                        transaction_relative_overflow.eq(relative_overflow),
                        transaction_button.eq(button_targets_report),
                        transaction_mask.eq(mask_targets_report),
                        transaction_clear.eq(self.clear_valid),
                        transaction_click_release.eq(
                            Mux(
                                button_targets_report,
                                (self.button_hold_reports != 0) & (self.button_buttons != 0),
                                Mux(
                                    captured_click_release_due | clear_buttons,
                                    0,
                                    captured_click_active,
                                ),
                            )
                        ),
                        transaction_invalidated.eq(0),
                        state_write_click_release_target.eq(
                            Mux(
                                button_targets_report,
                                self.accepted_report_count + self.button_hold_reports,
                                captured_click_release_target,
                            )
                        ),
                        transaction_sequence.eq(
                            Mux(
                                self.clear_valid,
                                self.clear_command_sequence,
                                Mux(
                                    mask_targets_report,
                                    self.mask_command_sequence,
                                    Mux(
                                        button_targets_report,
                                        self.button_command_sequence,
                                        Mux(
                                            relative_admitted,
                                            self.relative_command_sequence,
                                            captured_sequence,
                                        ),
                                    ),
                                ),
                            )
                        ),
                        entry_index.eq(0),
                    ]
                    with m.If(snapshot_entry_count == 0):
                        m.d.usb += [
                            output_index.eq(0),
                            output_started.eq(0),
                            emit_native.eq(0),
                        ]
                        m.next = "OUTPUT_PRIME"
                    with m.Else():
                        m.next = "LOOKUP_WAIT"

            with m.State("LOOKUP_WAIT"):
                # InjectionMapStore's synchronous read port and lookup-valid
                # pipeline both settle before this entry is consumed.
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.next = "CHECK_ENTRY"

            with m.State("CHECK_ENTRY"):
                m.d.comb += self._entry_snapshot_active.eq(1)
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += [
                        entry_matches_q.eq(entry_snapshot_matches),
                        entry_class_q.eq(entry_snapshot_class),
                        entry_bit_offset_q.eq(entry.bit_offset),
                        entry_bit_width_q.eq(entry.bit_width),
                        entry_usage_q.eq(entry.usage),
                        entry_signed_q.eq((entry.flags & INJ_MAP_ENTRY_FLAG_SIGNED) != 0),
                        entry_axis_q.eq(entry_snapshot_axis),
                        entry_logical_minimum_q.eq(entry.logical_minimum),
                        entry_logical_maximum_q.eq(entry.logical_maximum),
                    ]
                    m.next = "DISPATCH_ENTRY"

            with m.State("DISPATCH_ENTRY"):
                m.d.comb += self._entry_dispatch_active.eq(1)
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    with m.If(entry_matches_q & (entry_class_q == 1)):
                        m.d.usb += bit_index.eq(0)
                        m.next = "MERGE_BUTTON_READ"
                    with m.Elif(entry_matches_q & (entry_class_q == 2)):
                        m.d.usb += [bit_index.eq(0), raw_field.eq(0)]
                        m.next = "EXTRACT_FIELD_READ"
                    with m.Else():
                        m.next = "NEXT_ENTRY"

            with m.State("EXTRACT_FIELD_READ"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_read_address.eq(absolute_bit >> 3),
                        buffer_read_enable.eq(1),
                    ]
                    m.next = "EXTRACT_FIELD_CONSUME"

            with m.State("EXTRACT_FIELD_CONSUME"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += raw_field.bit_select(bit_index, 1).eq(template_bit)
                    with m.If(bit_index == entry_bit_width_q - 1):
                        m.next = "CALCULATE_FIELD"
                    with m.Else():
                        m.d.usb += bit_index.eq(bit_index + 1)
                        m.next = "EXTRACT_FIELD_READ"

            with m.State("CALCULATE_FIELD"):
                m.d.comb += self._field_total_capture_active.eq(1)
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += field_total_q.eq(total_wide)
                    m.next = "COMMIT_FIELD"

            with m.State("COMMIT_FIELD"):
                m.d.comb += self._field_commit_active.eq(1)
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += emitted_field.eq(clamped_wide)
                    with m.If(entry_axis_q == 0):
                        m.d.usb += working_x.eq(residual_wide)
                    with m.Elif(entry_axis_q == 1):
                        m.d.usb += working_y.eq(residual_wide)
                    with m.Elif(entry_axis_q == 2):
                        m.d.usb += working_wheel.eq(residual_wide)
                    with m.Else():
                        m.d.usb += working_pan.eq(residual_wide)
                    m.d.usb += bit_index.eq(0)
                    m.next = "INSERT_FIELD_READ"

            with m.State("INSERT_FIELD_READ"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_read_address.eq(absolute_bit >> 3),
                        buffer_read_enable.eq(1),
                    ]
                    m.next = "INSERT_FIELD_CONSUME"

            with m.State("INSERT_FIELD_CONSUME"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += [
                        pending_buffer_address.eq(absolute_bit >> 3),
                        pending_buffer_word.eq(Cat(buffer_native_byte, inserted_working_byte)),
                    ]
                    m.next = "INSERT_FIELD_WRITE"

            with m.State("INSERT_FIELD_WRITE"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_write_address.eq(pending_buffer_address),
                        buffer_write_data.eq(pending_buffer_word),
                        buffer_write_enable.eq(1),
                    ]
                    with m.If(bit_index == entry_bit_width_q - 1):
                        m.next = "NEXT_ENTRY"
                    with m.Else():
                        m.d.usb += bit_index.eq(bit_index + 1)
                        m.next = "INSERT_FIELD_READ"

            with m.State("MERGE_BUTTON_READ"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_read_address.eq(absolute_bit >> 3),
                        buffer_read_enable.eq(1),
                    ]
                    m.next = "MERGE_BUTTON_CONSUME"

            with m.State("MERGE_BUTTON_CONSUME"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.usb += [
                        pending_buffer_address.eq(absolute_bit >> 3),
                        pending_buffer_word.eq(
                            Cat(
                                buffer_native_byte,
                                Mux(
                                    (entry_usage_q >= 1) & (button_usage_index < 64),
                                    merged_working_byte,
                                    buffer_working_byte,
                                ),
                            )
                        ),
                    ]
                    m.next = "MERGE_BUTTON_WRITE"

            with m.State("MERGE_BUTTON_WRITE"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_write_address.eq(pending_buffer_address),
                        buffer_write_data.eq(pending_buffer_word),
                        buffer_write_enable.eq(1),
                    ]
                    with m.If(bit_index == entry_bit_width_q - 1):
                        m.next = "NEXT_ENTRY"
                    with m.Else():
                        m.d.usb += bit_index.eq(bit_index + 1)
                        m.next = "MERGE_BUTTON_READ"

            with m.State("NEXT_ENTRY"):
                with m.If(~snapshot_matches):
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    with m.If(entry_index == snapshot_entry_count - 1):
                        m.d.usb += [
                            output_index.eq(0),
                            output_started.eq(0),
                            emit_native.eq(0),
                        ]
                        m.next = "OUTPUT_PRIME"
                    with m.Else():
                        m.d.usb += entry_index.eq(entry_index + 1)
                        m.next = "LOOKUP_WAIT"

            with m.State("ABORT_MUTATION"):
                with m.If(transaction_stationary):
                    m.d.usb += [
                        transaction_relative.eq(0),
                        transaction_relative_admitted.eq(0),
                        transaction_relative_overflow.eq(0),
                        transaction_button.eq(0),
                        transaction_mask.eq(0),
                        transaction_clear.eq(0),
                        transaction_click_release.eq(0),
                        transaction_stationary.eq(0),
                        transaction_mapped.eq(0),
                        transaction_invalidated.eq(0),
                    ]
                    m.next = "CAPTURE"
                with m.Else():
                    m.d.usb += [
                        output_index.eq(0),
                        output_started.eq(0),
                        emit_native.eq(1),
                        transaction_relative.eq(0),
                        transaction_relative_admitted.eq(0),
                        transaction_relative_overflow.eq(0),
                        transaction_button.eq(0),
                        transaction_mask.eq(0),
                        transaction_clear.eq(0),
                        transaction_click_release.eq(0),
                        transaction_mapped.eq(0),
                        transaction_invalidated.eq(0),
                    ]
                    m.next = "OUTPUT_PRIME"

            with m.State("OUTPUT_PRIME"):
                snapshot_lost = transaction_mapped & ~snapshot_matches
                with m.If(snapshot_lost):
                    m.d.usb += transaction_invalidated.eq(1)
                    m.next = "ABORT_MUTATION"
                with m.Else():
                    m.d.comb += [
                        buffer_read_address.eq(0),
                        buffer_read_enable.eq(1),
                    ]
                    m.next = "OUTPUT"

            with m.State("OUTPUT"):
                snapshot_lost = transaction_mapped & ~snapshot_matches
                commit_allowed = transaction_mapped & ~transaction_invalidated & ~snapshot_lost
                m.d.comb += self.output_valid.eq(~(snapshot_lost & ~output_started))
                output_accept = self.output_ready & ~(snapshot_lost & ~output_started)
                final_accept = output_accept & self.output_last
                m.d.comb += [
                    self.relative_ready.eq(final_accept & commit_allowed & transaction_relative),
                    self.button_ready.eq(final_accept & commit_allowed & transaction_button),
                    self.mask_ready.eq(final_accept & commit_allowed & transaction_mask),
                    self.clear_ready.eq(final_accept & commit_allowed & transaction_clear),
                    state_memory_write_enable.eq(
                        final_accept & commit_allowed & selected_state_available
                    ),
                ]
                with m.If(snapshot_lost & output_started):
                    m.d.usb += transaction_invalidated.eq(1)
                with m.If(snapshot_lost & ~output_started):
                    m.d.usb += transaction_invalidated.eq(1)
                    m.next = "ABORT_MUTATION"
                with m.Elif(output_accept):
                    with m.If(self.output_last):
                        with m.If(commit_allowed & selected_state_available):
                            m.d.usb += [
                                state_valid.eq(state_valid | selected_state_mask),
                                state_motion_live.eq(
                                    Mux(
                                        transaction_clear
                                        & ((self.clear_flags & INJ_CLEAR_FLAG_MOTION) != 0),
                                        0,
                                        state_motion_live,
                                    )
                                    | selected_state_mask
                                ),
                                state_buttons_live.eq(
                                    Mux(
                                        transaction_clear
                                        & ((self.clear_flags & INJ_CLEAR_FLAG_BUTTONS) != 0),
                                        0,
                                        state_buttons_live,
                                    )
                                    | selected_state_mask
                                ),
                                state_masks_live.eq(
                                    Mux(
                                        transaction_clear
                                        & ((self.clear_flags & INJ_CLEAR_FLAG_PHYSICAL_MASKS) != 0),
                                        0,
                                        state_masks_live,
                                    )
                                    | selected_state_mask
                                ),
                                self.pending_x.eq(working_x),
                                self.pending_y.eq(working_y),
                                self.pending_wheel.eq(working_wheel),
                                self.pending_pan.eq(working_pan),
                                self.injected_buttons.eq(working_buttons),
                                self.physical_mask.eq(working_mask),
                                self.last_committed_command_sequence.eq(transaction_sequence),
                            ]
                        with m.If(
                            commit_allowed
                            & selected_state_available
                            & transaction_relative_overflow
                            & (self.command_overflow != (1 << 32) - 1)
                        ):
                            m.d.usb += self.command_overflow.eq(self.command_overflow + 1)
                        with m.If(
                            commit_allowed
                            & (
                                transaction_relative_admitted
                                | transaction_button
                                | transaction_mask
                                | transaction_clear
                            )
                        ):
                            m.d.usb += self.command_committed.eq(1)
                        m.d.usb += [
                            transaction_relative.eq(0),
                            transaction_relative_admitted.eq(0),
                            transaction_relative_overflow.eq(0),
                            transaction_button.eq(0),
                            transaction_mask.eq(0),
                            transaction_clear.eq(0),
                            transaction_click_release.eq(0),
                            transaction_stationary.eq(0),
                            transaction_mapped.eq(0),
                            transaction_invalidated.eq(0),
                            output_started.eq(0),
                        ]
                        with m.If(
                            commit_allowed & selected_state_available & ~transaction_stationary
                        ):
                            m.d.usb += cache_copy_index.eq(0)
                            m.next = "CACHE_COPY_PRIME"
                        with m.Else():
                            m.next = "CAPTURE"
                    with m.Else():
                        m.d.comb += [
                            buffer_read_address.eq(output_index + 1),
                            buffer_read_enable.eq(1),
                        ]
                        m.d.usb += [
                            output_index.eq(output_index + 1),
                            output_started.eq(1),
                            transaction_invalidated.eq(transaction_invalidated | snapshot_lost),
                        ]

            with m.State("CACHE_COPY_PRIME"):
                m.d.comb += [
                    buffer_read_address.eq(0),
                    buffer_read_enable.eq(1),
                ]
                with m.If(self.sof_tick):
                    m.d.usb += deferred_sof.eq(1)
                m.next = "CACHE_COPY"

            with m.State("CACHE_COPY"):
                m.d.comb += template_cache_write_enable.eq(1)
                with m.If(self.sof_tick):
                    m.d.usb += deferred_sof.eq(1)
                with m.If(cache_copy_index == report_length - 1):
                    m.next = "CAPTURE"
                with m.Else():
                    m.d.comb += [
                        buffer_read_address.eq(cache_copy_index + 1),
                        buffer_read_enable.eq(1),
                    ]
                    m.d.usb += cache_copy_index.eq(cache_copy_index + 1)

        with m.If(store.invalidate):
            m.d.usb += [
                state_valid.eq(0),
                state_motion_live.eq(0),
                state_buttons_live.eq(0),
                state_masks_live.eq(0),
            ]

        return m
