# syntax=docker/dockerfile:1
#
# Reproducible ECP5 toolchain for hurra-cynthion.
#
# The yosys version decides whether this design closes timing. Measured on
# DETERMINISTIC netlists -- same source, same nextpnr, twelve placer seeds per
# arm, only yosys differing:
#
#     yosys 0.48+47  -> 3 of 12 seeds pass   (the old CI pin)
#     yosys 0.68+136 -> 12 of 12 seeds pass
#
# Old yosys is not "never closes" -- it closes on a quarter of seeds, which is
# worse, because it means a single green build proves nothing. An earlier
# single-seed table here read 0.48 -> 56.57 MHz "no bitstream"; that was one
# draw from a netlist that changed every build, and seed 9 alone spans
# 59.69-68.71 MHz on one unchanged yosys.
#
# There is no official YosysHQ container image -- the suite ships as dated
# tarballs and YosysHQ uses Docker only to build it. Community images carry
# unpinned yosys versions, which is precisely the failure this image exists to
# prevent, so we pin the exact bundle that was validated end-to-end.
FROM python:3.12-slim-bookworm

ARG OSS_CAD_SUITE_DATE=2026-09-01
ARG OSS_CAD_SUITE_STAMP=20260901
# Consumed by the version check at the end of this file. ENV, not just ARG:
# the check runs in a heredoc subprocess, which inherits the environment but
# not build arguments, so an ARG alone would make --build-arg a silent no-op.
ARG MINIMUM_YOSYS_MAJOR=0
ARG MINIMUM_YOSYS_MINOR=60
ENV MINIMUM_YOSYS_MAJOR=${MINIMUM_YOSYS_MAJOR} \
    MINIMUM_YOSYS_MINOR=${MINIMUM_YOSYS_MINOR}

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/oss-cad-suite.tgz \
      "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_CAD_SUITE_DATE}/oss-cad-suite-linux-x64-${OSS_CAD_SUITE_STAMP}.tgz" \
 && tar -xzf /tmp/oss-cad-suite.tgz -C /opt \
 && rm /tmp/oss-cad-suite.tgz

# PATH is APPENDED, never prepended. The bundle ships its own python3 in the
# same bin/ as yosys; prepending would shadow the interpreter that carries
# amaranth. The binaries are additionally pinned absolutely below, because
# LUNA's generated build_top.sh honours these exact variables:
#     : ${YOSYS:=yosys} ; ${NEXTPNR_ECP5:=nextpnr-ecp5} ; ${ECPPACK:=ecppack}
ENV PATH="${PATH}:/opt/oss-cad-suite/bin" \
    YOSYS=/opt/oss-cad-suite/bin/yosys \
    NEXTPNR_ECP5=/opt/oss-cad-suite/bin/nextpnr-ecp5 \
    ECPPACK=/opt/oss-cad-suite/bin/ecppack

# A login shell (bash -lc, and some CI step shells) sources /etc/profile, which
# RESETS PATH and discards the ENV above -- `yosys` then reports "command not
# found" inside the very image that contains it. The absolute pins above keep
# the build scripts working regardless, but interactive use and any `yosys -V`
# step need this too.
RUN printf 'export PATH="$PATH:/opt/oss-cad-suite/bin"\n' \
      > /etc/profile.d/oss-cad-suite.sh \
 && chmod 0644 /etc/profile.d/oss-cad-suite.sh

# nextpnr's own --threads does not reach the HeAP placer's Eigen solver, which
# diverges at its first iteration with core availability. Pinned, placement is
# bit-identical even under oversubscription -- which is what makes fanning the
# sweep out across containers meaningful rather than noise.
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    EIGEN_DONT_PARALLELIZE=1

# Dependencies only, pinned to match pyproject.toml. Source is mounted at run
# time so the image stays cached across commits.
RUN pip install --no-cache-dir \
      amaranth==0.5.7 \
      cynthion==0.2.3 \
      luna-usb==0.2.2 \
      usb-protocol==0.9.1

# Fail the IMAGE BUILD if the toolchain is wrong, rather than discovering it in
# a ten-minute build that produces no bitstream.
RUN set -eu; \
    "$YOSYS" -V; "$NEXTPNR_ECP5" --version; "$ECPPACK" --version; \
    python3 -c "import amaranth, luna; print('amaranth', amaranth.__version__)"; \
    python3 - <<'PYEOF'
import os, re, subprocess, sys
floor = (
    int(os.environ["MINIMUM_YOSYS_MAJOR"]),
    int(os.environ["MINIMUM_YOSYS_MINOR"]),
)
out = subprocess.run([os.environ["YOSYS"], "-V"], capture_output=True, text=True).stdout
m = re.search(r"Yosys\s+(\d+)\.(\d+)", out)
if m is None:
    sys.exit(f"cannot parse yosys version from: {out!r}")
have = (int(m.group(1)), int(m.group(2)))
if have < floor:
    sys.exit(f"yosys {have[0]}.{have[1]} is below the {floor[0]}.{floor[1]} floor; this design will not close timing")
print(f"yosys {have[0]}.{have[1]} >= {floor[0]}.{floor[1]} OK")
PYEOF

COPY synth.sh pnr.sh sweep.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/synth.sh /usr/local/bin/pnr.sh /usr/local/bin/sweep.sh

WORKDIR /work
