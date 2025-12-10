#include <iostream>
#include <string>
#include <memory>
#include <thread>
#include <chrono>
#include <fstream>
#include <csignal>
#include <cstdlib>
#include <atomic>
#include <linphone++/linphone.hh>

using namespace std;
using namespace linphone;

// RTSP stream URLs for virtual audio devices
// These can be overridden via environment variables
string VIRTUAL_MIC = "rtsp://140.112.31.164:8554/u5004/mic";
string VIRTUAL_SPK = "rtsp://140.112.31.164:8554/u5004/spk";

// Virtual device names (PulseAudio/PipeWire device names)
// These should match the devices created by setup_rtsp_audio.sh
// Note: linphone sees PulseAudio device descriptions, not sink names
string VIRTUAL_MIC_DEVICE = "RTSP_Mic";       // Remap-source with Record capability
string VIRTUAL_SPK_DEVICE = "RTSP_Speaker";   // Sink for speaker output

// Global flag for shutdown
volatile sig_atomic_t g_running = 1;

void signal_handler(int signum) {
    cout << "\n[ep] Ctrl+C, shutting down..." << endl;
    g_running = 0;
}

// Helper to convert Call::State to string
static const char* callStateToString(Call::State state) {
    switch (state) {
        case Call::State::Idle: return "Idle";
        case Call::State::IncomingReceived: return "IncomingReceived";
        case Call::State::PushIncomingReceived: return "PushIncomingReceived";
        case Call::State::OutgoingInit: return "OutgoingInit";
        case Call::State::OutgoingProgress: return "OutgoingProgress";
        case Call::State::OutgoingRinging: return "OutgoingRinging";
        case Call::State::OutgoingEarlyMedia: return "OutgoingEarlyMedia";
        case Call::State::Connected: return "Connected";
        case Call::State::StreamsRunning: return "StreamsRunning";
        case Call::State::Pausing: return "Pausing";
        case Call::State::Paused: return "Paused";
        case Call::State::Resuming: return "Resuming";
        case Call::State::Referred: return "Referred";
        case Call::State::Error: return "Error";
        case Call::State::End: return "End";
        case Call::State::PausedByRemote: return "PausedByRemote";
        case Call::State::UpdatedByRemote: return "UpdatedByRemote";
        case Call::State::IncomingEarlyMedia: return "IncomingEarlyMedia";
        case Call::State::Updating: return "Updating";
        case Call::State::Released: return "Released";
        case Call::State::EarlyUpdatedByRemote: return "EarlyUpdatedByRemote";
        case Call::State::EarlyUpdating: return "EarlyUpdating";
        default: return "Unknown";
    }
}

// Simple .env loader
void load_dotenv(const string& dotenv_path = ".env") {
    ifstream file(dotenv_path);
    if (!file.is_open()) {
        return;
    }
    
    string line;
    while (getline(file, line)) {
        // Trim whitespace
        size_t start = line.find_first_not_of(" \t\r\n");
        if (start == string::npos) continue;
        size_t end = line.find_last_not_of(" \t\r\n");
        line = line.substr(start, end - start + 1);
        
        // Skip empty lines and comments
        if (line.empty() || line[0] == '#') continue;
        
        // Find '='
        size_t eq_pos = line.find('=');
        if (eq_pos == string::npos) continue;
        
        string key = line.substr(0, eq_pos);
        string val = line.substr(eq_pos + 1);
        
        // Trim key and value
        start = key.find_first_not_of(" \t");
        end = key.find_last_not_of(" \t");
        if (start != string::npos) {
            key = key.substr(start, end - start + 1);
        }
        
        start = val.find_first_not_of(" \t");
        end = val.find_last_not_of(" \t");
        if (start != string::npos) {
            val = val.substr(start, end - start + 1);
        }
        
        // Remove quotes
        if ((val.size() >= 2) && 
            ((val.front() == '"' && val.back() == '"') ||
             (val.front() == '\'' && val.back() == '\''))) {
            val = val.substr(1, val.size() - 2);
        }
        
        // Only set if not already in environment
        if (getenv(key.c_str()) == nullptr) {
            setenv(key.c_str(), val.c_str(), 0);
        }
    }
}

// Get environment variable with empty string default
string getenv_str(const char* name) {
    const char* val = getenv(name);
    return val ? string(val) : "";
}

// Load RTSP URLs from environment if set
void load_rtsp_config() {
    string mic = getenv_str("VIRTUAL_MIC");
    string spk = getenv_str("VIRTUAL_SPK");
    string mic_dev = getenv_str("VIRTUAL_MIC_DEVICE");
    string spk_dev = getenv_str("VIRTUAL_SPK_DEVICE");
    
    if (!mic.empty()) VIRTUAL_MIC = mic;
    if (!spk.empty()) VIRTUAL_SPK = spk;
    if (!mic_dev.empty()) VIRTUAL_MIC_DEVICE = mic_dev;
    if (!spk_dev.empty()) VIRTUAL_SPK_DEVICE = spk_dev;
    
    cout << "[config] VIRTUAL_MIC: " << VIRTUAL_MIC << endl;
    cout << "[config] VIRTUAL_SPK: " << VIRTUAL_SPK << endl;
    cout << "[config] VIRTUAL_MIC_DEVICE: " << VIRTUAL_MIC_DEVICE << endl;
    cout << "[config] VIRTUAL_SPK_DEVICE: " << VIRTUAL_SPK_DEVICE << endl;
}

// Helper function to find audio device by name pattern
shared_ptr<AudioDevice> findAudioDevice(const list<shared_ptr<AudioDevice>>& devices, 
                                         const string& pattern,
                                         AudioDevice::Capabilities requiredCaps) {
    // First try exact capability match
    for (const auto& dev : devices) {
        string devName = dev->getDeviceName();
        string devId = dev->getId();
        auto caps = dev->getCapabilities();
        
        // Check if device name or ID contains the pattern
        if ((devName.find(pattern) != string::npos || devId.find(pattern) != string::npos) &&
            (static_cast<int>(caps) & static_cast<int>(requiredCaps))) {
            return dev;
        }
    }
    
    // If not found with required caps, try matching by name only (for cases where 
    // PipeWire/PulseAudio may report capabilities differently)
    for (const auto& dev : devices) {
        string devName = dev->getDeviceName();
        string devId = dev->getId();
        
        if (devName.find(pattern) != string::npos || devId.find(pattern) != string::npos) {
            return dev;
        }
    }
    
    return nullptr;
}

// Core listener to handle incoming calls and registration state
class MyCoreListener : public CoreListener {
public:
    void onCallStateChanged(const shared_ptr<Core>& core, 
                           const shared_ptr<Call>& call,
                           Call::State state,
                           const string& message) override {
        int callId = call->getCallLog()->getCallId().empty() ? 0 : hash<string>{}(call->getCallLog()->getCallId()) % 10000;
        
        cout << "[call " << callId << "] State: " << callStateToString(state) 
             << " (" << message << ")" << endl;
        
        switch (state) {
            case Call::State::IncomingReceived: {
                // Incoming call
                auto remoteAddr = call->getRemoteAddress();
                cout << "[call " << callId << "] Incoming from: " << remoteAddr->asString() << endl;
                
                // Auto-answer with 200 OK
                cout << "[call " << callId << "] Auto-answer 200 OK" << endl;
                shared_ptr<CallParams> params = core->createCallParams(call);
                params->enableAudio(true);
                params->enableVideo(false);
                call->acceptWithParams(params);
                break;
            }
            
            case Call::State::StreamsRunning: {
                // Call is established and media is flowing
                cout << "[call " << callId << "] CONFIRMED, audio streams running" << endl;
                
                // Show current device configuration (set at core startup)
                auto inputDevice = call->getInputAudioDevice();
                auto outputDevice = call->getOutputAudioDevice();
                
                cout << "[call " << callId << "] Audio configuration:" << endl;
                if (inputDevice) {
                    cout << "[call " << callId << "]   Input:  " << inputDevice->getDeviceName() << endl;
                }
                if (outputDevice) {
                    cout << "[call " << callId << "]   Output: " << outputDevice->getDeviceName() << endl;
                }
                
                cout << "[call " << callId << "] RTSP streams:" << endl;
                cout << "[call " << callId << "]   MIC <- " << VIRTUAL_MIC << endl;
                cout << "[call " << callId << "]   SPK -> " << VIRTUAL_SPK << endl;
                
                break;
            }
            
            case Call::State::End:
            case Call::State::Released: {
                // Call ended
                cout << "[call " << callId << "] DISCONNECTED, cleanup" << endl;
                break;
            }
            
            case Call::State::Error: {
                cout << "[call " << callId << "] ERROR: " << message << endl;
                break;
            }
            
            default:
                break;
        }
    }
    
    void onRegistrationStateChanged(const shared_ptr<Core>& core,
                                   const shared_ptr<ProxyConfig>& proxyConfig,
                                   RegistrationState state,
                                   const string& message) override {
        cout << "[acc] Reg state: ";
        switch (state) {
            case RegistrationState::None:
                cout << "None";
                break;
            case RegistrationState::Progress:
                cout << "Progress";
                break;
            case RegistrationState::Ok:
                cout << "OK (registered)";
                break;
            case RegistrationState::Cleared:
                cout << "Cleared";
                break;
            case RegistrationState::Failed:
                cout << "Failed";
                break;
            default:
                cout << "Unknown";
        }
        cout << " (" << message << ")" << endl;
    }
    
    void onAccountRegistrationStateChanged(const shared_ptr<Core>& core,
                                          const shared_ptr<Account>& account,
                                          RegistrationState state,
                                          const string& message) override {
        cout << "[acc] Account reg state: ";
        switch (state) {
            case RegistrationState::None:
                cout << "None";
                break;
            case RegistrationState::Progress:
                cout << "Progress";
                break;
            case RegistrationState::Ok:
                cout << "OK (registered)";
                break;
            case RegistrationState::Cleared:
                cout << "Cleared";
                break;
            case RegistrationState::Failed:
                cout << "Failed";
                break;
            default:
                cout << "Unknown";
        }
        cout << " (" << message << ")" << endl;
    }
};

int main() {
    // Load .env file
    load_dotenv();
    
    // Load RTSP configuration
    load_rtsp_config();
    
    // Get SIP configuration from environment
    string sip_domain = getenv_str("SIP_DOMAIN");
    string sip_user = getenv_str("SIP_USER");
    string sip_passwd = getenv_str("SIP_PASSWD");
    
    if (sip_domain.empty() || sip_user.empty()) {
        cerr << "Error: SIP_DOMAIN and SIP_USER must be set in environment or .env file" << endl;
        return 1;
    }
    
    // Set up signal handler for Ctrl+C
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    
    // Create Linphone factory and core
    shared_ptr<Factory> factory = Factory::get();
    
    // Set resource paths for linphone data files (grammars, etc.)
    // These are typically installed in /usr/share/linphone or /usr/local/share/linphone
    string dataDir = "/usr/share/linphone";
    string altDataDir = "/usr/local/share/linphone";
    
    // Check which path exists
    ifstream testFile(dataDir + "/rootca.pem");
    if (!testFile.good()) {
        testFile.open(altDataDir + "/rootca.pem");
        if (testFile.good()) {
            dataDir = altDataDir;
        }
    }
    testFile.close();
    
    // Set top resources directory - this helps linphone find its grammar files
    factory->setTopResourcesDir(dataDir);
    factory->setDataResourcesDir(dataDir);
    factory->setImageResourcesDir(dataDir + "/images");
    factory->setRingResourcesDir(dataDir + "/rings");
    factory->setSoundResourcesDir(dataDir + "/sounds");
    
    cout << "[ep] Using linphone data dir: " << dataDir << endl;
    
    // Create core with default config (no config file, no factory config)
    shared_ptr<Core> core = factory->createCore("", "", nullptr);
    
    // Set log level (similar to pjsua2 level 4)
    factory->enableLogCollection(LogCollectionState::Disabled);
    /********************* this is not ok fix
    // Configure NAT traversal - critical for receiving ACKs through NAT/firewall
    // Get STUN server from environment or use a public one
    string stun_server = getenv_str("STUN_SERVER");
    if (stun_server.empty()) {
        stun_server = "stun.linphone.org";  // Default public STUN server
    }
    
    // Create NAT policy
    auto natPolicy = core->createNatPolicy();
    natPolicy->setStunServer(stun_server);
    natPolicy->enableStun(true);
    natPolicy->enableIce(true);  // Enable ICE for NAT traversal
    natPolicy->enableUpnp(false);
    core->setNatPolicy(natPolicy);
    
    cout << "[ep] NAT policy configured - STUN: " << stun_server << ", ICE: enabled" << endl;
    ********************* this is not ok fix  */
    // Configure audio to use RTSP streams
    // Set the input (microphone) to use the RTSP mic stream
    // Set the output (speaker) to use the RTSP spk stream
    // Note: This requires setting up virtual audio devices via PulseAudio/PipeWire
    // that are connected to the RTSP streams using GStreamer or FFmpeg
    cout << "[ep] Configuring RTSP audio streams..." << endl;
    cout << "[ep] Note: Ensure virtual audio devices are set up for RTSP streams" << endl;
    
    // Create and add core listener
    auto listener = make_shared<MyCoreListener>();
    core->addListener(listener);
    
    // Start the core first
    core->start();
    cout << "[ep] Linphone core started" << endl;
    
    // Configure transports - use random high port to avoid conflicts
    auto transports = core->getTransports();
    transports->setUdpPort(-1);  // -1 = random available port
    transports->setTcpPort(-1);  // -1 = random available port
    core->setTransports(transports);
    
    // Print actual ports used
    transports = core->getTransports();
    cout << "[ep] Transports configured - UDP:" << transports->getUdpPort() 
         << " TCP:" << transports->getTcpPort() << endl;
    
    // Set default audio devices to RTSP virtual devices BEFORE any calls
    // This is necessary because setInputAudioDevice/setOutputAudioDevice during a call
    // doesn't work with PulseAudio backend
    {
        auto audioDevices = core->getExtendedAudioDevices();
        cout << "[ep] Available audio devices:" << endl;
        for (const auto& dev : audioDevices) {
            auto caps = dev->getCapabilities();
            string capsStr = "";
            if (static_cast<int>(caps) & static_cast<int>(AudioDevice::Capabilities::CapabilityRecord)) {
                capsStr += "Record ";
            }
            if (static_cast<int>(caps) & static_cast<int>(AudioDevice::Capabilities::CapabilityPlay)) {
                capsStr += "Play ";
            }
            cout << "  - " << dev->getDeviceName() << " [" << capsStr << "]" << endl;
        }
        
        // Find and set default input device (mic)
        auto micDevice = findAudioDevice(audioDevices, VIRTUAL_MIC_DEVICE, 
                                          AudioDevice::Capabilities::CapabilityRecord);
        if (micDevice) {
            core->setDefaultInputAudioDevice(micDevice);
            cout << "[ep] Set default input to: " << micDevice->getDeviceName() << endl;
        } else {
            cout << "[ep] WARNING: Virtual mic '" << VIRTUAL_MIC_DEVICE << "' not found!" << endl;
        }
        
        // Find and set default output device (speaker)
        auto spkDevice = findAudioDevice(audioDevices, VIRTUAL_SPK_DEVICE,
                                          AudioDevice::Capabilities::CapabilityPlay);
        if (spkDevice) {
            core->setDefaultOutputAudioDevice(spkDevice);
            cout << "[ep] Set default output to: " << spkDevice->getDeviceName() << endl;
        } else {
            cout << "[ep] WARNING: Virtual speaker '" << VIRTUAL_SPK_DEVICE << "' not found!" << endl;
        }
    }
    
    // Set identity (SIP URI)
    string identity = "sip:" + sip_user + "@" + sip_domain;
    shared_ptr<Address> identityAddr = factory->createAddress(identity);
    if (!identityAddr) {
        cerr << "Error: Failed to create identity address: " << identity << endl;
        return 1;
    }
    
    // Add authentication info first
    shared_ptr<AuthInfo> authInfo = factory->createAuthInfo(
        sip_user,          // username
        sip_user,          // userid 
        sip_passwd,        // password
        "",                // ha1 (empty = use password)
        "",                // realm (empty = accept any)
        sip_domain         // domain
    );
    core->addAuthInfo(authInfo);
    cout << "[auth] Added auth info for " << sip_user << "@" << sip_domain << endl;
    
    // Create proxy config (the older but more reliable approach)
    shared_ptr<ProxyConfig> proxyCfg = core->createProxyConfig();
    
    // Set identity
    proxyCfg->setIdentityAddress(identityAddr);
    
    // Set server address
    string serverAddr = "sip:" + sip_domain;
    proxyCfg->setServerAddr(serverAddr);
    
    // Set route (same as server)
    proxyCfg->setRoute(serverAddr);
    
    // Enable registration
    proxyCfg->enableRegister(true);
    
    // Set registration expiry
    proxyCfg->setExpires(3600);
    
    // Disable publish
    proxyCfg->enablePublish(false);
    
    // Add proxy config to core
    core->addProxyConfig(proxyCfg);
    
    // Set as default
    core->setDefaultProxyConfig(proxyCfg);
    
    cout << "[acc] Created and registering as " << identity << endl;
    
    // Main loop - iterate the core
    while (g_running) {
        core->iterate();
        this_thread::sleep_for(chrono::milliseconds(20));
    }
    
    // Cleanup
    cout << "[ep] Shutting down..." << endl;
    
    // Terminate all calls
    core->terminateAllCalls();
    
    // Unregister
    if (proxyCfg) {
        proxyCfg->edit();
        proxyCfg->enableRegister(false);
        proxyCfg->done();
    }
    
    // Wait a bit for unregister to complete
    for (int i = 0; i < 50; i++) {
        core->iterate();
        this_thread::sleep_for(chrono::milliseconds(20));
    }
    
    // Remove listener and stop
    core->removeListener(listener);
    core->stop();
    
    cout << "[ep] libDestroy done." << endl;
    
    return 0;
}
