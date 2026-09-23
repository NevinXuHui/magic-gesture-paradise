cmake_minimum_required(VERSION 3.8)
project(homi_speech_interface)

if("${CMAKE_BUILD_TYPE}" STREQUAL "Release")
  set(CMAKE_C_FLAGS_RELEASE "${CMAKE_C_FLAGS_RELEASE} -s")
  set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -s")
endif()

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

#find dependencies
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(rclcpp REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
# uncomment the following section in order to fill in
# further dependencies manually.
# find_package(<dependency> REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/Followcfg.action"
  "msg/Trakerimageinfo.msg"
  "msg/FollowImageInfo.msg"
  "msg/TargetVelInfo.msg"   # 新建（告诉 ament 构建系统（ROS 2 使用的构建系统）在构建过程中包含并生成 TargetVelInfo.msg 的源代码）
  "msg/SIGCEvent.msg" 
  # 新增
  "action/Robotcfg.action"
  "action/RobotCmd.action"

  "msg/AssistantEvent.msg" 
  "msg/ContinueMove.msg" 
  "msg/DeviceState.msg" 
  "msg/JointState.msg" 
  "msg/ExecStep.msg" 
  "msg/IotControlParam.msg" 
  "msg/JpegStream.msg"
  "msg/NewUdpConnect.msg" 
  "msg/PCMStream.msg" 
  "msg/ProperToApp.msg" 
  "msg/ProprietySet.msg" 
  "msg/RobdogAction.msg" 
  "msg/RobdogState.msg" 
  "msg/Task.msg"
  "msg/AudioControlStatus.msg"
  "msg/TaskStep.msg" 
  "msg/Wakeup.msg"
  "msg/LiveStreamTask.msg"   
  "msg/TaskStatus.msg"
  "msg/UltrasonicData.msg"
  "msg/MediaResponse.msg"
  "msg/SubModuleVersion.msg"
  "msg/ComponentVersion.msg"
  "msg/SystemVersionInfo.msg"
  "msg/HomiPlayerControlStatus.msg"
  "msg/MotorSkillStatus.msg"

  "srv/AssistantAbort.srv"
  "srv/AssistantCtrl.srv"
  "srv/AssistantQuiet.srv"
  "srv/AssistantSpeechText.srv"
  "srv/AssistantTakePhoto.srv"
  "srv/BleHeadset.srv"
  "srv/EndPcmPlayer.srv"
  "srv/EndQtVideo.srv"
  "srv/EndVideoStream.srv"
  "srv/ForceIDR.srv"
  "srv/GetPcmPlayer.srv"
  "srv/GetPlayerStatus.srv"
  "srv/GetQtVideo.srv"
  "srv/GetVideoStream.srv"
  "srv/IotControl.srv"
  "srv/NetCtrl.srv"
  "srv/NtripAccount.srv"
  "srv/CommonStr.srv"
  "srv/PhoneCall.srv"
  "srv/PlayWav.srv"
  "srv/PrePlayWav.srv"
  "srv/SetDiyWord.srv"
  "srv/SetNrMode.srv"
  "srv/SetWakeEvent.srv"
  "srv/SIGCData.srv"
  "srv/StopWav.srv"
  "srv/UploadImage.srv"
  "srv/UploadImageUrl.srv"
  "srv/PlayFile.srv"
  "srv/PeripheralsCtrl.srv"
  "srv/PeripheralsStatus.srv"
  "srv/UltrasonicCtrl.srv"
  "srv/VisUnderstand.srv"
  "srv/GetVideoStatus.srv"
  "srv/MediaControl.srv"
  "srv/NetNotify.srv"  
  "srv/IotLocation.srv"
  "srv/GetVersionInfo.srv"
  "srv/ListComponentsByType.srv"
  "srv/EventTracking.srv"
  "srv/ExpressionConfig.srv"
  "srv/GetRobotState.srv"
  "srv/SetRobotState.srv"
  "srv/FacePersonManage.srv"
  "srv/MotorSkillCtrl.srv"
  "srv/ExpressionCtrl.srv"
  "srv/LightCtrl.srv"
  DEPENDENCIES builtin_interfaces geometry_msgs sensor_msgs std_msgs)


if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
