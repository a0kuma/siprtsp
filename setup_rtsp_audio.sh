#!/bin/bash
# setup_rtsp_audio.sh
# Creates virtual audio devices connected to RTSP streams
# Requires: FFmpeg, PulseAudio or PipeWire with pactl

VIRTUAL_MIC_URL="${VIRTUAL_MIC:-rtsp://140.112.31.164:8554/u5004/mic}"
VIRTUAL_SPK_URL="${VIRTUAL_SPK:-rtsp://140.112.31.164:8554/u5004/spk}"

echo "Setting up RTSP audio bridges..."
echo "  MIC URL: $VIRTUAL_MIC_URL"
echo "  SPK URL: $VIRTUAL_SPK_URL"

# Create virtual sink for speaker output (audio going TO the RTSP stream)
echo "Creating virtual speaker sink..."
pactl load-module module-null-sink sink_name=rtsp_spk sink_properties=device.description="RTSP_Speaker"

# Create virtual source for microphone input (audio coming FROM the RTSP stream)  
echo "Creating virtual microphone source..."
# First create a null sink to receive the audio
pactl load-module module-null-sink \
  sink_name=rtsp_mic_sink \
  sink_properties="device.description=RTSP_Mic_Sink"
# Then create a remap-source to expose the monitor as a proper Audio/Source
# This makes it show up with [Record] capability in applications like linphone
pactl load-module module-remap-source \
  source_name=rtsp_mic \
  master=rtsp_mic_sink.monitor \
  source_properties="device.description=RTSP_Mic"

echo ""
echo "Virtual devices created:"
echo "  - Speaker output: rtsp_spk (use rtsp_spk.monitor as source)"
echo "  - Microphone input: rtsp_mic_sink.monitor"
echo ""
echo "Starting FFmpeg pipelines..."

# Start FFmpeg pipeline: RTSP mic stream -> virtual mic source
# This captures audio from RTSP and sends it to the virtual mic
# Use PULSE_SINK env var to force FFmpeg to use the correct sink
PULSE_SINK=rtsp_mic_sink ffmpeg -hide_banner -loglevel warning \
    -rtsp_transport tcp \
    -i "$VIRTUAL_MIC_URL" \
    -map 0:a \
    -f pulse \
    -ac 2 -ar 48000 \
    "RTSP_Mic_Input" &
MIC_PID=$!
echo "RTSP Mic pipeline started (PID: $MIC_PID)"

# Start FFmpeg pipeline: virtual speaker sink -> RTSP speaker stream
# This captures audio from virtual speaker and pushes it to RTSP server
# Also includes a looped image as video since RTSP server requires both A/V
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ffmpeg -hide_banner -loglevel warning \
    -re \
    -loop 1 -framerate 1 -i "$SCRIPT_DIR/vlcsnap-2025-07-28-18h57m36s822.png" \
    -f pulse -thread_queue_size 64 -ac 2 -i rtsp_spk.monitor \
    -vf "drawtext=text='%{localtime}':fontcolor=white:fontsize=28:x=20:y=20:box=1:boxcolor=0x00000080" \
    -map 0:v -map 1:a \
    -c:v libx264 -tune stillimage -preset ultrafast -pix_fmt yuv420p -g 50 -r 1 \
    -c:a aac -b:a 64k -ac 2 -ar 44100 \
    -f rtsp -rtsp_transport tcp \
    "$VIRTUAL_SPK_URL" &
SPK_PID=$!
echo "RTSP Spk pipeline started (PID: $SPK_PID)"

echo ""
echo "Audio bridges running. Press Ctrl+C to stop."
echo "Use these devices in your SIP client:"
echo "  Input (mic):  rtsp_mic_sink.monitor"
echo "  Output (spk): rtsp_spk"

# Cleanup on exit
cleanup() {
    echo "Stopping pipelines..."
    kill $MIC_PID 2>/dev/null
    kill $SPK_PID 2>/dev/null
    sleep 1
    echo "Unloading PipeWire null-sink modules..."
    
    # Find and destroy rtsp_spk node
    SPK_NODE_ID=$(pw-cli ls Node 2>/dev/null | grep -B 10 "rtsp_spk" | grep "id [0-9]*" | tail -1 | sed 's/.*id \([0-9]*\).*/\1/')
    if [ -n "$SPK_NODE_ID" ]; then
        echo "Destroying rtsp_spk node (id: $SPK_NODE_ID)"
        pw-cli destroy "$SPK_NODE_ID" 2>/dev/null
    fi
    
    # Find and destroy rtsp_mic_sink node
    MIC_NODE_ID=$(pw-cli ls Node 2>/dev/null | grep -B 10 "rtsp_mic_sink" | grep "id [0-9]*" | tail -1 | sed 's/.*id \([0-9]*\).*/\1/')
    if [ -n "$MIC_NODE_ID" ]; then
        echo "Destroying rtsp_mic_sink node (id: $MIC_NODE_ID)"
        pw-cli destroy "$MIC_NODE_ID" 2>/dev/null
    fi
    
    echo "Cleanup complete."
}
trap cleanup EXIT

# Wait for Ctrl+C
wait
