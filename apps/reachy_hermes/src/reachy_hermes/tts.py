"""Local KittenTTS model provisioning and synthesis for Reachy Hermes."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

MODEL_NAME = "kitten-nano-en-v0_8-int8"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
    "kitten-nano-en-v0_8-int8.tar.bz2"
)
MODEL_SHA256 = "6fa5be852612ce761094ba74ee6123b4fc4acfefa79bf64dc63acae4a83af2fd"
MODEL_FILES = ("model.int8.onnx", "voices.bin", "tokens.txt", "espeak-ng-data")
VOICE_NAMES = ("Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo")
DEFAULT_SPEAKER_ID = 0
DEFAULT_SPEED = 1.05


class NeuralTTS:
    """Lazily download and run the small int8 KittenTTS model on CPU."""

    def __init__(self, model_root: str | Path | None = None) -> None:
        configured_root = model_root or os.getenv("REACHY_HERMES_MODEL_DIR") or "~/.cache/reachy-hermes/models"
        self.model_root = Path(configured_root).expanduser()
        self.model_dir = self.model_root / MODEL_NAME
        self._engine: Any | None = None

    def ensure_loaded(self) -> None:
        """Provision the verified model and initialize sherpa-onnx once."""
        if self._engine is not None:
            return
        self._ensure_model_files()
        import sherpa_onnx

        threads = max(1, min(6, os.cpu_count() or 1))
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kitten=sherpa_onnx.OfflineTtsKittenModelConfig(
                    model=str(self.model_dir / "model.int8.onnx"),
                    voices=str(self.model_dir / "voices.bin"),
                    tokens=str(self.model_dir / "tokens.txt"),
                    data_dir=str(self.model_dir / "espeak-ng-data"),
                ),
                num_threads=threads,
                provider="cpu",
                debug=False,
            )
        )
        if not config.validate():
            raise RuntimeError(f"Invalid KittenTTS model configuration at {self.model_dir}")
        self._engine = sherpa_onnx.OfflineTts(config)
        logger.info("Loaded neural TTS model %s with %d threads", MODEL_NAME, threads)

    def synthesize(self, text: str, speaker_id: int | None = None) -> tuple[int, NDArray[np.int16]]:
        """Generate mono PCM suitable for the Reachy SDK speaker queue."""
        self.ensure_loaded()
        sid = self._speaker_id() if speaker_id is None else speaker_id
        if not 0 <= sid < len(VOICE_NAMES):
            raise ValueError(f"KittenTTS speaker ID must be between 0 and {len(VOICE_NAMES) - 1}")
        engine = self._engine
        if engine is None:
            raise RuntimeError("Neural TTS engine failed to initialize")
        generated = engine.generate(text, sid=sid, speed=DEFAULT_SPEED)
        samples = np.asarray(generated.samples, dtype=np.float32)
        pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        return int(generated.sample_rate), pcm

    def _speaker_id(self) -> int:
        raw = os.getenv("REACHY_HERMES_TTS_SPEAKER", str(DEFAULT_SPEAKER_ID)).strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError("REACHY_HERMES_TTS_SPEAKER must be an integer from 0 through 7") from exc

    def _ensure_model_files(self) -> None:
        if self._model_is_complete(self.model_dir):
            return
        self.model_root.mkdir(parents=True, exist_ok=True)
        if self.model_dir.exists():
            shutil.rmtree(self.model_dir)

        with tempfile.TemporaryDirectory(prefix=f".{MODEL_NAME}-", dir=self.model_root) as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / f"{MODEL_NAME}.tar.bz2"
            logger.info("Downloading neural TTS model %s", MODEL_NAME)
            digest = hashlib.sha256()
            with urllib.request.urlopen(MODEL_URL, timeout=60) as response, archive_path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            actual_digest = digest.hexdigest()
            if actual_digest != MODEL_SHA256:
                raise RuntimeError(
                    f"KittenTTS model checksum mismatch: expected {MODEL_SHA256}, got {actual_digest}"
                )

            extraction_root = temporary_root / "extracted"
            extraction_root.mkdir()
            with tarfile.open(archive_path, "r:bz2") as archive:
                archive.extractall(extraction_root, filter="data")
            extracted_model = extraction_root / MODEL_NAME
            if not self._model_is_complete(extracted_model):
                raise RuntimeError("KittenTTS archive is missing required model files")
            os.replace(extracted_model, self.model_dir)
            logger.info("Installed neural TTS model at %s", self.model_dir)

    @staticmethod
    def _model_is_complete(model_dir: Path) -> bool:
        return model_dir.is_dir() and all((model_dir / name).exists() for name in MODEL_FILES)


__all__ = [
    "DEFAULT_SPEAKER_ID",
    "MODEL_NAME",
    "MODEL_SHA256",
    "MODEL_URL",
    "NeuralTTS",
    "VOICE_NAMES",
]
