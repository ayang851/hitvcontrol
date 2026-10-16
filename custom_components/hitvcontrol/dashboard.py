"""自动为 Hisense Home 设备创建默认 Lovelace 遥控器视图.

绑定成功后，可在用户选择的 storage 仪表盘视图中添加一个遥控器 section，
避免用户手动复制 YAML。是否添加、添加到哪个仪表盘/视图由 config_flow 中的
用户选择决定。
"""
import logging

from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DASHBOARD_VIEW_TITLE = "电视遥控"
DASHBOARD_VIEW_PATH = "tv_remote"

# 标识「在此仪表盘下新建视图」的特殊值
VIEW_PATH_AUTO = "__auto__"


async def async_create_remote_dashboard_card(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entity_id: str,
    device_name: str,
    dashboard_id: str | None = None,
    view_path: str | None = None,
    new_view_title: str | None = None,
    new_view_path: str | None = None,
) -> bool:
    """为指定 remote 实体在 storage 仪表盘中创建/追加遥控器 section.

    行为：
      - 若传入 dashboard_id，则只操作该仪表盘；否则遍历所有可用 storage 仪表盘。
      - 若传入 view_path 为具体路径，则把 section 放到该视图；若为 None 或
        VIEW_PATH_AUTO，则在没有可用视图时新建视图，否则复用第一个视图。
      - 当需要新建视图时，优先使用 new_view_title/new_view_path；未提供则使用
        默认的「电视遥控」标题和路径。
      - 若目标视图中已存在该实体的遥控器 section，则替换而非重复添加。

    Returns:
        True 表示至少一个仪表盘已更新，False 表示本次未处理（可重试）。
    """
    _LOGGER.info(
        "[Dashboard] 开始为 %s (%s) 创建遥控器卡片", device_name, entity_id
    )

    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return False

    # 如果用户指定了 dashboard_id，只操作该仪表盘
    if dashboard_id is not None:
        target = next(
            ((dash_id, dash) for dash_id, dash in candidates if dash_id == dashboard_id),
            None,
        )
        if target is None:
            _LOGGER.warning(
                "[Dashboard] 未找到指定的仪表盘 %s，跳过", dashboard_id
            )
            return False
        candidates = [target]

    updated_any = False
    for dash_id, dash in candidates:
        updated = await _async_update_dashboard(
            dash_id,
            dash,
            entity_id,
            device_name,
            view_path=view_path,
            new_view_title=new_view_title,
            new_view_path=new_view_path,
        )
        if updated:
            updated_any = True

    return updated_any


async def async_delete_remote_dashboard_card(
    hass: HomeAssistant,
    entity_id: str,
) -> bool:
    """删除指定 remote 实体在 storage 仪表盘中对应的遥控器 section.

    只会删除由本集成生成的 section（通过 entity_id + 典型按键指令识别），
    不会删除整个视图，避免误删用户手动添加的其他卡片。

    Returns:
        True 表示至少一个仪表盘已清理，False 表示未找到或清理失败。
    """
    _LOGGER.info("[Dashboard] 开始清理 %s 的遥控器卡片", entity_id)

    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return False

    deleted_any = False
    for dash_id, dash in candidates:
        deleted = await _async_delete_from_dashboard(
            dash_id, dash, entity_id
        )
        if deleted:
            deleted_any = True

    return deleted_any


async def async_get_dashboard_choices(
    hass: HomeAssistant,
) -> list[dict[str, str]]:
    """收集所有可用 storage 仪表盘，供 config_flow 第一级选择.

    Returns:
        每个元素包含 dashboard_id、dashboard_title、label。
    """
    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return []

    choices: list[dict[str, str]] = []
    for dash_id, dash in candidates:
        dash_title = _dashboard_display_title(dash_id, dash)
        # 「概览」即侧边栏 /lovelace 默认入口，无需再带 id 注解
        label = (
            dash_title
            if dash_id == "default"
            else f"{dash_title} ({dash_id})"
        )
        choices.append({
            "dashboard_id": dash_id,
            "dashboard_title": dash_title,
            "label": label,
        })

    return choices


async def async_get_view_choices(
    hass: HomeAssistant,
    dashboard_id: str,
) -> list[dict[str, str]]:
    """收集指定 storage 仪表盘下的所有视图，供 config_flow 第二级选择.

    Returns:
        每个元素包含 view_path、view_title、label。
    """
    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return []

    target = next(
        (dash for dash_id, dash in candidates if dash_id == dashboard_id),
        None,
    )
    if target is None:
        return []

    try:
        config = await target.async_load(False)
        if config is None:
            config = {}
    except Exception:  # noqa: BLE001
        config = {}

    choices: list[dict[str, str]] = []
    for view in config.get("views") or []:
        if not isinstance(view, dict):
            continue
        view_path = view.get("path") or view.get("title")
        view_title = view.get("title") or view_path or "未命名视图"
        if not view_path:
            continue
        choices.append({
            "view_path": str(view_path),
            "view_title": str(view_title),
            "label": str(view_title),
        })

    return choices


async def async_create_new_dashboard(
    hass: HomeAssistant,
    title: str,
    url_path: str,
) -> object | None:
    """创建一个新的 storage dashboard 并返回其 LovelaceStorage 对象.

    Args:
        title: 仪表盘侧边栏标题。
        url_path: 仪表盘 URL 路径（必须包含连字符“-”，否则 HA 校验会失败；
                 若希望使用单个单词，需传 allow_single_word）。

    Returns:
        新创建的 dashboard 对象；失败返回 None。
    """
    from homeassistant.components.lovelace.const import (
        CONF_ALLOW_SINGLE_WORD,
        CONF_ICON,
        CONF_TITLE,
        CONF_URL_PATH,
        DOMAIN as LOVELACE_DOMAIN,
        LOVELACE_DATA,
    )
    from homeassistant.components.lovelace.dashboard import (
        DashboardsCollection,
        LovelaceStorage,
    )

    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        _LOGGER.warning("[Dashboard] Lovelace 尚未加载，无法新建仪表盘")
        return None

    dashboards_collection = DashboardsCollection(hass)
    await dashboards_collection.async_load()

    try:
        await dashboards_collection.async_create_item({
            CONF_ALLOW_SINGLE_WORD: True,
            CONF_ICON: "mdi:remote-tv",
            CONF_TITLE: title,
            CONF_URL_PATH: url_path,
        })
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("[Dashboard] 创建仪表盘失败: %s", exc)
        return None

    # async_create_item 会触发原 DashboardsCollection 的 listener，
    # 新的 dashboard 对象应已被注册到 hass.data[LOVELACE_DATA].dashboards。
    new_dashboard = lovelace_data.dashboards.get(url_path)
    if new_dashboard is None:
        # 兜底：手动构造 LovelaceStorage 并注册 panel，避免 listener 时序异常
        new_dashboard = LovelaceStorage(
            hass,
            {
                "id": url_path,
                CONF_URL_PATH: url_path,
                CONF_TITLE: title,
                CONF_ICON: "mdi:remote-tv",
            },
        )
        lovelace_data.dashboards[url_path] = new_dashboard

        try:
            from homeassistant.components import frontend
            from homeassistant.components.lovelace.const import (
                DEFAULT_ICON,
                MODE_STORAGE,
            )

            if not frontend.async_panel_exists(hass, url_path):
                frontend.async_register_built_in_panel(
                    hass,
                    LOVELACE_DOMAIN,
                    frontend_url_path=url_path,
                    require_admin=False,
                    show_in_sidebar=True,
                    sidebar_title=title,
                    sidebar_icon="mdi:remote-tv",
                    config={"mode": MODE_STORAGE},
                )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "[Dashboard] 为新仪表盘 %s 注册侧边栏入口失败: %s",
                url_path, exc,
            )

    _LOGGER.info("[Dashboard] 已新建仪表盘 %s (%s)", url_path, title)
    return new_dashboard


def _dashboard_display_title(dashboard_id: str, dash: object) -> str:
    """返回仪表盘用于展示给用户的标题."""
    # dashboards[None] 是系统默认仪表盘（LovelaceStorage(hass, None)），
    # 也就是侧边栏「概览」（/lovelace）入口本身，与系统「添加到仪表盘」
    # 的写入目标一致；HA 迁移出的 "lovelace" 条目只是历史遗留副本。
    if dashboard_id == "default":
        return "概览"
    config = getattr(dash, "config", None) or {}
    return (
        config.get("title")
        or getattr(dash, "url_path", None)
        or dashboard_id
    )


def _get_storage_dashboard_candidates(
    hass: HomeAssistant,
) -> list[tuple[str, object]] | None:
    """收集所有可用的 storage 仪表盘候选.

    Returns:
        候选列表；若 Lovelace 未加载或无可用仪表盘则返回 None。
    """
    lovelace_data = hass.data.get(LOVELACE_DOMAIN)
    if lovelace_data is None:
        _LOGGER.warning("[Dashboard] Lovelace 尚未加载，跳过")
        return None

    dashboards = getattr(lovelace_data, "dashboards", {})
    if not dashboards:
        _LOGGER.warning("[Dashboard] 未找到任何仪表盘")
        return None

    _LOGGER.info("[Dashboard] 可用仪表盘: %s", list(dashboards.keys()))

    candidates: list[tuple[str, object]] = []
    # dashboards[None] 即「概览」入口；恒为 LovelaceStorage，但仍校验 mode，
    # 避免 HA 未来改为 YAML 默认概览时把只读配置加进候选。
    default_dash = dashboards.get(None)
    if default_dash is not None and getattr(default_dash, "mode", None) == "storage":
        candidates.append(("default", default_dash))
    for dash_id, dash in dashboards.items():
        if dash_id is None:
            continue
        # 排除迁移遗留的「概览」副本（/lovelace/lovelace，侧边栏无入口），
        # 与「概览」合并为一个选项，避免同一页面出现两个名字。
        if str(dash_id) == LOVELACE_DOMAIN:
            continue
        if getattr(dash, "mode", None) == "storage":
            candidates.append((str(dash_id), dash))

    if not candidates:
        _LOGGER.warning("[Dashboard] 未找到可用的 storage 仪表盘")
        return None

    return candidates


async def _async_update_dashboard(
    dash_id: str,
    dash: object,
    entity_id: str,
    device_name: str,
    view_path: str | None = None,
    new_view_title: str | None = None,
    new_view_path: str | None = None,
) -> bool:
    """尝试更新单个仪表盘，返回是否成功。"""
    _LOGGER.info("[Dashboard] 处理仪表盘: %s", dash_id)

    try:
        config = await dash.async_load(False)
        if config is None:
            config = {}
    except Exception as exc:  # noqa: BLE001
        exc_name = type(exc).__name__
        if exc_name == "ConfigNotFound":
            config = {"title": "Home", "views": []}
            _LOGGER.info("[Dashboard] 仪表盘 %s 尚无配置，将新建", dash_id)
        else:
            _LOGGER.warning(
                "[Dashboard] 仪表盘 %s 加载失败: %s (%s)",
                dash_id,
                exc_name,
                exc,
            )
            return False

    views = list(config.get("views", []))

    # 确定目标视图：
    # 1. 若指定了具体 view_path 且存在，则使用它；
    # 2. 若 view_path 为 VIEW_PATH_AUTO 或没有可用视图，则新建视图；
    # 3. 否则复用第一个已有视图。
    created_view = False
    view_index = None
    if view_path and view_path != VIEW_PATH_AUTO:
        for idx, view in enumerate(views):
            if isinstance(view, dict) and view.get("path") == view_path:
                view_index = idx
                _LOGGER.info(
                    "[Dashboard] 使用仪表盘 %s 的视图 path=%s", dash_id, view_path
                )
                break
        if view_index is None:
            _LOGGER.warning(
                "[Dashboard] 仪表盘 %s 中未找到 path=%s 的视图，将新建",
                dash_id, view_path,
            )

    if view_index is None:
        if view_path == VIEW_PATH_AUTO or not views:
            view_title = new_view_title or DASHBOARD_VIEW_TITLE
            view_path_value = new_view_path or DASHBOARD_VIEW_PATH
            views.append({
                "title": view_title,
                "path": view_path_value,
                "type": "sections",
                "sections": [],
            })
            view_index = len(views) - 1
            created_view = True
            _LOGGER.info(
                "[Dashboard] 在仪表盘 %s 新建视图 %s (%s)",
                dash_id, view_path_value, view_title,
            )
        else:
            view_index = 0
            _LOGGER.info("[Dashboard] 使用仪表盘 %s 的第一个视图", dash_id)

    view = views[view_index]
    if not isinstance(view, dict):
        _LOGGER.warning("[Dashboard] 仪表盘 %s 视图格式异常，跳过", dash_id)
        return False

    # 确保是 sections 视图
    if view.get("type") != "sections":
        view.pop("cards", None)
        view["type"] = "sections"
        view["sections"] = []
        created_view = True

    sections = list(view.get("sections", []))

    # 查找是否已存在该实体的 section（兼容旧版 type: grid section 与新版嵌套 grid card）
    existing_index = None
    for idx, section in enumerate(sections):
        if isinstance(section, dict) and _section_contains_entity(section, entity_id):
            existing_index = idx
            break

    remote_section = _build_remote_grid_section(entity_id, device_name)

    if existing_index is not None:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中实体 %s 已存在遥控器卡片，替换",
            dash_id,
            entity_id,
        )
        sections[existing_index] = remote_section
    else:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中为实体 %s 追加遥控器卡片",
            dash_id,
            entity_id,
        )
        sections.append(remote_section)

    view["sections"] = sections
    views[view_index] = view
    config["views"] = views

    try:
        await dash.async_save(config)
        if created_view:
            _LOGGER.info(
                "[Dashboard] 已在仪表盘 %s 创建新视图", dash_id
            )
        else:
            _LOGGER.info(
                "[Dashboard] 已更新仪表盘 %s 的目标视图", dash_id
            )
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 保存仪表盘 %s 失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False


async def _async_delete_from_dashboard(
    dash_id: str,
    dash: object,
    entity_id: str,
) -> bool:
    """尝试从单个仪表盘中清理指定实体的遥控器 section，返回是否成功。"""
    _LOGGER.info("[Dashboard] 清理仪表盘: %s", dash_id)

    try:
        config = await dash.async_load(False)
        if config is None:
            config = {}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 仪表盘 %s 加载失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False

    views = list(config.get("views", []))
    changed = False

    for view in views:
        if not isinstance(view, dict):
            continue
        sections = list(view.get("sections", []))
        original_len = len(sections)
        sections = [
            section for section in sections
            if not (
                isinstance(section, dict)
                and _is_remote_section(section, entity_id)
            )
        ]
        if len(sections) < original_len:
            view["sections"] = sections
            changed = True
            _LOGGER.info(
                "[Dashboard] 已从仪表盘 %s 的视图中移除 %s 的遥控器 section",
                dash_id,
                entity_id,
            )

    if not changed:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中未找到 %s 的遥控器 section", dash_id, entity_id
        )
        return True

    config["views"] = views
    try:
        await dash.async_save(config)
        _LOGGER.info("[Dashboard] 已保存清理后的仪表盘 %s", dash_id)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 保存清理后的仪表盘 %s 失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False


def _is_remote_section(section: dict, entity_id: str) -> bool:
    """判断 section 是否是由本集成为指定实体生成的遥控器 section.

    识别条件：
      1. section 中包含该 entity_id；
      2. section 中包含本集成遥控器特有的指令（如 KEY_OK 或 remote.turn_off）。
    这样可以避免误删用户手动添加的同名实体卡片。
    """
    section_str = str(section)
    if entity_id not in section_str:
        return False
    remote_markers = ("KEY_OK", "remote.turn_off", "KEY_VOLUMEUP", "KEY_VOLUMEDOWN")
    return any(marker in section_str for marker in remote_markers)


def _section_contains_entity(section: dict, entity_id: str) -> bool:
    """递归检查 section 配置中是否已包含指定 entity_id。"""
    if not isinstance(section, dict):
        return False
    section_str = str(section)
    if entity_id in section_str:
        return True
    for value in section.values():
        if isinstance(value, list):
            for item in value:
                if _section_contains_entity(item, entity_id):
                    return True
        elif isinstance(value, dict):
            if _section_contains_entity(value, entity_id):
                return True
    return False


def _build_remote_grid_section(entity_id: str, title: str) -> dict:
    """构建原生遥控器 section（可直接作为 sections 视图的子项）。

    布局与 hitvcontrol_card_native.yaml 保持一致：
    使用 vertical-stack 嵌套 horizontal-stack，每行按钮数量与 YAML 完全相同，
    避免 grid card 强制 3 列带来的多余空白占位。
    """
    def _btn(icon: str, name: str, action: str | None, command: str | None = None) -> dict:
        return _button(icon, name, entity_id, action, command)

    def _blank() -> dict:
        return _button("mdi:blank", "", entity_id, None)

    return {
        "title": title,
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _btn("mdi:power", "关机", "remote.turn_off"),
                            _btn("mdi:volume-mute", "静音", "remote.send_command", "KEY_MUTE"),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _btn("mdi:volume-minus", "音量-", "remote.send_command", "KEY_VOLUMEDOWN"),
                            _btn("mdi:volume-plus", "音量+", "remote.send_command", "KEY_VOLUMEUP"),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _blank(),
                            _btn("mdi:chevron-up", "上", "remote.send_command", "KEY_UP"),
                            _blank(),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _btn("mdi:chevron-left", "左", "remote.send_command", "KEY_LEFT"),
                            _btn("mdi:circle-outline", "确认", "remote.send_command", "KEY_OK"),
                            _btn("mdi:chevron-right", "右", "remote.send_command", "KEY_RIGHT"),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _blank(),
                            _btn("mdi:chevron-down", "下", "remote.send_command", "KEY_DOWN"),
                            _blank(),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _btn("mdi:home", "主页", "remote.send_command", "KEY_HOME"),
                            _btn("mdi:arrow-u-left-top", "返回", "remote.send_command", "KEY_BACK"),
                            _btn("mdi:menu", "菜单", "remote.send_command", "KEY_MENU"),
                        ],
                    },
                    {
                        "type": "horizontal-stack",
                        "cards": [
                            _btn("mdi:import", "信号源", "remote.send_command", "KEY_SOURCE"),
                        ],
                    },
                ],
            }
        ],
    }


def _button(
    icon: str,
    name: str,
    entity_id: str,
    action: str | None,
    command: str | None = None,
) -> dict:
    """构建单个按钮卡片。"""
    if action is None:
        return {
            "type": "button",
            "icon": icon,
            "name": name,
            "tap_action": {"action": "none"},
        }

    tap_action: dict = {
        "action": "perform-action",
        "perform_action": action,
        "target": {"entity_id": entity_id},
    }
    if command is not None:
        tap_action["data"] = {"command": command}

    return {
        "type": "button",
        "icon": icon,
        "name": name,
        "tap_action": tap_action,
    }


async def async_create_apps_dashboard_card(
    hass: HomeAssistant,
    entry: ConfigEntry,
    media_entity_id: str,
    apps: list[dict],
    device_name: str,
    dashboard_id: str | None = None,
    view_path: str | None = None,
    new_view_title: str | None = None,
    new_view_path: str | None = None,
) -> bool:
    """为指定 media_player 实体在 storage 仪表盘中创建/追加应用宫格 section.

    行为与 async_create_remote_dashboard_card 保持一致：
      - 去重依据改为 media_player.play_media 动作
      - section 内使用 grid card + button 卡片展示应用
    """
    _LOGGER.info(
        "[Dashboard] 开始为 %s (%s) 创建应用卡片", device_name, media_entity_id
    )

    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return False

    if dashboard_id is not None:
        target = next(
            ((dash_id, dash) for dash_id, dash in candidates if dash_id == dashboard_id),
            None,
        )
        if target is None:
            _LOGGER.warning(
                "[Dashboard] 未找到指定的仪表盘 %s，跳过", dashboard_id
            )
            return False
        candidates = [target]

    updated_any = False
    for dash_id, dash in candidates:
        updated = await _async_update_apps_dashboard(
            dash_id,
            dash,
            media_entity_id,
            device_name,
            apps,
            view_path=view_path,
            new_view_title=new_view_title,
            new_view_path=new_view_path,
        )
        if updated:
            updated_any = True

    return updated_any


async def async_delete_apps_dashboard_card(
    hass: HomeAssistant,
    media_entity_id: str,
) -> bool:
    """删除指定 media_player 实体在 storage 仪表盘中对应的应用宫格 section.

    只会删除由本集成生成的 section（通过 entity_id + media_player.play_media 识别）。
    """
    _LOGGER.info("[Dashboard] 开始清理 %s 的应用卡片", media_entity_id)

    candidates = _get_storage_dashboard_candidates(hass)
    if candidates is None:
        return False

    deleted_any = False
    for dash_id, dash in candidates:
        deleted = await _async_delete_apps_from_dashboard(
            dash_id, dash, media_entity_id
        )
        if deleted:
            deleted_any = True

    return deleted_any


async def _async_update_apps_dashboard(
    dash_id: str,
    dash: object,
    media_entity_id: str,
    device_name: str,
    apps: list[dict],
    view_path: str | None = None,
    new_view_title: str | None = None,
    new_view_path: str | None = None,
) -> bool:
    """尝试更新单个仪表盘的应用 section，返回是否成功."""
    _LOGGER.info("[Dashboard] 处理仪表盘 %s 的应用卡片", dash_id)

    try:
        config = await dash.async_load(False)
        if config is None:
            config = {}
    except Exception as exc:  # noqa: BLE001
        exc_name = type(exc).__name__
        if exc_name == "ConfigNotFound":
            config = {"title": "Home", "views": []}
            _LOGGER.info("[Dashboard] 仪表盘 %s 尚无配置，将新建", dash_id)
        else:
            _LOGGER.warning(
                "[Dashboard] 仪表盘 %s 加载失败: %s (%s)",
                dash_id,
                exc_name,
                exc,
            )
            return False

    views = list(config.get("views", []))

    created_view = False
    view_index = None
    if view_path and view_path != VIEW_PATH_AUTO:
        for idx, view in enumerate(views):
            if isinstance(view, dict) and view.get("path") == view_path:
                view_index = idx
                _LOGGER.info(
                    "[Dashboard] 使用仪表盘 %s 的视图 path=%s", dash_id, view_path
                )
                break
        if view_index is None:
            _LOGGER.warning(
                "[Dashboard] 仪表盘 %s 中未找到 path=%s 的视图，将新建",
                dash_id, view_path,
            )

    if view_index is None:
        if view_path == VIEW_PATH_AUTO or not views:
            view_title = new_view_title or DASHBOARD_VIEW_TITLE
            view_path_value = new_view_path or DASHBOARD_VIEW_PATH
            views.append({
                "title": view_title,
                "path": view_path_value,
                "type": "sections",
                "sections": [],
            })
            view_index = len(views) - 1
            created_view = True
            _LOGGER.info(
                "[Dashboard] 在仪表盘 %s 新建视图 %s (%s)",
                dash_id, view_path_value, view_title,
            )
        else:
            view_index = 0
            _LOGGER.info("[Dashboard] 使用仪表盘 %s 的第一个视图", dash_id)

    view = views[view_index]
    if not isinstance(view, dict):
        _LOGGER.warning("[Dashboard] 仪表盘 %s 视图格式异常，跳过", dash_id)
        return False

    if view.get("type") != "sections":
        view.pop("cards", None)
        view["type"] = "sections"
        view["sections"] = []
        created_view = True

    sections = list(view.get("sections", []))

    existing_index = None
    for idx, section in enumerate(sections):
        if isinstance(section, dict) and _is_apps_section(section, media_entity_id):
            existing_index = idx
            break

    apps_section = _build_apps_section(media_entity_id, apps)

    if existing_index is not None:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中实体 %s 已存在应用卡片，替换",
            dash_id,
            media_entity_id,
        )
        sections[existing_index] = apps_section
    else:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中为实体 %s 追加应用卡片",
            dash_id,
            media_entity_id,
        )
        sections.append(apps_section)

    view["sections"] = sections
    views[view_index] = view
    config["views"] = views

    try:
        await dash.async_save(config)
        if created_view:
            _LOGGER.info("[Dashboard] 已在仪表盘 %s 创建新视图", dash_id)
        else:
            _LOGGER.info("[Dashboard] 已更新仪表盘 %s 的目标视图", dash_id)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 保存仪表盘 %s 失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False


async def _async_delete_apps_from_dashboard(
    dash_id: str,
    dash: object,
    media_entity_id: str,
) -> bool:
    """尝试从单个仪表盘中清理指定 media_player 实体的应用 section."""
    _LOGGER.info("[Dashboard] 清理仪表盘 %s 的应用卡片", dash_id)

    try:
        config = await dash.async_load(False)
        if config is None:
            config = {}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 仪表盘 %s 加载失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False

    views = list(config.get("views", []))
    changed = False

    for view in views:
        if not isinstance(view, dict):
            continue
        sections = list(view.get("sections", []))
        original_len = len(sections)
        sections = [
            section for section in sections
            if not (
                isinstance(section, dict)
                and _is_apps_section(section, media_entity_id)
            )
        ]
        if len(sections) < original_len:
            view["sections"] = sections
            changed = True
            _LOGGER.info(
                "[Dashboard] 已从仪表盘 %s 的视图中移除 %s 的应用 section",
                dash_id,
                media_entity_id,
            )

    if not changed:
        _LOGGER.info(
            "[Dashboard] 仪表盘 %s 中未找到 %s 的应用 section", dash_id, media_entity_id
        )
        return True

    config["views"] = views
    try:
        await dash.async_save(config)
        _LOGGER.info("[Dashboard] 已保存清理后的仪表盘 %s", dash_id)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning(
            "[Dashboard] 保存清理后的仪表盘 %s 失败: %s (%s)",
            dash_id,
            type(exc).__name__,
            exc,
        )
        return False


def _is_apps_section(section: dict, entity_id: str) -> bool:
    """判断 section 是否是由本集成为指定 media_player 实体生成的应用宫格."""
    section_str = str(section)
    if entity_id not in section_str:
        return False
    return "media_player.play_media" in section_str


def _build_apps_section(media_entity_id: str, apps: list[dict]) -> dict:
    """构建应用宫格 section.

    布局：sections 视图下的普通 section，内部放一张 grid card（columns=4），
    grid card 里每个应用一张 button 卡片，点击触发 media_player.play_media。
    """
    app_cards = []
    for app in apps:
        app_cards.append({
            "type": "button",
            "icon": app.get("icon", "mdi:application"),
            "name": app["name"],
            "tap_action": {
                "action": "perform-action",
                "perform_action": "media_player.play_media",
                "target": {"entity_id": media_entity_id},
                "data": {
                    "media_content_type": "app",
                    "media_content_id": app["package"],
                },
            },
        })

    return {
        "title": "应用",
        "cards": [
            {
                "type": "grid",
                "columns": 4,
                "square": True,
                "cards": app_cards,
            }
        ],
    }
