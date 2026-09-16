"""Cynthion r1.4 top level for the bounded mouse host."""

# Ruff's context-manager simplification obscures nested Amaranth control-flow DSL structure.
# ruff: noqa: SIM117

import argparse
from pathlib import Path

from amaranth import Elaboratable, Module, Mux, Signal
from amaranth.back import rtlil
from amaranth.build import Attrs, Pins, Resource, Subsignal
from amaranth.lib import io
from amaranth.lib.cdc import FFSynchronizer
from luna.gateware.interface.ulpi import UTMITranslator

from .build_env import apply_build_environment
from .debug_block import DebugRegisterBlock
from .debug_regs import report_injection_register_map
from .descriptor_export import DescriptorExportEngine
from .device import MouseCloneDevice
from .host import BoundedMouseHost
from .injection import ReportInjectionEngine
from .injection_map import InjectionMapStore, MapError
from .injection_wire import (
    INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED,
    INJ_MAP_STATUS_STATUS_REJECTED,
    INJ_TYPE_BUTTON_STATE,
    INJ_TYPE_CLEAR,
    INJ_TYPE_MAP_BEGIN,
    INJ_TYPE_MAP_COMMIT,
    INJ_TYPE_MAP_ENTRY,
    INJ_TYPE_MAP_STATUS,
    INJ_TYPE_PHYSICAL_MASK,
    INJ_TYPE_RELATIVE,
    MAX_PAYLOAD,
)
from .report_monitor import ReportMonitor
from .spi_link import SPISlotMaster
from .types import HostError


class ReportInjectionDataPlane(Elaboratable):
    """Production report-injection wiring at a decoded fixed-slot boundary."""

    def __init__(self, descriptor_store, *, max_fields: int = 64) -> None:
        self.descriptor_store = descriptor_store

        # Authoritative native report input.
        self.report_valid = Signal()
        self.report_ready = Signal()
        self.report_data = Signal(8)
        self.report_first = Signal()
        self.report_last = Signal()
        self.report_interface = Signal(8)
        self.report_endpoint = Signal(4)

        # Authoritative output to the clone relay.
        self.output_valid = Signal()
        self.output_ready = Signal()
        self.output_data = Signal(8)
        self.output_first = Signal()
        self.output_last = Signal()
        self.output_interface = Signal(8)
        self.output_endpoint = Signal(4)
        self.output_report_id = Signal(8)

        self.sof_tick = Signal()
        self.session_active = Signal()
        self.link_ready = Signal()

        # Decoded fixed-slot receive interface.
        self.rx_valid = Signal()
        self.rx_ready = Signal()
        self.rx_type = Signal(8)
        self.rx_sequence = Signal(8)
        self.rx_payload_address = Signal(5)
        self.rx_payload_read_enable = Signal()
        self.rx_payload_data = Signal(8)

        # One-message fixed-slot transmit interface.
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.tx_type = Signal(8)
        self.tx_fill_start = Signal()
        self.tx_payload_request = Signal()
        self.tx_payload_address = Signal(5)
        self.tx_payload_response = Signal()
        self.tx_payload_data = Signal(8)

        self.map_store = InjectionMapStore(max_fields=max_fields)
        self.engine = ReportInjectionEngine(self.map_store)
        self.monitor = ReportMonitor(depth=2)
        self.descriptor_export = DescriptorExportEngine(descriptor_store)

        # Saturating integration diagnostics.
        self.accepted_report_count = Signal(32)
        self.native_report_count = Signal(32)
        self.mutated_report_count = Signal(32)
        self.synthesized_report_count = Signal(32)
        self.command_commit_count = Signal(32)
        self.invalid_rx_count = Signal(32)
        self.link_loss_count = Signal(32)
        self.telemetry_drop_count = Signal(32)
        self.duplicate_rx_count = Signal(32)
        self.stale_rx_count = Signal(32)
        self.sequence_gap_count = Signal(32)
        self.rx_sequence_valid = Signal()
        self.last_rx_sequence = Signal(8)
        self.sequence_class_valid = Signal()
        self.sequence_class_allowed = Signal()
        self.sequence_class_duplicate = Signal()
        self.sequence_class_stale = Signal()
        self.sequence_class_gap = Signal()

    def elaborate(self, platform) -> Module:
        del platform
        m = Module()
        m.submodules.map_store = map_store = self.map_store
        m.submodules.engine = engine = self.engine
        m.submodules.monitor = monitor = self.monitor
        m.submodules.descriptor_export = descriptor_export = self.descriptor_export
        store = self.descriptor_store

        link_ready_d = Signal()
        session_active_d = Signal()
        map_receiving = Signal()
        export_pending = Signal()
        native_transaction = Signal()

        # Taken from the store rather than re-derived here. The equivalent
        # comparison (generation != a local delayed copy) is cycle-identical,
        # but it placed a 16-bit comparator -- fed by a bus from the far side
        # of the die -- at the head of `invalidate`, `tx_valid` and `tx_abort`.
        descriptor_changed = store.descriptor_generation_changed
        link_lost = link_ready_d & ~self.link_ready
        invalidate = ~self.link_ready | ~self.session_active | descriptor_changed

        # Copy the link's byte-addressed RX payload into one local decoder
        # staging register. The link queue is released only after all 26 bytes
        # have been captured; loss invalidates both partial and complete state.
        rx_staged_valid = Signal()
        rx_staged_type = Signal(8)
        rx_staged_sequence = Signal(8)
        rx_staged_payload = Signal(MAX_PAYLOAD * 8)
        rx_load_active = Signal()
        rx_load_primed = Signal()
        rx_load_index = Signal(5)
        rx_load_last = rx_load_active & rx_load_primed & (rx_load_index == MAX_PAYLOAD - 1)
        rx_sequence_delta = Signal(8)
        m.d.comb += [
            rx_sequence_delta.eq(rx_staged_sequence - self.last_rx_sequence),
            self.rx_payload_read_enable.eq(rx_load_active),
            self.rx_payload_address.eq(
                Mux(
                    rx_load_primed & (rx_load_index != MAX_PAYLOAD - 1),
                    rx_load_index + 1,
                    rx_load_index,
                )
            ),
            self.rx_ready.eq(invalidate | rx_load_last),
        ]
        with m.If(invalidate):
            m.d.usb += [
                rx_staged_valid.eq(0),
                rx_load_active.eq(0),
                rx_load_primed.eq(0),
                rx_load_index.eq(0),
                self.sequence_class_valid.eq(0),
                self.sequence_class_allowed.eq(0),
                self.sequence_class_duplicate.eq(0),
                self.sequence_class_stale.eq(0),
                self.sequence_class_gap.eq(0),
            ]
        with m.Elif(rx_load_active):
            with m.If(~self.rx_valid):
                m.d.usb += [
                    rx_load_active.eq(0),
                    rx_load_primed.eq(0),
                    rx_load_index.eq(0),
                ]
            with m.Elif(~rx_load_primed):
                m.d.usb += rx_load_primed.eq(1)
            with m.Else():
                m.d.usb += rx_staged_payload.word_select(rx_load_index, 8).eq(self.rx_payload_data)
                with m.If(rx_load_index == MAX_PAYLOAD - 1):
                    m.d.usb += [
                        rx_staged_valid.eq(1),
                        rx_load_active.eq(0),
                        rx_load_primed.eq(0),
                        rx_load_index.eq(0),
                        self.sequence_class_valid.eq(1),
                        self.sequence_class_allowed.eq(
                            ~self.rx_sequence_valid
                            | ((rx_sequence_delta != 0) & ~rx_sequence_delta[7])
                        ),
                        self.sequence_class_duplicate.eq(
                            self.rx_sequence_valid & (rx_sequence_delta == 0)
                        ),
                        self.sequence_class_stale.eq(self.rx_sequence_valid & rx_sequence_delta[7]),
                        self.sequence_class_gap.eq(
                            self.rx_sequence_valid & (rx_sequence_delta > 1) & ~rx_sequence_delta[7]
                        ),
                    ]
                with m.Else():
                    m.d.usb += rx_load_index.eq(rx_load_index + 1)
        with m.Elif(self.rx_valid & ~rx_staged_valid):
            m.d.usb += [
                rx_staged_type.eq(self.rx_type),
                rx_staged_sequence.eq(self.rx_sequence),
                rx_load_active.eq(1),
                rx_load_primed.eq(0),
                rx_load_index.eq(0),
            ]

        # The descriptor exporter is retriggered after each new live
        # descriptor/session/link epoch. A pending request is bounded to one.
        export_trigger = (
            self.link_ready
            & self.session_active
            & (~link_ready_d | ~session_active_d | descriptor_changed)
        )
        export_start = (
            export_pending
            & self.link_ready
            & self.session_active
            & ~descriptor_export.busy
            & ~store.clear
        )
        m.d.comb += descriptor_export.start.eq(export_start)
        with m.If(export_trigger):
            m.d.usb += export_pending.eq(1)
        with m.Elif(export_start):
            m.d.usb += export_pending.eq(0)

        m.d.usb += [
            link_ready_d.eq(self.link_ready),
            session_active_d.eq(self.session_active),
        ]

        # Decode fields directly from the frozen little-endian 26-byte payload.
        rx_descriptor_generation = rx_staged_payload[0:16]
        rx_map_generation = rx_staged_payload[16:32]
        rx_entry_count = rx_staged_payload[32:40]
        rx_layout_count = rx_staged_payload[40:48]
        rx_entries_crc32 = rx_staged_payload[64:96]

        is_map_begin = rx_staged_type == INJ_TYPE_MAP_BEGIN
        is_map_entry = rx_staged_type == INJ_TYPE_MAP_ENTRY
        is_map_commit = rx_staged_type == INJ_TYPE_MAP_COMMIT
        is_relative = rx_staged_type == INJ_TYPE_RELATIVE
        is_button = rx_staged_type == INJ_TYPE_BUTTON_STATE
        is_mask = rx_staged_type == INJ_TYPE_PHYSICAL_MASK
        is_clear = rx_staged_type == INJ_TYPE_CLEAR
        is_command = is_relative | is_button | is_mask | is_clear
        command_fresh = (
            self.link_ready
            & self.session_active
            & map_store.active_valid
            & (rx_map_generation == map_store.active_generation)
        )

        command_ready = Mux(
            is_relative,
            engine.relative_ready,
            Mux(
                is_button,
                engine.button_ready,
                Mux(is_mask, engine.mask_ready, engine.clear_ready),
            ),
        )
        supported_rx = is_map_begin | is_map_entry | is_map_commit | is_command
        map_message_allowed = (
            (is_map_begin & ~map_store.busy)
            | (is_map_entry & map_receiving)
            | (is_map_commit & map_receiving)
        )
        decoded_ready = Signal()
        m.d.comb += decoded_ready.eq(
            Mux(
                ~self.link_ready | ~self.session_active,
                1,
                Mux(
                    self.sequence_class_valid,
                    Mux(
                        self.sequence_class_allowed,
                        Mux(
                            is_command,
                            Mux(command_fresh, command_ready, 1),
                            1,
                        ),
                        1,
                    ),
                    0,
                ),
            )
        )
        rx_accept = rx_staged_valid & decoded_ready & ~invalidate
        begin_accept = rx_accept & self.sequence_class_allowed & is_map_begin & ~map_store.busy
        entry_accept = rx_accept & self.sequence_class_allowed & is_map_entry & map_receiving
        commit_accept = rx_accept & self.sequence_class_allowed & is_map_commit & map_receiving
        # Every in-window frame advances the window, whether or not its *content*
        # is acted on. Advancing only on accepted content freezes last_rx_sequence
        # while the sender keeps incrementing, so after 127 content-rejected frames
        # the delta reaches 0x80 and every subsequent frame classifies STALE - a
        # 129-frame blackout with no recovery but sender wrap. "Act on" and
        # "advance the window" are distinct predicates; the field is named
        # last_rx_sequence ("last received"), which settles which one this is.
        #
        # rx_accept alone would be wrong: that would let a STALE frame drag the
        # window backwards and reopen already-consumed sequences.
        sequence_consumed = rx_accept & self.sequence_class_allowed
        invalid_rx = rx_accept & (
            ~self.sequence_class_allowed
            | ~supported_rx
            | ((is_map_begin | is_map_entry | is_map_commit) & ~map_message_allowed)
            | (is_command & ~command_fresh)
        )

        with m.If(invalidate):
            m.d.usb += map_receiving.eq(0)
        with m.Elif(begin_accept):
            m.d.usb += map_receiving.eq(1)
        with m.Elif(commit_accept):
            m.d.usb += map_receiving.eq(0)

        m.d.comb += [
            map_store.begin.eq(begin_accept),
            map_store.entry_valid.eq(entry_accept),
            map_store.entry.as_value().eq(rx_staged_payload),
            map_store.commit.eq(commit_accept),
            map_store.abort.eq(invalidate),
            map_store.invalidate.eq(invalidate),
            map_store.descriptor_generation.eq(store.descriptor_generation),
            map_store.candidate_descriptor_generation.eq(rx_descriptor_generation),
            map_store.candidate_map_generation.eq(rx_map_generation),
            map_store.candidate_entry_count.eq(rx_entry_count),
            map_store.candidate_layout_count.eq(rx_layout_count),
            map_store.candidate_entries_crc32.eq(rx_entries_crc32),
        ]

        # Commands remain asserted while the slot master's one-item RX queue is
        # held. The engine's ready pulse is the transactional final-report ack.
        m.d.comb += [
            engine.relative_valid.eq(
                rx_staged_valid
                & self.sequence_class_valid
                & self.sequence_class_allowed
                & is_relative
                & command_fresh
            ),
            engine.relative_interface.eq(rx_staged_payload[64:72]),
            engine.relative_endpoint.eq(rx_staged_payload[72:80]),
            engine.relative_report_id.eq(rx_staged_payload[80:88]),
            engine.relative_flags.eq(rx_staged_payload[88:92]),
            engine.relative_x.eq(rx_staged_payload[96:112].as_signed()),
            engine.relative_y.eq(rx_staged_payload[112:128].as_signed()),
            engine.relative_wheel.eq(rx_staged_payload[128:144].as_signed()),
            engine.relative_pan.eq(rx_staged_payload[144:160].as_signed()),
            engine.relative_command_sequence.eq(rx_staged_payload[32:48]),
            engine.button_valid.eq(
                rx_staged_valid
                & self.sequence_class_valid
                & self.sequence_class_allowed
                & is_button
                & command_fresh
            ),
            engine.button_interface.eq(rx_staged_payload[64:72]),
            engine.button_endpoint.eq(rx_staged_payload[72:80]),
            engine.button_report_id.eq(rx_staged_payload[80:88]),
            engine.button_buttons.eq(rx_staged_payload[96:160]),
            engine.button_hold_reports.eq(rx_staged_payload[160:176]),
            engine.button_command_sequence.eq(rx_staged_payload[32:48]),
            engine.mask_valid.eq(
                rx_staged_valid
                & self.sequence_class_valid
                & self.sequence_class_allowed
                & is_mask
                & command_fresh
            ),
            engine.mask_interface.eq(rx_staged_payload[64:72]),
            engine.mask_endpoint.eq(rx_staged_payload[72:80]),
            engine.mask_report_id.eq(rx_staged_payload[80:88]),
            engine.mask_buttons.eq(rx_staged_payload[96:160]),
            engine.mask_command_sequence.eq(rx_staged_payload[32:48]),
            engine.clear_valid.eq(
                rx_staged_valid
                & self.sequence_class_valid
                & self.sequence_class_allowed
                & is_clear
                & command_fresh
            ),
            engine.clear_flags.eq(rx_staged_payload[64:80]),
            engine.clear_command_sequence.eq(rx_staged_payload[32:48]),
        ]

        # Authoritative path: host -> transactional engine -> transparent
        # monitor -> clone relay. No telemetry signal enters this ready chain.
        m.d.comb += [
            engine.report_valid.eq(self.report_valid),
            engine.report_data.eq(self.report_data),
            engine.report_first.eq(self.report_first),
            engine.report_last.eq(self.report_last),
            engine.report_interface.eq(self.report_interface),
            engine.report_endpoint.eq(self.report_endpoint),
            self.report_ready.eq(engine.report_ready),
            engine.sof_tick.eq(self.sof_tick),
            engine.accepted_report_count.eq(self.accepted_report_count),
            monitor.report_valid.eq(engine.output_valid),
            monitor.report_data.eq(engine.output_data),
            monitor.report_first.eq(engine.output_first),
            monitor.report_last.eq(engine.output_last),
            monitor.report_interface.eq(engine.output_interface),
            monitor.report_endpoint.eq(engine.output_endpoint),
            monitor.report_id.eq(engine.output_report_id),
            monitor.descriptor_generation.eq(store.descriptor_generation),
            engine.output_ready.eq(monitor.report_ready),
            monitor.output_ready.eq(self.output_ready),
            self.output_valid.eq(monitor.output_valid),
            self.output_data.eq(monitor.output_data),
            self.output_first.eq(monitor.output_first),
            self.output_last.eq(monitor.output_last),
            self.output_interface.eq(monitor.output_interface),
            self.output_endpoint.eq(monitor.output_endpoint),
            self.output_report_id.eq(monitor.output_report_id),
        ]

        native_input_start = engine.report_valid & engine.report_ready & engine.report_first
        output_accept = monitor.output_valid & monitor.output_ready
        report_accept = output_accept & monitor.output_last
        command_commit = report_accept & (
            engine.relative_ready | engine.button_ready | engine.mask_ready | engine.clear_ready
        )
        with m.If(native_input_start):
            m.d.usb += native_transaction.eq(1)
        with m.If(report_accept):
            m.d.usb += native_transaction.eq(0)
            with m.If(self.accepted_report_count != 0xFFFF_FFFF):
                m.d.usb += self.accepted_report_count.eq(self.accepted_report_count + 1)
            with m.If(native_transaction):
                with m.If(self.native_report_count != 0xFFFF_FFFF):
                    m.d.usb += self.native_report_count.eq(self.native_report_count + 1)
            with m.Else():
                with m.If(self.synthesized_report_count != 0xFFFF_FFFF):
                    m.d.usb += self.synthesized_report_count.eq(self.synthesized_report_count + 1)
        with m.If(command_commit):
            with m.If(self.command_commit_count != 0xFFFF_FFFF):
                m.d.usb += self.command_commit_count.eq(self.command_commit_count + 1)
            with m.If(self.mutated_report_count != 0xFFFF_FFFF):
                m.d.usb += self.mutated_report_count.eq(self.mutated_report_count + 1)
        with m.If(invalid_rx & (self.invalid_rx_count != 0xFFFF_FFFF)):
            m.d.usb += self.invalid_rx_count.eq(self.invalid_rx_count + 1)
        with m.If(link_lost & (self.link_loss_count != 0xFFFF_FFFF)):
            m.d.usb += self.link_loss_count.eq(self.link_loss_count + 1)
        with m.If(self.sequence_class_duplicate & rx_accept):
            with m.If(self.duplicate_rx_count != 0xFFFF_FFFF):
                m.d.usb += self.duplicate_rx_count.eq(self.duplicate_rx_count + 1)
        with m.If(self.sequence_class_stale & rx_accept):
            with m.If(self.stale_rx_count != 0xFFFF_FFFF):
                m.d.usb += self.stale_rx_count.eq(self.stale_rx_count + 1)
        with m.If(invalidate):
            m.d.usb += self.rx_sequence_valid.eq(0)
        with m.Elif(sequence_consumed):
            m.d.usb += [
                self.rx_sequence_valid.eq(1),
                self.last_rx_sequence.eq(rx_staged_sequence),
            ]
            with m.If(self.sequence_class_gap & (self.sequence_gap_count != 0xFFFF_FFFF)):
                m.d.usb += self.sequence_gap_count.eq(self.sequence_gap_count + 1)
        with m.If(rx_accept):
            m.d.usb += [
                rx_staged_valid.eq(0),
                self.sequence_class_valid.eq(0),
                self.sequence_class_allowed.eq(0),
                self.sequence_class_duplicate.eq(0),
                self.sequence_class_stale.eq(0),
                self.sequence_class_gap.eq(0),
            ]

        # Hold one MAP_STATUS result while stalled. Descriptor and report
        # fragments already hold stable under their valid/ready contracts.
        candidate_map_generation = Signal(16)
        candidate_entries_crc32 = Signal(32)
        status_pending = Signal()
        status_descriptor_generation = Signal(16)
        status_map_generation = Signal(16)
        status_active_map_generation = Signal(16)
        status_entry_index = Signal(8)
        status_code = Signal(8)
        status_error = Signal(8)
        status_entries_crc32 = Signal(32)
        with m.If(begin_accept | commit_accept):
            m.d.usb += [
                candidate_map_generation.eq(rx_map_generation),
                candidate_entries_crc32.eq(rx_entries_crc32),
            ]

        _TX_STATUS = 0
        _TX_DESCRIPTOR = 1
        _TX_MONITOR = 2
        tx_owner = Signal(range(3))
        tx_owner_locked = Signal()
        tx_request_pending = Signal()
        tx_request_owner = Signal(range(3))
        tx_request_address = Signal(5)
        status_response_q = Signal()
        priority_status = status_pending
        priority_descriptor = ~priority_status & descriptor_export.message_valid
        priority_monitor = (
            ~priority_status & ~descriptor_export.message_valid & monitor.message_valid
        )
        locked_status_valid = (tx_owner == _TX_STATUS) & status_pending
        locked_descriptor_valid = (tx_owner == _TX_DESCRIPTOR) & descriptor_export.message_valid
        locked_monitor_valid = (tx_owner == _TX_MONITOR) & monitor.message_valid
        status_selected = Mux(tx_owner_locked, locked_status_valid, priority_status)
        descriptor_selected = Mux(tx_owner_locked, locked_descriptor_valid, priority_descriptor)
        monitor_selected = Mux(tx_owner_locked, locked_monitor_valid, priority_monitor)
        session_lost = session_active_d & ~self.session_active
        tx_abort = tx_owner_locked & (
            ~self.link_ready
            | session_lost
            | descriptor_changed
            | ~(locked_status_valid | locked_descriptor_valid | locked_monitor_valid)
        )
        tx_request_issue = (
            tx_owner_locked & self.tx_payload_request & ~tx_request_pending & ~tx_abort
        )
        status_request = tx_request_issue & (tx_owner == _TX_STATUS)
        descriptor_request = tx_request_issue & (tx_owner == _TX_DESCRIPTOR)
        monitor_request = tx_request_issue & (tx_owner == _TX_MONITOR)

        status_payload_data = Signal(8)
        m.d.comb += status_payload_data.eq(0)
        with m.Switch(tx_request_address):
            with m.Case(0):
                m.d.comb += status_payload_data.eq(status_descriptor_generation[:8])
            with m.Case(1):
                m.d.comb += status_payload_data.eq(status_descriptor_generation[8:16])
            with m.Case(2):
                m.d.comb += status_payload_data.eq(status_map_generation[:8])
            with m.Case(3):
                m.d.comb += status_payload_data.eq(status_map_generation[8:16])
            with m.Case(4):
                m.d.comb += status_payload_data.eq(status_active_map_generation[:8])
            with m.Case(5):
                m.d.comb += status_payload_data.eq(status_active_map_generation[8:16])
            with m.Case(6):
                m.d.comb += status_payload_data.eq(status_entry_index)
            with m.Case(7):
                m.d.comb += status_payload_data.eq(status_code)
            with m.Case(8):
                m.d.comb += status_payload_data.eq(status_error)
            with m.Case(10):
                m.d.comb += status_payload_data.eq(status_entries_crc32[:8])
            with m.Case(11):
                m.d.comb += status_payload_data.eq(status_entries_crc32[8:16])
            with m.Case(12):
                m.d.comb += status_payload_data.eq(status_entries_crc32[16:24])
            with m.Case(13):
                m.d.comb += status_payload_data.eq(status_entries_crc32[24:32])

        selected_response = Mux(
            tx_request_owner == _TX_STATUS,
            status_response_q,
            Mux(
                tx_request_owner == _TX_DESCRIPTOR,
                descriptor_export.message_payload_response,
                monitor.message_payload_response,
            ),
        )
        selected_data = Mux(
            tx_request_owner == _TX_STATUS,
            status_payload_data,
            Mux(
                tx_request_owner == _TX_DESCRIPTOR,
                descriptor_export.message_payload_data,
                monitor.message_payload_data,
            ),
        )
        response_matches = (
            tx_owner_locked
            & tx_request_pending
            & (tx_owner == tx_request_owner)
            & (self.tx_payload_address == tx_request_address)
            & selected_response
            & ~tx_abort
        )
        m.d.comb += [
            self.tx_valid.eq(
                self.link_ready
                & ~session_lost
                & ~descriptor_changed
                & (status_selected | descriptor_selected | monitor_selected)
            ),
            self.tx_type.eq(
                Mux(
                    status_selected,
                    INJ_TYPE_MAP_STATUS,
                    Mux(
                        descriptor_selected,
                        descriptor_export.message_type,
                        monitor.message_type,
                    ),
                )
            ),
            self.tx_payload_response.eq(response_matches),
            self.tx_payload_data.eq(selected_data),
            descriptor_export.message_payload_request.eq(descriptor_request),
            descriptor_export.message_payload_address.eq(self.tx_payload_address),
            descriptor_export.message_payload_cancel.eq(tx_abort),
            monitor.message_payload_request.eq(monitor_request),
            monitor.message_payload_address.eq(self.tx_payload_address),
            monitor.message_payload_cancel.eq(tx_abort),
            descriptor_export.message_ready.eq(
                tx_owner_locked & (tx_owner == _TX_DESCRIPTOR) & self.tx_ready
            ),
            monitor.message_ready.eq(tx_owner_locked & (tx_owner == _TX_MONITOR) & self.tx_ready),
        ]
        with m.If(tx_abort):
            m.d.usb += [
                tx_owner_locked.eq(0),
                tx_request_pending.eq(0),
                status_response_q.eq(0),
            ]
        with m.Elif(self.tx_fill_start & ~tx_owner_locked):
            m.d.usb += [
                tx_owner.eq(
                    Mux(
                        priority_status,
                        _TX_STATUS,
                        Mux(priority_descriptor, _TX_DESCRIPTOR, _TX_MONITOR),
                    )
                ),
                tx_owner_locked.eq(1),
                tx_request_pending.eq(0),
                status_response_q.eq(0),
            ]
        with m.Elif(tx_owner_locked & self.tx_ready):
            m.d.usb += [
                tx_owner_locked.eq(0),
                tx_request_pending.eq(0),
                status_response_q.eq(0),
            ]
        with m.Else():
            m.d.usb += status_response_q.eq(status_request)
            with m.If(tx_request_issue):
                m.d.usb += [
                    tx_request_pending.eq(1),
                    tx_request_owner.eq(tx_owner),
                    tx_request_address.eq(self.tx_payload_address),
                ]
            with m.Elif(response_matches):
                m.d.usb += tx_request_pending.eq(0)

        status_accept = tx_owner_locked & (tx_owner == _TX_STATUS) & self.tx_ready
        with m.If(status_accept):
            m.d.usb += status_pending.eq(0)
        with m.If(map_store.commit_ack):
            with m.If(status_pending & ~status_accept):
                with m.If(self.telemetry_drop_count != 0xFFFF_FFFF):
                    m.d.usb += self.telemetry_drop_count.eq(self.telemetry_drop_count + 1)
            with m.Else():
                m.d.usb += [
                    status_pending.eq(1),
                    status_descriptor_generation.eq(store.descriptor_generation),
                    status_map_generation.eq(candidate_map_generation),
                    status_active_map_generation.eq(map_store.active_generation),
                    status_entry_index.eq(map_store.commit_error_entry_index),
                    status_code.eq(
                        Mux(
                            map_store.commit_error == MapError.NONE,
                            INJ_MAP_STATUS_STATUS_COMMIT_ACCEPTED,
                            INJ_MAP_STATUS_STATUS_REJECTED,
                        )
                    ),
                    status_error.eq(map_store.commit_error),
                    status_entries_crc32.eq(candidate_entries_crc32),
                ]

        return m


def _injection_link_pin(name: str, pin: str, direction: str) -> Subsignal:
    """One PMOD-A pin, addressed by its *physical* connector pin number.

    Inputs carry an explicit pull-down. The toolchain leaves pads with no pull at
    all (yosys techmaps ``IB``/``OBZ`` with ``PULLMODE="NONE"``), so an unpulled
    input floats rather than resting at a defined level. A floating ``MCU_READY``
    that reads high silently discards every transmitted frame: ``send_message``
    dequeues on ``mcu_ready``, and the descriptor export only retriggers on a
    *rising* ``link_ready``, so the export fires once into a dead MCU and never
    retries.
    """
    attrs = (Attrs(PULLMODE="DOWN"),) if direction == "i" else ()
    return Subsignal(name, Pins(pin, conn=("pmod", 0), dir=direction), *attrs)


# Per-pin direction is only expressible as separate buffers over separate subsignals.
# ``io.Buffer.Signature`` declares ``oe: Out(1)`` regardless of port width, and the
# Lattice backend drives ``t = ~oe.replicate(len(port))`` - so a single ``dir="io"``
# buffer of any width shares one direction control across all its pads. The flat
# ``user_pmod`` resource additionally cannot carry per-pin ``Attrs``: ``res.py``
# hands the same attrs dict to every physical pin of a resource.
#
# Pin numbers are physical PMOD-A connector pins; see docs/hardware/ch32-cynthion-wiring.md.
_INJECTION_LINK_PMOD_A = Resource(
    "injection_link",
    0,
    _injection_link_pin("sck", "1", "o"),
    _injection_link_pin("mosi", "2", "o"),
    _injection_link_pin("miso", "3", "i"),
    _injection_link_pin("cs_n", "4", "o"),
    _injection_link_pin("mcu_ready", "7", "i"),
    _injection_link_pin("usb_sync", "8", "o"),
    # Unconnected today. Held as inputs so the pads are explicitly high-Z with a
    # defined level rather than driven against whatever a future harness attaches.
    _injection_link_pin("spare0", "9", "i"),
    _injection_link_pin("spare1", "10", "i"),
    Attrs(IO_TYPE="LVCMOS33"),
)


def injection_link_directions() -> dict[str, str]:
    """Per-pin direction, read back out of the resource declaration itself."""
    return {sub.name: sub.ios[0].dir for sub in _INJECTION_LINK_PMOD_A.ios}


class CynthionMouseHostTop(Elaboratable):
    """Connect the mouse host and PC-facing clone on Cynthion r1.4.

    LEDs 0..5 show attached, enumerating, ready, report activity, error, and
    clone configuration status.
    """

    def elaborate(self, platform) -> Module:
        m = Module()
        m.submodules.clocking = platform.clock_domain_generator()

        target_phy = platform.request("target_phy")
        m.submodules.utmi = utmi = UTMITranslator(ulpi=target_phy)
        m.submodules.host = host = BoundedMouseHost(utmi=utmi)
        self.host = host

        # The AUX port presents the captured mouse to the PC.
        # Hand LUNA the raw ULPI resource, not a UTMITranslator we built.
        #
        # USBDevice.__init__ duck-types its bus. A bus with a ``dir`` attribute
        # is raw ULPI, so it builds its own translator and sets always_fs=False
        # (data_clock 60 MHz). Anything else is assumed to be a plain UTMI
        # interface belonging to a Full-Speed-only gateware PHY, so it sets
        # always_fs=True and data_clock=12 MHz.
        #
        # A pre-made UTMITranslator has no ``dir``, so passing one lands in that
        # second branch and pins the port to Full Speed via
        # ``full_speed_only.eq(self.full_speed_only | self.always_fs)`` -- which
        # silently overrides the speed policy register. This port measured
        # "Up to 12 Mb/s" on the host until the translator was removed here.
        aux_phy = platform.request("aux_phy")
        m.submodules.device = device = MouseCloneDevice(bus=aux_phy, store=host.descriptor_store)
        self.device = device

        m.submodules.injection_plane = injection_plane = ReportInjectionDataPlane(
            host.descriptor_store
        )
        self.injection_plane = injection_plane
        m.submodules.spi_link = spi_link = SPISlotMaster(
            clock_hz=60_000_000, slot_cycles=7_500, sck_div=4
        )
        self.spi_link = spi_link

        platform.add_resources([_INJECTION_LINK_PMOD_A])
        injection_link = platform.request("injection_link", 0, dir="-")
        self.pmod_a_buffers = {}
        for pin_name, direction in injection_link_directions().items():
            buffer = io.Buffer(direction, getattr(injection_link, pin_name))
            self.pmod_a_buffers[pin_name] = buffer
            m.submodules[f"pmod_a_{pin_name}"] = buffer
        pmod_a = self.pmod_a_buffers

        mcu_ready = Signal()
        m.submodules.mcu_ready_sync = FFSynchronizer(
            pmod_a["mcu_ready"].i, mcu_ready, o_domain="usb", stages=2
        )

        m.d.comb += [
            # AUX vbus_valid stays low in the CONTROL-powered topology even
            # while the PC enumerates the clone. Connect only after the host has
            # enumerated the mouse and its shared descriptor store is stable.
            device.copy_enable.eq(host.enumerated),
            device.connect.eq(host.enumerated & device.copy_done),
            injection_plane.report_valid.eq(host.report_valid),
            injection_plane.report_data.eq(host.report_data),
            injection_plane.report_first.eq(host.report_first),
            injection_plane.report_last.eq(host.report_last),
            injection_plane.report_interface.eq(host.report_interface),
            injection_plane.report_endpoint.eq(host.report_endpoint),
            host.report_ready.eq(injection_plane.report_ready),
            injection_plane.output_ready.eq(device.report_ready),
            device.report_valid.eq(injection_plane.output_valid),
            device.report_data.eq(injection_plane.output_data),
            device.report_first.eq(injection_plane.output_first),
            device.report_last.eq(injection_plane.output_last),
            device.report_endpoint.eq(injection_plane.output_endpoint),
            injection_plane.sof_tick.eq(host.scheduler.frame_tick),
            injection_plane.session_active.eq(host.enumerated),
            injection_plane.link_ready.eq(mcu_ready),
            spi_link.mcu_ready.eq(mcu_ready),
            spi_link.sof_tick.eq(host.scheduler.frame_tick),
            # Never assign ``oe``: ``io.Buffer("o", ...)`` declares ``oe: Out(1, init=1)``
            # and the netlist ties it high, so the output buffers drive permanently and
            # the input buffers have no ``o`` to drive at all.
            spi_link.miso.eq(pmod_a["miso"].i),
            pmod_a["sck"].o.eq(spi_link.sck),
            pmod_a["mosi"].o.eq(spi_link.mosi),
            pmod_a["cs_n"].o.eq(spi_link.cs_n),
            pmod_a["usb_sync"].o.eq(spi_link.usb_sync),
            injection_plane.rx_valid.eq(spi_link.rx_valid),
            injection_plane.rx_type.eq(spi_link.rx_type),
            injection_plane.rx_sequence.eq(spi_link.rx_sequence),
            spi_link.rx_payload_address.eq(injection_plane.rx_payload_address),
            spi_link.rx_payload_read_enable.eq(injection_plane.rx_payload_read_enable),
            injection_plane.rx_payload_data.eq(spi_link.rx_payload_data),
            spi_link.rx_ready.eq(injection_plane.rx_ready),
            spi_link.tx_valid.eq(injection_plane.tx_valid),
            spi_link.tx_type.eq(injection_plane.tx_type),
            injection_plane.tx_fill_start.eq(spi_link.tx_fill_start),
            injection_plane.tx_payload_request.eq(spi_link.tx_payload_request),
            injection_plane.tx_payload_address.eq(spi_link.tx_payload_address),
            spi_link.tx_payload_response.eq(injection_plane.tx_payload_response),
            spi_link.tx_payload_data.eq(injection_plane.tx_payload_data),
            injection_plane.tx_ready.eq(spi_link.tx_ready),
        ]

        regmap = report_injection_register_map()
        debug = DebugRegisterBlock(
            regmap=regmap,
            aux_vbus_en=host.aux_vbus_en,
            target_discharge=host.target_discharge,
            power_from_control=True,
        )
        self.debug = debug
        map_store = injection_plane.map_store
        map_rejected = map_store.commit_ack & (map_store.commit_error != MapError.NONE)
        debug.status(
            "injection_link",
            link_ready=mcu_ready,
            session_active=host.enumerated,
            spi_busy=spi_link.busy,
            rx_pending=spi_link.rx_valid,
            map_active=map_store.active_valid,
            map_busy=map_store.busy,
            descriptor_busy=injection_plane.descriptor_export.busy,
            relay_ready=device.report_ready,
        )
        debug.status(
            "injection_map",
            descriptor_generation=host.descriptor_store.descriptor_generation,
            active_map_generation=map_store.active_generation,
        )
        # ``commit_error``/``commit_error_entry_index`` are one-cycle pulses that
        # re-default every cycle, so they must be latched to survive an asynchronous
        # JTAG read. ``error_entry`` keeps the source's 0xFF "no entry" idle value.
        debug.status(
            "map_validation",
            error=debug.latch_on(map_store.commit_ack, map_store.commit_error, name="map_error"),
            error_entry=debug.latch_on(
                map_store.commit_ack,
                map_store.commit_error_entry_index,
                name="map_error_entry",
                init=0xFF,
            ),
            active_valid=map_store.active_valid,
            busy=map_store.busy,
            active_entries=map_store.active_entry_count,
            active_layouts=map_store.active_layout_count,
        )
        for name, value in (
            ("spi_slots", spi_link.slot_counter),
            ("spi_bad_sof", spi_link.bad_sof_count),
            ("spi_bad_crc", spi_link.bad_crc_count),
            ("spi_bad_length", spi_link.bad_length_count),
            ("spi_bad_type", spi_link.bad_type_count),
            ("spi_queue_full", spi_link.rx_queue_full_count),
            ("link_losses", injection_plane.link_loss_count),
            ("native_reports", injection_plane.native_report_count),
            ("mutated_reports", injection_plane.mutated_report_count),
            ("synthesized_reports", injection_plane.synthesized_report_count),
            ("command_commits", injection_plane.command_commit_count),
            ("command_overflows", injection_plane.engine.command_overflow),
            ("monitoring_drops", injection_plane.monitor.monitoring_drops),
            ("telemetry_drops", injection_plane.telemetry_drop_count),
            ("rx_duplicates", injection_plane.duplicate_rx_count),
            ("rx_stale", injection_plane.stale_rx_count),
            ("rx_gaps", injection_plane.sequence_gap_count),
            ("accepted_reports", injection_plane.accepted_report_count),
            ("rx_invalid", injection_plane.invalid_rx_count),
            ("polls_issued", host.polls_issued),
            ("poll_naks", host.poll_naks),
            ("map_commits", debug.counter(map_store.commit_ack, name="map_commits")),
            ("map_rejects", debug.counter(map_rejected, name="map_rejects")),
        ):
            debug.status(name, value=value)
        # Kept out of ``injection_link``, whose live bits toggle at 8 kHz and would
        # bury a sticky one under ``regdebug watch``.
        debug.status(
            "injection_seen",
            export_done_seen=debug.sticky(
                injection_plane.descriptor_export.done, name="export_done_seen"
            ),
            map_busy_seen=debug.sticky(map_store.busy, name="map_busy_seen"),
        )
        # The relay is a non-blocking sink: it drops whole reports rather than
        # stalling the shared engine. These are the only record that it did.
        debug.status(
            "relay_drops",
            unmatched=debug.counter(
                device.relay.unmatched_report, name="relay_unmatched", width=16
            ),
            congested=debug.counter(
                device.relay.congested_report, name="relay_congested", width=16
            ),
        )
        # Speed observability for the High Speed work. The two ports negotiate
        # independently, so both sides are reported: aux_speed is LUNA's
        # post-chirp result on AUX, and the target_* fields are what our own
        # enumerator is commanding the TARGET PHY to do.
        # Speed policy is a runtime control, not a build-time constant: both
        # fields reset to 0, which means "negotiate", so the default bitstream
        # attempts High Speed on both ports and USB's own chirp handshake
        # settles the result. The override exists so a problem device can be
        # pinned to Full Speed without building a second bitstream.
        speed_policy, _ = debug.control("speed_policy")
        # AUX mirrors TARGET. The relay's job is to look like the device it is
        # relaying, and descriptors are cloned to the PC verbatim -- so if the
        # two halves negotiated different speeds, bInterval would be handed over
        # in the wrong encoding. A Full Speed mouse declaring bInterval=1 (1 ms)
        # would be advertised to the PC as 1 microframe, or 8 kHz, against the
        # ~1 kHz the device actually delivers.
        #
        # Mirroring keeps both halves in one encoding, so the verbatim clone is
        # correct by construction rather than by a translation table. The cost
        # is that a Full Speed device no longer gets a High Speed AUX link,
        # which for 8-byte reports buys nothing.
        m.d.comb += [
            device.full_speed_only.eq(speed_policy[0] | ~host.high_speed),
            # Bit 1 pins TARGET to Full Speed by suppressing the chirp. AUX
            # follows through the mirror above, so this one bit returns the
            # whole relay to the Full Speed behaviour. It was declared in the
            # register map before the chirp existed and had nothing to drive.
            host.force_full_speed.eq(speed_policy[1]),
        ]

        debug.status(
            "usb_speed",
            aux_speed=device.speed,
            aux_full_speed_only=device.full_speed_only,
            target_xcvr_select=host.utmi.xcvr_select,
            target_term_select=host.utmi.term_select,
            target_op_mode=host.utmi.op_mode,
            target_high_speed=host.high_speed,
            target_host_disconnect=host.host_disconnect,
            target_host_disconnect_seen=host.host_disconnect_seen,
            target_device_unresponsive=host.device_unresponsive,
        )
        debug.set_led(0, host.connected)
        debug.set_led(1, host.enumerating)
        debug.set_led(2, host.enumerated)
        debug.set_led(3, host.report_activity)
        debug.set_led(4, host.error_code != HostError.NONE.value)
        debug.set_led(5, device.configured)
        m.submodules.debug = debug

        return m


def emit_host_rtlil(filename: str) -> None:
    """Emit deterministic RTLIL for the platform-independent host core."""
    host = BoundedMouseHost()
    ports = [
        host.utmi.tx_ready,
        host.utmi.rx_data,
        host.utmi.rx_valid,
        host.utmi.rx_active,
        host.utmi.rx_error,
        host.utmi.line_state,
        host.utmi.vbus_valid,
        host.utmi.tx_valid,
        host.utmi.tx_data,
        host.utmi.xcvr_select,
        host.utmi.term_select,
        host.utmi.op_mode,
        host.utmi.dp_pulldown,
        host.utmi.dm_pulldown,
        host.aux_vbus_en,
        host.target_discharge,
        host.connected,
        host.enumerating,
        host.enumerated,
        host.error_code,
        host.polling_active,
        host.polling_failed,
        host.report_activity,
        host.report_valid,
        host.report_ready,
        host.report_data,
        host.report_first,
        host.report_last,
        host.report_interface,
        host.report_endpoint,
        host.ep_count,
        *host.ep_interface,
        *host.ep_number,
        *host.ep_max_packet,
        *host.ep_interval,
        host.lookup_type,
        host.lookup_index,
        host.lookup_w_index,
        host.lookup_offset,
        host.lookup_request,
        host.lookup_ready,
        host.lookup_response,
        host.lookup_found,
        host.lookup_length,
        host.lookup_data,
    ]
    output = Path(filename)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rtlil.convert(host, name="bounded_mouse_host", ports=ports))


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--rtlil", metavar="filename")
    args, remaining = parser.parse_known_args()
    if args.rtlil is not None:
        if remaining:
            parser.error("--rtlil cannot be combined with bitstream options")
        emit_host_rtlil(args.rtlil)
        return

    # Must precede the LUNA import: the placer timing weight is read from the
    # environment when the build plan is prepared, and this is the single
    # choke point every bitstream-producing invocation passes through.
    #
    # ``enforce_yosys`` is passed *only* here. This is the bitstream path, so an
    # unsupported synthesis tool should abort now rather than spend ten minutes
    # producing no bitstream -- the design does not close timing below yosys
    # 0.60. The test suite calls the same function without it, because it is
    # pure Python and runs where no toolchain exists.
    apply_build_environment(enforce_yosys=True)

    from luna import top_level_cli

    top_level_cli(CynthionMouseHostTop)


if __name__ == "__main__":
    main()
