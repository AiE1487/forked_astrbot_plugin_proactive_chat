"""主动消息插件管理页 API（AstrBot 插件 Pages 后端）。

独立 uvicorn / FastAPI 服务已在 v1.3.0 移除，管理端页面改为 AstrBot 内置插件
页面（v4.24.1+，Dashboard 沙箱 iframe 加载）。本模块通过
``context.register_web_api`` 把原管理端的全部接口重新注册到 Dashboard 转发层，
鉴权由 Dashboard 身份体系承担，不再需要独立端口与访问密码。

响应约定：所有接口固定返回 HTTP 200 的 JSON，用 ``ok`` 字段表达业务成败：
- 成功：``{"ok": True, ...原有载荷}``（在原载荷基础上合并 ok 字段，前端兼容）；
- 失败：``{"ok": False, "error": "..."}``。
前端统一按 ``ok`` 字段判断，避免依赖宿主对非 2xx 状态码的解包行为。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

from ..utils.version import get_plugin_version

try:
    # 插件 Pages 后端 API 依赖 astrbot.api.web 的请求代理与响应封装。
    from astrbot.api.web import json_response, request

    PLUGIN_PAGES_AVAILABLE = True
except ImportError:
    PLUGIN_PAGES_AVAILABLE = False
    logger.warning(
        "[主动消息] 当前 AstrBot 版本缺少 astrbot.api.web 喵，"
        "插件管理页 API 不可用（需要 AstrBot >= 4.24.1）。"
    )


# 插件名：register_web_api 的路由前缀硬性要求。
_PLUGIN_NAME = "astrbot_plugin_proactive_chat"
# 管理页目录名：对应 pages/console/index.html。
_PAGE_NAME = "console"

# asset 接口允许下发的扩展名白名单与单文件大小上限。
_ASSET_ALLOWED_SUFFIXES = {".js", ".jsx", ".css", ".png"}
_ASSET_MAX_BYTES = 4 * 1024 * 1024


def _is_running_in_docker() -> bool:
    """检测当前进程是否运行在 Docker / 容器环境中。"""
    # /.dockerenv 是最常见的容器特征文件，若存在可直接判定为容器环境。
    if os.path.exists("/.dockerenv"):
        return True

    try:
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            # Linux 容器通常会在 cgroup 信息中暴露 docker / kubepods 的路径片段。
            content = cgroup_path.read_text(encoding="utf-8", errors="ignore")
            if "/docker/" in content or "/kubepods/" in content:
                return True
    except Exception:
        # 环境探测失败时宁可保守忽略，不影响主流程。
        pass

    # 额外兼容某些自定义镜像通过环境变量主动标记容器场景的做法。
    return os.environ.get("DOCKER_CONTAINER") == "true"


class AdminApi:
    """主动消息插件管理页 API：注册到 Dashboard 转发层的全部后端接口。"""

    def __init__(self, plugin: Any):
        # plugin 是主插件实例，所有状态与操作都通过它间接访问。
        self.plugin = plugin
        # 直接缓存配置引用，便于路由中统一读写。
        self.config = plugin.config
        # 缓存插件版本，避免高频状态轮询中重复读取文件。
        self._metadata_version = get_plugin_version(default="未知版本")
        # 注册结果标记，便于生命周期日志诊断。
        self._registered = False

    # ------------------------------------------------------------------
    # 注册入口
    # ------------------------------------------------------------------

    def register(self, context: Any) -> None:
        """把管理页全部接口注册到 AstrBot Dashboard 转发层。"""
        if not PLUGIN_PAGES_AVAILABLE:
            logger.warning(
                "[主动消息] 插件管理页 API 未注册喵：astrbot.api.web 不可用。"
            )
            return

        try:
            self._register_routes(context)
            self._registered = True
            logger.info("[主动消息] 插件管理页 API 已注册喵（AstrBot 插件页面）。")
        except Exception as e:
            logger.error(f"[主动消息] 插件管理页 API 注册失败喵: {e}")

    def _register_routes(self, context: Any) -> None:
        """逐条注册路由；bridge 端点即去掉插件名前缀后的相对路径。"""
        reg = context.register_web_api
        p = _PLUGIN_NAME

        # 运行状态汇总（首页卡片与 1s 兜底轮询的数据源）。
        reg(f"/{p}/status", self.api_status, ["GET"], "管理页：运行状态")

        # 全局配置读取与保存（白名单覆盖全部 5 个一级配置组）。
        reg(f"/{p}/config", self.api_get_config, ["GET"], "管理页：读取全局配置")
        reg(f"/{p}/config", self.api_update_config, ["POST"], "管理页：保存全局配置")
        reg(
            f"/{p}/config-schema",
            self.api_get_config_schema,
            ["GET"],
            "管理页：配置 schema",
        )

        # 会话列表与会话差异配置。注意 sessions 必须先于 <path:umo> 注册。
        reg(
            f"/{p}/session-config/sessions",
            self.api_list_session_configs,
            ["GET"],
            "管理页：会话列表",
        )
        reg(
            f"/{p}/session-config/<path:umo>",
            self.api_session_config,
            ["GET", "POST", "DELETE"],
            "管理页：会话差异配置（读取/保存/清空）",
        )

        # 调度任务。
        reg(f"/{p}/jobs", self.api_list_jobs, ["GET"], "管理页：任务列表")
        reg(
            f"/{p}/jobs/<path:umo>/trigger",
            self.api_trigger_job,
            ["POST"],
            "管理页：立即触发",
        )
        reg(
            f"/{p}/jobs/<path:umo>/reschedule",
            self.api_reschedule_job,
            ["POST"],
            "管理页：重新调度",
        )
        reg(
            f"/{p}/jobs/<path:umo>",
            self.api_cancel_job,
            ["POST", "DELETE"],
            "管理页：取消任务",
        )

        # 通知系统。
        reg(
            f"/{p}/notifications",
            self.api_get_notifications,
            ["GET"],
            "管理页：通知列表",
        )
        reg(
            f"/{p}/notifications/read",
            self.api_mark_notification_read,
            ["POST"],
            "管理页：单条已读",
        )
        reg(
            f"/{p}/notifications/read-all",
            self.api_mark_all_notifications_read,
            ["POST"],
            "管理页：全部已读",
        )
        reg(
            f"/{p}/notifications/refresh",
            self.api_refresh_notifications,
            ["POST"],
            "管理页：立即同步通知",
        )

        # Markdown 文档浏览。
        reg(
            f"/{p}/markdown-files",
            self.api_list_markdown_files,
            ["GET"],
            "管理页：文档列表",
        )
        reg(
            f"/{p}/markdown-files/<path:file_path>",
            self.api_get_markdown_file,
            ["GET"],
            "管理页：读取文档",
        )

        # 本地目录快速打开（桌面端便捷入口）。
        reg(
            f"/{p}/open-directory",
            self.api_open_directory,
            ["POST"],
            "管理页：打开插件/数据目录",
        )

        # 内嵌页静态资源（admin/ 目录白名单），供 pages/console/index.html 引导加载。
        reg(
            f"/{p}/asset/<path:file_path>",
            self.api_get_asset,
            ["GET"],
            "管理页：前端资源",
        )

    # ------------------------------------------------------------------
    # 响应封装
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(payload: Any) -> Any:
        """成功响应：原载荷合并 ok 字段后原样返回。"""
        if isinstance(payload, dict):
            body = {"ok": True, **payload} if "ok" not in payload else payload
        else:
            body = {"ok": True, "data": payload}
        return json_response(body)

    @staticmethod
    def _err(message: str, **extra: Any) -> Any:
        """失败响应：固定 HTTP 200 + ok=False，错误文案放 error 字段。"""
        return json_response({"ok": False, "error": message, **extra})

    async def _json_body(self) -> dict[str, Any]:
        """解析 POST 请求体；空体或非 JSON 一律回退为空对象。

        注意：astrbot.api.web 的请求代理只暴露 ``json(default=...)`` 方法，
        并不存在 Quart 风格的 ``get_json()``；误用会抛 AttributeError 并被
        吞掉，导致所有 POST 请求体都被替换成空对象（保存接口看似成功、
        实际未写入任何字段），因此这里必须使用 ``request.json()``。
        """
        try:
            payload = await request.json(default={})
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------
    # 状态 / 任务 / 会话载荷构造（自原 web_admin_server 迁移）
    # ------------------------------------------------------------------

    def _safe_timer_meta(self, timer: Any, now: float) -> dict[str, float | int | None]:
        # 某些会话可能当前没有有效 timer，此时直接返回空元信息。
        if timer is None:
            return {"remaining_seconds": None, "target_time": None}

        try:
            # 某些定时句柄可能已取消；这里优先过滤掉不可用状态。
            if getattr(timer, "cancelled", lambda: False)():
                return {"remaining_seconds": None, "target_time": None}
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

        # asyncio 定时句柄通常暴露 when() 方法，返回 loop 单调时钟上的目标时刻。
        when_method = getattr(timer, "when", None)
        if not callable(when_method):
            return {"remaining_seconds": None, "target_time": None}

        try:
            loop_time = when_method()
            loop = getattr(timer, "_loop", None)
            current_loop_time = loop.time() if loop else None
            if current_loop_time is None:
                return {"remaining_seconds": None, "target_time": None}

            # 用单调时钟差值推导剩余秒数，再换算成当前 Unix 时间戳，避免受系统时间跳变影响。
            remaining_precise = max(0.0, loop_time - current_loop_time)
            target_time = now + remaining_precise
            return {
                # 向上取整，保证 UI 倒计时不会过早显示为 0。
                "remaining_seconds": max(0, int(math.ceil(remaining_precise))),
                "target_time": target_time,
            }
        except Exception:
            return {"remaining_seconds": None, "target_time": None}

    def _detect_session_category(self, session_id: str) -> str:
        # 优先使用插件已有解析逻辑识别会话类型，避免前后端规则不一致。
        parsed = self.plugin._parse_session_id(session_id)
        if not parsed:
            lowered = str(session_id).lower()
            # 兜底规则只在插件解析失败时启用，尽量保证前端仍有可用分类。
            return "group" if "group" in lowered else "friend"

        _, msg_type, _ = parsed
        return "group" if "group" in msg_type.lower() else "friend"

    def _collect_timer_cards(self, now: float) -> dict[str, list[dict[str, Any]]]:
        # auto_cards：自动触发检测计时器；group_cards：群沉默计时器。
        auto_cards: list[dict[str, Any]] = []
        group_cards: list[dict[str, Any]] = []
        # 群计时器优先展示为 group_silence，避免同一群会话被重复渲染两种卡片。
        active_group_sessions = {
            str(session_id) for session_id in self.plugin.group_timers.keys()
        }

        for session_id, timer in list(self.plugin.auto_trigger_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            if normalized_session_id in active_group_sessions:
                continue

            session_config = self.plugin._get_session_config(session_id) or {}
            session_data = self.plugin.session_data.get(session_id, {})
            auto_settings = session_config.get("auto_trigger_settings", {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            trigger_delay_minutes = int(
                auto_settings.get("auto_trigger_after_minutes", 0) or 0
            )
            # 前端展示与进度计算统一按秒处理，因此这里先把分钟窗口换算为秒。
            trigger_delay_seconds = max(0, trigger_delay_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 若拿不到真实开始时间，则退化为“插件启动时间”或根据窗口长度反推一个近似值。
            started_at = max(self.plugin.plugin_start_time, now - trigger_delay_seconds)
            progress_percent = 0
            if trigger_delay_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, trigger_delay_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / trigger_delay_seconds) * 100))
                )

            auto_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "auto_trigger",
                    "title": "自动触发检测",
                    # remaining_seconds 可用时说明计时器处于有效运行状态，否则只能标为 unknown。
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at,
                    "window_seconds": trigger_delay_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "auto_trigger_after_minutes": trigger_delay_minutes,
                }
            )

        for session_id, timer in list(self.plugin.group_timers.items()):
            normalized_session_id = self.plugin._normalize_session_id(str(session_id))
            session_config = (
                self.plugin._get_session_config(normalized_session_id) or {}
            )
            session_data = self.plugin.session_data.get(normalized_session_id, {})
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            idle_minutes = int(session_config.get("group_idle_trigger_minutes", 0) or 0)
            idle_seconds = max(0, idle_minutes * 60)
            timer_meta = self._safe_timer_meta(timer, now)
            remaining_seconds = timer_meta["remaining_seconds"]
            target_time = timer_meta["target_time"]
            # 群沉默计时器更适合以“最后一条用户消息时间”作为窗口起点。
            last_message_time = self.plugin.last_message_times.get(
                normalized_session_id, 0
            )
            temp_state = self.plugin.session_temp_state.get(normalized_session_id, {})
            last_user_time = (
                temp_state.get("last_user_time") or last_message_time or None
            )
            # 若历史时间缺失，则根据剩余时间反推一个近似 started_at。
            started_at = last_user_time or (
                now - max(0, idle_seconds - (remaining_seconds or 0))
            )
            progress_percent = 0
            if idle_seconds > 0 and remaining_seconds is not None:
                consumed = max(0, idle_seconds - remaining_seconds)
                progress_percent = max(
                    0, min(100, round((consumed / idle_seconds) * 100))
                )

            group_cards.append(
                {
                    "session_id": normalized_session_id,
                    "session_name": self.plugin._get_session_name(
                        normalized_session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        normalized_session_id, session_config
                    ),
                    "session_category": self._detect_session_category(
                        normalized_session_id
                    ),
                    "source_mode": context_settings.get(
                        "source_mode", "platform_message_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times", 0
                    ),
                    "timer_kind": "group_silence",
                    "title": "群沉默检测",
                    # 群沉默卡的状态定义与 auto_trigger 保持一致，便于前端复用状态渲染逻辑。
                    "status": "running" if remaining_seconds is not None else "unknown",
                    "remaining_seconds": remaining_seconds,
                    "target_time": target_time,
                    "started_at": started_at if started_at else None,
                    "window_seconds": idle_seconds,
                    "progress_percent": progress_percent,
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "group_idle_trigger_minutes": idle_minutes,
                    "last_message_time": last_message_time or None,
                    "last_user_time": last_user_time,
                    # 显式标记这是实时群计时器，便于前端做差异化展示或调试。
                    "is_live_group_timer": True,
                }
            )

        # 统一按剩余时间升序排序，让最接近触发的卡片优先显示。
        auto_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        group_cards.sort(
            key=lambda item: (
                item.get("remaining_seconds") is None,
                item.get("remaining_seconds") or 0,
                item["session_id"],
            )
        )
        return {
            "auto_trigger_cards": auto_cards,
            "group_timer_cards": group_cards,
        }

    def _build_status_payload(self) -> dict[str, Any]:
        now = time.time()
        uptime_sec = max(0, int(now - self.plugin.plugin_start_time))
        timer_cards = self._collect_timer_cards(now)

        return {
            "running": True,
            # 版本来源按优先级依次回退，保证控制台总能显示一个可读值。
            "version": getattr(self.plugin, "version", None)
            or getattr(self.plugin, "__version__", None)
            or self._metadata_version
            or "未知版本",
            "uptime_seconds": uptime_sec,
            # uptime 使用 datetime 差值字符串，便于直接面向人类展示。
            "uptime": str(
                datetime.fromtimestamp(now)
                - datetime.fromtimestamp(self.plugin.plugin_start_time)
            ),
            "scheduler_running": bool(
                self.plugin.scheduler and self.plugin.scheduler.running
            ),
            "sessions_count": len(self.plugin.session_data),
            "auto_trigger_timers": len(self.plugin.auto_trigger_timers),
            "group_timers": len(self.plugin.group_timers),
            "jobs_count": len(self.plugin.scheduler.get_jobs())
            if self.plugin.scheduler
            else 0,
            # 计时器总数在前端可直接用于角标和标题，无需再做两次求和。
            "timer_cards_total": len(timer_cards["auto_trigger_cards"])
            + len(timer_cards["group_timer_cards"]),
            "auto_trigger_cards": timer_cards["auto_trigger_cards"],
            "group_timer_cards": timer_cards["group_timer_cards"],
            # 独立 WebSocket 服务已随 v1.3.0 移除，字段保留仅为前端兼容。
            "ws_connections": 0,
            # 时间戳用于前端判断数据新鲜度或手动刷新完成时间。
            "timestamp": datetime.now().isoformat(),
        }

    def _collect_jobs(self) -> list[dict[str, Any]]:
        if not self.plugin.scheduler:
            return []

        jobs = []
        for job in self.plugin.scheduler.get_jobs():
            session_id = str(job.id)
            session_data = self.plugin.session_data.get(session_id, {})
            session_config = self.plugin._get_session_config(session_id) or {}
            schedule_settings = session_config.get("schedule_settings", {})
            context_settings = session_config.get("context_settings", {})
            auto_trigger_settings = session_config.get("auto_trigger_settings", {})
            jobs.append(
                {
                    "id": session_id,
                    "session_name": self.plugin._get_session_name(
                        session_id, session_config
                    ),
                    "session_display_name": self.plugin._get_session_display_name(
                        session_id, session_config
                    ),
                    "session_category": self._detect_session_category(session_id),
                    "source_mode": context_settings.get(
                        "source_mode", "conversation_history"
                    ),
                    "max_unanswered_times": schedule_settings.get(
                        "max_unanswered_times",
                        auto_trigger_settings.get("max_unanswered_times", 0),
                    ),
                    # APScheduler 的 next_run_time 是 datetime，这里统一序列化为 ISO 字符串。
                    "next_run_time": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                    "unanswered_count": session_data.get("unanswered_count", 0),
                    "manual_trigger_in_progress": session_id
                    in self.plugin.manual_trigger_sessions,
                    # 以下字段用于前端推导进度条与调度窗口说明。
                    "next_trigger_time": session_data.get("next_trigger_time"),
                    "last_scheduled_at": session_data.get("last_scheduled_at"),
                    "last_schedule_min_interval_seconds": session_data.get(
                        "last_schedule_min_interval_seconds"
                    ),
                    "last_schedule_max_interval_seconds": session_data.get(
                        "last_schedule_max_interval_seconds"
                    ),
                    "last_schedule_random_interval_seconds": session_data.get(
                        "last_schedule_random_interval_seconds"
                    ),
                    # 透出当前会话配置中的调度区间与免打扰时段，供任务卡片展示。
                    "schedule_min_interval_minutes": schedule_settings.get(
                        "min_interval_minutes"
                    ),
                    "schedule_max_interval_minutes": schedule_settings.get(
                        "max_interval_minutes"
                    ),
                    "quiet_hours_enabled": schedule_settings.get(
                        "enable_quiet_hours", True
                    ),
                    "quiet_hours": schedule_settings.get("quiet_hours", ""),
                }
            )
        return jobs

    def _list_known_sessions(self) -> list[str]:
        sessions: set[str] = set()

        # 先收集全局配置里显式声明的会话。
        for scope_key in ("friend_settings", "group_settings"):
            cfg = self.config.get(scope_key, {})
            for session in cfg.get("session_list", []):
                if isinstance(session, str) and session:
                    sessions.add(self.plugin._normalize_session_id(session))

        # 再并入运行时数据与会话覆写记录，保证“曾经出现过”的会话也能在管理端看到。
        sessions.update(self.plugin.session_data.keys())
        sessions.update(self.plugin.session_override_manager.list_sessions())
        return sorted(sessions)

    def _list_known_session_summaries(self) -> list[dict[str, Any]]:
        """返回带展示信息的已知会话摘要。"""
        result: list[dict[str, Any]] = []
        for session in self._list_known_sessions():
            effective = self.plugin._get_session_config(session)
            result.append(
                {
                    "session": session,
                    "session_name": self.plugin._get_session_name(session, effective),
                    "session_display_name": self.plugin._get_session_display_name(
                        session, effective
                    ),
                    # has_override 让前端在摘要态就能知道这个会话是否存在局部改写。
                    "has_override": bool(
                        self.plugin.session_override_manager.get_override(session)
                    ),
                    "unanswered_count": self.plugin.session_data.get(session, {}).get(
                        "unanswered_count", 0
                    ),
                    "manual_trigger_in_progress": session
                    in self.plugin.manual_trigger_sessions,
                }
            )
        return result

    async def _build_notification_payload(self) -> dict[str, Any]:
        # 统一封装通知载荷构造，避免 HTTP 路由与各调用方重复拼装。
        if not getattr(self.plugin, "notification_center", None):
            return {
                "items": [],
                "meta": {
                    "unread_count": 0,
                    "last_sync_at": None,
                    "total_count": 0,
                },
            }
        return await self.plugin.notification_center.get_payload()

    def _save_plugin_config(self) -> None:
        try:
            # AstrBot 配置对象通常提供 save_config 方法，这里做鸭子类型兼容。
            if hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[主动消息] 保存配置失败喵: {e}")

    # ------------------------------------------------------------------
    # Markdown 文档白名单解析（自原 web_admin_server 迁移）
    # ------------------------------------------------------------------

    def _list_markdown_documents(self) -> list[dict[str, Any]]:
        """列出允许浏览的 Markdown 文档摘要。"""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        docs_root = (plugin_root / "docs").resolve()

        allowed_paths: list[Path] = []

        # 插件根目录只暴露顶层 Markdown 文档，避免把实现目录中的内部文档一并暴露出来。
        if plugin_root.exists():
            allowed_paths.extend(sorted(plugin_root.glob("*.md")))

        # docs 目录作为显式文档区，允许递归收集其中的所有 Markdown 文件。
        if docs_root.exists():
            allowed_paths.extend(sorted(docs_root.rglob("*.md")))

        for path in allowed_paths:
            if not path.is_file():
                continue

            try:
                # 所有路径统一转为插件工作区相对路径，方便前端展示与请求。
                relative_path = self._to_workspace_relative_path(path)
            except ValueError:
                # 若文件不在工作区内，说明超出允许范围，直接忽略。
                continue

            normalized = relative_path.replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)

            items.append(
                {
                    "path": normalized,
                    # title 面向展示，filename 更偏向调试或原始文件识别。
                    "title": path.stem,
                    "filename": path.name,
                    # category 便于前端未来按目录做分组；插件根目录下文件统一标为 root。
                    "category": "root"
                    if path.parent.resolve() == plugin_root
                    else path.parent.name,
                }
            )

        # 优先展示根目录文档，再按路径字母序排序，通常更符合 README / CHANGELOG 的阅读优先级。
        items.sort(
            key=lambda item: (
                0 if item["path"].count("/") == 0 else 1,
                item["path"].lower(),
            )
        )
        return items

    def _resolve_markdown_document(self, raw_path: str) -> Path | None:
        """将前端请求的 Markdown 相对路径解析为插件目录中的受信任文件。"""
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if not normalized or not normalized.lower().endswith(".md"):
            return None
        # 明确拒绝绝对路径与上级目录跳转，防止路径穿越访问到插件目录外的文件。
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
        ):
            return None

        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        docs_root = (plugin_root / "docs").resolve()
        candidate = (plugin_root / normalized).resolve()

        if not candidate.is_file():
            return None

        try:
            relative_path = candidate.relative_to(plugin_root)
        except ValueError:
            return None

        # 根目录仅允许访问顶层 Markdown；docs 目录允许访问其内部任意层级 Markdown。
        if relative_path.parent == Path("."):
            return candidate

        try:
            candidate.relative_to(docs_root)
            return candidate
        except ValueError:
            return None

    def _to_workspace_relative_path(self, path: Path) -> str:
        """将绝对路径转换为插件工作区内的相对路径。"""
        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        return str(path.resolve().relative_to(plugin_root)).replace("\\", "/")

    # ------------------------------------------------------------------
    # 内嵌页静态资源（admin/ 目录白名单）
    # ------------------------------------------------------------------

    def _resolve_asset(self, raw_path: str) -> tuple[Path, str] | None:
        """把前端请求的资源路径解析为 admin/ 目录内的受信任文件。

        Returns:
            (文件绝对路径, 归一化后的相对路径)；不合法或不存在时返回 None。
        """
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if not normalized:
            return None
        # 明确拒绝绝对路径与上级目录跳转，防止穿越到 admin/ 目录之外。
        if (
            normalized.startswith("/")
            or normalized.startswith("../")
            or "/../" in normalized
            or ":" in normalized
        ):
            return None

        suffix = Path(normalized).suffix.lower()
        if suffix not in _ASSET_ALLOWED_SUFFIXES:
            return None

        plugin_root = Path(__file__).resolve().parent.parent.resolve()
        admin_root = (plugin_root / "admin").resolve()
        candidate = (admin_root / normalized).resolve()

        try:
            candidate.relative_to(admin_root)
        except ValueError:
            return None

        if not candidate.is_file():
            # 发布包中 JSX 已由 CI 预编译为同名 .js，这里自动回退，兼容两种产物形态。
            if suffix == ".jsx":
                js_candidate = candidate.with_suffix(".js")
                if js_candidate.is_file():
                    return js_candidate, normalized[:-4] + ".js"
            return None

        return candidate, normalized

    # ------------------------------------------------------------------
    # HTTP 处理器
    # ------------------------------------------------------------------

    async def api_status(self) -> Any:
        """汇总插件运行状态、计时器与任务数，供首页卡片与轮询逻辑使用。"""
        try:
            return self._ok(self._build_status_payload())
        except Exception as e:
            logger.error(f"[主动消息] 构造状态载荷失败喵: {e}")
            return self._err("获取运行状态失败")

    async def api_get_config(self) -> Any:
        """返回全部 5 个一级配置组；web_admin 组显式过滤密码字段。

        注意：本接口的载荷是“配置组名 -> 对象”的映射，前端会按顶层键遍历，
        因此成功时不能附加 ok 信封字段（否则 ok 会被当作一个配置组处理）。
        失败时才使用 {"ok": False, "error": ...} 信封。
        """
        try:
            web_admin = {
                k: v
                for k, v in self.config.get("web_admin", {}).items()
                if k != "password"
            }
            return json_response(
                {
                    "friend_settings": dict(self.config.get("friend_settings", {})),
                    "group_settings": dict(self.config.get("group_settings", {})),
                    "web_admin": web_admin,
                    "notification_settings": dict(
                        self.config.get("notification_settings", {})
                    ),
                    "telemetry_config": dict(self.config.get("telemetry_config", {})),
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 读取配置失败喵: {e}")
            return self._err("读取配置失败")

    async def api_update_config(self) -> Any:
        """保存全局配置；白名单覆盖全部 5 个一级配置组。"""
        try:
            payload = await self._json_body()
            allowed_keys = {
                "friend_settings",
                "group_settings",
                "web_admin",
                "notification_settings",
                "telemetry_config",
            }
            for key in allowed_keys:
                if key not in payload:
                    continue
                if not isinstance(payload[key], dict):
                    return self._err(f"配置组 {key} 必须是对象")
                if key == "web_admin":
                    # web_admin 采用增量合并，避免未提交字段被整个覆盖掉。
                    old = dict(self.config.get("web_admin", {}))
                    old.update(payload.get("web_admin", {}))
                    # 密码字段允许显式更新，但不会通过 get_config 回传给前端。
                    if "password" in payload.get("web_admin", {}):
                        old["password"] = payload["web_admin"]["password"]
                    self.config["web_admin"] = old
                else:
                    # 其余配置组按前端提交的完整对象直接替换。
                    self.config[key] = payload[key]

            self._save_plugin_config()
            return self._ok({"message": "全局配置已保存"})
        except Exception as e:
            logger.error(f"[主动消息] 保存配置失败喵: {e}")
            return self._err("保存配置失败")

    async def api_get_config_schema(self) -> Any:
        """返回 _conf_schema.json，前端据此动态渲染配置表单。

        与 GET /config 同理：schema 是“配置组名 -> 定义”的映射且会被前端
        整体遍历，成功时必须原样返回，不能附加 ok 信封字段。
        """
        try:
            schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
            if schema_path.exists():
                try:
                    # Schema 文件可能较大，放在线程池读取，减少主循环阻塞。
                    schema_text = await asyncio.to_thread(
                        schema_path.read_text, encoding="utf-8"
                    )
                    return json_response(json.loads(schema_text))
                except Exception as e:
                    logger.error(f"[主动消息] 读取 Schema 失败喵: {e}")
            return json_response({})
        except Exception as e:
            logger.error(f"[主动消息] 返回配置 Schema 失败喵: {e}")
            return self._err("读取配置 Schema 失败")

    async def api_list_session_configs(self) -> Any:
        """汇总所有已知会话，给前端会话差异配置页做选择器与列表展示。"""
        try:
            sessions = self._list_known_sessions()
            result = []
            for session in sessions:
                override = self.plugin.session_override_manager.get_override(session)
                effective = self.plugin._get_session_config(session)
                session_name = self.plugin._get_session_name(session, effective)
                result.append(
                    {
                        "session": session,
                        "session_name": session_name,
                        "session_display_name": self.plugin._get_session_display_name(
                            session, effective
                        ),
                        # 标记是否存在会话级覆写，前端可据此展示提示标签。
                        "has_override": bool(override),
                        # 额外把 override keys 暴露给前端，便于提示“哪些配置项被会话级改写”。
                        "override_keys": list(override.keys()),
                        # effective 可能为空，因此这里需要防御式布尔判断。
                        "enabled": bool(effective and effective.get("enable", False)),
                        # 从运行时会话数据中拿到下一次触发时间，用于列表辅助信息展示。
                        "next_trigger_time": self.plugin.session_data.get(
                            session, {}
                        ).get("next_trigger_time"),
                        "unanswered_count": self.plugin.session_data.get(
                            session, {}
                        ).get("unanswered_count", 0),
                    }
                )
            return self._ok({"sessions": result})
        except Exception as e:
            logger.error(f"[主动消息] 列举会话失败喵: {e}")
            return self._err("获取会话列表失败")

    async def api_session_config(self, umo: str = "") -> Any:
        """会话差异配置的读取 / 保存 / 清空，同一个路由按方法分发。"""
        try:
            try:
                method = str(request.method or "GET").upper()
            except Exception:
                method = "GET"
            if method == "POST":
                return await self._update_session_config(umo)
            if method == "DELETE":
                return await self._reset_session_config(umo)
            return await self._get_session_config(umo)
        except Exception as e:
            logger.error(f"[主动消息] 会话差异配置处理失败喵: {e}")
            return self._err("会话差异配置处理失败")

    def _decode_umo_param(self, umo: str) -> str:
        """解码路径参数中的会话 UMO，容忍双重编码。

        AstrBot 插件页 bridge 会对转发路径逐段编码一次；旧版前端还可能
        传入预编码后的值，叠加后服务端解一次码仍残留 %3A 字样。正常 UMO
        不含百分号转义，因此仅在原样解析失败、且解码后可被解析时才采用
        解码结果，避免误伤包含字面 % 的会话 ID。
        """
        umo = str(umo or "")
        parse = getattr(self.plugin, "_parse_session_id", None)
        if not callable(parse):
            return umo

        try:
            if parse(umo):
                return umo
        except Exception:
            pass

        try:
            decoded = unquote(umo)
        except Exception:
            return umo
        if decoded == umo:
            return umo
        try:
            return decoded if parse(decoded) else umo
        except Exception:
            return umo

    async def _get_session_config(self, umo: str) -> Any:
        # 路径参数使用 path 转换器，允许会话 ID 中包含特殊字符。
        normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
        base = self.plugin._get_base_session_config(normalized)
        return self._ok(
            {
                "session": normalized,
                # base 表示命中 friend/group 全局配置后的基础配置。
                "base": base,
                # override 是该会话显式保存的差异字段。
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                # effective 是基础配置与覆写合并后的最终生效配置。
                "effective": self.plugin._get_session_config(normalized),
            }
        )

    async def _update_session_config(self, umo: str) -> Any:
        normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
        payload = await self._json_body()
        # mode 用于兼容两种写法：直接提交 override，或提交最终 effective 配置。
        mode = payload.get("mode", "effective")

        if mode == "override":
            override = payload.get("override", {})
            if not isinstance(override, dict):
                return self._err("override 必须是对象")
            # override 模式由前端显式提交差异配置，后端不再做反推。
            await self.plugin.session_override_manager.set_override(
                normalized, override
            )
        else:
            effective = payload.get("effective", {})
            if not isinstance(effective, dict):
                return self._err("effective 必须是对象")
            base = self.plugin._get_base_session_config(normalized)
            if not base:
                # 没有基础配置时无法反推出差异项，因此拒绝保存 effective。
                return self._err("会话未命中 friend/group 全局配置，无法保存 effective")
            await self.plugin.session_override_manager.update_session_from_effective(
                normalized,
                base,
                effective,
            )

        return self._ok(
            {
                "session": normalized,
                "override": self.plugin.session_override_manager.get_override(
                    normalized
                ),
                "effective": self.plugin._get_session_config(normalized),
                "message": "会话差异配置已保存",
            }
        )

    async def _reset_session_config(self, umo: str) -> Any:
        # 删除覆写后，会话会重新完全继承全局配置。
        normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
        await self.plugin.session_override_manager.delete_override(normalized)
        return self._ok(
            {
                "session": normalized,
                "override": {},
                "effective": self.plugin._get_session_config(normalized),
                "message": "已清空该会话的差异配置",
            }
        )

    async def api_list_jobs(self) -> Any:
        """返回调度器中的待执行任务列表，供任务页卡片展示。"""
        try:
            return self._ok({"jobs": self._collect_jobs()})
        except Exception as e:
            logger.error(f"[主动消息] 列举任务失败喵: {e}")
            return self._err("获取任务列表失败")

    async def api_reschedule_job(self, umo: str = "") -> Any:
        """重新调度指定会话的下一次主动消息时间。"""
        try:
            normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
            session_config = self.plugin._get_session_config(normalized)
            if not session_config or not session_config.get("enable", False):
                return self._err(
                    "会话未启用或配置不存在，无法重新调度",
                    session=normalized,
                )

            await self.plugin._schedule_next_chat_and_save(
                normalized, reset_counter=False
            )
            return self._ok(
                {
                    "session": normalized,
                    "message": "已重新调度下一次主动消息时间",
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 重新调度失败喵: {e}")
            return self._err("重新调度失败")

    async def api_trigger_job(self, umo: str = "") -> Any:
        """立即手动触发一次指定会话的检查与发言流程。"""
        try:
            normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
            if normalized in self.plugin.manual_trigger_sessions:
                return self._err(
                    "该任务正在立即触发中，请等待当前执行完成",
                    session=normalized,
                    in_progress=True,
                )

            self.plugin.manual_trigger_sessions.add(normalized)
            # 主动创建后台任务，避免前端请求长时间挂起等待业务执行完成。
            asyncio.create_task(self.plugin.check_and_chat(normalized))
            return self._ok(
                {
                    "session": normalized,
                    "in_progress": True,
                    "message": "已开始立即触发，正在等待 LLM 完成回复",
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 立即触发失败喵: {e}")
            return self._err("立即触发失败")

    async def api_cancel_job(self, umo: str = "") -> Any:
        """取消指定会话的调度任务；任务不存在时保持幂等。"""
        try:
            normalized = self.plugin._normalize_session_id(self._decode_umo_param(umo))
            removed = False
            try:
                # APScheduler 中的 job id 直接使用规范化后的 session id。
                self.plugin.scheduler.remove_job(normalized)
                removed = True
            except Exception:
                # 任务不存在时保持幂等，不把异常直接抛给前端。
                pass

            async with self.plugin.data_lock:
                if normalized in self.plugin.session_data:
                    # 同步清理持久化数据中的 next_trigger_time，避免界面显示过期信息。
                    self.plugin.session_data[normalized].pop("next_trigger_time", None)
                    await self.plugin._save_data_internal()

            if removed:
                logger.info(
                    f"[主动消息] 管理页已取消 {self.plugin._get_session_log_str(normalized)} 的调度任务喵。"
                )
            else:
                logger.warning(
                    f"[主动消息] 管理页请求取消 {self.plugin._get_session_log_str(normalized)} 的调度任务喵，但当前未找到可取消任务。"
                )

            return self._ok({"session": normalized, "removed": removed})
        except Exception as e:
            logger.error(f"[主动消息] 取消任务失败喵: {e}")
            return self._err("取消任务失败")

    async def api_get_notifications(self) -> Any:
        """通知列表统一从插件本地缓存读取，前端不直接访问外部通知平台。"""
        try:
            # 复用统一的通知载荷构造函数，确保接口输出结构一致。
            return self._ok(await self._build_notification_payload())
        except Exception as e:
            logger.error(f"[主动消息] 读取通知失败喵: {e}")
            return self._err("获取通知列表失败")

    async def api_mark_notification_read(self) -> Any:
        """单条已读只影响插件本地缓存中的 read_map，不涉及远端接口写回。"""
        try:
            if not getattr(self.plugin, "notification_center", None):
                return self._err("通知系统不可用")

            payload = await self._json_body()
            notification_id = payload.get("id")
            if notification_id is None:
                return self._err("缺少必填字段 id")
            try:
                # 前端传值可能是字符串，因此这里统一转成 int，方便下游逻辑处理。
                normalized_id = int(notification_id)
            except (TypeError, ValueError):
                return self._err("id 必须是数字")

            result = await self.plugin.notification_center.mark_as_read(normalized_id)
            return self._ok(result if isinstance(result, dict) else {})
        except Exception as e:
            logger.error(f"[主动消息] 标记通知已读失败喵: {e}")
            return self._err("标记已读失败")

    async def api_mark_all_notifications_read(self) -> Any:
        """批量已读，未读角标同步归零。"""
        try:
            if not getattr(self.plugin, "notification_center", None):
                return self._err("通知系统不可用")
            result = await self.plugin.notification_center.mark_all_as_read()
            return self._ok(result if isinstance(result, dict) else {})
        except Exception as e:
            logger.error(f"[主动消息] 全部已读失败喵: {e}")
            return self._err("全部已读失败")

    async def api_refresh_notifications(self) -> Any:
        """供“立即同步”按钮调用，强制拉取远端最新通知并回传完整快照。"""
        try:
            if not getattr(self.plugin, "notification_center", None):
                return self._err("通知系统不可用")
            changed = await self.plugin.notification_center.refresh()
            payload = await self.plugin.notification_center.get_payload()
            return self._ok(
                {
                    "changed": changed,
                    "items": payload.get("items", []),
                    "meta": payload.get("meta", {}),
                    "message": "通知已同步",
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 同步通知失败喵: {e}")
            return self._err("同步通知失败")

    async def api_list_markdown_files(self) -> Any:
        """仅暴露插件目录内明确允许浏览的 Markdown 文档。"""
        try:
            return self._ok({"items": self._list_markdown_documents()})
        except Exception as e:
            logger.error(f"[主动消息] 列举文档失败喵: {e}")
            return self._err("获取文档列表失败")

    async def api_get_markdown_file(self, file_path: str = "") -> Any:
        """读取白名单内的 Markdown 文档内容。"""
        try:
            # path 转换器已完成一次 URL 解码，这里直接交给白名单解析。
            resolved = self._resolve_markdown_document(file_path)
            if not resolved:
                return self._err("文档不存在或不允许访问")

            try:
                # 文件读取放到线程池中执行，避免阻塞事件循环。
                content = await asyncio.to_thread(resolved.read_text, encoding="utf-8")
            except UnicodeDecodeError:
                # 前端当前只按 UTF-8 渲染 Markdown；若编码不匹配，直接返回可理解错误提示。
                return self._err("文档编码不受支持，仅支持 UTF-8 Markdown 文件")
            except Exception as e:
                logger.error(f"[主动消息] 读取 Markdown 文档失败喵: {e}")
                return self._err("读取文档失败")

            return self._ok(
                {
                    # path 返回工作区相对路径，便于前端做目录列表高亮和当前文档定位。
                    "path": self._to_workspace_relative_path(resolved),
                    # title 直接取 stem，减少前端再做文件名拆分。
                    "title": resolved.stem,
                    # content 保留原始 Markdown 文本，由前端统一负责渲染。
                    "content": content,
                    # 显式告诉前端这是 Markdown 内容，方便后续复用统一渲染管线。
                    "content_format": "markdown",
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 返回文档失败喵: {e}")
            return self._err("读取文档失败")

    async def api_open_directory(self) -> Any:
        """允许前端请求打开插件目录或数据目录，便于管理员快速定位文件。"""
        try:
            payload = await self._json_body()
            target = str(payload.get("path", "plugin")).strip().lower()
            if target == "data":
                directory = Path(self.plugin.data_dir)
            else:
                # 默认回退到插件根目录，保证前端传值异常时仍有一个安全目标。
                directory = Path(__file__).resolve().parent.parent

            # 确保目录存在，再根据当前系统选择合适的打开方式。
            directory.mkdir(parents=True, exist_ok=True)
            dir_str = str(directory)

            if _is_running_in_docker():
                return self._err(
                    "Docker 环境下不支持在宿主机直接打开目录，请手动查看挂载路径",
                    path=dir_str,
                )

            if os.name == "nt":
                # Windows 使用系统默认资源管理器，封装为异步避免阻塞事件循环。
                await asyncio.to_thread(os.startfile, dir_str)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                # macOS 通过 open 命令调起 Finder；失败时把 stderr 带回前端便于定位。
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "未知错误").strip()
                    return self._err(
                        "打开目录失败（macOS）",
                        message=f"open 命令执行失败: {detail}",
                        path=dir_str,
                    )
            else:
                # 其它类 Unix 系统优先尝试 xdg-open，兼容常见 Linux 桌面环境。
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["xdg-open", dir_str],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "未知错误").strip()
                    return self._err(
                        "打开目录失败（Linux）",
                        message=(
                            "xdg-open 执行失败，服务器可能缺少桌面环境或未安装 xdg-open: "
                            f"{detail}"
                        ),
                        path=dir_str,
                    )

            return self._ok(
                {
                    "path": dir_str,
                    "message": "已在系统文件管理器中打开目录",
                }
            )
        except FileNotFoundError as e:
            logger.error(f"[主动消息] 打开目录失败（命令缺失）喵: {e}")
            return self._err("打开目录失败：系统缺少所需命令")
        except PermissionError as e:
            logger.error(f"[主动消息] 打开目录失败（权限不足）喵: {e}")
            return self._err("打开目录失败：权限不足", message=str(e))
        except Exception as e:
            logger.error(f"[主动消息] 打开目录失败喵: {e}")
            return self._err("打开目录失败", message=str(e))

    async def api_get_asset(self, file_path: str = "") -> Any:
        """向内嵌管理页下发 admin/ 目录内的前端源码与资源。"""
        try:
            resolved = self._resolve_asset(file_path)
            if not resolved:
                return self._err("资源不存在或不允许访问")
            candidate, normalized = resolved

            try:
                data = await asyncio.to_thread(candidate.read_bytes)
            except Exception as e:
                logger.error(f"[主动消息] 读取管理页资源失败喵: {e}")
                return self._err("读取资源失败")

            if len(data) > _ASSET_MAX_BYTES:
                return self._err("资源超过大小限制")

            if candidate.suffix.lower() == ".png":
                # 二进制资源以 data URL 下发，前端可直接赋给 img.src。
                encoded = base64.b64encode(data).decode("ascii")
                return self._ok(
                    {
                        "path": normalized,
                        "kind": "dataurl",
                        "content": f"data:image/png;base64,{encoded}",
                    }
                )

            return self._ok(
                {
                    "path": normalized,
                    "kind": "text",
                    "content": data.decode("utf-8"),
                }
            )
        except Exception as e:
            logger.error(f"[主动消息] 下发管理页资源失败喵: {e}")
            return self._err("读取资源失败")

    # ------------------------------------------------------------------
    # 广播占位
    # ------------------------------------------------------------------

    async def broadcast(self, reason: str) -> None:
        """更新广播占位：独立 WebSocket 服务已移除，保留调用位以兼容旧调用点。"""
        del reason  # 无实时通道，仅为兼容保留；前端依靠轮询获取更新。
