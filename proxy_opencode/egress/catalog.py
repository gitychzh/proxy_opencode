"""Zen free-model catalog and public alias policy (P3).

``GET {zen}/v1/models`` is polled on a slow loop; entries whose cost is
zero (``allowAnonymous`` free models, e.g. ``big-pickle``) become the
public catalog. Public naming:

* the existing aliases stay stable — ``cusoc/DeepSeek-V4.1-Flash`` and
  ``ds41f_cus`` map to ``big-pickle``;
* every other free model is exposed under its bare upstream id.

The registry keeps masking upstream ids out of responses (the existing
``sse_mask``/``registry`` behaviour) so vendor internals never leak.
"""

from __future__ import annotations
