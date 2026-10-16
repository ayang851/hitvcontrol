"""Config flow for TV Control integration."""
import asyncio
import hashlib
import logging
import socket
import ssl
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from aiomqtt import Client, ProtocolVersion

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import aiohttp_client

from .const import (
    CONF_ADD_TO_DASHBOARD,
    CONF_DASHBOARD_ID,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USERNAME,
    CONF_NEW_VIEW_PATH,
    CONF_NEW_VIEW_TITLE,
    CONF_PIN,
    CONF_VIEW_PATH,
    DOMAIN,
    MQTT_CONNECT_TIMEOUT,
    MQTT_CREDENTIAL_PWD_SALT,
    MQTT_CREDENTIAL_USER_SALT,
    MQTT_CREDENTIAL_USERNAME_PREFIX,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    MQTT_TLS_CA_FILENAME,
    MQTT_TLS_CERT_FILENAME,
    MQTT_TLS_KEY_FILENAME,
)
from .dashboard import (
    async_get_dashboard_choices,
)

_LOGGER = logging.getLogger(__name__)

_COMPONENT_DIR = Path(__file__).resolve().parent


def generate_mqtt_credentials(pin: str) -> tuple[str, str]:
    """根据 PIN 码生成 MQTT 连接的用户名和密码.

    算法规则与电视端 HomeLink 保持一致：
    - username = "homelink_" + SHA256(PIN + "homelink_user_salt").hex()[:8]
    - password = SHA256(PIN + "homelink_pwd_salt").hex()[:16]
    """
    username = MQTT_CREDENTIAL_USERNAME_PREFIX + hashlib.sha256(
        (pin + MQTT_CREDENTIAL_USER_SALT).encode()
    ).hexdigest()[:8]
    password = hashlib.sha256(
        (pin + MQTT_CREDENTIAL_PWD_SALT).encode()
    ).hexdigest()[:16]
    return username, password


class TVControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TV Control."""

    VERSION = 4

    def __init__(self) -> None:
        """初始化配置流程状态。"""
        self._host: str | None = None
        self._name: str | None = None
        self._device_id: str | None = None
        self._pin: str | None = None
        self._mqtt_username: str | None = None
        self._mqtt_password: str | None = None
        self._dashboard_id: str | None = None
        self._view_path: str | None = None
        self._new_view_title: str | None = None
        self._new_view_path: str | None = None
        self._discovered_devices: dict[str, dict[str, str]] = {}

    async def async_step_user(self, user_input=None):
        """主动发送 SSDP M-SEARCH，并展示扫描后的可用电视。"""
        devices, scan_error = await self._async_scan_ssdp()
        if scan_error:
            return self.async_abort(reason="scan_failed")
        if not devices:
            return self.async_abort(reason="no_ssdp_response")

        available_devices = await self._async_filter_homelink_devices(devices)
        if not available_devices:
            return self.async_abort(reason="no_homelink_device")

        # 已绑定的电视不进下拉选项（彻底无法被选中），改为在表单描述中
        # 列出名称 + IP 并给出说明；下拉选项只保留未绑定的电视。
        configured_ids = self._configured_unique_ids()
        unbound_devices = [
            device for device in available_devices
            if device.get("id") not in configured_ids
        ]
        bound_devices = [
            device for device in available_devices
            if device.get("id") in configured_ids
        ]

        if not unbound_devices:
            # HA 的 abort 描述同样按 Markdown 渲染（breaks 模式），
            # 用列表逐台展示已绑定的电视（名称 + IP），而不是只给数量提示。
            bound_list = "\n".join(
                f"- {device[CONF_NAME]} ({device[CONF_HOST]})"
                for device in bound_devices
            )
            return self.async_abort(
                reason="all_devices_bound",
                description_placeholders={"bound_devices": bound_list},
            )

        self._discovered_devices = {
            device[CONF_HOST]: device for device in unbound_devices
        }

        # 表单描述按 Markdown 渲染，已绑定电视以列表形式逐台展示。
        bound_list = "\n".join(
            f"- {device[CONF_NAME]} ({device[CONF_HOST]})"
            for device in bound_devices
        ) or "无"
        return self._async_show_select_device_form(
            description_placeholders={"bound_devices": bound_list}
        )

    def _configured_unique_ids(self) -> set[str]:
        """收集当前已绑定电视的唯一标识集合."""
        return {entry.unique_id for entry in self._async_current_entries()}

    def _async_show_select_device_form(
        self,
        errors: dict | None = None,
        description_placeholders: dict | None = None,
    ):
        """展示电视选择表单；选项中仅包含未绑定的电视."""
        options: dict[str, str] = {
            host: f"{device[CONF_NAME]} ({host})"
            for host, device in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({
                vol.Required("device"): vol.In(options),
            }),
            errors=errors or {},
            description_placeholders=description_placeholders,
        )

    async def async_step_select_device(self, user_input=None):
        """让用户从多个已发现且可连接的电视中选择一台。"""
        if user_input is None:
            return await self.async_step_user()
        device = self._discovered_devices[user_input["device"]]
        # 兜底：扫描后、提交前这段时间被其他流程绑定的情况。
        if device.get("id") in self._configured_unique_ids():
            return self._async_show_select_device_form({"base": "already_bound"})
        return await self._async_select_device(device)

    async def async_step_ssdp(self, discovery_info: Any):
        """处理 HA 被动收到的兼容 DLNA/UPnP SSDP 广播。"""
        host = self._extract_discovery_host(discovery_info)
        location = self._extract_discovery_value(
            discovery_info, "ssdp_location", "_location_original", "location", "LOCATION"
        )
        if host is None:
            _LOGGER.debug("Ignoring SSDP discovery without a valid location: %s", discovery_info)
            return self.async_abort(reason="invalid_discovery")

        device_name = self._extract_discovery_name(discovery_info, host)
        device_id = self._extract_discovery_id(discovery_info, host)
        self._device_id = device_id

        # 尝试从设备描述 XML 获取真正的 friendlyName。
        if location:
            friendly_name, _mac_identity = await self._async_fetch_device_info(location)
            if friendly_name:
                device_name = friendly_name

        _LOGGER.warning("[SSDP发现] discovery_info=%s", discovery_info)
        _LOGGER.warning(
            "[SSDP发现] name=%s, host=%s, id=%s, st=%s, server=%s, location=%s",
            device_name,
            host,
            device_id,
            self._extract_discovery_value(discovery_info, "st", "ST") or "unknown",
            self._extract_discovery_value(discovery_info, "server", "SERVER") or "unknown",
            location or "unknown",
        )

        # 同一电视可能针对多个 ST 分别广播，HA 会为每个匹配触发一次本方法；
        # 尽早以唯一 ID 去重，避免发现列表里出现重复卡片。
        await self.async_set_unique_id(device_id)

        # 若设备已配置但 IP 变化（网络切换 / DHCP 重新分配），以 SSDP 发现的地址为准，
        # 更新 host 后 HA 会自动重载该配置项，触发 remote 实体立即重新连接 MQTT，
        # 无需等待 60 秒重连周期。
        existing = next(
            (entry for entry in self._async_current_entries() if entry.unique_id == device_id),
            None,
        )
        if existing and existing.data.get(CONF_HOST) != host:
            _LOGGER.warning(
                "[SSDP发现] 已配置设备 IP 变化: %s -> %s, 立即更新并触发重连",
                existing.data.get(CONF_HOST),
                host,
            )

        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        self.context["title_placeholders"] = {"name": device_name}

        device = {
            CONF_HOST: host,
            CONF_NAME: device_name,
            "id": device_id,
        }
        if not await self._test_tcp_connection(host, MQTT_PORT):
            _LOGGER.debug("Ignoring DLNA device at %s because MQTT port is unavailable", host)
            return self.async_abort(reason="not_supported")
        return await self._async_select_device(device)

    async def _async_select_device(self, device: dict[str, str]):
        """保存发现到的设备，并进入 PIN 验证步骤。"""
        await self.async_set_unique_id(device["id"])
        # 若该设备已配置但 IP 变化，以最新发现地址为准更新 host，触发 HA 重载后立刻重连。
        self._abort_if_unique_id_configured(updates={CONF_HOST: device[CONF_HOST]})
        self._device_id = device["id"]
        self._host = device[CONF_HOST]
        self._name = device[CONF_NAME]
        _LOGGER.info("Discovered HomeLink TV via SSDP at %s (%s)", self._host, self._name)
        return await self.async_step_pin()

    async def _async_scan_ssdp(self) -> tuple[list[dict[str, str]], bool]:
        """发送 M-SEARCH 并收集五秒内返回的任意 SSDP 设备。"""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        responses: dict[str, dict[str, str]] = {}
        targets = (
            "ssdp:all",
            "upnp:rootdevice",
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaServer:1",
        )
        try:
            sock.bind(("0.0.0.0", 0))
            _LOGGER.info("Starting SSDP M-SEARCH for DLNA/UPnP devices")
            for target in targets:
                request = (
                    "M-SEARCH * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "MAN: \"ssdp:discover\"\r\n"
                    "MX: 2\r\n"
                    f"ST: {target}\r\n\r\n"
                ).encode()
                await loop.sock_sendto(sock, request, ("239.255.255.250", 1900))

            deadline = loop.time() + 5
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
                if host is None:
                    continue
                # 同一设备可能针对多个 ST 分别答复；以地址去重，UDN 用于配置唯一标识。
                device_id = (headers.get("usn") or location or host).split("::", 1)[0]
                _LOGGER.warning(
                    "[SSDP扫描] host=%s, id=%s, st=%s, server=%s, usn=%s, location=%s",
                    host,
                    device_id,
                    headers.get("st", "unknown"),
                    headers.get("server", "unknown"),
                    headers.get("usn", "unknown"),
                    location or "unknown",
                )
                _LOGGER.warning("[SSDP扫描] full headers for %s: %s", host, headers)
                responses[host] = {
                    CONF_HOST: host,
                    CONF_NAME: headers.get("server", f"DLNA 设备 {host}"),
                    "id": device_id,
                    "location": location or "",
                }
        except OSError as err:
            _LOGGER.warning("Unable to send or receive SSDP M-SEARCH: %s", err)
            return [], True
        finally:
            sock.close()

        # 尝试从设备描述 XML 获取真正的 friendlyName 和 MAC 身份。
        for device in responses.values():
            location = device.get("location")
            if location:
                friendly_name, mac_identity = await self._async_fetch_device_info(location)
                if friendly_name:
                    device[CONF_NAME] = friendly_name
                if mac_identity:
                    device["id"] = mac_identity

        _LOGGER.info("SSDP M-SEARCH received %s unique response(s)", len(responses))
        return list(responses.values()), False

    async def _async_filter_homelink_devices(
        self, devices: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """仅保留运行 HomeLink MQTT 服务的 DLNA/UPnP 设备。"""
        reachable = await asyncio.gather(
            *(self._test_tcp_connection(device[CONF_HOST], MQTT_PORT) for device in devices)
        )
        matches = [device for device, is_reachable in zip(devices, reachable) if is_reachable]
        for device, is_reachable in zip(devices, reachable):
            _LOGGER.info(
                "SSDP device: host=%s, name=%s, HomeLink MQTT %s",
                device[CONF_HOST],
                device[CONF_NAME],
                "available" if is_reachable else "unavailable",
            )
        _LOGGER.info(
            "SSDP scan found %s device(s); %s expose HomeLink MQTT port %s",
            len(devices), len(matches), MQTT_PORT,
        )
        return matches

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

    @staticmethod
    def _extract_discovery_value(discovery_info: Any, *keys: str) -> str | None:
        """从不同 HA SSDP 数据版本中读取指定字段。"""
        sources = (
            discovery_info,
            getattr(discovery_info, "ssdp_headers", None),
            getattr(discovery_info, "upnp", None),
        )
        for source in sources:
            if source is None:
                continue
            for key in keys:
                value = source.get(key) if hasattr(source, "get") else None
                if value:
                    return str(value)
        return None

    @classmethod
    def _extract_discovery_host(cls, discovery_info: Any) -> str | None:
        """从 SSDP LOCATION URL 中提取发现电视的 IP 地址。"""
        location = cls._extract_discovery_value(
            discovery_info,
            "ssdp_location",
            "_location_original",
            "location",
            "LOCATION",
        )
        return urlparse(location).hostname if location else None

    @classmethod
    def _extract_discovery_id(cls, discovery_info: Any, host: str) -> str:
        """优先使用 MAC 身份，其次 UDN/USN，最后以发现地址兜底。

        电视跨网切换或恢复出厂后 UDN 可能变化，而 Wi-Fi/有线 MAC 相对稳定，
        因此优先从 modelDescription 提取 macWifi + macEthernet 组合作为唯一标识。
        """
        mac_identity = cls._extract_mac_identity(discovery_info)
        if mac_identity:
            return mac_identity
        raw_id = cls._extract_discovery_value(
            discovery_info, "UDN", "udn", "USN", "usn"
        ) or host
        return raw_id.split("::", 1)[0]

    @classmethod
    def _extract_mac_identity(cls, discovery_info: Any) -> str | None:
        """从 SSDP 发现信息中解析 modelDescription，提取 MAC 身份。"""
        model_description = cls._extract_discovery_value(
            discovery_info, "modelDescription", "model_description"
        )
        return cls._parse_mac_identity(model_description)

    @classmethod
    def _extract_discovery_name(cls, discovery_info: Any, host: str) -> str:
        """获取 DLNA 设备名，无法获取时使用地址生成默认名称。"""
        return cls._extract_discovery_value(
            discovery_info,
            "friendlyName",
            "friendly_name",
            "modelName",
            "model_name",
            "server",
            "SERVER",
        ) or f"电视 {host}"

    async def _async_fetch_device_info(
        self, location_url: str
    ) -> tuple[str | None, str | None]:
        """从 DLNA 设备描述 XML 中提取 friendlyName 和 MAC 身份信息。"""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with asyncio.timeout(5):
                async with session.get(location_url) as resp:
                    if resp.status != 200:
                        return None, None
                    text = await resp.text()
        except Exception as exc:
            _LOGGER.debug("Failed to fetch device description from %s: %s", location_url, exc)
            return None, None

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            _LOGGER.debug("Failed to parse device description XML from %s: %s", location_url, exc)
            return None, None

        friendly_name = None
        model_description = None
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "friendlyName" and elem.text and not friendly_name:
                friendly_name = elem.text.strip()
            elif tag == "modelDescription" and elem.text and not model_description:
                model_description = elem.text.strip()
                # modelDescription 通常包含 MAC，拿到即可提前结束
                break

        mac_identity = self._parse_mac_identity(model_description)
        return friendly_name, mac_identity

    @staticmethod
    def _parse_mac_identity(model_description: str | None) -> str | None:
        """从 modelDescription 的 key=value 块中提取 macWifi + macEthernet 身份。

        说明：
          - 电视的 UDN 可能随网络/重置变化，但 Wi-Fi 和有线网卡 MAC 相对稳定。
          - 三台字段 mac、macWifi、macEthernet 不会全部为空，且 mac 可能与
            macWifi 或 macEthernet 重复，因此取 macWifi + macEthernet 组合
            作为跨网络切换时的唯一身份标识。
        """
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

    async def async_step_pin(self, user_input=None):
        """Step 2 - 输入电视端 PIN 码，验证 MQTT 凭据."""
        errors = {}

        if user_input is not None:
            pin = user_input[CONF_PIN]

            # 根据 PIN 码生成 MQTT 认证信息
            mqtt_username, mqtt_password = generate_mqtt_credentials(pin)

            # 尝试用生成的凭据连接 MQTT Broker 验证 PIN 码是否正确
            connected = await self._test_mqtt_connection(
                self._host, mqtt_username, mqtt_password,
            )
            if not connected:
                errors["base"] = "invalid_pin"
            else:
                # PIN 验证通过，保存凭据并进入仪表盘选择步骤。
                # device_id 使用 DLNA modelDescription 中的 MAC 身份（macWifi|macEthernet），
                # 比 uuid4 更稳定：即使电视 UDN 变化或跨网切换，仍能通过 MAC 识别同一设备。
                self._device_id = getattr(self, "_device_id", None) or uuid.uuid4().hex
                self._pin = pin
                self._mqtt_username = mqtt_username
                self._mqtt_password = mqtt_password
                return await self.async_step_dashboard()

        data_schema = vol.Schema({
            vol.Required(CONF_PIN): str,
        })

        return self.async_show_form(
            step_id="pin",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_dashboard(self, user_input=None):
        """选择遥控器卡片要放入的 Lovelace 仪表盘.

        与系统自带「添加到仪表盘」保持一致，只让用户选仪表盘，
        不选视图；后续沿用「复用第一个已有视图，没有则新建」的逻辑。
        """
        errors = {}

        if user_input is not None:
            choice = user_input.get("dashboard_choice", "__skip__")
            if choice == "__skip__":
                return self._async_create_dashboard_entry(
                    add_to_dashboard=False
                )
            self._dashboard_id = choice
            return self._async_create_dashboard_entry(add_to_dashboard=True)

        choices = await async_get_dashboard_choices(self.hass)
        options: dict[str, str] = {
            "__skip__": "跳过，不添加到仪表盘",
        }
        for choice in choices:
            options[choice["dashboard_id"]] = choice["label"]

        # 默认选中第一个已有仪表盘；没有则默认跳过
        default_choice = next(
            (key for key in options if key != "__skip__"),
            "__skip__",
        )

        return self.async_show_form(
            step_id="dashboard",
            data_schema=vol.Schema({
                vol.Required(
                    "dashboard_choice", default=default_choice
                ): vol.In(options),
            }),
            errors=errors,
        )

    def _async_create_dashboard_entry(self, add_to_dashboard: bool) -> dict:
        """根据用户选择创建最终配置条目."""
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_NAME: self._name,
                CONF_DEVICE_ID: self._device_id,
                CONF_HOST: self._host,
                CONF_PIN: self._pin,
                CONF_MQTT_USERNAME: self._mqtt_username,
                CONF_MQTT_PASSWORD: self._mqtt_password,
                CONF_ADD_TO_DASHBOARD: add_to_dashboard,
                CONF_DASHBOARD_ID: self._dashboard_id,
                CONF_VIEW_PATH: None,
                CONF_NEW_VIEW_TITLE: None,
                CONF_NEW_VIEW_PATH: None,
            },
        )

    async def _test_tcp_connection(self, host: str, port: int) -> bool:
        """测试电视 MQTT Broker 的 TCP 连通性."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            _LOGGER.warning("Cannot connect to TV at %s:%s", host, port)
            return False

    def _build_ssl_context(self) -> ssl.SSLContext:
        """构建 mutual TLS 的 SSLContext，禁用 hostname 校验（自签证书 + IP 直连）."""
        ca_path = _COMPONENT_DIR / MQTT_TLS_CA_FILENAME
        cert_path = _COMPONENT_DIR / MQTT_TLS_CERT_FILENAME
        key_path = _COMPONENT_DIR / MQTT_TLS_KEY_FILENAME

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False  # 自签证书 CN 与 IP 不匹配，跳过 hostname 校验
        ctx.verify_mode = ssl.CERT_REQUIRED  # 仍然验证服务端证书是否由我们的 CA 签发
        ctx.load_verify_locations(cafile=str(ca_path))
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return ctx

    async def _test_mqtt_connection(
        self, host: str, username: str, password: str,
    ) -> bool:
        """使用 PIN 生成的凭据测试 MQTT+TLS 连接（双向认证）."""
        ca_path = _COMPONENT_DIR / MQTT_TLS_CA_FILENAME
        cert_path = _COMPONENT_DIR / MQTT_TLS_CERT_FILENAME
        key_path = _COMPONENT_DIR / MQTT_TLS_KEY_FILENAME

        # 预检查证书文件是否存在
        for p in (ca_path, cert_path, key_path):
            if not p.exists():
                _LOGGER.error("TLS cert file missing: %s", p)
                return False

        _LOGGER.debug(
            "Testing MQTT+TLS to %s:%s user=%s ca=%s cert=%s key=%s",
            host, MQTT_PORT, username, ca_path, cert_path, key_path,
        )

        try:
            ssl_ctx = await asyncio.get_running_loop().run_in_executor(
                None, self._build_ssl_context,
            )
            client = Client(
                hostname=host,
                port=MQTT_PORT,
                username=username,
                password=password,
                keepalive=MQTT_KEEPALIVE,
                protocol=ProtocolVersion.V31,
                timeout=MQTT_CONNECT_TIMEOUT,
                tls_context=ssl_ctx,
            )
            async with client:
                pass  # 连接成功即关闭
            _LOGGER.info("MQTT+TLS connection to %s succeeded", host)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "MQTT connection test failed for %s: [%s] %s",
                host, type(err).__name__, err,
            )
            return False
