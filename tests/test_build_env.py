"""Tests for build-environment composition.

Pure Python. No hardware, no toolchain, no build.
"""

import os

import pytest

from hurra_cynthion.build_env import (
    DETERMINISM_VARS,
    NEXTPNR_OPTS_VAR,
    REQUIRED_NEXTPNR_FLAG,
    REQUIRED_NEXTPNR_OPTS,
    apply_build_environment,
    compose_nextpnr_opts,
    require_nextpnr_opts,
)


def test_compose_adds_the_required_option_when_unset():
    assert compose_nextpnr_opts(None) == REQUIRED_NEXTPNR_OPTS
    assert compose_nextpnr_opts("") == REQUIRED_NEXTPNR_OPTS
    assert compose_nextpnr_opts("   ") == REQUIRED_NEXTPNR_OPTS


def test_compose_preserves_a_user_supplied_option():
    composed = compose_nextpnr_opts("--seed 4")
    assert REQUIRED_NEXTPNR_FLAG in composed
    assert "--seed 4" in composed
    # Ours first, so a deliberate later flag wins where nextpnr takes the last.
    assert composed.index(REQUIRED_NEXTPNR_FLAG) < composed.index("--seed 4")


def test_compose_is_idempotent():
    """Blind prepending would duplicate the flag here."""
    once = compose_nextpnr_opts(None)
    twice = compose_nextpnr_opts(once)
    assert twice == once
    assert twice.count(REQUIRED_NEXTPNR_FLAG) == 1


def test_require_rejects_a_string_without_the_timing_weight():
    with pytest.raises(SystemExit) as excinfo:
        require_nextpnr_opts("--seed 4")
    assert REQUIRED_NEXTPNR_FLAG in str(excinfo.value)


def test_require_accepts_a_string_carrying_the_flag():
    require_nextpnr_opts(REQUIRED_NEXTPNR_OPTS)
    require_nextpnr_opts(f"--seed 4 {REQUIRED_NEXTPNR_OPTS}")


def test_apply_build_environment_writes_the_composed_value():
    env: dict[str, str] = {}
    before = os.environ.get(NEXTPNR_OPTS_VAR)

    returned = apply_build_environment(env)

    assert env[NEXTPNR_OPTS_VAR] == returned
    assert REQUIRED_NEXTPNR_FLAG in env[NEXTPNR_OPTS_VAR]
    # The real environment must be untouched when an explicit mapping is given.
    assert os.environ.get(NEXTPNR_OPTS_VAR) == before


def test_apply_build_environment_is_idempotent():
    env: dict[str, str] = {}
    apply_build_environment(env)
    first = env[NEXTPNR_OPTS_VAR]
    apply_build_environment(env)
    assert env[NEXTPNR_OPTS_VAR] == first
    assert env[NEXTPNR_OPTS_VAR].count(REQUIRED_NEXTPNR_FLAG) == 1


def test_apply_build_environment_preserves_a_user_option():
    env = {NEXTPNR_OPTS_VAR: "--seed 4"}
    apply_build_environment(env)
    assert "--seed 4" in env[NEXTPNR_OPTS_VAR]
    assert REQUIRED_NEXTPNR_FLAG in env[NEXTPNR_OPTS_VAR]


def test_apply_build_environment_pins_the_solver_thread_counts():
    """Placement is only reproducible when the Eigen/OpenMP threading is pinned.

    nextpnr's --threads does not cover the HeAP placer's solver, so without
    these the same netlist and seed place differently depending on machine
    load. A pinned seed depends on this holding.
    """
    env: dict[str, str] = {}
    apply_build_environment(env)
    for name, value in DETERMINISM_VARS.items():
        assert env[name] == value


def test_apply_build_environment_overrides_inherited_thread_counts():
    """An unrelated exported OMP_NUM_THREADS must not silently cost determinism."""
    env = {"OMP_NUM_THREADS": "8"}
    apply_build_environment(env)
    assert env["OMP_NUM_THREADS"] == "1"


def test_determinism_vars_do_not_touch_the_real_environment():
    before = {name: os.environ.get(name) for name in DETERMINISM_VARS}
    apply_build_environment({})
    assert {name: os.environ.get(name) for name in DETERMINISM_VARS} == before
