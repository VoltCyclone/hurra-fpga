# FRDM-MCXN947 controller firmware -- migration step 2: CPU0 only.
#
# Builds ONE image. It brings the part to overdrive + 150 MHz, arms a 1 kHz
# SysTick, blinks the green LED, and brings up LPSPI6 as an SPI slave on
# LP_FLEXCOMM6 fed by a self-loading eDMA0 scatter-gather ring that transmits a
# permanently IDLE slot. There is no USB, no display and no CPU1 image yet;
# each arrives with its own step in docs/MCXN947_CONTROLLER.md section 9.
#
# `make all` is not the gate. --gc-sections will silently discard a translation
# unit that nothing references, and the vector table declares every handler
# .weak, so a dropped ISR is replaced by a stub that branches to itself with no
# error anywhere. `make check` asserts on the built artifacts instead.
#
# Deliberately NOT wired into the repository's top-level `make verify` yet:
# this target needs arm-none-eabi-gcc, and the existing firmware targets must
# keep working for someone who does not have it. Opt in explicitly:
#
#     make -C firmware/mcxn947 check
#
# Rule for the host tests below: if one of them ever needs -Ivendor, the module
# it exercises is mis-split. The portable half of a module must not reach for a
# vendor header.

# One variable, no probe, no $(HOME) fallback. Homebrew's arm-none-eabi-gcc
# (14.2.1) is on PATH on the development machine.
CROSS_COMPILE ?= arm-none-eabi-

CC      := $(CROSS_COMPILE)gcc
OBJCOPY := $(CROSS_COMPILE)objcopy
SIZE    := $(CROSS_COMPILE)size
HOST_CC ?= cc

# Cortex-M33 with the single-precision FPU and DSP extension the part
# implements (__FPU_PRESENT / __DSP_PRESENT in
# vendor/mcux-sdk/devices/MCXN947/MCXN947_cm33_core0_COMMON.h).
ARCH := -mcpu=cortex-m33 -mthumb -mfloat-abi=hard -mfpu=fpv5-sp-d16

# CPU_MCXN947VDF_cm33_core0 is what fsl_device_registers.h switches on; it is
# the part fitted to FRDM-MCXN947 and is the macro the SDK's own frdmmcxn947
# examples define. MCXN947 is *our* guard macro -- MMIO lives inside
# `#if defined(MCXN947)`, so a new file with no guard is portable by default.
DEFINES := -DCPU_MCXN947VDF_cm33_core0 -DMCXN947

VENDOR_SDK := vendor/mcux-sdk
VENDOR_HAL := vendor/hal_nxp

LINKER_SCRIPT := $(VENDOR_HAL)/devices/MCXN947/gcc/MCXN947_cm33_core0_flash.ld
STARTUP       := $(VENDOR_HAL)/devices/MCXN947/gcc/startup_MCXN947_cm33_core0.S

# `include/` holds the generated wire contract, so it is -isystem rather than
# -I: injection_wire.h is machine-generated from
# protocol/report_injection_wire.json, carries a do-not-edit header, and does
# not survive -Wconversion. Warning about code we are forbidden to fix would
# only invite someone to fix it. Same treatment as the CH32 tree.
INCLUDES := -Isrc -isystem include \
	-isystem $(VENDOR_SDK)/CMSIS/Core/Include \
	-isystem $(VENDOR_SDK)/devices/MCXN947 \
	-isystem $(VENDOR_SDK)/devices/MCXN947/periph \
	-isystem $(VENDOR_SDK)/devices/MCXN947/drivers \
	-isystem $(VENDOR_SDK)/boards/frdmmcxn947/project_template

COMMON_CFLAGS := $(ARCH) $(DEFINES) $(INCLUDES) -Os -ffunction-sections \
	-fdata-sections -fno-common
APP_WARNINGS      := -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow
# Vendor sources are byte-exact imports and are not held to the app's warning
# set. -Wno-unused-* covers file-local helpers and prototypes unused in this
# selected-driver build, exactly as the CH32 tree does;
# -Wno-old-style-declaration covers fsl_clock.c's `volatile static` file
# statics, which are a style the vendor uses throughout and we may not edit.
VENDOR_WARNINGS   := -Wall -Wextra -Wno-unused-function -Wno-unused-variable \
	-Wno-unused-parameter -Wno-old-style-declaration

# Flash and RAM geometry. Declared here ONCE and passed to the checker, so the
# Makefile and the gate cannot drift apart. Every value is read out of
# $(LINKER_SCRIPT):
#
#   m_interrupts   ORIGIN = 0x00000000          (TEXT_START)
#   m_text         ORIGIN + 0x400, LENGTH = TEXT_SIZE - 0x400
#   m_core1_image  ORIGIN = 0x000C0000          <- CPU0's flash ceiling, and the
#                                                  CORE1_OFFSET of step 5
#   m_data         ORIGIN = 0x20000000, LENGTH = 0x0004E000 - RPMSG_SHMEM_SIZE
#   rpmsg_sh_mem   ORIGIN = 0x2004E000 - RPMSG_SHMEM_SIZE
#   HEAP_SIZE / STACK_SIZE defaults
#
# RPMSG_SHMEM_SIZE is 0x2000 because we link with __use_shmem__=1 (below), so
# m_data ends at 0x2004C000 and 0x2004C000..0x2004E000 is reserved. We are
# reusing RPMsg's *address reservation*, not RPMsg: it is where the CPU0 ->
# CPU1 snapshot goes at step 6, and reserving it now keeps CPU0's data from
# growing into it in the meantime. Both vendored .ld files stay byte-exact.
CORE0_FLASH_START := 0x00000000
CORE0_FLASH_END   := 0x000C0000
CORE1_OFFSET      := 0x000C0000
CORE0_RAM_START   := 0x20000000
CORE0_RAM_END     := 0x2004C000
RAM_SHARED_START  := 0x2004C000
RAM_SHARED_END    := 0x2004E000
STACK_SIZE        := 0x0800
HEAP_SIZE         := 0x0400

# --no-warn-rwx-segments: the vendored linker script places `.data` with
# `AT(__DATA_ROM)`, so its LMA in m_text (RX) and its VMA in m_data (RW) share
# one LOAD segment, which ld then reports as RWX. That is structural to the
# byte-exact script and would otherwise print on every single link. Recorded in
# PROVENANCE.md.
#
# The `_write is not implemented` notes that nosys.specs produces are NOT
# suppressed. They name a true fact -- newlib's exit path is linked and its
# syscall stubs always fail -- and suppressing them now would hide the signal
# when a later step actually calls into stdio.
LDFLAGS := $(ARCH) -T$(LINKER_SCRIPT) -Wl,--gc-sections \
	-Wl,-Map=build/core0.map \
	-Wl,--defsym,__use_shmem__=1 \
	-Wl,--no-warn-rwx-segments \
	--specs=nano.specs --specs=nosys.specs

# Retention roots, as SYMBOL=OWNING_OBJECT.
#
# This one list feeds both the link (-Wl,--undefined=SYMBOL) and the gate
# (--retain core0.elf:SYMBOL=OBJECT), so a root cannot be linked but unchecked,
# or checked but not linked.
#
# Why the mechanism exists at all, stated now while the list is nearly empty:
# startup_MCXN947_cm33_core0.S declares every handler `.weak`, so if
# --gc-sections drops the real one the link still succeeds against the vendor
# stub. Worse than on the CH32, this part uses a *two-level* vector: the table
# entry `<NAME>_IRQHandler` is a weak trampoline that branches to
# `<NAME>_DriverIRQHandler`, and it is the *Driver* name that is `.set` to
# DefaultISR. So for a peripheral IRQ the root that means anything is
# `<NAME>_DriverIRQHandler`; rooting `<NAME>_IRQHandler` alone would pass the
# "is it defined?" question against the vendor's own trampoline and prove
# nothing. The core exception vectors (SysTick_Handler, HardFault_Handler, ...)
# are a third shape again -- weak stubs that branch to themselves rather than
# aliases of DefaultISR -- which is why the gate tests the symbol's *binding*
# and not just its address.
#
# Measured, so the claim is not overstated: with every object named explicitly
# on the link line as they are here, strong-beats-weak resolution already keeps
# SysTick_Handler at `T` with these flags removed. So today these two roots are
# a defensive backstop, and it is the *gate* that is load-bearing -- it reports
# the weak-stub outcome no matter which way it was arrived at. The roots start
# earning their keep as soon as a handler's translation unit arrives via an
# archive, or is dropped from the object list on the assumption that the vector
# table alone retains it. The mechanism is established now, while the list is
# two lines long, rather than during the step that first needs it.
#
# Step 2 adds no root. Its eDMA0 rings are scatter-gather and self-loading, so
# the link needs no ISR at all -- and with no console yet, an MCU-side liveness
# counter would be unreadable anyway. The retention roots that matter here are
# the ones step 3 adds when it turns on INTMAJOR for the RX channel, and the
# name to root then is `EDMA_0_CH1_DriverIRQHandler`, not the vector-table
# `EDMA_0_CH1_IRQHandler`, which is only a weak trampoline.
CORE0_RETAIN := \
	main=build/core0/main_core0.o \
	SysTick_Handler=build/core0/heartbeat.o

retain_symbol = $(word 1,$(subst =, ,$(1)))

CORE0_RETAIN_LDFLAGS := \
	$(foreach r,$(CORE0_RETAIN),-Wl,--undefined=$(call retain_symbol,$(r)))

# Object list. This is the source of truth for what must survive into the
# image; `make check` asserts that every one of these contributes at least one
# symbol to core0.elf.
CORE0_OBJECTS := \
	build/core0/startup.o \
	build/core0/system.o \
	build/core0/clock_config.o \
	build/core0/fsl_clock.o \
	build/core0/fsl_spc.o \
	build/core0/fsl_gpio.o \
	build/core0/fsl_reset.o \
	build/core0/fsl_lpflexcomm.o \
	build/core0/fsl_edma.o \
	build/core0/fsl_edma_soc.o \
	build/core0/platform.o \
	build/core0/heartbeat.o \
	build/core0/spi_frame.o \
	build/core0/dbg_uart.o \
	build/core0/link.o \
	build/core0/main_core0.o

.PHONY: all check check-copies clean flash probes test test-platform \
	test-heartbeat test-link test-spi-frame

all: build/hurra-mcxn947-core0.bin

build/hurra-mcxn947-core0.bin: build/core0.elf
	$(OBJCOPY) -O binary $< $@

build/core0.elf: $(CORE0_OBJECTS) $(LINKER_SCRIPT) | build/core0
	$(CC) $(LDFLAGS) $(CORE0_RETAIN_LDFLAGS) -o $@ $(CORE0_OBJECTS)
	$(SIZE) $@

# --- application objects ----------------------------------------------------

build/core0/main_core0.o: src/main_core0.c src/heartbeat.h src/platform.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/platform.o: src/platform.c src/platform.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/heartbeat.o: src/heartbeat.c src/heartbeat.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/link.o: src/link.c src/link.h src/spi_frame.h src/platform.h src/dbg_uart.h \
		include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# TEMPORARY step-2 bring-up diagnostic -- remove with src/dbg_uart.*
build/core0/dbg_uart.o: src/dbg_uart.c src/dbg_uart.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# Byte-exact copy of firmware/ch32h417/src/spi_frame.c; `make check-copies`
# holds the two identical. Built with the app warning set, not the vendor one:
# it is our code, just shared.
build/core0/spi_frame.o: src/spi_frame.c src/spi_frame.h include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# --- vendor objects ---------------------------------------------------------

build/core0/startup.o: $(STARTUP) | build/core0
	$(CC) $(COMMON_CFLAGS) -c $< -o $@

build/core0/system.o: $(VENDOR_SDK)/devices/MCXN947/system_MCXN947_cm33_core0.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/clock_config.o: $(VENDOR_SDK)/boards/frdmmcxn947/project_template/clock_config.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/fsl_%.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_%.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0 build/host:
	mkdir -p $@

# --- the gate ---------------------------------------------------------------

CHECK_OBJECT_ARGS := $(foreach o,$(CORE0_OBJECTS),--object core0.elf:$(o))
CHECK_RETAIN_ARGS := $(foreach r,$(CORE0_RETAIN),--retain core0.elf:$(r))

# The wire codec and the generated contract header are byte-exact copies of the
# CH32 originals, so that the two controllers can be diffed against each other
# until step 10 retires that tree. "Byte-exact" is worth nothing as a claim in
# PROVENANCE.md and everything as an assertion, so assert it: a drifting copy
# is how the two ends of a hand-written-per-language codec silently disagree.
#
# injection_wire.h additionally carries a do-not-edit header and is generated
# from protocol/report_injection_wire.json by
# tools/generate_report_injection_wire.py; this rung is what stops it being
# hand-patched here.
CH32_TREE := ../ch32h417
COPIED_FILES := src/spi_frame.c src/spi_frame.h test/spi_frame_test.c

check-copies:
	@for f in $(COPIED_FILES); do \
		cmp "$$f" "$(CH32_TREE)/$$f" || exit 1; \
		echo "  PASS  byte-exact copy: $$f"; \
	done
	@cmp include/injection_wire.h $(CH32_TREE)/include/injection_wire.h || exit 1
	@echo "  PASS  byte-exact copy: include/injection_wire.h"

check: all check-copies
	python3 tools/check_mcxn947_images.py --self-test
	python3 tools/check_mcxn947_images.py --cross-compile $(CROSS_COMPILE) \
		--core0 build/core0.elf \
		--core0-bin build/hurra-mcxn947-core0.bin \
		--core0-flash-start $(CORE0_FLASH_START) \
		--core0-flash-end $(CORE0_FLASH_END) \
		--core0-ram-start $(CORE0_RAM_START) \
		--core0-ram-end $(CORE0_RAM_END) \
		--ram-shared-start $(RAM_SHARED_START) \
		--ram-shared-end $(RAM_SHARED_END) \
		--stack-size $(STACK_SIZE) --heap-size $(HEAP_SIZE) \
		$(CHECK_RETAIN_ARGS) $(CHECK_OBJECT_ARGS)

# --- host tests -------------------------------------------------------------
#
# These compile the *same* sources the target build uses, without -DMCXN947.
# They are the demonstration that the guard polarity holds: if a module ever
# grows MMIO outside its guard, the host build of it stops compiling here.

test: test-platform test-heartbeat test-spi-frame test-link

test-platform: build/host/platform_test
	./build/host/platform_test

build/host/platform_test: test/platform_test.c src/platform.c src/platform.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/platform_test.c src/platform.c -o $@

test-heartbeat: build/host/heartbeat_test
	./build/host/heartbeat_test

build/host/heartbeat_test: test/heartbeat_test.c src/heartbeat.c src/heartbeat.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/heartbeat_test.c src/heartbeat.c -o $@

test-spi-frame: build/host/spi_frame_test
	./build/host/spi_frame_test

build/host/spi_frame_test: test/spi_frame_test.c src/spi_frame.c src/spi_frame.h \
		include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/spi_frame_test.c src/spi_frame.c -o $@

test-link: build/host/link_test
	./build/host/link_test

build/host/link_test: test/link_test.c src/link.c src/spi_frame.c src/link.h \
		src/spi_frame.h include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/link_test.c src/link.c src/spi_frame.c -o $@

# --- flash -----------------------------------------------------------------
#
# NEVER a CI target: it needs the board.
#
# LinkServer, not J-Link, and the design doc (§7) is wrong about this. The
# FRDM-MCXN947 ships its on-board MCU-Link running CMSIS-DAP firmware --
# enumerating as NXP 0x1fc9:0x0143 "MCU-LINK FRDM-MCXN947 (r0E7) CMSIS-DAP" --
# which JLinkExe cannot drive. Using J-Link would mean reflashing the MCU-Link
# with SEGGER firmware, which is reversible but takes the board away from
# MCUXpresso IDE and from every NXP example.
#
# LinkServer is NXP's own tool for NXP's own probe on NXP's own part, it is
# already installed, and it identifies the target unaided:
#
#     #  Description                                    Device    Board
#     1  MCU-LINK FRDM-MCXN947 (r0E7) CMSIS-DAP V3.128  MCXN947   FRDM-MCXN947
#
# Point LINKSERVER at another install if yours differs.
LINKSERVER ?= /Applications/LinkServer_24.12.21/LinkServer
LINKSERVER_DEVICE ?= MCXN947:FRDM-MCXN947

# Load the ELF rather than the .bin: it carries its own load addresses, so
# there is no --addr to get wrong and no way for the flash offset to drift
# from what the linker script decided.
flash: all
	$(LINKSERVER) flash $(LINKSERVER_DEVICE) load build/core0.elf

probes:
	$(LINKSERVER) probes

clean:
	rm -rf build
