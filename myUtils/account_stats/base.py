# -*- coding: utf-8 -*-
"""账号数据的公共契约：**统一的规范化结构** + 平台分发（at-ai-xhs 改造新增）。

为什么要先定一个统一结构
------------------------
四个平台的接口字段完全不一样：小红书的粉丝数叫 `fans_count`，抖音大概率是
`follower_count`，视频号又是另一套名字；而且不同平台能拿到的指标本身就不一样
（小红书有「观看来源」和「粉丝占比」，快手未必有）。如果让前端去认四套字段，
`AccountManagement.vue` 里会长出四份几乎一样的表格，改一个平台要动四个地方。

所以这里定一份**规范化结构**（见 `build_stats()` 的注释），平台适配器只负责
"把平台原始响应翻译成这份结构"。前端只认这一份；某个平台缺哪个指标就留空，
前端按「该平台没提供」渲染，而不是显示成 0——**0 和"没有这个数据"是两回事**，
混在一起会让人误以为账号真的零播放。

规范化结构
----------
```python
{
  "account":   {nickname, avatar, platform_no, platform_name, account_id, extra},
  "fans":      {count, following_count, likes_total, gained, lost, net,
                series: [{date, gained, lost, net, count}], periods: {...}},
  "engagement":{periods: {"7d": {...}, "30d": {...}}, summary},
  "media":     {supported: bool, items: [...], total, has_more},
  "fetched_at": "2026-10-01 11:40:00",
  "cache":     {"hit": bool, "age": 秒},
}
```

约定：
* **`periods` 的 key 固定为 `"7d"` / `"30d"`**。小红书原生就是 7 天 / 30 天；
  其它平台只有 7 天（或只有累计）时，只放存在的那个，前端按 "有哪些 tab" 渲染。
* **缺的指标一律不写 key**（或写 `None`，由适配器自己决定），不写 0。
* `series` 里的日期统一是 `"YYYY-MM-DD"` 本地时间字符串，和项目其它表
  （`publish_records.created_at` 等）保持一致，方便直接看、直接筛。

错误
----
* `StatsUnavailable` —— 平台/账号**不支持**或**暂时拿不到**，属于"预期内的失败"。
  Web 层据此返回一条**可读的说明**（例如"该平台未提供数据接口"），而不是 500。
  前端把这条说明原样显示在详情弹窗里，用户知道是"这个平台没有"，不是"工具坏了"。
* `AccountStatsError` —— 真出错了（网络、签名失败、响应结构变了）。
"""

from __future__ import annotations

import importlib
from typing import Any

#: 平台编号 → 适配器模块名。
#:
#: 编号沿用整个项目里那一套（`user_info.type` / `check_cookie()` 的 match 分支 /
#: 前端 `platformTypes`）：1 小红书 2 视频号 3 抖音 4 快手 5 拼多多视频。
#: 这里用**字符串模块名 + 延迟 import**，而不是在文件顶部直接 import 四个适配器：
#: 适配器各自要 import playwright 之类的重家伙，而本模块在 Flask 启动时就会被
#: 引用（Web 层要拿平台名做校验）；顶部 import 等于把浏览器的启动成本挂到启动路径上。
#:
#: 拼多多（5）走 `pdd` 适配器：它没有可直连的数据接口（请求都带页面现算的签名），
#: 所以和快手一样"开浏览器进创作者后台，让页面自己发请求、我们只收响应"。
ADAPTERS: dict[int, str] = {
    1: "xhs",
    2: "tencent",
    3: "douyin",
    4: "kuaishou",
    5: "pdd",
}

PLATFORM_NAMES: dict[int, str] = {
    1: "小红书",
    2: "视频号",
    3: "抖音",
    4: "快手",
    5: "拼多多视频",
}


class AccountStatsError(RuntimeError):
    """抓取账号数据时真出错了（网络失败 / 签名失败 / 响应结构变了）。"""


class StatsUnavailable(AccountStatsError):
    """平台或账号**暂时/根本**拿不到这份数据。

    单独一个类型是为了让 Web 层能把"这个平台没有这个能力"和"这次抓取炸了"
    分开：前者是一条可读说明（HTTP 200 + 说明文案），后者才是错误。
    """


def platform_supported(platform_no: int) -> bool:
    """这个平台有没有适配器。"""
    return int(platform_no or 0) in ADAPTERS


def platform_name(platform_no: int) -> str:
    return PLATFORM_NAMES.get(int(platform_no or 0), "未知平台")


def load_adapter(platform_no: int):
    """取某个平台的适配器模块。没有就抛 `StatsUnavailable`。

    用**相对 import**（`package=__package__`）而不是拼绝对模块名：后端既可以
    `import myUtils.account_stats`（测试 / 脚本），也可以 `python sau_backend.py`
    直接跑——后者会把本包注册成 `__main__`，那时候拼出来的
    `myUtils.account_stats.xhs` 是个**不存在的模块名**，报错还长得像
    "base 不是一个包"（实测踩过）。相对 import 两种情况都指向真正已加载的那个包。
    """
    name = ADAPTERS.get(int(platform_no or 0))
    if not name:
        raise StatsUnavailable(f"{platform_name(platform_no)}暂时没有账号数据接口")
    return importlib.import_module(f".{name}", package=__package__)


# --------------------------------------------------------------------------- #
# 规范化结构
# --------------------------------------------------------------------------- #


def canonicalize(value: Any) -> Any:
    """把平台给的数字收拾成前端好渲染的形状（递归作用在整份 stats 上）。

    两个平台的差异是实打实的，不统一会在界面上露出来：

    * **快手**把计数返回成浮点（`"playCount": 6763.0`、`sumCount: 18.0`），
      直接渲染就是"6763.0 次播放"；
    * **抖音**返回的是**字符串**（`"view_count": "40"`，适配器已经转过了，但
      漏一处就会让前端拿 `"188"` 去做加法变成 `"188" + 1 = "1881"`）。

    规则只有一条：**整数值的浮点/字符串 → int；真正的小数（比例、时长、完播率）保留**。
    反向的错也要防——不能把 `0.0432` 这种完播率硬转成 0，那比显示 `0.0432` 严重得多。
    所以判断依据是"这个浮点数是不是整数值"，而不是"它看起来像不像计数"。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，先挡掉，否则 True 会变成 1
        return value
    if isinstance(value, float):
        if value == int(value):
            return int(value)
        # 小数保留，但把浮点误差带来的长尾砍掉（0.043200000000000002 → 0.0432）
        return round(value, 4)
    if isinstance(value, dict):
        return {key: canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    return value


def build_stats(
    *,
    platform_no: int,
    account_name: str,
    nickname: str | None = None,
    avatar: str | None = None,
    account_id: str | None = None,
    fans: dict[str, Any] | None = None,
    engagement: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    fetched_at: str | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """把适配器抓到的碎片拼成规范化结构。

    适配器只管填自己拿得到的部分，缺失的段落这里是**空壳而不是 None**——
    前端渲染时不用到处判空（`stats.fans.count` 和 `stats.media.items` 永远存在）。
    """
    from datetime import datetime

    stats = {
        "account": {
            "name": account_name,
            "nickname": nickname,
            "avatar": avatar,
            "account_id": account_id,
            "platform_no": int(platform_no or 0),
            "platform_name": platform_name(platform_no),
        },
        "fans": fans or {},
        "engagement": engagement or {},
        "media": media or {"supported": False, "items": [], "total": None, "has_more": False},
        # 平台特有的补充数据（例如小红书的「观看来源」、账号简介）。
        # 故意单独放一层：前端只认上面那几段，这里的变化不会波及主结构。
        "extra": extra or {},
        # 抓取过程中的说明（例如「作品列表只取了最近 20 条」）。
        # 用来解释"为什么数字看起来不全"，而不是让用户猜。
        "notes": notes or [],
        "fetched_at": fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return canonicalize(stats)


def fetch(
    platform_no: int,
    account_file: str,
    account_name: str,
    *,
    with_media: bool = True,
    media_limit: int = 20,
    wait_timeout: float = 20.0,
) -> dict[str, Any]:
    """抓一个账号的数据（**阻塞**，内部会开浏览器，必须在工作线程里调用）。

    `account_file` 是 `cookiesFile/` 下的文件名（`user_info.filePath` 那一列），
    不是绝对路径——解析成绝对路径是适配器的事，因为四个平台的 cookie 文件
    不在同一个目录（视频号的登录态其实躺在持久化 profile 里，见 tencent 适配器）。

    `wait_timeout` 是"等这个账号的浏览器空出来"的上限（秒），转发给适配器：

    * **详情页手动打开**（默认 20）——用户在看，值得等一小会儿；
    * **定时任务批量同步**（`sync.py` 传 15）——在一个账号上干等会把它后面所有账号
      都推后，所以等不到就跳过，下一个时间点还会再来；
    * **发布任务正占着这个账号**时，超时会被翻译成"账号正忙"，而不是硬卡住。
    """
    adapter = load_adapter(platform_no)
    return adapter.fetch(
        account_file=account_file,
        account_name=account_name,
        with_media=with_media,
        media_limit=media_limit,
        wait_timeout=wait_timeout,
    )


__all__ = [
    "ADAPTERS",
    "PLATFORM_NAMES",
    "AccountStatsError",
    "StatsUnavailable",
    "build_stats",
    "fetch",
    "load_adapter",
    "platform_name",
    "platform_supported",
]
