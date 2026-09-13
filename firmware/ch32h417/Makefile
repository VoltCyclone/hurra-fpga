TOOLCHAIN ?= riscv-none-elf

TOOLCHAIN_BIN ?= $(shell \
	if command -v $(TOOLCHAIN)-gcc >/dev/null 2>&1; then \
		dirname `command -v $(TOOLCHAIN)-gcc`; \
	else \
		ls -d $(HOME)/Library/xPacks/@xpack-dev-tools/riscv-none-elf-gcc/*/.content/bin \
			2>/dev/null | sort -V | tail -1; \
	fi)
TOOLCHAIN_PREFIX := $(if $(TOOLCHAIN_BIN),$(TOOLCHAIN_BIN)/,)$(TOOLCHAIN)

CC      := $(TOOLCHAIN_PREFIX)-gcc
OBJCOPY := $(TOOLCHAIN_PREFIX)-objcopy
SIZE    := $(TOOLCHAIN_PREFIX)-size
HOST_CC ?= cc

# zifencei enables the `fence.i` opcode the WCH core_riscv.h emits in
# NVIC_DisableIRQ / __disable_irq (an instruction-fetch fence after masking).
# The V5F link's poll critical section reaches it; kept on both cores since the
# vendor core header is shared and the extension is purely additive.
V3F_ARCH := -march=rv32imac_zicsr_zifencei -mabi=ilp32
V5F_ARCH := -march=rv32imafc_zicsr_zifencei -mabi=ilp32f

# Flash geometry. Written here once and consumed by both the merge tool and the
# image gate, so the offset and the ceiling cannot drift apart.
#   V5F_OFFSET       core/link_v5f.ld:12  FLASH ORIGIN = 0x00010000
#   V5F_FLASH_LEN    core/link_v5f.ld:12  FLASH LENGTH = 128K
#   V3F_FLASH_LEN    core/link_v3f.ld:12  FLASH LENGTH = 64K (== V5F_OFFSET)
V5F_OFFSET      := 0x10000
# V5F core clock of the profile enabled in core/system_ch32h417.c:20
# (SYSCLK_400M_CoreCLK_V5F_400M_V3F_100M_HSE). The FPU divider gate is
# parameterised on this, so the profile and the check cannot drift apart.
V5F_CORE_HZ     := 400000000
V5F_FLASH_LEN   := 0x20000
V3F_FLASH_LEN   := 0x10000
FLASH_LIMIT     := 0x30000

# RAM geometry. These mirror the pristine linker scripts and are consumed by
# the image gate, so the checker has no second hard-coded copy.
#   V3F RAM       core/link_v3f.ld:14  0x20110100..0x20178000
#   V5F RAM       core/link_v5f.ld:14  0x200C0300..0x20100000
#   RAM_SHARED    core/link_v3f.ld:16  0x20178000..0x20180000
#   STACK_SIZE    both linker scripts:3
V3F_RAM_START    := 0x20110100
V3F_RAM_END      := 0x20178000
V5F_RAM_START    := 0x200C0300
V5F_RAM_END      := 0x20100000
RAM_SHARED_START := 0x20178000
RAM_SHARED_END   := 0x20180000
STACK_SIZE       := 2048

INCLUDES := -Iinclude -Isrc -Icore \
	-isystem vendor/wch/Core -isystem vendor/wch/Debug \
	-isystem vendor/wch/Peripheral/inc
COMMON_CFLAGS := -DCH32H417 $(INCLUDES) -Os -ffunction-sections \
	-fdata-sections -fno-common
APP_WARNINGS := -Wall -Wextra -Werror -Wconversion -Wshadow
PLATFORM_WARNINGS := -Wall -Wextra -Wno-unused-variable
# WCH sources are preserved byte-for-byte. Their only current suppression is
# for file-local helpers that are unused in this selected-driver build.
VENDOR_WARNINGS := -Wall -Wextra -Wno-unused-function -Wno-unused-variable
COMMON_LDFLAGS := -nostartfiles -Wl,--gc-sections \
	--specs=nano.specs --specs=nosys.specs

VENDOR_DRIVERS := dma eth gpio iwdg rcc spi tim
V5F_VENDOR_OBJECTS := $(addprefix build/v5f/vendor_,$(addsuffix .o,$(VENDOR_DRIVERS)))

# The V3F links the StdPeriph drivers that SystemInit() itself needs, and no
# others. core/system_ch32h417.c:131 has SetSysClock() call GPIO_IPD_Unused()
# unconditionally; that lives in ch32h417_gpio.c, which in turn calls
# RCC_HB1/HB2PeriphClockCmd (ch32h417_rcc.c) and FLASH_BOOT_GetMode
# (ch32h417_flash.c). Without these three the V3F image does not link at all
# once main() calls SystemInit(). The reference platform sidesteps this by
# linking `wildcard vendor/wch/Peripheral/src/*.c` into both cores and letting
# --gc-sections sort it out; this tree vendors a selected driver set instead,
# so the dependency has to be named.
V3F_VENDOR_DRIVERS := flash gpio rcc
V3F_VENDOR_OBJECTS := $(addprefix build/v3f/vendor_,$(addsuffix .o,$(V3F_VENDOR_DRIVERS)))

V3F_OBJECTS := \
	build/v3f/startup.o \
	build/v3f/main.o \
	build/v3f/trap_witness.o \
	build/v3f/system.o \
	build/v3f/core_riscv.o \
	$(V3F_VENDOR_OBJECTS)

V5F_OBJECTS := \
	build/v5f/startup.o \
	build/v5f/main.o \
	build/v5f/trap_witness.o \
	build/v5f/spi_frame.o \
	build/v5f/ch32_link.o \
	build/v5f/usb_cdc_fs.o \
	build/v5f/usb_time.o \
	build/v5f/watchdog.o \
	build/v5f/status_led.o \
	build/v5f/ws2812.o \
	build/v5f/ws2812_pioc_code.o \
	build/v5f/system.o \
	build/v5f/core_riscv.o \
	build/v5f/timebase.o \
	$(V5F_VENDOR_OBJECTS)

# Retention roots for the V5F link. --gc-sections discards any section with no
# path from an entry point, so a translation unit that is compiled and linked
# but not yet called is silently thrown away -- which is defect W8, and how W4
# survived.
#
# Two kinds of root live here:
#   ISR handlers  -- the vector table in startup_v5f.S references them .weak, so
#                    without a strong-symbol root --gc-sections drops the real
#                    handler and the weak default-trap stub is linked instead.
#                    These are load-bearing: TIM3 tick, DMA1_Channel2 (SPI RX),
#                    TIM8 CC/UP (USB-frame capture), USBFS (CDC).
#   Module entry  -- timebase/ch32_link/usb_time/watchdog/status_led init+poll.
#                    Now that main_v5f.c's service loop calls all of these, the
#                    call graph from main already retains them; these entries are
#                    redundant but kept as a defensive backstop (and a cheap
#                    no-op). A later cleanup may trim them once the loop is
#                    settled -- doing so is its own check_images-gated change,
#                    not folded into a feature commit.
# Kept here rather than as KEEP() in core/link_v5f.ld, which is byte-identical
# to the reference platform and is the least-reviewed file in the tree.
V5F_LDFLAGS := \
	-Wl,--undefined=TIM3_IRQHandler \
	-Wl,--undefined=DMA1_Channel2_IRQHandler \
	-Wl,--undefined=TIM8_CC_IRQHandler \
	-Wl,--undefined=TIM8_UP_IRQHandler \
	-Wl,--undefined=USBFS_IRQHandler \
	-Wl,--undefined=millis \
	-Wl,--undefined=timebase_init \
	-Wl,--undefined=ch32_link_init \
	-Wl,--undefined=ch32_link_poll \
	-Wl,--undefined=usb_time_init \
	-Wl,--undefined=usb_time_poll \
	-Wl,--undefined=watchdog_init \
	-Wl,--undefined=watchdog_poll \
	-Wl,--undefined=status_led_init \
	-Wl,--undefined=status_led_poll

.PHONY: all check clean test test-spi-frame test-link-logic test-usb-time \
	test-status-led test-watchdog

all: build/hurra-ch32h417.bin

build/hurra-ch32h417.bin: build/v3f.bin build/v5f.bin tools/merge_images.py
	python3 tools/merge_images.py build/v3f.bin build/v5f.bin $(V5F_OFFSET) $@ \
		$(FLASH_LIMIT)

build/v3f.bin: build/v3f.elf
	$(OBJCOPY) -O binary $< $@

build/v5f.bin: build/v5f.elf
	$(OBJCOPY) -O binary $< $@

build/v3f.elf: $(V3F_OBJECTS) core/link_v3f.ld
	$(CC) $(V3F_ARCH) $(COMMON_LDFLAGS) -Tcore/link_v3f.ld \
		-o $@ $(V3F_OBJECTS)
	$(SIZE) $@

build/v5f.elf: $(V5F_OBJECTS) core/link_v5f.ld
	$(CC) $(V5F_ARCH) $(COMMON_LDFLAGS) $(V5F_LDFLAGS) -Tcore/link_v5f.ld \
		-o $@ $(V5F_OBJECTS)
	$(SIZE) $@

build/v3f/startup.o: core/startup_v3f.S | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) -DCore_V3F -c $< -o $@

build/v5f/startup.o: core/startup_v5f.S | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) -DCore_V5F -Dsystick2 -c $< -o $@

build/v3f/main.o: src/main_v3f.c | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V3F -c $< -o $@

build/v5f/main.o: src/main_v5f.c | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v3f/trap_witness.o: src/trap_witness.c src/trap_witness.h src/shared_window.h | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V3F -c $< -o $@

build/v5f/trap_witness.o: src/trap_witness.c src/trap_witness.h src/shared_window.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/spi_frame.o: src/spi_frame.c src/spi_frame.h include/injection_wire.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/ch32_link.o: src/ch32_link.c src/ch32_link.h src/board.h \
		src/spi_frame.h include/injection_wire.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/usb_cdc_fs.o: src/usb_cdc_fs.c src/usb_cdc_fs.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/usb_time.o: src/usb_time.c src/usb_time.h src/board.h \
		src/spi_frame.h include/injection_wire.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/watchdog.o: src/watchdog.c src/watchdog.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/status_led.o: src/status_led.c src/status_led.h src/ws2812.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/ws2812.o: src/ws2812.c src/ws2812.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/ws2812_pioc_code.o: src/ws2812_pioc_code.c | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(APP_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v3f/system.o: core/system_ch32h417.c | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) $(PLATFORM_WARNINGS) -DCore_V3F \
		-c $< -o $@

build/v5f/timebase.o: core/timebase.c core/timebase.h | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(PLATFORM_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v5f/system.o: core/system_ch32h417.c | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(PLATFORM_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v3f/core_riscv.o: vendor/wch/Core/core_riscv.c | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) $(PLATFORM_WARNINGS) -DCore_V3F \
		-c $< -o $@

build/v5f/core_riscv.o: vendor/wch/Core/core_riscv.c | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(PLATFORM_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/v3f/vendor_%.o: vendor/wch/Peripheral/src/ch32h417_%.c | build/v3f
	$(CC) $(V3F_ARCH) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -DCore_V3F \
		-c $< -o $@

build/v5f/vendor_%.o: vendor/wch/Peripheral/src/ch32h417_%.c | build/v5f
	$(CC) $(V5F_ARCH) $(COMMON_CFLAGS) $(VENDOR_WARNINGS) -DCore_V5F -Dsystick2 \
		-c $< -o $@

build/host build/v3f build/v5f:
	mkdir -p $@

# Static verification ladder. `all` exits 0 while --gc-sections discards the
# application, so this asserts on the built artifacts instead. The object lists
# above are the source of truth for what must survive into each image.
CHECK_OBJECT_ARGS := \
	$(foreach o,$(V3F_OBJECTS),--object v3f.elf:$(o)) \
	$(foreach o,$(V5F_OBJECTS),--object v5f.elf:$(o))

check: all
	python3 tools/check_images.py --self-test
	python3 tools/check_images.py --prefix $(TOOLCHAIN_PREFIX) \
		--v3f build/v3f.elf --v5f build/v5f.elf \
		--merged build/hurra-ch32h417.bin \
		--v5f-offset $(V5F_OFFSET) --v5f-core-hz $(V5F_CORE_HZ) \
		--v3f-region $(V3F_FLASH_LEN) \
		--v5f-region $(V5F_FLASH_LEN) --flash-limit $(FLASH_LIMIT) \
		--v3f-ram-start $(V3F_RAM_START) --v3f-ram-end $(V3F_RAM_END) \
		--v5f-ram-start $(V5F_RAM_START) --v5f-ram-end $(V5F_RAM_END) \
		--stack-size $(STACK_SIZE) \
		--ram-shared-start $(RAM_SHARED_START) --ram-shared-end $(RAM_SHARED_END) \
		--expect-mmio-store v3f.elf:main:0xE000E724:0x00010000 \
		$(CHECK_OBJECT_ARGS)

test: test-spi-frame test-link-logic test-usb-time test-status-led test-watchdog

test-spi-frame: build/host/spi_frame_test
	./build/host/spi_frame_test

build/host/spi_frame_test: test/spi_frame_test.c src/spi_frame.c \
		src/spi_frame.h include/injection_wire.h | build/host
	$(HOST_CC) -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-isystem include -Isrc test/spi_frame_test.c src/spi_frame.c -o $@

# The link retirement/queue logic is MMIO-free: the SPI1/DMA/ISR half of
# ch32_link.c is behind `#if defined(CH32H417)`, which this host build does not
# set, so the same source compiles on the host and the test drives it with
# fabricated completed slots.
test-link-logic: build/host/ch32_link_logic_test
	./build/host/ch32_link_logic_test

build/host/ch32_link_logic_test: test/ch32_link_logic_test.c src/ch32_link.c \
		src/spi_frame.c src/ch32_link.h src/spi_frame.h \
		include/injection_wire.h | build/host
	$(HOST_CC) -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-isystem include -Isrc test/ch32_link_logic_test.c src/ch32_link.c \
		src/spi_frame.c -o $@

# usb_time's timing/sync core is MMIO-free; the TIM8 capture/overflow half is
# behind `#if defined(CH32H417)`, off in this host build.
test-usb-time: build/host/usb_time_test
	./build/host/usb_time_test

build/host/usb_time_test: test/usb_time_test.c src/usb_time.c src/usb_time.h \
		| build/host
	$(HOST_CC) -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-isystem include -Isrc test/usb_time_test.c src/usb_time.c -o $@

# status_led's priority/style policy is MMIO-free; the WS2812/PIOC drive is
# behind `#if defined(CH32H417)`, off in this host build.
test-status-led: build/host/status_led_test
	./build/host/status_led_test

build/host/status_led_test: test/status_led_test.c src/status_led.c \
		src/status_led.h | build/host
	$(HOST_CC) -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-isystem include -Isrc test/status_led_test.c src/status_led.c -o $@

# watchdog gating is MMIO-free; the IWDG poking is behind `#if defined(CH32H417)`.
test-watchdog: build/host/watchdog_test
	./build/host/watchdog_test

build/host/watchdog_test: test/watchdog_test.c src/watchdog.c src/watchdog.h \
		| build/host
	$(HOST_CC) -std=c11 -Wall -Wextra -Werror -Wconversion -Wshadow \
		-isystem include -Isrc test/watchdog_test.c src/watchdog.c -o $@

clean:
	rm -rf build
