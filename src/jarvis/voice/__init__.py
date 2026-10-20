"""Hands-free voice service.

Audio input (microphone, wake word, end of speech, push-to-talk hotkey), audio output (sentence
streaming into macOS `say`), on-screen confirmations and the state machine that ties them to the
same `Assistant` the terminal and Telegram use. Heavy dependencies (sounddevice, openwakeword,
silero-vad, pynput, mlx-whisper) live in the optional `voice` extra and are imported lazily, so
this package imports cleanly without them and `jarvis doctor` can report what is missing.
"""
