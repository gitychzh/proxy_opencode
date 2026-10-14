"""Test package marker.

Makes ``from tests.test_x import ...`` work regardless of how pytest is
invoked: with this file present, pytest's default "prepend" import mode
inserts the repo root (the first directory without ``__init__.py``) into
``sys.path``, so the cross-test imports resolve also when running the
``pytest`` console script (CI) instead of ``python -m pytest``.
"""
