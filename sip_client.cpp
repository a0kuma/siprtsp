#include <iostream>
#include <string>
#include <memory>
#include <thread>
#include <chrono>
#include <fstream>
#include <csignal>
#include <cstdlib>
#include <linphone++/linphone.hh>

using namespace std;
using namespace linphone;

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
                
                // Get current audio devices
                auto inputDevice = call->getInputAudioDevice();
                auto outputDevice = call->getOutputAudioDevice();
                
                if (inputDevice) {
                    cout << "[call " << callId << "] Input device: " << inputDevice->getDeviceName() << endl;
                }
                if (outputDevice) {
                    cout << "[call " << callId << "] Output device: " << outputDevice->getDeviceName() << endl;
                }
                
                cout << "[call " << callId << "] Audio bridged to default devices" << endl;
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
    
    // Create core with default config (no config file, no factory config)
    shared_ptr<Core> core = factory->createCore("", "", nullptr);
    
    // Set log level (similar to pjsua2 level 4)
    factory->enableLogCollection(LogCollectionState::Disabled);
    
    // Create and add core listener
    auto listener = make_shared<MyCoreListener>();
    core->addListener(listener);
    
    // Configure transports - UDP on ephemeral port (0 = OS chooses)
    shared_ptr<Transports> transports = factory->createTransports();
    transports->setUdpPort(0);  // Let OS choose port, similar to LOCAL_SIP_PORT = 0
    core->setTransports(transports);
    
    // Start the core
    core->start();
    cout << "[ep] Linphone started, listening on UDP" << endl;
    
    // Create account configuration
    shared_ptr<AccountParams> accountParams = core->createAccountParams();
    
    // Set identity (SIP URI)
    string identity = "sip:" + sip_user + "@" + sip_domain;
    shared_ptr<Address> identityAddr = factory->createAddress(identity);
    accountParams->setIdentityAddress(identityAddr);
    
    // Set server/registrar address
    string serverAddr = "sip:" + sip_domain;
    shared_ptr<Address> serverAddress = factory->createAddress(serverAddr);
    accountParams->setServerAddress(serverAddress);
    
    // Enable registration
    accountParams->enableRegister(true);
    
    // Create and add account
    shared_ptr<Account> account = core->createAccount(accountParams);
    
    // Set authentication info
    shared_ptr<AuthInfo> authInfo = factory->createAuthInfo(
        sip_user,      // username
        "",            // userid (empty = use username)
        sip_passwd,    // password
        "",            // ha1 (empty = use password)
        sip_domain,    // realm
        sip_domain     // domain
    );
    core->addAuthInfo(authInfo);
    
    // Set as default account
    core->setDefaultAccount(account);
    
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
    if (account) {
        auto params = account->getParams()->clone();
        params->enableRegister(false);
        account->setParams(params);
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
