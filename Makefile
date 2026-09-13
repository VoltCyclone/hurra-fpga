.PHONY: build firmware firmware-check firmware-test lint package rtlil test verify

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
