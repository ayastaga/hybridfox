cmake_minimum_required(VERSION 3.21)
project(hybrid_chromium_host)

set_property(GLOBAL PROPERTY OS_FOLDERS ON)
if(NOT CEF_ROOT)
  set(CEF_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/cef")
endif()
list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT}/cmake")
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(CEF REQUIRED)
add_subdirectory(${CEF_LIBCEF_DLL_WRAPPER_PATH} libcef_dll_wrapper)

set(OUT "${CMAKE_CURRENT_BINARY_DIR}/out")
ADD_LOGICAL_TARGET("libcef_lib" "${CEF_LIB_DEBUG}" "${CEF_LIB_RELEASE}")

add_executable(chromium-host chromium_host.cc)
SET_EXECUTABLE_TARGET_PROPERTIES(chromium-host)
add_dependencies(chromium-host libcef_dll_wrapper)
target_link_libraries(chromium-host libcef_lib libcef_dll_wrapper ${CEF_STANDARD_LIBS} X11)
set_target_properties(chromium-host PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${OUT}"
  BUILD_WITH_INSTALL_RPATH TRUE
  INSTALL_RPATH "$ORIGIN")

COPY_FILES(chromium-host "${CEF_BINARY_FILES}" "${CEF_BINARY_DIR}" "${OUT}")
COPY_FILES(chromium-host "${CEF_RESOURCE_FILES}" "${CEF_RESOURCE_DIR}" "${OUT}")
