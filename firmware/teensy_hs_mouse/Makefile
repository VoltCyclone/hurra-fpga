# Synthetic High Speed HID mouse for validating the hurra-cynthion relay.
# Target: SparkFun MicroMod Teensy (iMXRT1062) on a MicroMod ATP carrier.
#
# The USB device stack, startup code and linker script are vendored from
# Hurra-v2; see PROVENANCE.md. Only src/ is written here.

TOOLCHAIN_BIN ?= $(shell \
	if [ -d $(HOME)/.platformio/packages/toolchain-gccarmnoneeabi-teensy/bin ]; then \
		echo $(HOME)/.platformio/packages/toolchain-gccarmnoneeabi-teensy/bin; \
	elif command -v arm-none-eabi-gcc >/dev/null 2>&1; then \
		dirname `command -v arm-none-eabi-gcc`; \
	fi)
CC      = $(TOOLCHAIN_BIN)/arm-none-eabi-gcc
OBJCOPY = $(TOOLCHAIN_BIN)/arm-none-eabi-objcopy
SIZE    = $(TOOLCHAIN_BIN)/arm-none-eabi-size

TARGET = teensy_hs_mouse
MCU_FLAGS = -mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb

# 600 MHz is the part's rated maximum. Hurra-v2 overclocks to 912 MHz for its
# own latency budget; this instrument has none -- it emits one 4-byte report
# every 125 us, which is ~75,000 cycles of headroom at the rated clock. Running
# in spec keeps the timebase trustworthy, and the whole point here is that the
# measured report rate means something.
F_CPU ?= 600000000

# Report period in microseconds. 125 = 8000 Hz, a rate only High Speed can
# carry; Full Speed schedules interrupt endpoints on 1 ms frames. Override to
# compare, e.g. `make REPORT_PERIOD_US=1000` for a 1000 Hz Full-Speed-legal rate.
REPORT_PERIOD_US ?= 125

# Transmit only every Nth generated report, while still generating one every
# REPORT_PERIOD_US. 1 (the default) is the instrument's normal behaviour.
#
# This exists to make the *generation* rate observable from the host, which
# otherwise it is not: the relay can only count reports it receives, so a
# device that generates 8000/sec and delivers 2000 is indistinguishable from
# one that generates 2000 and delivers all of them. Sweeping REPORT_PERIOD_US
# cannot separate those either -- both are min(generation, delivery) over the
# same two numbers, so they predict the same rate at every period. Dividing
# the transmit rate by a known N does separate them: the observed rate becomes
# generation/N, so N=8 reads 250/sec if generation is loop-bound at 2000/sec
# and 1000/sec if it is the full 8000/sec.
DIAG_SEND_EVERY ?= 1

DEFINES = -DARDUINO_TEENSY_MICROMOD -D__IMXRT1062__ -DF_CPU=$(F_CPU) \
          -DREPORT_PERIOD_US=$(REPORT_PERIOD_US) \
          -DDIAG_SEND_EVERY=$(DIAG_SEND_EVERY)

CFLAGS = $(MCU_FLAGS) $(DEFINES) \
         -Os -Wall \
         -ffunction-sections -fdata-sections \
         -flto -fsingle-precision-constant \
         -Ivendor/include -Ivendor/src -Isrc \
         -MD -MP

LDFLAGS = $(MCU_FLAGS) \
          -Tvendor/core/imxrt1062_mm.ld \
          -Wl,--gc-sections \
          -Wl,-Map=$(TARGET).map \
          -Wl,--print-memory-usage \
          -flto -fuse-linker-plugin \
          --specs=nano.specs --specs=nosys.specs

SRC = vendor/core/startup.c vendor/core/bootdata.c vendor/src/usb_device.c \
      src/main.c src/usb_host_shim.c
OBJ = $(SRC:.c=.o)

# Every object depends on the exact set of -D flags it was compiled with.
#
# Without this, `make REPORT_PERIOD_US=250` compiles nothing: make decides what
# is stale from timestamps and header dependencies, and a command-line define
# changes neither. The objects are up to date, so the recipe runs `size` and
# exits, and the flashed binary still carries the old period -- an override
# that silently does nothing, and that reads as "the report period made no
# difference to the measured rate". The stamp turns it into a rebuild.
DEFINE_STAMP = .build-defines
$(shell printf '%s' '$(DEFINES)' | cmp -s - $(DEFINE_STAMP) 2>/dev/null \
        || printf '%s' '$(DEFINES)' > $(DEFINE_STAMP))

all: $(TARGET).hex
	@$(SIZE) $(TARGET).elf

$(TARGET).elf: $(OBJ)
	$(CC) $(LDFLAGS) -o $@ $^ -lm

$(TARGET).hex: $(TARGET).elf
	$(OBJCOPY) -O ihex -R .eeprom $< $@

# The report loop and the USB device stack are the only timing-sensitive code.
vendor/src/usb_device.o src/main.o: CFLAGS := $(patsubst -O%,,$(CFLAGS)) -O2

$(OBJ): $(DEFINE_STAMP)

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<

flash: $(TARGET).hex
	teensy_loader_cli --mcu=TEENSY_MICROMOD -w -v $(TARGET).hex

clean:
	rm -f $(OBJ) $(OBJ:.o=.d) $(TARGET).elf $(TARGET).hex $(TARGET).map $(DEFINE_STAMP)

.PHONY: all flash clean
