cmake_minimum_required(VERSION 3.8)
project(homi_speech_interface)

# 引入交叉编译工具链
include(${CMAKE_CUSTOM_TOOLCHIAN})

if("${CMAKE_BUILD_TYPE}" STREQUAL "Release")
  set(CMAKE_C_FLAGS_RELEASE "${CMAKE_C_FLAGS_RELEASE} -s")
  set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -s")
endif()

# Default to C99
if(NOT CMAKE_C_STANDARD)
  set(CMAKE_C_STANDARD 99)
endif()

# Default to C++17
if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# 设置交叉编译环境的头文件和库路径
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} ${CMAKE_CUSTOM_CROSS_RPATH}")

# 使用相对路径构建 x86_64 构建目录路径
# CMAKE_BINARY_DIR 指向当前包的构建目录: build_2.0_cross/debug/build/homi_speech_interface
# X86 构建目录在: build_2.0_cross/debug/build_x86/homi_speech_interface
# X86 安装目录在: build_2.0_cross/debug/install_x86/homi_speech_interface
set(X86_BUILD_DIR ${CMAKE_BINARY_DIR}/../../build_x86/homi_speech_interface)
set(X86_INSTALL_DIR ${CMAKE_BINARY_DIR}/../../install_x86/homi_speech_interface)
set(CROSS_ROS_DIR ${CMAKE_CUSTOM_COLCON}/../foxy)
set(CROSS_PYTHON_INCLUDE ${CMAKE_BINARY_DIR}/../../../../../pre/cross/python3.8)

include_directories(
  ${CMAKE_CUSTOM_CROSS_INC}
  ${CMAKE_CUSTOM_COLCON}/rcutils/include
  ${CMAKE_CUSTOM_COLCON}/follow_msgs/include
  ${X86_INSTALL_DIR}/include
  ${CROSS_ROS_DIR}/include
  # 添加 x86_64 build 目录中 rosidl 生成的头文件路径
  ${X86_BUILD_DIR}/rosidl_generator_c
  ${X86_BUILD_DIR}/rosidl_typesupport_c
  ${X86_BUILD_DIR}/rosidl_typesupport_cpp
  ${X86_BUILD_DIR}/rosidl_typesupport_introspection_c
  ${X86_BUILD_DIR}/rosidl_typesupport_introspection_cpp
  ${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_c
  ${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_cpp
)

link_directories(
  ${CMAKE_CUSTOM_CROSS_LIB}
  ${CMAKE_CUSTOM_COLCON}/rcutils/lib
  ${CMAKE_CUSTOM_COLCON}/follow_msgs/lib
  ${CROSS_ROS_DIR}/lib
)

# 收集 rosidl_generator_c 生成的 C 源文件
file(GLOB_RECURSE ROSIDL_GENERATOR_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_generator_c/homi_speech_interface/msg/detail/*__functions.c"
  "${X86_BUILD_DIR}/rosidl_generator_c/homi_speech_interface/srv/detail/*__functions.c"
  "${X86_BUILD_DIR}/rosidl_generator_c/homi_speech_interface/action/detail/*__functions.c"
)

# 收集 rosidl_typesupport_c 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_c/homi_speech_interface/msg/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_c/homi_speech_interface/srv/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_c/homi_speech_interface/action/*__type_support.cpp"
)

# 收集 rosidl_typesupport_cpp 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_CPP_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_cpp/homi_speech_interface/msg/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_cpp/homi_speech_interface/srv/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_cpp/homi_speech_interface/action/*__type_support.cpp"
)

# 收集 rosidl_typesupport_introspection_c 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_INTROSPECTION_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_c/homi_speech_interface/msg/detail/*__type_support.c"
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_c/homi_speech_interface/srv/detail/*__type_support.c"
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_c/homi_speech_interface/action/detail/*__type_support.c"
)

# 收集 rosidl_typesupport_introspection_cpp 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_INTROSPECTION_CPP_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_cpp/homi_speech_interface/msg/detail/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_cpp/homi_speech_interface/srv/detail/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_introspection_cpp/homi_speech_interface/action/detail/*__type_support.cpp"
)

# 收集 rosidl_typesupport_fastrtps_c 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_FASTRTPS_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_c/homi_speech_interface/msg/detail/*__type_support_c.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_c/homi_speech_interface/srv/detail/*__type_support_c.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_c/homi_speech_interface/action/detail/*__type_support_c.cpp"
)

# 收集 rosidl_typesupport_fastrtps_cpp 生成的 C++ 源文件
file(GLOB_RECURSE ROSIDL_TYPESUPPORT_FASTRTPS_CPP_SOURCES
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_cpp/homi_speech_interface/msg/detail/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_cpp/homi_speech_interface/srv/detail/*__type_support.cpp"
  "${X86_BUILD_DIR}/rosidl_typesupport_fastrtps_cpp/homi_speech_interface/action/detail/*__type_support.cpp"
)

# 编译 rosidl_generator_c 库
add_library(${PROJECT_NAME}__rosidl_generator_c SHARED
  ${ROSIDL_GENERATOR_C_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_generator_c
  rcutils
  rosidl_runtime_c
  # 链接标准 ROS2 消息的 generator_c 库
  action_msgs__rosidl_generator_c
  sensor_msgs__rosidl_generator_c
  builtin_interfaces__rosidl_generator_c
  unique_identifier_msgs__rosidl_generator_c
  std_msgs__rosidl_generator_c
)

# 编译 rosidl_typesupport_c 库
add_library(${PROJECT_NAME}__rosidl_typesupport_c SHARED
  ${ROSIDL_TYPESUPPORT_C_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_c
  ${PROJECT_NAME}__rosidl_generator_c
  rosidl_typesupport_c
  # 链接标准 ROS2 消息的 typesupport_c 库
  action_msgs__rosidl_typesupport_c
  sensor_msgs__rosidl_typesupport_c
  builtin_interfaces__rosidl_typesupport_c
  unique_identifier_msgs__rosidl_typesupport_c
  std_msgs__rosidl_typesupport_c
)

# 编译 rosidl_typesupport_cpp 库
add_library(${PROJECT_NAME}__rosidl_typesupport_cpp SHARED
  ${ROSIDL_TYPESUPPORT_CPP_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_cpp
  rosidl_typesupport_cpp
  rosidl_typesupport_c
  rosidl_typesupport_introspection_cpp
  # 链接标准 ROS2 消息的 typesupport_cpp 库（不是 introspection 版本）
  action_msgs__rosidl_typesupport_cpp
  sensor_msgs__rosidl_typesupport_cpp
  builtin_interfaces__rosidl_typesupport_cpp
  unique_identifier_msgs__rosidl_typesupport_cpp
  std_msgs__rosidl_typesupport_cpp
)

# 编译 rosidl_typesupport_fastrtps_c 库
add_library(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c SHARED
  ${ROSIDL_TYPESUPPORT_FASTRTPS_C_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c
  ${PROJECT_NAME}__rosidl_generator_c
  fastrtps
  fastcdr
  rosidl_typesupport_fastrtps_c
  # 链接标准 ROS2 消息的 typesupport_fastrtps_c 库
  action_msgs__rosidl_typesupport_fastrtps_c
  sensor_msgs__rosidl_typesupport_fastrtps_c
  builtin_interfaces__rosidl_typesupport_fastrtps_c
  unique_identifier_msgs__rosidl_typesupport_fastrtps_c
  std_msgs__rosidl_typesupport_fastrtps_c
)

# 编译 rosidl_typesupport_fastrtps_cpp 库
add_library(${PROJECT_NAME}__rosidl_typesupport_fastrtps_cpp SHARED
  ${ROSIDL_TYPESUPPORT_FASTRTPS_CPP_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_fastrtps_cpp
  fastrtps
  fastcdr
  rosidl_typesupport_fastrtps_cpp
  # 链接标准 ROS2 消息的 typesupport_fastrtps_cpp 库
  action_msgs__rosidl_typesupport_fastrtps_cpp
  sensor_msgs__rosidl_typesupport_fastrtps_cpp
  builtin_interfaces__rosidl_typesupport_fastrtps_cpp
  unique_identifier_msgs__rosidl_typesupport_fastrtps_cpp
  std_msgs__rosidl_typesupport_fastrtps_cpp
)

# 编译 rosidl_typesupport_introspection_c 库
add_library(${PROJECT_NAME}__rosidl_typesupport_introspection_c SHARED
  ${ROSIDL_TYPESUPPORT_INTROSPECTION_C_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_introspection_c
  ${PROJECT_NAME}__rosidl_generator_c
  rosidl_typesupport_introspection_c
  # 链接标准 ROS2 消息的 typesupport_introspection_c 库
  action_msgs__rosidl_typesupport_introspection_c
  sensor_msgs__rosidl_typesupport_introspection_c
  builtin_interfaces__rosidl_typesupport_introspection_c
  unique_identifier_msgs__rosidl_typesupport_introspection_c
  std_msgs__rosidl_typesupport_introspection_c
)

# 编译 rosidl_typesupport_introspection_cpp 库
add_library(${PROJECT_NAME}__rosidl_typesupport_introspection_cpp SHARED
  ${ROSIDL_TYPESUPPORT_INTROSPECTION_CPP_SOURCES}
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_introspection_cpp
  rosidl_typesupport_introspection_cpp
  # 链接标准 ROS2 消息的 introspection_cpp 库
  action_msgs__rosidl_typesupport_introspection_cpp
  sensor_msgs__rosidl_typesupport_introspection_cpp
  builtin_interfaces__rosidl_typesupport_introspection_cpp
  unique_identifier_msgs__rosidl_typesupport_introspection_cpp
  std_msgs__rosidl_typesupport_introspection_cpp
)

# 收集 Python 扩展模块的源文件
set(PYTHON_EXTENSION_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/_homi_speech_interface_s.ep.rosidl_typesupport_c.c"
)
set(PYTHON_EXTENSION_FASTRTPS_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/_homi_speech_interface_s.ep.rosidl_typesupport_fastrtps_c.c"
)
set(PYTHON_EXTENSION_INTROSPECTION_C_SOURCES
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/_homi_speech_interface_s.ep.rosidl_typesupport_introspection_c.c"
)

# 编译 Python typesupport_c 扩展模块
add_library(${PROJECT_NAME}__rosidl_typesupport_c__pyext MODULE
  ${PYTHON_EXTENSION_C_SOURCES}
)
set_target_properties(${PROJECT_NAME}__rosidl_typesupport_c__pyext PROPERTIES
  OUTPUT_NAME "homi_speech_interface_s__rosidl_typesupport_c"
  PREFIX ""
  SUFFIX ".so"
)
target_include_directories(${PROJECT_NAME}__rosidl_typesupport_c__pyext PRIVATE
  ${CROSS_PYTHON_INCLUDE}
  /usr/include/python3.8
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_c__pyext
  ${PROJECT_NAME}__rosidl_generator_c
  ${PROJECT_NAME}__rosidl_typesupport_c
  ${PROJECT_NAME}__python
)

# 编译 Python typesupport_fastrtps_c 扩展模块
add_library(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c__pyext MODULE
  ${PYTHON_EXTENSION_FASTRTPS_C_SOURCES}
)
set_target_properties(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c__pyext PROPERTIES
  OUTPUT_NAME "homi_speech_interface_s__rosidl_typesupport_fastrtps_c"
  PREFIX ""
  SUFFIX ".so"
)
target_include_directories(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c__pyext PRIVATE
  ${CROSS_PYTHON_INCLUDE}
  /usr/include/python3.8
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_fastrtps_c__pyext
  ${PROJECT_NAME}__rosidl_generator_c
  ${PROJECT_NAME}__rosidl_typesupport_fastrtps_c
  ${PROJECT_NAME}__python
)

# 编译 Python typesupport_introspection_c 扩展模块
add_library(${PROJECT_NAME}__rosidl_typesupport_introspection_c__pyext MODULE
  ${PYTHON_EXTENSION_INTROSPECTION_C_SOURCES}
)
set_target_properties(${PROJECT_NAME}__rosidl_typesupport_introspection_c__pyext PROPERTIES
  OUTPUT_NAME "homi_speech_interface_s__rosidl_typesupport_introspection_c"
  PREFIX ""
  SUFFIX ".so"
)
target_include_directories(${PROJECT_NAME}__rosidl_typesupport_introspection_c__pyext PRIVATE
  ${CROSS_PYTHON_INCLUDE}
  /usr/include/python3.8
)
target_link_libraries(${PROJECT_NAME}__rosidl_typesupport_introspection_c__pyext
  ${PROJECT_NAME}__rosidl_generator_c
  ${PROJECT_NAME}__rosidl_typesupport_introspection_c
  ${PROJECT_NAME}__python
)

# 收集 Python 绑定的所有源文件（用于 libhomi_speech_interface__python.so）
file(GLOB_RECURSE PYTHON_BINDING_SOURCES
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/msg/*_s.c"
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/srv/*_s.c"
  "${X86_BUILD_DIR}/rosidl_generator_py/homi_speech_interface/action/*_s.c"
)

# 编译 Python C 扩展库（包含 convert_from_py/convert_to_py 函数）
add_library(${PROJECT_NAME}__python SHARED
  ${PYTHON_BINDING_SOURCES}
)
target_include_directories(${PROJECT_NAME}__python PRIVATE
  ${CROSS_PYTHON_INCLUDE}
  /usr/include/python3.8
)
target_link_libraries(${PROJECT_NAME}__python
  ${PROJECT_NAME}__rosidl_generator_c
  rcutils
  rosidl_runtime_c
  # 链接标准 ROS2 消息的 Python 库
  builtin_interfaces__python
  std_msgs__python
  sensor_msgs__python
  unique_identifier_msgs__python
  action_msgs__python
)

# 安装库文件
install(TARGETS
  ${PROJECT_NAME}__rosidl_generator_c
  ${PROJECT_NAME}__rosidl_typesupport_c
  ${PROJECT_NAME}__rosidl_typesupport_cpp
  ${PROJECT_NAME}__rosidl_typesupport_fastrtps_c
  ${PROJECT_NAME}__rosidl_typesupport_fastrtps_cpp
  ${PROJECT_NAME}__rosidl_typesupport_introspection_c
  ${PROJECT_NAME}__rosidl_typesupport_introspection_cpp
  ${PROJECT_NAME}__python
  LIBRARY DESTINATION ${CMAKE_CUSTOM_INTALL}/lib
)

# 安装 Python 扩展模块（交叉编译的 aarch64 版本）
install(TARGETS
  ${PROJECT_NAME}__rosidl_typesupport_c__pyext
  ${PROJECT_NAME}__rosidl_typesupport_fastrtps_c__pyext
  ${PROJECT_NAME}__rosidl_typesupport_introspection_c__pyext
  LIBRARY DESTINATION ${CMAKE_CUSTOM_INTALL}/lib/python3.8/site-packages/homi_speech_interface
)

# 安装头文件
install(DIRECTORY
  ${X86_INSTALL_DIR}/include/
  DESTINATION ${CMAKE_CUSTOM_INTALL}/include
)

# 安装 share 目录（包含 ament 索引和配置文件）
install(DIRECTORY
  ${X86_INSTALL_DIR}/share/
  DESTINATION ${CMAKE_CUSTOM_INTALL}/share
)

# 安装 Python 绑定（从预编译目录复制，但排除 x86_64 的扩展模块）
install(DIRECTORY
  ${X86_INSTALL_DIR}/lib/python3.8/
  DESTINATION ${CMAKE_CUSTOM_INTALL}/lib/python3.8
  PATTERN "*__rosidl_typesupport_*.so" EXCLUDE
)
