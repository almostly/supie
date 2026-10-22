"""Print the Docker images the Supabase CLI currently ships, as KEY=value lines.

The upstream workflow appends them to $GITHUB_ENV, so the edge and postgres tests
run against what Supabase users get today rather than supie's pins.

    python scripts/supabase_images.py [branch]
"""

import re
import sys
import urllib.request

DOCKERFILE = "https://raw.githubusercontent.com/supabase/cli/{branch}/apps/cli-go/pkg/config/templates/Dockerfile"
WANTED = {
    "SUPIE_EDGE_IMAGE": ("supabase/edge-runtime", "edgeruntime"),
    "SUPIE_POSTGRES_IMAGE": ("supabase/postgres", "pg"),
    "SUPIE_POSTGREST_IMAGE": ("postgrest/postgrest", "postgrest"),
}


def main(branch="develop"):
    """Read the CLI's Dockerfile and print one public ECR image per variable."""
    with urllib.request.urlopen(
        DOCKERFILE.format(branch=branch), timeout=30
    ) as response:
        text = response.read().decode()
    pins = dict(
        (alias, (image, tag))
        for image, tag, alias in re.findall(r"^FROM (\S+):(\S+) AS (\S+)$", text, re.M)
    )
    for variable, (image, alias) in WANTED.items():
        found, tag = pins.get(alias, (None, None))
        if found != image:
            sys.exit(f"the CLI's Dockerfile has no {image} AS {alias}")
        name = image.split("/")[1]
        print(f"{variable}=public.ecr.aws/supabase/{name}:{tag}")


if __name__ == "__main__":
    main(*sys.argv[1:])
