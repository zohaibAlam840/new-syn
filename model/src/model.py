import base64
from pathlib import Path
from tempfile import mkdtemp

import torch as th
from demucs.apply import apply_model
from demucs.audio import save_audio
from demucs.pretrained import get_model
from demucs.separate import load_track

from config import settings


class Demucs(object):
    """Demucs 4.0.1 wrapper that produces a karaoke instrumental
    (every stem except vocals, summed)."""

    def __init__(self, output_dir, model_id="htdemucs", load=False):
        self.output_dir = output_dir
        self.model_id = model_id
        self.model = None
        self.device = "cpu"
        if load:
            self.load()

    def load(self):
        if self.model is None:
            print("Loading model:", self.model_id)
            th.hub.set_dir(str(settings.models))
            self.model = get_model(self.model_id)
            self.device = "cuda" if th.cuda.is_available() else "cpu"
            self.model.to(self.device)
            self.model.eval()
        return True

    def separate_instrumental(self, in_path, out_path):
        """Separate `in_path`, write the vocals-removed instrumental mp3 to
        `out_path`, and return `out_path`."""
        self.load()
        wav = load_track(Path(in_path), self.model.audio_channels, self.model.samplerate)

        ref = wav.mean(0)
        wav_n = (wav - ref.mean()) / (ref.std() + 1e-8)

        with th.no_grad():
            sources = apply_model(
                self.model, wav_n[None], device=self.device,
                shifts=0, split=True, overlap=0.25, progress=True,
            )[0]
        sources = sources * ref.std() + ref.mean()

        instrumental = None
        for tensor, name in zip(sources, self.model.sources):
            if name == "vocals":
                continue
            instrumental = tensor.clone() if instrumental is None else instrumental + tensor

        if instrumental is None:
            raise RuntimeError("No non-vocal stems produced")

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        save_audio(instrumental.cpu(), str(out_path), samplerate=self.model.samplerate)
        return out_path

    def separate(self, fpath):
        """Base64 variant used by the legacy /predict endpoint."""
        out = Path(mkdtemp()) / "no_vocals.mp3"
        self.separate_instrumental(fpath, out)
        with open(out, "rb") as f:
            return {"no_vocals": base64.b64encode(f.read()).decode("utf-8")}
