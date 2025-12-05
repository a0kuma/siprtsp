import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import pjsua2 as pj


# Small .env loader (simple, no external deps)
def load_dotenv(dotenv_path: str = ".env") -> None:
    p = Path(dotenv_path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


# try to load `.env` in project root
load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().lower()
    return val in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val.strip())
    except (TypeError, ValueError):
        return default


# =========================
# SIP 帳號設定
# =========================
# These values can be provided via environment or `./.env`.
SIP_DOMAIN = os.getenv("SIP_DOMAIN")
SIP_USER = os.getenv("SIP_USER")          # 這支 py 是 5004
SIP_PASSWD = os.getenv("SIP_PASSWD")
SIP_FORCE_CONTACT_URI = os.getenv("SIP_FORCE_CONTACT_URI")
SIP_FORCE_CONTACT_HOST = os.getenv("SIP_FORCE_CONTACT_HOST")
SIP_FORCE_CONTACT_PORT = os.getenv("SIP_FORCE_CONTACT_PORT")

# 本機 SIP 來源 port
# 5060 會跟別的東西打架就改 0，讓 OS 自動選
LOCAL_SIP_PORT = _env_int("LOCAL_SIP_PORT", 0)

# =========================
# RTSP 設定
# =========================
RTSP_HOST = os.getenv("RTSP_HOST", "127.0.0.1")
RTSP_PORT = os.getenv("RTSP_PORT", "8554")
RTSP_SPK_PATH = os.getenv("RTSP_SPK_PATH", "/u5004/spk")
RTSP_MIC_PATH = os.getenv("RTSP_MIC_PATH", "/u5004/mic")

if RTSP_PORT:
    RTSP_BASE_URL = f"rtsp://{RTSP_HOST}:{RTSP_PORT.strip()}"
else:
    RTSP_BASE_URL = f"rtsp://{RTSP_HOST}"

RTSP_SPK_URL = f"{RTSP_BASE_URL}{RTSP_SPK_PATH}"
RTSP_MIC_URL = f"{RTSP_BASE_URL}{RTSP_MIC_PATH}"
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
MONITOR_SIP_TRAFFIC = _env_flag("MONITOR_SIP_TRAFFIC", False)
MONITOR_SIP_INTERFACE = os.getenv("MONITOR_SIP_INTERFACE", "lo")
MONITOR_SIP_COMMAND = os.getenv("MONITOR_SIP_COMMAND", "tcpdump")
SIP_DIAG_ENABLED = _env_flag("SIP_DIAG", False)
SIP_ACK_TIMEOUT = _env_int("SIP_ACK_TIMEOUT", 35)


def _parse_port_from_text(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r":(\d+)(?!.*:\d+)", text)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


def _extract_port_from_addr(addr: Optional[object]) -> Optional[int]:
    if addr is None:
        return None

    for attr in ("port", "Port"):
        port_val = getattr(addr, attr, None)
        if isinstance(port_val, int) and port_val > 0:
            return port_val

    get_port = getattr(addr, "getPort", None)
    if callable(get_port):
        try:
            port_val = get_port()
            if isinstance(port_val, int) and port_val > 0:
                return port_val
        except Exception:
            pass

    to_string = getattr(addr, "toString", None)
    if callable(to_string):
        for with_port in (True, False):
            try:
                text = to_string(with_port)
            except TypeError:
                try:
                    text = to_string()
                except Exception:
                    text = ""
            except Exception:
                text = ""
            if text:
                parsed = _parse_port_from_text(text)
                if parsed:
                    return parsed

    return None


def _detect_bound_sip_port(ep: pj.Endpoint, transport_id: int, fallback: int) -> int:
    port = fallback or 0
    try:
        info = ep.transportGetInfo(transport_id)
    except pj.Error:
        return port

    addr_candidates = [
        getattr(info, "localAddress", None),
        getattr(info, "localAddr", None),
        getattr(info, "boundAddress", None),
        getattr(info, "addrName", None),
        getattr(info, "localName", None),
    ]

    for addr in addr_candidates:
        resolved = _extract_port_from_addr(addr)
        if resolved:
            return resolved

    info_text = getattr(info, "info", None)
    if isinstance(info_text, str):
        parsed = _parse_port_from_text(info_text)
        if parsed:
            return parsed

    return port


def _parse_ip_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(r"((?:\d{1,3}\.){3}\d{1,3})", text)
    if match:
        return match.group(1)
    return None


def _extract_ip_from_addr(addr: Optional[object]) -> Optional[str]:
    if addr is None:
        return None

    for attr in ("ipAddress", "ip", "host", "addr"):
        val = getattr(addr, attr, None)
        if isinstance(val, str):
            ip = _parse_ip_from_text(val) or (val if val.count(".") == 3 else None)
            if ip:
                return ip

    to_string = getattr(addr, "toString", None)
    if callable(to_string):
        for with_port in (True, False):
            try:
                text = to_string(with_port)
            except TypeError:
                try:
                    text = to_string()
                except Exception:
                    continue
            except Exception:
                continue
            ip = _parse_ip_from_text(text)
            if ip:
                return ip

    return None


def _detect_bound_sip_host(ep: pj.Endpoint, transport_id: int) -> Optional[str]:
    try:
        info = ep.transportGetInfo(transport_id)
    except pj.Error:
        return None

    addr_candidates = [
        getattr(info, "localAddress", None),
        getattr(info, "localAddr", None),
        getattr(info, "boundAddress", None),
        getattr(info, "addrName", None),
        getattr(info, "localName", None),
    ]

    for addr in addr_candidates:
        ip = _extract_ip_from_addr(addr)
        if ip:
            return ip

    info_text = getattr(info, "info", None)
    if isinstance(info_text, str):
        parsed = _parse_ip_from_text(info_text)
        if parsed:
            return parsed

    return None


# =========================
# 全域 calls map
# =========================
# key: call.id  (int)
g_calls = {}


class CallDiagInfo:
    def __init__(self, call_id: int) -> None:
        self.call_id = call_id
        self.invite_ts: Optional[float] = None
        self.ok_ts: Optional[float] = None
        self.ack_ts: Optional[float] = None
        self.remote_uri: Optional[str] = None
        self.remote_contact: Optional[str] = None
        self.missing_ack_reported = False

    def mark_invite(self, remote_uri: str, remote_contact: Optional[str]) -> None:
        self.invite_ts = time.time()
        self.remote_uri = remote_uri
        self.remote_contact = remote_contact
        _diag_print(self.call_id, "INVITE", f"From {remote_uri} contact={remote_contact}")

    def mark_ok(self) -> None:
        self.ok_ts = time.time()
        _diag_print(self.call_id, "200-OK", "Sent final response, waiting for ACK")

    def mark_ack(self) -> None:
        self.ack_ts = time.time()
        _diag_print(self.call_id, "ACK", "Received ACK, call confirmed")


class SipDiagMonitor:
    def __init__(self, enabled: bool, timeout_seconds: int) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self._entries: dict[int, CallDiagInfo] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sip-diag-monitor", daemon=True)
        self._thread.start()
        print(f"[diag] SIP diagnostics enabled (ACK timeout {self.timeout_seconds}s)")

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                for info in list(self._entries.values()):
                    if info.ok_ts and not info.ack_ts:
                        elapsed = now - info.ok_ts
                        if elapsed >= self.timeout_seconds and not info.missing_ack_reported:
                            info.missing_ack_reported = True
                            self._report_missing_ack(info, elapsed)
            self._stop.wait(1)

    def _report_missing_ack(self, info: CallDiagInfo, elapsed: float) -> None:
        invite_age = (time.time() - info.invite_ts) if info.invite_ts else None
        age_str = f"{invite_age:.1f}s" if invite_age is not None else "n/a"
        msg = (
            f"No ACK after {elapsed:.1f}s (call age {age_str}, remote={info.remote_uri},"
            f" contact={info.remote_contact})"
        )
        _diag_print(info.call_id, "WARN", msg)

    def attach(self, call_id: int, remote_uri: str, remote_contact: Optional[str]) -> None:
        if not self.enabled:
            return
        with self._lock:
            entry = self._entries.get(call_id)
            if entry is None:
                entry = CallDiagInfo(call_id)
                self._entries[call_id] = entry
            entry.mark_invite(remote_uri, remote_contact)

    def mark_ok(self, call_id: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            entry = self._entries.get(call_id)
            if entry:
                entry.mark_ok()

    def mark_ack(self, call_id: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            entry = self._entries.get(call_id)
            if entry:
                entry.mark_ack()

    def detach(self, call_id: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries.pop(call_id, None)


def _diag_print(call_id: int, label: str, text: str) -> None:
    if not SIP_DIAG_ENABLED:
        return
    timestamp = time.strftime("%H:%M:%S")
    print(f"[diag {timestamp}] call {call_id} {label}: {text}")


diag_monitor = SipDiagMonitor(SIP_DIAG_ENABLED, SIP_ACK_TIMEOUT)


def _media_port_valid(media: Optional[object]) -> bool:
    if media is None:
        return False
    try:
        port_id = media.getPortId()  # type: ignore[attr-defined]
    except (pj.Error, AttributeError):
        return False
    return isinstance(port_id, int) and port_id >= 0


def _safe_stop_transmit(src: Optional[object], dst: Optional[object], label: str) -> None:
    if not _media_port_valid(src) or not _media_port_valid(dst):
        return
    try:
        src.stopTransmit(dst)  # type: ignore[attr-defined]
    except pj.Error as err:
        print(f"[{label}] stopTransmit failed: {err}")


def _terminate_process(proc: Optional[subprocess.Popen], name: str) -> None:
    if not proc:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            else:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    print(f"[{name}] ffmpeg kill timeout")
    if proc.stdout:
        try:
            proc.stdout.close()
        except Exception:
            pass
    if proc.stderr:
        try:
            proc.stderr.close()
        except Exception:
            pass


def _drain_process_stderr(proc: subprocess.Popen) -> str:
    if proc.stderr is None:
        return ""
    try:
        data = proc.stderr.read()
    except Exception:
        return ""

    if not data:
        return ""

    if isinstance(data, bytes):
        try:
            return data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    return str(data).strip()


class PortTrafficMonitor:
    def __init__(self, enabled: bool, interface: str, command: str) -> None:
        self.enabled = enabled
        self.interface = interface
        self.command = command
        self.proc: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None

    def start(self, port: int) -> None:
        if not self.enabled or self.proc is not None:
            return

        cmd = [
            self.command,
            "-nn",
            "-l",
            "-i",
            self.interface,
            "udp",
            "port",
            str(port),
        ]

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print(f"[mon] Command not found: {' '.join(cmd)}")
            return
        except PermissionError:
            print("[mon] Permission denied starting traffic monitor (tcpdump needs sudo)")
            return

        def _reader() -> None:
            assert self.proc is not None
            if self.proc.stdout is None:
                return
            for line in self.proc.stdout:
                print(f"[mon] {line.rstrip()}" )

        self.thread = threading.Thread(target=_reader, name="port-monitor", daemon=True)
        self.thread.start()
        print(f"[mon] Monitoring UDP traffic on port {port} via {' '.join(cmd)}")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
        except OSError:
            pass
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.proc = None
        self.thread = None


class SpeakerAudioPort(pj.AudioMediaPort):
    def __init__(
        self,
        call_id: int,
        ffmpeg_cmd: list[str],
        sample_rate: int,
        channels: int,
        frame_time_usec: int,
        bits_per_sample: int = 16,
    ) -> None:
        super().__init__()
        self.call_id = call_id
        self.ffmpeg_cmd = ffmpeg_cmd
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_time_usec = frame_time_usec
        self.bits_per_sample = bits_per_sample
        self.proc: Optional[subprocess.Popen] = None
        self._stop = False
        self._last_error: Optional[str] = None
        self._bytes_written = 0
        self._logged_first_frame = False
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop = False
        self._last_error = None

        fmt = pj.MediaFormatAudio()
        fmt.init(
            pj.PJMEDIA_TYPE_AUDIO,
            self.sample_rate,
            self.channels,
            self.frame_time_usec,
            self.bits_per_sample,
            0,
            0,
        )
        self.createPort(f"spk-port-{self.call_id}", fmt)

        try:
            self.proc = subprocess.Popen(
                self.ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as err:
            raise RuntimeError("ffmpeg binary not found for speaker pipeline") from err

        if self.proc.stdin is None:
            raise RuntimeError("speaker pipeline did not expose stdin pipe")

        proc_ref = self.proc
        if proc_ref.stderr is not None:
            def _stderr_reader() -> None:
                for line in proc_ref.stderr:  # pragma: no cover - debug support
                    text = line.rstrip()
                    if text:
                        print(f"[call {self.call_id}] [spk-ffmpeg] {text}")

            self._stderr_thread = threading.Thread(
                target=_stderr_reader,
                name=f"spk-ffmpeg-{self.call_id}",
                daemon=True,
            )
            self._stderr_thread.start()

    def stop(self) -> None:
        self._stop = True
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        if self.proc:
            _terminate_process(self.proc, f"call {self.call_id} spk")
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1)
                self._stderr_thread = None
            self.proc = None

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def _capture_process_error(self) -> None:
        if not self.proc:
            return
        exit_code = self.proc.returncode
        stderr_msg = _drain_process_stderr(self.proc)
        if stderr_msg:
            self._last_error = f"ffmpeg spk exited with {exit_code}: {stderr_msg}"
        else:
            self._last_error = f"ffmpeg spk exited with {exit_code}"

    def putFrame(self, frame: pj.MediaFrame) -> None:  # type: ignore[override]
        if self._stop or not self.proc or self.proc.stdin is None:
            return
        if frame.size <= 0:
            return

        if self.proc.poll() is not None:
            self._capture_process_error()
            self._stop = True
            return

        try:
            raw = frame.buf[: frame.size]
        except TypeError:
            raw = bytes(frame.buf)[: frame.size]

        if not isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)

        try:
            self.proc.stdin.write(raw)
            if not self._logged_first_frame:
                print(
                    f"[call {self.call_id}] Speaker port streaming {len(raw)} bytes/frame "
                    f"({self.sample_rate} Hz, {self.channels} ch, {self.bits_per_sample}-bit)"
                )
                self._logged_first_frame = True
            self._bytes_written += len(raw)
        except BrokenPipeError:
            self._stop = True
            self._capture_process_error()
        except Exception as err:
            self._last_error = f"speaker stdin write failed: {err}"
            self._stop = True


class MicAudioPort(pj.AudioMediaPort):
    def __init__(
        self,
        call_id: int,
        ffmpeg_cmd: list[str],
        sample_rate: int,
        channels: int,
        frame_time_usec: int,
        bits_per_sample: int = 16,
    ) -> None:
        super().__init__()
        self.call_id = call_id
        self.ffmpeg_cmd = ffmpeg_cmd
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_time_usec = frame_time_usec
        self.bits_per_sample = bits_per_sample
        self.proc: Optional[subprocess.Popen] = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stop = False
        self._last_error: Optional[str] = None

        bytes_per_sample = max(1, bits_per_sample // 8)
        samples_per_frame = max(1, int(self.sample_rate * self.frame_time_usec / 1_000_000))
        self._channel_stride = self.channels * bytes_per_sample
        self._frame_bytes = samples_per_frame * self._channel_stride

    def start(self) -> None:
        self._stop = False
        self._buffer.clear()
        self._last_error = None

        fmt = pj.MediaFormatAudio()
        fmt.init(
            pj.PJMEDIA_TYPE_AUDIO,
            self.sample_rate,
            self.channels,
            self.frame_time_usec,
            self.bits_per_sample,
            0,
            0,
        )
        self.createPort(f"mic-port-{self.call_id}", fmt)

        try:
            self.proc = subprocess.Popen(
                self.ffmpeg_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as err:
            raise RuntimeError("ffmpeg binary not found for mic pipeline") from err

        if self.proc.stdout is None:
            raise RuntimeError("mic pipeline did not expose stdout pipe")

    def stop(self) -> None:
        self._stop = True
        if self.proc and self.proc.stdout:
            try:
                self.proc.stdout.close()
            except Exception:
                pass
        if self.proc:
            _terminate_process(self.proc, f"call {self.call_id} mic")
            self.proc = None

    def get_last_error(self) -> Optional[str]:
        return self._last_error

    def _capture_process_error(self) -> None:
        if not self.proc:
            return
        exit_code = self.proc.returncode
        stderr_msg = _drain_process_stderr(self.proc)
        if stderr_msg:
            self._last_error = f"ffmpeg mic exited with {exit_code}: {stderr_msg}"
        else:
            self._last_error = f"ffmpeg mic exited with {exit_code}"

    def onFrameRequested(self, frame: pj.MediaFrame) -> None:  # type: ignore[override]
        with self._lock:
            needed = frame.size if frame.size > 0 else self._frame_bytes
            if needed <= 0:
                needed = self._frame_bytes or self._channel_stride

            data = bytearray()

            while len(data) < needed and not self._stop:
                if self._buffer:
                    take = min(needed - len(data), len(self._buffer))
                    data += self._buffer[:take]
                    del self._buffer[:take]
                    continue

                if not self.proc or not self.proc.stdout:
                    break

                chunk = self.proc.stdout.read(needed - len(data))
                if chunk:
                    data += chunk
                    continue

                if self.proc.poll() is not None:
                    self._capture_process_error()
                    self._stop = True
                    break

                break

            if len(data) < needed:
                data.extend(b"\x00" * (needed - len(data)))

            if len(data) > needed:
                self._buffer.extend(data[needed:])
                data = data[:needed]

            frame.buf.assign_from_bytes(bytes(data))
            frame.size = len(data)
            frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO


class RtspAudioBridge:
    def __init__(self, call_id: int, audio_media: pj.AudioMedia):
        self.call_id = call_id
        self.audio_media = audio_media
        self.spk_port: Optional[SpeakerAudioPort] = None
        self.mic_ready = False
        self.last_mic_error: Optional[str] = None
        self.mic_port: Optional[MicAudioPort] = None

    def start(self) -> bool:
        self.mic_ready = False
        self.last_mic_error = None
        try:
            self._start_spk_pipeline()
        except Exception:
            self.stop()
            raise

        try:
            self._start_mic_pipeline()
            self.mic_ready = True
        except Exception as exc:
            self.last_mic_error = str(exc)
            print(f"[call {self.call_id}] Mic pipeline disabled: {exc}")
            self._cleanup_failed_mic()

        return self.mic_ready

    def stop(self) -> None:
        if self.audio_media and self.spk_port:
            _safe_stop_transmit(self.audio_media, self.spk_port, f"call {self.call_id} spk disconnect")
        if self.mic_port and self.audio_media:
            _safe_stop_transmit(self.mic_port, self.audio_media, f"call {self.call_id} mic disconnect")

        if self.mic_port:
            self.mic_port.stop()
            if not self.last_mic_error:
                self.last_mic_error = self.mic_port.get_last_error()
            self.mic_port = None

        if self.spk_port:
            self.spk_port.stop()
            self.spk_port = None

    def _start_spk_pipeline(self) -> None:
        sample_rate = int(os.getenv("FFMPEG_SPK_AR", "48000"))
        channels = int(os.getenv("FFMPEG_SPK_AC", "2"))
        bits_per_sample = int(os.getenv("FFMPEG_SPK_BITS", "16"))
        frame_time_usec = int(os.getenv("RTSP_SPK_FRAME_TIME", "20000"))

        ffmpeg_cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            os.getenv("FFMPEG_LOGLEVEL", "warning"),
            "-fflags",
            "nobuffer",
            "-re",
            "-f",
            os.getenv("FFMPEG_SPK_FMT", "s16le"),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-i",
            "pipe:0",
            "-use_wallclock_as_timestamps",
            "1",
            "-f",
            "lavfi",
            "-i",
            "color=c=pink:size=640x360:rate=15",
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            os.getenv("FFMPEG_V_CODEC", "libx264"),
            "-preset",
            os.getenv("FFMPEG_V_PRESET", "veryfast"),
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            os.getenv("FFMPEG_A_CODEC", "aac"),
            "-ar",
            os.getenv("FFMPEG_SPK_OUT_AR", os.getenv("FFMPEG_SPK_AR", "48000")),
            "-ac",
            os.getenv("FFMPEG_SPK_OUT_AC", os.getenv("FFMPEG_SPK_AC", "2")),
            "-f",
            "rtsp",
            "-rtsp_transport",
            os.getenv("FFMPEG_SPK_TRANSPORT", "tcp"),
            RTSP_SPK_URL,
        ]

        spk_port = SpeakerAudioPort(
            self.call_id,
            ffmpeg_cmd,
            sample_rate,
            channels,
            frame_time_usec,
            bits_per_sample,
        )

        spk_port.start()

        print(
            f"[call {self.call_id}] Speaker pipeline: {sample_rate} Hz, {channels} ch, {bits_per_sample}-bit -> {RTSP_SPK_URL}"
        )

        try:
            self.audio_media.startTransmit(spk_port)
        except pj.Error as err:
            spk_port.stop()
            raise RuntimeError(f"pjsua2 speaker port transmit failed: {err}")

        self.spk_port = spk_port

    def _start_mic_pipeline(self) -> None:
        sample_rate = int(os.getenv("FFMPEG_MIC_AR", "16000"))
        channels = int(os.getenv("FFMPEG_MIC_AC", "1"))
        bits_per_sample = int(os.getenv("FFMPEG_MIC_BITS", "16"))
        frame_time_usec = int(os.getenv("RTSP_MIC_FRAME_TIME", "20000"))

        ffmpeg_cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            os.getenv("FFMPEG_LOGLEVEL", "warning"),
            "-y",
            "-rtsp_transport",
            os.getenv("FFMPEG_MIC_TRANSPORT", "tcp"),
            "-i",
            RTSP_MIC_URL,
            "-vn",
            "-acodec",
            os.getenv("FFMPEG_MIC_CODEC", "pcm_s16le"),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-f",
            os.getenv("FFMPEG_MIC_FMT", "s16le"),
            "pipe:1",
        ]

        mic_port = MicAudioPort(
            self.call_id,
            ffmpeg_cmd,
            sample_rate,
            channels,
            frame_time_usec,
            bits_per_sample,
        )

        mic_port.start()

        try:
            mic_port.startTransmit(self.audio_media)
        except pj.Error as err:
            mic_port.stop()
            raise RuntimeError(f"pjsua2 mic port transmit failed: {err}")

        self.mic_port = mic_port

    def _cleanup_failed_mic(self) -> None:
        if self.mic_port and self.audio_media and _media_port_valid(self.mic_port):
            try:
                self.mic_port.stopTransmit(self.audio_media)
            except pj.Error:
                pass
        if self.mic_port:
            self.mic_port.stop()
            if not self.last_mic_error:
                self.last_mic_error = self.mic_port.get_last_error()
        self.mic_port = None


class MyCall(pj.Call):
    def __init__(self, account, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(account, call_id)
        self.acc = account
        self.call_media: Optional[pj.AudioMedia] = None
        self.bridge: Optional[RtspAudioBridge] = None

    def _maybe_start_bridge(self, context: str) -> None:
        if self.bridge is not None:
            return

        ci = self.getInfo()

        try:
            media = self.getMedia(0)
        except pj.Error:
            print(f"[call {ci.id}] getMedia(0) failed during {context}")
            return

        audio_media: Optional[pj.AudioMedia] = None
        if media is not None:
            try:
                audio_media = pj.AudioMedia.typecastFromMedia(media)
            except (pj.Error, TypeError):
                audio_media = None

        if audio_media is None:
            print(f"[call {ci.id}] Media[0] could not be treated as AudioMedia during {context}")
            return

        try:
            bridge = RtspAudioBridge(ci.id, audio_media)
            mic_ready = bridge.start()
        except Exception as err:
            print(f"[call {ci.id}] Failed to start RTSP bridge ({context}): {err}")
            return

        self.bridge = bridge
        self.call_media = audio_media
        if mic_ready:
            print(f"[call {ci.id}] RTSP bridge active (spk -> {RTSP_SPK_URL}, mic <- {RTSP_MIC_URL})")
        else:
            error_hint = bridge.last_mic_error or "mic pipeline inactive"
            print(f"[call {ci.id}] Speaker streaming to {RTSP_SPK_URL}; mic input unavailable ({error_hint})")

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"[call {ci.id}] State: {ci.stateText} ({ci.lastReason})")

        if ci.state == pj.PJSIP_INV_STATE_CONNECTING:
            diag_monitor.mark_ok(ci.id)

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            print(f"[call {ci.id}] CONFIRMED, bridge to RTSP pipelines")
            diag_monitor.mark_ack(ci.id)
            self._maybe_start_bridge("state confirmed")

        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"[call {ci.id}] DISCONNECTED, cleanup")

            if self.bridge:
                self.bridge.stop()
                self.bridge = None

            self.call_media = None

            if ci.id in g_calls:
                del g_calls[ci.id]
                print(f"[call {ci.id}] Removed from g_calls")
            diag_monitor.detach(ci.id)

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        media_state = getattr(ci, "mediaState", None)
        if media_state is None:
            media_info = getattr(ci, "media", None)
            if media_info:
                states = []
                for idx, info in enumerate(media_info):
                    states.append(f"{idx}:{getattr(info, 'state', 'unknown')}")
                state_repr = ", ".join(states) or "unknown"
            else:
                state_repr = "unknown"
        else:
            state_repr = str(media_state)
        print(f"[call {ci.id}] Media state changed: {state_repr}")
        self._maybe_start_bridge("media state change")


class MyAccount(pj.Account):
    def __init__(self):
        super().__init__()

    def onRegState(self, prm):
        ai = self.getInfo()
        reason = getattr(ai, "regStatusText", None)
        if reason is None:
            reason = getattr(ai, "regReason", "")
        print(f"[acc] Reg state: {ai.regIsActive} ({ai.regStatus} {reason})")

    def onIncomingCall(self, prm):
        call = MyCall(self, prm.callId)
        ci = call.getInfo()
        print(f"[call {ci.id}] Incoming from: {ci.remoteUri}")

        g_calls[ci.id] = call
        remote_contact = getattr(ci, "remoteContact", None)
        diag_monitor.attach(ci.id, ci.remoteUri, remote_contact)

        op = pj.CallOpParam()
        op.statusCode = 200
        print(f"[call {ci.id}] Auto-answer 200 OK")
        call.answer(op)


def main():
    ep = pj.Endpoint()
    ep.libCreate()

    monitors: list[PortTrafficMonitor] = []
    monitored_ports: set[int] = set()

    def start_monitor_for_port(port: int) -> None:
        if not MONITOR_SIP_TRAFFIC or port <= 0 or port in monitored_ports:
            return
        mon = PortTrafficMonitor(
            enabled=True,
            interface=MONITOR_SIP_INTERFACE,
            command=MONITOR_SIP_COMMAND,
        )
        mon.start(port)
        if mon.proc is not None:
            monitors.append(mon)
            monitored_ports.add(port)

    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 4
    ep_cfg.logConfig.consoleLevel = 4

    ep.libInit(ep_cfg)
    diag_monitor.start()

    sip_cfg = pj.TransportConfig()
    sip_cfg.port = LOCAL_SIP_PORT
    transport_id = ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, sip_cfg)
    sip_port = _detect_bound_sip_port(ep, transport_id, sip_cfg.port)

    ep.audDevManager().setNullDev()

    ep.libStart()
    listen_port = _detect_bound_sip_port(ep, transport_id, sip_port or sip_cfg.port)
    listen_host = _detect_bound_sip_host(ep, transport_id)
    print(f"[ep] PJSUA2 started, listening on UDP port {listen_port}")
    if LOCAL_SIP_PORT and listen_port != LOCAL_SIP_PORT:
        print(
            f"[ep] Warning: requested SIP port {LOCAL_SIP_PORT} but OS bound {listen_port}; check if the port is busy"
        )

    start_monitor_for_port(listen_port)
    if LOCAL_SIP_PORT and LOCAL_SIP_PORT != listen_port:
        start_monitor_for_port(LOCAL_SIP_PORT)

    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{SIP_USER}@{SIP_DOMAIN}"
    acc_cfg.regConfig.registrarUri = f"sip:{SIP_DOMAIN}"
    contact_override: Optional[str] = None
    if SIP_FORCE_CONTACT_URI:
        contact_override = SIP_FORCE_CONTACT_URI.strip()
    else:
        port_override: Optional[int] = None
        if SIP_FORCE_CONTACT_PORT:
            try:
                port_override = int(SIP_FORCE_CONTACT_PORT)
            except ValueError:
                print(f"[acc] Warning: invalid SIP_FORCE_CONTACT_PORT='{SIP_FORCE_CONTACT_PORT}'")
        host_for_contact = SIP_FORCE_CONTACT_HOST or listen_host
        port_for_contact = port_override or listen_port or sip_cfg.port
        if (SIP_FORCE_CONTACT_HOST or SIP_FORCE_CONTACT_PORT) and host_for_contact and port_for_contact:
            contact_override = f"sip:{SIP_USER}@{host_for_contact}:{port_for_contact}"

    if contact_override:
        acc_cfg.regConfig.contactUri = contact_override
        print(f"[acc] Forcing contact URI to {contact_override}")
        if listen_port and contact_override.endswith(f":{listen_port}"):
            pass
        elif listen_port:
            print(
                f"[acc] Warning: forced contact port does not match actual listener {listen_port};"
                " ensure firewall/NAT forwards that port"
            )
    # Force plain RTP to match callers that do not negotiate SRTP
    acc_cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_DISABLED

    cred = pj.AuthCredInfo("digest", SIP_DOMAIN, SIP_USER, 0, SIP_PASSWD)
    acc_cfg.sipConfig.authCreds.append(cred)

    acc = MyAccount()
    acc.create(acc_cfg)
    print(f"[acc] Created and registering as {acc_cfg.idUri}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[ep] Ctrl+C, shutting down...")
    finally:
        for mon in monitors:
            mon.stop()
        diag_monitor.stop()
        ep.libDestroy()
        print("[ep] libDestroy done.")


if __name__ == "__main__":
    main()
