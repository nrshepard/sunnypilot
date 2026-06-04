#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

# mici-nav: cap UI render at 30fps (from 60). The base mici UI does a full GPU
# frame every tick; in-car cProfile showed ~59% CPU @60fps dropping to ~30% @30fps,
# with no perceptible loss parked/onroad. Env knob is read by application.py:_DEFAULT_FPS.
export FPS=30

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="17.2"
fi

export STAGING_ROOT="/data/safe_staging"
