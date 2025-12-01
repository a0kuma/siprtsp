import errno
import os
import select
import stat
import subprocess
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


# =========================
# SIP 帳號設定
# =========================
# These values can be provided via environment or `./.env`.
SIP_DOMAIN = os.getenv("SIP_DOMAIN")
SIP_USER = os.getenv("SIP_USER")          # 這支 py 是 5004
SIP_PASSWD = os.getenv("SIP_PASSWD")

# 本機 SIP 來源 port
# 5060 會跟別的東西打架就改 0，讓 OS 自動選
LOCAL_SIP_PORT = 0

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
RTSP_FIFO_ROOT = Path(os.getenv("RTSP_FIFO_ROOT", "/tmp/siprtsp"))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

# =========================
# 全域 calls map
# =========================
# key: call.id  (int)
g_calls = {}


def _media_port_valid(media: Optional[pj.AudioMedia]) -> bool:
    if media is None:
        return False
    try:
        return media.getPortId() != -1
    except pj.Error:
        return False


def _ensure_fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            mode = path.stat().st_mode
        except OSError:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            if stat.S_ISFIFO(mode):
                return
            path.unlink()
    os.mkfifo(path, 0o600)


def _cleanup_fifo(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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
    try:
        return data.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _wait_fd_readable(fd: int, path: Path, timeout: float, *, proc: Optional[subprocess.Popen] = None, proc_name: str = "") -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for writer on FIFO {path}")
        if proc is not None and proc.poll() is not None:
            stderr_msg = _drain_process_stderr(proc)
            msg = f"{proc_name or 'subprocess'} exited with {proc.returncode}"
            if stderr_msg:
                msg = f"{msg}: {stderr_msg}"
            raise RuntimeError(msg)
        rlist, _, _ = select.select([fd], [], [], remaining)
        if rlist:
            return
        time.sleep(0.05)


class RtspAudioBridge:
    def __init__(self, call_id: int, audio_media: pj.AudioMedia):
        self.call_id = call_id
        self.audio_media = audio_media
        self.recorder: Optional[pj.AudioMediaRecorder] = None
        self.player: Optional[pj.AudioMediaPlayer] = None
        self.ffmpeg_spk: Optional[subprocess.Popen] = None
        self.ffmpeg_mic: Optional[subprocess.Popen] = None
        self.fifo_spk = RTSP_FIFO_ROOT / f"call_{call_id}_spk.wav"
        self.fifo_mic = RTSP_FIFO_ROOT / f"call_{call_id}_mic.wav"
        self._fifo_mic_guard_fd: Optional[int] = None
        self.mic_ready = False
        self.last_mic_error: Optional[str] = None

    def start(self) -> bool:
        _ensure_fifo(self.fifo_spk)
        _ensure_fifo(self.fifo_mic)
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
        if self.audio_media and self.recorder and _media_port_valid(self.recorder):
            try:
                self.audio_media.stopTransmit(self.recorder)
            except pj.Error:
                pass
        if self.player and self.audio_media and _media_port_valid(self.player):
            try:
                self.player.stopTransmit(self.audio_media)
            except pj.Error:
                pass

        self._close_fifo_guard()

        _terminate_process(self.ffmpeg_spk, f"call {self.call_id} spk")
        _terminate_process(self.ffmpeg_mic, f"call {self.call_id} mic")

        self.ffmpeg_spk = None
        self.ffmpeg_mic = None
        self.recorder = None
        self.player = None

        _cleanup_fifo(self.fifo_spk)
        _cleanup_fifo(self.fifo_mic)

    def _start_spk_pipeline(self) -> None:
        ffmpeg_cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            os.getenv("FFMPEG_LOGLEVEL", "warning"),
            "-fflags",
            "nobuffer",
            "-re",
            "-i",
            str(self.fifo_spk),
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
            os.getenv("FFMPEG_SPK_AR", "48000"),
            "-ac",
            os.getenv("FFMPEG_SPK_AC", "2"),
            "-f",
            "rtsp",
            "-rtsp_transport",
            os.getenv("FFMPEG_SPK_TRANSPORT", "tcp"),
            RTSP_SPK_URL,
        ]

        try:
            self.ffmpeg_spk = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as err:
            raise RuntimeError("ffmpeg binary not found for speaker pipeline") from err

        self.recorder = pj.AudioMediaRecorder()
        try:
            self.recorder.createRecorder(str(self.fifo_spk))
            self.audio_media.startTransmit(self.recorder)
        except pj.Error as err:
            raise RuntimeError(f"pjsua2 recorder init failed: {err}")

    def _start_mic_pipeline(self) -> None:
        guard_fd: Optional[int] = None
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
            os.getenv("FFMPEG_MIC_AR", "16000"),
            "-ac",
            os.getenv("FFMPEG_MIC_AC", "1"),
            "-f",
            "wav",
            str(self.fifo_mic),
        ]

        try:
            guard_fd = os.open(str(self.fifo_mic), os.O_RDONLY | os.O_NONBLOCK)
            self._fifo_mic_guard_fd = guard_fd
        except OSError as err:
            raise RuntimeError(f"failed to open FIFO guard for mic: {err}") from err

        try:
            self.ffmpeg_mic = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as err:
            self._close_fifo_guard()
            raise RuntimeError("ffmpeg binary not found for mic pipeline") from err

        try:
            _wait_fd_readable(
                guard_fd,
                self.fifo_mic,
                timeout=float(os.getenv("RTSP_MIC_FIFO_TIMEOUT", "5")),
                proc=self.ffmpeg_mic,
                proc_name="ffmpeg mic pipeline",
            )
        except TimeoutError as err:
            self._close_fifo_guard()
            raise RuntimeError(str(err)) from err

        time.sleep(float(os.getenv("RTSP_MIC_READY_DELAY", "0.1")))

        self.player = pj.AudioMediaPlayer()
        try:
            self.player.createPlayer(str(self.fifo_mic))
            self.player.startTransmit(self.audio_media)
        except pj.Error as err:
            self._close_fifo_guard()
            self.player = None
            raise RuntimeError(f"pjsua2 player init failed: {err}")
        else:
            self._close_fifo_guard()

    def _cleanup_failed_mic(self) -> None:
        self._close_fifo_guard()
        if self.player and self.audio_media and _media_port_valid(self.player):
            try:
                self.player.stopTransmit(self.audio_media)
            except pj.Error:
                pass
        self.player = None
        if self.ffmpeg_mic:
            _terminate_process(self.ffmpeg_mic, f"call {self.call_id} mic")
            self.ffmpeg_mic = None


    def _close_fifo_guard(self) -> None:
        if self._fifo_mic_guard_fd is not None:
            try:
                os.close(self._fifo_mic_guard_fd)
            except OSError:
                pass
            self._fifo_mic_guard_fd = None


class MyCall(pj.Call):
    def __init__(self, account, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(account, call_id)
        self.acc = account
        self.call_media: Optional[pj.AudioMedia] = None
        self.bridge: Optional[RtspAudioBridge] = None

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"[call {ci.id}] State: {ci.stateText} ({ci.lastReason})")

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            print(f"[call {ci.id}] CONFIRMED, bridge to RTSP pipelines")

            try:
                media = self.getMedia(0)
            except pj.Error:
                print(f"[call {ci.id}] getMedia(0) failed")
                return

            audio_media: Optional[pj.AudioMedia] = None
            if media is not None:
                try:
                    audio_media = pj.AudioMedia.typecastFromMedia(media)
                except (pj.Error, TypeError):
                    audio_media = None

            if audio_media is not None:
                try:
                    bridge = RtspAudioBridge(ci.id, audio_media)
                    mic_ready = bridge.start()
                except Exception as err:
                    print(f"[call {ci.id}] Failed to start RTSP bridge: {err}")
                else:
                    self.bridge = bridge
                    self.call_media = audio_media
                    if mic_ready:
                        print(f"[call {ci.id}] RTSP bridge active (spk -> {RTSP_SPK_URL}, mic <- {RTSP_MIC_URL})")
                    else:
                        error_hint = bridge.last_mic_error or "mic pipeline inactive"
                        print(f"[call {ci.id}] Speaker streaming to {RTSP_SPK_URL}; mic input unavailable ({error_hint})")
            else:
                print(f"[call {ci.id}] Media[0] could not be treated as AudioMedia")

        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"[call {ci.id}] DISCONNECTED, cleanup")

            if self.bridge:
                self.bridge.stop()
                self.bridge = None

            self.call_media = None

            if ci.id in g_calls:
                del g_calls[ci.id]
                print(f"[call {ci.id}] Removed from g_calls")


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

        op = pj.CallOpParam()
        op.statusCode = 200
        print(f"[call {ci.id}] Auto-answer 200 OK")
        call.answer(op)


def main():
    ep = pj.Endpoint()
    ep.libCreate()

    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 4
    ep_cfg.logConfig.consoleLevel = 4

    ep.libInit(ep_cfg)

    sip_cfg = pj.TransportConfig()
    sip_cfg.port = LOCAL_SIP_PORT
    ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, sip_cfg)

    ep.audDevManager().setNullDev()

    ep.libStart()
    print(f"[ep] PJSUA2 started, listening on UDP port {sip_cfg.port}")

    acc_cfg = pj.AccountConfig()
    acc_cfg.idUri = f"sip:{SIP_USER}@{SIP_DOMAIN}"
    acc_cfg.regConfig.registrarUri = f"sip:{SIP_DOMAIN}"

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
        ep.libDestroy()
        print("[ep] libDestroy done.")


if __name__ == "__main__":
    main()
