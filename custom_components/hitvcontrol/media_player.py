"""TV Control Media Player platform.

媒体播放器实体：承载电视的应用列表与应用启动。

能力：
  - source_list：来自电视端 MQTT 回推的真实应用列表（getapps → apps）
  - select_source / play_media：发布 launchapp 指令启动应用（包名或深链）
  - turn_off：仅记录日志（关机仍走 remote 实体的 KEY_POWER）
  - 应用列表到手/变化后，重写仪表盘里的应用宫格 section（图标用 /local/ 图片卡）

数据来源说明：
  应用列表由 AppListManager 统一缓存（与 remote 实体共用同一条 MQTT 连接），
  本实体只订阅它的变化回调，不自己建连接、不自己解析 JSON。
"""
import asyncio
import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .apps import AppListManager
from .const import (
    APP_LAUNCH_TYPE_AUTO,
    APP_LAUNCH_TYPE_URI,
    APP_LIST_TIMEOUT,
    CONF_ADD_TO_DASHBOARD,
    CONF_DASHBOARD_ID,
    CONF_DEVICE_ID,
    CONF_NEW_VIEW_PATH,
    CONF_NEW_VIEW_TITLE,
    CONF_VIEW_PATH,
    DOMAIN,
    KEY_MUTE,
    KEY_POWER,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
)
from .dashboard import (
    async_create_apps_dashboard_card,
    async_delete_apps_dashboard_card,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
) -> None:
    """HA 平台入口：为每个配置项创建一个 TVMediaPlayer 实体."""
    data = hass.data[DOMAIN][entry.entry_id]
    add_entities([TVMediaPlayer(entry, data)])


class TVMediaPlayer(MediaPlayerEntity):
    """HA MediaPlayerEntity：代表电视的应用列表与媒体控制.

    设计约定：
      - _attr_name = None：实体显示名继承设备名（与 remote 实体一致）
      - unique_id 后缀 _media_player：避免与 remote 实体冲突
      - device_info 与 remote 使用同一 (DOMAIN, device_id)，归到同一个设备页
      - 应用列表只存 name/package/icon_url，base64 图标留在 www 目录，
        避免实体状态超过 HA 的 16KB 属性上限
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY_MEDIA
        # 音量：电视没有可读写音量值的 API，只能按键步进/静音，
        # 因此只声明 STEP + MUTE，不声明 VOLUME_SET（“音量调到 30%”会匹配失败）。
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, entry: ConfigEntry, data: dict) -> None:
        """初始化 TV 媒体播放器实体."""
        self._entry = entry
        self._device_id = data[CONF_DEVICE_ID]
        # 应用列表管理器由 __init__.py 创建，可能为 None（异常兜底）
        self._app_manager: AppListManager | None = data.get("app_manager")

        self._attr_unique_id = f"{DOMAIN}_{self._device_id}_media_player"
        self._attr_source_list = self._current_source_list()
        self._attr_source = None
        self._attr_media_title = None
        self._attr_media_content_id = None
        self._state = STATE_ON

        # 最近一次写入仪表盘时用的应用签名，用于跳过无变化的重复写入
        self._apps_dashboard_signature: str | None = None
        # 上一次已知的包名集合：用于在日志里明确指出新增/卸载了哪些应用
        # （应用列表靠 remote 实体的 60 秒轮询刷新，日志是排障第一入口）
        self._known_packages: set[str] = set()

    # ────────────────────────── 生命周期 ──────────────────────────

    async def async_added_to_hass(self) -> None:
        """实体注册到 HA 后：订阅应用列表变化，并异步写入应用宫格卡片.

        仪表盘写入等 5 秒再操作，给 remote 实体的遥控器卡片预留创建/保存时间，
        降低两个实体并发读写同一视图的冲突概率（暂不引入文件锁）。
        """
        if self._app_manager is not None:
            self._app_manager.register_listener(self._handle_apps_updated)
        # 登记本实体的 entity_id，供服务按 entity_id 反查配置项：
        # __init__._find_entry_data 会看 device_id / remote entity_id /
        # 这个 media_player entity_id，三者任填一个都能命中。
        self.hass.data[DOMAIN][self._entry.entry_id][
            "media_player_entity_id"
        ] = self.entity_id
        self.hass.loop.create_task(self._async_ensure_apps_dashboard_card())

    async def async_will_remove_from_hass(self) -> None:
        """实体移除时注销回调并清理自动生成的应用宫格 section."""
        if self._app_manager is not None:
            self._app_manager.unregister_listener(self._handle_apps_updated)
        # 清理登记的 entity_id，避免卸载后服务仍按旧 entity_id 命中
        self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).pop(
            "media_player_entity_id", None
        )
        try:
            await async_delete_apps_dashboard_card(self.hass, self.entity_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[Dashboard] 清理 %s 的应用卡片失败: %s", self.entity_id, exc
            )

    # ────────────────────────── 应用列表 ──────────────────────────

    def _current_apps(self) -> list[dict]:
        """当前应用列表（无管理器时返回空列表）."""
        return self._app_manager.apps if self._app_manager is not None else []

    def _current_source_list(self) -> list[str]:
        """当前应用名列表，供 media_player 的 source 下拉展示."""
        return [app["name"] for app in self._current_apps()]

    def _apps_signature(self) -> str:
        """应用列表签名：包名 + 图标 URL，用于判断是否需要重写仪表盘."""
        return "|".join(
            f"{app['package']}:{app.get('icon_url') or ''}"
            for app in self._current_apps()
        )

    @callback
    def _handle_apps_updated(self) -> None:
        """AppListManager 通知应用列表变化时的回调（在事件循环中同步执行）.

        这里是应用增删的唯一汇聚点：更新 source_list + 实体状态，
        再把仪表盘宫格重写一次。Lovelace 存储的 async_save 会广播
        lovelace_updated，前端收到后自动重拉配置，因此不需要手动刷新页面。
        """
        apps = self._current_apps()
        packages = {app["package"] for app in apps}
        added = packages - self._known_packages
        removed = self._known_packages - packages
        # 首次上报时 _known_packages 为空，不算"新增全部"，不重复记日志
        if self._known_packages and (added or removed):
            _LOGGER.info(
                "[MediaPlayer] %s 应用变化: 共 %s 个，新增 [%s]，移除 [%s]",
                self.entity_id,
                len(apps),
                ", ".join(sorted(added)),
                ", ".join(sorted(removed)),
            )
        self._known_packages = packages

        self._attr_source_list = self._current_source_list()
        self.async_write_ha_state()
        if self._entry.data.get(CONF_ADD_TO_DASHBOARD, False):
            self.hass.async_create_task(self._async_refresh_apps_dashboard())

    @property
    def extra_state_attributes(self) -> dict:
        """把应用清单暴露给前端/模板（不含图标数据，避免状态体积过大）."""
        apps = self._current_apps()
        last_updated = (
            self._app_manager.last_updated
            if self._app_manager is not None
            else None
        )
        return {
            "app_count": len(apps),
            # 最近一次收到电视回推的时间（UTC）：轮询正常时会每分钟刷新，
            # 长时间不变说明电视没回推或 MQTT 断了
            "apps_updated_at": (
                last_updated.isoformat(timespec="seconds") if last_updated else None
            ),
            "apps": [
                {"name": app["name"], "package": app["package"]} for app in apps
            ],
        }

    # ────────────────────────── 仪表盘应用宫格 ──────────────────────────

    async def _async_ensure_apps_dashboard_card(self) -> None:
        """首次加载时为当前电视写入应用宫格 section."""
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        if entry_data.get("apps_dashboard_created"):
            return

        # 等遥控器卡片先落盘，再追加应用宫格
        await asyncio.sleep(5)

        if not self._entry.data.get(CONF_ADD_TO_DASHBOARD, False):
            _LOGGER.info(
                "[Dashboard] 用户选择不添加 %s 的应用卡片", self.entity_id
            )
            entry_data["apps_dashboard_created"] = True
            return

        # 应用列表通常晚于实体创建（要等 MQTT 建连 + getapps 往返），
        # 先主动催一次，避免仪表盘写出一个空宫格
        if self._app_manager is not None and not self._app_manager.has_apps:
            await self._app_manager.async_request_apps(
                attempts=1, timeout=APP_LIST_TIMEOUT
            )

        for attempt in range(3):
            _LOGGER.info(
                "[Dashboard] 第 %s 次为 %s 创建应用卡片", attempt + 1, self.entity_id
            )
            if await self._async_write_apps_section():
                entry_data["apps_dashboard_created"] = True
                return
            _LOGGER.warning("[Dashboard] 本次创建应用卡片失败，10 秒后重试")
            await asyncio.sleep(10)

        _LOGGER.warning(
            "[Dashboard] 为 %s 创建应用卡片最终失败", self.entity_id
        )

    async def _async_refresh_apps_dashboard(self) -> None:
        """应用列表变化后重写宫格（内容未变则跳过）."""
        if self._apps_signature() == self._apps_dashboard_signature:
            return
        _LOGGER.info(
            "[Dashboard] 应用列表变化，重写 %s 的应用卡片", self.entity_id
        )
        if not await self._async_write_apps_section():
            _LOGGER.warning(
                "[Dashboard] 重写 %s 的应用卡片失败，下次列表变化再试", self.entity_id
            )

    async def _async_write_apps_section(self) -> bool:
        """调用 dashboard 模块写入 section，成功时记录签名."""
        entry_data = self._entry.data
        success = await async_create_apps_dashboard_card(
            self.hass,
            self._entry,
            self.entity_id,
            self._current_apps(),
            self._entry.title or "电视",
            dashboard_id=entry_data.get(CONF_DASHBOARD_ID),
            view_path=entry_data.get(CONF_VIEW_PATH),
            new_view_title=entry_data.get(CONF_NEW_VIEW_TITLE),
            new_view_path=entry_data.get(CONF_NEW_VIEW_PATH),
        )
        if success:
            self._apps_dashboard_signature = self._apps_signature()
        return success

    # ────────────────────────── 实体属性 ──────────────────────────

    @property
    def state(self) -> str:
        """返回当前状态：主要用 ON/OFF."""
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

    # ────────────────────────── 动作 ──────────────────────────

    async def async_select_source(self, source: str) -> None:
        """用户从 source_list 下拉选择应用时触发：直接启动该应用."""
        _LOGGER.info(
            "[MediaPlayer] %s select_source: %s", self.entity_id, source
        )
        if self._app_manager is None:
            _LOGGER.error(
                "[MediaPlayer] %s 的应用列表管理器未就绪，无法启动 %s",
                self.entity_id, source,
            )
            return
        if self._app_manager.find_app(source) is None:
            # 不在列表里也照发：可能是用户手工填的包名，或列表尚未上报
            _LOGGER.warning(
                "[MediaPlayer] 应用列表中没有名为 %s 的应用，按包名/深链尝试启动",
                source,
            )
        await self.async_play_media("app", source)

    async def async_play_media(
        self,
        media_type: str | None = None,
        media_id: str | None = None,
        enqueue: str | None = None,
        announce: bool | None = None,
        **kwargs,
    ) -> None:
        """HA media_player.play_media 动作入口：发布 launchapp 指令.

        media_type 约定：
          - "app" / "package" / 空：把 media_id 当作应用名或包名
          - "url" / "uri"：把 media_id 当作深链（type=uri）

        参数命名兼容说明（重要，已实测）：
          media_player.play_media 的服务 schema 只接受
          media_content_type / media_content_id（在卡片 data 里写
          media_type / media_id 会被校验拒绝：not a valid option）。
          但核心在转调实体时，会用关键字参数 media_type / media_id 调用本方法，
          旧字段的值被映射到新形参名上。
          所以：形参必须用新名（否则两个值会全部落进 **kwargs、形参恒为 None，
          表现为日志 "type=None id=None"），同时在 kwargs / extra 里对
          旧名兜底，兼容直接以旧名调用实体的路径。

        Args:
            media_type: 内容类型，决定 launchapp 的 type 字段。
            media_id: 应用名、包名或深链。
            enqueue / announce / kwargs: HA 框架预留参数，未使用。
        """
        # 旧命名兜底（HA 2024.10 之前、或存量卡片仍写 media_content_*）
        media_type = media_type or kwargs.pop("media_content_type", None)
        media_id = media_id or kwargs.pop("media_content_id", None)
        # 前端有时把未识别字段统一下沉到 extra
        extra = kwargs.pop("extra", None) or {}
        if isinstance(extra, dict):
            media_type = media_type or extra.get("media_type") or extra.get(
                "media_content_type"
            )
            media_id = media_id or extra.get("media_id") or extra.get(
                "media_content_id"
            )

        _LOGGER.info(
            "[MediaPlayer] %s play_media: type=%s id=%s enqueue=%s announce=%s raw=%s",
            self.entity_id,
            media_type,
            media_id,
            enqueue,
            announce,
            kwargs,
        )

        if not media_id:
            _LOGGER.error(
                "[MediaPlayer] %s play_media 缺少应用标识（media_id / media_content_id），"
                "调用方需传入应用名/包名/深链；收到的参数: %s",
                self.entity_id,
                kwargs,
            )
            return

        if self._app_manager is None:
            _LOGGER.error(
                "[MediaPlayer] %s 的应用列表管理器未就绪，无法启动 %s",
                self.entity_id,
                media_id,
            )
            return

        content_type = (media_type or "").lower()
        launch_type = (
            APP_LAUNCH_TYPE_URI
            if content_type in ("url", "uri", "channel")
            else APP_LAUNCH_TYPE_AUTO
        )

        app = self._app_manager.find_app(media_id)
        launched = await self._app_manager.async_launch_app(media_id, launch_type)
        if not launched:
            return

        # 命中应用列表时同步刷新实体展示；未命中（如深链）只改内容 ID
        if app is not None:
            self._attr_source = app["name"]
            self._attr_media_title = app["name"]
            self._attr_media_content_id = app["package"]
        else:
            self._attr_media_content_id = media_id
        self._state = STATE_ON
        self.async_write_ha_state()

    # ────────────────────────── 按键通道（音量 / 电源） ──────────────────────────

    def _remote_entity(self):
        """取同设备的遥控器实体，作为发按键的通道.

        entry_data["entity"] 是 remote 平台的专用槽位，media_player 只读不写，
        避免覆盖遥控器引用（否则 send_command 服务会失效）。
        """
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("entity")
        )

    async def _async_send_key(self, key: str) -> bool:
        """借遥控器实体发一个按键，返回是否已交付 MQTT."""
        remote = self._remote_entity()
        if remote is None:
            _LOGGER.error(
                "[MediaPlayer] %s 的遥控实体未就绪，无法发送按键 %s",
                self.entity_id, key,
            )
            return False
        _LOGGER.info("[MediaPlayer] %s 发送按键 %s", self.entity_id, key)
        await remote.async_send_command([key])
        return True

    # ────────────────────────── 音量 / 开关机 ──────────────────────────

    async def async_volume_up(self) -> None:
        """音量+."""
        await self._async_send_key(KEY_VOLUMEUP)

    async def async_volume_down(self) -> None:
        """音量-."""
        await self._async_send_key(KEY_VOLUMEDOWN)

    async def async_mute_volume(self, mute: bool) -> None:
        """静音/取消静音.

        电视侧没有独立的开/关静音指令，KEY_MUTE 是切换语义，
        因此 mute=True/False 都发同一个键，仅本地记录预期状态。
        """
        if await self._async_send_key(KEY_MUTE):
            self._attr_is_volume_muted = mute
            self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """关机：发 KEY_POWER（电视靠这颗键做开关机切换）."""
        _LOGGER.info("[MediaPlayer] %s turn_off 发送 KEY_POWER", self.entity_id)
        if not await self._async_send_key(KEY_POWER):
            return
        self._state = STATE_OFF
        self.async_write_ha_state()
