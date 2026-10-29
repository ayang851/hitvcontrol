"""TV Control - 通过 MQTT+TLS 直连电视内嵌 Broker 的集成（HomeLink 服务）."""
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .apps import AppListManager
from .const import (
    APP_LAUNCH_TYPE_AUTO,
    APP_LAUNCH_TYPE_PACKAGE,
    APP_LAUNCH_TYPE_URI,
    CONF_ADD_TO_DASHBOARD,
    CONF_APP_ID,
    CONF_DASHBOARD_ID,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LAUNCH_TYPE,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_NEW_VIEW_PATH,
    CONF_NEW_VIEW_TITLE,
    CONF_VIEW_PATH,
    DOMAIN,
    KEY_MAP,
    MQTT_PORT,
    MQTT_TOPIC_COMMAND,
    MQTT_TOPIC_TEXT_COMMAND,
    SERVICE_LAUNCH_APP,
    SERVICE_REFRESH_APPS,
    SERVICE_SEND_COMMAND,
    SERVICE_SEND_TEXT_COMMAND,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["remote", "media_player"]

# 服务参数校验：放在 Python 注册侧才会真正生效（services.yaml 仅是 UI 元数据）
SCHEMA_SEND_COMMAND = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required("command"): cv.string,
    }
)
SCHEMA_SEND_TEXT_COMMAND = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required("text"): cv.string,
    }
)
SCHEMA_LAUNCH_APP = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_APP_ID): cv.string,
        vol.Optional(CONF_LAUNCH_TYPE, default=APP_LAUNCH_TYPE_AUTO): vol.In(
            [APP_LAUNCH_TYPE_AUTO, APP_LAUNCH_TYPE_PACKAGE, APP_LAUNCH_TYPE_URI]
        ),
    }
)
SCHEMA_REFRESH_APPS = vol.Schema({vol.Required(CONF_DEVICE_ID): cv.string})


def _find_entry_data(hass: HomeAssistant, target: str) -> dict | None:
    """按 device_id 或实体 entity_id 查找配置项运行时数据.

    target 三种格式都接受:
      · device_id 精确匹配（后端 MQTT topic 拼接用）
      · remote 实体 entity_id（data["entity"]，传输控按键）
      · media_player 实体 entity_id（data["media_player_entity_id"]，
        启动应用/刷新列表最常用的写法）

    注意：一个配置项会创建两个实体（remote + media_player），
    两个都要能命中，否则用户从状态页复制哪个都可能落空。
    """
    for data in hass.data.get(DOMAIN, {}).values():
        if isinstance(data, dict) and data.get(CONF_DEVICE_ID) == target:
            return data

    for data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(data, dict):
            continue
        # remote 实体（遥控器）
        entity = data.get("entity")
        if entity is not None and entity.entity_id == target:
            return data
        # media_player 实体（应用列表/启动应用）
        if data.get("media_player_entity_id") == target:
            return data
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the TV Control component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TV Control from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    device_id = entry.data[CONF_DEVICE_ID]
    host = entry.data[CONF_HOST]
    mqtt_username = entry.data[CONF_MQTT_USERNAME]
    mqtt_password = entry.data[CONF_MQTT_PASSWORD]
    port = MQTT_PORT  # 始终使用 TLS 端口
    command_topic = MQTT_TOPIC_COMMAND.format(device_id=device_id)
    text_command_topic = MQTT_TOPIC_TEXT_COMMAND.format(device_id=device_id)

    # 存储配置数据，供 remote 平台创建实体时使用
    # app_manager：应用列表缓存与 getapps/launchapp 调度（remote 建连后注入发布通道）
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_DEVICE_ID: device_id,
        CONF_HOST: host,
        CONF_MQTT_USERNAME: mqtt_username,
        CONF_MQTT_PASSWORD: mqtt_password,
        "port": port,
        "command_topic": command_topic,
        "text_command_topic": text_command_topic,
        "app_manager": AppListManager(hass, device_id),
    }

    # 注册发送指令的服务
    async def handle_send_command(call) -> None:
        """处理发送指令的服务调用.

        通过查找对应的实体来发送指令，统一出口。
        target 字段同时接受 device_id 和 entity_id 两种格式，
        具体匹配顺序见 _find_entry_data。
        """
        target = call.data[CONF_DEVICE_ID]
        command = call.data["command"]

        # 按键名映射：卡片中写的常量名 → 实际发送的 payload
        # 例如卡片写 KEY_RETURNS，映射后实际发送 KEY_BACK
        if command in KEY_MAP:
            payload = KEY_MAP[command]
            if payload != command:
                _LOGGER.info("Key mapped: %s -> %s", command, payload)
        else:
            # 不在映射表中的按键，原样发送并记录警告便于排查
            _LOGGER.warning("Unknown key '%s', sending as-is", command)
            payload = command

        entry_data = _find_entry_data(hass, target)
        if entry_data is None:
            # 抛异常而不是静默 return：前端“执行动作”会直接红字提示，
            # 避免用户以为指令发了而电视没反应
            raise HomeAssistantError(
                f"未找到电视 {target}（可填 device_id、remote.xxx 或 media_player.xxx）"
            )

        entity = entry_data.get("entity")
        if entity is None:
            raise HomeAssistantError(f"电视 {target} 的遥控实体尚未就绪")

        _LOGGER.info("Sending command to TV %s: %s", target, payload)
        await entity.async_send_command([payload])

    async def handle_send_text_command(call) -> None:
        """处理发送文本控制指令的服务调用.

        与 send_command 不同：本服务把任意文本直接发布到独立的 text topic，
        不经过 KEY_MAP 映射，专门供电视端自定义解析处理。
        """
        target = call.data[CONF_DEVICE_ID]
        text = call.data["text"]

        entry_data = _find_entry_data(hass, target)
        if entry_data is None:
            raise HomeAssistantError(
                f"未找到电视 {target}（可填 device_id、remote.xxx 或 media_player.xxx）"
            )

        entity = entry_data.get("entity")
        if entity is None:
            raise HomeAssistantError(f"电视 {target} 的遥控实体尚未就绪")

        _LOGGER.info("Sending text command to TV %s: %s", target, text)
        await entity.async_send_text_command(text)

    async def handle_launch_app(call) -> None:
        """处理启动应用的服务调用（app_id 支持应用名/包名/深链）."""
        target = call.data[CONF_DEVICE_ID]
        app_id = call.data[CONF_APP_ID]
        launch_type = call.data.get(CONF_LAUNCH_TYPE, APP_LAUNCH_TYPE_AUTO)

        entry_data = _find_entry_data(hass, target)
        if entry_data is None:
            raise HomeAssistantError(
                f"未找到电视 {target}（可填 device_id、remote.xxx 或 media_player.xxx）"
            )

        manager = entry_data.get("app_manager")
        if manager is None:
            raise HomeAssistantError(f"电视 {target} 的应用管理器尚未就绪")

        _LOGGER.info("Launching app on TV %s: %s (%s)", target, app_id, launch_type)
        if not await manager.async_launch_app(app_id, launch_type):
            raise HomeAssistantError(
                f"启动应用失败：电视 {target} 的 MQTT 通道未就绪（请检查连接状态）"
            )

    async def handle_refresh_apps(call) -> None:
        """处理刷新应用列表的服务调用：重新发 getapps 并等待回推."""
        target = call.data[CONF_DEVICE_ID]

        entry_data = _find_entry_data(hass, target)
        if entry_data is None:
            raise HomeAssistantError(
                f"未找到电视 {target}（可填 device_id、remote.xxx 或 media_player.xxx）"
            )

        manager = entry_data.get("app_manager")
        if manager is None:
            raise HomeAssistantError(f"电视 {target} 的应用管理器尚未就绪")

        if not await manager.async_request_apps():
            _LOGGER.warning("TV %s did not report app list", target)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        hass.services.async_register(
            DOMAIN, SERVICE_SEND_COMMAND, handle_send_command, schema=SCHEMA_SEND_COMMAND
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_TEXT_COMMAND):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_TEXT_COMMAND,
            handle_send_text_command,
            schema=SCHEMA_SEND_TEXT_COMMAND,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_LAUNCH_APP):
        hass.services.async_register(
            DOMAIN, SERVICE_LAUNCH_APP, handle_launch_app, schema=SCHEMA_LAUNCH_APP
        )
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_APPS):
        hass.services.async_register(
            DOMAIN, SERVICE_REFRESH_APPS, handle_refresh_apps, schema=SCHEMA_REFRESH_APPS
        )

    # 转发到平台，创建实体
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """迁移旧版本配置项.

    版本 3 -> 4：增加仪表盘卡片相关字段，旧条目默认不再自动添加卡片，
    避免升级后突然修改用户仪表盘布局。
    """
    if entry.version >= 4:
        return True

    data = {**entry.data}
    if entry.version < 4:
        data.setdefault(CONF_ADD_TO_DASHBOARD, False)
        data.setdefault(CONF_DASHBOARD_ID, None)
        data.setdefault(CONF_VIEW_PATH, None)
        data.setdefault(CONF_NEW_VIEW_TITLE, None)
        data.setdefault(CONF_NEW_VIEW_PATH, None)

    hass.config_entries.async_update_entry(entry, data=data, version=4)
    _LOGGER.info("Migrated hitvcontrol config entry %s to version 4", entry.entry_id)
    return True
