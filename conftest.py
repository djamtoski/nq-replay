import pytest

import journal


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path):
    """Tests never touch the real data/journal.sqlite."""
    journal.configure(tmp_path / "journal.sqlite")
    yield
    journal.configure(None)
