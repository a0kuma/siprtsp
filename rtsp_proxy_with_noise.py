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

# If we haven't seen a frame from /abc/in within this many seconds, use noise
SOURCE_TIMEOUT_SEC = 1.0


def start_ffmpeg(width, height, fps, out_url):
    """
    Start an ffmpeg process that reads raw BGR24 frames from stdin and pushes RTSP.
    """
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",

        # Input from stdin: raw video frames (OpenCV gives BGR)
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",

        # Encode + stream
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-g", str(fps * 2),

        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        out_url,
    ]

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


def main():
    # Start ffmpeg publisher for /abc/out
    ffmpeg = start_ffmpeg(WIDTH, HEIGHT, FPS, OUT_URL)
    print(f"[main] Started ffmpeg -> {OUT_URL}")

    reader = RtspReader(IN_URL)
    reader.start()
    print(f"[main] Started RTSP reader for {IN_URL}")

    frame_interval = 1.0 / FPS
    using_input = False
    last_status_print = 0.0

    try:
        while True:
            loop_start = time.time()

            # Check if ffmpeg is still alive
            if ffmpeg.poll() is not None:
                print("[main] ffmpeg exited, stopping.", file=sys.stderr)
                break

            # Get freshest frame from /abc/in (if any)
            in_frame, age = reader.get_latest_frame()
            use_input = (
                in_frame is not None
                and age is not None
                and age <= SOURCE_TIMEOUT_SEC
            )

            if use_input:
                # Resize to our output size if needed
                if (in_frame.shape[1] != WIDTH) or (in_frame.shape[0] != HEIGHT):
                    frame = cv2.resize(in_frame, (WIDTH, HEIGHT))
                else:
                    frame = in_frame
            else:
                # No fresh input -> white noise (video snow)
                frame = np.random.randint(
                    0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8
                )

            # Debug print when mode changes or every ~5 seconds
            now = time.time()
            if use_input != using_input or (now - last_status_print) > 5.0:
                using_input = use_input
                last_status_print = now
                if using_input:
                    print("[main] Using /abc/in stream as source")
                else:
                    print("[main] Using WHITE NOISE as source")

            # Write frame to ffmpeg stdin
            try:
                ffmpeg.stdin.write(frame.tobytes())
            except BrokenPipeError:
                print("[main] Broken pipe to ffmpeg, exiting.", file=sys.stderr)
                break

            # Keep roughly real-time pace
            elapsed = time.time() - loop_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n[main] Ctrl+C received, exiting.")

    finally:
        reader.stop()
        if ffmpeg.stdin:
            try:
                ffmpeg.stdin.close()
            except Exception:
                pass
        if ffmpeg.poll() is None:
            ffmpeg.terminate()
        reader.join()
        print("[main] Clean shutdown")


if __name__ == "__main__":
    main()
