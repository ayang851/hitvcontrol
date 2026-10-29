"""TV Control Remote platform.

本模块实现了 hitvcontrol 集成的遥控器平台（Remote Platform）。
主要功能：
  - TVMQTTClient：封装与电视内嵌 MQTT Broker 的 mutual TLS 加密连接，
    负责 TLS 上下文构建、连接建立、Topic 订阅、遗嘱消息发布及断连检测。
  - TVRemote：HA RemoteEntity 实体，对接 TVMQTTClient，将连接状态映射
    为实体 available 属性，并对外提供 send_command 服务接口。

连接状态流转：
  初始 offline → async_added_to_hass 触发后台连接 → 连接成功 online
  → 消息监听器检测到断连 → offline → 周期重连直到恢复 online
"""
import asyncio
import logging
import socket
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from aiomqtt import Client, ProtocolVersion, Will
from homeassistant.components.remote import RemoteEntity, RemoteEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    APP_LIST_POLL_INTERVAL,
    APP_LIST_TIMEOUT,
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
    KEY_POWER,
    MQTT_CONNECT_TIMEOUT,
    MQTT_KEEPALIVE,
    MQTT_LASTWILL_PAYLOAD,
    MQTT_LASTWILL_QOS,
    MQTT_PORT,
    MQTT_PUBLISH_TIMEOUT,
    MQTT_RECONNECT_INTERVAL,
    MQTT_TLS_CA_FILENAME,
    MQTT_TLS_CERT_FILENAME,
    MQTT_TLS_KEY_FILENAME,
    MQTT_TOPIC_APPS_WILDCARD,
    MQTT_TOPIC_BROADCAST,
    MQTT_TOPIC_CALLBACK,
    MQTT_TOPIC_LASTWILL,
)
from .apps import AppListManager
from .dashboard import async_create_remote_dashboard_card, async_delete_remote_dashboard_card

_LOGGER = logging.getLogger(__name__)

# 组件目录的绝对路径，用于定位证书文件（ca.pem / client.pem / client.key）
_COMPONENT_DIR = Path(__file__).resolve().parent

# MQTT 收发日志里 payload 的截断长度。
# 按键/启动应用这类小包会完整打印；app 列表（带 base64 图标）可达数百 KB，
# 只保留开头一小段+总长度，否则会把日志文件写爆。
_LOG_PAYLOAD_LIMIT = 500


def _truncate_for_log(payload: str) -> str:
    """把 payload 截断到日志可接受的长度，并标明被截掉的字数."""
    if len(payload) <= _LOG_PAYLOAD_LIMIT:
        return payload
    return (
        f"{payload[:_LOG_PAYLOAD_LIMIT]}...(+{len(payload) - _LOG_PAYLOAD_LIMIT} chars)"
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """HA 平台入口：为每个配置项创建一个 TVRemote 实体.

    HA 在加载 remote 平台时调用此函数，从 hass.data 取出已由
    config_flow 写入的配置数据，构造 TVRemote 并注册到 HA 实体注册表。
    """
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TVRemote(entry, data)])


class TVMQTTClient:
    """管理与电视内嵌 MQTT Broker 的 TLS 加密连接.

    职责边界：
      - 只负责 MQTT 层面的连接管理（建立 / 断开 / 重发）。
      - 不持有任何 HA 状态；连接断开时通过 on_disconnected 回调通知上层。
      - 消息监听器（_listen_messages）作为后台 Task 持续运行，
        当 aiomqtt 内部迭代器因连接异常退出时，视为断连事件。

    mutual TLS 说明：
      电视侧 MQTT Broker 使用自签 CA 颁发的服务端证书，且监听 IP 地址而非域名，
      因此 SSLContext 必须关闭 hostname 校验（check_hostname=False），
      同时仍保留 CERT_REQUIRED 对服务端证书做 CA 链校验，防止中间人攻击。
    """

    def __init__(
        self,
        host: str,
        port: int,
        device_id: str,
        mqtt_username: str,
        mqtt_password: str,
        on_disconnected: Callable[[], None] | None = None,
        on_message: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        """初始化 MQTT 客户端.

        Args:
            host: 电视 IP 地址（用于 TCP 连接和日志）。
            port: MQTT Broker 监听端口（TLS 端口通常为 8883）。
            device_id: 设备 ID，同时作为 MQTT clientId 和 Topic 占位符。
            mqtt_username: MQTT 认证用户名（由 PIN 码派生）。
            mqtt_password: MQTT 认证密码（由 PIN 码派生）。
            on_disconnected: 连接异常断开时的回调，在 HA 事件循环线程调用。
            on_message: 收到订阅消息时的异步回调 (topic, payload)，
                在监听器 Task 中 await；回调异常只记日志，不影响连接。
        """
        self._host = host
        self._port = port
        self._device_id = device_id  # clientId 即 device_id，同时用于格式化 Topic
        self._mqtt_username = mqtt_username
        self._mqtt_password = mqtt_password
        self._on_disconnected = on_disconnected
        self._on_message = on_message
        # aiomqtt Client 实例，None 表示当前未连接
        self._client: Client | None = None
        # 消息监听后台 Task，用于检测断连；None 表示未启动
        self._listener_task: asyncio.Task | None = None

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """构建 mutual TLS 的 SSLContext.

        配置说明：
          - PROTOCOL_TLS_CLIENT：启用 TLS 客户端模式，默认开启 hostname 校验。
          - check_hostname=False：关闭主机名校验。电视侧证书的 CN/SAN 是 IP 地址
            而非域名，Python ssl 默认不接受 IP 作为 hostname，必须关闭。
          - verify_mode=CERT_REQUIRED：仍验证服务端证书的 CA 链，确保连接到合法电视。
          - load_verify_locations：加载自签 CA 证书，用于验证服务端证书。
          - load_cert_chain：加载客户端证书 + 私钥，供电视侧验证 HA 身份（mutual TLS）。

        注意：
          此方法包含同步阻塞 I/O（PEM 文件读取），必须通过
          loop.run_in_executor 在线程池中调用，避免阻塞 HA 事件循环。
        """
        ca_path = _COMPONENT_DIR / MQTT_TLS_CA_FILENAME
        cert_path = _COMPONENT_DIR / MQTT_TLS_CERT_FILENAME
        key_path = _COMPONENT_DIR / MQTT_TLS_KEY_FILENAME

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 关闭 hostname 校验（自签证书 CN 为 IP，Python ssl 默认不支持）
        ctx.check_hostname = False
        # 仍要求对服务端证书做 CA 链校验，保证连接到合法设备
        ctx.verify_mode = ssl.CERT_REQUIRED
        # 加载自签 CA，用于验证服务端证书
        ctx.load_verify_locations(cafile=str(ca_path))
        # 加载客户端证书和私钥，用于服务端验证 HA 客户端身份
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx

    def _build_connect_params(self, tls_context: ssl.SSLContext) -> dict:
        """构建传给 aiomqtt Client 的连接参数字典.

        遗嘱消息（LWT）设计：
          连接时向 lastwill_topic 发布 "OPEN"，若连接意外断开，
          Broker 会自动向该 topic 发布 MQTT_LASTWILL_PAYLOAD（通常为 "CLOSE"），
          让电视侧感知到 HA 已离线，避免电视持续等待 HA 响应。

        协议版本固定为 MQTT 3.1（ProtocolVersion.V31），与电视侧 Broker 保持一致。
        """
        # 构造遗嘱消息，topic 以 device_id 区分不同设备
        lastwill_topic = MQTT_TOPIC_LASTWILL.format(device_id=self._device_id)
        will = Will(topic=lastwill_topic, payload=MQTT_LASTWILL_PAYLOAD, qos=MQTT_LASTWILL_QOS)

        kwargs: dict = {
            "hostname": self._host,
            "port": self._port,
            "username": self._mqtt_username,
            "password": self._mqtt_password,
            # keepalive：心跳间隔（秒），超时未收到 PINGRESP 则断连
            "keepalive": MQTT_KEEPALIVE,
            # 遗嘱消息：HA 意外离线时由 Broker 代为发布
            "will": will,
            # 协议版本：与电视 Broker 约定使用 MQTT 3.1
            "protocol": ProtocolVersion.V31,
            # 连接超时：包含 TLS 握手 + CONNACK 等待，超时后抛异常
            "timeout": MQTT_CONNECT_TIMEOUT,
            # TLS 上下文：已在线程池中构建好，直接传入
            "tls_context": tls_context,
        }

        return kwargs

    async def connect(self) -> bool:
        """连接到电视 MQTT Broker.

        执行步骤：
          1. 停止旧监听器、销毁旧客户端（幂等，保证每次都是干净连接）。
          2. 在线程池中构建 SSLContext（避免阻塞事件循环）。
          3. 创建 aiomqtt Client 并进入异步上下文（触发 TCP + TLS + MQTT 握手）。
          4. 连接成功后订阅 callback_topic 和 broadcast_topic。
          5. 向 lastwill_topic 发布 "OPEN" 通知电视侧 HA 已上线。
          6. 启动后台消息监听器，持续接收消息并检测断连。

        Returns:
            True 表示连接成功并已启动监听器，False 表示连接失败。
        """
        # 清理旧连接，防止重复连接时资源泄漏
        self._stop_listener()
        await self._destroy_client()

        try:
            # SSL 证书文件加载是同步阻塞 I/O，放到线程池执行，
            # 避免在高并发时阻塞 HA 异步事件循环
            loop = asyncio.get_running_loop()
            tls_context = await loop.run_in_executor(None, self._build_ssl_context)
            params = self._build_connect_params(tls_context)
            # 创建 aiomqtt Client 并手动进入异步上下文
            # __aenter__ 内部完成 TCP 连接 + TLS 握手 + MQTT CONNECT/CONNACK 交互
            self._client = Client(**params)
            await self._client.__aenter__()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to connect to TV MQTT broker at %s:%s: %s",
                self._host, self._port, err,
            )
            # 连接失败时清理残留对象，确保 self._client 回到 None 状态
            await self._destroy_client()
            return False

        # ── 连接成功，执行后续初始化 ──

        # 订阅 callback_topic（接收按键响应）和 broadcast_topic（接收广播通知）
        # 另加应用列表通配符 topic：既命中契约 topic
        # /remoteapp/mobile/homelink/apps，也兼容带 device_id 的变体
        # 订阅失败不影响连接本身，仅记录警告
        try:
            callback_topic = MQTT_TOPIC_CALLBACK.format(device_id=self._device_id)
            broadcast_topic = MQTT_TOPIC_BROADCAST.format(device_id=self._device_id)
            await self._client.subscribe((callback_topic, 0))
            await self._client.subscribe((broadcast_topic, 0))
            await self._client.subscribe((MQTT_TOPIC_APPS_WILDCARD, 0))
            _LOGGER.info(
                "Subscribed to callback, broadcast and apps topics for %s",
                self._device_id,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to subscribe topics: %s", err)

        # 向遗嘱 topic 发布 "OPEN"，通知电视侧 HA 已上线
        # 若此后连接意外断开，Broker 会自动向同一 topic 发布遗嘱 payload（"CLOSE"）
        try:
            lastwill_topic = MQTT_TOPIC_LASTWILL.format(device_id=self._device_id)
            # QoS=1：与按键命令一致。不要用 QoS=2 —— 它需要 Broker 回复
            # PUBREC/PUBCOMP，而电视端内嵌 Broker 对该握手响应缺失，
            # 曾导致此处阻塞 30 秒才超时，实体迟迟无法标记上线。
            # 外层再套 MQTT_PUBLISH_TIMEOUT 短超时兜底：即使 Broker
            # 迟迟不回 PUBACK，也快速放弃（最多丢一条上线通知，无碍）。
            await asyncio.wait_for(
                self._client.publish(lastwill_topic, "OPEN", qos=1),
                timeout=MQTT_PUBLISH_TIMEOUT,
            )
            _LOGGER.info("Published connection message for %s", self._device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to publish connection message: %s", err)

        # 启动后台消息监听器：持续阻塞等待消息，迭代器异常退出即视为断连
        self._start_listener()

        return True

    def _start_listener(self) -> None:
        """启动消息监听器后台 Task.

        先停止旧 Task（幂等），再创建新 Task。
        使用 get_event_loop().create_task 而非 asyncio.ensure_future，
        语义更明确，且与 HA 异步模型兼容。
        """
        self._stop_listener()
        self._listener_task = asyncio.get_event_loop().create_task(self._listen_messages())
        _LOGGER.debug("Message listener started for %s", self._device_id)

    def _stop_listener(self) -> None:
        """停止消息监听器后台 Task.

        通过 cancel() 触发 CancelledError，_listen_messages 中捕获后静默退出。
        done() 检查防止对已结束的 Task 重复 cancel。
        """
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            self._listener_task = None

    async def _listen_messages(self) -> None:
        """后台消息监听协程，作为断连检测的核心机制.

        工作原理：
          aiomqtt 的 client.messages 是一个异步迭代器，只要 MQTT 连接正常，
          它会持续阻塞等待新消息。当底层 TCP 连接断开（网络故障、电视重启等），
          迭代器会抛出异常（通常是 MqttError 或 OSError），协程进入 except 分支，
          从而实现无轮询的断连检测。

        异常分类处理：
          - CancelledError：由 _stop_listener 主动取消（如重连前清理、实体卸载），
            属于正常流程，直接返回，不触发断连回调。
          - 其他 Exception：网络层或协议层异常，视为连接意外断开，
            销毁客户端并通知上层（TVRemote）更新为离线状态。

        注意：
          消息迭代器仍承担断连检测职责；收到的消息交给 on_message 回调
          （当前用于应用列表回推），回调内的异常只记日志，不触发断连。
        """
        try:
            async for message in self._client.messages:
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            # 主动取消（重连前清理 / 实体卸载），不触发断连回调，直接返回
            _LOGGER.debug("Message listener cancelled for %s", self._device_id)
        except Exception as err:  # noqa: BLE001
            # 迭代器异常退出 = 连接意外断开，记录日志并通知上层
            _LOGGER.warning(
                "MQTT connection lost for %s at %s:%s: %s",
                self._device_id, self._host, self._port, err,
            )
            # 清理已失效的客户端对象，将 self._client 重置为 None
            await self._destroy_client()
            # 通知上层（TVRemote._on_mqtt_disconnected），触发离线状态更新和重连
            if self._on_disconnected:
                self._on_disconnected()

    async def _dispatch_message(self, message) -> None:
        """把单条 MQTT 消息解码后交给 on_message 回调.

        未注入回调时直接返回（保持旧行为：只靠迭代器阻塞检测断连）。
        所有收到的消息都以 [MQTT←] 前缀打 INFO 日志（截断到 _LOG_PAYLOAD_LIMIT
        字符），应用列表这类大 payload 只记录总长度，避免日志被 base64 撑爆。
        """
        topic = str(message.topic)
        payload = self._decode_payload(message.payload)
        _LOGGER.info(
            "[MQTT←] %s topic=%s len=%s payload=%s",
            self._device_id,
            topic,
            len(payload),
            _truncate_for_log(payload),
        )
        if self._on_message is None:
            return

        try:
            await self._on_message(topic, payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[MQTT] 处理消息失败 topic=%s len=%s", topic, len(payload)
            )

    @staticmethod
    def _decode_payload(payload: object) -> str:
        """统一把 aiomqtt 的 payload（bytes/str/其他）解成 UTF-8 文本."""
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload).decode("utf-8", errors="replace")
        return "" if payload is None else str(payload)

    async def _destroy_client(self) -> None:
        """销毁 aiomqtt Client，释放底层 TCP 连接资源.

        先停止监听器（防止 _listen_messages 持有 client 引用导致 GC 延迟），
        再调用 __aexit__ 关闭 aiomqtt 内部的网络连接。
        如果 __aexit__ 本身报错（如连接已经断开），静默忽略，
        因为我们的目标是确保 self._client 最终为 None。
        """
        self._stop_listener()
        if self._client is not None:
            try:
                # 手动调用 __aexit__ 以触发 aiomqtt 内部的 DISCONNECT 发包和 socket 关闭
                await self._client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                # 连接已断开时 __aexit__ 可能报错，静默处理，目标只是释放资源
                _LOGGER.debug("Error destroying MQTT client", exc_info=True)
            self._client = None

    async def disconnect(self) -> None:
        """主动断开与电视 MQTT Broker 的连接.

        由上层（TVRemote.async_will_remove_from_hass）在实体卸载时调用。
        先取消监听器（避免 CancelledError 触发断连回调），再销毁客户端。
        """
        self._stop_listener()
        await self._destroy_client()

    async def publish(self, topic: str, payload: str) -> None:
        """异步发布消息到电视 MQTT Broker.

        如果当前未连接（self._client is None），先尝试重连再发送。
        设计原则：
          - 发布失败只记录日志，不抛异常，调用方无需处理异常。
          - 在线 / 离线状态由消息监听器驱动，不依赖本方法的成功与否。
          - 重连逻辑仅作为 publish 路径的临时补救，正式重连由 TVRemote 的
            _async_reconnect_loop 负责，两者互不干扰。

        Args:
            topic: 目标 MQTT Topic。
            payload: 消息内容（按键名称字符串，如 "KEY_OK"）。
        """
        if self._client is None:
            # 客户端未连接，先尝试重连（可能是首次发送前连接尚未建立）
            _LOGGER.warning("MQTT client not connected, attempting reconnect")
            connected = await self.connect()
            if not connected:
                _LOGGER.error(
                    "[MQTT→] %s 发送失败: 重连 %s:%s 未成功，topic=%s payload=%s",
                    self._device_id, self._host, self._port,
                    topic, _truncate_for_log(payload),
                )
                return

        _LOGGER.info(
            "[MQTT→] %s topic=%s len=%s payload=%s",
            self._device_id,
            topic,
            len(payload),
            _truncate_for_log(payload),
        )
        try:
            # QoS=1：确保消息至少送达 Broker 一次（允许重复，但不会丢失）
            # 外层加短超时兜底：电视端 Broker 偶发不回 PUBACK，
            # 不设超时会把调用方（如应用列表请求）一起卡死。
            await asyncio.wait_for(
                self._client.publish(topic, payload, qos=1),
                timeout=MQTT_PUBLISH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "[MQTT→] %s 发送超时 (%ss) topic=%s",
                self._device_id, MQTT_PUBLISH_TIMEOUT, topic,
            )
        except Exception:  # noqa: BLE001
            # 发布失败通常意味着连接已断开，消息监听器会独立检测并触发重连
            _LOGGER.warning(
                "[MQTT→] %s 发送失败 topic=%s (连接可能已断开 %s:%s)",
                self._device_id, topic, self._host, self._port,
            )

    @property
    def connected(self) -> bool:
        """当前是否处于已连接状态.

        以 self._client 是否为 None 作为判断依据：
          - 连接建立后 self._client 被赋值。
          - 连接断开后 _destroy_client 将其重置为 None。
        """
        return self._client is not None


class TVRemote(RemoteEntity):
    """HA RemoteEntity：代表一台通过 MQTT 控制的智能电视.

    职责：
      - 管理 TVMQTTClient 的生命周期（创建 / 连接 / 断开）。
      - 将 MQTT 连接状态映射为 HA 实体的 available 属性，
        available=True 对应 online，False 对应 offline。
      - 对外暴露 send_command 服务，将按键名称通过 MQTT 发送给电视。
      - 维护周期性重连后台 Task，在连接断开后自动恢复。

    状态机：
      offline(初始) → 后台连接 → online
                ↑                    |
                └── 断连回调 ← 消息监听器检测到断连
      重连 Task 持续尝试 connect，成功后回到 online。
    """

    # 实体名称从 entry.title 读取，遵循 HA 命名规范
    _attr_has_entity_name = True
    # 不支持 turn_on（硬件限制：无法远程开机），turn_off 发送 KEY_POWER
    _attr_supported_features = RemoteEntityFeature(0)

    def __init__(self, entry: ConfigEntry, data: dict) -> None:
        """初始化 TV 遥控器实体.

        Args:
            entry: HA 配置项，提供 entry_id、title 等元信息。
            data:  由 config_flow 填充的运行时数据字典，包含主机、端口、
                   MQTT 认证信息、command_topic 等。
        """
        self._entry = entry
        self._device_id = data[CONF_DEVICE_ID]
        # command_topic：发送按键指令的 MQTT Topic，由 config_flow 在配对时确定
        self._command_topic = data["command_topic"]
        # text_command_topic：发送文本控制指令的 MQTT Topic，电视端接收后自定义处理
        self._text_command_topic = data["text_command_topic"]
        # 应用列表管理器由 __init__.py 创建，与 remote 共用同一条 MQTT 连接
        self._app_manager: AppListManager | None = data.get("app_manager")

        # ── 创建 MQTT 客户端 ──
        # 认证信息由用户在 config_flow 中输入 PIN 码后派生，固定使用 TLS 加密
        host = data[CONF_HOST]
        port = data["port"]
        mqtt_username = data[CONF_MQTT_USERNAME]
        mqtt_password = data[CONF_MQTT_PASSWORD]
        self._mqtt_client = TVMQTTClient(
            host, port, self._device_id,
            mqtt_username, mqtt_password,
            # 断连回调：连接丢失时通知本实体更新状态并启动重连
            on_disconnected=self._on_mqtt_disconnected,
            # 消息回调：把电视回推的应用列表交给 AppListManager 解析缓存
            on_message=(
                self._app_manager.async_handle_message
                if self._app_manager is not None
                else None
            ),
        )
        if self._app_manager is not None:
            # 注入发布通道，让管理器能发 getapps / launchapp
            self._app_manager.set_publisher(self._mqtt_client.publish)

        # ── HA 实体属性 ──
        self._attr_name = entry.title          # 实体显示名称
        self._attr_unique_id = f"{DOMAIN}_{self._device_id}"  # 全局唯一 ID
        self._attr_available = False           # 初始为离线，连接成功后由 _set_available 更新

        # 周期重连后台 Task 的引用，None 表示当前无重连任务在运行
        self._reconnect_task: asyncio.Task | None = None
        # 用于唤醒重连循环的事件，断开或 broker 恢复时 set，让循环立即重试
        self._reconnect_event: asyncio.Event | None = None
        # 应用列表轮询 Task 的引用（电视端增删应用不会主动推送，靠定时补拉）
        self._apps_poll_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        """实体注册到 HA 后的初始化钩子.

        HA 完成实体注册后调用此方法，适合在此发起网络连接。

        关键设计：使用 hass.loop.create_task 而非 hass.async_create_task。
          - hass.async_create_task 创建的 Task 会被 HA 启动流程追踪，
            HA 会等待所有追踪 Task 完成后才进入下一启动阶段。
          - TLS 握手 + MQTT 连接可能耗时数秒（电视未开机时甚至会超时），
            若使用 async_create_task，会阻塞整个 HA 启动流程。
          - 使用 loop.create_task 创建"游离 Task"，HA 不追踪它，
            连接过程完全在后台进行，不影响 HA 启动速度。
        """
        # 将实体引用存入 hass.data，__init__.py 中的服务处理函数通过此引用调用 send_command
        self.hass.data[DOMAIN][self._entry.entry_id]["entity"] = self

        # 后台启动连接，不阻塞 HA 启动流程（见上方设计说明）
        self.hass.loop.create_task(self._async_connect())

        # 应用列表定时补拉：电视端新增/卸载应用不会通知 HA，只能轮询
        self._start_apps_poll_task()

        # 绑定成功后自动在默认仪表盘创建遥控器卡片（仅首次）
        self.hass.loop.create_task(self._async_ensure_dashboard_card())

    async def _async_connect(self) -> None:
        """后台连接 MQTT Broker 的协程.

        连接成功：更新实体为 online。
        连接失败：更新为 offline 并启动周期重连 Task，
                  等待 MQTT_RECONNECT_INTERVAL 秒后重试。
        """
        connected = await self._try_connect()

        if connected:
            _LOGGER.info(
                "Connected to TV MQTT broker at %s:%s for %s",
                self._mqtt_client._host, self._mqtt_client._port, self._device_id,
            )
            self._set_available(True)
            # 建连后主动拉一次应用列表（拿不到只影响展示，不影响遥控）
            self.hass.loop.create_task(self._async_request_apps())
        else:
            _LOGGER.warning(
                "Failed to connect to TV MQTT broker at %s:%s for %s, will retry in %ss",
                self._mqtt_client._host, self._mqtt_client._port,
                self._device_id, MQTT_RECONNECT_INTERVAL,
            )
            self._set_available(False)
            # 连接失败，启动后台重连循环
            self._start_reconnect_task()

    async def _async_request_apps(self) -> None:
        """建连/重连成功后主动请求一次应用列表.

        由 loop.create_task 驱动，不阻塞实体就绪；失败仅记日志。
        """
        if self._app_manager is None:
            return
        await self._app_manager.async_request_apps()

    # ────────────────────────── 应用列表轮询 ──────────────────────────

    def _start_apps_poll_task(self) -> None:
        """启动应用列表轮询 Task（幂等，重复调用不会创建多个）."""
        if self._app_manager is None:
            return
        if self._apps_poll_task and not self._apps_poll_task.done():
            return
        self._apps_poll_task = self.hass.loop.create_task(
            self._async_apps_poll_loop()
        )

    def _cancel_apps_poll_task(self) -> None:
        """取消应用列表轮询 Task（实体卸载时调用）."""
        if self._apps_poll_task and not self._apps_poll_task.done():
            self._apps_poll_task.cancel()
        self._apps_poll_task = None

    async def _async_apps_poll_loop(self) -> None:
        """每隔 APP_LIST_POLL_INTERVAL 秒拉一次应用列表.

        背景：电视端只在 HA 请求时回推 apps，新增/卸载应用不会主动上报，
        导致仪表盘上的应用宫格长期停在旧列表。这里做定时补拉：
          - 仅在 MQTT 已连接时发请求，避免电视关机时把 publish 的
            重连兜底路径拖成每分钟一次 TLS 握手；
          - 单次只发 1 条 getapps、不重试，尽量轻量；
          - 内容没变时 AppListManager 不会通知订阅者，因此不会写实体
            状态、也不会重写仪表盘，开销仅一次 MQTT 往返。
        """
        _LOGGER.info(
            "[Apps] %s 已启动应用列表轮询（每 %s 秒一次）",
            self._device_id, APP_LIST_POLL_INTERVAL,
        )
        while True:
            try:
                await asyncio.sleep(APP_LIST_POLL_INTERVAL)
                if not self._mqtt_client.connected:
                    _LOGGER.debug(
                        "[Apps] %s 轮询跳过：MQTT 未连接", self._device_id
                    )
                    continue
                await self._app_manager.async_request_apps(
                    attempts=1, timeout=APP_LIST_TIMEOUT
                )
            except asyncio.CancelledError:
                _LOGGER.debug("[Apps] %s 应用列表轮询已停止", self._device_id)
                return
            except Exception:  # noqa: BLE001
                # 单轮失败不影响下一轮，避免任务静默退出
                _LOGGER.exception(
                    "[Apps] %s 应用列表轮询出错", self._device_id
                )

    async def _async_ensure_dashboard_card(self) -> None:
        """首次加载时，按用户在 config_flow 中的选择创建遥控器卡片."""
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        if entry_data.get("dashboard_created"):
            return

        # 等待 entity_id 稳定注册，同时给 Lovelace 完成加载的时间。
        await asyncio.sleep(3)

        add_to_dashboard = self._entry.data.get(CONF_ADD_TO_DASHBOARD, False)
        if not add_to_dashboard:
            _LOGGER.info(
                "[Dashboard] 用户选择不添加 %s 的遥控器卡片", self.entity_id
            )
            entry_data["dashboard_created"] = True
            return

        dashboard_id = self._entry.data.get(CONF_DASHBOARD_ID)
        view_path = self._entry.data.get(CONF_VIEW_PATH)
        new_view_title = self._entry.data.get(CONF_NEW_VIEW_TITLE)
        new_view_path = self._entry.data.get(CONF_NEW_VIEW_PATH)

        for attempt in range(3):
            _LOGGER.info(
                "[Dashboard] 第 %s 次为 %s 创建卡片", attempt + 1, self.entity_id
            )
            success = await async_create_remote_dashboard_card(
                self.hass,
                self._entry,
                self.entity_id,
                self._attr_name or "电视",
                dashboard_id=dashboard_id,
                view_path=view_path,
                new_view_title=new_view_title,
                new_view_path=new_view_path,
            )
            if success:
                entry_data["dashboard_created"] = True
                return
            _LOGGER.warning("[Dashboard] 本次创建失败，10 秒后重试")
            await asyncio.sleep(10)

        _LOGGER.warning(
            "[Dashboard] 为 %s 创建遥控器卡片最终失败", self.entity_id
        )

    async def _try_connect(self) -> bool:
        """调用 TVMQTTClient.connect() 尝试建立连接.

        独立封装为方法，方便后续统一加调试日志或重试策略。

        超时策略说明：
          不在外层添加 asyncio.wait_for 超时，而是依赖 aiomqtt Client 的
          内部 timeout 参数（MQTT_CONNECT_TIMEOUT）控制连接超时。
          原因：SSL 文件加载（线程池）+ TLS 握手的总耗时难以精确预估，
          外层固定超时容易在慢速网络上误判为失败，造成反复重连。
        """
        connected = await self._mqtt_client.connect()
        return connected

    @callback
    def _on_mqtt_disconnected(self) -> None:
        """MQTT 连接断开的回调函数.

        由 TVMQTTClient._listen_messages 在检测到连接异常时调用。
        使用 @callback 装饰器表示此函数在 HA 事件循环线程中同步执行（非协程），
        内部不能有 await，适合做状态更新和启动新 Task。

        处理逻辑：
          1. 将实体标记为 offline，触发 HA 界面更新。
          2. 启动周期重连 Task，尝试恢复连接。
        """
        _LOGGER.warning(
            "MQTT disconnected for %s, updating entity to offline",
            self._device_id,
        )
        self._set_available(False)
        self._start_reconnect_task()

    @callback
    def _set_available(self, available: bool) -> None:
        """更新实体可用状态并通知 HA 刷新界面.

        使用 @callback 装饰器，在 HA 事件循环线程中同步调用。
        只在状态真正发生变化时才调用 async_write_ha_state，避免无效刷新。

        Args:
            available: True 表示设备在线，False 表示离线。
        """
        if self._attr_available != available:
            self._attr_available = available
            # 通知 HA 状态机，触发前端界面和状态历史的更新
            self.async_write_ha_state()

    def _start_reconnect_task(self) -> None:
        """启动周期性重连后台 Task.

        幂等：若已有重连 Task 在运行，则通过 Event 唤醒它立即重试，
        而不是等 60 秒；只有没有运行时才创建新 Task。

        使用 hass.loop.create_task 创建游离 Task（同 async_added_to_hass 的理由）。
        """
        if self._reconnect_task and not self._reconnect_task.done():
            # 已有重连任务在运行，唤醒它立即尝试一次
            if self._reconnect_event is not None:
                self._reconnect_event.set()
            return
        self._reconnect_event = asyncio.Event()
        self._reconnect_task = self.hass.loop.create_task(self._async_reconnect_loop())

    def _cancel_reconnect_task(self) -> None:
        """取消正在运行的周期性重连 Task.

        在实体卸载（async_will_remove_from_hass）时调用，
        确保 HA 停止后不再有游离 Task 持续运行。
        """
        if self._reconnect_task and not self._reconnect_task.done():
            # 先唤醒再取消，避免任务卡在 Event.wait 中无法响应取消
            if self._reconnect_event is not None:
                self._reconnect_event.set()
            self._reconnect_task.cancel()
        self._reconnect_task = None
        self._reconnect_event = None

    async def _async_discover_homelink_host(self) -> str | None:
        """主动 SSDP 扫描，按 MAC 身份匹配当前配置项对应的电视当前 IP.

        电视切网 / DHCP 重新分配后，配置项里的旧 IP 可能已不可用。
        通过主动发送 M-SEARCH 并探测 37771 端口，再抓 DLNA XML 解析 MAC 身份，
        确保即使 UDN 变化也能准确找到同一台电视，避免误连其他设备。
        """
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

        candidates: list[tuple[str, str]] = []  # (host, location)
        targets = (
            "ssdp:all",
            "upnp:rootdevice",
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaServer:1",
        )
        try:
            sock.bind(("0.0.0.0", 0))
            _LOGGER.info("[重连扫描] 发送 SSDP M-SEARCH 寻找电视")
            for target in targets:
                request = (
                    "M-SEARCH * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "MAN: \"ssdp:discover\"\r\n"
                    "MX: 2\r\n"
                    f"ST: {target}\r\n\r\n"
                ).encode()
                await loop.sock_sendto(sock, request, ("239.255.255.250", 1900))

            deadline = loop.time() + 3
            while (remaining := deadline - loop.time()) > 0:
                try:
                    payload, address = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, 65535), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break
                headers = self._parse_ssdp_response(payload)
                if not headers:
                    continue
                location = headers.get("location")
                host = urlparse(location).hostname if location else address[0]
                if host and (host, location) not in candidates:
                    candidates.append((host, location))
        except OSError as err:
            _LOGGER.warning("[重连扫描] SSDP 扫描失败: %s", err)
            return None
        finally:
            sock.close()

        _LOGGER.info("[重连扫描] 收到 %s 个 SSDP 响应，探测 HomeLink MQTT 端口", len(candidates))
        expected_id = self._entry.data.get(CONF_DEVICE_ID)
        # 新配对条目使用 "macWifi|macEthernet" 作为 device_id；旧条目是 uuid4，无法按 MAC 精确匹配。
        is_mac_identity = expected_id and "|" in expected_id
        for host, location in candidates:
            if not await self._test_tcp_connection(host, MQTT_PORT):
                continue
            if not is_mac_identity:
                _LOGGER.info(
                    "[重连扫描] device_id 不是 MAC 身份，返回首个可达电视: %s", host
                )
                return host
            mac_identity = await self._async_fetch_mac_identity(location)
            _LOGGER.info(
                "[重连扫描] host=%s mac_identity=%s expected=%s",
                host, mac_identity, expected_id,
            )
            if mac_identity and mac_identity == expected_id:
                return host
        return None

    @staticmethod
    def _parse_ssdp_response(payload: bytes) -> dict[str, str]:
        """解析 SSDP HTTP 风格响应头；忽略 NOTIFY 与无效数据包。"""
        lines = payload.decode("utf-8", errors="replace").replace("\r", "").split("\n")
        if not lines or not lines[0].startswith("HTTP/1.1 200"):
            return {}
        return {
            key.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for key, value in [line.split(":", 1)]
        }

    async def _async_fetch_mac_identity(self, location_url: str) -> str | None:
        """从 DLNA 设备描述 XML 的 modelDescription 中提取 MAC 身份。"""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with asyncio.timeout(5):
                async with session.get(location_url) as resp:
                    if resp.status != 200:
                        return None
                    text = await resp.text()
        except Exception as exc:
            _LOGGER.debug("[重连扫描] 获取设备描述失败 %s: %s", location_url, exc)
            return None

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            _LOGGER.debug("[重连扫描] 解析设备描述失败 %s: %s", location_url, exc)
            return None

        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "modelDescription" and elem.text:
                return self._parse_mac_identity(elem.text.strip())
        return None

    @staticmethod
    def _parse_mac_identity(model_description: str | None) -> str | None:
        """从 modelDescription 的 key=value 块中提取 macWifi + macEthernet 身份。"""
        if not model_description:
            return None
        mac_wifi = ""
        mac_ethernet = ""
        for line in model_description.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lower()
            value = value.strip().lower()
            if key == "macwifi":
                mac_wifi = value
            elif key == "macethernet":
                mac_ethernet = value
        identity = f"{mac_wifi}|{mac_ethernet}"
        return identity if identity != "|" else None

    async def _test_tcp_connection(self, host: str, port: int) -> bool:
        """测试指定主机端口的 TCP 连通性。"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def _async_reconnect_loop(self) -> None:
        """周期性重连协程，直到连接恢复为止.

        循环逻辑：
          1. 主动 SSDP 扫描，找到电视当前 IP；若与配置项不同则更新 host 并触发 HA 重载。
          2. 尝试连接当前 host，成功则更新为 online 并退出循环。
          3. 失败则按指数退避等待，或被断开事件 / broker 恢复信号唤醒。

        退出条件：
          - 连接成功（return）。
          - Task 被外部 cancel（CancelledError → return）。
          - _mqtt_client.connected 变为 True（while 条件不满足，正常退出）。
          - SSDP 发现新 IP 并更新 config entry（HA 会卸载本实体并重新加载）。
        """
        attempt = 0
        event = self._reconnect_event
        if event is None:
            return

        while not self._mqtt_client.connected:
            delay = min(MQTT_RECONNECT_INTERVAL, 5)
            try:
                # 先主动扫描一次 SSDP，避免死守旧 IP。
                # 首次进入循环即扫描，无需等待，实现电视切网后快速恢复。
                new_host = await self._async_discover_homelink_host()
                current_host = self._entry.data[CONF_HOST]
                if new_host and new_host != current_host:
                    _LOGGER.warning(
                        "[重连扫描] 电视 IP 变化: %s -> %s, 更新配置并触发重载",
                        current_host, new_host,
                    )
                    self.hass.config_entries.async_update_entry(
                        self._entry,
                        data={**self._entry.data, CONF_HOST: new_host},
                    )
                    return  # config entry 更新后 HA 会重载该条目，新实体用新 IP 连接

                _LOGGER.info(
                    "Attempting reconnect to TV MQTT broker at %s:%s for %s",
                    self._mqtt_client._host, self._mqtt_client._port, self._device_id,
                )
                connected = await self._try_connect()
                if connected:
                    _LOGGER.info(
                        "Reconnected to TV MQTT broker at %s:%s for %s",
                        self._mqtt_client._host, self._mqtt_client._port, self._device_id,
                    )
                    self._set_available(True)
                    # 重连成功后同样补拉一次应用列表
                    self.hass.loop.create_task(self._async_request_apps())
                    return  # 重连成功，退出循环；消息监听器接管后续断连检测

                attempt += 1
                # 指数退避：5s, 10s, 20s, 40s... 最大 60s
                delay = min(MQTT_RECONNECT_INTERVAL, 5 * (2 ** (attempt - 1)))
                _LOGGER.warning(
                    "Reconnect failed to TV MQTT broker at %s:%s for %s, next retry in %ss or on broker restore",
                    self._mqtt_client._host, self._mqtt_client._port,
                    self._device_id, delay,
                )
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.exception(
                    "Unexpected error in reconnect loop for %s: %s", self._device_id, err
                )

            try:
                # 等待退避间隔，期间若 broker 恢复/断开/Task 取消则提前唤醒
                await asyncio.wait_for(event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                # 正常超时，进入下一轮
                pass
            except asyncio.CancelledError:
                return
            else:
                # 被事件唤醒（如 broker 重启后又断开），立即重试并回到短间隔
                event.clear()
                attempt = 0

    async def async_will_remove_from_hass(self) -> None:
        """实体从 HA 移除前的清理钩子.

        HA 在卸载集成或重启时调用，确保：
          1. 取消重连 Task，停止后台重连行为。
          2. 主动断开 MQTT 连接，向电视侧发送 DISCONNECT，
             触发 Broker 发布遗嘱消息（"CLOSE"），通知电视 HA 已下线。
          3. 清理本集成在 Lovelace 仪表盘中为该实体生成的遥控器 section。
        """
        self._cancel_apps_poll_task()
        self._cancel_reconnect_task()
        await self._mqtt_client.disconnect()

        # 清理自动生成的遥控器 section（不删除整个视图）
        try:
            await async_delete_remote_dashboard_card(
                self.hass, self.entity_id
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[Dashboard] 清理 %s 的遥控器卡片失败: %s", self.entity_id, exc
            )

    @property
    def is_on(self) -> bool:
        """遥控器"开关"状态，映射为 MQTT 连接状态.

        HA RemoteEntity 使用 is_on 表示设备是否处于可控状态。
        此处以 MQTT 连接是否建立作为判断标准：
          - True：已连接，可以发送指令。
          - False：未连接，指令无法送达。
        """
        return self._attr_available

    @property
    def device_info(self) -> dict:
        """返回设备信息，用于 HA 设备注册表.

        HA 通过 identifiers 将多个实体归属到同一个设备，
        前端"设备页"会展示此处配置的制造商、型号等信息。
        """
        return {
            # 以 (domain, device_id) 作为全局唯一设备标识
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._attr_name,
            "manufacturer": "TV Control",
            "model": "Smart TV",
        }

    async def async_turn_on(self, **kwargs) -> None:
        """开机操作——硬件不支持，显式报错.

        电视侧 MQTT Broker 仅在电视开机后才运行，
        因此无法通过 MQTT 实现远程开机。
        """
        raise HomeAssistantError(
            "电视不支持远程开机：MQTT Broker 随电视启动，关机后无法连接。"
        )

    async def async_turn_off(self, **kwargs) -> None:
        """关机操作——发送电源键指令.

        通过发送 KEY_POWER 按键实现关机（或开关机切换，取决于电视当前状态）。
        """
        await self.async_send_command([KEY_POWER])

    async def async_toggle(self, **kwargs) -> None:
        """开关切换——不支持，显式报错.

        遥控器实体只有 MQTT 连接状态（is_on），不代表电视真实开关机，
        因此不做 toggle，避免用户以为能一键开关机而误发指令。
        """
        raise HomeAssistantError(
            "电视不支持开关切换：无法判断真实开关机状态。"
            "需要关机请用 remote.turn_off（发送 KEY_POWER）。"
        )

    async def async_learn_command(self, **kwargs) -> None:
        """学习按键——不支持，显式报错."""
        raise HomeAssistantError(
            "电视不支持学习按键：本集成通过 MQTT 直接发送 KEY_* 按键名，"
            "无需学习红外码。可用按键见 const.py 的 KEY_* 常量。"
        )

    async def async_delete_command(self, **kwargs) -> None:
        """删除已学习按键——不支持（没有可管理的按键库）."""
        raise HomeAssistantError(
            "电视不支持删除按键：本集成没有可管理的学习按键库。"
        )

    async def async_send_command(self, command, **kwargs) -> None:
        """向电视发送按键指令（HA Remote 平台标准接口）.

        协议说明：
          - payload 直接为按键名称字符串（如 "KEY_OK"），无需 JSON 包装。
          - 每次调用只处理 command 列表的第一个元素（单按键发送）。
            电视侧 MQTT 协议不支持批量按键，多余的指令会被丢弃并记录警告。

        Args:
            command: HA 框架传入的按键名称列表（如 ["KEY_OK"] 或 ["KEY_UP", "KEY_DOWN"]）。
            **kwargs: 框架预留参数（如 num_repeats、delay_secs），当前不使用。
        """
        cmd = command[0]
        if len(command) > 1:
            # 电视侧不支持批量指令，多余的按键名丢弃并记录警告，便于排查配置错误
            _LOGGER.warning(
                "Only single command supported, ignoring extra: %s", command[1:]
            )
        _LOGGER.info("Remote sending command %s to %s", cmd, self._command_topic)
        await self._mqtt_client.publish(self._command_topic, cmd)

    async def async_send_text_command(self, text: str) -> None:
        """向电视发送文本控制指令.

        与 async_send_command 不同：本方法把任意文本直接发布到独立的
        text_command_topic，不经过 KEY_MAP 映射，供电视端自定义解析。

        Args:
            text: 要发送的文本字符串（如 JSON、自定义协议报文等）。
        """
        _LOGGER.info(
            "Remote sending text command %s to %s", text, self._text_command_topic
        )
        await self._mqtt_client.publish(self._text_command_topic, text)
