"""Pull the test images, falling back to Docker Hub when public ECR refuses.

Public ECR limits anonymous pulls per IP, and GitHub's shared runners hit that
("toomanyrequests: Data limit exceeded"). The same images are on Docker Hub, so a
refused pull is retried there and tagged with the ECR name the tests use.

    python scripts/pull_images.py
"""

import runpy
import subprocess
import sys
import time
from pathlib import Path

IMAGES = runpy.run_path(str(Path(__file__).parent.parent / "tests" / "images.py"))[
    "IMAGES"
]
DOCKER_HUB = {
    "public.ecr.aws/supabase/edge-runtime": "supabase/edge-runtime",
    "public.ecr.aws/supabase/postgres": "supabase/postgres",
    "public.ecr.aws/supabase/postgrest": "postgrest/postgrest",
}


def pull(image: str) -> bool:
    """Pull an image, retrying once; return whether it worked."""
    for attempt in range(2):
        if subprocess.run(["docker", "pull", "-q", image]).returncode == 0:
            return True
        time.sleep(5 * (attempt + 1))
    return False


def main() -> None:
    """Pull every test image, from ECR or else from Docker Hub."""
    for image in IMAGES.values():
        if pull(image):
            continue
        name, _, tag = image.rpartition(":")
        mirror = DOCKER_HUB.get(name)
        if mirror is None or not pull(f"{mirror}:{tag}"):
            sys.exit(f"couldn't pull {image}")
        subprocess.run(["docker", "tag", f"{mirror}:{tag}", image], check=True)
        print(f"{image}: pulled from Docker Hub ({mirror}:{tag})")


if __name__ == "__main__":
    main()
