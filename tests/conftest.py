"""Shared pytest configuration and fixtures for the OpenWhisper test suite.

Putting the project root on sys.path here removes the need for the
``sys.path.insert`` boilerplate that used to be repeated in most test
modules.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest


@pytest.fixture
def db(tmp_path):
    """A DatabaseManager backed by a throwaway sqlite file."""
    from services.database import DatabaseManager

    manager = DatabaseManager(db_path=str(tmp_path / "test.db"))
    yield manager
    manager.close()


@pytest.fixture
def repo(db):
    """A SqlMeetingRepository on top of the shared ``db`` fixture."""
    from meeting.persist.repository import SqlMeetingRepository

    return SqlMeetingRepository(db=db)
