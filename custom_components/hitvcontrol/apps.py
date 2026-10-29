"""应用列表管理模块.

职责（只做应用列表这一件事，不持有 HA 实体）：
  - 向电视发布 getapps 请求，接收电视回推的 apps 列表并缓存；
  - 把 base64 PNG 图标落盘到 <config>/www/hitvcontrol/apps/，
    供 Lovelace 卡片用 /local/ URL 引用（图标不进实体属性，避免状态体积爆炸）；
  - 应用列表变化时通知订阅者（media_player 实体、仪表盘写入逻辑）；
  - 把"应用名 / 包名 / 深链"统一解析成 launchapp 的 (type, id)。

数据流：
  TVRemote 建连成功 → AppListManager.async_request_apps()
    → publish /remoteapp/tv/homelink/actions/getapps
    → TVMQTTClient 收到 /remoteapp/mobile/+/apps
    → AppListManager.async_handle_message() 解析 + 落盘图标 + 通知订阅者
    → TVMediaPlayer 刷新 source_list / 重写仪表盘应用宫格

刷新策略：
  电视端新增/卸载应用不会主动推送，TVRemote 每 APP_LIST_POLL_INTERVAL 秒
  补发一条 getapps（仅在 MQTT 已连接时）。内容未变化时只更新 last_updated，
  不通知订阅者，因此不会重复写实体状态与仪表盘。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    APP_ICON_DIR,
    APP_ICON_URL_PREFIX,
    APP_LAUNCH_TYPE_AUTO,
    APP_LAUNCH_TYPE_PACKAGE,
    APP_LAUNCH_TYPE_URI,
    APP_LIST_ATTEMPTS,
    APP_LIST_TIMEOUT,
    MQTT_GETAPPS_PAYLOAD,
    MQTT_TOPIC_APPS,
    MQTT_TOPIC_GETAPPS,
    MQTT_TOPIC_LAUNCHAPP,
)

_LOGGER = logging.getLogger(__name__)

# 文件名里只保留这些字符，其余（含 . 与 : 之外的符号）统一替换为下划线
_UNSAFE_FILENAME_CHARS = re.compile(r"[^0-9A-Za-z._-]")

# 电视端可能带 data URL 前缀，也可能直接给裸 base64，两种都接受
_DATA_URL_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/jpg;base64,",
    "data:image/webp;base64,",
)

# 发布通道签名：topic + payload → 交给 TVMQTTClient.publish
Publisher = Callable[[str, str], Awaitable[None]]


def _icon_filename(package: str) -> str:
    """把包名转成安全的图标文件名."""
    return f"{_UNSAFE_FILENAME_CHARS.sub('_', package)}.png"


class AppListManager:
    """单台电视的应用列表缓存与请求调度器."""

    def __init__(self, hass: HomeAssistant, device_id: str) -> None:
        """初始化管理器.

        Args:
            hass: HA 实例，用于线程池写文件和读配置目录。
            device_id: 设备标识，仅用于日志定位（topic 本身不带 device_id）。
        """
        self._hass = hass
        self._device_id = device_id

        # 归一化后的应用列表，元素固定含 name / package，可能含 icon_url
        self._apps: list[dict] = []
        # 应用列表内容签名，用于跳过无变化的重复通知与重复写仪表盘
        self._signature: str | None = None
        # 最近一次收到有效应用列表的时间（供实体属性展示，便于确认轮询在跑）
        self._last_updated: datetime | None = None

        # 本管理器写过的图标文件名。卸载应用后的图标清理只动这些文件，
        # 避免多台电视共用 www/hitvcontrol/apps 时互相删掉对方的图标。
        self._written_icons: set[str] = set()

        # 由 TVRemote 在创建 MQTT 客户端后注入，管理器自身不建连接
        self._publisher: Publisher | None = None

        # 订阅者：应用列表变化时逐个回调（同步、无参）
        self._listeners: list[Callable[[], None]] = []

        # 图标落盘目录：<config>/www/hitvcontrol/apps
        self._icon_dir = Path(hass.config.path("www")) / APP_ICON_DIR

        # 一次 getapps 请求对应一个 Event，电视回推时 set
        self._response_event: asyncio.Event | None = None
        # 串行化请求，避免多处同时刷新时互相等待对方的 Event
        self._request_lock = asyncio.Lock()

    # ────────────────────────── 对外属性 ──────────────────────────

    @property
    def apps(self) -> list[dict]:
        """当前缓存的应表列表（浅拷贝，调用方可安全遍历）."""
        return list(self._apps)

    @property
    def has_apps(self) -> bool:
        """是否已经拿到过应用列表."""
        return bool(self._apps)

    @property
    def last_updated(self) -> datetime | None:
        """最近一次收到有效应用列表的时间（UTC），从未收到过为 None."""
        return self._last_updated

    # ────────────────────────── 发布通道 ──────────────────────────

    def set_publisher(self, publisher: Publisher | None) -> None:
        """注入 MQTT 发布函数（TVMQTTClient.publish），断连时传 None 清空."""
        self._publisher = publisher

    # ────────────────────────── 订阅者 ──────────────────────────

    def register_listener(self, listener: Callable[[], None]) -> None:
        """注册应用列表变化回调."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unregister_listener(self, listener: Callable[[], None]) -> None:
        """注销回调（实体卸载时调用，避免持有已销毁实体）."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify_listeners(self) -> None:
        """逐个通知订阅者；单个订阅者异常不影响其他订阅者."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "[Apps] 应用列表回调执行失败: %s", self._device_id
                )

    # ────────────────────────── 请求 / 下发 ──────────────────────────

    async def async_request_apps(
        self,
        attempts: int = APP_LIST_ATTEMPTS,
        timeout: float = APP_LIST_TIMEOUT,
    ) -> bool:
        """向电视请求应用列表并等待回推.

        每次请求发一条 getapps，等待 timeout 秒；超时则重试，全部失败返回 False。
        返回 True 表示本轮请求收到了电视回推（不代表列表非空——空列表也是有效应答）。

        Args:
            attempts: 最多请求几次。
            timeout: 单次等待回推的秒数。
        """
        if self._publisher is None:
            _LOGGER.warning(
                "[Apps] %s 的 MQTT 通道尚未就绪，跳过本次应用列表请求", self._device_id
            )
            return False

        async with self._request_lock:
            event = asyncio.Event()
            self._response_event = event
            try:
                for attempt in range(1, attempts + 1):
                    _LOGGER.info(
                        "[Apps] 第 %s/%s 次请求应用列表: %s",
                        attempt, attempts, MQTT_TOPIC_GETAPPS,
                    )
                    await self._publisher(MQTT_TOPIC_GETAPPS, MQTT_GETAPPS_PAYLOAD)
                    try:
                        await asyncio.wait_for(event.wait(), timeout=timeout)
                        _LOGGER.info(
                            "[Apps] %s 已回推应用列表，共 %s 个",
                            self._device_id, len(self._apps),
                        )
                        return True
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "[Apps] %s 在 %s 秒内未回推应用列表", self._device_id, timeout
                        )
                return False
            finally:
                self._response_event = None

    async def async_launch_app(
        self,
        target: str,
        launch_type: str = APP_LAUNCH_TYPE_AUTO,
    ) -> bool:
        """向电视下发启动应用指令.

        Args:
            target: 应用名、包名或深链。
            launch_type: auto / package / uri；auto 时按 target 形态自动判别。

        Returns:
            True 表示已交付 MQTT 发布。
        """
        if self._publisher is None:
            _LOGGER.error(
                "[Apps] %s 的 MQTT 通道未就绪，无法启动应用 %s", self._device_id, target
            )
            return False

        resolved_type, resolved_id = self.resolve_launch_target(target, launch_type)
        payload = json.dumps(
            {"type": resolved_type, "id": resolved_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _LOGGER.info(
            "[Apps] 启动应用: topic=%s payload=%s", MQTT_TOPIC_LAUNCHAPP, payload
        )
        await self._publisher(MQTT_TOPIC_LAUNCHAPP, payload)
        return True

    def resolve_launch_target(
        self, target: str, launch_type: str = APP_LAUNCH_TYPE_AUTO
    ) -> tuple[str, str]:
        """把用户输入解析成 launchapp 的 (type, id).

        - 显式指定 package / uri → 按显式值下发（优先级最高）
        - auto 时命中应用名 → 用该应用的包名，type=package
        - auto 且未命中 → 含 "://" 视为深链，否则视为包名
        """
        if launch_type in (APP_LAUNCH_TYPE_PACKAGE, APP_LAUNCH_TYPE_URI):
            return launch_type, target
        app = self.find_app(target)
        if app is not None:
            return APP_LAUNCH_TYPE_PACKAGE, app["package"]
        return (
            APP_LAUNCH_TYPE_URI if "://" in target else APP_LAUNCH_TYPE_PACKAGE,
            target,
        )

    def find_app(self, key: str) -> dict | None:
        """按包名或显示名查找应用."""
        for app in self._apps:
            if app["package"] == key:
                return app
        for app in self._apps:
            if app["name"] == key:
                return app
        return None

    # ────────────────────────── 接收电视回推 ──────────────────────────

    async def async_handle_message(self, topic: str, payload: str) -> bool:
        """处理电视推送的消息；命中应用列表 topic 时刷新缓存.

        Args:
            topic: 消息 topic（由 TVMQTTClient 解出）。
            payload: 消息正文（UTF-8 文本）。

        Returns:
            True 表示这条消息被本模块消费。
        """
        if not self._is_apps_topic(topic):
            return False

        apps = await self._parse_payload(payload)
        if apps is None:
            # 解析失败也算"已消费"，避免上层继续当作未知消息处理
            return True

        signature = self._build_signature(apps)
        unchanged = signature == self._signature
        self._apps = apps
        self._signature = signature
        self._last_updated = dt_util.utcnow()

        # 无论内容是否变化，都要唤醒等待中的请求（否则请求方会白等一轮超时）
        if self._response_event is not None:
            self._response_event.set()

        if unchanged:
            _LOGGER.debug("[Apps] %s 应用列表无变化，跳过通知", self._device_id)
            return True

        _LOGGER.info(
            "[Apps] %s 应用列表已更新，共 %s 个: %s",
            self._device_id,
            len(apps),
            ", ".join(app["name"] for app in apps[:10]) + (" ..." if len(apps) > 10 else ""),
        )
        # 列表变了：清理已卸载应用遗留的图标文件（空列表不动，避免瞬时空白误删）
        if apps:
            await self._async_prune_icons(apps)
        self._notify_listeners()
        return True

    @staticmethod
    def _is_apps_topic(topic: str) -> bool:
        """判断 topic 是否为本契约的应用列表 topic（兼容带 device_id 的变体）."""
        if topic == MQTT_TOPIC_APPS:
            return True
        parts = topic.split("/")
        # /remoteapp/mobile/<任意一段>/apps
        return (
            len(parts) == 5
            and parts[1] == "remoteapp"
            and parts[2] == "mobile"
            and parts[4] == "apps"
        )

    async def _parse_payload(self, payload: str) -> list[dict] | None:
        """解析 {"apps": [...]}，并把图标落盘成可被 /local/ 引用的 URL.

        返回 None 表示 payload 非法（JSON 错误或 apps 不是列表）。
        条目缺少 name 或 package 时丢弃该条并记警告，其余条目继续保留。
        """
        try:
            data = json.loads(payload)
        except (ValueError, TypeError) as err:
            _LOGGER.warning(
                "[Apps] 应用列表 JSON 解析失败 (%s): %.200s", err, payload
            )
            return None

        raw_apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(raw_apps, list):
            _LOGGER.warning("[Apps] 应用列表缺少 apps 数组: %.200s", payload)
            return None

        apps: list[dict] = []
        for raw in raw_apps:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            package = str(raw.get("package") or "").strip()
            if not name or not package:
                _LOGGER.warning("[Apps] 跳过缺少 name/package 的条目: %.120s", raw)
                continue
            apps.append(
                {
                    "name": name,
                    "package": package,
                    "icon_url": await self._async_save_icon(package, raw.get("icon")),
                }
            )
        return apps

    def _build_signature(self, apps: list[dict]) -> str:
        """按 name/package/icon_url 生成内容签名，用于判断列表是否真的变了."""
        digest = hashlib.sha256()
        for app in apps:
            digest.update(
                f"{app['package']}|{app['name']}|{app.get('icon_url') or ''}\n".encode()
            )
        return digest.hexdigest()

    # ────────────────────────── 图标落盘 ──────────────────────────

    async def _async_save_icon(self, package: str, raw_icon: object) -> str | None:
        """把单张图标写入 www 目录（在线程池执行，避免阻塞事件循环）."""
        if not isinstance(raw_icon, str) or not raw_icon.strip():
            return None
        url = await self._hass.async_add_executor_job(
            self._save_icon_sync, package, raw_icon
        )
        if url is not None:
            # 记录本管理器写过的文件，供卸载应用后的定向清理使用
            self._written_icons.add(_icon_filename(package))
        return url

    async def _async_prune_icons(self, apps: list[dict]) -> None:
        """删除已卸载应用遗留的图标文件.

        只处理本管理器写过的文件（见 _written_icons），多台电视共用同一个
        www/hitvcontrol/apps 目录时不会误删其它电视的图标。
        写失败/删失败只记日志，不影响应用列表本身。
        """
        valid = {_icon_filename(app["package"]) for app in apps}
        stale = self._written_icons - valid
        if not stale:
            return
        removed = await self._hass.async_add_executor_job(
            self._prune_icons_sync, stale
        )
        self._written_icons -= stale
        if removed:
            _LOGGER.info(
                "[Apps] %s 已清理 %s 个卸载应用的图标: %s",
                self._device_id, len(removed), ", ".join(sorted(removed)),
            )

    def _prune_icons_sync(self, filenames: set[str]) -> list[str]:
        """在线程池里删除图标文件，返回实际删除的文件名列表."""
        removed: list[str] = []
        for filename in filenames:
            path = self._icon_dir / filename
            try:
                path.unlink()
                removed.append(filename)
            except FileNotFoundError:
                pass
            except OSError as err:
                _LOGGER.warning("[Apps] 删除图标 %s 失败: %s", path, err)
        return removed

    def _save_icon_sync(self, package: str, raw_icon: str) -> str | None:
        """解码 base64 并写入 www 目录，返回带内容哈希的 /local/ URL.

        说明：
          - 写失败只丢图标（卡片回退成 mdi 图标），不影响应用列表本身；
          - URL 带 ?v=<内容哈希>，图标变了浏览器不会继续用旧缓存。
        """
        encoded = raw_icon.strip()
        for prefix in _DATA_URL_PREFIXES:
            if encoded.startswith(prefix):
                encoded = encoded[len(prefix):]
                break
        # 去掉换行/空格（部分端会用 76 列折行的 base64）
        encoded = "".join(encoded.split())

        try:
            icon_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as err:
            _LOGGER.warning("[Apps] %s 的图标 base64 解码失败: %s", package, err)
            return None

        filename = _icon_filename(package)
        path = self._icon_dir / filename
        try:
            self._icon_dir.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_bytes() != icon_bytes:
                path.write_bytes(icon_bytes)
        except OSError as err:
            _LOGGER.warning("[Apps] 写入图标 %s 失败: %s", path, err)
            return None

        version = hashlib.sha256(icon_bytes).hexdigest()[:8]
        return f"{APP_ICON_URL_PREFIX}/{filename}?v={version}"
