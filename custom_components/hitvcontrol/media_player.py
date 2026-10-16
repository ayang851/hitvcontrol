"""TV Control Media Player platform.

Demo 阶段硬编码一个 media_player 实体，用于在 HA 前端调试"应用列表 +
启动应用"的展示与交互。实体提供：
  - source_list：固定显示 DEMO_APPS 中的应用名（下拉选源）
  - select_source / play_media：模拟启动应用，仅记录日志，不发送 MQTT
  - turn_off：模拟关机，仅记录日志

后续对接电视时，只需把 DEMO_APPS 替换为 MQTT 返回的真实列表，并在
async_play_media 中发布 launchapp 命令即可。
"""
import asyncio
import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ADD_TO_DASHBOARD,
    CONF_DASHBOARD_ID,
    CONF_DEVICE_ID,
    CONF_NEW_VIEW_PATH,
    CONF_NEW_VIEW_TITLE,
    CONF_VIEW_PATH,
    DEMO_APPS,
    DOMAIN,
)
from .dashboard import (
    async_create_apps_dashboard_card,
    async_delete_apps_dashboard_card,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """HA 平台入口：为每个配置项创建一个 TVMediaPlayer 实体."""
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TVMediaPlayer(entry, data)])


class TVMediaPlayer(MediaPlayerEntity):
    """HA MediaPlayerEntity：代表电视的应用列表与媒体控制.

    Demo 阶段能力：
      - 固定 source_list = DEMO_APPS 中的应用名
      - select_source / play_media 仅做日志记录，不实际发命令
      - turn_off 仅做日志记录

    设计约定：
      - _attr_name = None：实体显示名继承设备名（与 remote 实体一致）
      - unique_id 后缀 _media_player：避免与 remote 实体冲突
      - device_info 与 remote 使用同一 (DOMAIN, device_id)，归到同一个设备页
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, entry: ConfigEntry, data: dict) -> None:
        """初始化 TV 媒体播放器实体."""
        self._entry = entry
        self._device_id = data[CONF_DEVICE_ID]

        # 应用名 -> 应用配置（用于 source 切换时查找包名）
        self._app_by_name = {app["name"]: app for app in DEMO_APPS}
        self._app_by_package = {app["package"]: app for app in DEMO_APPS}

        self._attr_unique_id = f"{DOMAIN}_{self._device_id}_media_player"
        self._attr_source_list = [app["name"] for app in DEMO_APPS]
        self._attr_source = None
        self._attr_media_title = None
        self._attr_media_content_id = None
        self._state = STATE_ON

    async def async_added_to_hass(self) -> None:
        """实体注册到 HA 后，异步写入应用宫格卡片.

        等待 5 秒再操作仪表盘，给 remote 实体的遥控器卡片预留创建/保存时间，
        降低两个实体并发读写同一视图的冲突概率（Demo 阶段暂不引入文件锁）。
        """
        self.hass.loop.create_task(self._async_ensure_apps_dashboard_card())

    async def _async_ensure_apps_dashboard_card(self) -> None:
        """按用户在 config_flow 中的选择，为当前电视写入应用宫格 section."""
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        if entry_data.get("apps_dashboard_created"):
            return

        # 等遥控器卡片先落盘，再追加应用宫格
        await asyncio.sleep(5)

        add_to_dashboard = self._entry.data.get(CONF_ADD_TO_DASHBOARD, False)
        if not add_to_dashboard:
            _LOGGER.info(
                "[Dashboard] 用户选择不添加 %s 的应用卡片", self.entity_id
            )
            entry_data["apps_dashboard_created"] = True
            return

        dashboard_id = self._entry.data.get(CONF_DASHBOARD_ID)
        view_path = self._entry.data.get(CONF_VIEW_PATH)
        new_view_title = self._entry.data.get(CONF_NEW_VIEW_TITLE)
        new_view_path = self._entry.data.get(CONF_NEW_VIEW_PATH)
        device_name = self._entry.title or "电视"

        for attempt in range(3):
            _LOGGER.info(
                "[Dashboard] 第 %s 次为 %s 创建应用卡片", attempt + 1, self.entity_id
            )
            success = await async_create_apps_dashboard_card(
                self.hass,
                self._entry,
                self.entity_id,
                DEMO_APPS,
                device_name,
                dashboard_id=dashboard_id,
                view_path=view_path,
                new_view_title=new_view_title,
                new_view_path=new_view_path,
            )
            if success:
                entry_data["apps_dashboard_created"] = True
                return
            _LOGGER.warning("[Dashboard] 本次创建应用卡片失败，10 秒后重试")
            await asyncio.sleep(10)

        _LOGGER.warning(
            "[Dashboard] 为 %s 创建应用卡片最终失败", self.entity_id
        )

    async def async_will_remove_from_hass(self) -> None:
        """实体移除时清理自动生成的应用宫格 section."""
        try:
            await async_delete_apps_dashboard_card(self.hass, self.entity_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[Dashboard] 清理 %s 的应用卡片失败: %s", self.entity_id, exc
            )

    @property
    def state(self) -> str:
        """返回当前状态：Demo 阶段主要用 ON/OFF."""
        return self._state

    @property
    def device_info(self) -> dict:
        """与 remote 实体归属同一设备."""
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._entry.title,
            "manufacturer": "TV Control",
            "model": "Smart TV",
        }

    async def async_select_source(self, source: str) -> None:
        """用户从 source_list 下拉选择应用时触发."""
        _LOGGER.info(
            "[MediaPlayer] %s select_source: %s", self.entity_id, source
        )
        if source not in self._app_by_name:
            _LOGGER.warning("[MediaPlayer] 未知应用: %s", source)
            return

        app = self._app_by_name[source]
        self._attr_source = app["name"]
        self._attr_media_title = app["name"]
        self.async_write_ha_state()

        # Demo 阶段：select_source 等价于 play_media(app, package)
        await self.async_play_media("app", app["package"])

    async def async_play_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
        enqueue: str | None = None,
        announce: bool | None = None,
        **kwargs,
    ) -> None:
        """HA media_player.play_media 动作入口.

        Demo 阶段只根据 package/name 匹配 DEMO_APPS，记录日志，不发送 MQTT。
        后续对接电视时，在此发布 launchapp topic。

        注意：
          HA 服务层应当始终传入 media_content_type 与 media_content_id。
          这里把两个参数改成可空并加防御日志，方便排查前端/卡片调用时参数
          未正确传递的问题。
        """
        _LOGGER.info(
            "[MediaPlayer] %s play_media: type=%s id=%s enqueue=%s announce=%s kwargs=%s",
            self.entity_id,
            media_content_type,
            media_content_id,
            enqueue,
            announce,
            kwargs,
        )

        if not media_content_type or not media_content_id:
            _LOGGER.error(
                "[MediaPlayer] %s play_media 缺少必要参数: "
                "media_content_type=%s, media_content_id=%s。"
                "请检查调用方（卡片/服务）是否传入了这两个字段。",
                self.entity_id,
                media_content_type,
                media_content_id,
            )
            return

        app = self._app_by_package.get(media_content_id) or self._app_by_name.get(
            media_content_id
        )
        if app is None:
            _LOGGER.warning(
                "[MediaPlayer] 无法匹配 Demo 应用: %s", media_content_id
            )
            return

        self._attr_source = app["name"]
        self._attr_media_title = app["name"]
        self._attr_media_content_id = app["package"]
        self._state = STATE_ON
        self.async_write_ha_state()

        # Demo：仅打印日志，后续替换为 MQTT launchapp 发布
        _LOGGER.info(
            "[MediaPlayer] Demo 启动应用 '%s' (package=%s)，尚未发送 MQTT",
            app["name"],
            app["package"],
        )

    async def async_turn_off(self) -> None:
        """关机动作：Demo 阶段仅记录日志."""
        _LOGGER.info("[MediaPlayer] %s turn_off (demo, no MQTT)", self.entity_id)
        self._state = STATE_OFF
        self.async_write_ha_state()
