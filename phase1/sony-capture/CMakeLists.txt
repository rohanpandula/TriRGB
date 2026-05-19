cmake_minimum_required(VERSION 3.20)
project(sony_capture CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Default to Release for performance/size; user can override with -DCMAKE_BUILD_TYPE=Debug.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# ---------------------------------------------------------------------------
# Sony Camera Remote SDK
#
# The SDK is not redistributable. The user must download it from Sony and
# either:
#   - extract its `app/CRSDK` headers and `external/crsdk/*.dylib` libs into
#     `third_party/sony_sdk/` next to this file, or
#   - point CMake at an external copy via `-DCRSDK_DIR=/path/to/unpacked/sdk`,
#     where that path contains `app/CRSDK/CameraRemote_SDK.h` and the dylibs
#     under `external/crsdk/`.
# ---------------------------------------------------------------------------
set(CRSDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/sony_sdk"
    CACHE PATH "Path to the unpacked Sony Camera Remote SDK")

set(_CRSDK_INCLUDE_CANDIDATES
    "${CRSDK_DIR}/app/CRSDK"
    "${CRSDK_DIR}/CRSDK"
    "${CRSDK_DIR}/include/CRSDK"
)
foreach(_p IN LISTS _CRSDK_INCLUDE_CANDIDATES)
    if(EXISTS "${_p}/CameraRemote_SDK.h")
        # Include parent so #include "CRSDK/CameraRemote_SDK.h" works.
        get_filename_component(_parent "${_p}" DIRECTORY)
        set(CRSDK_INCLUDE_DIR "${_parent}")
        break()
    endif()
endforeach()

set(_CRSDK_LIB_CANDIDATES
    "${CRSDK_DIR}/external/crsdk"
    "${CRSDK_DIR}/lib"
    "${CRSDK_DIR}"
)
foreach(_p IN LISTS _CRSDK_LIB_CANDIDATES)
    if(EXISTS "${_p}/libCr_Core.dylib")
        set(CRSDK_LIB_DIR "${_p}")
        break()
    endif()
endforeach()

if(NOT CRSDK_INCLUDE_DIR OR NOT CRSDK_LIB_DIR)
    message(FATAL_ERROR
        "Could not locate the Sony Camera Remote SDK.\n"
        "Expected to find CameraRemote_SDK.h under ${CRSDK_DIR}/app/CRSDK/\n"
        "and libCr_Core.dylib under ${CRSDK_DIR}/external/crsdk/.\n"
        "Set -DCRSDK_DIR=/path/to/unpacked/CrSDK or copy the SDK into "
        "third_party/sony_sdk/."
    )
endif()

message(STATUS "Sony SDK headers: ${CRSDK_INCLUDE_DIR}")
message(STATUS "Sony SDK libs:    ${CRSDK_LIB_DIR}")

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------
add_executable(sony-capture src/main.cpp)
target_include_directories(sony-capture PRIVATE "${CRSDK_INCLUDE_DIR}")
target_link_libraries(sony-capture PRIVATE
    "${CRSDK_LIB_DIR}/libCr_Core.dylib"
)

# At runtime the loader needs to find Cr_Core and its sibling dylibs
# (libusb-1.0.0, libCr_PTP_USB.dylib, libssh2). Embed the SDK directory in
# the rpath; CrAdapter is a subdir so add both.
set_target_properties(sony-capture PROPERTIES
    BUILD_WITH_INSTALL_RPATH TRUE
    INSTALL_RPATH "${CRSDK_LIB_DIR};${CRSDK_LIB_DIR}/CrAdapter"
)

# Strict warnings — SDK headers themselves are not strict-clean, so we relax
# them to non-fatal for the headers and keep our own code tight.
target_compile_options(sony-capture PRIVATE
    -Wall -Wextra -Wpedantic
    -Wno-unused-parameter   # SDK callback signatures have many unused args
)
