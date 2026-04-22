# AstrBot LifeUp 插件

AstrBot 联动插件，完整对接 [人升(LifeUp)](https://lifeupapp.fun/) App 的 HTTP API。支持通过聊天指令管理人升中的**全部功能模块**：任务、清单、属性、金币、物品、成就、番茄钟、感想、子任务、合成配方等。

> SDK 源码参考：[LifeUp-SDK](https://github.com/Ayagikei/LifeUp-SDK/blob/main/http/src/main/java/net/lifeupapp/lifeup/http/service/KtorService.kt)

## 功能概览

| 模块 | 说明 |
|------|------|
| **任务管理** | 增删改查、完成/放弃/冻结/解冻、编辑属性、子任务 |
| **经济系统** | 金币/经验奖励惩罚、ATM存取款、直接编辑余额、商店设置 |
| **物品管理** | 购买/使用/合成、商品CRUD（添加/编辑/删除）、开箱 |
| **属性管理** | 查询属性列表、属性CRUD（添加/编辑/删除） |
| **清单管理** | 任务/商品/成就/合成 分类CRUD |
| **成就管理** | 成就列表、成就CRUD |
| **番茄钟** | 记录/查询番茄钟 |
| **感想** | 创建/查询感想 |
| **数据备份** | 导出备份 |
| **LLM 工具** | 11个AI可调用的工具 |

## 安装方法

### 方式1：插件市场（推荐）
在 AstrBot WebUI 的插件市场中搜索 `astrbot_plugin_lifeup` 并安装。

### 方式2：手动安装
将本插件文件夹复制到 AstrBot 的 `data/plugins/` 目录下，在 WebUI 中重载插件。

## 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `api_url` | LifeUp HTTP API 地址 | `http://localhost:13276` |
| `api_token` | API 鉴权 Token（开启安全密钥时必填） | 空 |
| `default_category_id` | 默认清单 ID | 0 |
| `timeout` | API 请求超时（秒） | 5 |

### 开启 LifeUp HTTP API

1. 打开 LifeUp App → 设置 → 实验 → HTTP API
2. 开启 HTTP API 服务
3. 记录显示的 API 地址（如 `http://手机IP:13276`）

## 指令列表（45个）

所有指令以 `/lifeup` 为前缀。

### 任务管理（8个）

| 指令 | 用法 | 说明 |
|------|------|------|
| `tasks` | `/lifeup tasks [category_id]` | 查看任务列表 |
| `add` | `/lifeup add <名称> [--coin N] [--exp N] [--skills 1 2] [--freq N] [--type normal\|count\|pomodoro] [--notes ...] [--deadline N] [--reminder HH:mm]` | 添加任务 |
| `complete` | `/lifeup complete <ID或名称> [--factor 1.0]` | 完成任务 |
| `giveup` | `/lifeup giveup <ID或名称>` | 放弃任务 |
| `freeze` | `/lifeup freeze <ID或名称>` | 冻结任务 |
| `unfreeze` | `/lifeup unfreeze <ID或名称>` | 解冻任务 |
| `delete` | `/lifeup delete <ID或名称>` | 删除任务 |
| `edit` | `/lifeup edit <ID或名称> [--todo 新名] [--coin N] [--exp N] [--skills 1 2] [--freq N] [--notes ...] [--freeze true]` | 编辑任务 |

### 查询类（10个）

| 指令 | 用法 | 说明 |
|------|------|------|
| `items` | `/lifeup items [list_id]` | 商品列表 |
| `skills` | `/lifeup skills` | 属性列表 |
| `coin` | `/lifeup coin` | 金币余额 |
| `history` | `/lifeup history [limit]` | 历史记录 |
| `achievements` | `/lifeup achievements [category_id]` | 成就列表 |
| `categories` | `/lifeup categories <tasks\|items\|achievements\|synthesis> [parent_id]` | 分类列表 |
| `pomodoro_records` | `/lifeup pomodoro_records [limit]` | 番茄钟记录 |
| `info` | `/lifeup info` | 应用信息 |
| `synthesis` | `/lifeup synthesis [category_id]` | 合成配方 |
| `feelings` | `/lifeup feelings [limit]` | 感想列表 |

### 经济管理（5个）

| 指令 | 用法 | 说明 |
|------|------|------|
| `reward` | `/lifeup reward <amount> [原因] [--type coin\|exp\|item] [--item_name ...] [--skills 1 2]` | 奖励 |
| `penalty` | `/lifeup penalty <amount> [原因] [--type coin\|exp]` | 惩罚 |
| `atm` | `/lifeup atm <deposit\|withdraw> <金额>` | ATM操作 |
| `editcoin` | `/lifeup editcoin <increase\|decrease\|set> <amount>` | 编辑金币 |
| `editexp` | `/lifeup editexp <skill_id> <increase\|decrease\|set> <amount>` | 编辑经验 |

### 物品管理（6个）

| 指令 | 用法 | 说明 |
|------|------|------|
| `buy` | `/lifeup buy <物品名或ID> [数量]` | 购买物品 |
| `use` | `/lifeup use <物品名或ID> [次数]` | 使用物品 |
| `synthesize` | `/lifeup synthesize <配方ID> [次数]` | 执行合成 |
| `item_add` | `/lifeup item_add <名称> [价格] [--quantity N] [--desc 描述]` | 添加商品 |
| `item_edit` | `/lifeup item_edit <ID或名称> [--price N] [--quantity N] [--delete]` | 编辑/删除商品 |
| `loot` | `/lifeup loot <物品名或ID>` | 触发开箱 |

### 其他（16个）

| 指令 | 用法 | 说明 |
|------|------|------|
| `pomodoro` | `/lifeup pomodoro <任务名> <分钟> [--no_reward]` | 记录番茄钟 |
| `feeling` | `/lifeup feeling <内容> [--task N] [--achievement N]` | 创建感想 |
| `tomato` | `/lifeup tomato <increase\|decrease\|set> <数值>` | 调整番茄 |
| `undo` | `/lifeup undo <history_id>` | 撤销完成 |
| `category` | `/lifeup category <add\|delete\|edit> <类型> <名称> [--id N] [--new_name ...]` | 清单管理 |
| `achievement` | `/lifeup achievement <add\|delete\|edit> [--id N] [--category N] [--title ...]` | 成就管理 |
| `skill_manage` | `/lifeup skill_manage <add\|delete\|edit> [--id N] [--name ...] [--color FFFFFF]` | 属性管理 |
| `shop_settings` | `/lifeup shop_settings [query\|update] [--atm_rate 0.05] [--max_loan 1000]` | 商店设置 |
| `subtask` | `/lifeup subtask <add\|delete> <task_id> [--name ...] [--subtask_id N]` | 子任务管理 |
| `subtask_check` | `/lifeup subtask_check <subtask_id> [check\|uncheck]` | 勾选子任务 |
| `step` | `/lifeup step <步数>` | 设置步数 |
| `formula` | `/lifeup formula <query\|add\|delete> [--id N] [--name ...] [--result N] [--materials ...]` | 合成配方管理 |
| `random` | `/lifeup random <API1> <API2> [API3 ...]` | 随机执行 |
| `export` | `/lifeup export [--no_media]` | 导出备份 |
| `status` | `/lifeup status` | 综合状态 |
| `help` | `/lifeup help` | 显示帮助 |

## LLM 工具（11个）

| 工具名 | 功能 |
|--------|------|
| `lifeup_query_tasks` | 查询任务列表 |
| `lifeup_add_task` | 添加任务 |
| `lifeup_complete_task` | 完成任务 |
| `lifeup_reward` | 奖励金币/经验 |
| `lifeup_penalty` | 惩罚扣除 |
| `lifeup_query_status` | 查询状态 |
| `lifeup_pomodoro` | 记录番茄钟 |
| `lifeup_query_items` | 查询商品 |
| `lifeup_buy_item` | 购买物品 |
| `lifeup_use_item` | 使用物品 |
| `lifeup_feeling` | 创建感想 |

## API 客户端方法（55个）

详见 `lifeup_client.py`，涵盖：

- **20个GET查询端点**：tasks, history, items, categories(tasks/items/achievements/synthesis), info, skills, achievements, feelings, synthesis, pomodoro_records, coin, export
- **35个URL Scheme动作**：add/complete/give_up/freeze/unfreeze/delete/edit_task, reward/penalty/deposit/withdraw/edit_coin/edit_exp, purchase/use/synthesize/add_pomodoro/edit_pomodoro, feeling, tomato, history_operation, category, achievement, skill, task_template, shop_settings, subtask/subtask_operation, step, synthesis_formula, random_execute, add_item/item_edit/loot_box, query, toast, execute_raw_urls

## 依赖

- `aiohttp >= 3.8.0`

## 注意事项

- 确保 LifeUp App 的 HTTP API 已开启且网络可达
- 跨设备访问时需使用目标设备的 IP 地址
- 部分高级功能需要 LifeUp 会员

## License

MIT License
