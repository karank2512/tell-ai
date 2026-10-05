"""Source adapters. Each module exposes ``COLLECTOR: CollectorSpec``; see ``base.py``.

This package deliberately imports nothing, so ``tell.collectors.base`` can be
imported by itself and ``discover_collectors()`` controls when adapters load.
"""
