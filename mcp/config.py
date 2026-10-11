import os
from pathlib import Path

REGISTRY_ADDRESS = "UQCYxSFNCJHmBxVpgfqAesgjLQDsLch3WJG3MJYyhnBDS7gg"

# Repo checkout the development tools scaffold into and deploy from.
PROJECT_ROOT = os.getenv("CATALLAXY_PROJECT_ROOT") or str(Path(__file__).resolve().parents[1])
