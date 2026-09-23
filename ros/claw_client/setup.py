from setuptools import find_packages, setup
from glob import glob

package_name = 'claw_client'

setup(
    name=package_name,
    version='1.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='xuhui',
    maintainer_email='xuhui@cmhi.chinamobile.com',
    description='用户与 SpeechCore 数据发送和接口代理节点',
    license='Apache License 2.0',
    extras_require={
        'test': ['pytest'],
    },
    entry_points={
        'console_scripts': [
            'claw_client_node = claw_client.claw_client_node:main',
            'openclaw_bridge = claw_client.openclaw_bridge:main',
            'hermes_bridge = claw_client.hermes_bridge:main',
            'test_send = claw_client.test_send:main',
            'external_chat = claw_client.external_chat:main',
        ],
    },
)
