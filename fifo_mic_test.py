#!/usr/bin/env python3
"""Quick diagnostic for the /u5004/mic FIFO writer pipeline."""

import argparse
import os
import select
import stat
import subprocess
import sys
import time
from pathlib import Path


def ensure_fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        st_mode = path.stat().st_mode
        if not stat.S_ISFIFO(st_mode):
            path.unlink()
            os.mkfifo(path, 0o600)
        return
    os.mkfifo(path, 0o600)


def parse_args() -> argparse.Namespace:
    default_fifo = Path(os.getenv("RTSP_FIFO_ROOT", "/tmp/siprtsp")) / "test_mic_fifo.wav"
    parser = argparse.ArgumentParser(description="Pipe rtsp://.../mic into a FIFO and read the first chunk")
    parser.add_argument("--rtsp", default=os.getenv("RTSP_MIC_URL", "rtsp://127.0.0.1:8554/u5004/mic"), help="RTSP mic source URL")
    parser.add_argument("--fifo", default=str(default_fifo), help="FIFO path to write wav data into")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("RTSP_MIC_FIFO_TIMEOUT", "5")), help="Seconds to wait for data")
    parser.add_argument("--ffmpeg", default=os.getenv("FFMPEG_BIN", "ffmpeg"), help="ffmpeg binary to invoke")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fifo_path = Path(args.fifo)
    ensure_fifo(fifo_path)

    print(f"[test] Using FIFO {fifo_path}")
    print(f"[test] Pulling RTSP from {args.rtsp}")

    try:
        guard_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        print(f"[test] Failed to open guard reader: {exc}", file=sys.stderr)
        return 1

    ffmpeg_cmd = [
        args.ffmpeg,
        "-hide_banner",
        "-loglevel",
        os.getenv("FFMPEG_LOGLEVEL", "warning"),
        "-y",
        "-rtsp_transport",
        os.getenv("FFMPEG_MIC_TRANSPORT", "tcp"),
        "-i",
        args.rtsp,
        "-vn",
        "-acodec",
        os.getenv("FFMPEG_MIC_CODEC", "pcm_s16le"),
        "-ar",
        os.getenv("FFMPEG_MIC_AR", "16000"),
        "-ac",
        os.getenv("FFMPEG_MIC_AC", "1"),
        "-f",
        "wav",
        str(fifo_path),
    ]

    print(f"[test] Launching: {' '.join(ffmpeg_cmd)}")
    try:
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print(f"[test] ffmpeg not found at {args.ffmpeg}", file=sys.stderr)
        os.close(guard_fd)
        return 1

    deadline = time.monotonic() + args.timeout
    ready = False
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            rlist, _, _ = select.select([guard_fd], [], [], 0.1)
            if rlist:
                ready = True
                break
        if not ready:
            print("[test] Timed out waiting for FIFO to become readable")
            return_code = proc.poll()
            if return_code is not None:
                print(f"[test] ffmpeg exit code: {return_code}")
                if proc.stderr:
                    err_payload = proc.stderr.read().decode("utf-8", "ignore").strip()
                    if err_payload:
                        print(err_payload)
            return 2
    finally:
        os.close(guard_fd)

    print("[test] FIFO reports readable, consuming 4 KiB for sanity")
    try:
        with open(fifo_path, "rb", buffering=0) as fifo_reader:
            chunk = fifo_reader.read(4096)
    except OSError as exc:
        print(f"[test] Failed to read from FIFO: {exc}", file=sys.stderr)
        proc.terminate()
        proc.wait(timeout=1)
        return 3

    if not chunk:
        print("[test] FIFO readable but produced no data (writer closed)")
    else:
        print(f"[test] Captured {len(chunk)} bytes. WAV header magic: {chunk[:4]!r}")

    print("[test] Stopping ffmpeg")
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    if proc.stderr:
        err_payload = proc.stderr.read().decode("utf-8", "ignore").strip()
        if err_payload:
            print("[test] ffmpeg stderr:\n" + err_payload)

    print("[test] Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
