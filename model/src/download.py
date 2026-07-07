import os

import torch as th
from demucs import pretrained

from config import settings

# Pre-download the model the API actually uses so it's baked into the Docker
# image (no slow first-request download on Render). Override with $demucs_model.
th.hub.set_dir(settings.models)

model_id = os.environ.get("demucs_model", "htdemucs")
pretrained.get_model(model_id)
print("downloaded model:", model_id)
