"""Shared fixtures: a throwaway Supabase project."""

import pytest

CONFIG = """# a comment supie must keep
project_id = "demo"

[functions.existing]
verify_jwt = false
"""


@pytest.fixture
def project(tmp_path):
    """A folder with supabase/config.toml, as `supabase init` leaves it."""
    (tmp_path / "supabase").mkdir()
    (tmp_path / "supabase" / "config.toml").write_text(CONFIG)
    return tmp_path
