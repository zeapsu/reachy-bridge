---
title: Reachy Chat
emoji: 🤖
colorFrom: purple
colorTo: gray
sdk: static
pinned: false
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# Reachy Chat

Forked from the Reachy Mini conversation app.

## OpenRouter setup

The default voice backend uses a turn-based local speech pipeline:

1. The Reachy Mini SDK captures 16 kHz microphone samples.
2. `faster-whisper` transcribes each utterance locally after trailing silence is detected.
3. The OpenAI-compatible client sends the bounded conversation history to OpenRouter's chat-completions API.
4. `pyttsx3` synthesizes the response locally.
5. The SDK streams the generated audio to Reachy Mini's speaker while the existing speech wobble is active.

OpenRouter handles text generation only; the app does not use an OpenAI Realtime websocket.

1. Copy `.env.example` to `.env`.
2. Set `OPENROUTER_API_KEY` to your OpenRouter API key.
3. Optionally set `OPENROUTER_MODEL` to another OpenRouter model ID. The default is `deepseek/deepseek-v4-flash`.

Required environment variables:

```bash
OPENROUTER_API_KEY=your_key_here
OPENROUTER_MODEL=deepseek/deepseek-v4-flash
```

## Local speech requirements

The full voice loop requires a working Reachy Mini microphone and speaker. The Python environment also includes:

- `faster-whisper>=1.0.0` for local speech recognition
- `pyttsx3>=2.90` for platform speech synthesis

On the first spoken turn, `faster-whisper` downloads the default `base.en` model to the Hugging Face cache (normally `~/.cache/huggingface/`). Later starts reuse that cache. The module import itself does not initialize either speech engine or download a model.

The optional `WHISPER_MODEL`, `WHISPER_DEVICE`, and `WHISPER_COMPUTE_TYPE` environment variables select a different faster-whisper runtime. Defaults are `base.en`, `cpu`, and `int8`.

Use the `src/reachy_chat/profiles/_reachy_chat_locked_profile` folder to customize your own app from this template:
- Edit instructions `_reachy_chat_locked_profile/instructions.txt`
- Edit available tools in `_reachy_chat_locked_profile/tools.txt`
- You can create your own tools in `_reachy_chat_locked_profile` by subclassing the `Tool` class.

Do not forget to customize:
- this `README.md` file
- the `index.html` file (Hugging Face Spaces landing page)
- the `src/reachy_chat/static/index.html` (the web app parameters page)

The original README from the conversation app is available in `README_OLD.md`.
