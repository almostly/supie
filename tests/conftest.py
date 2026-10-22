"""Shared fixtures: a throwaway Supabase project, and the Docker images to test against."""

import os

import pytest

# Pinned to what the Supabase CLI ships. The upstream workflow overrides them with
# the CLI's current pins (scripts/supabase_images.py) to catch changes early.
IMAGES = {
    "edge": os.environ.get(
        "SUPIE_EDGE_IMAGE", "public.ecr.aws/supabase/edge-runtime:v1.77.8"
    ),
    "postgres": os.environ.get(
        "SUPIE_POSTGRES_IMAGE", "public.ecr.aws/supabase/postgres:17.11.0.006"
    ),
    "postgrest": os.environ.get(
        "SUPIE_POSTGREST_IMAGE", "public.ecr.aws/supabase/postgrest:v16.5"
    ),
}

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


@pytest.fixture(scope="session")
def images():
    """The Docker images for the edge and postgres tests."""
    return IMAGES
