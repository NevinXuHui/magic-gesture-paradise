from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():
    # 获取配置文件路径
    config_dir = os.path.join(
        get_package_share_directory('claw_client'),
        'config'
    )
    hermes_config = os.path.join(config_dir, 'hermes_bridge.yaml')

    hermes_bridge_node = Node(
        package='claw_client',
        executable='hermes_bridge',
        name='hermes_bridge',
        output='screen',
        parameters=[hermes_config],
        respawn=False
    )

    return LaunchDescription([
        hermes_bridge_node
    ])
