"""Hands-free Claude Code: headphone swipes record speech, Claude's replies are spoken.

Gestures (Sony WH-1000XM4 over AVRCP):
    swipe forward  start recording / interrupt Claude / allow
    swipe back     stop speaking / repeat last reply / deny
    double tap     passed through to the system (music play/pause)

A recording ends by itself after a pause in speech (endpointing): while the mic
is open the headphones are in the headset (HFP) profile, in which they send no
button events at all.
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from evdev import InputDevice, UInput, ecodes, list_devices

from faster_whisper.vad import get_speech_timestamps

from voice_chat.audio import MIC_RATE, Recorder, Speaker, load_whisper

REPO = Path(__file__).resolve().parents[2]
BUTTONS = "WH-1000XM4 (AVRCP)"
FORWARD = ecodes.KEY_NEXTSONG
BACK = ecodes.KEY_PREVIOUSSONG
MAX_RECORDING = 120  # seconds
END_SILENCE = 2.0  # seconds of silence after speech that end a recording
NO_SPEECH = 8.0  # seconds without any speech that cancel a recording
SYSTEM_PROMPT_APPEND = (
    "This is a hands-free voice session. Everything you write is read aloud by "
    "text-to-speech through the user's headphones, and the user's messages are "
    "speech-to-text transcripts. Follow the voice:voice-output skill for every reply."
)


async def read_buttons(gestures: asyncio.Queue[int]) -> None:
    """Grab the headphone buttons so swipes only reach voice-chat, and re-emit
    every other event (double tap = play/pause) through a uinput clone of the device."""
    while True:
        devices = [InputDevice(path) for path in list_devices()]
        device = next((d for d in devices if d.name == BUTTONS), None)
        for d in devices:
            if d is not device:
                d.close()
        if device is None:
            await asyncio.sleep(2)  # headphones not connected (yet)
            continue
        try:
            device.grab()
        except OSError as error:
            device.close()
            print(
                f"buttons: can't grab {BUTTONS} ({error.strerror}), is voice-chat already running?"
            )
            await asyncio.sleep(5)
            continue
        passthrough = UInput.from_device(device, name=f"{BUTTONS} passthrough")
        print(f"buttons: {device.path}")
        try:
            async for event in device.async_read_loop():
                if event.type == ecodes.EV_KEY and event.code in (FORWARD, BACK):
                    if event.value == 1:
                        gestures.put_nowait(event.code)
                else:
                    passthrough.write_event(event)
        except OSError:
            print("buttons: disconnected")
        finally:
            passthrough.close()
            device.close()  # also releases the grab
        await asyncio.sleep(1)


class VoiceChat:
    def __init__(
        self, speaker: Speaker, whisper: Any, gestures: asyncio.Queue[int]
    ) -> None:
        self.speaker = speaker
        self.whisper = whisper
        self.gestures = gestures
        self.recorder = Recorder()
        self.recording: asyncio.Task[None] | None = None  # endpointing while recording
        self.turn: asyncio.Task[None] | None = None
        self.approval: asyncio.Future[bool] | None = None  # waiting for allow/deny
        self.answer: asyncio.Future[str] | None = None  # waiting for a spoken answer
        self.last_reply = ""
        self.client: ClaudeSDKClient

    async def run(self, options: ClaudeAgentOptions) -> None:
        async with ClaudeSDKClient(options=options) as self.client:
            # Claude only loads a skill when it judges it relevant, so load it
            # explicitly; the reply to this doubles as the "ready" message.
            self.turn = asyncio.create_task(self.run_turn("/voice:voice-output"))
            while True:
                await self.on_gesture(await self.gestures.get())

    async def on_gesture(self, gesture: int) -> None:
        if self.recording is not None:
            self.recording.cancel()
            self.recording = None
            audio = await self.recorder.stop()
            if gesture == BACK:
                print("recording cancelled")
                self.speaker.tone(330)
                return
            self.speaker.tone(660)
            print(f"transcribing {len(audio) / MIC_RATE:.1f} s...")
            text = await asyncio.to_thread(
                lambda: "".join(
                    s.text
                    for s in self.whisper.transcribe(
                        audio, language="en", vad_filter=True
                    )[0]
                ).strip()
            )
            print(f"\n> {text}")
            if not text:
                self.speaker.say("I didn't catch that.")
            elif self.answer is not None:
                self.answer.set_result(text)
            else:
                if self.turn is not None:
                    await self.turn  # interrupted when the recording started
                self.turn = asyncio.create_task(self.run_turn(text))
            return

        if self.approval is not None:
            self.speaker.stop()
            self.approval.set_result(gesture == FORWARD)
            self.speaker.tone(660 if gesture == FORWARD else 330)
            return

        if gesture == FORWARD:
            self.speaker.stop()
            if self.turn is not None and not self.turn.done() and self.answer is None:
                await self.client.interrupt()
            await self.recorder.start()
            self.speaker.tone(880)
            self.recording = asyncio.create_task(self.detect_endpoint())
            print("recording...")
        elif self.speaker.speaking:
            self.speaker.stop()
        elif self.turn is None or self.turn.done():
            self.speaker.say(self.last_reply or "There is nothing to repeat yet.")

    async def detect_endpoint(self) -> None:
        """End the recording once the speaker pauses (sends a FORWARD gesture), or
        cancel it when nothing is said (BACK), using the Silero VAD."""
        while True:
            await asyncio.sleep(0.3)
            audio = self.recorder.audio()
            speech = await asyncio.to_thread(
                get_speech_timestamps,
                audio,
                min_silence_duration_ms=300,
                speech_pad_ms=0,
            )
            if len(audio) > MAX_RECORDING * MIC_RATE or (
                speech and len(audio) - speech[-1]["end"] > END_SILENCE * MIC_RATE
            ):
                self.gestures.put_nowait(FORWARD)
                return
            if not speech and len(audio) > NO_SPEECH * MIC_RATE:
                self.gestures.put_nowait(BACK)
                return

    async def run_turn(self, prompt: str) -> None:
        reply = []
        try:
            await self.client.query(prompt)
            async for message in self.client.receive_response():
                if (
                    isinstance(message, AssistantMessage)
                    and message.parent_tool_use_id is None
                ):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(block.text)
                            self.speaker.say(block.text)
                            reply.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            print(f"[{block.name}]")
                            self.speaker.tone(1200, seconds=0.04, volume=0.1)
                elif isinstance(message, ResultMessage) and message.is_error:
                    print(f"error: {message.subtype} {message.errors}")
                    self.speaker.say("Something went wrong, see the terminal.")
        except Exception as error:  # noqa: BLE001 -- report it by voice, keep the session
            print(f"error: {error!r}")
            self.speaker.say("Something went wrong, see the terminal.")
        self.last_reply = "\n".join(reply)
        self.speaker.tone(520, seconds=0.08)
        self.speaker.tone(780, seconds=0.12)

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        loop = asyncio.get_running_loop()
        if tool_name == "AskUserQuestion":
            answers = {}
            for question in tool_input["questions"]:
                options = " ".join(
                    f"Option {i}: {option['label']}."
                    for i, option in enumerate(question["options"], start=1)
                )
                self.speaker.say(
                    f"{question['question']} {options} Swipe forward and say your answer."
                )
                self.answer = loop.create_future()
                answers[question["question"]] = await self.answer
                self.answer = None
            return PermissionResultAllow(
                updated_input={**tool_input, "answers": answers}
            )

        match tool_name:
            case "Bash":
                action = f"run a command: {tool_input.get('description') or tool_input['command']}"
            case "Edit" | "Write" | "NotebookEdit":
                file = tool_input.get("file_path") or tool_input.get(
                    "notebook_path", ""
                )
                action = f"{tool_name.lower()} the file {Path(file).name}"
            case "WebFetch":
                action = f"fetch a page from {urlparse(tool_input['url']).netloc}"
            case "ExitPlanMode":
                action = "stop planning and start working"
            case _:
                action = f"use the tool {tool_name}"
        print(f"? {tool_name}: {tool_input}")
        self.speaker.say(
            f"Claude wants to {action[:200]}. Swipe forward to allow, back to deny."
        )
        self.approval = loop.create_future()
        allowed = await self.approval
        self.approval = None
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message="The user denied this with the headphone buttons."
        )


async def amain(args: argparse.Namespace) -> None:
    print("loading Whisper and Piper...")
    whisper = await asyncio.to_thread(load_whisper, args.whisper_model)
    speaker = Speaker(args.voice)  # needs the running loop for its worker task
    gestures: asyncio.Queue[int] = asyncio.Queue()
    buttons = asyncio.create_task(read_buttons(gestures))
    chat = VoiceChat(speaker, whisper, gestures)
    options = ClaudeAgentOptions(
        cwd=Path.cwd(),
        cli_path=shutil.which("claude")
        if args.no_sandbox
        else REPO / "scripts/sandboxed-claude",
        plugins=[{"type": "local", "path": str(REPO / "plugin")}],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": SYSTEM_PROMPT_APPEND,
        },
        setting_sources=["user", "project", "local"],
        can_use_tool=chat.can_use_tool,
        # Pipe Claude Code's stderr so it doesn't write into our terminal; shown with --debug.
        stderr=lambda line: print(line, file=sys.stderr) if args.debug else None,
    )
    try:
        await chat.run(options)
    finally:
        buttons.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hands-free Claude Code over headphones."
    )
    parser.add_argument(
        "--no-sandbox", action="store_true", help="run claude outside bwrap"
    )
    parser.add_argument("--whisper-model", default="distil-large-v3")
    parser.add_argument("--voice", default="en_US-lessac-high", help="Piper voice name")
    parser.add_argument(
        "--debug", action="store_true", help="show Claude Code's stderr"
    )
    try:
        asyncio.run(amain(parser.parse_args()))
    except KeyboardInterrupt:
        pass
