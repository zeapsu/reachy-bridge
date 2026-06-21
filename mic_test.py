"""Standalone mic probe: is the robot delivering audio samples to the SDK?

Run with the app STOPPED (the mic is a shared resource) and the daemon running:
    cd ~/reachy/apps/reachy_chat && uv run python ~/reachy/mic_test.py
Then talk for the 6-second window.
"""
import time
import numpy as np
from reachy_mini import ReachyMini

with ReachyMini() as r:
    r.media.start_recording()
    time.sleep(1.0)  # let the capture pipeline spin up
    print(">>> TALK NOW for ~6 seconds <<<", flush=True)

    nonnull = 0
    nulls = 0
    max_rms = 0.0
    last_shape = None
    t0 = time.time()
    while time.time() - t0 < 6.0:
        s = r.media.get_audio_sample()
        if s is not None and getattr(s, "size", 0) > 0:
            nonnull += 1
            last_shape = s.shape
            a = s.astype("float32")
            rms = float(np.sqrt(np.mean(a * a)))
            if rms > max_rms:
                max_rms = rms
        else:
            nulls += 1
        time.sleep(0.01)

    r.media.stop_recording()
    print(f"\nRESULT: non-null samples={nonnull}  null/empty={nulls}  "
          f"last_shape={last_shape}  max_RMS={max_rms:.5f}", flush=True)
    print("handler speech threshold is 0.015 — compare against max_RMS above.", flush=True)
