# Reachy Chat

A voice conversation app for Reachy Mini with multiple backend options. The default backend is **OpenAI Realtime**, which provides low-latency bidirectional speech over WebSocket with smart "addressed-to-me" barge-in. Alternative backends include **Gemini Live**, **Hugging Face Realtime**, and a local turn-based pipeline via **OpenRouter Chat**.

## Setup

1. Copy `.env.example` to `.env` in the app root directory (next to this README).
2. Choose your backend and set the required API key(s):

| Backend | Required Keys | Optional |
|---------|---------------|----------|
| **OpenAI Realtime** (default) | `OPENAI_API_KEY` | `OPENAI_REALTIME_MODEL` |
| Gemini Live | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | — |
| OpenRouter Chat | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` (default: `minimax/minimax-m3`) |
| Hugging Face | — | `HF_TOKEN` |

3. Set `BACKEND_PROVIDER` in `.env` to one of: `openai_realtime`, `gemini`, `openai`, `huggingface`.
   - If not set, the app defaults to **`openai_realtime`**.
4. When launched from Reachy Controller, use the settings button on the `reachy_chat` app card to configure the active backend's API key and conversation voice.

## Features

- **OpenAI Realtime (default)**: Low-latency bidirectional voice conversation over WebSocket with native OpenAI voice (e.g., `cedar`). Includes smart "addressed-to-me" gate to suppress unaddressed barge-in.
- **Gemini Live**: Real-time conversation with Google's Gemini 3.1 Flash, supporting multiple voices.
- **Hugging Face Realtime**: Community-hosted realtime backend.
- **OpenRouter Chat**: Turn-based local speech pipeline: mic → `faster-whisper` → OpenRouter text-generation → native TTS → speaker.
  - On **macOS/Apple Silicon**, uses the native `say` command for TTS (pyttsx3's `save_to_file` is broken there). On other platforms, falls back to pyttsx3.
  - Camera vision context is attached to OpenRouter requests.
- **Personalities**: Unlocked personality selection with voice-optimized default prompt.

## Local Speech Requirements (OpenRouter / Turn-Based Path Only)

The turn-based local pipeline requires:

- `faster-whisper>=1.0.0` for local speech recognition
- `pyttsx3>=2.90` for local platform speech synthesis (non-macOS) or native `say` (macOS)

On the first spoken turn, `faster-whisper` downloads the default `base.en` model to the Hugging Face cache (normally `~/.cache/huggingface/`). Later starts reuse that cache. The module import itself does not initialize either speech engine or download a model.

Use environment variables to customize the faster-whisper runtime:

```bash
WHISPER_MODEL=base.en          # Model size (tiny, base, small, medium, large)
WHISPER_DEVICE=cpu             # Device (cpu, cuda)
WHISPER_COMPUTE_TYPE=int8      # Quantization (int8, int16, float32)
```

## Configuration

### Media Backend

- **`REACHY_MINI_MEDIA_BACKEND`** (default: `local`): Selects the SDK media backend. On Reachy Mini Lite, use `local`.

### Dotenv Loading

- **`REACHY_MINI_SKIP_DOTENV=1`**: Disable `.env` file loading (use environment variables only).
- The app loads `.env` from the app root directory (`apps/reachy_chat/.env`), independent of the current working directory.

### Custom Profiles and Personalities

Use the `profiles/_reachy_chat_locked_profile` folder to customize your own app from this template:

- Edit instructions in `_reachy_chat_locked_profile/instructions.txt`
- Define available tools in `_reachy_chat_locked_profile/tools.txt`
- Create custom tools by subclassing the `Tool` class in `_reachy_chat_locked_profile/*.py`

To finalize your custom app:
- Update this `README.md` file

## Archived Documentation

The original README from the Reachy Mini conversation app is available in `README_OLD.md`.
