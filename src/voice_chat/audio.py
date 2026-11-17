"""Audio through PipeWire: recording, Whisper speech-to-text and Piper text-to-speech."""

import asyncio
import ctypes
import re
import time
from pathlib import Path

import numpy as np
from piper import PiperVoice
from piper.download_voices import download_voice

MIC_RATE = 16000  # Hz, what Whisper expects
PIPER_DIR = Path.home() / ".local/share/voice-chat/piper"


def load_whisper(model: str):
    """Whisper on the GPU. ctranslate2 needs CUDA 12 cuBLAS and cuDNN 9, which come
    from the nvidia-*-cu12 wheels; preload them since they are not on the
    library path (the system CUDA is a different major version)."""
    import nvidia.cublas
    import nvidia.cudnn

    for package, pattern in (
        (nvidia.cublas, "libcublas*.so.*"),
        (nvidia.cudnn, "libcudnn*.so.*"),
    ):
        for lib in sorted((Path(package.__path__[0]) / "lib").glob(pattern)):
            ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)

    from faster_whisper import WhisperModel

    # On the T550, int8_float16 is ~3x faster than float16 at the same accuracy.
    whisper = WhisperModel(model, device="cuda", compute_type="int8_float16")
    list(
        whisper.transcribe(np.zeros(MIC_RATE, np.float32))[0]
    )  # warm-up: first call is slow
    return whisper


class Recorder:
    """Records from the headphone mic with pw-record. Recording from WirePlumber's
    bluez_input source switches the headphones to the headset (HFP) profile, and
    back to A2DP once the recording stops."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.data = bytearray()

    async def start(self) -> None:
        pactl = await asyncio.create_subprocess_exec(
            "pactl", "list", "short", "sources", stdout=asyncio.subprocess.PIPE
        )
        sources = (await pactl.communicate())[0].decode()
        bluez = re.search(r"^\d+\s+(bluez_input\.\S+)", sources, re.MULTILINE)
        target = (
            ["--target", bluez.group(1)] if bluez else []
        )  # else the default source
        self.proc = await asyncio.create_subprocess_exec(
            "pw-record",
            "--raw",
            *target,
            "--rate", str(MIC_RATE),
            "--channels", "1",
            "--format", "s16",
            "-",
            stdout=asyncio.subprocess.PIPE,
        )  # fmt: skip
        self.data = bytearray()
        stdout = self.proc.stdout
        assert stdout is not None

        async def read() -> None:
            while chunk := await stdout.read(4096):
                self.data.extend(chunk)

        self.reader = asyncio.create_task(read())

    def audio(self) -> np.ndarray:
        """The audio recorded so far as float32 in [-1, 1]."""
        n = len(self.data) // 2 * 2
        return np.frombuffer(bytes(self.data[:n]), np.int16).astype(np.float32) / 32768

    async def stop(self) -> np.ndarray:
        assert self.proc is not None and self.reader is not None
        self.proc.terminate()
        await self.reader
        await self.proc.wait()
        self.proc = self.reader = None
        return self.audio()


def speakable(text: str) -> str:
    """Strip markdown and things that sound bad when read aloud."""
    text = re.sub(
        r"```.*?(```|$)", " I'm skipping a code block. ", text, flags=re.DOTALL
    )
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^\s*(#+|>|[-*+]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[-:| ]+\|?\s*$", "", text, flags=re.MULTILINE)  # table rules
    text = re.sub(r"\*\*|__|(?<!\w)[*_]|[*_](?!\w)|\|", " ", text)
    return text


class Speaker:
    """Speaks queued text with Piper and plays tones, through one pw-play process
    per burst of speech so that stop() can cut it off immediately."""

    def __init__(self, voice: str) -> None:
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        download_voice(voice, PIPER_DIR)  # no-op when already downloaded
        self.voice = PiperVoice.load(PIPER_DIR / f"{voice}.onnx")
        self.rate = self.voice.config.sample_rate
        self.queue: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.player: asyncio.subprocess.Process | None = None
        self.play_until = 0.0  # monotonic time when the written audio has played
        self.generation = 0  # bumped by stop() so sentences in flight are dropped
        self.worker = asyncio.create_task(self.run())

    def say(self, text: str) -> None:
        self.queue.put_nowait(speakable(text))

    def tone(self, freq: float, seconds: float = 0.12, volume: float = 0.3) -> None:
        t = np.arange(int(seconds * self.rate)) / self.rate
        fade = np.minimum(1, np.minimum(t, seconds - t) / 0.01)  # avoid clicks
        wave = volume * fade * np.sin(2 * np.pi * freq * t)
        self.queue.put_nowait((wave * 32767).astype(np.int16).tobytes())

    @property
    def speaking(self) -> bool:
        return not self.queue.empty() or time.monotonic() < self.play_until

    def stop(self) -> None:
        self.generation += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        if self.player is not None and self.player.returncode is None:
            self.player.kill()
        self.player = None
        self.play_until = 0.0

    async def run(self) -> None:
        while True:
            item = await self.queue.get()
            generation = self.generation
            if isinstance(item, bytes):
                await self.play(item)
                continue
            for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", item):
                if not sentence.strip():
                    continue
                pcm = await asyncio.to_thread(
                    lambda s=sentence: b"".join(
                        c.audio_int16_bytes for c in self.voice.synthesize(s)
                    )
                )
                if generation != self.generation:
                    break
                await self.play(pcm)
            if (
                self.queue.empty()
                and self.player is not None
                and self.player.stdin is not None
            ):
                self.player.stdin.close()  # pw-play exits once the buffered audio has played
                self.player = None

    async def play(self, pcm: bytes) -> None:
        if self.player is None:
            self.player = await asyncio.create_subprocess_exec(
                "pw-play",
                "--raw",
                "--rate", str(self.rate),
                "--channels", "1",
                "--format", "s16",
                "-",
                stdin=asyncio.subprocess.PIPE,
            )  # fmt: skip
        stdin = self.player.stdin
        assert stdin is not None
        self.play_until = (
            max(self.play_until, time.monotonic()) + len(pcm) / 2 / self.rate
        )
        try:
            stdin.write(pcm)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # killed by stop()
