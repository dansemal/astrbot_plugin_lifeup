"""
LifeUp (人升) HTTP API 异步客户端封装。

基于 LifeUp SDK 源码与官方 API 文档，封装了所有 HTTP 查询端点
以及所有 ``lifeup://api/`` URL Scheme 动作接口。

两种通信模式
--------------
1. **HTTP GET 查询** —— 直接请求 REST 端点返回 JSON 数据
2. **URL Scheme 动作** —— 将 ``lifeup://api/<action>?<params>`` 格式的
   URL 数组 POST 到 ``/api/contentprovider`` 执行写操作

SDK 源码参考：
https://github.com/Ayagikei/LifeUp-SDK/blob/main/http/src/main/java/net/lifeupapp/lifeup/http/service/KtorService.kt
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 公共返回包装
# ---------------------------------------------------------------------------


def _success(data: Any = None) -> dict[str, Any]:
    return {"status": "success", "data": data, "message": ""}


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "data": None, "message": message}


# ---------------------------------------------------------------------------
# URL Scheme 构建辅助
# ---------------------------------------------------------------------------


def _build_url(action: str, params: dict[str, Any]) -> str:
    """将动作名与参数字典编码为 ``lifeup://api/<action>?...`` 字符串。"""
    filtered: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, list):
            # 空列表跳过
            if not v:
                continue
            filtered[k] = v
        elif isinstance(v, bool):
            filtered[k] = "true" if v else "false"
        else:
            s = str(v)
            if s:
                filtered[k] = s
    query = urlencode(filtered, doseq=True, safe=",")
    return f"lifeup://api/{action}?{query}" if query else f"lifeup://api/{action}"


# ---------------------------------------------------------------------------
# LifeUpClient
# ---------------------------------------------------------------------------


class LifeUpClient:
    """LifeUp HTTP API 异步客户端。

n    Args:
        api_url: LifeUp API 根地址，如 ``http://localhost:13276``。
        api_token: 可选鉴权 Token（在 LifeUp 云设置中配置安全密钥时必填）。
        timeout: 请求超时秒数。
    """

    def __init__(
        self,
        api_url: str,
        api_token: str = "",
        timeout: int = 5,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    # -- headers --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_token:
            h["Authorization"] = self.api_token
        return h

    # -- 内部请求 --------------------------------------------------------------

    async def _get_json(self, endpoint: str) -> dict[str, Any]:
        """发送 GET 请求并解析返回 JSON。"""
        url = f"{self.api_url}{endpoint}"
        try:
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                headers=self._headers(),
            ) as session:
                async with session.get(url) as resp:
                    data: Any = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        return data
                    return _success(data)
        except asyncio.TimeoutError as exc:
            logger.error("GET %s 超时: %s", endpoint, exc)
            return _error(
                f"请求 LifeUp API 超时，请确认：\n"
                f"1. LifeUp App 已开启 HTTP API 服务\n"
                f"2. 地址 {self.api_url} 可访问\n"
                f"3. 手机和 AstrBot 在同一网络"
            )
        except aiohttp.ClientError as exc:
            logger.error("GET %s 连接失败: %s", endpoint, exc)
            return _error(
                f"无法连接到 LifeUp API ({self.api_url})，"
                f"请检查配置和网络连接。详情: {exc}"
            )
        except Exception as exc:
            logger.error("GET %s 异常: %s", endpoint, exc)
            return _error(f"请求异常: {exc}")

    async def _post_urls(self, urls: list[str]) -> dict[str, Any]:
        """将 URL Scheme 数组 POST 到 ``/api/contentprovider`` 执行动作。"""
        payload: dict[str, Any] = {"urls": urls}
        try:
            async with aiohttp.ClientSession(
                timeout=self.timeout,
                headers=self._headers(),
            ) as session:
                async with session.post(
                    f"{self.api_url}/api/contentprovider",
                    json=payload,
                ) as resp:
                    data: Any = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        return data
                    return _success(data)
        except asyncio.TimeoutError as exc:
            logger.error("POST /api/contentprovider 超时: %s", exc)
            return _error(
                f"请求 LifeUp API 超时，请确认：\n"
                f"1. LifeUp App 已开启 HTTP API 服务\n"
                f"2. 地址 {self.api_url} 可访问"
            )
        except aiohttp.ClientError as exc:
            logger.error("POST /api/contentprovider 连接失败: %s", exc)
            return _error(f"无法连接到 LifeUp API: {exc}")
        except Exception as exc:
            logger.error("POST /api/contentprovider 异常: %s", exc)
            return _error(f"请求异常: {exc}")

    # ==================================================================
    #  1) 查询端点（HTTP GET） — 基于 SDK KtorService.kt
    # ==================================================================

    # -- tasks --

    async def query_tasks(
        self, category_id: int | None = None,
    ) -> dict[str, Any]:
        """查询任务列表。

        端点：``GET /tasks`` 或 ``GET /tasks/{category_id}``
        """
        endpoint = f"/tasks/{category_id}" if category_id is not None else "/tasks"
        return await self._get_json(endpoint)

    # -- history --

    async def query_history(
        self, offset: int = 0, limit: int = 100, gid: int | None = None,
    ) -> dict[str, Any]:
        """查询历史记录。

        端点：``GET /history?offset={}&limit={}&gid={}``
        """
        params: dict[str, str] = {"offset": str(offset), "limit": str(limit)}
        if gid is not None:
            params["gid"] = str(gid)
        qs = urlencode(params)
        return await self._get_json(f"/history?{qs}")

    # -- items --

    async def query_items(
        self, list_id: int | None = None, ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """查询商品列表。

n        端点：``GET /items``、``GET /items?id=1&id=2`` 或 ``GET /items/{listId}``
        """
        if ids:
            qs = "&".join(f"id={i}" for i in ids)
            return await self._get_json(f"/items?{qs}")
        if list_id is not None:
            return await self._get_json(f"/items/{list_id}")
        return await self._get_json("/items")

    # -- tasks_categories --

    async def query_tasks_categories(self) -> dict[str, Any]:
        """查询任务清单分类列表。

        端点：``GET /tasks_categories``
        """
        return await self._get_json("/tasks_categories")

    # -- achievement_categories --

    async def query_achievement_categories(self) -> dict[str, Any]:
        """查询成就分类列表。

        端点：``GET /achievement_categories``
        """
        return await self._get_json("/achievement_categories")

    # -- items_categories --

    async def query_items_categories(self) -> dict[str, Any]:
        """查询商品分类列表。

        端点：``GET /items_categories``
        """
        return await self._get_json("/items_categories")

    # -- info --

    async def query_info(self) -> dict[str, Any]:
        """查询应用信息。

        端点：``GET /info``
        """
        return await self._get_json("/info")

    # -- skills --

    async def query_skills(self) -> dict[str, Any]:
        """查询属性（技能）列表。

        端点：``GET /skills``
        """
        return await self._get_json("/skills")

    # -- achievements --

    async def query_achievements(
        self, category_id: int | None = None,
    ) -> dict[str, Any]:
        """查询成就列表。

        端点：``GET /achievements`` 或 ``GET /achievements/{category_id}``
        """
        endpoint = f"/achievements/{category_id}" if category_id is not None else "/achievements"
        return await self._get_json(endpoint)

    # -- feelings --

    async def query_feelings(
        self, offset: int = 0, limit: int = 100,
    ) -> dict[str, Any]:
        """查询感想列表。

        端点：``GET /feelings?offset={}&limit={}``
        """
        qs = urlencode({"offset": str(offset), "limit": str(limit)})
        return await self._get_json(f"/feelings?{qs}")

    # -- synthesis --

    async def query_synthesis(
        self, category_id: int | None = None,
    ) -> dict[str, Any]:
        """查询合成配方列表。

        端点：``GET /synthesis`` 或 ``GET /synthesis/{category_id}``
        """
        endpoint = f"/synthesis/{category_id}" if category_id is not None else "/synthesis"
        return await self._get_json(endpoint)

    # -- synthesis_categories --

    async def query_synthesis_categories(
        self, parent_id: int | None = None,
    ) -> dict[str, Any]:
        """查询合成配方分类列表。

        端点：``GET /synthesis_categories`` 或 ``GET /synthesis_categories/{id}``
        """
        endpoint = f"/synthesis_categories/{parent_id}" if parent_id is not None else "/synthesis_categories"
        return await self._get_json(endpoint)

    # -- pomodoro_records --

    async def query_pomodoro_records(
        self,
        offset: int = 0,
        limit: int = 100,
        time_range_start: int | None = None,
        time_range_end: int | None = None,
    ) -> dict[str, Any]:
        """查询番茄钟记录。

        端点：``GET /pomodoro_records?offset={}&limit={}&time_range_start={}&time_range_end={}``
        """
        params: dict[str, str] = {"offset": str(offset), "limit": str(limit)}
        if time_range_start is not None:
            params["time_range_start"] = str(time_range_start)
        if time_range_end is not None:
            params["time_range_end"] = str(time_range_end)
        qs = urlencode(params)
        return await self._get_json(f"/pomodoro_records?{qs}")

    # -- coin --

    async def query_coin(self) -> dict[str, Any]:
        """查询金币信息。

        端点：``GET /coin``（内部调用 ``query?key=coin``）
        """
        return await self._get_json("/coin")

    # -- data/export --

    async def export_backup(self, with_media: bool = True) -> dict[str, Any]:
        """导出数据备份。

        端点：``GET /data/export?withMedia={}``
        """
        qs = urlencode({"withMedia": "true" if with_media else "false"})
        return await self._get_json(f"/data/export?{qs}")

    # ==================================================================
    #  2) URL Scheme 动作（写操作）
    # ==================================================================

    # -----------------------------------------------------------------
    # 2.1 任务管理
    # -----------------------------------------------------------------

    async def add_task(
        self,
        todo: str,
        notes: str = "",
        coin: int | None = None,
        coin_var: int | None = None,
        exp: int | None = None,
        skills: list[int] | None = None,
        category: int | None = None,
        item_name: str = "",
        frequency: int | None = None,
        task_type: int | None = None,
        count: int | None = None,
        item_id: int | None = None,
        reminder: str = "",
        deadline: int | None = None,
        start_date: int | None = None,
        repetition_period: int | None = None,
        auto_check: bool | None = None,
        end_count: int | None = None,
        freeze: bool | None = None,
        freeze_time: int | None = None,
        display_order: int | None = None,
        notification: str = "",
        secrecy: int | None = None,
        task_check_items: list[str] | None = None,
    ) -> dict[str, Any]:
        """添加新任务。"""
        url = _build_url("add_task", {
            "todo": todo,
            "notes": notes,
            "coin": coin,
            "coin_var": coin_var,
            "exp": exp,
            "skills": skills,
            "category": category,
            "item_name": item_name,
            "frequency": frequency,
            "type": task_type,
            "count": count,
            "item_id": item_id,
            "reminder": reminder,
            "deadline": deadline,
            "startDate": start_date,
            "repetition_period": repetition_period,
            "auto_check": auto_check,
            "end_count": end_count,
            "freeze": freeze,
            "freeze_time": freeze_time,
            "display_order": display_order,
            "notification": notification,
            "secrecy": secrecy,
            "task_check_items": task_check_items,
        })
        return await self._post_urls([url])

    async def complete_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
        reward_factor: float = 1.0,
    ) -> dict[str, Any]:
        """完成任务。"""
        key, val = ("id", task_id) if task_id is not None else ("name", name)
        url = _build_url("complete", {
            key: val,
            "reward_factor": reward_factor,
            "ui": True,
        })
        return await self._post_urls([url])

    async def give_up_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """放弃任务。"""
        key, val = ("id", task_id) if task_id is not None else ("name", name)
        url = _build_url("give_up", {key: val})
        return await self._post_urls([url])

    async def freeze_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """冻结任务。"""
        key, val = ("id", task_id) if task_id is not None else ("name", name)
        url = _build_url("freeze", {key: val})
        return await self._post_urls([url])

    async def unfreeze_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """解冻任务。"""
        key, val = ("id", task_id) if task_id is not None else ("name", name)
        url = _build_url("unfreeze", {key: val})
        return await self._post_urls([url])

    async def delete_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """删除任务。"""
        key, val = ("id", task_id) if task_id is not None else ("name", name)
        url = _build_url("delete_task", {key: val})
        return await self._post_urls([url])

    async def edit_task(
        self,
        task_id: int | None = None,
        name: str | None = None,
        todo: str | None = None,
        notes: str | None = None,
        coin: int | None = None,
        coin_var: int | None = None,
        exp: int | None = None,
        skills: list[int] | None = None,
        category: int | None = None,
        frequency: int | None = None,
        task_type: int | None = None,
        count: int | None = None,
        item_id: int | None = None,
        reminder: str | None = None,
        deadline: int | None = None,
        start_date: int | None = None,
        repetition_period: int | None = None,
        auto_check: bool | None = None,
        end_count: int | None = None,
        freeze: bool | None = None,
        freeze_time: int | None = None,
        display_order: int | None = None,
        notification: str | None = None,
        secrecy: int | None = None,
    ) -> dict[str, Any]:
        """编辑已有任务。

        至少需要 ``task_id`` 或 ``name`` 指定目标，其他字段仅传入需要修改的项。
        """
        params: dict[str, Any] = {}
        if task_id is not None:
            params["id"] = task_id
        if name is not None:
            params["name"] = name
        if todo is not None:
            params["todo"] = todo
        if notes is not None:
            params["notes"] = notes
        if coin is not None:
            params["coin"] = coin
        if coin_var is not None:
            params["coin_var"] = coin_var
        if exp is not None:
            params["exp"] = exp
        if skills is not None:
            params["skills"] = skills
        if category is not None:
            params["category"] = category
        if frequency is not None:
            params["frequency"] = frequency
        if task_type is not None:
            params["type"] = task_type
        if count is not None:
            params["count"] = count
        if item_id is not None:
            params["item_id"] = item_id
        if reminder is not None:
            params["reminder"] = reminder
        if deadline is not None:
            params["deadline"] = deadline
        if start_date is not None:
            params["startDate"] = start_date
        if repetition_period is not None:
            params["repetition_period"] = repetition_period
        if auto_check is not None:
            params["auto_check"] = auto_check
        if end_count is not None:
            params["end_count"] = end_count
        if freeze is not None:
            params["freeze"] = freeze
        if freeze_time is not None:
            params["freeze_time"] = freeze_time
        if display_order is not None:
            params["display_order"] = display_order
        if notification is not None:
            params["notification"] = notification
        if secrecy is not None:
            params["secrecy"] = secrecy

        url = _build_url("edit_task", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.2 经济管理
    # -----------------------------------------------------------------

    async def reward(
        self,
        type_: str,
        content: str,
        number: int | None = None,
        skills: list[int] | None = None,
        item_id: int | None = None,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """奖励金币/经验/物品。"""
        url = _build_url("reward", {
            "type": type_,
            "content": content,
            "number": number,
            "skills": skills,
            "item_id": item_id,
            "item_name": item_name,
        })
        return await self._post_urls([url])

    async def penalty(
        self,
        type_: str,
        content: str,
        number: int | None = None,
    ) -> dict[str, Any]:
        """惩罚扣除。"""
        url = _build_url("penalty", {
            "type": type_,
            "content": content,
            "number": number,
        })
        return await self._post_urls([url])

    async def deposit(self, amount: int) -> dict[str, Any]:
        """ATM 存款。"""
        url = _build_url("deposit", {"amount": amount})
        return await self._post_urls([url])

    async def withdraw(self, amount: int) -> dict[str, Any]:
        """ATM 取款。"""
        url = _build_url("withdraw", {"amount": amount})
        return await self._post_urls([url])

    async def edit_coin(
        self, operation: str, value: int,
    ) -> dict[str, Any]:
        """直接编辑金币余额。

        Args:
            operation: ``increase`` / ``decrease`` / ``set``
            value: 数值
        """
        url = _build_url("edit_coin", {"operation": operation, "value": value})
        return await self._post_urls([url])

    async def edit_exp(
        self,
        skill_id: int,
        operation: str = "set",
        value: int = 0,
    ) -> dict[str, Any]:
        """直接编辑属性经验值/等级。

        Args:
            skill_id: 属性ID
            operation: ``increase`` / ``decrease`` / ``set``
            value: 数值
        """
        url = _build_url("edit_exp", {
            "skill_id": skill_id,
            "operation": operation,
            "value": value,
        })
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.3 物品管理
    # -----------------------------------------------------------------

    async def purchase_item(
        self,
        item_id: int | None = None,
        item_name: str | None = None,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """购买物品。"""
        key, val = ("id", item_id) if item_id is not None else ("name", item_name)
        url = _build_url("purchase_item", {key: val, "purchase_quantity": quantity})
        return await self._post_urls([url])

    async def use_item(
        self,
        item_id: int | None = None,
        item_name: str | None = None,
        use_times: int = 1,
    ) -> dict[str, Any]:
        """使用物品。"""
        key, val = ("id", item_id) if item_id is not None else ("name", item_name)
        url = _build_url("use_item", {key: val, "use_times": use_times})
        return await self._post_urls([url])

    async def synthesize(
        self, synthesis_id: int, times: int = 1,
    ) -> dict[str, Any]:
        """合成配方。"""
        url = _build_url("synthesize", {"id": synthesis_id, "times": times})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.4 番茄钟
    # -----------------------------------------------------------------

    async def add_pomodoro(
        self,
        task_name: str,
        duration_minutes: int,
        reward_tomatoes: bool = True,
    ) -> dict[str, Any]:
        """添加番茄钟记录。"""
        url = _build_url("add_pomodoro", {
            "task_name": task_name,
            "duration": int(duration_minutes * 60_000),
            "reward_tomatoes": reward_tomatoes,
            "ui": True,
        })
        return await self._post_urls([url])

    async def edit_pomodoro(
        self,
        pomodoro_id: int,
        task_name: str | None = None,
        duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        """编辑番茄钟记录。"""
        params: dict[str, Any] = {"id": pomodoro_id}
        if task_name is not None:
            params["task_name"] = task_name
        if duration_minutes is not None:
            params["duration"] = int(duration_minutes * 60_000)
        url = _build_url("edit_pomodoro", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.5 感想
    # -----------------------------------------------------------------

    async def feeling(
        self,
        content: str,
        feeling_id: int | None = None,
        attach_task: int | None = None,
        attach_achievement: int | None = None,
        attach_item: int | None = None,
        attach_shop_history: int | None = None,
    ) -> dict[str, Any]:
        """创建/更新感想。

        Args:
            content: 感想内容
            feeling_id: 指定则更新已有感想，否则新建
            attach_task: 关联任务ID
            attach_achievement: 关联成就ID
            attach_item: 关联商品ID
            attach_shop_history: 关联购买记录ID
        """
        params: dict[str, Any] = {"content": content}
        if feeling_id is not None:
            params["id"] = feeling_id
        if attach_task is not None:
            params["attach_task"] = attach_task
        if attach_achievement is not None:
            params["attach_achievement"] = attach_achievement
        if attach_item is not None:
            params["attach_item"] = attach_item
        if attach_shop_history is not None:
            params["attach_shop_history"] = attach_shop_history
        url = _build_url("feeling", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.6 番茄管理
    # -----------------------------------------------------------------

    async def tomato(
        self,
        operation: str = "increase",
        value: int = 1,
    ) -> dict[str, Any]:
        """调整番茄数量。

        Args:
            operation: ``increase`` / ``decrease`` / ``set``
            value: 数值
        """
        url = _build_url("tomato", {"operation": operation, "value": value})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.7 历史操作
    # -----------------------------------------------------------------

    async def history_operation(
        self,
        history_id: int,
        operation: str = "undo",
    ) -> dict[str, Any]:
        """历史任务操作。

        Args:
            history_id: 历史记录ID
            operation: ``undo``（撤销完成）/ ``re_complete``（重新开始）
        """
        url = _build_url("history_operation", {
            "id": history_id,
            "operation": operation,
        })
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.8 清单管理 (category)
    # -----------------------------------------------------------------

    async def category(
        self,
        action: str = "query",
        name: str | None = None,
        category_id: int | None = None,
        type_: str | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        """清单管理。

        Args:
            action: ``query`` / ``add`` / ``delete`` / ``edit``
            name: 清单名称（add 时必填）
            category_id: 清单ID（delete/edit 时必填）
            type_: 清单类型（tasks/items/achievements/synthesis）
            new_name: 新名称（edit 时使用）
        """
        params: dict[str, Any] = {"action": action}
        if name is not None:
            params["name"] = name
        if category_id is not None:
            params["id"] = category_id
        if type_ is not None:
            params["type"] = type_
        if new_name is not None:
            params["new_name"] = new_name
        url = _build_url("category", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.9 成就管理 (achievement)
    # -----------------------------------------------------------------

    async def achievement(
        self,
        action: str = "add",
        achievement_id: int | None = None,
        category: int | None = None,
        title: str | None = None,
        content: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        link_task: int | None = None,
        link_shop: int | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        """成就管理。

        Args:
            action: ``add`` / ``delete`` / ``edit``
            achievement_id: 成就ID（delete/edit 时必填）
            category: 成就分类ID（add 时必填）
            title: 成就标题
            content: 成就描述
            icon: 图标名称
            color: 颜色
            link_task: 关联任务ID
            link_shop: 关联商品ID
            new_name: 新标题（edit 时使用）
        """
        params: dict[str, Any] = {"action": action}
        if achievement_id is not None:
            params["id"] = achievement_id
        if category is not None:
            params["category"] = category
        if title is not None:
            params["title"] = title
        if content is not None:
            params["content"] = content
        if icon is not None:
            params["icon"] = icon
        if color is not None:
            params["color"] = color
        if link_task is not None:
            params["link_task"] = link_task
        if link_shop is not None:
            params["link_shop"] = link_shop
        if new_name is not None:
            params["new_name"] = new_name
        url = _build_url("achievement", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.10 技能管理 (skill)
    # -----------------------------------------------------------------

    async def skill(
        self,
        action: str = "add",
        skill_id: int | None = None,
        name: str | None = None,
        color: str | None = None,
        icon: str | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        """技能/属性管理。

        Args:
            action: ``add`` / ``delete`` / ``edit``
            skill_id: 属性ID（delete/edit 时必填）
            name: 属性名称（add 时必填）
            color: 颜色（hex，如 FFFFFF）
            icon: 图标
            new_name: 新名称（edit 时使用）
        """
        params: dict[str, Any] = {"action": action}
        if skill_id is not None:
            params["id"] = skill_id
        if name is not None:
            params["name"] = name
        if color is not None:
            params["color"] = color
        if icon is not None:
            params["icon"] = icon
        if new_name is not None:
            params["new_name"] = new_name
        url = _build_url("skill", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.11 任务模板 (task_template)
    # -----------------------------------------------------------------

    async def task_template(
        self,
        action: str = "query",
        name: str | None = None,
        template_id: int | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        """任务模板管理。

        Args:
            action: ``query`` / ``add`` / ``delete`` / ``edit``
            name: 模板名称（add 时必填）
            template_id: 模板ID（delete/edit 时必填）
            new_name: 新名称（edit 时使用）
        """
        params: dict[str, Any] = {"action": action}
        if name is not None:
            params["name"] = name
        if template_id is not None:
            params["id"] = template_id
        if new_name is not None:
            params["new_name"] = new_name
        url = _build_url("task_template", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.12 商店设置 (shop_settings)
    # -----------------------------------------------------------------

    async def shop_settings(
        self,
        action: str = "query",
        atm_rate: float | None = None,
        max_loan: int | None = None,
        overdue_penalty: int | None = None,
    ) -> dict[str, Any]:
        """商店设置管理。

        Args:
            action: ``query`` / ``update``
            atm_rate: ATM 利率
            max_loan: 最大贷款额度
            overdue_penalty: 逾期惩罚
        """
        params: dict[str, Any] = {"action": action}
        if atm_rate is not None:
            params["atm_rate"] = atm_rate
        if max_loan is not None:
            params["max_loan"] = max_loan
        if overdue_penalty is not None:
            params["overdue_penalty"] = overdue_penalty
        url = _build_url("shop_settings", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.13 子任务
    # -----------------------------------------------------------------

    async def subtask(
        self,
        action: str = "add",
        task_id: int | None = None,
        subtask_name: str | None = None,
        subtask_id: int | None = None,
    ) -> dict[str, Any]:
        """子任务管理。

        Args:
            action: ``add`` / ``delete`` / ``edit``
            task_id: 所属任务ID
            subtask_name: 子任务名称
            subtask_id: 子任务ID（delete/edit 时必填）
        """
        params: dict[str, Any] = {"action": action}
        if task_id is not None:
            params["task_id"] = task_id
        if subtask_name is not None:
            params["subtask_name"] = subtask_name
        if subtask_id is not None:
            params["subtask_id"] = subtask_id
        url = _build_url("subtask", params)
        return await self._post_urls([url])

    async def subtask_operation(
        self,
        subtask_id: int,
        operation: str = "check",
    ) -> dict[str, Any]:
        """子任务操作。

        Args:
            subtask_id: 子任务ID
            operation: ``check`` / ``uncheck``
        """
        url = _build_url("subtask_operation", {
            "subtask_id": subtask_id,
            "operation": operation,
        })
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.14 步数
    # -----------------------------------------------------------------

    async def step(self, steps: int) -> dict[str, Any]:
        """设置步数。

        Args:
            steps: 步数值
        """
        url = _build_url("step", {"steps": steps})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.15 合成配方管理 (synthesis_formula)
    # -----------------------------------------------------------------

    async def synthesis_formula(
        self,
        action: str = "query",
        formula_id: int | None = None,
        name: str | None = None,
        result_item: int | None = None,
        materials: list[str] | None = None,
    ) -> dict[str, Any]:
        """合成配方管理。

        Args:
            action: ``query`` / ``add`` / ``delete``
            formula_id: 配方ID
            name: 配方名称
            result_item: 合成结果商品ID
            materials: 合成材料列表（格式: ["item_id:quantity", ...]）
        """
        params: dict[str, Any] = {"action": action}
        if formula_id is not None:
            params["id"] = formula_id
        if name is not None:
            params["name"] = name
        if result_item is not None:
            params["result_item"] = result_item
        if materials is not None:
            params["materials"] = materials
        url = _build_url("synthesis_formula", params)
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.16 随机执行
    # -----------------------------------------------------------------

    async def random_execute(
        self, urls: list[str],
    ) -> dict[str, Any]:
        """从给定的多个 API 中随机执行一个。

        Args:
            urls: ``lifeup://api/...`` 格式的 URL 列表
        """
        url = _build_url("random", {"urls": urls})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.17 商品CRUD (add_item / item / loot_box)
    # -----------------------------------------------------------------

    async def add_item(
        self,
        name: str,
        price: int = 0,
        quantity: int = 1,
        description: str = "",
        category: int | None = None,
        icon: str = "",
        color: str = "",
    ) -> dict[str, Any]:
        """添加商品到商店。"""
        url = _build_url("add_item", {
            "name": name,
            "price": price,
            "quantity": quantity,
            "description": description,
            "category": category,
            "icon": icon,
            "color": color,
        })
        return await self._post_urls([url])

    async def item_edit(
        self,
        action: str = "update",
        item_id: int | None = None,
        name: str | None = None,
        price: int | None = None,
        quantity: int | None = None,
        description: str | None = None,
        category: int | None = None,
    ) -> dict[str, Any]:
        """编辑商品信息。

        Args:
            action: ``update`` / ``delete``
            item_id: 商品ID
            name: 商品名称
            price: 价格
            quantity: 库存数量
            description: 描述
            category: 分类ID
        """
        params: dict[str, Any] = {"action": action}
        if item_id is not None:
            params["id"] = item_id
        if name is not None:
            params["name"] = name
        if price is not None:
            params["price"] = price
        if quantity is not None:
            params["quantity"] = quantity
        if description is not None:
            params["description"] = description
        if category is not None:
            params["category"] = category
        url = _build_url("item", params)
        return await self._post_urls([url])

    async def loot_box(
        self,
        item_id: int | None = None,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """触发开箱效果。

        触发指定物品（如宝箱）的开箱效果。
        """
        key, val = ("id", item_id) if item_id is not None else ("name", item_name)
        url = _build_url("loot_box", {key: val})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.18 简单查询 (query)
    # -----------------------------------------------------------------

    async def query(
        self,
        key: str,
    ) -> dict[str, Any]:
        """简单查询接口。

        端点内部调用 lifeup://api/query?key=xxx

        Args:
            key: 查询关键词，可选值:
                 ``coin`` / ``atm`` / ``bank`` / ``items`` /
                 ``tomatoes`` / ``pomodoro`` / ``task``
        """
        url = _build_url("query", {"key": key})
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.19 弹窗
    # -----------------------------------------------------------------

    async def toast(
        self, text: str, type_: int = 1, is_long: bool = False,
    ) -> dict[str, Any]:
        """发送 Toast 弹窗到 LifeUp App。

        Args:
            text: 弹窗文本
            type_: 图标类型（0-6）
            is_long: 是否长显示
        """
        url = _build_url("toast", {
            "text": text,
            "type": type_,
            "isLong": is_long,
        })
        return await self._post_urls([url])

    # -----------------------------------------------------------------
    # 2.20 万能执行
    # -----------------------------------------------------------------

    async def execute_raw_urls(self, urls: list[str]) -> dict[str, Any]:
        """批量执行任意 ``lifeup://api/...`` 格式的 URL。

        适用于以上所有封装方法未覆盖的接口，或需要组合调用多个 API 的场景。
        """
        return await self._post_urls(urls)

    # -----------------------------------------------------------------
    # 2.21 便捷组合方法
    # -----------------------------------------------------------------

    async def reward_coin(self, content: str, number: int) -> dict[str, Any]:
        """便捷方法：奖励金币。"""
        return await self.reward("coin", content, number)

    async def reward_exp(
        self, content: str, number: int, skills: list[int] | None = None,
    ) -> dict[str, Any]:
        """便捷方法：奖励经验。"""
        return await self.reward("exp", content, number, skills=skills)

    async def reward_item(
        self, content: str, item_id: int | None = None, item_name: str | None = None,
    ) -> dict[str, Any]:
        """便捷方法：奖励物品。"""
        return await self.reward("item", content, item_id=item_id, item_name=item_name)
