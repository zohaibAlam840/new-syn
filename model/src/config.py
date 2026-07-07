import os
from pathlib import Path

root_dir = Path(__file__).parent.parent.resolve()


class Settings:
    """Plain settings (no pydantic) so it works on any Python/pydantic version."""

    def __init__(self):
        self.models = Path(os.environ.get("demucs_models", root_dir / "models"))
        self.data = Path(os.environ.get("demucs_data", root_dir / "data"))
        self.static = Path(os.environ.get("demucs_static", root_dir))
        self.docker = os.environ.get("demucs_docker", "false").lower() == "true"
        self.models.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)


settings = Settings()
