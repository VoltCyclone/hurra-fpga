# FRDM-MCXN947 controller firmware -- migration step 7: CPU1 display.
#
# CPU0 brings the part to overdrive + 150 MHz, arms a 1 kHz
# SysTick, blinks the green LED, brings up LPSPI6 as an SPI slave on
# LP_FLEXCOMM6 fed by a self-loading eDMA0 scatter-gather ring that transmits a
# permanently IDLE slot, retires every received slot through the portable
# transport in src/link_retire.c, detects and recovers ERR051588 through the
# ladder in src/link_recovery.c, and presents a TinyUSB CDC console on the
# ChipIdea High Speed controller behind J11. CPU1 is a separate image released
# last; it reads the one-way snapshot, echoes the slot count it actually saw,
# drives the blue LED from slot-counter bit 11, and repaints the S035 panel at
# about 20 Hz through FlexIO0 + eDMA1.
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
CORE0_ARCH := -mcpu=cortex-m33 -mthumb -mfloat-abi=hard -mfpu=fpv5-sp-d16
# The two cores are NOT the same core. MCXN947_cm33_core1_COMMON.h declares
# __FPU_PRESENT 0, __DSP_PRESENT 0 and __MPU_PRESENT 0 where the core0 header
# declares 1, 1; LinkServer's own device description calls core1 "cm33_nodsp".
# So core1 gets +nodsp and a soft float ABI -- CMSIS stops the build outright
# if given core0's hard-float ABI, which is the correct behaviour and how this
# was found. No -mfpu here: it is inert under a soft ABI and naming one would
# imply a choice that is not being made. The missing MPU matters later --
# section 10's two-DMA coherency story cannot be solved with an MPU region on
# this core. Recorded as a step-5 finding in PROVENANCE.md.
CORE1_ARCH := -mcpu=cortex-m33+nodsp -mthumb -mfloat-abi=soft

# CPU_MCXN947VDF_cm33_core0 is what fsl_device_registers.h switches on; it is
# the part fitted to FRDM-MCXN947 and is the macro the SDK's own frdmmcxn947
# examples define. MCXN947 is *our* guard macro -- MMIO lives inside
# `#if defined(MCXN947)`, so a new file with no guard is portable by default.
CORE0_DEFINES := -DCPU_MCXN947VDF_cm33_core0 -DMCXN947
CORE1_DEFINES := -DCPU_MCXN947VDF_cm33_core1 -DMCXN947

VENDOR_SDK := vendor/mcux-sdk
VENDOR_HAL := vendor/hal_nxp
VENDOR_TUSB := vendor/tinyusb

CORE0_LINKER_SCRIPT := $(VENDOR_HAL)/devices/MCXN947/gcc/MCXN947_cm33_core0_flash.ld
CORE1_LINKER_SCRIPT := $(VENDOR_HAL)/devices/MCXN947/gcc/MCXN947_cm33_core1_flash.ld
CORE0_STARTUP := $(VENDOR_HAL)/devices/MCXN947/gcc/startup_MCXN947_cm33_core0.S
CORE1_STARTUP := $(VENDOR_HAL)/devices/MCXN947/gcc/startup_MCXN947_cm33_core1.S

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
	-isystem $(VENDOR_SDK)/boards/frdmmcxn947/project_template \
	-isystem $(VENDOR_SDK)/components/video \
	-isystem $(VENDOR_SDK)/components/video/display/dbi \
	-isystem $(VENDOR_SDK)/components/video/display/dbi/flexio \
	-isystem $(VENDOR_SDK)/components/display/st7796s \
	-isystem $(VENDOR_TUSB)/src

COMMON_CFLAGS := $(CORE0_ARCH) $(CORE0_DEFINES) $(INCLUDES) $(EXTRA_CFLAGS) -Os \
	-ffunction-sections -fdata-sections -fno-common
# MCUX_DBI_LEGACY stays at its default of 1, and that is load-bearing.
#
# With MCUX_DBI_LEGACY=0, ST7796S_WriteCommand() hands its parameter BYTE array
# straight to FLEXIO_MCULCD_WriteDataArrayBlocking(), which on a 16-bit bus does
# `size /= 2U` and reinterprets the bytes as uint16_t. A one-byte parameter
# therefore becomes ZERO bus beats and is never transmitted: COLMOD, MADCTL,
# TEON and the CSCON unlock keys all silently vanish, and CASET/RASET lose half
# their bytes. Parameterless commands are unaffected, which is why SLPOUT and
# DISPON worked while the panel showed nothing but its random power-on GRAM.
#
# The legacy path widens each parameter byte into its own 16-bit beat
# (fsl_st7796s.c: `uint16_t param_data = params[i]`), which is correct, and is
# the path NXP's own working frdmmcxn947 display examples use.
CORE1_CFLAGS := $(CORE1_ARCH) $(CORE1_DEFINES) \
	$(INCLUDES) $(EXTRA_CFLAGS) -Os \
	-ffunction-sections -fdata-sections -fno-common
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
# $(CORE0_LINKER_SCRIPT):
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
# reusing RPMsg's *address reservation*, not RPMsg: step 5 puts CPU1 liveness
# there and step 6 appends the snapshot. Both vendored .ld files stay byte-exact.
CORE0_FLASH_START := 0x00000000
CORE0_FLASH_END   := 0x000C0000
CORE1_OFFSET      := 0x000C0000
FLASH_LIMIT       := 0x00100000
CORE0_RAM_START   := 0x20000000
CORE0_RAM_END     := 0x2004C000
RAM_SHARED_START  := 0x2004C000
RAM_SHARED_END    := 0x2004E000
SHARED_WINDOW_SIZE := 0x40
CORE1_RAM_START   := 0x2004E000
CORE1_RAM_END     := 0x20068000
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
CORE0_LDFLAGS := $(CORE0_ARCH) -T$(CORE0_LINKER_SCRIPT) -Wl,--gc-sections \
	-Wl,-Map=build/core0.map \
	-Wl,--defsym,__use_shmem__=1 \
	-Wl,--no-warn-rwx-segments \
	--specs=nano.specs --specs=nosys.specs

CORE1_LDFLAGS := $(CORE1_ARCH) -T$(CORE1_LINKER_SCRIPT) -Wl,--gc-sections \
	-Wl,-Map=build/core1.map \
	-Wl,--defsym,__use_shmem__=1 \
	-Wl,--no-warn-rwx-segments \
	--specs=nano.specs --specs=nosys.specs

# Reaches exactly ONE translation unit -- core1_release.c, the only code that
# needs CPU1's boot address. core1_release() takes no argument precisely so
# that this literal has a single home; passing it as a parameter would create
# a second place for it to be wrong and a silent no-op when the two disagree.
CORE1_VECTOR_DEFINE := -DCORE1_VECTOR_ADDR=$(CORE1_OFFSET)

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
# Step 3 is where the mechanism stops being defensive. It turns INTMAJOR on for
# the RX channel, so there is now an ISR whose loss would be silent: the rings
# stay self-loading either way, so a dropped handler does not stop the link --
# it stops RETIREMENT, and the symptom would be `slots` frozen at zero while
# every FPGA counter stayed perfectly flat. Exactly the shape of failure the
# gate exists for.
#
# The name rooted is `EDMA_0_CH1_DriverIRQHandler`, NOT the vector-table
# `EDMA_0_CH1_IRQHandler`. The table entry is a weak trampoline that branches to
# the Driver name, and it is the Driver name that startup_MCXN947_cm33_core0.S
# `.set`s to DefaultISR -- so rooting the vector name would ask "is it defined?"
# of the vendor's own trampoline and always get yes.
#
# Step 4 adds the USB controller vector, and it is the same shape again: the
# table names USB1_HS_IRQHandler, which is a weak trampoline branching to
# USB1_HS_DriverIRQHandler, and it is the Driver name that startup
# `.set`s to DefaultISR. Rooting the table name would ask "is it defined?" of
# the vendor's own trampoline and always get yes, while the real handler --
# the only thing that ever calls tusb_int_handler() -- had been discarded. The
# symptom would be a device that never enumerates at all, with a perfectly
# clean build.
#
# Note the controller is USB1_HS, not USB0. UM12018: "only the HS USB
# controller and PHY interface is used and it is connected to the USB Type-C
# connector (J11)". USB0_FS is routed nowhere on this board, so USB0_FS_IRQn
# (vector 50) is not the vector this design uses; USB1_HS_IRQn is vector 67.
CORE0_RETAIN := \
	main=build/core0/main_core0.o \
	SysTick_Handler=build/core0/heartbeat.o \
	EDMA_0_CH1_DriverIRQHandler=build/core0/link.o \
	USB1_HS_DriverIRQHandler=build/core0/usb_console.o

retain_symbol = $(word 1,$(subst =, ,$(1)))

CORE0_RETAIN_LDFLAGS := \
	$(foreach r,$(CORE0_RETAIN),-Wl,--undefined=$(call retain_symbol,$(r)))

# SystemInit is rooted as well as main: the ownership check makes the decision
# not to link NXP's chip-wide core1 SystemInit a property of the ELF, not a
# review promise. See PROVENANCE.md deviation 12.
# EDMA_1_CH0 follows the same two-level startup rule as CPU0's DMA vector: the
# vector-table name is only a weak trampoline, so the Driver name is the root.
CORE1_RETAIN := \
	main=build/core1/main_core1.o \
	SystemInit=build/core1/system_core1.o \
	EDMA_1_CH0_DriverIRQHandler=build/core1/display_panel.o

CORE1_RETAIN_LDFLAGS := \
	$(foreach r,$(CORE1_RETAIN),-Wl,--undefined=$(call retain_symbol,$(r)))

# Object lists. These are the source of truth for what must survive into each
# image; `make check` asserts that every listed object contributes at least one
# symbol to its ELF.
#
# fsl_edma_soc.c left this list at step 3 and its .c is no longer vendored --
# only fsl_edma_soc.h, which fsl_edma.h includes. The file defines NOTHING but
# 32 strong `EDMA_<n>_CH<m>_DriverIRQHandler` wrappers around
# `EDMA_DriverIRQHandler(instance, channel)`, which dispatches through the
# transactional handle array `s_EDMAHandle[][]` that this firmware never
# populates. Two consequences, both settled by measurement rather than by
# preference:
#
#   * it COLLIDES. Step 3's retirement ISR must be named
#     EDMA_0_CH1_DriverIRQHandler (see CORE0_RETAIN), and the vendor's strong
#     definition of that same symbol makes the link fail outright. That is the
#     good failure -- the alternative name, EDMA_0_CH1_IRQHandler, would have
#     silently displaced only the trampoline and left the other 31 in place.
#   * it was never inert. `EDMA_HandleIRQ()` opens with `assert(handle !=
#     NULL)`, assert is live in this image, and nosys.specs makes newlib's
#     failure path hang rather than reset -- so every one of those 32 vectors
#     was a latent hang behind any eDMA interrupt anyone later enabled. Step 2
#     enabled none, so nothing showed.
#
# Dropping it also removes 31 handlers the image had no use for; the weak
# `.set`-to-DefaultISR entries in startup_MCXN947_cm33_core0.S are the correct
# occupants of those slots.
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
	build/core0/platform.o \
	build/core0/heartbeat.o \
	build/core0/spi_frame.o \
	build/core0/dbg_uart.o \
	build/core0/link_retire.o \
	build/core0/link_recovery.o \
	build/core0/link_snapshot.o \
	build/core0/link.o \
	build/core0/inj_command.o \
	build/core0/inj_session.o \
	build/core0/console.o \
	build/core0/usb_descriptors.o \
	build/core0/usb_console.o \
	build/core0/tusb.o \
	build/core0/tusb_fifo.o \
	build/core0/usbd.o \
	build/core0/usbd_control.o \
	build/core0/cdc_device.o \
	build/core0/dcd_ci_hs.o \
	build/core0/shared_window.o \
	build/core0/core1_release.o \
	build/core0/main_core0.o

CORE1_OBJECTS := \
	build/core1/startup.o \
	build/core1/system_core1.o \
	build/core1/fsl_clock.o \
	build/core1/fsl_reset.o \
	build/core1/fsl_gpio.o \
	build/core1/fsl_edma.o \
	build/core1/fsl_flexio.o \
	build/core1/fsl_flexio_mculcd.o \
	build/core1/fsl_flexio_mculcd_edma.o \
	build/core1/fsl_dbi_flexio_edma.o \
	build/core1/fsl_st7796s.o \
	build/core1/link_snapshot.o \
	build/core1/shared_window.o \
	build/core1/status_led.o \
	build/core1/text_grid.o \
	build/core1/glyphs.o \
	build/core1/display_panel.o \
	build/core1/display.o \
	build/core1/main_core1.o

.PHONY: all check check-copies clean flash flash-core0-only flash-merged \
	verify-core1-blank probes test test-platform \
	test-heartbeat test-link test-link-retire test-link-recovery test-spi-frame \
	test-console test-core1-release test-shared-window test-link-snapshot test-status-led \
	test-text-grid test-glyphs test-display-panel test-display \
	test-merge-images

all: build/hurra-mcxn947.bin

build/hurra-mcxn947-core0.bin: build/core0.elf
	$(OBJCOPY) -O binary $< $@

build/hurra-mcxn947-core1.bin: build/core1.elf
	$(OBJCOPY) -O binary $< $@

build/hurra-mcxn947.bin: build/hurra-mcxn947-core0.bin \
		build/hurra-mcxn947-core1.bin tools/merge_mcxn947_images.py
	python3 tools/merge_mcxn947_images.py $< build/hurra-mcxn947-core1.bin \
		$(CORE1_OFFSET) $@ $(FLASH_LIMIT)

build/core0.elf: $(CORE0_OBJECTS) $(CORE0_LINKER_SCRIPT) | build/core0
	$(CC) $(CORE0_LDFLAGS) $(CORE0_RETAIN_LDFLAGS) -o $@ $(CORE0_OBJECTS)
	$(SIZE) $@

build/core1.elf: $(CORE1_OBJECTS) $(CORE1_LINKER_SCRIPT) | build/core1
	$(CC) $(CORE1_LDFLAGS) $(CORE1_RETAIN_LDFLAGS) -o $@ $(CORE1_OBJECTS)
	$(SIZE) $@

# --- application objects ----------------------------------------------------

build/core0/main_core0.o: src/main_core0.c src/core1_release.h src/heartbeat.h \
		src/link.h src/platform.h src/shared_window.h src/usb_console.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/core1_release.o: src/core1_release.c src/core1_release.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(CORE1_VECTOR_DEFINE) $(APP_WARNINGS) -c $< -o $@

build/core0/shared_window.o: src/shared_window.c src/shared_window.h \
		src/link_snapshot.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/link_snapshot.o: src/link_snapshot.c src/link_snapshot.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/platform.o: src/platform.c src/platform.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/heartbeat.o: src/heartbeat.c src/heartbeat.h src/platform.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/link.o: src/link.c src/link.h src/link_retire.h src/link_recovery.h \
		src/spi_frame.h src/platform.h src/dbg_uart.h src/shared_window.h \
		src/link_snapshot.h src/inj_command.h src/inj_session.h \
		include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# The injection TX side: command builders and the session policy above them.
# Both are MMIO-free (host-tested below); link.c pulls them in inside its
# MCXN947 guard, so only the target link needs these objects.
build/core0/inj_command.o: src/inj_command.c src/inj_command.h src/spi_frame.h \
		include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/inj_session.o: src/inj_session.c src/inj_session.h src/inj_command.h \
		src/spi_frame.h include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# The portable transport half of step 3. Neither of these compiles a single
# MMIO access, which is the point: -Ivendor is absent from their host test
# recipes below, so a vendor header creeping into either one stops the host
# build rather than passing unnoticed into the target image.
build/core0/link_retire.o: src/link_retire.c src/link_retire.h src/spi_frame.h \
		include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0/link_recovery.o: src/link_recovery.c src/link_recovery.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# --- the step-4 console ----------------------------------------------------
#
# console.c is the portable half and is host-tested below. It never sees a
# TinyUSB header, which is what keeps that true -- note the absence of
# $(VENDOR_TUSB) from its host recipe, and the Makefile rule at the top of this
# file: if a host test ever needs -Ivendor, the module is mis-split.
build/core0/console.o: src/console.c src/console.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# The glue and the descriptor table. Both include tusb.h, so both are built
# with GLUE_WARNINGS rather than APP_WARNINGS: -Wconversion fires inside the
# vendored headers' own macros (TUD_CDC_DESCRIPTOR packs 16-bit fields with
# U16_TO_U8S_LE, tu_htole16 and friends), and we may not edit those. Every
# other warning in the app set still applies, including -Werror. The narrowing
# is on the headers, not on this code.
GLUE_WARNINGS := -std=c11 -Wall -Wextra -Werror -Wshadow

build/core0/usb_descriptors.o: src/usb_descriptors.c src/tusb_config.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(GLUE_WARNINGS) -c $< -o $@

build/core0/usb_console.o: src/usb_console.c src/usb_console.h src/console.h \
		src/dbg_uart.h src/link.h src/link_retire.h src/platform.h \
		src/shared_window.h src/link_snapshot.h src/tusb_config.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(GLUE_WARNINGS) -c $< -o $@

# --- vendored TinyUSB ------------------------------------------------------
#
# Subset only; PROVENANCE.md lists every file and the git archive command that
# produced it. The device stack, the CDC class and the ChipIdea HS device
# controller driver -- no host stack, no other class, and specifically NOT
# dcd_ci_fs.c: USB0_FS is routed nowhere on this board.
#
# COMMON_CFLAGS carries -DMCXN947, our guard macro, which TinyUSB does not
# read; what TinyUSB switches on is CFG_TUSB_MCU, set in src/tusb_config.h and
# reached through -Isrc.
build/core0/tusb.o: $(VENDOR_TUSB)/src/tusb.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/tusb_fifo.o: $(VENDOR_TUSB)/src/common/tusb_fifo.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/usbd.o: $(VENDOR_TUSB)/src/device/usbd.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/usbd_control.o: $(VENDOR_TUSB)/src/device/usbd_control.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/cdc_device.o: $(VENDOR_TUSB)/src/class/cdc/cdc_device.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/dcd_ci_hs.o: $(VENDOR_TUSB)/src/portable/chipidea/ci_hs/dcd_ci_hs.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

# Bring-up diagnostic UART. Retained deliberately THROUGH step 5, against the
# note in dbg_uart.c saying it goes when the CDC console arrives.
#
# Two reasons, both measured rather than sentimental. The console is on the
# USB the console is used to debug, so a USB fault takes out the instrument
# exactly when it is needed -- the debug UART is on a different peripheral, a
# different connector and a different host cable (MCU-Link VCOM on J17, not
# J11). And step 4's own gates are boot-rate measurements against a 20% boot
# hazard: the UART prints from link_init() onward, before USB enumeration has
# even been attempted, so a boot that fails before enumeration is still
# visible. It goes when there is a second independent channel, not before.
build/core0/dbg_uart.o: src/dbg_uart.c src/dbg_uart.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# Byte-exact copy of firmware/ch32h417/src/spi_frame.c; `make check-copies`
# holds the two identical. Built with the app warning set, not the vendor one:
# it is our code, just shared.
build/core0/spi_frame.o: src/spi_frame.c src/spi_frame.h include/injection_wire.h | build/core0
	$(CC) $(COMMON_CFLAGS) $(APP_WARNINGS) -c $< -o $@

# --- vendor objects ---------------------------------------------------------

build/core0/startup.o: $(CORE0_STARTUP) | build/core0
	$(CC) $(COMMON_CFLAGS) -c $< -o $@

build/core0/system.o: $(VENDOR_SDK)/devices/MCXN947/system_MCXN947_cm33_core0.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/clock_config.o: $(VENDOR_SDK)/boards/frdmmcxn947/project_template/clock_config.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core0/fsl_%.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_%.c | build/core0
	$(CC) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

# --- core1 image ------------------------------------------------------------

build/core1/startup.o: $(CORE1_STARTUP) | build/core1
	$(CC) $(CORE1_CFLAGS) -c $< -o $@

# This is our core-private-only SystemInit, not the vendored reference. Linking
# the SDK implementation would replay chip-wide power/security policy after
# mcu_ready is already high.
build/core1/system_core1.o: src/system_core1.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/fsl_gpio.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_gpio.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_clock.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_clock.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_reset.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_reset.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_edma.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_edma.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_flexio.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_flexio.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_flexio_mculcd.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_flexio_mculcd.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_flexio_mculcd_edma.o: $(VENDOR_SDK)/devices/MCXN947/drivers/fsl_flexio_mculcd_edma.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_dbi_flexio_edma.o: $(VENDOR_SDK)/components/video/display/dbi/flexio/fsl_dbi_flexio_edma.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/fsl_st7796s.o: $(VENDOR_SDK)/components/display/st7796s/fsl_st7796s.c | build/core1
	$(CC) $(CORE1_CFLAGS) $(VENDOR_WARNINGS) -c $< -o $@

build/core1/link_snapshot.o: src/link_snapshot.c src/link_snapshot.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/shared_window.o: src/shared_window.c src/shared_window.h \
		src/link_snapshot.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/status_led.o: src/status_led.c src/status_led.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/text_grid.o: src/text_grid.c src/text_grid.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/glyphs.o: src/glyphs.c src/glyphs.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/display_panel.o: src/display_panel.c src/display_panel.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/display.o: src/display.c src/display.h src/display_panel.h \
		src/glyphs.h src/text_grid.h src/link_snapshot.h \
		include/injection_wire.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core1/main_core1.o: src/main_core1.c src/shared_window.h \
		src/link_snapshot.h src/status_led.h src/display.h | build/core1
	$(CC) $(CORE1_CFLAGS) $(APP_WARNINGS) -c $< -o $@

build/core0 build/core1 build/host:
	mkdir -p $@

# --- the gate ---------------------------------------------------------------

CHECK_OBJECT_ARGS := \
	$(foreach o,$(CORE0_OBJECTS),--object core0.elf:$(o)) \
	$(foreach o,$(CORE1_OBJECTS),--object core1.elf:$(o))
CHECK_RETAIN_ARGS := \
	$(foreach r,$(CORE0_RETAIN),--retain core0.elf:$(r)) \
	$(foreach r,$(CORE1_RETAIN),--retain core1.elf:$(r))

CORE0_FORBIDDEN := (?i)flexio (?i)edma1 EDMA_1_ ST7796S DBI \
	(?i)display (?i)text_grid (?i)glyphs (?i)status_led
CORE1_FORBIDDEN := (?i)lpspi6 LP_FLEXCOMM6 (?i)edma0 EDMA_0_ CTIMER2 SPC GDET ITRC
CHECK_FORBIDDEN_ARGS := \
	$(foreach p,$(CORE0_FORBIDDEN),--forbid 'core0.elf:$(p)') \
	$(foreach p,$(CORE1_FORBIDDEN),--forbid 'core1.elf:$(p)')

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
		--core1 build/core1.elf \
		--core0-bin build/hurra-mcxn947-core0.bin \
		--core1-bin build/hurra-mcxn947-core1.bin \
		--merged-bin build/hurra-mcxn947.bin \
		--core0-flash-start $(CORE0_FLASH_START) \
		--core0-flash-end $(CORE0_FLASH_END) \
		--core1-offset $(CORE1_OFFSET) \
		--flash-limit $(FLASH_LIMIT) \
		--core0-ram-start $(CORE0_RAM_START) \
		--core0-ram-end $(CORE0_RAM_END) \
		--core1-ram-start $(CORE1_RAM_START) \
		--core1-ram-end $(CORE1_RAM_END) \
		--ram-shared-start $(RAM_SHARED_START) \
		--ram-shared-end $(RAM_SHARED_END) \
		--shared-symbol g_shared_window \
		--shared-window-size $(SHARED_WINDOW_SIZE) \
		--stack-size $(STACK_SIZE) --heap-size $(HEAP_SIZE) \
		$(CHECK_RETAIN_ARGS) $(CHECK_OBJECT_ARGS) $(CHECK_FORBIDDEN_ARGS)

# --- host tests -------------------------------------------------------------
#
# These compile the *same* sources the target build uses, without -DMCXN947.
# They are the demonstration that the guard polarity holds: if a module ever
# grows MMIO outside its guard, the host build of it stops compiling here.

test: test-platform test-heartbeat test-spi-frame test-link test-link-retire \
	test-link-recovery test-console test-core1-release test-shared-window \
	test-link-snapshot test-status-led test-text-grid test-glyphs \
	test-display-panel test-display test-inj-command test-inj-session \
	test-merge-images

test-merge-images:
	python3 test/merge_mcxn947_images_test.py

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

test-inj-command: build/host/inj_command_test
	./build/host/inj_command_test

build/host/inj_command_test: test/inj_command_test.c src/inj_command.c src/spi_frame.c \
		src/inj_command.h src/spi_frame.h include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/inj_command_test.c \
		src/inj_command.c src/spi_frame.c -o $@

test-inj-session: build/host/inj_session_test
	./build/host/inj_session_test

build/host/inj_session_test: test/inj_session_test.c src/inj_session.c src/inj_command.c \
		src/spi_frame.c src/inj_session.h src/inj_command.h src/spi_frame.h \
		include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/inj_session_test.c \
		src/inj_session.c src/inj_command.c src/spi_frame.c -o $@

test-link: build/host/link_test
	./build/host/link_test

build/host/link_test: test/link_test.c src/link.c src/spi_frame.c src/link.h \
		src/spi_frame.h include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/link_test.c src/link.c src/spi_frame.c -o $@

test-link-retire: build/host/link_retire_test
	./build/host/link_retire_test

build/host/link_retire_test: test/link_retire_test.c src/link_retire.c src/spi_frame.c \
		src/link_retire.h src/spi_frame.h include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/link_retire_test.c \
		src/link_retire.c src/spi_frame.c -o $@

test-link-recovery: build/host/link_recovery_test
	./build/host/link_recovery_test

# Note what is NOT on this recipe: -isystem $(VENDOR_TUSB)/src. The console is
# one of the two modules design doc section 7 calls portable by construction,
# and a TinyUSB include creeping into it would break this build rather than
# pass unnoticed into the image.
test-console: build/host/console_test
	./build/host/console_test

build/host/console_test: test/console_test.c src/console.c src/console.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/console_test.c src/console.c -o $@

test-core1-release: build/host/core1_release_test
	./build/host/core1_release_test

build/host/core1_release_test: test/core1_release_test.c src/core1_release.c \
		src/core1_release.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/core1_release_test.c src/core1_release.c -o $@

test-shared-window: build/host/shared_window_test
	./build/host/shared_window_test

build/host/shared_window_test: test/shared_window_test.c src/shared_window.c \
		src/shared_window.h src/link_snapshot.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/shared_window_test.c src/shared_window.c -o $@

test-link-snapshot: build/host/link_snapshot_test
	./build/host/link_snapshot_test

build/host/link_snapshot_test: test/link_snapshot_test.c src/link_snapshot.c \
		src/link_snapshot.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -DLINK_SNAPSHOT_TESTING -Isrc \
		test/link_snapshot_test.c src/link_snapshot.c -o $@

test-status-led: build/host/status_led_test
	./build/host/status_led_test

build/host/status_led_test: test/status_led_test.c src/status_led.c src/status_led.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/status_led_test.c src/status_led.c -o $@

test-text-grid: build/host/text_grid_test
	./build/host/text_grid_test

build/host/text_grid_test: test/text_grid_test.c src/text_grid.c src/text_grid.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/text_grid_test.c src/text_grid.c -o $@

test-glyphs: build/host/glyphs_test
	./build/host/glyphs_test

build/host/glyphs_test: test/glyphs_test.c src/glyphs.c src/glyphs.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/glyphs_test.c src/glyphs.c -o $@

test-display-panel: build/host/display_panel_test
	./build/host/display_panel_test

build/host/display_panel_test: test/display_panel_test.c src/display_panel.c \
		src/display_panel.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/display_panel_test.c \
		src/display_panel.c -o $@

test-display: build/host/display_test
	./build/host/display_test

build/host/display_test: test/display_test.c src/display.c src/display.h \
		src/text_grid.c src/text_grid.h src/glyphs.c src/glyphs.h \
		src/link_snapshot.h include/injection_wire.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc -isystem include test/display_test.c \
		src/display.c src/text_grid.c src/glyphs.c -o $@

build/host/link_recovery_test: test/link_recovery_test.c src/link_recovery.c \
		src/link_recovery.h | build/host
	$(HOST_CC) $(APP_WARNINGS) -Isrc test/link_recovery_test.c src/link_recovery.c -o $@

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

# -u none is not an optimisation, it is the difference between working and
# hanging. LinkServer defaults to `--update-mode check`, which probes the
# MCU-Link's own firmware version before it will talk to the target. Measured
# 2026-09-16 on this setup: the default stalled for over three minutes with no
# output and had to be killed, twice; the same command with `-u none`
# completed in about eight seconds. The target was live and healthy throughout
# both attempts -- the FPGA's link_ready stayed high -- so the stall is in the
# probe handshake, not the part. Pass `-u auto` by hand on the rare occasion
# the probe firmware actually needs updating.
LINKSERVER_FLAGS ?= -u none

# ELFs carry their own load addresses. One LinkServer transaction programs
# them in CPU0-then-CPU1 order and resets only after both are present.
flash: build/core0.elf build/core1.elf
	$(LINKSERVER) flash $(LINKSERVER_FLAGS) $(LINKSERVER_DEVICE) load build/core0.elf build/core1.elf

# Gate configuration (a): erase the whole device, then install only CPU0.
# CPU0 must still raise mcu_ready and run the link with the core1 region blank.
#
# `load -e` and NOT a separate `erase` followed by `load`. Measured
# 2026-09-16: the two-command form left the target sitting at the boot-ROM
# stall -- its log is missing the `restart on reset` line the single-command
# form prints -- so the part never ran the image that had just been written.
# That failure is deceptive from the FPGA side: mcu_ready floats high with the
# MCU halted, so link_ready reads 1 and every returned slot is garbage,
# producing spi_bad_sof saturated 1:1 with spi_slots. That is pixel-identical
# to the boot-random frame offset of design doc section 10, and it cost a gate
# run to tell the two apart. One transaction, one reset, no ambiguity.
flash-core0-only: build/core0.elf
	$(LINKSERVER) flash $(LINKSERVER_FLAGS) $(LINKSERVER_DEVICE) load -e build/core0.elf

flash-merged: build/hurra-mcxn947.bin
	$(LINKSERVER) flash $(LINKSERVER_FLAGS) $(LINKSERVER_DEVICE) load --addr 0x0 $<

verify-core1-blank:
	$(LINKSERVER) flash $(LINKSERVER_FLAGS) $(LINKSERVER_DEVICE) blank --addr $(CORE1_OFFSET) --size 0x40000

probes:
	$(LINKSERVER) probes

clean:
	rm -rf build
