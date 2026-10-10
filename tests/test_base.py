# tests/test_base.py
# Copyright (C) 2024 - 2028 the PythonAccounting authors and contributors
# <see AUTHORS file>
#
# This module is part of PythonAccounting and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""
Tests for the Base model class.

"""
from datetime import datetime

from python_accounting.models import Currency


def test_timestamps_are_the_insert_time(session, entity):
    """created_at and updated_at record the insert, not the import, time."""
    before = datetime.now()
    currency = Currency(name="Test Currency", code="XTS", entity_id=entity.id)
    session.add(currency)
    session.commit()
    record = session.get(Currency, currency.id)
    assert record.created_at >= before
    assert record.updated_at >= before
