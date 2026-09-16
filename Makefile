.PHONY: build container container-image container-synth container-sweep firmware \
        firmware-check firmware-test lint package rtlil test verify

# Pinned ECP5 toolchain image. yosys >= 0.60 is load-bearing; see docker/README.md.
IMAGE ?= ecp5-toolchain:2026-09-01
OUT   ?= out
SEEDS ?= 1 2 3 4 5 6 7 8 9 10 11 12

build:
	@test -n "$(LUNA_PLATFORM)" || { echo "Set LUNA_PLATFORM explicitly (see README.md)"; exit 2; }
	mkdir -p build
	PYTHONPATH=src python3 -m hurra_cynthion.gateware --output build/hurra-cynthion.bit

firmware:
	$(MAKE) -C firmware/ch32h417 clean all

firmware-test:
	$(MAKE) -C firmware/ch32h417 test

firmware-check:
	$(MAKE) -C firmware/ch32h417 check

package:
	python3 -m build

lint:
	ruff check src tests
	ruff format --check src tests

rtlil:
	PYTHONPATH=src python3 -m hurra_cynthion.gateware --rtlil build/host.il

test:
	pytest

verify: lint test rtlil firmware-test firmware-check firmware

container-image:
	docker build -t $(IMAGE) docker/

# Synthesis is identical across seeds, so it runs once and every seed in the
# sweep places the same netlist.
container-synth: container-image
	mkdir -p $(OUT)
	docker run --rm -v "$(CURDIR):/work" -v "$(CURDIR)/$(OUT):/out" \
	  -e REPO=/work -e OUT=/out $(IMAGE) synth.sh

# Parallel fan-out. Safe to parallelise only because the image pins the
# Eigen/OpenMP thread counts to 1.
container-sweep:
	docker run --rm -v "$(CURDIR)/$(OUT):/out" $(IMAGE) \
	  sweep.sh /out/top.json /out/top.lpf /out/sweep $(SEEDS)

container: container-synth container-sweep
