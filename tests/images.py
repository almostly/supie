"""The Docker images the edge and postgres tests run against.

Pinned to what the Supabase CLI ships. The upstream workflow overrides them with
the CLI's current pins (scripts/supabase_images.py) to catch changes early, and
CI pulls them with scripts/pull_images.py.
"""

import os

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
