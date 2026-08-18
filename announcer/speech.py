"""Speech backends, playback serialization, and debounce state."""

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple


DEBOUNCE_MAX_AGE_SECONDS = 24 * 60 * 60


class PlaybackLockTimeout(TimeoutError):
    pass


def load_debounce_state(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def is_debounced(
    state: Dict[str, Any], pane_id: str, status: str, now: float, seconds: int
) -> bool:
    previous = state.get(pane_id)
    if not isinstance(previous, dict) or previous.get("status") != status:
        return False
    timestamp = previous.get("ts")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False
    return now - float(timestamp) <= seconds


def _prune_debounce_state(state: Dict[str, Any], now: float) -> None:
    for key, value in list(state.items()):
        timestamp = value.get("ts") if isinstance(value, dict) else None
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or now - float(timestamp) > DEBOUNCE_MAX_AGE_SECONDS
        ):
            del state[key]


def save_debounce_state(
    state_dir: Path, state: Dict[str, Any], pane_id: str, status: str, now: float
) -> None:
    _prune_debounce_state(state, now)
    state[pane_id] = {"status": status, "ts": now}
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(state_dir),
            prefix="last.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, str(state_dir / "last.json"))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def check_and_record_debounce(
    state_dir: Path, pane_id: str, status: str, seconds: int
) -> bool:
    """Atomically check and record the announcement, so two hooks racing on
    the same event can't both pass the debounce window."""
    import fcntl

    with (state_dir / "debounce.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            state = load_debounce_state(state_dir / "last.json")
            if is_debounced(state, pane_id, status, now, seconds):
                return True
            save_debounce_state(state_dir, state, pane_id, status, now)
            return False
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def rollback_debounce(state_dir: Path, pane_id: str, status: str) -> None:
    import fcntl

    with (state_dir / "debounce.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_debounce_state(state_dir / "last.json")
            value = state.get(pane_id)
            if isinstance(value, dict) and value.get("status") == status:
                del state[pane_id]
                temporary_name = ""
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=str(state_dir),
                        prefix="last.",
                        suffix=".tmp",
                        delete=False,
                    ) as output:
                        temporary_name = output.name
                        json.dump(
                            state, output, separators=(",", ":"), sort_keys=True
                        )
                        output.write("\n")
                    os.replace(temporary_name, str(state_dir / "last.json"))
                    temporary_name = ""
                finally:
                    if temporary_name:
                        try:
                            os.unlink(temporary_name)
                        except FileNotFoundError:
                            pass
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def playback_lock(
    state_dir: Path,
    reasons: Optional[List[str]] = None,
    timeout: float = 90.0,
    poll_interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    import fcntl

    started = clock()
    with (state_dir / "speak.lock").open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = timeout - (clock() - started)
                if remaining <= 0:
                    if reasons is not None:
                        reasons.append("playback-lock: timeout")
                    raise PlaybackLockTimeout("playback lock timed out")
                sleeper(min(poll_interval, remaining))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_custom_speech(command_value: Any, text: str) -> str:
    if not isinstance(command_value, list) or not all(
        isinstance(argument, str) for argument in command_value
    ):
        raise ValueError("speak_command must be an argv array of strings")
    if not command_value:
        raise ValueError("speak_command must not be empty")
    used_placeholder = any("{text}" in argument for argument in command_value)
    command = [argument.replace("{text}", text) for argument in command_value]
    subprocess.run(
        command,
        check=True,
        input=None if used_placeholder else text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    return "command"


def _audio_player() -> Optional[Tuple[str, bool]]:
    mp3_players = (
        ("afplay", "mpv", "ffplay")
        if platform.system() == "Darwin"
        else ("mpv", "ffplay", "afplay")
    )
    for name in mp3_players:
        if shutil.which(name):
            return name, False
    for name in ("paplay", "pw-play", "aplay"):
        if shutil.which(name):
            return name, True
    return None


def synthesize_elevenlabs(
    config: Dict[str, Any],
    text: str,
    state_dir: Path,
    output_format: str = "mp3_44100_128",
) -> Path:
    voice_id = urllib.parse.quote(str(config["elevenlabs_voice_id"]), safe="")
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/{}"
        "?output_format={}"
    ).format(voice_id, output_format)
    body = json.dumps(
        {"text": text, "model_id": str(config["elevenlabs_model"])}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": str(config["elevenlabs_api_key"]),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        audio = response.read()
    if not audio:
        raise ValueError("ElevenLabs returned no audio")
    suffix = ".pcm" if output_format == "pcm_22050" else ".mp3"
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=str(state_dir), suffix=suffix, delete=False
    ) as handle:
        handle.write(audio)
        return Path(handle.name)


def play_audio_file(
    path: Path, player: Optional[str] = None, raw_pcm: bool = False
) -> str:
    if player is None:
        selected = _audio_player()
        if selected is None or selected[1]:
            raise FileNotFoundError("no MP3 player found")
        player = selected[0]
    if raw_pcm and player == "paplay":
        command = [
            "paplay",
            "--raw",
            "--rate=22050",
            "--channels=1",
            "--format=s16le",
            str(path),
        ]
    elif raw_pcm and player == "pw-play":
        command = [
            "pw-play",
            "--rate=22050",
            "--channels=1",
            "--format=s16",
            str(path),
        ]
    elif raw_pcm and player == "aplay":
        command = [
            "aplay",
            "--file-type=raw",
            "--format=S16_LE",
            "--rate=22050",
            "--channels=1",
            str(path),
        ]
    elif player == "mpv":
        command = ["mpv", "--no-video", str(path)]
    elif player == "ffplay":
        command = ["ffplay", "-nodisp", "-autoexit", str(path)]
    elif player == "afplay":
        command = ["afplay", str(path)]
    else:
        raise FileNotFoundError("unsupported audio player")
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    return "elevenlabs"


def _speech_failure(name: str, error: BaseException) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return "{}: timeout".format(name)
    detail = str(error).strip()
    return "{}: {}".format(
        name, detail[:120] if detail else error.__class__.__name__
    )


def run_local_speech(
    config: Dict[str, Any], text: str, reasons: Optional[List[str]] = None
) -> str:
    system = platform.system()
    if system == "Darwin":
        command = ["say"]
        voice = config.get("voice")
        if isinstance(voice, str) and voice:
            command.extend(["-v", voice])
        subprocess.run(
            command,
            check=True,
            input=text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        return "say"
    if system == "Linux":
        attempts = (
            ("spd-say", ["spd-say", "-e", "-w"], 5),
            ("espeak-ng", ["espeak-ng"], 60),
            ("espeak", ["espeak"], 60),
        )
        last_error = None
        for name, command, timeout in attempts:
            try:
                subprocess.run(
                    command,
                    check=True,
                    input=text,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
                return name
            except (OSError, subprocess.SubprocessError) as error:
                last_error = error
                if reasons is not None:
                    reasons.append(_speech_failure(name, error))
        if last_error is not None:
            raise last_error
    raise OSError("local text-to-speech is unsupported on {}".format(system))


def _elevenlabs_reason(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return "elevenlabs: HTTP {}".format(error.code)
    detail = str(error).strip()
    return "elevenlabs: {}".format(
        detail[:120] if detail else error.__class__.__name__
    )


def speak(
    config: Dict[str, Any],
    text: str,
    state_dir: Path,
    reasons: Optional[List[str]] = None,
) -> str:
    if config.get("speak_command") is not None:
        with playback_lock(state_dir, reasons=reasons):
            return run_custom_speech(config["speak_command"], text)

    if config.get("elevenlabs_api_key"):
        selected = _audio_player()
        if selected is None:
            if reasons is not None:
                reasons.append("elevenlabs: no-player")
                reasons.append("play: mpv/ffplay missing")
        else:
            audio_path: Optional[Path] = None
            player, raw_pcm = selected
            try:
                output_format = "pcm_22050" if raw_pcm else "mp3_44100_128"
                audio_path = synthesize_elevenlabs(
                    config, text, state_dir, output_format=output_format
                )
                with playback_lock(state_dir, reasons=reasons):
                    return play_audio_file(audio_path, player, raw_pcm)
            except PlaybackLockTimeout:
                raise
            except (
                OSError,
                ValueError,
                TypeError,
                subprocess.SubprocessError,
            ) as error:
                if reasons is not None:
                    reasons.append(_elevenlabs_reason(error))
            finally:
                if audio_path is not None:
                    try:
                        audio_path.unlink()
                    except FileNotFoundError:
                        pass

    with playback_lock(state_dir, reasons=reasons):
        return run_local_speech(config, text, reasons=reasons)


def capabilities() -> Dict[str, Optional[str]]:
    return {
        name: shutil.which(name)
        for name in (
            "codex",
            "claude",
            "say",
            "spd-say",
            "espeak-ng",
            "espeak",
            "mpv",
            "ffplay",
            "afplay",
            "paplay",
            "pw-play",
            "aplay",
        )
    }


# Compatibility alias for announce._capabilities and older package consumers.
_capabilities = capabilities


__all__ = [
    "DEBOUNCE_MAX_AGE_SECONDS",
    "PlaybackLockTimeout",
    "_capabilities",
    "_prune_debounce_state",
    "capabilities",
    "check_and_record_debounce",
    "is_debounced",
    "load_debounce_state",
    "play_audio_file",
    "playback_lock",
    "rollback_debounce",
    "run_custom_speech",
    "run_local_speech",
    "save_debounce_state",
    "speak",
    "synthesize_elevenlabs",
]
