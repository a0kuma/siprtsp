#作廢


#!/usr/bin/env python3
import subprocess
import threading
import time
import sys
import cv2
import numpy as np

IN_URL = "rtsp://127.0.0.1:8554/abc/in"
OUT_URL = "rtsp://127.0.0.1:8554/abc/out"

# Output stream settings (what /abc/out will look like)
WIDTH = 1280
HEIGHT = 720
FPS = 25

# Audio settings
AUDIO_SAMPLE_RATE = 48000  # Hz

# If we haven't seen a frame from /abc/in within this many seconds, use noise
SOURCE_TIMEOUT_SEC = 1.0


def start_ffmpeg(mode, width, height, fps, in_url, out_url):
    """
    Start an ffmpeg process based on mode ('input' or 'noise').

    - 'input':  video from stdin, audio = /abc/in + white noise mix
    - 'noise':  video from stdin, audio from anoisesrc (white noise)
    """
    common_video_input = [
        # VIDEO INPUT from stdin (OpenCV BGR)
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",  # video input 0
    ]

    if mode == "input":
        # Use RTSP audio from IN_URL + anoisesrc, mix them (約 9:1)
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",

            # Video from stdin
            *common_video_input,

            # RTSP input for audio (and ignore its video)
            "-rtsp_transport", "tcp",
            "-i", in_url,  # input 1 (audio from /abc/in)

            # AUDIO INPUT: generated white noise (input 2)
            "-f", "lavfi",
            "-i", f"anoisesrc=c=white:r={AUDIO_SAMPLE_RATE}:a=0.1",

            # Audio mix: [1:a]*9 + [2:a]*1 -> [aout]
            "-filter_complex",
            "[1:a]volume=9[a1];[2:a]volume=1[a2];"
            "[a1][a2]amix=inputs=2:duration=longest[aout]",

            # Map: video from stdin (0:v), mixed audio [aout]
            "-map", "0:v:0",
            "-map", "[aout]",

            # Video encode
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline",
            "-g", str(fps * 2),

            # Audio encode
            "-c:a", "aac",
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-ac", "2",

            # Output RTSP
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            out_url,
        ]
    else:
        # 'noise' mode: white noise audio only
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",

            # Video from stdin
            *common_video_input,

            # AUDIO INPUT: generated white noise
            "-f", "lavfi",
            "-i", f"anoisesrc=c=white:r={AUDIO_SAMPLE_RATE}:a=0.1",  # input 1

            # Map: video from stdin (0:v), audio from anoisesrc (1:a)
            "-map", "0:v:0",
            "-map", "1:a:0",

            # Video encode
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-profile:v", "baseline",
            "-g", str(fps * 2),

            # Audio encode
            "-c:a", "aac",
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-ac", "2",

            # Output RTSP
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            out_url,
        ]

    print(f"[ffmpeg] Starting ffmpeg in {mode.upper()} mode")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


class RtspReader(threading.Thread):
    """
    Background thread that continuously tries to read frames from IN_URL.
    Keeps only the most recent frame + timestamp.
    """

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url = url
        self.cap = None
        self.lock = threading.Lock()
        self.latest_frame = None
        self.last_frame_ts = 0.0
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            if self.cap is None or not self.cap.isOpened():
                # Try to (re)open the RTSP input
                self._open_capture()
                # Small pause before next attempt
                time.sleep(1.0)
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                # Lost the stream; close and retry
                self.cap.release()
                self.cap = None
                time.sleep(1.0)
                continue

            with self.lock:
                self.latest_frame = frame
                self.last_frame_ts = time.time()

    def _open_capture(self):
        try:
            # You can tweak backend & options if needed:
            # self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            self.cap = cv2.VideoCapture(self.url)
        except Exception as e:
            print(f"[reader] Failed to open {self.url}: {e}", file=sys.stderr)
            self.cap = None

    def get_latest_frame(self):
        """
        Returns (frame, age_seconds) or (None, None) if no frame yet.
        """
        with self.lock:
            frame = self.latest_frame
            ts = self.last_frame_ts
        if frame is None:
            return None, None
        age = time.time() - ts
        return frame, age

    def stop(self):
        self.stop_flag = True
        if self.cap is not None:
            self.cap.release()


def stop_ffmpeg(ffmpeg):
    if ffmpeg is None:
        return
    try:
        if ffmpeg.stdin:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass
        if ffmpeg.poll() is None:
            ffmpeg.terminate()
    except Exception:
        pass


def main():
    reader = RtspReader(IN_URL)
    reader.start()
    print(f"[main] Started RTSP reader for {IN_URL}")

    frame_interval = 1.0 / FPS
    using_input = False
    last_status_print = 0.0

    # ffmpeg process + mode tracking
    ffmpeg = None
    current_mode = None  # "input" or "noise"

    try:
        while True:
            loop_start = time.time()

            # Get freshest frame from /abc/in (if any)
            in_frame, age = reader.get_latest_frame()
            use_input = (
                in_frame is not None
                and age is not None
                and age <= SOURCE_TIMEOUT_SEC
            )

            # Decide desired mode based on whether we have fresh input
            desired_mode = "input" if use_input else "noise"

            # If ffmpeg died for any reason, force restart in desired_mode
            if ffmpeg is not None and ffmpeg.poll() is not None:
                print("[main] ffmpeg exited, restarting.", file=sys.stderr)
                stop_ffmpeg(ffmpeg)
                ffmpeg = None
                current_mode = None

            # If mode changed, restart ffmpeg in new mode
            if desired_mode != current_mode:
                print(f"[main] Switching mode: {current_mode} -> {desired_mode}")
                stop_ffmpeg(ffmpeg)
                ffmpeg = start_ffmpeg(desired_mode, WIDTH, HEIGHT, FPS, IN_URL, OUT_URL)
                current_mode = desired_mode

            # ---- Build the frame to send (input + noise overlay) ----
            # Base noise frame
            noise_frame = np.random.randint(
                0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8
            )

            if use_input and in_frame is not None:
                # Resize to our output size if needed
                if (in_frame.shape[1] != WIDTH) or (in_frame.shape[0] != HEIGHT):
                    in_resized = cv2.resize(in_frame, (WIDTH, HEIGHT))
                else:
                    in_resized = in_frame

                # Blend input video (9) and noise (1)
                # frame = 0.9 * in_resized + 0.1 * noise_frame
                frame = cv2.addWeighted(in_resized, 0.9, noise_frame, 0.1, 0)
            else:
                # No fresh input -> pure white noise (video snow)
                frame = noise_frame

            # Debug print when video mode changes or every ~5 seconds
            now = time.time()
            if use_input != using_input or (now - last_status_print) > 5.0:
                using_input = use_input
                last_status_print = now
                if using_input:
                    print("[main] Using /abc/in VIDEO (mixed with noise) + AUDIO (mixed with noise)")
                else:
                    print("[main] Using WHITE NOISE VIDEO + AUDIO as source")

            # If ffmpeg isn't running for some reason, start in desired_mode
            if ffmpeg is None:
                ffmpeg = start_ffmpeg(desired_mode, WIDTH, HEIGHT, FPS, IN_URL, OUT_URL)
                current_mode = desired_mode

            # Write frame to ffmpeg stdin
            try:
                ffmpeg.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                print("[main] Broken pipe to ffmpeg, will restart.", file=sys.stderr)
                stop_ffmpeg(ffmpeg)
                ffmpeg = None
                current_mode = None
                # skip sleep; next loop will restart ffmpeg
                continue

            # Keep roughly real-time pace
            elapsed = time.time() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[main] Ctrl+C received, exiting.")

    finally:
        reader.stop()
        reader.join()
        stop_ffmpeg(ffmpeg)
        print("[main] Clean shutdown")


if __name__ == "__main__":
    main()
