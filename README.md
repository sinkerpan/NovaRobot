# NovaRobot Workspace 源码说明

这是 NovaRobot 机器人在 ROS 2 Humble 下的源码工作区目录，位于 `/home/novarobot/NovaRobot_ws/src`。

当前 `src` 目录中包含了机器人底盘、定位、雷达、视觉、导航、应用桥接和控制相关的多个 ROS 2 软件包，实际用途是搭建一套完整的网球机器人控制与感知系统。

> 这个目录不是单一 package，而是一组功能模块的组合工作区。核心目标是：底盘控制 + IMU/LiDAR 定位 + 视觉识别 + 追球与捡球策略 + 监控和应用桥接。

---

## 1. 目录结构与功能分层

| 目录 / 包 | 主要用途 |
| --- | --- |
| `novarobot_bringup` | 总启动与底盘系统启动入口，负责启动底盘节点、IMU、EKF、App Bridge 等 |
| `novarobot_base_node` | 底盘轮式里程计 / odom -> base_footprint 等 TF 发布 |
| `novarobot_description` | 机器人 URDF / 机械模型与 TF 关系定义 |
| `novarobot_follower` | 网球追球 / 捡球跟随逻辑，包含 YOLO 目标跟踪和状态机 |
| `novarobot_vision_court` | 球场/视觉识别相关功能 |
| `wit_ros2_imu` | WIT IMU 串口驱动，发布 IMU 数据 |
| `livox_ros_driver2` | Livox MID360/雷达驱动 |
| `FASTLIO2_ROS2` | LiDAR-IMU 融合定位 / FAST-LIO 方案 |
| `slam_toolbox` | 激光 SLAM / 地图与定位支持 |
| `robot_localization` | EKF / robot_localization 过滤融合 |
| `pointcloud_to_laserscan-humble` | 点云转扫描线，用于 2D 激光避障与导航 |
| `YOLOX-ROS` | YOLO 检测推理 ROS 包 |
| `yolo_ros` | YOLO/视觉检测封装与接口 |
| `realsense-ros-4.57.4` | Intel RealSense 相机驱动 |
| `app_server` | App/Web 交互桥接、连接管理与页面服务 |
| `scan_tools` | 扫描线处理工具 |
| `imu_tools` | IMU 工具链 |

---

## 2. 这个工作区的实际用途

当前源码组合，面向的是一台 ROS 2 机器人平台，典型功能包括：

### 2.1 底盘与状态控制
- `novarobot_bringup` 包含总启动文件 `launch/novarobot_bringup.launch.py`
- 负责启动：
  - 底盘控制节点 `novarobot_bringup_T1`
  - 手柄控制节点 `novarobot_joy_T1`
  - 球机控制 / 收球装置控制
  - 底盘 TF 发布节点 `base_node_T1`
  - IMU 读取节点 `wit_ros2_imu`
  - EKF 融合节点 `robot_localization`
  - App bridge

### 2.2 传感器与定位
- `wit_ros2_imu`：读取 `/dev/imu_usb`，发布 IMU 数据
- `livox_ros_driver2`：读取 Livox 雷达数据
- `FASTLIO2_ROS2`：基于 LiDAR + IMU 的高精度里程计/定位
- `robot_localization`：用于 IMU、odom、laser 等多源融合
- `slam_toolbox`：用于建图和在线定位

### 2.3 视觉与追球
- `YOLOX-ROS` / `yolo_ros`：Yolo 检测推理与 ROS 接口
- `novarobot_follower`：网球追踪与捡球策略，采用 YOLO 检测结果控制机器人动作
- `novarobot_vision_court`：球场视觉判断和定位辅助

### 2.4 导航与避障
- `pointcloud_to_laserscan-humble`：把点云转 2D 激光扫描用于避障/导航
- `slam_toolbox`：建图和定位
- `robot_localization`：提高里程计与地图之间的稳定性

### 2.5 应用层
- `app_server`：启动 Web/App 桥接服务，便于远程查看、控制和调试机器人状态

---

## 3. 关键启动入口

### 3.1 总体底盘与机器人启动
```bash
source /opt/ros/humble/setup.bash
source /home/novarobot/NovaRobot_ws/install/setup.bash
ros2 launch novarobot_bringup novarobot_bringup.launch.py
```

这通常是最核心的启动入口，包含底盘控制、IMU、基础 TF 以及 EKF 等。

### 3.2 仅启动 LiDAR / 扫描 / SLAM 相关功能
根据实际脚本和 launch 文件，常见启动方式有：

```bash
ros2 launch novarobot_bringup livox_mid360_scan.launch.py
```

该 launch 文件用于 Livox 雷达驱动 + 点云转 scan 的组合。

### 3.3 IMU 相关启动
```bash
ros2 launch wit_ros2_imu rviz_and_imu.launch.py
```

### 3.4 视觉识别相关启动
```bash
ros2 launch yolox_ros_cpp yolox_tensorrt.launch.py
```

或由其他包装脚本启动 YOLO 检测节点和目标跟随逻辑。

### 3.5 App / Web bridge
```bash
ros2 launch app_server app_bridge.launch.py
```

---

## 4. 主要 ROS 节点和数据流

当前系统的典型数据链路通常是：

- IMU：`/imu/data_raw` / `/imu` 相关话题
- LiDAR：`/livox/lidar`、`/scan` 等
- odom：`/odom`、`/odometry/filtered`
- TF：`map -> odom -> base_footprint -> base_link`
- YOLO：检测结果 `DetectionArray` 或目标状态
- 控制：`/cmd_vel`、`/cmd_vel_nav`、`/cmd_vel_ball`

关键点：
- `base_node` 负责底盘 TF / odom 发布
- `robot_localization` 负责多源融合，降低里程计漂移
- `yolo` / follower 负责感知目标
- `ball machine` / controller 负责收球/机械动作

---

## 5. 编译与开发建议

建议在 ROS 2 工作区根目录执行：

```bash
cd /home/novarobot/NovaRobot_ws
source /opt/ros/humble/setup.bash
colcon build --symlink-install
source install/setup.bash
```

如果只需要调试某个单独包，可以按包名单独编译：

```bash
colcon build --packages-select novarobot_bringup --symlink-install
colcon build --packages-select wit_ros2_imu --symlink-install
colcon build --packages-select novarobot_follower --symlink-install
```

---

## 6. 当前代码状态说明

这个源码工作区目前处于“功能迭代中”的状态，特点是：

- 许多模块已经可以独立运行
- 但不同功能包之间的桥接与参数配置仍在持续调整
- 视觉、定位、避障和底盘控制的融合需要按实际硬件状态不断验证
- 不同 launch 文件可能用于不同阶段的调试或实验配置

因此，建议按功能分层调试：

1. 先保证 IMU / LiDAR / odom / TF 正常
2. 再确认 YOLO 检测和跟踪输出正常
3. 再验证底盘命令和避障逻辑
4. 最后做总启动和任务流程联调

---

## 7. 适合的开发工作流

### 场景 A：底盘联调
```bash
ros2 launch novarobot_bringup novarobot_bringup.launch.py
```

### 场景 B：雷达与地图
```bash
ros2 launch novarobot_bringup livox_mid360_scan.launch.py
```

### 场景 C：视觉追球
```bash
ros2 launch <yolo_pkg> <launch_file>
ros2 launch novarobot_bringup <相关追球启动文件>
```

### 场景 D：App 调试
```bash
ros2 launch app_server app_bridge.launch.py
```

---

## 8. 结论

当前 `/home/novarobot/NovaRobot_ws/src` 是一个典型的 ROS 2 机器人开发工作区，整合了：

- 传感器驱动
- 里程计与定位融合
- 地图/导航
- 视觉检测
- 追球/捡球策略
- 底盘控制
- 应用交互

它不是单一算法仓库，而是完整机器人的 ROS 2 控制与感知软件栈。

如果需要二次开发，建议按照“底盘 -> 传感器 -> 定位 -> 视觉 -> 控制”的顺序逐步验证，避免在总启动状态下同时排查过多问题。

---

## 9. 系统示例图

下面展示了当前机器人 App 的四个关键界面，分别对应发球控制、遥控/导航、控制参数和连接/配网状态，便于快速理解当前系统的交互方式。

<div align="center">
  <table>
    <tr>
      <td><img src="连接页面.jpg" alt="APP 配网连接界面" width="260" /></td>
      <td><img src="发球页面.jpg" alt="APP 发球控制界面" width="260" /></td>
    </tr>
    <tr>
      <td><img src="控制页面.jpg" alt="APP 连接与遥控界面" width="260" /></td>
      <td><img src="高级页面.jpg" alt="APP "控制参数界面 width="260" /></td>
    </tr>
  </table>
</div>

---

## 10. 整机外观图

下面展示两张 NovaRobot 的整机实景图，用于快速理解机器人的整体外观与现场使用状态。

<div align="center">
  <table>
    <tr>
      <td><img src="NovaRobot-火烧云-A-收官一球.png" alt="NovaRobot 整机图 A" width="600" /></td>
      <td><img src="NovaRobot-火烧云-B-网前同行.png" alt="NovaRobot 整机图 B" width="600" /></td>
    </tr>
  </table>
</div>

---

## 11. 备注

- 该工作区的配置和启动方式会随着硬件接线、IMU/LiDAR选择、launch 文件更新而调整。
- 具体节点名称、topic 名称、参数字段可能因代码迭代发生变化。
- 如需进入生产/稳定调试状态，建议优先保留一套“最稳定的 launch + param 配置”，并在每次更改前做回归验证。
