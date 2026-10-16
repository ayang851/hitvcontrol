# Hisense Home (hitvcontrol)

Home Assistant custom integration for TVs running the HomeLink service: automatic LAN discovery, PIN-based pairing, MQTT over TLS remote control, and one-click remote card setup on your dashboard.

把运行 **HomeLink** 服务的电视接入 Home Assistant 的自定义集成：局域网自动发现、PIN 码安全配对、MQTT over TLS 加密遥控，并可一键把遥控器卡片写入你的仪表盘。

## 功能特性

- **自动发现**：通过 SSDP/DLNA 主动扫描局域网中运行 HomeLink 服务的电视（同时校验 HomeLink 服务端口 37771 是否开放）
- **安全配对**：在电视端查看 PIN 码完成绑定；MQTT 账号密码由 PIN 码派生，通信全程 TLS 加密
- **遥控实体（remote）**：连接状态实时映射为实体可用性，断线自动重连
- **遥控器卡片**：绑定过程中可选择目标仪表盘，自动写入由原生 grid + button 组成的遥控器视图
- **媒体播放器实体（media_player）**：应用列表展示与启动（开发中，当前为演示数据）
- **send_command 服务**：向指定电视发送按键指令，可在自动化中调用

## 环境要求

- Home Assistant（Home Assistant OS / Supervised / Container 均可）
- 电视与 Home Assistant 在**同一局域网**（网络需允许 UDP 多播 239.255.255.250:1900）
- 电视端已安装并运行 HomeLink 应用

## 安装

### 方式一：HACS（推荐）

1. 打开 HACS → 右上角菜单（⋮）→ **自定义仓库 / Custom repositories**
2. 仓库地址填入 `https://github.com/ayang851/hitvcontrol`，类别选择 **集成 / Integration**
3. 添加后在 HACS 中搜索 **Hisense** 安装
4. 重启 Home Assistant

### 方式二：手动安装

1. 在本仓库 Releases 页面下载最新版本
2. 将 `custom_components/hitvcontrol` 整个文件夹放入你的 `config/custom_components/` 目录
3. 重启 Home Assistant

## 配置使用

1. 设置 → 设备与服务 → 添加集成 → 搜索 **Hisense Home**
2. 等待自动扫描（约 5 秒），在列表中选中要添加的电视
3. 在电视端打开 HomeLink 应用，查看 PIN 码并填入表单
4. 选择要把遥控器卡片写入的仪表盘（也可以跳过）
5. 完成绑定后，仪表盘中即可看到遥控器；相关实体可在自动化中使用

## 常见问题

| 现象 | 排查方向 |
|---|---|
| 扫描不到电视（5 秒内无响应） | 确认电视已开启 DLNA/UPnP 且 HomeLink 应用正在运行；确认 HA 与电视在同一网段；检查网络是否允许 UDP 多播 |
| 有 SSDP 响应但没有可用设备 | 电视端 HomeLink 服务未运行，或 37771 端口未开放 |
| PIN 码验证失败 | 回到电视端 HomeLink 应用重新查看 PIN 码后再试 |
| 提示“该电视已绑定” | 删除已有集成条目后重新添加 |

## 服务说明

### `hitvcontrol.send_command`

| 字段 | 必填 | 说明 |
|---|---|---|
| `device_id` | 是 | 目标电视的 device_id 或 entity_id |
| `command` | 是 | 按键名称，如 `KEY_OK`、`KEY_POWER`、`KEY_UP`、`KEY_VOLUMEUP` 等 |

支持的按键：`KEY_POWER`、`KEY_HOME`、`KEY_MENU`、`KEY_RETURNS`（返回）、`KEY_JUBAO`、`KEY_VOLUMEDOWN`、`KEY_VOLUMEUP`、`KEY_MUTE`、`KEY_UP`、`KEY_DOWN`、`KEY_LEFT`、`KEY_RIGHT`、`KEY_OK`、`KEY_SOURCE`、`KEY_AV`

## 反馈

使用中遇到问题，欢迎在本仓库 [Issues](https://github.com/ayang851/hitvcontrol/issues) 中反馈。
