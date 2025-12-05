// setup_rtsp_audio.cpp
// Creates virtual audio devices connected to RTSP streams
// Requires: FFmpeg, PulseAudio or PipeWire with pactl
// Press 'q' to quit gracefully

#include <iostream>
#include <string>
#include <vector>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <sys/wait.h>
#include <termios.h>
#include <fcntl.h>
#include <poll.h>
#include <filesystem>
#include <linux/limits.h>

namespace fs = std::filesystem;

// Global state for cleanup
static std::vector<pid_t> child_pids;
static bool running = true;
static struct termios orig_termios;
static bool terminal_raw_mode = false;

// ANSI color codes
namespace Color {
    const char* Reset = "\033[0m";
    const char* Bold = "\033[1m";
    const char* Red = "\033[31m";
    const char* Green = "\033[32m";
    const char* Yellow = "\033[33m";
    const char* Blue = "\033[34m";
    const char* Cyan = "\033[36m";
    const char* Dim = "\033[2m";
}

void restore_terminal() {
    if (terminal_raw_mode) {
        tcsetattr(STDIN_FILENO, TCSAFLUSH, &orig_termios);
        terminal_raw_mode = false;
    }
}

void enable_raw_mode() {
    if (tcgetattr(STDIN_FILENO, &orig_termios) == -1) {
        return;
    }
    terminal_raw_mode = true;
    atexit(restore_terminal);
    
    struct termios raw = orig_termios;
    raw.c_lflag &= ~(ECHO | ICANON);  // Disable echo and canonical mode
    raw.c_cc[VMIN] = 0;   // Non-blocking read
    raw.c_cc[VTIME] = 0;
    tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw);
}

std::string get_env(const char* name, const std::string& default_value) {
    const char* val = std::getenv(name);
    return val ? std::string(val) : default_value;
}

std::string get_script_dir() {
    char path[PATH_MAX];
    ssize_t count = readlink("/proc/self/exe", path, PATH_MAX);
    if (count != -1) {
        return fs::path(std::string(path, count)).parent_path().string();
    }
    // Fallback to current directory
    char* cwd = getcwd(nullptr, 0);
    std::string result(cwd);
    free(cwd);
    return result;
}

int run_command(const std::string& cmd) {
    return system(cmd.c_str());
}

std::string run_command_output(const std::string& cmd) {
    std::string result;
    FILE* pipe = popen(cmd.c_str(), "r");
    if (pipe) {
        char buffer[256];
        while (fgets(buffer, sizeof(buffer), pipe)) {
            result += buffer;
        }
        pclose(pipe);
    }
    // Trim trailing whitespace
    while (!result.empty() && (result.back() == '\n' || result.back() == '\r')) {
        result.pop_back();
    }
    return result;
}

pid_t spawn_process(const std::vector<std::string>& args, 
                    const std::vector<std::pair<std::string, std::string>>& env_vars = {}) {
    pid_t pid = fork();
    if (pid == 0) {
        // Child process
        // Set environment variables
        for (const auto& [key, val] : env_vars) {
            setenv(key.c_str(), val.c_str(), 1);
        }
        
        // Redirect stdout/stderr to /dev/null for cleaner output
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        
        // Build argv
        std::vector<char*> argv;
        for (const auto& arg : args) {
            argv.push_back(const_cast<char*>(arg.c_str()));
        }
        argv.push_back(nullptr);
        
        execvp(argv[0], argv.data());
        _exit(127);  // exec failed
    }
    return pid;
}

void cleanup() {
    std::cout << "\n" << Color::Yellow << "Stopping pipelines..." << Color::Reset << std::endl;
    
    // Kill child processes
    for (pid_t pid : child_pids) {
        if (pid > 0) {
            kill(pid, SIGTERM);
        }
    }
    
    // Wait for children to terminate
    usleep(500000);  // 500ms
    
    for (pid_t pid : child_pids) {
        if (pid > 0) {
            int status;
            waitpid(pid, &status, WNOHANG);
        }
    }
    
    std::cout << Color::Dim << "Unloading PipeWire null-sink modules..." << Color::Reset << std::endl;
    
    // Find and destroy rtsp_spk node
    std::string spk_node_id = run_command_output(
        "pw-cli ls Node 2>/dev/null | grep -B 10 'rtsp_spk' | grep 'id [0-9]*' | tail -1 | sed 's/.*id \\([0-9]*\\).*/\\1/'"
    );
    if (!spk_node_id.empty()) {
        std::cout << Color::Dim << "  Destroying rtsp_spk node (id: " << spk_node_id << ")" << Color::Reset << std::endl;
        run_command("pw-cli destroy " + spk_node_id + " 2>/dev/null");
    }
    
    // Find and destroy rtsp_mic_sink node
    std::string mic_node_id = run_command_output(
        "pw-cli ls Node 2>/dev/null | grep -B 10 'rtsp_mic_sink' | grep 'id [0-9]*' | tail -1 | sed 's/.*id \\([0-9]*\\).*/\\1/'"
    );
    if (!mic_node_id.empty()) {
        std::cout << Color::Dim << "  Destroying rtsp_mic_sink node (id: " << mic_node_id << ")" << Color::Reset << std::endl;
        run_command("pw-cli destroy " + mic_node_id + " 2>/dev/null");
    }
    
    // Also try to find and destroy rtsp_mic remap-source
    std::string remap_node_id = run_command_output(
        "pw-cli ls Node 2>/dev/null | grep -B 10 'rtsp_mic\"' | grep 'id [0-9]*' | tail -1 | sed 's/.*id \\([0-9]*\\).*/\\1/'"
    );
    if (!remap_node_id.empty() && remap_node_id != mic_node_id) {
        std::cout << Color::Dim << "  Destroying rtsp_mic remap node (id: " << remap_node_id << ")" << Color::Reset << std::endl;
        run_command("pw-cli destroy " + remap_node_id + " 2>/dev/null");
    }
    
    std::cout << Color::Green << "Cleanup complete." << Color::Reset << std::endl;
}

void signal_handler(int sig) {
    running = false;
}

void print_header() {
    std::cout << Color::Bold << Color::Cyan;
    std::cout << "╔═══════════════════════════════════════════════════════════╗\n";
    std::cout << "║           RTSP Audio Bridge - Virtual Devices             ║\n";
    std::cout << "║        check device command: pactl list short sinks       ║\n";
    std::cout << "║                 want to remove device?                    ║\n";
    std::cout << "║      command: pw-cli ls Node   ...   then find out id     ║\n";
    std::cout << "║        and run: pw-cli destroy <id>                       ║\n";
    std::cout << "╚═══════════════════════════════════════════════════════════╝\n";
    std::cout << Color::Reset << std::endl;
}

void print_status(const std::string& mic_url, const std::string& spk_url, 
                  pid_t mic_pid, pid_t spk_pid) {
    std::cout << Color::Bold << "Configuration:" << Color::Reset << std::endl;
    std::cout << "  " << Color::Blue << "MIC URL: " << Color::Reset << mic_url << std::endl;
    std::cout << "  " << Color::Blue << "SPK URL: " << Color::Reset << spk_url << std::endl;
    std::cout << std::endl;
    
    std::cout << Color::Bold << "Virtual Devices:" << Color::Reset << std::endl;
    std::cout << "  " << Color::Green << "● " << Color::Reset << "Speaker output: " << Color::Cyan << "rtsp_spk" << Color::Reset << std::endl;
    std::cout << "  " << Color::Green << "● " << Color::Reset << "Microphone input: " << Color::Cyan << "rtsp_mic" << Color::Reset << std::endl;
    std::cout << std::endl;
    
    std::cout << Color::Bold << "FFmpeg Pipelines:" << Color::Reset << std::endl;
    std::cout << "  " << Color::Green << "● " << Color::Reset << "RTSP Mic pipeline (PID: " << mic_pid << ")" << std::endl;
    std::cout << "  " << Color::Green << "● " << Color::Reset << "RTSP Spk pipeline (PID: " << spk_pid << ")" << std::endl;
    std::cout << std::endl;
    
    std::cout << Color::Bold << "Use these devices in your SIP client:" << Color::Reset << std::endl;
    std::cout << "  Input (mic):  " << Color::Cyan << "rtsp_mic" << Color::Reset << std::endl;
    std::cout << "  Output (spk): " << Color::Cyan << "rtsp_spk" << Color::Reset << std::endl;
    std::cout << std::endl;
}

void print_controls() {
    std::cout << Color::Bold << "Controls:" << Color::Reset << std::endl;
    std::cout << "  " << Color::Yellow << "[q]" << Color::Reset << " Quit" << std::endl;
    std::cout << "  " << Color::Yellow << "[s]" << Color::Reset << " Show status" << std::endl;
    std::cout << "  " << Color::Yellow << "[r]" << Color::Reset << " Restart pipelines" << std::endl;
    std::cout << "  " << Color::Yellow << "[v]" << Color::Reset << " View audio sinks" << std::endl;
    std::cout << std::endl;
    std::cout << Color::Dim << "Audio bridges running..." << Color::Reset << std::endl;
}

bool check_process_alive(pid_t pid) {
    if (pid <= 0) return false;
    int status;
    pid_t result = waitpid(pid, &status, WNOHANG);
    return result == 0;  // Process still running
}

bool sink_exists(const std::string& sink_name) {
    std::string output = run_command_output("pactl list short sinks 2>/dev/null");
    // Check if sink_name appears as a word in the output
    // Format: "82      rtsp_spk        PipeWire..."
    std::string search = "\t" + sink_name + "\t";
    return output.find(search) != std::string::npos;
}

bool source_exists(const std::string& source_name) {
    std::string output = run_command_output("pactl list short sources 2>/dev/null");
    std::string search = "\t" + source_name + "\t";
    return output.find(search) != std::string::npos;
}

int main(int argc, char* argv[]) {
    // Get RTSP URLs from environment or use defaults
    std::string mic_url = get_env("VIRTUAL_MIC", "rtsp://140.112.31.164:8554/u5004/mic");
    std::string spk_url = get_env("VIRTUAL_SPK", "rtsp://140.112.31.164:8554/u5004/spk");
    
    // Get script directory for image file
    std::string script_dir;
    if (argc > 0) {
        fs::path exe_path = fs::canonical(fs::path(argv[0]));
        script_dir = exe_path.parent_path().parent_path().string();  // Go up from build/
    }
    if (script_dir.empty() || !fs::exists(script_dir)) {
        script_dir = get_script_dir();
    }
    
    std::string image_path = script_dir + "/vlcsnap-2025-07-28-18h57m36s822.png";
    if (!fs::exists(image_path)) {
        // Try current directory's parent
        image_path = "../vlcsnap-2025-07-28-18h57m36s822.png";
    }
    
    // Setup signal handlers
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    signal(SIGCHLD, SIG_IGN);  // Prevent zombies
    
    print_header();
    
    std::cout << Color::Yellow << "Setting up RTSP audio bridges..." << Color::Reset << std::endl;
    std::cout << std::endl;
    
    // Create virtual sink for speaker output (if not exists)
    if (sink_exists("rtsp_spk")) {
        std::cout << Color::Dim << "Virtual speaker sink already exists, skipping..." << Color::Reset << std::endl;
    } else {
        std::cout << Color::Dim << "Creating virtual speaker sink..." << Color::Reset << std::endl;
        run_command("pactl load-module module-null-sink sink_name=rtsp_spk "
                    "sink_properties=device.description=\"RTSP_Speaker\" 2>/dev/null");
    }
    
    // Create virtual source for microphone input (if not exists)
    if (sink_exists("rtsp_mic_sink")) {
        std::cout << Color::Dim << "Virtual microphone sink already exists, skipping..." << Color::Reset << std::endl;
    } else {
        std::cout << Color::Dim << "Creating virtual microphone source..." << Color::Reset << std::endl;
        run_command("pactl load-module module-null-sink sink_name=rtsp_mic_sink "
                    "sink_properties=\"device.description=RTSP_Mic_Sink\" 2>/dev/null");
    }
    
    // Create remap source (if not exists)
    if (source_exists("rtsp_mic")) {
        std::cout << Color::Dim << "Virtual microphone remap source already exists, skipping..." << Color::Reset << std::endl;
    } else {
        std::cout << Color::Dim << "Creating virtual microphone remap source..." << Color::Reset << std::endl;
        run_command("pactl load-module module-remap-source source_name=rtsp_mic "
                    "master=rtsp_mic_sink.monitor "
                    "source_properties=\"device.description=RTSP_Mic\" 2>/dev/null");
    }
    
    std::cout << std::endl;
    std::cout << Color::Green << "Virtual devices created!" << Color::Reset << std::endl;
    std::cout << Color::Dim << "Starting FFmpeg pipelines..." << Color::Reset << std::endl;
    std::cout << std::endl;
    
    // Start FFmpeg pipeline: RTSP mic stream -> virtual mic source
    pid_t mic_pid = spawn_process({
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", mic_url,
        "-map", "0:a",
        "-f", "pulse",
        "-ac", "2", "-ar", "48000",
        "RTSP_Mic_Input"
    }, {{"PULSE_SINK", "rtsp_mic_sink"}});
    child_pids.push_back(mic_pid);
    
    // Start FFmpeg pipeline: virtual speaker sink -> RTSP speaker stream
    pid_t spk_pid = spawn_process({
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re",
        "-loop", "1", "-framerate", "60", "-i", image_path,
        "-f", "pulse", "-thread_queue_size", "64", "-ac", "2", "-i", "rtsp_spk.monitor",
        "-vf", "drawtext=text='%{localtime}':fontcolor=white:fontsize=28:x=20:y=20:box=1:boxcolor=0x00000080",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "ultrafast", 
        "-pix_fmt", "yuv420p", "-g", "50", "-r", "1",
        "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-ar", "44100",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        spk_url
    });
    child_pids.push_back(spk_pid);
    
    // Small delay to let processes start
    usleep(500000);
    
    print_status(mic_url, spk_url, mic_pid, spk_pid);
    print_controls();
    
    // Enable raw terminal mode for key detection
    enable_raw_mode();
    
    // Main loop - check for keypresses and process status
    int check_counter = 0;
    while (running) {
        // Check for keypress
        char c;
        if (read(STDIN_FILENO, &c, 1) == 1) {
            switch (c) {
                case 'q':
                case 'Q':
                    running = false;
                    break;
                    
                case 's':
                case 'S':
                    restore_terminal();
                    std::cout << "\n";
                    print_status(mic_url, spk_url, mic_pid, spk_pid);
                    
                    // Check if processes are alive
                    std::cout << Color::Bold << "Process Status:" << Color::Reset << std::endl;
                    if (check_process_alive(mic_pid)) {
                        std::cout << "  " << Color::Green << "● " << Color::Reset 
                                  << "Mic pipeline: Running" << std::endl;
                    } else {
                        std::cout << "  " << Color::Red << "● " << Color::Reset 
                                  << "Mic pipeline: Stopped" << std::endl;
                    }
                    if (check_process_alive(spk_pid)) {
                        std::cout << "  " << Color::Green << "● " << Color::Reset 
                                  << "Spk pipeline: Running" << std::endl;
                    } else {
                        std::cout << "  " << Color::Red << "● " << Color::Reset 
                                  << "Spk pipeline: Stopped" << std::endl;
                    }
                    std::cout << std::endl;
                    print_controls();
                    enable_raw_mode();
                    break;
                    
                case 'r':
                case 'R':
                    restore_terminal();
                    std::cout << "\n" << Color::Yellow << "Restarting pipelines..." 
                              << Color::Reset << std::endl;
                    
                    // Kill existing processes
                    for (pid_t pid : child_pids) {
                        if (pid > 0) kill(pid, SIGTERM);
                    }
                    usleep(500000);
                    child_pids.clear();
                    
                    // Restart mic pipeline
                    mic_pid = spawn_process({
                        "ffmpeg", "-hide_banner", "-loglevel", "warning",
                        "-rtsp_transport", "tcp",
                        "-i", mic_url,
                        "-map", "0:a",
                        "-f", "pulse",
                        "-ac", "2", "-ar", "48000",
                        "RTSP_Mic_Input"
                    }, {{"PULSE_SINK", "rtsp_mic_sink"}});
                    child_pids.push_back(mic_pid);
                    
                    // Restart spk pipeline
                    spk_pid = spawn_process({
                        "ffmpeg", "-hide_banner", "-loglevel", "warning",
                        "-re",
                        "-loop", "1", "-framerate", "1", "-i", image_path,
                        "-f", "pulse", "-thread_queue_size", "64", "-ac", "2", 
                        "-i", "rtsp_spk.monitor",
                        "-map", "0:v", "-map", "1:a",
                        "-c:v", "libx264", "-tune", "stillimage", "-preset", "ultrafast", 
                        "-pix_fmt", "yuv420p", "-g", "50", "-r", "1",
                        "-c:a", "aac", "-b:a", "64k", "-ac", "2", "-ar", "44100",
                        "-f", "rtsp", "-rtsp_transport", "tcp",
                        spk_url
                    });
                    child_pids.push_back(spk_pid);
                    
                    usleep(500000);
                    std::cout << Color::Green << "Pipelines restarted!" << Color::Reset << std::endl;
                    std::cout << std::endl;
                    print_controls();
                    enable_raw_mode();
                    break;
                    
                case 'v':
                case 'V':
                    restore_terminal();
                    std::cout << "\n" << Color::Bold << "Audio Sinks:" << Color::Reset << std::endl;
                    {
                        std::string sinks_output = run_command_output("pactl list short sinks 2>/dev/null");
                        std::cout << Color::Cyan << sinks_output << Color::Reset << std::endl;
                    }
                    std::cout << std::endl;
                    print_controls();
                    enable_raw_mode();
                    break;
            }
        }
        
        // Periodically check if child processes are still running
        if (++check_counter >= 100) {  // Every ~1 second
            check_counter = 0;
            // Check processes but don't spam output
        }
        
        usleep(10000);  // 10ms sleep to avoid busy waiting
    }
    
    restore_terminal();
    cleanup();
    
    return 0;
}
