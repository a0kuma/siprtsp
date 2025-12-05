cmake_minimum_required(VERSION 3.10)
project(SipRtspProject)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find linphone package using pkg-config
find_package(PkgConfig REQUIRED)
pkg_check_modules(LINPHONE REQUIRED linphone)

# List audio devices utility
add_executable(list_audio_devices list_audio_devices.cpp)
target_include_directories(list_audio_devices PRIVATE ${LINPHONE_INCLUDE_DIRS})
target_link_libraries(list_audio_devices linphone++ ${LINPHONE_LIBRARIES})
target_link_directories(list_audio_devices PRIVATE ${LINPHONE_LIBRARY_DIRS})
target_compile_options(list_audio_devices PRIVATE ${LINPHONE_CFLAGS_OTHER})

# SIP Client application
add_executable(sip_client sip_client.cpp)
target_include_directories(sip_client PRIVATE ${LINPHONE_INCLUDE_DIRS})
target_link_libraries(sip_client linphone++ ${LINPHONE_LIBRARIES})
target_link_directories(sip_client PRIVATE ${LINPHONE_LIBRARY_DIRS})
target_compile_options(sip_client PRIVATE ${LINPHONE_CFLAGS_OTHER})
