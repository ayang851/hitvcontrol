"""全局常量定义."""

# 集成域
DOMAIN = "hitvcontrol"

# Config Flow 配置项
CONF_DEVICE_ID = "device_id"
CONF_HOST = "host"
CONF_PIN = "pin"
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"

# 仪表盘卡片配置项
CONF_ADD_TO_DASHBOARD = "add_to_dashboard"
CONF_DASHBOARD_ID = "dashboard_id"
CONF_VIEW_PATH = "view_path"
CONF_NEW_VIEW_TITLE = "new_view_title"
CONF_NEW_VIEW_PATH = "new_view_path"

# MQTT 端口（始终使用 TLS 加密连接）
MQTT_PORT = 37771

# PIN 码生成 MQTT 认证信息的盐值（与电视端 HomeLink 保持一致）
MQTT_CREDENTIAL_USER_SALT = "homelink_user_salt"
MQTT_CREDENTIAL_PWD_SALT = "homelink_pwd_salt"
MQTT_CREDENTIAL_USERNAME_PREFIX = "homelink_"

# MQTT 连接参数
MQTT_CONNECT_TIMEOUT = 30         # TLS 连接超时（秒）
MQTT_KEEPALIVE = 30              # 心跳间隔（秒）
MQTT_VERSION = 3                  # MQTT 协议版本
MQTT_RECONNECT_INTERVAL = 60     # 重连间隔（秒）
MQTT_PUBLISH_TIMEOUT = 5         # 发布消息等待确认的超时（秒）
                                 # 注：电视端内嵌 Broker 对 QoS 2 确认握手响应缺失，
                                 # 曾导致 QoS 2 发布阻塞 30 秒，需用短超时兜底

# TLS 证书文件名（运行时由组件目录动态拼接绝对路径）
MQTT_TLS_CA_FILENAME = "certificate.pem"
MQTT_TLS_CERT_FILENAME = "client.pem"
MQTT_TLS_KEY_FILENAME = "client.pem"  # 证书和私钥合并存储

# MQTT Topic 模板（clientId 即 device_id）
MQTT_TOPIC_COMMAND = "/remoteapp/tv/homelink_cmd/{device_id}/actions/sendkey"
MQTT_TOPIC_LASTWILL = "/remoteapp/tv/homelink_cmd/{device_id}/actions/lastwill"
MQTT_TOPIC_CALLBACK = "/remoteapp/mobile/{device_id}/#"
MQTT_TOPIC_BROADCAST = "/remoteapp/mobile/broadcast/#"

# 遗嘱消息
MQTT_LASTWILL_PAYLOAD = "CLOSE"
MQTT_LASTWILL_QOS = 2

# Demo 阶段硬编码应用列表（调试用，后续由电视端 MQTT 推送替换）
# 每个元素：name=显示名称, package=启动包名, icon=HA 卡片图标（mdi）
DEMO_APPS = [
    {
        "name": "Demo App",
        "package": "com.hitvcontrol.demo",
        "icon": "mdi:application",
    }
]

# 服务名称
SERVICE_SEND_COMMAND = "send_command"

# 按键常量
KEY_POWER = "KEY_POWER"
KEY_HOME = "KEY_HOME"
KEY_MENU = "KEY_MENU"
KEY_RETURNS = "KEY_BACK"
KEY_JUBAO = "KEY_JUBAO"
KEY_VOLUMEDOWN = "KEY_VOLUMEDOWN"
KEY_VOLUMEUP = "KEY_VOLUMEUP"
KEY_MUTE = "KEY_MUTE"
KEY_DOWN = "KEY_DOWN"
KEY_UP = "KEY_UP"
KEY_LEFT = "KEY_LEFT"
KEY_RIGHT = "KEY_RIGHT"
KEY_OK = "KEY_OK"

# 信号源按键
KEY_SOURCE = "KEY_SOURCE"       # 循环切换信号源
KEY_AV = "KEY_AV"               # AV 输入

# 按键名 → 实际发送 payload 的映射表
# 卡片 YAML 中写常量名（如 KEY_RETURNS），发送时自动映射为实际 payload（如 KEY_BACK）
# 大部分按键名与 payload 相同，仅个别按键存在别名（如返回键）
KEY_MAP = {
    "KEY_POWER": KEY_POWER,
    "KEY_HOME": KEY_HOME,
    "KEY_MENU": KEY_MENU,
    "KEY_RETURNS": KEY_RETURNS,        # KEY_RETURNS → "KEY_BACK"
    "KEY_JUBAO": KEY_JUBAO,
    "KEY_VOLUMEDOWN": KEY_VOLUMEDOWN,
    "KEY_VOLUMEUP": KEY_VOLUMEUP,
    "KEY_MUTE": KEY_MUTE,
    "KEY_DOWN": KEY_DOWN,
    "KEY_UP": KEY_UP,
    "KEY_LEFT": KEY_LEFT,
    "KEY_RIGHT": KEY_RIGHT,
    "KEY_OK": KEY_OK,
    "KEY_SOURCE": KEY_SOURCE,
    "KEY_AV": KEY_AV,
}
