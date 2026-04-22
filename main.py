"""
AstrBot LifeUp (人升) 联动插件。

对接 LifeUp App 的 HTTP API，支持通过聊天指令管理任务、属性、
金币、物品、番茄钟、成就、感想、清单等所有功能模块。

LifeUp API 文档：https://wiki.lifeupapp.fun/zh-cn/index.html#/guide/api
SDK 源码：https://github.com/Ayagikei/LifeUp-SDK

使用方式
--------
所有指令以 ``/lifeup`` 为前缀，例如::

    /lifeup tasks
    /lifeup add 背单词50个 --coin 10 --exp 5
    /lifeup complete 背单词50个
    /lifeup reward 100 完成项目里程碑

指令与 ``/lifeup help`` 查看完整帮助。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger

from .lifeup_client import LifeUpClient

# ---------------------------------------------------------------------------
# 辅助函数 — 参数解析
# ---------------------------------------------------------------------------

def _strip_cmd_prefix(message: str, *cmd_words: str) -> str:
    """去掉消息前面的指令前缀，返回剩余参数文本。

    Args:
        message: 原始消息文本，如 ``/lifeup tasks 123``。
        cmd_words: 指令层级词，如 ``("lifeup", "tasks")``。

    Returns:
        去掉指令前缀后的参数字符串。
    """
    if not message:
        return ""
    text = message.strip()
    # 去掉开头的唤醒符（如 / 或 !）
    if text and not text[0].isalnum():
        text = text[1:].lstrip()
    for word in cmd_words:
        if text.lower().startswith(word.lower()):
            text = text[len(word):].lstrip()
    return text


def _extract_positional_args(message: str, expected: int = 1) -> list[str]:
    """从消息字符串中提取前 N 个位置参数（忽略 ``--`` 开头的选项）。

    Args:
        message: 原始消息文本（已去掉指令前缀）。
        expected: 期望提取的位置参数个数。

    Returns:
        提取到的位置参数列表，长度可能小于 expected。
    """
    if not message:
        return []
    try:
        tokens = shlex.split(message)
    except ValueError:
        tokens = message.split()

    result = [t for t in tokens if not t.startswith("--")]
    return result[:expected]


def _safe_int(val: str, default: int = 0) -> int:
    """安全地将字符串转为整数，失败时返回 default。"""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: str, default: float = 1.0) -> float:
    """安全地将字符串转为浮点数，失败时返回 default。"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _extract_named_args(message: str) -> dict[str, list[str]]:
    """提取 ``--key value1 value2`` 样式的具名参数。

    返回::

        {
            "coin": ["10"],
            "skills": ["1", "2"],
        }

    当遇到下一个 ``--`` 时停止收集当前 key 的值。
    """
    result: dict[str, list[str]] = {}
    try:
        tokens = shlex.split(message)
    except ValueError:
        tokens = message.split()

    current_key: str | None = None
    for token in tokens:
        if token.startswith("--"):
            current_key = token.lstrip("-").lower()
            result[current_key] = []
        elif current_key is not None:
            result[current_key].append(token)
    return result


def _int_list(values: list[str]) -> list[int]:
    """将字符串列表转为整数列表，忽略无法转换的项。"""
    return [int(v) for v in values if v.lstrip("-").isdigit()]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 格式化辅助 (QQ/OneBot V11 风格)
# ---------------------------------------------------------------------------

def _emoji_status(status: int | None) -> str:
    """任务状态码 -> 表情。"""
    if status is None:
        return "❓"
    mapping = {0: "⏳", 1: "✅", 2: "❌", 3: "🧊"}
    return mapping.get(status, "❓")


def _status_label(status: int | None) -> str:
    """任务状态码 -> 文字标签。"""
    if status is None:
        return "未知"
    mapping = {0: "待完成", 1: "已完成", 2: "已放弃", 3: "已冻结"}
    return mapping.get(status, f"状态{status}")


def _task_type_emoji(t: int | None) -> str:
    """任务类型 -> 表情。"""
    if t is None:
        return "📝"
    mapping = {0: "📝", 1: "🔢", 4: "🍅"}
    return mapping.get(t, "📝")


def _freq_label(f: int | None) -> str:
    """频率 -> 标签。"""
    if f is None:
        return ""
    mapping = {0: "", 1: "🔁", -1: "♾️"}
    return mapping.get(f, "")


def _emoji_progress(current: int, total: int, width: int = 6) -> str:
    """QQ 风格 emoji 进度条。"""
    if total <= 0:
        return "⬜⬜⬜⬜⬜⬜ 0%"
    ratio = min(current / total, 1.0)
    filled = int(width * ratio)
    empty = width - filled
    bar = "🟩" * filled + "⬜" * empty
    pct = int(ratio * 100)
    return f"{bar} {pct}%"


def _format_timestamp(ts: int | str | None) -> str:
    """格式化时间戳为友好字符串。"""
    if not ts:
        return "?"
    try:
        from datetime import datetime
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%m-%d %H:%M")
        return str(ts)
    except Exception:
        return str(ts)[:16]


class LifeUpPlugin(Star):
    """LifeUp 联动插件主类。

    通过 HTTP API 对接 LifeUp App，提供完整的任务、经济、物品、
    属性、成就、番茄钟等管理功能。

    Attributes:
        context: AstrBot 上下文
        config: 插件配置字典
        client: LifeUp HTTP API 客户端实例
    """

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context)
        self.config = config
        self.client = LifeUpClient(
            api_url=config.get("api_url", "http://localhost:13276"),
            api_token=config.get("api_token", ""),
            timeout=config.get("timeout", 5),
        )

    def terminate(self) -> None:
        """插件卸载时的清理逻辑（当前无需额外资源释放）。"""
        logger.info("LifeUp 插件已卸载")

    # ==================================================================
    #  错误提示
    # ==================================================================

    def _api_error_msg(self, exc: Exception) -> str:
        """生成面向用户的 API 错误提示文本。"""
        return (
            f"❌ LifeUp API 请求失败\n"
            f"原因：{exc}\n"
            f"\n"
            f"请检查以下配置项：\n"
            f"1. API 地址：{self.config.get('api_url', '未设置')}\n"
            f"2. LifeUp App 是否已开启 HTTP API 服务\n"
            f"3. 网络是否连通"
        )

    def _no_data_msg(self, category: str = "数据") -> str:
        return f"ℹ️ 暂无 {category} 数据"

    # ==================================================================
    #  格式化输出方法
    # ==================================================================

    def _fmt_tasks(self, tasks: list[dict[str, Any]] | None) -> str:
        if not tasks:
            return "━━━ 📋 任务清单 ━━━\n\n  (暂无任务)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        total = len(tasks)
        pending = sum(1 for t in tasks if t.get("status") == 0)
        done = sum(1 for t in tasks if t.get("status") == 1)
        lines = [
            f"━━━ 📋 任务清单 ({total}项) ━━━",
            f"  ⏳待办{pending}  ✅完成{done}  ❌放弃{sum(1 for t in tasks if t.get('status')==2)}",
            "",
        ]
        for t in tasks[:20]:
            tid = t.get("id", "?")
            name = t.get("title", t.get("todo", "无名"))
            status_icon = _emoji_status(t.get("status", 0))
            freq = _freq_label(t.get("frequency"))
            coin = t.get("coin", 0)
            exp = t.get("exp", 0)
            cat = t.get("categoryName", "")
            display_name = name[:14] + "…" if len(name) > 15 else name
            reward = []
            if coin:
                reward.append(f"💰{coin}")
            if exp:
                reward.append(f"🧪{exp}")
            reward_str = " ".join(reward) if reward else ""
            cat_str = f" 📂{cat}" if cat else ""
            lines.append(f"{status_icon} [{tid}] {display_name} {freq}{cat_str}")
            if reward_str:
                lines.append(f"    └─ {reward_str}")
        if len(tasks) > 20:
            lines.append(f"\n  ... 还有 {len(tasks) - 20} 个任务")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_items(self, items: list[dict[str, Any]] | None) -> str:
        if not items:
            return "━━━ 🛒 商店货架 ━━━\n\n  (暂无商品)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ 🛒 商店货架 ({len(items)}件) ━━━",
            "",
        ]
        for it in items[:15]:
            iid = it.get("id", "?")
            name = it.get("name", "无名")
            price = it.get("price", "?")
            stock = it.get("quantity", it.get("stock", "?"))
            if isinstance(stock, int):
                stock_icon = "🟢" if stock > 10 else "🟡" if stock > 0 else "🔴"
            else:
                stock_icon = "⚪"
            display_name = name[:14] + "…" if len(name) > 15 else name
            lines.append(f"  [{iid}] {display_name} 💰{price} {stock_icon}库存:{stock}")
        if len(items) > 15:
            lines.append(f"\n  ... 还有 {len(items) - 15} 件")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_skills(self, skills: list[dict[str, Any]] | None) -> str:
        if not skills:
            return "━━━ 📊 属性面板 ━━━\n\n  (暂无属性)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ 📊 属性面板 ({len(skills)}项) ━━━",
            "",
        ]
        for sk in skills:
            name = sk.get("name", "无名")
            level = sk.get("level", 0)
            cur_exp = sk.get("cur_exp", sk.get("exp", 0))
            max_exp = sk.get("max_exp", 100)
            icon = sk.get("icon", "")
            icon_str = f"{icon} " if icon else ""
            bar = _emoji_progress(cur_exp, max_exp, 6)
            lines.append(f"  {icon_str}{name} Lv.{level}")
            lines.append(f"    └─ {bar} {cur_exp}/{max_exp}")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_coin(self, data: dict[str, Any] | None) -> str:
        if not data:
            return "━━━ 💰 资产概览 ━━━\n\n  金币数据不可用\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        if isinstance(data, dict):
            amount = data.get("value", data.get("coin", "未知"))
            bank = data.get("bank", data.get("atm", "?"))
        else:
            amount = data
            bank = "?"
        return (
            "━━━ 💰 资产概览 ━━━\n\n"
            f"  👛 钱包 {amount}\n"
            f"  🏦 存款 {bank}\n\n"
            "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        )

    def _fmt_synthesis(self, formulas: list[dict[str, Any]] | None) -> str:
        if not formulas:
            return "━━━ ⚗️ 合成配方 ━━━\n\n  (暂无配方)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ ⚗️ 合成配方 ({len(formulas)}种) ━━━",
            "",
        ]
        for f in formulas[:12]:
            fid = f.get("id", "?")
            name = f.get("name", "无名")
            result = f.get("resultItemName", f.get("result_name", "?"))
            display_name = name[:14] + "…" if len(name) > 15 else name
            lines.append(f"  [{fid}] {display_name} -> {result}")
        if len(formulas) > 12:
            lines.append(f"\n  ... 还有 {len(formulas) - 12} 种")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_feelings(self, feelings: list[dict[str, Any]] | None) -> str:
        if not feelings:
            return "━━━ 📝 感想墙 ━━━\n\n  (暂无感想)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ 📝 感想墙 ({len(feelings)}条) ━━━",
            "",
        ]
        for fl in feelings[:12]:
            fid = fl.get("id", "?")
            content = fl.get("content", "")
            ts = _format_timestamp(fl.get("timestamp", fl.get("time")))
            display = content[:35] + "…" if len(content) > 35 else content
            lines.append(f"  [{fid}] 💭 {display}")
            lines.append(f"    └─ 🕐 {ts}")
        if len(feelings) > 12:
            lines.append(f"\n  ... 还有 {len(feelings) - 12} 条")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_history(self, history: list[dict[str, Any]] | None) -> str:
        if not history:
            return "━━━ 📜 历史记录 ━━━\n\n  (暂无记录)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ 📜 历史记录 ({len(history)}条) ━━━",
            "",
        ]
        for h in history[:12]:
            hid = h.get("id", "?")
            task_name = h.get("taskName", h.get("title", "未知"))
            action = h.get("action", "?")
            action_icon = {"complete": "✅", "give_up": "❌", "undo": "↩️"}.get(action, "📝")
            ts = _format_timestamp(h.get("timestamp", h.get("time")))
            coin = h.get("coin", 0)
            exp = h.get("exp", 0)
            reward = []
            if coin:
                reward.append(f"💰+{coin}")
            if exp:
                reward.append(f"🧪+{exp}")
            reward_str = " ".join(reward) if reward else ""
            display_name = task_name[:14] + "…" if len(task_name) > 15 else task_name
            lines.append(f"  {action_icon} [{hid}] {display_name}")
            detail = f"🕐 {ts}"
            if reward_str:
                detail += f" · {reward_str}"
            lines.append(f"    └─ {detail}")
        if len(history) > 12:
            lines.append(f"\n  ... 还有 {len(history) - 12} 条")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_categories(self, cats: list[dict[str, Any]] | None, label: str = "分类") -> str:
        if not cats:
            return f"━━━ 📂 {label} ━━━\n\n  (暂无{label})\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = [
            f"━━━ 📂 {label} ({len(cats)}项) ━━━",
            "",
        ]
        for c in cats:
            cid = c.get("id", "?")
            name = c.get("name", "未命名")
            count = c.get("count", c.get("itemCount", ""))
            count_str = f" ({count}项)" if count else ""
            lines.append(f"  📁 [{cid}] {name}{count_str}")
        lines.append(f"\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_achievements(self, achievements: list[dict[str, Any]] | None) -> str:
        if not achievements:
            return "━━━ 🏆 成就殿堂 ━━━\n\n  (暂无成就)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        total = len(achievements)
        unlocked = sum(1 for a in achievements if a.get("unlocked", a.get("achieved", False)))
        bar = _emoji_progress(unlocked, total, 8)
        lines = [
            f"━━━ 🏆 成就殿堂 ({unlocked}/{total}) ━━━",
            f"  进度 {bar}",
            "",
        ]
        for a in achievements[:15]:
            aid = a.get("id", "?")
            title = a.get("title", a.get("name", "无名"))
            is_unlocked = a.get("unlocked", a.get("achieved", False))
            status_icon = "🌟" if is_unlocked else "🔒"
            desc = a.get("content", a.get("description", ""))
            cat = a.get("categoryName", a.get("category", ""))
            cat_str = f" · {cat}" if cat else ""
            display_title = title[:14] + "…" if len(title) > 15 else title
            lines.append(f"  {status_icon} [{aid}] {display_title}{cat_str}")
            if desc and not is_unlocked:
                desc_short = desc[:20] + "…" if len(desc) > 20 else desc
                lines.append(f"    └─ 💡 {desc_short}")
        if len(achievements) > 15:
            lines.append(f"\n  ... 还有 {len(achievements) - 15} 个")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_pomodoro_records(self, records: list[dict[str, Any]] | None) -> str:
        if not records:
            return "━━━ 🍅 专注日历 ━━━\n\n  (暂无番茄记录)\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        total_ms = sum(r.get("duration", 0) for r in records)
        total_min = int(total_ms / 60000)
        lines = [
            f"━━━ 🍅 专注日历 ({len(records)}条) ━━━",
            f"  总计专注 {total_min} 分钟",
            "",
        ]
        for r in records[:12]:
            rid = r.get("id", "?")
            task = r.get("taskName", "专注")
            dur = r.get("duration", 0)
            dur_min = int(dur / 60000) if dur else 0
            ts = _format_timestamp(r.get("timestamp", r.get("time")))
            display_task = task[:14] + "…" if len(task) > 15 else task
            if dur_min >= 45:
                dur_icon = "🔥"
            elif dur_min >= 25:
                dur_icon = "✨"
            else:
                dur_icon = "🌱"
            lines.append(f"  {dur_icon} [{rid}] {display_task} ⏱️{dur_min}分 🕐{ts}")
        if len(records) > 12:
            lines.append(f"\n  ... 还有 {len(records) - 12} 条")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_info(self, info: dict[str, Any] | None) -> str:
        if not info:
            return "━━━ 📱 应用信息 ━━━\n\n  应用信息不可用\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        lines = ["━━━ 📱 应用信息 ━━━", ""]
        key_labels = {
            "version": "📦 版本",
            "appVersion": "📦 版本",
            "packageName": "📁 包名",
            "deviceModel": "📱 设备",
            "apiVersion": "🔌 API版本",
        }
        for k, v in info.items():
            label = key_labels.get(k, f"  {k}")
            lines.append(f"  {label}: {v}")
        lines.append("\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈")
        return "\n".join(lines)

    def _fmt_status(self, coin_data: dict[str, Any], skills: list[dict[str, Any]]) -> str:
        parts = [self._fmt_coin(coin_data), ""]
        if skills:
            parts.append(self._fmt_skills(skills))
        total_level = sum(s.get("level", 0) for s in skills) if skills else 0
        if total_level > 0:
            avg = total_level // len(skills) if skills else 0
            parts.append(f"\n📈 总等级 {total_level} · 平均 Lv.{avg}")
        return "\n".join(parts)

    def _fmt_success(self, resp: dict[str, Any], action: str = "操作") -> str:
        """QQ 风格操作结果提示。"""
        if isinstance(resp, dict):
            if resp.get("status") == "error":
                msg = resp.get("message", "未知错误")
                return f"❌ ━━━ {action}失败 ━━━\n\n  {msg}\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
            data = resp.get("data")
            if data and isinstance(data, str) and data != "success":
                return f"✅ ━━━ {action}成功 ━━━\n\n  {data}\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
        return f"✅ ━━━ {action}成功 ━━━\n\n  已完成！\n\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
    # ==================================================================
    #  指令组定义
    # ==================================================================

    @filter.command_group("lifeup")
    def lifeup_group(self):
        """人升(LifeUp) App 联动指令组。"""
        pass


    # ==================================================================
    #  A. 任务管理指令
    # ==================================================================

    @lifeup_group.command("tasks")
    async def tasks_cmd(self, event: AstrMessageEvent) -> None:
        """查看任务列表。用法：/lifeup tasks [category_id]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "tasks")
        args = _extract_positional_args(msg, expected=1)
        category_id: int | None = None
        if args:
            category_id = _safe_int(args[0])
            if category_id <= 0:
                category_id = None

        try:
            resp = await self.client.query_tasks(category_id=category_id)
            tasks = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_tasks(tasks))
        except Exception as exc:
            logger.error("query_tasks 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("add")
    async def add_cmd(self, event: AstrMessageEvent) -> None:
        """添加任务。用法：/lifeup add <任务名称> [--coin N] [--exp N] [--skills 1 2] [--category N] [--freq N] [--type normal|habit|repeat] [--notes 备注] [--deadline N] [--reminder HH:mm]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "add")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup add <任务名称> [--coin N] [--exp N] ...")
            return

        todo = pos[0]
        named = _extract_named_args(msg)

        coin = _safe_int(named.get("coin", ["0"])[0])
        exp = _safe_int(named.get("exp", ["0"])[0])
        skills = _int_list(named.get("skills", []))
        category = _safe_int(named.get("category", ["0"])[0])
        freq_str = named.get("freq", ["0"])[0]
        freq_map = {"normal": 0, "habit": -1, "repeat": 1, "daily": 1, "once": 0}
        frequency = freq_map.get(freq_str.lower(), _safe_int(freq_str))
        type_str = named.get("type", ["0"])[0]
        type_map = {"normal": 0, "habit": 0, "repeat": 0, "count": 1, "counter": 1, "pomodoro": 4, "tomato": 4}
        task_type = type_map.get(type_str.lower(), _safe_int(type_str))
        notes = " ".join(named.get("notes", []))
        deadline = _safe_int(named.get("deadline", ["0"])[0])
        reminder = named.get("reminder", [""])[0]

        try:
            resp = await self.client.add_task(
                todo=todo, notes=notes, coin=coin if coin else None,
                exp=exp if exp else None, skills=skills if skills else None,
                category=category if category else None,
                frequency=frequency, task_type=task_type if task_type else None,
                deadline=deadline if deadline else None,
                reminder=reminder if reminder else None,
            )
            yield event.plain_result(self._fmt_success(resp, "添加任务"))
        except Exception as exc:
            logger.error("add_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("complete")
    async def complete_cmd(self, event: AstrMessageEvent) -> None:
        """完成任务。用法：/lifeup complete <task_id 或任务名称> [--factor 1.0]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "complete")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup complete <任务ID或名称> [--factor 1.0]")
            return

        target = pos[0]
        named = _extract_named_args(msg)
        factor = _safe_float(named.get("factor", ["1.0"])[0])

        try:
            task_id = _safe_int(target)
            if str(task_id) == target and task_id > 0:
                resp = await self.client.complete_task(task_id=task_id, reward_factor=factor)
            else:
                resp = await self.client.complete_task(name=target, reward_factor=factor)
            yield event.plain_result(self._fmt_success(resp, "完成任务"))
        except Exception as exc:
            logger.error("complete_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("giveup")
    async def giveup_cmd(self, event: AstrMessageEvent) -> None:
        """放弃任务。用法：/lifeup giveup <task_id 或任务名称>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "giveup")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup giveup <任务ID或名称>")
            return

        target = pos[0]
        try:
            task_id = _safe_int(target)
            if str(task_id) == target and task_id > 0:
                resp = await self.client.give_up_task(task_id=task_id)
            else:
                resp = await self.client.give_up_task(name=target)
            yield event.plain_result(self._fmt_success(resp, "放弃任务"))
        except Exception as exc:
            logger.error("give_up_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("freeze")
    async def freeze_cmd(self, event: AstrMessageEvent) -> None:
        """冻结任务。用法：/lifeup freeze <task_id 或任务名称>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "freeze")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup freeze <任务ID或名称>")
            return

        target = pos[0]
        try:
            task_id = _safe_int(target)
            if str(task_id) == target and task_id > 0:
                resp = await self.client.freeze_task(task_id=task_id)
            else:
                resp = await self.client.freeze_task(name=target)
            yield event.plain_result(self._fmt_success(resp, "冻结任务"))
        except Exception as exc:
            logger.error("freeze_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("unfreeze")
    async def unfreeze_cmd(self, event: AstrMessageEvent) -> None:
        """解冻任务。用法：/lifeup unfreeze <task_id 或任务名称>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "unfreeze")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup unfreeze <任务ID或名称>")
            return

        target = pos[0]
        try:
            task_id = _safe_int(target)
            if str(task_id) == target and task_id > 0:
                resp = await self.client.unfreeze_task(task_id=task_id)
            else:
                resp = await self.client.unfreeze_task(name=target)
            yield event.plain_result(self._fmt_success(resp, "解冻任务"))
        except Exception as exc:
            logger.error("unfreeze_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("delete")
    async def delete_cmd(self, event: AstrMessageEvent) -> None:
        """删除任务。用法：/lifeup delete <task_id 或任务名称>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "delete")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup delete <任务ID或名称>")
            return

        target = pos[0]
        try:
            task_id = _safe_int(target)
            if str(task_id) == target and task_id > 0:
                resp = await self.client.delete_task(task_id=task_id)
            else:
                resp = await self.client.delete_task(name=target)
            yield event.plain_result(self._fmt_success(resp, "删除任务"))
        except Exception as exc:
            logger.error("delete_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("edit")
    async def edit_cmd(self, event: AstrMessageEvent) -> None:
        """编辑任务。用法：/lifeup edit <task_id 或名称> [--todo 新名称] [--coin N] [--exp N] [--skills 1 2] [--category N] [--freq N] [--notes 备注] [--deadline N] [--freeze true]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "edit")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup edit <任务ID或名称> [--todo 新名称] [--coin N] ...")
            return

        target = pos[0]
        named = _extract_named_args(msg)

        task_id = _safe_int(target) if str(_safe_int(target)) == target and _safe_int(target) > 0 else None
        name = None if task_id else target

        params: dict[str, Any] = {}
        if task_id:
            params["task_id"] = task_id
        if name:
            params["name"] = name
        if "todo" in named:
            params["todo"] = named["todo"][0] if named["todo"] else ""
        if "coin" in named:
            params["coin"] = _safe_int(named["coin"][0])
        if "exp" in named:
            params["exp"] = _safe_int(named["exp"][0])
        if "skills" in named:
            params["skills"] = _int_list(named["skills"])
        if "category" in named:
            params["category"] = _safe_int(named["category"][0])
        if "freq" in named:
            freq_str = named["freq"][0]
            freq_map = {"normal": 0, "habit": -1, "repeat": 1, "daily": 1, "once": 0}
            params["frequency"] = freq_map.get(freq_str.lower(), _safe_int(freq_str))
        if "notes" in named:
            params["notes"] = " ".join(named["notes"])
        if "deadline" in named:
            params["deadline"] = _safe_int(named["deadline"][0])
        if "freeze" in named:
            params["freeze"] = named["freeze"][0].lower() in ("true", "1", "yes") if named["freeze"] else True

        try:
            resp = await self.client.edit_task(**params)
            yield event.plain_result(self._fmt_success(resp, "编辑任务"))
        except Exception as exc:
            logger.error("edit_task 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  B. 查询类指令
    # ==================================================================

    @lifeup_group.command("items")
    async def items_cmd(self, event: AstrMessageEvent) -> None:
        """查看商品列表。用法：/lifeup items [list_id]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "items")
        pos = _extract_positional_args(msg, expected=1)
        list_id: int | None = _safe_int(pos[0]) if pos else None

        try:
            resp = await self.client.query_items(list_id=list_id if list_id else None)
            items = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_items(items))
        except Exception as exc:
            logger.error("query_items 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("skills")
    async def skills_cmd(self, event: AstrMessageEvent) -> None:
        """查看属性列表。用法：/lifeup skills"""
        try:
            resp = await self.client.query_skills()
            skills = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_skills(skills))
        except Exception as exc:
            logger.error("query_skills 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("coin")
    async def coin_cmd(self, event: AstrMessageEvent) -> None:
        """查看金币信息。用法：/lifeup coin"""
        try:
            resp = await self.client.query_coin()
            coin_data = resp.get("data", {}) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_coin(coin_data))
        except Exception as exc:
            logger.error("query_coin 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("history")
    async def history_cmd(self, event: AstrMessageEvent) -> None:
        """查看历史记录。用法：/lifeup history [limit]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "history")
        pos = _extract_positional_args(msg, expected=1)
        limit = _safe_int(pos[0], 100) if pos else 100

        try:
            resp = await self.client.query_history(limit=limit)
            history = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_history(history))
        except Exception as exc:
            logger.error("query_history 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("achievements")
    async def achievements_cmd(self, event: AstrMessageEvent) -> None:
        """查看成就列表。用法：/lifeup achievements [category_id]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "achievements")
        pos = _extract_positional_args(msg, expected=1)
        category_id: int | None = _safe_int(pos[0]) if pos else None

        try:
            resp = await self.client.query_achievements(category_id=category_id if category_id else None)
            achievements = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_achievements(achievements))
        except Exception as exc:
            logger.error("query_achievements 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("categories")
    async def categories_cmd(self, event: AstrMessageEvent) -> None:
        """查看分类列表。用法：/lifeup categories <tasks|items|achievements|synthesis> [parent_id]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "categories")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup categories <tasks|items|achievements|synthesis> [parent_id]")
            return

        cat_type = pos[0].lower()
        parent_id: int | None = _safe_int(pos[1]) if len(pos) > 1 else None

        try:
            if cat_type in ("task", "tasks"):
                resp = await self.client.query_tasks_categories()
            elif cat_type in ("item", "items"):
                resp = await self.client.query_items_categories()
            elif cat_type in ("achievement", "achievements"):
                resp = await self.client.query_achievement_categories()
            elif cat_type in ("synthesis", "synthesize"):
                resp = await self.client.query_synthesis_categories(parent_id=parent_id)
            else:
                yield event.plain_result(f"❌ 未知分类类型：{cat_type}\n可选：tasks, items, achievements, synthesis")
                return
            cats = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_categories(cats, f"{cat_type} 分类"))
        except Exception as exc:
            logger.error("query_categories 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("pomodoro_records")
    async def pomodoro_records_cmd(self, event: AstrMessageEvent) -> None:
        """查看番茄钟记录。用法：/lifeup pomodoro_records [limit]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "pomodoro_records")
        pos = _extract_positional_args(msg, expected=1)
        limit = _safe_int(pos[0], 100) if pos else 100

        try:
            resp = await self.client.query_pomodoro_records(limit=limit)
            records = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_pomodoro_records(records))
        except Exception as exc:
            logger.error("query_pomodoro_records 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("info")
    async def info_cmd(self, event: AstrMessageEvent) -> None:
        """查看 LifeUp 应用信息。用法：/lifeup info"""
        try:
            resp = await self.client.query_info()
            info = resp.get("data", {}) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_info(info))
        except Exception as exc:
            logger.error("query_info 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("synthesis")
    async def synthesis_cmd(self, event: AstrMessageEvent) -> None:
        """查看合成配方。用法：/lifeup synthesis [category_id]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "synthesis")
        pos = _extract_positional_args(msg, expected=1)
        category_id: int | None = _safe_int(pos[0]) if pos else None

        try:
            resp = await self.client.query_synthesis(category_id=category_id if category_id else None)
            formulas = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_synthesis(formulas))
        except Exception as exc:
            logger.error("query_synthesis 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("feelings")
    async def feelings_cmd(self, event: AstrMessageEvent) -> None:
        """查看感想列表。用法：/lifeup feelings [limit]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "feelings")
        pos = _extract_positional_args(msg, expected=1)
        limit = _safe_int(pos[0], 100) if pos else 100

        try:
            resp = await self.client.query_feelings(limit=limit)
            feelings = resp.get("data", []) if isinstance(resp, dict) else resp
            yield event.plain_result(self._fmt_feelings(feelings))
        except Exception as exc:
            logger.error("query_feelings 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("status")
    async def status_cmd(self, event: AstrMessageEvent) -> None:
        """综合状态查询（金币+属性）。用法：/lifeup status"""
        try:
            coin_resp, skills_resp = await asyncio.gather(
                self.client.query_coin(),
                self.client.query_skills(),
            )
            coin_data = coin_resp.get("data", {}) if isinstance(coin_resp, dict) else coin_resp
            skills = skills_resp.get("data", []) if isinstance(skills_resp, dict) else skills_resp
            yield event.plain_result(self._fmt_status(coin_data, skills))
        except Exception as exc:
            logger.error("status 查询失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  C. 经济管理指令
    # ==================================================================

    @lifeup_group.command("reward")
    async def reward_cmd(self, event: AstrMessageEvent) -> None:
        """奖励金币/经验/物品。用法：/lifeup reward <amount> [reason] [--type coin|exp|item] [--item_name 物品名] [--skills 1 2]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "reward")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup reward <amount> [reason] [--type coin|exp|item]")
            return

        amount = _safe_int(pos[0])
        reason = pos[1] if len(pos) > 1 else "来自AstrBot的奖励"
        named = _extract_named_args(msg)
        type_ = named.get("type", ["coin"])[0].lower()
        skills = _int_list(named.get("skills", []))
        item_name = named.get("item_name", [None])[0]

        try:
            if type_ == "item":
                resp = await self.client.reward_item(reason, item_name=item_name)
            elif type_ == "exp":
                resp = await self.client.reward_exp(reason, amount, skills=skills if skills else None)
            else:
                resp = await self.client.reward_coin(reason, amount)
            yield event.plain_result(self._fmt_success(resp, f"奖励{type_}"))
        except Exception as exc:
            logger.error("reward 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("penalty")
    async def penalty_cmd(self, event: AstrMessageEvent) -> None:
        """惩罚扣除。用法：/lifeup penalty <amount> [reason] [--type coin|exp]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "penalty")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup penalty <amount> [reason] [--type coin|exp]")
            return

        amount = _safe_int(pos[0])
        reason = pos[1] if len(pos) > 1 else "来自AstrBot的惩罚"
        named = _extract_named_args(msg)
        type_ = named.get("type", ["coin"])[0].lower()

        try:
            resp = await self.client.penalty(type_, reason, amount)
            yield event.plain_result(self._fmt_success(resp, f"惩罚扣除{type_}"))
        except Exception as exc:
            logger.error("penalty 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("atm")
    async def atm_cmd(self, event: AstrMessageEvent) -> None:
        """ATM 存款/取款。用法：/lifeup atm <deposit|withdraw> <amount>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "atm")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup atm <deposit|withdraw> <amount>")
            return

        action = pos[0].lower()
        amount = _safe_int(pos[1])

        try:
            if action == "deposit":
                resp = await self.client.deposit(amount)
                yield event.plain_result(self._fmt_success(resp, "ATM存款"))
            elif action in ("withdraw", "取"):
                resp = await self.client.withdraw(amount)
                yield event.plain_result(self._fmt_success(resp, "ATM取款"))
            else:
                yield event.plain_result(f"❌ 未知操作：{action}\n可选：deposit（存款）/ withdraw（取款）")
        except Exception as exc:
            logger.error("atm 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("editcoin")
    async def editcoin_cmd(self, event: AstrMessageEvent) -> None:
        """直接编辑金币。用法：/lifeup editcoin <increase|decrease|set> <amount>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "editcoin")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup editcoin <increase|decrease|set> <amount>")
            return

        operation = pos[0].lower()
        value = _safe_int(pos[1])

        try:
            resp = await self.client.edit_coin(operation=operation, value=value)
            yield event.plain_result(self._fmt_success(resp, "编辑金币"))
        except Exception as exc:
            logger.error("edit_coin 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("editexp")
    async def editexp_cmd(self, event: AstrMessageEvent) -> None:
        """直接编辑属性经验。用法：/lifeup editexp <skill_id> <increase|decrease|set> <amount>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "editexp")
        pos = _extract_positional_args(msg, expected=3)
        if len(pos) < 3:
            yield event.plain_result("❌ 用法：/lifeup editexp <skill_id> <increase|decrease|set> <amount>")
            return

        skill_id = _safe_int(pos[0])
        operation = pos[1].lower()
        value = _safe_int(pos[2])

        try:
            resp = await self.client.edit_exp(skill_id=skill_id, operation=operation, value=value)
            yield event.plain_result(self._fmt_success(resp, "编辑经验"))
        except Exception as exc:
            logger.error("edit_exp 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  D. 物品管理指令
    # ==================================================================

    @lifeup_group.command("buy")
    async def buy_cmd(self, event: AstrMessageEvent) -> None:
        """购买物品。用法：/lifeup buy <item_name 或 item_id> [quantity]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "buy")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup buy <物品名或ID> [数量]")
            return

        target = pos[0]
        quantity = _safe_int(pos[1], 1) if len(pos) > 1 else 1

        try:
            item_id = _safe_int(target)
            if str(item_id) == target and item_id > 0:
                resp = await self.client.purchase_item(item_id=item_id, quantity=quantity)
            else:
                resp = await self.client.purchase_item(item_name=target, quantity=quantity)
            yield event.plain_result(self._fmt_success(resp, "购买物品"))
        except Exception as exc:
            logger.error("purchase_item 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("use")
    async def use_cmd(self, event: AstrMessageEvent) -> None:
        """使用物品。用法：/lifeup use <item_name 或 item_id> [times]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "use")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup use <物品名或ID> [次数]")
            return

        target = pos[0]
        times = _safe_int(pos[1], 1) if len(pos) > 1 else 1

        try:
            item_id = _safe_int(target)
            if str(item_id) == target and item_id > 0:
                resp = await self.client.use_item(item_id=item_id, use_times=times)
            else:
                resp = await self.client.use_item(item_name=target, use_times=times)
            yield event.plain_result(self._fmt_success(resp, "使用物品"))
        except Exception as exc:
            logger.error("use_item 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("synthesize")
    async def synthesize_cmd(self, event: AstrMessageEvent) -> None:
        """执行合成。用法：/lifeup synthesize <synthesis_id> [times]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "synthesize")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup synthesize <配方ID> [次数]")
            return

        sid = _safe_int(pos[0])
        times = _safe_int(pos[1], 1) if len(pos) > 1 else 1

        try:
            resp = await self.client.synthesize(synthesis_id=sid, times=times)
            yield event.plain_result(self._fmt_success(resp, "合成"))
        except Exception as exc:
            logger.error("synthesize 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("item_add")
    async def item_add_cmd(self, event: AstrMessageEvent) -> None:
        """添加商品到商店。用法：/lifeup item_add <name> [price] [--quantity N] [--desc 描述] [--category N]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "item_add")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup item_add <名称> [价格] [--quantity N] [--desc 描述]")
            return

        name = pos[0]
        price = _safe_int(pos[1], 0) if len(pos) > 1 else 0
        named = _extract_named_args(msg)
        quantity = _safe_int(named.get("quantity", ["1"])[0])
        desc = " ".join(named.get("desc", []))
        category = _safe_int(named.get("category", ["0"])[0])

        try:
            resp = await self.client.add_item(
                name=name, price=price, quantity=quantity,
                description=desc, category=category if category else None,
            )
            yield event.plain_result(self._fmt_success(resp, "添加商品"))
        except Exception as exc:
            logger.error("add_item 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("item_edit")
    async def item_edit_cmd(self, event: AstrMessageEvent) -> None:
        """编辑/删除商品。用法：/lifeup item_edit <item_id 或名称> [--price N] [--quantity N] [--desc 描述] [--delete]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "item_edit")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup item_edit <ID或名称> [--price N] [--quantity N] [--delete]")
            return

        target = pos[0]
        named = _extract_named_args(msg)

        item_id = _safe_int(target) if str(_safe_int(target)) == target and _safe_int(target) > 0 else None
        is_delete = "delete" in named

        params: dict[str, Any] = {
            "action": "delete" if is_delete else "update",
            "item_id": item_id,
            "name": None if item_id else target,
        }
        if "price" in named:
            params["price"] = _safe_int(named["price"][0])
        if "quantity" in named:
            params["quantity"] = _safe_int(named["quantity"][0])
        if "desc" in named:
            params["description"] = " ".join(named["desc"])
        if "category" in named:
            params["category"] = _safe_int(named["category"][0])

        try:
            resp = await self.client.item_edit(**params)
            yield event.plain_result(self._fmt_success(resp, "删除商品" if is_delete else "编辑商品"))
        except Exception as exc:
            logger.error("item_edit 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("loot")
    async def loot_cmd(self, event: AstrMessageEvent) -> None:
        """触发开箱效果。用法：/lifeup loot <item_name 或 item_id>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "loot")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup loot <物品名或ID>")
            return

        target = pos[0]
        try:
            item_id = _safe_int(target)
            if str(item_id) == target and item_id > 0:
                resp = await self.client.loot_box(item_id=item_id)
            else:
                resp = await self.client.loot_box(item_name=target)
            yield event.plain_result(self._fmt_success(resp, "开箱"))
        except Exception as exc:
            logger.error("loot_box 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  E. 番茄钟指令
    # ==================================================================

    @lifeup_group.command("pomodoro")
    async def pomodoro_cmd(self, event: AstrMessageEvent) -> None:
        """记录番茄钟。用法：/lifeup pomodoro <task_name> <duration_minutes> [--no_reward]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "pomodoro")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup pomodoro <任务名> <分钟> [--no_reward]")
            return

        task_name = pos[0]
        minutes = _safe_int(pos[1])
        named = _extract_named_args(msg)
        reward = "no_reward" not in named

        try:
            resp = await self.client.add_pomodoro(
                task_name=task_name, duration_minutes=minutes, reward_tomatoes=reward,
            )
            yield event.plain_result(self._fmt_success(resp, "记录番茄钟"))
        except Exception as exc:
            logger.error("add_pomodoro 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  F. 感想
    # ==================================================================

    @lifeup_group.command("feeling")
    async def feeling_cmd(self, event: AstrMessageEvent) -> None:
        """创建感想。用法：/lifeup feeling <content> [--task N] [--achievement N] [--item N]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "feeling")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup feeling <感想内容> [--task N] [--achievement N]")
            return

        content = pos[0]
        named = _extract_named_args(msg)
        attach_task = _safe_int(named.get("task", ["0"])[0]) or None
        attach_achievement = _safe_int(named.get("achievement", ["0"])[0]) or None
        attach_item = _safe_int(named.get("item", ["0"])[0]) or None

        try:
            resp = await self.client.feeling(
                content=content,
                attach_task=attach_task,
                attach_achievement=attach_achievement,
                attach_item=attach_item,
            )
            yield event.plain_result(self._fmt_success(resp, "创建感想"))
        except Exception as exc:
            logger.error("feeling 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  G. 番茄管理
    # ==================================================================

    @lifeup_group.command("tomato")
    async def tomato_cmd(self, event: AstrMessageEvent) -> None:
        """调整番茄数量。用法：/lifeup tomato <increase|decrease|set> <value>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "tomato")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup tomato <increase|decrease|set> <数值>")
            return

        operation = pos[0].lower()
        value = _safe_int(pos[1])

        try:
            resp = await self.client.tomato(operation=operation, value=value)
            yield event.plain_result(self._fmt_success(resp, "调整番茄"))
        except Exception as exc:
            logger.error("tomato 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  H. 历史操作
    # ==================================================================

    @lifeup_group.command("undo")
    async def undo_cmd(self, event: AstrMessageEvent) -> None:
        """撤销历史任务完成。用法：/lifeup undo <history_id>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "undo")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup undo <历史记录ID>")
            return

        history_id = _safe_int(pos[0])
        try:
            resp = await self.client.history_operation(history_id=history_id, operation="undo")
            yield event.plain_result(self._fmt_success(resp, "撤销完成"))
        except Exception as exc:
            logger.error("history_operation undo 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  I. 清单管理
    # ==================================================================

    @lifeup_group.command("category")
    async def category_cmd(self, event: AstrMessageEvent) -> None:
        """清单管理。用法：/lifeup category <add|delete|edit> <type> <name> [--id N] [--new_name 新名称]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "category")
        pos = _extract_positional_args(msg, expected=3)

        named = _extract_named_args(msg)
        action = pos[0].lower() if pos else "query"

        if action == "add" and len(pos) < 3:
            yield event.plain_result("❌ 用法：/lifeup category add <tasks|items|achievements|synthesis> <名称>")
            return
        if action in ("delete", "edit") and ("id" not in named or not named["id"]):
            yield event.plain_result("❌ delete/edit 需要 --id 参数\n用法：/lifeup category delete <type> --id N")
            return

        cat_type = pos[1] if len(pos) > 1 else "tasks"
        name = pos[2] if len(pos) > 2 else ""
        category_id = _safe_int(named.get("id", ["0"])[0]) or None
        new_name = named.get("new_name", [None])[0]

        try:
            resp = await self.client.category(
                action=action, type_=cat_type, name=name,
                category_id=category_id, new_name=new_name,
            )
            yield event.plain_result(self._fmt_success(resp, f"清单{action}"))
        except Exception as exc:
            logger.error("category 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  J. 成就管理
    # ==================================================================

    @lifeup_group.command("achievement")
    async def achievement_cmd(self, event: AstrMessageEvent) -> None:
        """成就管理。用法：/lifeup achievement <add|delete|edit> [--id N] [--category N] [--title 标题] [--content 描述] [--icon 图标] [--color 颜色] [--link_task N] [--link_shop N] [--new_name 新标题]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "achievement")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup achievement <add|delete|edit> [--id N] [--title ...]")
            return

        action = pos[0].lower()
        named = _extract_named_args(msg)

        achievement_id = _safe_int(named.get("id", ["0"])[0]) or None
        category = _safe_int(named.get("category", ["0"])[0]) or None
        title = named.get("title", [None])[0]
        content = " ".join(named.get("content", [])) if "content" in named else None
        icon = named.get("icon", [None])[0]
        color = named.get("color", [None])[0]
        link_task = _safe_int(named.get("link_task", ["0"])[0]) or None
        link_shop = _safe_int(named.get("link_shop", ["0"])[0]) or None
        new_name = named.get("new_name", [None])[0]

        try:
            resp = await self.client.achievement(
                action=action, achievement_id=achievement_id, category=category,
                title=title, content=content, icon=icon, color=color,
                link_task=link_task, link_shop=link_shop, new_name=new_name,
            )
            yield event.plain_result(self._fmt_success(resp, f"成就{action}"))
        except Exception as exc:
            logger.error("achievement 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  K. 技能管理
    # ==================================================================

    @lifeup_group.command("skill_manage")
    async def skill_manage_cmd(self, event: AstrMessageEvent) -> None:
        """属性管理。用法：/lifeup skill_manage <add|delete|edit> [--id N] [--name 名称] [--color FFFFFF] [--icon 图标] [--new_name 新名称]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "skill_manage")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup skill_manage <add|delete|edit> [--id N] [--name ...]")
            return

        action = pos[0].lower()
        named = _extract_named_args(msg)

        skill_id = _safe_int(named.get("id", ["0"])[0]) or None
        name = named.get("name", [None])[0]
        color = named.get("color", [None])[0]
        icon = named.get("icon", [None])[0]
        new_name = named.get("new_name", [None])[0]

        try:
            resp = await self.client.skill(
                action=action, skill_id=skill_id, name=name,
                color=color, icon=icon, new_name=new_name,
            )
            yield event.plain_result(self._fmt_success(resp, f"属性{action}"))
        except Exception as exc:
            logger.error("skill 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  L. 商店设置
    # ==================================================================

    @lifeup_group.command("shop_settings")
    async def shop_settings_cmd(self, event: AstrMessageEvent) -> None:
        """商店设置。用法：/lifeup shop_settings [query|update] [--atm_rate 0.05] [--max_loan 1000] [--overdue_penalty 10]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "shop_settings")
        pos = _extract_positional_args(msg, expected=1)
        action = pos[0].lower() if pos else "query"
        named = _extract_named_args(msg)

        atm_rate = _safe_float(named.get("atm_rate", ["-1"])[0])
        max_loan = _safe_int(named.get("max_loan", ["-1"])[0])
        overdue_penalty = _safe_int(named.get("overdue_penalty", ["-1"])[0])

        try:
            resp = await self.client.shop_settings(
                action=action,
                atm_rate=atm_rate if atm_rate >= 0 else None,
                max_loan=max_loan if max_loan >= 0 else None,
                overdue_penalty=overdue_penalty if overdue_penalty >= 0 else None,
            )
            yield event.plain_result(self._fmt_success(resp, f"商店设置{action}"))
        except Exception as exc:
            logger.error("shop_settings 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  M. 子任务
    # ==================================================================

    @lifeup_group.command("subtask")
    async def subtask_cmd(self, event: AstrMessageEvent) -> None:
        """子任务管理。用法：/lifeup subtask <add|delete> <task_id> [--name 子任务名] [--subtask_id N]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "subtask")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup subtask <add|delete> <task_id> [--name 名称] [--subtask_id N]")
            return

        action = pos[0].lower()
        task_id = _safe_int(pos[1])
        named = _extract_named_args(msg)
        subtask_name = named.get("name", [None])[0]
        subtask_id = _safe_int(named.get("subtask_id", ["0"])[0]) or None

        try:
            resp = await self.client.subtask(
                action=action, task_id=task_id,
                subtask_name=subtask_name, subtask_id=subtask_id,
            )
            yield event.plain_result(self._fmt_success(resp, f"子任务{action}"))
        except Exception as exc:
            logger.error("subtask 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    @lifeup_group.command("subtask_check")
    async def subtask_check_cmd(self, event: AstrMessageEvent) -> None:
        """勾选/取消勾选子任务。用法：/lifeup subtask_check <subtask_id> [check|uncheck]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "subtask_check")
        pos = _extract_positional_args(msg, expected=2)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup subtask_check <subtask_id> [check|uncheck]")
            return

        subtask_id = _safe_int(pos[0])
        operation = pos[1].lower() if len(pos) > 1 else "check"

        try:
            resp = await self.client.subtask_operation(subtask_id=subtask_id, operation=operation)
            yield event.plain_result(self._fmt_success(resp, f"子任务{operation}"))
        except Exception as exc:
            logger.error("subtask_operation 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  N. 步数
    # ==================================================================

    @lifeup_group.command("step")
    async def step_cmd(self, event: AstrMessageEvent) -> None:
        """设置步数。用法：/lifeup step <steps>"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "step")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup step <步数>")
            return

        steps = _safe_int(pos[0])
        try:
            resp = await self.client.step(steps=steps)
            yield event.plain_result(self._fmt_success(resp, "设置步数"))
        except Exception as exc:
            logger.error("step 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  O. 合成配方管理
    # ==================================================================

    @lifeup_group.command("formula")
    async def formula_cmd(self, event: AstrMessageEvent) -> None:
        """合成配方管理。用法：/lifeup formula <query|add|delete> [--id N] [--name 名称] [--result N] [--materials item1:1 item2:2]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "formula")
        pos = _extract_positional_args(msg, expected=1)
        if not pos:
            yield event.plain_result("❌ 用法：/lifeup formula <query|add|delete> [--id N] [--name ...]")
            return

        action = pos[0].lower()
        named = _extract_named_args(msg)
        formula_id = _safe_int(named.get("id", ["0"])[0]) or None
        name = named.get("name", [None])[0]
        result_item = _safe_int(named.get("result", ["0"])[0]) or None
        materials = named.get("materials", [])

        try:
            resp = await self.client.synthesis_formula(
                action=action, formula_id=formula_id, name=name,
                result_item=result_item, materials=materials if materials else None,
            )
            yield event.plain_result(self._fmt_success(resp, f"合成配方{action}"))
        except Exception as exc:
            logger.error("synthesis_formula 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  P. 随机执行
    # ==================================================================

    @lifeup_group.command("random")
    async def random_cmd(self, event: AstrMessageEvent) -> None:
        """从多个API中随机执行一个。用法：/lifeup random <url1> <url2> [url3 ...]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "random")
        pos = _extract_positional_args(msg, expected=2)
        if len(pos) < 2:
            yield event.plain_result("❌ 用法：/lifeup random <API1> <API2> [API3 ...]\n示例：/lifeup random reward:coin:10 reward:exp:5")
            return

        urls = [f"lifeup://api/{p}" for p in pos]
        try:
            resp = await self.client.random_execute(urls=urls)
            yield event.plain_result(self._fmt_success(resp, "随机执行"))
        except Exception as exc:
            logger.error("random_execute 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  Q. 导出备份
    # ==================================================================

    @lifeup_group.command("export")
    async def export_cmd(self, event: AstrMessageEvent) -> None:
        """导出数据备份。用法：/lifeup export [--no_media]"""
        msg = _strip_cmd_prefix(event.message_str, "lifeup", "export")
        named = _extract_named_args(msg)
        with_media = "no_media" not in named

        try:
            resp = await self.client.export_backup(with_media=with_media)
            yield event.plain_result(self._fmt_success(resp, "导出备份"))
        except Exception as exc:
            logger.error("export_backup 失败: %s", exc)
            yield event.plain_result(self._api_error_msg(exc))

    # ==================================================================
    #  R. 帮助
    # ==================================================================

    @lifeup_group.command("help")
    async def help_cmd(self, event: AstrMessageEvent) -> None:
        """显示帮助信息。用法：/lifeup help"""
        help_text = """🎮 LifeUp (人升) 联动插件 — 指令帮助

📋 任务管理
  /lifeup tasks [category_id]       查看任务列表
  /lifeup add <名称> [--coin N] [--exp N] [--skills 1 2] [--freq N] [--type normal|count|pomodoro] [--notes ...] [--deadline N] [--reminder HH:mm]
  /lifeup complete <ID或名称> [--factor 1.0]  完成任务
  /lifeup giveup <ID或名称>          放弃任务
  /lifeup freeze <ID或名称>          冻结任务
  /lifeup unfreeze <ID或名称>        解冻任务
  /lifeup delete <ID或名称>          删除任务
  /lifeup edit <ID或名称> [--todo 新名] [--coin N] [--exp N] [--skills 1 2] [--freq N] [--notes ...] [--freeze true]

📊 查询
  /lifeup items [list_id]            商品列表
  /lifeup skills                      属性列表
  /lifeup coin                        金币余额
  /lifeup history [limit]             历史记录
  /lifeup achievements [category_id]  成就列表
  /lifeup categories <tasks|items|achievements|synthesis> [parent_id]  分类列表
  /lifeup pomodoro_records [limit]    番茄钟记录
  /lifeup synthesis [category_id]     合成配方
  /lifeup feelings [limit]            感想列表
  /lifeup info                        应用信息
  /lifeup status                      综合状态（金币+属性）

💰 经济系统
  /lifeup reward <amount> [原因] [--type coin|exp|item] [--item_name ...] [--skills 1 2]
  /lifeup penalty <amount> [原因] [--type coin|exp]
  /lifeup atm <deposit|withdraw> <金额>
  /lifeup editcoin <increase|decrease|set> <amount>
  /lifeup editexp <skill_id> <increase|decrease|set> <amount>

🛒 物品管理
  /lifeup buy <物品名或ID> [数量]
  /lifeup use <物品名或ID> [次数]
  /lifeup synthesize <配方ID> [次数]
  /lifeup item_add <名称> [价格] [--quantity N] [--desc 描述]
  /lifeup item_edit <ID或名称> [--price N] [--quantity N] [--delete]
  /lifeup loot <物品名或ID>           触发开箱

🍅 番茄钟
  /lifeup pomodoro <任务名> <分钟> [--no_reward]

📝 感想
  /lifeup feeling <内容> [--task N] [--achievement N] [--item N]

🍅 番茄管理
  /lifeup tomato <increase|decrease|set> <数值>

↩️ 历史操作
  /lifeup undo <history_id>           撤销任务完成

📂 清单管理
  /lifeup category <add|delete|edit> <tasks|items|achievements|synthesis> <名称> [--id N] [--new_name ...]

🏆 成就管理
  /lifeup achievement <add|delete|edit> [--id N] [--category N] [--title ...] [--content ...] [--icon ...] [--color ...]

📊 属性管理
  /lifeup skill_manage <add|delete|edit> [--id N] [--name ...] [--color FFFFFF] [--icon ...] [--new_name ...]

⚙️ 商店设置
  /lifeup shop_settings [query|update] [--atm_rate 0.05] [--max_loan 1000]

📌 子任务
  /lifeup subtask <add|delete> <task_id> [--name 子任务名] [--subtask_id N]
  /lifeup subtask_check <subtask_id> [check|uncheck]

👟 步数
  /lifeup step <步数>

⚗️ 合成配方
  /lifeup formula <query|add|delete> [--id N] [--name ...] [--result N] [--materials item1:1 item2:2]

🎲 随机执行
  /lifeup random <API1> <API2> [API3 ...]

💾 备份
  /lifeup export [--no_media]

❓ 帮助
  /lifeup help
"""
        yield event.plain_result(help_text)


    # ==================================================================
    #  LLM 工具注册（供 AI 自然语言调用）
    # ==================================================================

    # ------------------------------------------------------------------
    #  层次1：查询工具 —— AI 获取上下文、做决策
    # ------------------------------------------------------------------

    @filter.llm_tool(name="lifeup_query_tasks")
    async def llm_query_tasks(self, event: AstrMessageEvent, category_id: int | None = None) -> str:
        """查询人升(LifeUp)当前任务列表。

        当用户询问"我还有什么任务"、"今天要做的事"、"查看待办"时调用。
        返回任务ID、名称、状态、奖励等信息，供AI分析用户进度。

        Args:
            category_id (number): 清单ID，可选。不填则返回全部清单的任务。
        """
        try:
            resp = await self.client.query_tasks(category_id=category_id)
            tasks = resp.get("data", []) if isinstance(resp, dict) else resp
            return self._fmt_tasks(tasks)
        except Exception as exc:
            logger.error("llm_query_tasks 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_categories")
    async def llm_query_categories(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)所有已有的清单/分类。

        当需要帮用户创建新计划时，先调用此工具了解已有哪些分类，
        决定是复用现有分类还是新建分类。返回任务清单、物品分类、成就分类、合成分类。

        无参数。
        """
        try:
            tc_resp, ic_resp, ac_resp, sc_resp = await asyncio.gather(
                self.client.query_tasks_categories(),
                self.client.query_items_categories(),
                self.client.query_achievement_categories(),
                self.client.query_synthesis_categories(),
            )
            parts = []
            for label, resp in (
                ("任务清单", tc_resp), ("物品分类", ic_resp),
                ("成就分类", ac_resp), ("合成分类", sc_resp),
            ):
                data = resp.get("data", []) if isinstance(resp, dict) else resp
                if data:
                    lines = [f"📂 {label}："]
                    for c in data:
                        cid = c.get("id", "?")
                        name = c.get("name", "未命名")
                        lines.append(f"  [{cid}] {name}")
                    parts.append("\n".join(lines))
            return "\n\n".join(parts) if parts else "ℹ️ 暂无分类数据"
        except Exception as exc:
            logger.error("llm_query_categories 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_skills")
    async def llm_query_skills(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)属性（技能）列表与等级。

        当用户询问"我的属性多少级了"、"力量/智力等级"、或需要分析用户能力成长时调用。
        返回各属性的当前等级、经验值，供AI做属性规划和建议。

        无参数。
        """
        try:
            resp = await self.client.query_skills()
            skills = resp.get("data", []) if isinstance(resp, dict) else resp
            return self._fmt_skills(skills)
        except Exception as exc:
            logger.error("llm_query_skills 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_coin")
    async def llm_query_coin(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)当前金币余额。

        当用户问"我有多少金币"、"钱包余额"、或AI需要判断用户是否有足够资金购买物品时调用。

        无参数。
        """
        try:
            resp = await self.client.query_coin()
            coin_data = resp.get("data", {}) if isinstance(resp, dict) else resp
            return self._fmt_coin(coin_data)
        except Exception as exc:
            logger.error("llm_query_coin 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_items")
    async def llm_query_items(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)商店商品列表。

        当用户问"商店有什么"、"可以买什么"、或AI推荐用户购买奖励物品时调用。

        无参数。
        """
        try:
            resp = await self.client.query_items()
            items = resp.get("data", []) if isinstance(resp, dict) else resp
            return self._fmt_items(items)
        except Exception as exc:
            logger.error("llm_query_items 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_achievements")
    async def llm_query_achievements(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)成就列表与解锁状态。

        当用户问"我解锁了什么成就"、"还有什么成就没完成"时调用。

        无参数。
        """
        try:
            resp = await self.client.query_achievements()
            achievements = resp.get("data", []) if isinstance(resp, dict) else resp
            return self._fmt_achievements(achievements)
        except Exception as exc:
            logger.error("llm_query_achievements 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_history")
    async def llm_query_history(self, event: AstrMessageEvent, limit: int = 10) -> str:
        """查询人升(LifeUp)最近的历史任务记录。

        当需要分析用户近期完成情况、执行力、习惯养成进度时调用。

        Args:
            limit (number): 返回条数，默认10
        """
        try:
            resp = await self.client.query_history(limit=limit)
            history = resp.get("data", []) if isinstance(resp, dict) else resp
            return self._fmt_history(history)
        except Exception as exc:
            logger.error("llm_query_history 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_query_status")
    async def llm_query_status(self, event: AstrMessageEvent) -> str:
        """查询人升(LifeUp)综合状态（金币 + 属性列表）。

        当用户问"看看我的状态"、"我现在怎么样"、或AI需要全面评估用户当前情况时调用。

        无参数。
        """
        try:
            coin_resp, skills_resp = await asyncio.gather(
                self.client.query_coin(),
                self.client.query_skills(),
            )
            coin_data = coin_resp.get("data", {}) if isinstance(coin_resp, dict) else coin_resp
            skills = skills_resp.get("data", []) if isinstance(skills_resp, dict) else skills_resp
            return self._fmt_status(coin_data, skills)
        except Exception as exc:
            logger.error("llm_query_status 失败: %s", exc)
            return self._api_error_msg(exc)

    # ------------------------------------------------------------------
    #  层次2：基础操作 —— 单条任务/奖惩/物品
    # ------------------------------------------------------------------

    @filter.llm_tool(name="lifeup_add_task")
    async def llm_add_task(
        self, event: AstrMessageEvent,
        todo: str, notes: str = "",
        coin: int = 0, exp: int = 0,
        skills: list[int] | None = None,
        frequency: int = 0,
    ) -> str:
        """向人升(LifeUp)添加一条新任务。

        当用户要求"帮我建个任务"、"添加待办"、或AI自动为用户创建计划中的单个任务时调用。
        frequency 说明：0=单次任务，1=每日重复，-1=无限重复（习惯）。

        Args:
            todo (string): 任务标题，必填。例如"晨跑3公里"。
            notes (string): 备注说明，可选。
            coin (number): 完成奖励金币数，默认0。
            exp (number): 完成奖励经验值，默认0。
            skills (array[number]): 关联的属性ID列表，可选。例如[1,3]关联力量和智力。
            frequency (number): 频率，0=单次（默认），1=每日，-1=无限/习惯。
        """
        try:
            resp = await self.client.add_task(
                todo=todo, notes=notes,
                coin=coin if coin else None,
                exp=exp if exp else None,
                skills=skills if skills else None,
                frequency=frequency,
            )
            return self._fmt_success(resp, "添加任务")
        except Exception as exc:
            logger.error("llm_add_task 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_complete_task")
    async def llm_complete_task(self, event: AstrMessageEvent, task_name: str) -> str:
        """完成人升(LifeUp)中的一个任务。

        当用户说"我跑完步了"、"做完了"、"任务完成"时调用。

        Args:
            task_name (string): 任务名称或ID，必填。
        """
        try:
            task_id = _safe_int(task_name)
            if str(task_id) == task_name and task_id > 0:
                resp = await self.client.complete_task(task_id=task_id)
            else:
                resp = await self.client.complete_task(name=task_name)
            return self._fmt_success(resp, f"完成任务「{task_name}」")
        except Exception as exc:
            logger.error("llm_complete_task 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_give_up_task")
    async def llm_give_up_task(self, event: AstrMessageEvent, task_name: str) -> str:
        """放弃人升(LifeUp)中的一个任务。

        当用户说"这个任务不想做了"、"放弃吧"时调用。

        Args:
            task_name (string): 任务名称或ID，必填。
        """
        try:
            task_id = _safe_int(task_name)
            if str(task_id) == task_name and task_id > 0:
                resp = await self.client.give_up_task(task_id=task_id)
            else:
                resp = await self.client.give_up_task(name=task_name)
            return self._fmt_success(resp, f"放弃任务「{task_name}」")
        except Exception as exc:
            logger.error("llm_give_up_task 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_delete_task")
    async def llm_delete_task(self, event: AstrMessageEvent, task_name: str) -> str:
        """删除人升(LifeUp)中的一个任务。

        当用户要求"删掉这个任务"、"清除旧任务"时调用。

        Args:
            task_name (string): 任务名称或ID，必填。
        """
        try:
            task_id = _safe_int(task_name)
            if str(task_id) == task_name and task_id > 0:
                resp = await self.client.delete_task(task_id=task_id)
            else:
                resp = await self.client.delete_task(name=task_name)
            return self._fmt_success(resp, f"删除任务「{task_name}」")
        except Exception as exc:
            logger.error("llm_delete_task 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_reward")
    async def llm_reward(
        self, event: AstrMessageEvent,
        type: str, content: str, number: int,
        skills: list[int] | None = None,
    ) -> str:
        """给予人升(LifeUp)金币或经验奖励。

        当用户完成目标、AI需要正向激励、或执行奖励逻辑时调用。

        Args:
            type (string): 奖励类型，必填，enum: ["coin", "exp"]
            content (string): 奖励原因/描述，必填。例如"完成健身计划第3天"。
            number (number): 奖励数量，必填。
            skills (array[number]): 经验奖励时关联的属性ID列表，可选。
        """
        try:
            if type == "exp":
                resp = await self.client.reward_exp(content, number, skills=skills if skills else None)
            else:
                resp = await self.client.reward_coin(content, number)
            return self._fmt_success(resp, f"奖励{type}")
        except Exception as exc:
            logger.error("llm_reward 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_penalty")
    async def llm_penalty(
        self, event: AstrMessageEvent,
        type: str, content: str, number: int,
    ) -> str:
        """从人升(LifeUp)扣除金币或经验。

        当用户未完成目标、违反约定、或AI执行惩罚逻辑时调用。

        Args:
            type (string): 扣除类型，必填，enum: ["coin", "exp"]
            content (string): 扣除原因，必填。
            number (number): 扣除数量，必填。
        """
        try:
            resp = await self.client.penalty(type, content, number)
            return self._fmt_success(resp, f"惩罚扣除{type}")
        except Exception as exc:
            logger.error("llm_penalty 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_buy_item")
    async def llm_buy_item(
        self, event: AstrMessageEvent, item_name: str, quantity: int = 1,
    ) -> str:
        """购买人升(LifeUp)商店中的物品。

        当用户要求"买某个物品"、或AI推荐购买奖励/道具时调用。

        Args:
            item_name (string): 物品名称或ID，必填。
            quantity (number): 购买数量，默认1。
        """
        try:
            item_id = _safe_int(item_name)
            if str(item_id) == item_name and item_id > 0:
                resp = await self.client.purchase_item(item_id=item_id, quantity=quantity)
            else:
                resp = await self.client.purchase_item(item_name=item_name, quantity=quantity)
            return self._fmt_success(resp, "购买物品")
        except Exception as exc:
            logger.error("llm_buy_item 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_use_item")
    async def llm_use_item(
        self, event: AstrMessageEvent, item_name: str,
    ) -> str:
        """使用人升(LifeUp)背包中的物品。

        当用户要求"用某个物品"、"打开宝箱"、或AI建议消耗道具时调用。

        Args:
            item_name (string): 物品名称或ID，必填。
        """
        try:
            item_id = _safe_int(item_name)
            if str(item_id) == item_name and item_id > 0:
                resp = await self.client.use_item(item_id=item_id)
            else:
                resp = await self.client.use_item(item_name=item_name)
            return self._fmt_success(resp, "使用物品")
        except Exception as exc:
            logger.error("llm_use_item 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_pomodoro")
    async def llm_pomodoro(
        self, event: AstrMessageEvent,
        task_name: str, duration_minutes: int,
    ) -> str:
        """向人升(LifeUp)记录番茄钟（专注时间）。

        当用户说"刚才专注了25分钟"、"记录番茄钟"时调用。

        Args:
            task_name (string): 关联的任务名称，必填。
            duration_minutes (number): 专注时长（分钟），必填。
        """
        try:
            resp = await self.client.add_pomodoro(
                task_name=task_name, duration_minutes=duration_minutes,
            )
            return self._fmt_success(resp, "记录番茄钟")
        except Exception as exc:
            logger.error("llm_pomodoro 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_feeling")
    async def llm_feeling(
        self, event: AstrMessageEvent, content: str,
    ) -> str:
        """向人升(LifeUp)创建一条感想记录。

        当用户分享心情、总结一天、或AI需要记录里程碑事件时调用。

        Args:
            content (string): 感想内容，必填。
        """
        try:
            resp = await self.client.feeling(content=content)
            return self._fmt_success(resp, "创建感想")
        except Exception as exc:
            logger.error("llm_feeling 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_undo")
    async def llm_undo(self, event: AstrMessageEvent, history_id: int) -> str:
        """撤销人升(LifeUp)中的一次历史任务完成记录。

        当用户说"刚才点错了"、"撤销完成"时调用。

        Args:
            history_id (number): 历史记录ID，必填。
        """
        try:
            resp = await self.client.history_operation(history_id=history_id, operation="undo")
            return self._fmt_success(resp, "撤销历史完成")
        except Exception as exc:
            logger.error("llm_undo 失败: %s", exc)
            return self._api_error_msg(exc)

    # ------------------------------------------------------------------
    #  层次3：高级智能 —— 批量计划、智能分析、自动推荐
    # ------------------------------------------------------------------

    @filter.llm_tool(name="lifeup_batch_create_tasks")
    async def llm_batch_create_tasks(
        self, event: AstrMessageEvent,
        tasks: list[dict[str, Any]],
    ) -> str:
        """批量创建人升(LifeUp)任务计划。适用于为用户创建完整的学习/健身/工作计划。

        当用户要求"帮我制定一个30天健身计划"、"创建一个每日学习计划"、
        "批量添加本周任务"时调用。可以一次创建多个任务，支持设置分类、奖励、重复频率等。

        推荐流程：
        1. 先调用 lifeup_query_categories 查看已有分类
        2. 再调用此工具批量创建任务（指定合适的 category）

        Args:
            tasks (array[object]): 任务配置数组，每个元素包含：
                - todo (string, 必填): 任务标题
                - notes (string, 可选): 备注
                - coin (number, 可选): 金币奖励，建议简单任务5-10，困难任务20-50
                - exp (number, 可选): 经验奖励
                - skills (array[number], 可选): 关联属性ID
                - category (number, 可选): 清单分类ID（先查 categories 获取）
                - frequency (number, 可选): 0=单次(默认), 1=每日, -1=无限/习惯
                - task_type (number, 可选): 0=普通(默认), 1=计数任务, 4=番茄钟任务
        """
        try:
            resp = await self.client.batch_add_tasks(tasks)
            if isinstance(resp, dict):
                if resp.get("status") == "error":
                    return f"❌ 批量创建失败：{resp.get('message', '未知错误')}"
            return f"✅ 已批量创建 {len(tasks)} 个任务\n" + "\n".join(
                f"  • {t.get('todo', '未命名')}（💰{t.get('coin', 0)} 🧪{t.get('exp', 0)}）"
                for t in tasks
            )
        except Exception as exc:
            logger.error("llm_batch_create_tasks 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_analyze_and_recommend")
    async def llm_analyze_and_recommend(
        self, event: AstrMessageEvent,
        focus: str = "balance",
    ) -> str:
        """分析用户在人升(LifeUp)中的当前数据，并给出个性化的任务和属性建议。

        当用户要求"帮我分析一下"、"给我一些建议"、"怎么规划比较好"时调用。
        AI会自动查询任务、属性、金币、历史等数据，综合分析后返回建议文本。

        Args:
            focus (string): 分析维度，可选值：
                - "balance": 综合平衡分析（默认）
                - "tasks": 任务完成效率分析
                - "skills": 属性成长分析与建议
                - "routine": 日常习惯与规律分析
        """
        try:
            # 并行查询所有相关数据
            tasks_resp, skills_resp, coin_resp, history_resp = await asyncio.gather(
                self.client.query_tasks(),
                self.client.query_skills(),
                self.client.query_coin(),
                self.client.query_history(limit=20),
            )
            tasks = tasks_resp.get("data", []) if isinstance(tasks_resp, dict) else tasks_resp
            skills = skills_resp.get("data", []) if isinstance(skills_resp, dict) else skills_resp
            coin_data = coin_resp.get("data", {}) if isinstance(coin_resp, dict) else coin_resp
            history = history_resp.get("data", []) if isinstance(history_resp, dict) else history_resp

            # 分析数据
            total_tasks = len(tasks) if isinstance(tasks, list) else 0
            pending = sum(1 for t in (tasks if isinstance(tasks, list) else []) if t.get("status") == 0)
            completed_today = sum(1 for h in (history if isinstance(history, list) else []) if h.get("action") == "complete")

            coin_val = coin_data.get("value", coin_data.get("coin", "未知")) if isinstance(coin_data, dict) else coin_data

            # 属性分析
            skill_lines = []
            if isinstance(skills, list) and skills:
                lowest = min(skills, key=lambda s: s.get("level", 999))
                highest = max(skills, key=lambda s: s.get("level", 0))
                skill_lines = [
                    f"📊 属性分析：",
                    f"  最高：{highest.get('name', '?')} Lv.{highest.get('level', '?')}",
                    f"  最低：{lowest.get('name', '?')} Lv.{lowest.get('level', '?')}",
                    f"  建议：优先提升 {lowest.get('name', '?')}，可通过相关任务积累经验",
                ]

            # 构建建议报告
            lines = [
                "📋 LifeUp 个人分析报告",
                "",
                f"💰 金币余额：{coin_val}",
                f"📋 任务总览：{total_tasks}个任务，{pending}个待完成",
                f"📜 近期完成：{completed_today}次",
                "",
            ]
            lines.extend(skill_lines)
            lines.extend([
                "",
                "💡 建议：",
            ])

            if focus == "tasks":
                lines.append("  1. 优先完成高奖励的待办任务")
                lines.append("  2. 将大任务拆解为多个小任务，逐步完成")
                lines.append("  3. 对重复性任务设置每日频率，养成习惯")
            elif focus == "skills":
                if skills and isinstance(skills, list):
                    weak = min(skills, key=lambda s: s.get("level", 999))
                    lines.append(f"  1. 重点提升「{weak.get('name', '?')}」属性")
                    lines.append(f"  2. 创建关联{weak.get('name', '?')}的任务，每次完成获得经验")
                    lines.append("  3. 平衡发展，避免某项属性过于落后")
                else:
                    lines.append("  1. 先创建几个属性（如力量、智力、耐力）")
                    lines.append("  2. 为每个属性创建对应的日常任务")
            elif focus == "routine":
                lines.append("  1. 建立固定的每日任务清单（晨间/晚间例行）")
                lines.append("  2. 设置 frequency=1 的每日重复任务")
                lines.append("  3. 连续完成任务可获得额外金币奖励")
            else:  # balance
                lines.append("  1. 保持任务、属性、金币的均衡发展")
                lines.append("  2. 高价值任务配高奖励，简单任务保持低奖励")
                lines.append("  3. 定期回顾历史记录，调整任务难度")
                if pending > 5:
                    lines.append(f"  4. 当前有{pending}个待办任务，建议优先清理积压")
                elif total_tasks < 3:
                    lines.append("  4. 当前任务较少，建议制定一个完整的成长计划")

            return "\n".join(lines)
        except Exception as exc:
            logger.error("llm_analyze_and_recommend 失败: %s", exc)
            return self._api_error_msg(exc)

    @filter.llm_tool(name="lifeup_smart_reward")
    async def llm_smart_reward(
        self, event: AstrMessageEvent,
        task_name: str, difficulty: str = "medium",
        skills: list[int] | None = None,
        reason: str = "",
    ) -> str:
        """根据任务难度智能计算并执行奖励。适用于AI自动评估用户表现后给予激励。

        当用户完成任务、AI需要自动判断奖励额度时调用。
        会根据难度自动计算金币和经验值：简单5/2、中等15/8、困难30/20、极限60/50。

        Args:
            task_name (string): 完成的任务名称，必填。
            difficulty (string): 难度，enum: ["easy", "medium", "hard", "extreme"]，默认"medium"。
            skills (array[number]): 关联的属性ID，可选。
            reason (string): 奖励原因，可选。默认使用任务名。
        """
        try:
            coin, exp = LifeUpClient.smart_reward(difficulty)
            content = reason or f"完成「{task_name}」"

            # 先奖励金币
            resp_coin = await self.client.reward_coin(content, coin)
            # 再奖励经验
            if skills:
                resp_exp = await self.client.reward_exp(content, exp, skills=skills)
            else:
                resp_exp = await self.client.reward_exp(content, exp)

            skill_str = f"（关联属性：{skills}）" if skills else ""
            return (
                f"🎯 智能奖励 — 「{task_name}」\n"
                f"难度：{difficulty} | 💰+{coin}金币 🧪+{exp}经验{skill_str}\n"
                f"✅ 奖励已发放"
            )
        except Exception as exc:
            logger.error("llm_smart_reward 失败: %s", exc)
            return self._api_error_msg(exc)
