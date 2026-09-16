"""Tests for build-environment composition.

Pure Python. No hardware, no toolchain, no build.
"""

import os

import pytest

from hurra_cynthion.build_env import (
    DETERMINISM_VARS,
    MINIMUM_YOSYS_VERSION,
    NEXTPNR_OPTS_VAR,
    REQUIRED_NEXTPNR_FLAG,
    REQUIRED_NEXTPNR_OPTS,
    apply_build_environment,
    compose_nextpnr_opts,
    parse_yosys_version,
    require_nextpnr_opts,
    require_yosys_version,
    resolve_yosys_executable,
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


def test_parse_yosys_version_reads_the_real_banner_format():
    """Exact banners emitted by the four versions this design was measured on."""
    assert parse_yosys_version("Yosys 0.68+136 (git sha1 c304574, clang++)") == (0, 68)
    assert parse_yosys_version("Yosys 0.60 (git sha1 5bafeb77, clang++)") == (0, 60)
    assert parse_yosys_version("Yosys 0.53+15 (git sha1 690081810, clang++)") == (0, 53)
    assert parse_yosys_version("Yosys 0.48+47 (git sha1 cbb95cb51, clang++)") == (0, 48)


def test_parse_yosys_version_returns_none_when_absent():
    assert parse_yosys_version("") is None
    assert parse_yosys_version("nextpnr-ecp5 -- Next Generation Place and Route") is None


def test_require_yosys_version_rejects_versions_below_the_floor():
    """Below the floor, closure is a coin toss rather than impossible.

    0.48 passes 3 of 12 placer seeds on a deterministic netlist where 0.68
    passes 12 of 12. The guard enforces a reliability floor; it is not
    asserting that an older yosys can never emit a bitstream.
    """
    for version in ((0, 48), (0, 53), (0, 59)):
        with pytest.raises(SystemExit) as excinfo:
            require_yosys_version(version)
        assert "too old" in str(excinfo.value)


def test_require_yosys_version_accepts_the_floor_and_above():
    require_yosys_version(MINIMUM_YOSYS_VERSION)
    require_yosys_version((0, 68))
    require_yosys_version((1, 0))


def test_require_yosys_version_tolerates_an_unknown_version():
    """A missing or unparseable yosys is not this guard's error to raise."""
    require_yosys_version(None)


def test_apply_build_environment_does_not_probe_yosys_by_default(monkeypatch):
    """The default path must never shell out to a toolchain.

    CI's test job has no yosys at all, and a developer whose PATH resolves to an
    older yosys must still be able to run pytest. Only the bitstream entry point
    passes ``enforce_yosys=True``.
    """

    def explode() -> None:
        raise AssertionError("apply_build_environment probed yosys by default")

    monkeypatch.setattr("hurra_cynthion.build_env.detect_yosys_version", explode)
    env: dict[str, str] = {}
    apply_build_environment(env)
    assert REQUIRED_NEXTPNR_FLAG in env[NEXTPNR_OPTS_VAR]


def test_apply_build_environment_enforces_yosys_when_asked(monkeypatch):
    monkeypatch.setattr("hurra_cynthion.build_env.detect_yosys_version", lambda: (0, 53))
    with pytest.raises(SystemExit):
        apply_build_environment({}, enforce_yosys=True)

    monkeypatch.setattr("hurra_cynthion.build_env.detect_yosys_version", lambda: (0, 68))
    env: dict[str, str] = {}
    apply_build_environment(env, enforce_yosys=True)
    assert REQUIRED_NEXTPNR_FLAG in env[NEXTPNR_OPTS_VAR]


# `resolve_yosys_executable` exists because LUNA's generated build_top.sh reads
# `: ${YOSYS:=yosys}`, so the build runs $YOSYS and only falls back to PATH.
# A guard that probed PATH unconditionally would be checking a different binary
# than the one that synthesises -- which is exactly the situation inside the
# container, where YOSYS is pinned absolute into the oss-cad-suite bundle.


def test_resolve_yosys_prefers_the_YOSYS_override(monkeypatch, tmp_path):
    pinned = tmp_path / "pinned-yosys"
    pinned.write_text("#!/bin/sh\nexit 0\n")
    pinned.chmod(0o755)
    monkeypatch.setenv("YOSYS", str(pinned))
    monkeypatch.setattr(
        "shutil.which", lambda name: name if name == str(pinned) else "/usr/bin/yosys"
    )
    assert resolve_yosys_executable() == str(pinned)


def test_resolve_yosys_falls_back_to_path_when_unset(monkeypatch):
    monkeypatch.delenv("YOSYS", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yosys" if name == "yosys" else None)
    assert resolve_yosys_executable() == "/usr/bin/yosys"


def test_resolve_yosys_ignores_an_empty_override(monkeypatch):
    """An exported-but-empty YOSYS is the shell default, not a selection.

    ``: ${YOSYS:=yosys}`` substitutes when the variable is unset OR empty, so
    the build would use PATH here and so must this.
    """
    monkeypatch.setenv("YOSYS", "")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yosys" if name == "yosys" else None)
    assert resolve_yosys_executable() == "/usr/bin/yosys"


def test_resolve_yosys_returns_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.delenv("YOSYS", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert resolve_yosys_executable() is None
