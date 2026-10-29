"""全局常量定义."""

# 集成域
DOMAIN = "hitvcontrol"

# Config Flow 配置项
CONF_DEVICE_ID = "device_id"
CONF_HOST = "host"
CONF_PIN = "pin"
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"

# 应用相关服务参数
CONF_APP_ID = "app_id"
CONF_LAUNCH_TYPE = "launch_type"

# 仪表盘卡片配置项
CONF_ADD_TO_DASHBOARD = "add_to_dashboard"
CONF_DASHBOARD_ID = "dashboard_id"
CONF_VIEW_PATH = "view_path"
CONF_NEW_VIEW_TITLE = "new_view_title"
CONF_NEW_VIEW_PATH = "new_view_path"

# 遥控器卡片样式（native=原生按钮卡片；universal-remote-card=社区卡片）
CONF_CARD_STYLE = "card_style"
CARD_STYLE_NATIVE = "native"
CARD_STYLE_URM = "universal-remote-card"

# 绑定流程是否自动进入仪表盘卡片添加步骤的代码级开关
# 改为 True 后，PIN 验证通过会弹出仪表盘选择页面；False 则直接完成绑定，UI 由用户自行搭建
DEFAULT_AUTO_ADD_DASHBOARD = False

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
MQTT_TOPIC_TEXT_COMMAND = "/remoteapp/tv/homelink_cmd/{device_id}/actions/voice_text"
MQTT_TOPIC_LASTWILL = "/remoteapp/tv/homelink_cmd/{device_id}/actions/lastwill"
MQTT_TOPIC_CALLBACK = "/remoteapp/mobile/{device_id}/#"
MQTT_TOPIC_BROADCAST = "/remoteapp/mobile/broadcast/#"

# ── 应用列表 / 启动应用 Topic 契约 ──
# 注意：这三条 topic 不带 device_id —— MQTT Broker 内嵌在每台电视内部，
# 每条连接天然只对应一台电视，因此"homelink"作为固定段使用。
#   HA → 电视：请求应用列表
MQTT_TOPIC_GETAPPS = "/remoteapp/tv/homelink/actions/getapps"
#   HA → 电视：启动应用（包名或深链）
MQTT_TOPIC_LAUNCHAPP = "/remoteapp/tv/homelink/actions/launchapp"
#   电视 → HA：应用列表（{"apps": [{"name", "package", "icon"}]}）
MQTT_TOPIC_APPS = "/remoteapp/mobile/homelink/apps"
# 实际订阅用通配符：既匹配上面契约 topic，也兼容电视端带 device_id 的变体
MQTT_TOPIC_APPS_WILDCARD = "/remoteapp/mobile/+/apps"

# getapps 请求 payload（空对象，电视端不解析内容）
MQTT_GETAPPS_PAYLOAD = "{}"
# 等待电视回推应用列表：单次超时（秒）与重试次数
APP_LIST_TIMEOUT = 5
APP_LIST_ATTEMPTS = 3

# 应用列表轮询间隔（秒）
# 电视端新增/卸载应用时不会主动推送 apps，HA 侧只能定时补拉一条 getapps；
# 轮询只在 MQTT 已连接时执行，单次只发 1 条请求（不做重试）。
# 列表内容未变化时不会写实体状态、也不会重写仪表盘，因此开销仅是一次往返。
APP_LIST_POLL_INTERVAL = 60

# 应用图标落盘位置：<config>/www/hitvcontrol/apps，前端经 /local/ 引用
APP_ICON_DIR = "hitvcontrol/apps"
APP_ICON_URL_PREFIX = "/local/hitvcontrol/apps"

# 启动应用类型
APP_LAUNCH_TYPE_AUTO = "auto"
APP_LAUNCH_TYPE_PACKAGE = "package"
APP_LAUNCH_TYPE_URI = "uri"

# 遗嘱消息
MQTT_LASTWILL_PAYLOAD = "CLOSE"
MQTT_LASTWILL_QOS = 2

# 服务名称
SERVICE_SEND_COMMAND = "send_command"
SERVICE_SEND_TEXT_COMMAND = "send_text_command"
SERVICE_LAUNCH_APP = "launch_app"
SERVICE_REFRESH_APPS = "refresh_apps"

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
