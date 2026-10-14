"""Daily-window request counters and the rotation trigger (P5).

Semantics (single unit-test contract — see the user's example):

* The window is ``[08:00, 08:00+1d)`` Asia/Shanghai (== UTC 00:00, the
  upstream's daily reset). At 08:00 every counter restarts from zero; a
  burst that spans the reset is NOT carried over (08:00-09:00 counts only
  what happened after 08:00).
* A bucket that reaches ``rotation.threshold`` completed requests inside
  the current window triggers exactly one rotation (ONT reboot via the
  agent); after the rotation the counter restarts, because the new IP is a
  fresh quota bucket.
* A hard 429 with ``Retry-After > 3600s`` bypasses the threshold and
  rotates immediately (upstream-proven classification: that shape means
  the per-IP daily quota is exhausted).

State is in-memory plus a JSON snapshot for restart survival; the
snapshot lives outside the repo and carries no message content.
"""

from __future__ import annotations
