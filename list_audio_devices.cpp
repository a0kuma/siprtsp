#include <iostream>
#include <linphone++/linphone.hh>

using namespace std;
using namespace linphone;

int main() {
    // Create a Linphone factory
    shared_ptr<Factory> factory = Factory::get();
    
    // Create a Core with default config
    shared_ptr<Core> core = factory->createCore("", "", nullptr);
    
    // Start the core
    core->start();
    
    // Get extended audio devices
    list<shared_ptr<AudioDevice>> audioDevices = core->getExtendedAudioDevices();
    
    cout << "=== Extended Audio Devices ===" << endl;
    cout << "Total devices found: " << audioDevices.size() << endl;
    cout << endl;
    
    int index = 0;
    for (const auto& device : audioDevices) {
        cout << "Device " << index++ << ":" << endl;
        cout << "  ID: " << device->getId() << endl;
        cout << "  Device Name: " << device->getDeviceName() << endl;
        cout << "  Driver Name: " << device->getDriverName() << endl;
        
        // Get capabilities
        AudioDevice::Capabilities caps = device->getCapabilities();
        cout << "  Capabilities: ";
        if (static_cast<int>(caps) & static_cast<int>(AudioDevice::Capabilities::CapabilityRecord)) {
            cout << "Record ";
        }
        if (static_cast<int>(caps) & static_cast<int>(AudioDevice::Capabilities::CapabilityPlay)) {
            cout << "Playback ";
        }
        cout << endl;
        
        // Get type
        AudioDevice::Type type = device->getType();
        cout << "  Type: ";
        switch (type) {
            case AudioDevice::Type::Unknown:
                cout << "Unknown";
                break;
            case AudioDevice::Type::Microphone:
                cout << "Microphone";
                break;
            case AudioDevice::Type::Earpiece:
                cout << "Earpiece";
                break;
            case AudioDevice::Type::Speaker:
                cout << "Speaker";
                break;
            case AudioDevice::Type::Bluetooth:
                cout << "Bluetooth";
                break;
            case AudioDevice::Type::BluetoothA2DP:
                cout << "Bluetooth A2DP";
                break;
            case AudioDevice::Type::Telephony:
                cout << "Telephony";
                break;
            case AudioDevice::Type::AuxLine:
                cout << "Aux Line";
                break;
            case AudioDevice::Type::GenericUsb:
                cout << "Generic USB";
                break;
            case AudioDevice::Type::Headset:
                cout << "Headset";
                break;
            case AudioDevice::Type::Headphones:
                cout << "Headphones";
                break;
            case AudioDevice::Type::HearingAid:
                cout << "Hearing Aid";
                break;
            default:
                cout << "Other";
                break;
        }
        cout << endl;
        cout << endl;
    }
    
    // Stop the core
    core->stop();
    
    return 0;
}
