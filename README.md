# voice-chat

Hands-free Claude Code with a Sony WH-1000XM4: swipe on the headphones, speak,
and hear Claude's reply. Speech-to-text (faster-whisper, GPU) and text-to-speech
(Piper) run locally; Claude Code runs inside the bwrap sandbox from
`~/Code/sandbox`.

## Usage

From the project you want Claude to work in (it becomes the sandbox's writable
root, as with `sandbox-shell`):

```sh
uv run --project ~/Code/voice-chat voice-chat
```

Options: `--no-sandbox`, `--whisper-model` (default `distil-large-v3`; `small.en`
is ~4x faster on the T550 but less accurate), `--voice` (Piper voice, default
`en_US-lessac-high`), `--debug` (show Claude Code's stderr).

Plain `claude` is unaffected: the voice skill and the button handling only exist
while this program runs.

## Controls

| | swipe forward | swipe back |
|---|---|---|
| idle | start recording (high beep) | repeat last reply |
| Claude working or speaking | interrupt and start recording | stop speaking |
| permission question | allow | deny |

A recording ends by itself 2 s after you stop talking (mid beep), or is
cancelled after 8 s without speech (low beep). Swipes can't end it: while the
mic is open the headphones are in the headset (HFP) profile, and in that profile
they send no button events. A double tap (play/pause) is passed through to the
system.

A soft tick means Claude started a tool; a rising two-tone chime means it has
finished its turn.

## How it works

- `src/voice_chat/main.py`: grabs the `WH-1000XM4 (AVRCP)` input device (re-emits
  everything but the swipes through uinput), runs the Claude Agent SDK client,
  and answers permission prompts and `AskUserQuestion` by voice/gesture.
- `src/voice_chat/audio.py`: `pw-record` from WirePlumber's `bluez_input` source
  (which switches the headphones to HFP while recording), Silero VAD
  endpointing, Whisper, and Piper played through `pw-play`.
- `scripts/sandboxed-claude`: the SDK's `cli_path`; runs
  `~/Code/sandbox/sandbox-shell claude`.
- `plugin/`: a local plugin with the `voice-output` skill (short spoken replies,
  no code or paths read aloud). It's loaded only by this program, and invoked as
  the first message of each session.

Needs: user in group `input` (for `/dev/input` and `/dev/uinput`), PipeWire with
WirePlumber, and an NVIDIA GPU. The CUDA 12 libraries come from the
`nvidia-*-cu12` wheels.

## Author

This project was made by Simon Thor.
