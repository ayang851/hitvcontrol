"""TV Control - 通过 MQTT+TLS 直连电视内嵌 Broker 的集成（HomeLink 服务）."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADD_TO_DASHBOARD,
    CONF_DASHBOARD_ID,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_NEW_VIEW_PATH,
    CONF_NEW_VIEW_TITLE,
    CONF_VIEW_PATH,
    DOMAIN,
    KEY_MAP,
    MQTT_PORT,
    MQTT_TOPIC_COMMAND,
    SERVICE_SEND_COMMAND,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["remote", "media_player"]


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

    # 存储配置数据，供 remote 平台创建实体时使用
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_DEVICE_ID: device_id,
        CONF_HOST: host,
        CONF_MQTT_USERNAME: mqtt_username,
        CONF_MQTT_PASSWORD: mqtt_password,
        "port": port,
        "command_topic": command_topic,
    }

    # 注册发送指令的服务
    async def handle_send_command(call) -> None:
        """处理发送指令的服务调用.

        通过查找对应的实体来发送指令，统一出口。
        target 字段同时接受 device_id 和 entity_id 两种格式:
          · 优先按 device_id 精确匹配(后端 MQTT topic 拼接用)
          · 未命中再按 entity_id 匹配(用户从前端 UI 复制粘贴时常用)
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

        # 查找匹配的设备配置，获取实体引用
        entry_data = None
        match_type = None  # 记录匹配类型，便于日志排查
        for data in hass.data[DOMAIN].values():
            if not isinstance(data, dict):
                continue
            if data.get(CONF_DEVICE_ID) == target:
                entry_data = data
                match_type = "device_id"
                break

        # 兜底：按 entity_id 匹配，允许用户直接填前端显示的实体 ID
        if entry_data is None:
            for data in hass.data[DOMAIN].values():
                if not isinstance(data, dict):
                    continue
                entity = data.get("entity")
                if entity is not None and entity.entity_id == target:
                    entry_data = data
                    match_type = "entity_id"
                    break

        if entry_data is None:
            _LOGGER.error(
                "Device %s not found (既不匹配 device_id 也不匹配 entity_id)", target
            )
            return

        entity = entry_data.get("entity")
        if entity is None:
            _LOGGER.error("Entity for device %s not ready", target)
            return

        _LOGGER.info(
            "Sending command to TV %s (matched by %s): %s", target, match_type, payload
        )
        await entity.async_send_command([payload])

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        hass.services.async_register(DOMAIN, SERVICE_SEND_COMMAND, handle_send_command)

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
