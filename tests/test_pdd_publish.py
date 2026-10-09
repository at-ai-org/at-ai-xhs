# Language: 中文
"""拼多多视频（多多视频）接入的回归测试。

三条契约，每一条都对应一个"真出过事或者一定会出事"的点：

1. **发布中心的接线**：`PLATFORMS` / `PUBLISH_STRATEGY` 里必须有 5，且
   `_invoke_uploader` 能按 5 造出 `PddVideo`——少一处就是"平台上勾不到"或者
   "勾了报不支持的平台类型"。
2. **平台侧的文本硬约束**：标题 ≤ 40 字且不能带话题、话题写在描述里且单个 ≤ 15 字。
   这些都来自拼多多发布页自己的校验文案，超出会被平台直接拒，所以在本地先收敛掉。
3. **登录态校验的分发**：`check_cookie(5, …)` 必须打到拼多多那条实现上，
   而且 `save_state` 要能透传（会话续期靠它回写轮换后的票据）。
"""

import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import conf
from uploader import pdd_uploader
from uploader.pdd_uploader import main as pdd_main


class PddPlatformWiringTests(unittest.TestCase):
    def test_publish_center_knows_platform_5(self):
        from sau_backend_publish import PLATFORMS, PUBLISH_STRATEGY

        self.assertIn(5, PLATFORMS)
        self.assertEqual(PLATFORMS[5]["name"], "拼多多视频")
        self.assertEqual(
            PUBLISH_STRATEGY[5],
            (pdd_main.PDD_PUBLISH_STRATEGY_IMMEDIATE, pdd_main.PDD_PUBLISH_STRATEGY_SCHEDULED),
        )

    def test_invoke_uploader_builds_pdd_uploader(self):
        """平台 5 走到拼多多那条分支，参数按发布中心的形状传下去。"""
        import sau_backend_publish as sp

        captured = {}

        class _FakePdd:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                self.step_results = []

            def main(self):
                # 返回一个"协程占位"就够了：这一步测的是**参数怎么传下去**，
                # 真正的执行被 _run_uploader_async 挡掉了（它是唯一开浏览器的地方）。
                return MagicMock()

        task = {
            "task_id": "t1",
            "platform": 5,
            "material": "demo.mp4",
            "account_file": "acc.json",
            "title": "标题",
            "desc": "描述",
            "tags": ["话题"],
            "publish_at": 0,
            "options": {"thumbnail": ""},
        }
        with patch.object(sp, "PddVideo", _FakePdd), patch.object(
            sp, "_run_uploader_async"
        ) as run, patch.object(sp, "_attach_app"):
            sp._invoke_uploader(task)

        self.assertTrue(run.called)
        args = captured["args"]
        self.assertEqual(args[0], "标题")
        self.assertTrue(str(args[1]).endswith("demo.mp4"))
        self.assertEqual(args[2], ["话题"])
        self.assertEqual(args[3], 0)
        self.assertTrue(str(args[4]).endswith("acc.json"))
        self.assertEqual(
            captured["kwargs"]["publish_strategy"], pdd_main.PDD_PUBLISH_STRATEGY_IMMEDIATE
        )

    def test_check_cookie_dispatches_to_pdd(self):
        """`check_cookie(5, …)` → 拼多多的校验实现，且 save_state 要透传。"""
        from myUtils import auth

        calls = {}

        async def _fake(account_file, save_state=False):
            calls["file"] = str(account_file)
            calls["save_state"] = save_state
            return True

        import asyncio

        with patch.object(auth, "cookie_auth_pdd", _fake):
            ok = asyncio.run(auth.check_cookie(5, "abc.json", save_state=True))

        self.assertTrue(ok)
        self.assertTrue(calls["save_state"])
        self.assertTrue(calls["file"].endswith("abc.json"))

    def test_login_types_and_names_cover_pdd(self):
        from myUtils import login
        from myUtils import session_renew

        self.assertEqual(login.LOGIN_URLS[5], "https://live.pinduoduo.com/login")
        self.assertEqual(login._PLATFORM_NAMES[5], "拼多多视频")
        self.assertEqual(session_renew.PLATFORM_NAMES[5], "拼多多视频")

    def test_account_stats_reports_unavailable_instead_of_crashing(self):
        """拼多多还没有数据适配器：结论应该是"暂时没有接口"，而不是异常。"""
        from myUtils.account_stats import StatsUnavailable, fetch, platform_name

        self.assertEqual(platform_name(5), "拼多多视频")
        with self.assertRaises(StatsUnavailable):
            fetch(5, "whatever.json", "测试账号")


class PddTextRulesTests(unittest.TestCase):
    """平台的文本约束（全部来自它自己前端产物里的校验/提示文案）。"""

    def _uploader(self, title, tags, desc=""):
        return pdd_main.PddVideo(title, "videoFile/x.mp4", tags, 0, "cookiesFile/x.json", desc=desc)

    def test_title_goes_into_desc_because_video_has_no_title_field(self):
        """**视频发布没有标题字段**，所以标题必须落到描述里。

        证据：平台发布载荷里 `title` 只出现在 `feedType === IMAGE`（图文）那一支，
        视频那一支是 `desc / cover / vid / scheduleTime / goodsIdList…`；
        发布页上 placeholder=「添加标题」的输入框也只在图文时渲染。
        标题丢了比报错更糟——没人会发现。
        """
        uploader = self._uploader("海边日落", ["旅行"])
        text = uploader._compose_desc()
        self.assertTrue(text.startswith("海边日落"), text)
        self.assertIn("#旅行", text)

    def test_title_is_omitted_when_page_has_a_title_input(self):
        """页面真有标题框（图文场景）时，标题单独填，不重复进描述。"""
        uploader = self._uploader("海边日落", ["旅行"], desc="正文")
        text = uploader._compose_desc(title_in_desc=False)
        self.assertNotIn("海边日落", text)
        self.assertIn("正文", text)

    def test_title_is_trimmed_to_platform_limit(self):
        uploader = self._uploader("长" * 60, [])
        self.assertEqual(len(uploader._build_title()), conf.PDD_TITLE_MAX)

    def test_title_never_contains_topics(self):
        """平台明确报「标题不允许添加话题」，所以在本地就把 # 去掉。"""
        uploader = self._uploader("夏天喝这个#冰美式", [])
        self.assertNotIn("#", uploader._build_title())

    def test_topics_are_capped_at_five(self):
        """平台提示「最多可以添加5个相关话题」，编辑器里 limit:5。"""
        uploader = self._uploader("标题", [f"话题{i}" for i in range(9)])
        text = uploader._compose_desc(title_in_desc=False)
        self.assertEqual(text.count("#"), conf.PDD_TOPIC_MAX_COUNT)

    def test_overlong_topic_is_dropped(self):
        """单个话题超过 15 字平台不收——宁可不带，也不要整条发布被拒。"""
        uploader = self._uploader("标题", ["超" * 20, "正常话题"])
        text = uploader._compose_desc(title_in_desc=False)
        self.assertNotIn("超超", text)
        self.assertIn("#正常话题", text)

    def test_desc_is_trimmed_to_platform_limit(self):
        uploader = self._uploader("标题", [], desc="字" * (conf.PDD_DESC_MAX + 120))
        self.assertEqual(len(uploader._compose_desc(title_in_desc=False)), conf.PDD_DESC_MAX)

    def test_empty_desc_and_tags_stay_empty(self):
        self.assertEqual(self._uploader("标题", [])._compose_desc(title_in_desc=False), "")

    def test_step_records_match_publish_center_shape(self):
        """执行步骤要能被发布中心直接渲染（读的是 status，不是 ok）。"""
        uploader = self._uploader("标题", [])
        uploader._record("upload_video", "ok", "视频上传完成")
        step = uploader.step_results[0]
        self.assertEqual(set(step), {"step", "status", "message", "detail"})
        self.assertEqual(step["status"], "ok")


class PddCoverSpecTests(unittest.TestCase):
    """封面规格：平台文案「需上传封面比例为3:4且分辨率大于720P；文件大小不超过5M」。"""

    def _fit(self, size):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "frame.jpg"
            Image.new("RGB", size, (140, 170, 200)).save(src)
            out = pdd_main._fit_cover_spec(src, tmp_path)
            self.assertIsNotNone(out, f"{size} 应该能处理成功")
            with Image.open(out) as image:
                return image.size, out.stat().st_size

    def test_landscape_frame_is_cropped_to_three_by_four(self):
        (width, height), _ = self._fit((1280, 720))
        self.assertAlmostEqual(width / height, conf.PDD_COVER_RATIO[0] / conf.PDD_COVER_RATIO[1], places=2)

    def test_vertical_frame_is_cropped_to_three_by_four(self):
        """9:16 竖版是多多视频最常见的素材，它比 3:4 更窄 → 裁上下。"""
        (width, height), _ = self._fit((1080, 1920))
        self.assertAlmostEqual(width / height, conf.PDD_COVER_RATIO[0] / conf.PDD_COVER_RATIO[1], places=2)
        self.assertEqual(width, 1080)  # 竖版不该被缩掉宽度

    def test_tiny_frame_is_upscaled_to_min_width(self):
        (width, _), _ = self._fit((320, 240))
        self.assertGreaterEqual(width, conf.PDD_COVER_MIN_WIDTH)

    def test_already_correct_frame_is_left_alone(self):
        (width, height), _ = self._fit((720, 960))
        self.assertEqual((width, height), (720, 960))

    def test_file_stays_under_size_limit(self):
        _, size = self._fit((2160, 3840))
        self.assertLessEqual(size, conf.PDD_COVER_MAX_MB * 1024 * 1024)


class PddResponseVerdictTests(unittest.TestCase):
    """发布接口回执的判读。

    真实事故（2026-10-08 晚）：平台的成功信封是
    `{"success": True, "errorCode": 1000000, "errorMsg": None, "result": {...}}`
    ——**成功时 errorCode 也是非空的**。当时的判读写的是"有 errorCode 就算失败"，
    于是两次**已经发布成功**的任务被记成失败（平台上作品都在，发布记录写"失败"）。
    """

    def test_real_success_envelope_is_success(self):
        """平台的成功信封：success=true + errorCode=1000000 + errorMsg=null。"""
        body = {
            "success": True,
            "errorCode": 1000000,
            "errorMsg": None,
            "result": {"pushPxqStatus": 1},
        }
        self.assertEqual(pdd_main._publish_response_error(body), "")

    def test_error_message_wins(self):
        self.assertEqual(
            pdd_main._publish_response_error({"success": False, "errorMsg": "请添加封面"}),
            "请添加封面",
        )

    def test_success_false_without_message_reports_code(self):
        self.assertIn("400010", pdd_main._publish_response_error({"success": False, "errorCode": 400010}))

    def test_unknown_error_code_is_failure(self):
        self.assertTrue(pdd_main._publish_response_error({"errorCode": 500}))

    def test_zero_and_known_ok_codes_are_success(self):
        self.assertEqual(pdd_main._publish_response_error({"success": True, "errorCode": 0}), "")
        self.assertEqual(pdd_main._publish_response_error({"errorCode": 1000000}), "")

    def test_missing_or_odd_bodies_are_success(self):
        self.assertEqual(pdd_main._publish_response_error(None), "")
        self.assertEqual(pdd_main._publish_response_error({}), "")
        self.assertEqual(pdd_main._publish_response_error({"pushPxqStatus": 1}), "")


class PddValidateTests(unittest.TestCase):
    def test_missing_video_file_is_rejected(self):
        uploader = pdd_main.PddVideo("标题", "videoFile/不存在的文件.mp4", [], 0, __file__)
        with self.assertRaises(FileNotFoundError):
            uploader.validate_upload_args()

    def test_missing_cookie_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "demo.mp4"
            video.write_bytes(b"fake")
            uploader = pdd_main.PddVideo("标题", str(video), [], 0, "cookiesFile/不存在.json")
            with self.assertRaises(FileNotFoundError) as ctx:
                uploader.validate_upload_args()
            self.assertIn("账号管理", str(ctx.exception))

    def test_scheduled_without_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "demo.mp4"
            video.write_bytes(b"fake")
            uploader = pdd_main.PddVideo(
                "标题",
                str(video),
                [],
                0,
                str(video),
                publish_strategy=pdd_main.PDD_PUBLISH_STRATEGY_SCHEDULED,
            )
            with self.assertRaises(ValueError):
                uploader.validate_upload_args()

    def test_schedule_too_close_is_rejected(self):
        """沿用基类的 2 小时提前量：太近的定时发布平台侧本来也不会收。"""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "demo.mp4"
            video.write_bytes(b"fake")
            uploader = pdd_main.PddVideo(
                "标题",
                str(video),
                [],
                datetime.now() + timedelta(minutes=10),
                str(video),
                publish_strategy=pdd_main.PDD_PUBLISH_STRATEGY_SCHEDULED,
            )
            with self.assertRaises(ValueError):
                uploader.validate_upload_args()


class PddCoverTests(unittest.TestCase):
    """封面：拼多多没有封面不许发布，所以"抽首帧兜底"这条路必须可靠、且不重复劳动。"""

    def test_cached_cover_is_reused_without_touching_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "素材.mp4"
            video.write_bytes(b"fake")
            covers = Path(tmp) / "covers"
            covers.mkdir()
            cached = covers / "素材.jpg"
            cached.write_bytes(b"already-a-cover")

            # PDD_DEBUG_DIR 置空：函数内部再拼一层 "covers"，这里就让它正好落在 tmp/covers。
            with patch.object(pdd_main, "BASE_DIR", tmp), patch.object(
                pdd_main, "PDD_DEBUG_DIR", ""
            ), patch(
                "uploader.xiaohongshu_uploader.ai_copywriter.extract_key_frames"
            ) as extract:
                self.assertEqual(pdd_main._auto_cover_frame(str(video)), str(cached))
                # 有缓存就不该再去抽帧（抽一次要跑 ffmpeg，是发布路上的纯开销）
                extract.assert_not_called()

    def test_extraction_failure_is_not_fatal(self):
        """抽帧失败只该返回 None（由页面校验兜底报错），不能让发布在本地就炸掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "素材.mp4"
            video.write_bytes(b"fake")
            with patch.object(pdd_main, "BASE_DIR", tmp), patch.object(
                pdd_main, "PDD_DEBUG_DIR", ""
            ), patch(
                "uploader.xiaohongshu_uploader.ai_copywriter.extract_key_frames",
                side_effect=RuntimeError("ffmpeg 不在"),
            ):
                self.assertIsNone(pdd_main._auto_cover_frame(str(video)))


class _FakeLocator:
    """模拟 Playwright 的 Locator：同一选择器下可能有多个元素，只有部分可见。"""

    def __init__(self, visibilities: list[bool], value: str = "", page=None):
        self._vis = visibilities
        self._value = value
        self.clicked = False
        self.page = page

    async def count(self) -> int:
        return len(self._vis)

    def nth(self, index: int):
        return _FakeElement(self._vis[index], self, index)

    @property
    def first(self):
        return _FakeElement(self._vis[0] if self._vis else False, self, 0)


class _FakeElement:
    def __init__(self, visible: bool, owner: _FakeLocator, index: int):
        self._visible = visible
        self._owner = owner
        self._index = index

    async def is_visible(self) -> bool:
        return self._visible

    async def click(self, timeout: int | None = None) -> None:
        self._owner.clicked = True
        if self._owner.page is not None:
            self._owner.page.clicked = True

    async def input_value(self) -> str:
        return self._owner._value


class _FakePage:
    """够用的假页面：按选择器回答"有几个、哪个可见"，按 JS 内容回答文本。

    `toast_after_click=True` 时，提示层**点了按钮之后才出现**——真实平台就是这样
    （`_submit` 会记一份"点之前的提示层"做基线，只认之后新出现的那句）。
    """

    def __init__(self, *, visible_text: str = "", toast_text: str = "",
                 toast_after_click: bool = False, **selectors):
        self.visible_text = visible_text
        self.toast_text = toast_text
        self.toast_after_click = toast_after_click
        self.clicked = False
        self.selectors = selectors  # 选择器 → _FakeLocator
        self.actions: list[str] = []

    def locator(self, selector: str):
        found = self.selectors.get(selector)
        if found is None:
            return _FakeLocator([])
        found.page = self
        return found

    async def evaluate(self, js: str):
        if "innerText" in js and "toast" not in js and "class*=" not in js:
            return self.visible_text
        if "toast" in js:
            if self.toast_after_click and not self.clicked:
                return ""
            return self.toast_text
        return ""

    async def content(self):
        # 故意让源码里躺着"发布成功"——真实平台也是这样（打包产物里的文案常量）
        return "<html><body>...</body><script>var t='发布成功';</script></html>"

    def on(self, event, handler):
        self.actions.append(f"on:{event}")

    def remove_listener(self, event, handler):
        self.actions.append(f"off:{event}")


class PddPageInteractionTests(unittest.TestCase):
    """离线自测抓出来的两个真 bug，在这里钉死。"""

    def test_first_visible_skips_hidden_matches(self):
        """`.first` 是"DOM 顺序第一个"，不是"第一个可见的"。

        假页面里隐藏的「仍要发布」排在真「发布」按钮前面时，用 `.first` 会永远命中隐藏的那个，
        于是卡在"找不到发布按钮"——而页面上明明有一个可见的按钮。
        """
        import asyncio

        page = _FakePage()
        page.selectors['button:has-text("发布")'] = _FakeLocator([False, True])
        found = asyncio.run(
            pdd_main._first_visible(page, ('button:has-text("发布")',), timeout_ms=0)
        )
        self.assertIsNotNone(found)
        self.assertTrue(asyncio.run(found.is_visible()))

    def test_first_visible_returns_none_when_all_hidden(self):
        import asyncio

        page = _FakePage()
        page.selectors['button:has-text("发布")'] = _FakeLocator([False, False])
        self.assertIsNone(
            asyncio.run(pdd_main._first_visible(page, ('button:has-text("发布")',), timeout_ms=0))
        )

    def test_submit_does_not_treat_script_constants_as_success(self):
        """源码里带着「发布成功」这几个字（打包产物常有的文案常量）不算成功。

        判定必须走 `innerText`（渲染出来的文本），否则每次发布都会报假成功——
        这是"发布记录写成功、平台上什么都没有"那类最难查的错。
        """
        import asyncio

        page = _FakePage(
            visible_text="发布视频 上传视频 标题 发布",  # 真实页面文本里**没有**发布成功
            toast_text="",
        )
        page.selectors['[class*="singlePublish"] button'] = _FakeLocator([True])

        with patch.object(pdd_main, "PDD_SUBMIT_TIMEOUT_MS", 1), patch.object(
            pdd_main, "PDD_SUBMIT_GRACE_SECONDS", 0
        ), patch.object(pdd_main, "PDD_DEBUG_DUMP", False), patch.object(
            pdd_main, "_dump", AsyncMock(return_value="")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(pdd_main._submit(page))
        # 没有接口回执、卡片状态也没变 → 结论应该是"结论未知，请人工核对"，而不是"发布成功"
        self.assertIn("核对", str(ctx.exception))

    def test_submit_reports_rejection_from_toast(self):
        """拒稿原因在 toast 里 → 必须抛错，并把平台原因翻译成可操作的建议。"""
        import asyncio

        page = _FakePage(
            visible_text="上传视频 # 添加话题", toast_text="请添加封面", toast_after_click=True
        )
        page.selectors['[class*="singlePublish"] button'] = _FakeLocator([True])

        with patch.object(pdd_main, "PDD_SUBMIT_GRACE_SECONDS", 0), patch.object(
            pdd_main, "PDD_DEBUG_DUMP", False
        ), patch.object(pdd_main, "_dump", AsyncMock(return_value="")):
            with self.assertRaises(pdd_main.PddPublishRejected) as ctx:
                asyncio.run(pdd_main._submit(page))
        self.assertIn("封面", str(ctx.exception))
        self.assertIn("弹窗提示", str(ctx.exception))
        # 缺封面是**唯一值得补救**的拒绝：上层会补一张封面再试一次
        self.assertEqual(ctx.exception.code, "missing_cover")

    def test_static_label_is_not_treated_as_rejection(self):
        """页面上静态就有的「请添加封面」（封面入口的文案）不能被当成拒稿。"""
        import asyncio

        page = _FakePage(visible_text="上传视频 请添加封面", toast_text="")  # 静态文案，无 toast
        page.selectors['[class*="singlePublish"] button'] = _FakeLocator([True])
        # 已发布 = 卡片按钮文案变成「已发布」（不能用 video-list_success：那是"上传成功"）
        page.selectors['[class*="singlePublish"] button:has-text("已发布")'] = _FakeLocator([True])

        with patch.object(pdd_main, "PDD_SUBMIT_GRACE_SECONDS", 0), patch.object(
            pdd_main, "PDD_DEBUG_DUMP", False
        ), patch.object(pdd_main, "_dump", AsyncMock(return_value="")):
            # 不该抛错：静态的「请添加封面」不是拒稿，卡片状态才是判据
            asyncio.run(pdd_main._submit(page))


class PddWiringTests(unittest.TestCase):
    """目标里那几处"接线"：登录分发、会话续期、发布记录 —— 少一处功能就是残的。"""

    def test_login_dispatch_type_5_calls_pdd_login(self):
        """`/login?type=5` 必须打到拼多多的达人扫码登录上。"""
        import sau_backend

        called = {}

        async def _fake_cookie_gen(name, status_queue):
            called["name"] = name
            called["queue"] = status_queue
            status_queue.put("200")

        queue_stub = MagicMock()
        queue_stub.put = lambda value: called.setdefault("status", value)

        with patch.object(sau_backend, "get_pdd_cookie", _fake_cookie_gen):
            sau_backend._run_login_task("5", "pdd-达人号", queue_stub)

        self.assertEqual(called["name"], "pdd-达人号")
        self.assertEqual(called.get("status"), "200")

    def test_unknown_login_type_does_not_silently_pass(self):
        """未知 type 要让界面报错，而不是永远转圈（拼多多这次是加第 5 个分支）。"""
        import sau_backend

        queue_stub = MagicMock()
        sau_backend._run_login_task("99", "x", queue_stub)
        queue_stub.put.assert_called_with("500")

    def test_session_renew_covers_pdd_accounts(self):
        """会话续期对 type=5 的账号要真的去校验（不能只是列表里有名字）。

        `renew_account` 是同步函数：内部自己 `asyncio.run(check_cookie(...))`，
        并且会把 `save_state=True` 传下去——续期不回写 cookie 等于白续一次
        （平台轮换掉的新票据随浏览器一起销毁）。
        """
        import asyncio

        from myUtils import session_renew

        calls = {}

        async def _fake_check(platform_type, file_name, save_state=False):
            calls["platform"] = platform_type
            calls["file"] = file_name
            calls["save_state"] = save_state
            return True

        with tempfile.TemporaryDirectory() as tmp:
            cookie_dir = Path(tmp)
            (cookie_dir / "pdd.json").write_text("{}", encoding="utf-8")
            account = {
                "id": 7,
                "type": 5,
                "file_path": "pdd.json",
                "name": "pdd-达人号",
                "platform": "拼多多视频",
            }
            with patch.object(session_renew, "COOKIE_DIR", cookie_dir), patch.object(
                session_renew, "check_cookie", _fake_check
            ), patch.object(session_renew, "_mark_invalid") as mark_invalid:
                result = session_renew.renew_account(account, with_stats=False)

        self.assertEqual(calls.get("platform"), 5, "续期必须把 type=5 原样传下去")
        self.assertTrue(calls.get("save_state"), "续期要回写 cookie（否则等于白续一次）")
        mark_invalid.assert_not_called()
        self.assertEqual(result["status"], "ok")

    def test_publish_record_uses_pdd_platform_name(self):
        """发布记录里平台名要写「拼多多视频」，不能被记成"平台5"。"""
        import tempfile

        import myUtils.publish_records as records

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "database.db"
            with patch.object(records, "DB_PATH", db_path):
                records.ensure_tables()
                record_id = records.insert_record(
                    records.new_record(
                        task_id="t-pdd",
                        source=records.SOURCE_PUBLISH_CENTER,
                        platform=5,
                        platform_name="拼多多视频",
                        account_name="pdd-达人号",
                        material="demo.mp4",
                        title="标题",
                        status=records.STATUS_QUEUED,
                    )
                )
                with records.db() as conn:
                    row = records.record_row(
                        conn.execute(
                            f"SELECT {records.select_columns()} FROM publish_records WHERE id = ?",
                            (record_id,),
                        ).fetchone()
                    )
        self.assertEqual(row["platform"], 5)
        self.assertEqual(row["platform_name"], "拼多多视频")

    def test_build_tasks_builds_pdd_task(self):
        """发布中心把「素材 × 平台5 × 账号」拼成任务时，平台名/账号/策略都要对。"""
        import tempfile

        import sau_backend_publish as BP

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "database.db"
            import myUtils.publish_records as records

            with patch.object(records, "DB_PATH", db_path):
                records.ensure_tables()
                with records.db() as conn:
                    conn.execute(
                        "CREATE TABLE user_info (id INTEGER PRIMARY KEY, userName TEXT, type INTEGER, filePath TEXT)"
                    )
                    conn.execute(
                        "CREATE TABLE file_records (id INTEGER PRIMARY KEY, file_path TEXT, filename TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO user_info (id, userName, type, filePath) VALUES (9, 'pdd-达人号', 5, 'pdd.json')"
                    )

                options, tasks = BP._build_tasks(
                    {
                        "items": [{"material": "demo.mp4", "title": "标题", "desc": "正文", "tags": ["话题"]}],
                        "targets": [{"platform": 5, "account_ids": [9]}],
                        "options": {},
                    }
                )

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["platform"], 5)
        self.assertEqual(task["platform_name"], "拼多多视频")
        self.assertEqual(task["account_file"], "pdd.json")
        self.assertEqual(task["tags"], ["话题"])
        self.assertEqual(options["productLink"], "")

    def test_mismatched_account_type_is_rejected(self):
        """拿小红书账号去发拼多多要被挡住，报错还得说清是哪个账号。"""
        import tempfile

        import myUtils.publish_records as records
        import sau_backend_publish as BP

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "database.db"
            with patch.object(records, "DB_PATH", db_path):
                records.ensure_tables()
                with records.db() as conn:
                    conn.execute(
                        "CREATE TABLE user_info (id INTEGER PRIMARY KEY, userName TEXT, type INTEGER, filePath TEXT)"
                    )
                    conn.execute(
                        "INSERT INTO user_info (id, userName, type, filePath) VALUES (3, '小红书号', 1, 'x.json')"
                    )
                with self.assertRaises(ValueError) as ctx:
                    BP._build_tasks(
                        {
                            "items": [{"material": "demo.mp4", "title": "标题"}],
                            "targets": [{"platform": 5, "account_ids": [3]}],
                            "options": {},
                        }
                    )
        self.assertIn("不能发到拼多多视频", str(ctx.exception))


class PddFrontendWiringTests(unittest.TestCase):
    """前端也要"每个平台都被照顾到"——加第 5 个平台时最容易漏的就是前端那几张表。

    真实事故（2026-10-08）：`PublishCenter.vue` 里写死了
    `const platformAccounts = reactive({ 1: [], 2: [], 3: [], 4: [] })`。
    加拼多多（key=5）时没跟着改，结果：
      * `platformAccounts[5]` 恒为 undefined → 这个平台的下拉框**永远空着、每次都要手选**；
      * 任何"自动选中该平台账号"的代码走到 `platformAccounts[5].length` 都会抛 TypeError，
        被 try/catch 静默吞掉——表现就是"改了也没用"。
    """

    @classmethod
    def setUpClass(cls):
        cls.frontend = Path(conf.BASE_DIR) / "sau_frontend" / "src"
        cls.publish_js = (cls.frontend / "api" / "publish.js").read_text(encoding="utf-8")

    def _platform_names(self) -> list[str]:
        return re.findall(r"\{ key: \d+, name: '([^']+)' \}", self.publish_js)

    def test_platform_list_covers_pdd(self):
        names = self._platform_names()
        self.assertIn("拼多多视频", names)
        self.assertIn("快手", names)

    def test_publish_center_does_not_hardcode_platform_keys(self):
        """发布中心的"每平台账号"表必须从 PLATFORMS 派生，不能手写死平台编号。"""
        source = (self.frontend / "views" / "PublishCenter.vue").read_text(encoding="utf-8")
        # 只看代码，不看注释：修复说明里就引用了那行旧代码，别把自己抓了
        code = "\n".join(
            line for line in source.split("\n") if not line.strip().startswith(("//", "*", "/*"))
        )
        self.assertNotIn(
            "{ 1: [], 2: [], 3: [], 4: [] }",
            code,
            "有人又把手写的平台表加回来了——加第 6 个平台时照样会漏",
        )
        self.assertIn("Object.fromEntries(PLATFORMS.map", source)

    def test_every_frontend_platform_map_knows_all_platforms(self):
        """前端每一张"平台 → 值"的表都要认得所有平台（漏一个就是那个平台显示异常）。"""
        names = self._platform_names()
        candidates = [
            "stores/account.js",
            "views/AccountManagement.vue",
            "views/Dashboard.vue",
            "components/AccountStatsDialog.vue",
            "api/publish.js",
        ]
        for relative in candidates:
            path = self.frontend / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for name in names:
                self.assertIn(
                    name,
                    text,
                    f"{relative} 里没有平台「{name}」——新加平台时漏改了这张表",
                )


class PddSelectorInvariantTests(unittest.TestCase):
    """选择器清单本身的不变量——这类错误单测抓得住，而且比"跑一遍才知道"便宜得多。"""

    def test_schedule_selectors_never_match_radio_inputs(self):
        """定时控件所在的那块同时包着「立即发布 / 定时发布」两个 radio。

        写成 `[class*="publishTime"] input` 会命中 radio，随后 `fill()` 报
        `Input of type "radio" cannot be filled`——离线自测抓过一次，这里钉死。
        """
        for selector in pdd_main.PDD_SELECTORS["schedule_input"]:
            constrained = (
                '[type="text"]' in selector
                or ':not([type="radio"])' in selector
                or "placeholder" in selector
            )
            self.assertTrue(
                constrained,
                f"定时控件选择器 {selector!r} 可能命中 radio/checkbox，必须加类型约束或用 placeholder",
            )

    def test_desc_editor_targets_contenteditable_rich_text(self):
        """描述编辑器是富文本 contenteditable，不是 textarea——候选里要有 editor 类名兜底。"""
        joined = " ".join(pdd_main.PDD_SELECTORS["desc_editor"])
        self.assertIn("saboIpt", joined)

    def test_submit_button_prefers_the_card_button(self):
        """优先点卡片上那个「发布」（`video-list_singlePublish`），而不是全局按钮。"""
        self.assertIn("singlePublish", pdd_main.PDD_SELECTORS["submit_button"][0])

    def test_published_marker_is_not_the_upload_success_label(self):
        """`video-list_success` 是「**视频上传成功**」的标签（上传完就在），不能拿来判已发布。

        真实页面核对（2026-10-08）：上传完成时卡片上就出现
        `video-list_success__zjCuV`「视频上传成功」，`video-list_mask__oxuJs` 则在
        `publishing || published` 时都盖。用它们当"已发布"= 上传完就报成功。
        """
        joined = " ".join(pdd_main.PDD_SELECTORS["item_published"])
        self.assertNotIn("video-list_success", joined)
        self.assertNotIn("video-list_mask", joined)
        self.assertIn("已发布", joined)

    def test_publish_confirm_is_scoped_to_a_dialog(self):
        """页脚本身有个全局「一键发布」，确认弹窗那条必须限定在弹窗里，别点到全局按钮。"""
        for selector in pdd_main.PDD_SELECTORS["publish_confirm"]:
            lowered = selector.lower()
            # 平台自己的弹窗容器类名前缀是 `MDL_`（真机核对），所以三种都算"限定在弹窗里"
            self.assertTrue(
                any(marker in lowered for marker in ("modal", "dialog", "mdl_")),
                f"确认弹窗选择器 {selector!r} 没有限定在弹窗容器里（会点到页脚那个全局「一键发布」）",
            )
        # 首选必须是平台自己那个弹窗确认按钮的 testid（真机核对：`同意并发布`）
        self.assertEqual(
            pdd_main.PDD_SELECTORS["publish_confirm"][0],
            '[data-testid="beast-core-modal-ok-button"]',
        )

    def test_toast_scan_knows_the_platform_notice_class(self):
        """提示层扫描必须认识拼多多自己的 `TST_notice*`。

        真实事故（2026-10-08）：平台点「发布」时若缺内容声明，**只弹一句 TST_notice 警告、
        连请求都不发**；而扫描只认 toast/message/alert/tip → 一个字都读不到，
        最终报成"点了发布但等不到任何回执，结论未知"，完全指不到真正的原因。
        """
        import inspect

        source = inspect.getsource(pdd_main._visible_toast_text)
        self.assertIn("TST_notice", source, "提示层扫描丢了拼多多的 TST_notice 类名")
        self.assertIn("TST_outerWrapper", source)

    def test_declaration_option_is_scoped_to_the_dropdown(self):
        """「内容声明」的选项必须限定在下拉浮层里找。

        `ContentDeclaration_title__*` 这组类名**同时被"?"图标的说明浮层复用**，
        全局找会点到说明文字上（真实页面核对时确认过）。
        """
        first = pdd_main.PDD_SELECTORS["declaration_option"][0]
        self.assertIn("ST_dropdown", first)
        self.assertIn("{label}", first)

    def test_declaration_trigger_uses_the_stable_testid(self):
        """下拉框用平台打好的 `data-testid="beast-core-select"`，比 class 稳。"""
        self.assertEqual(
            pdd_main.PDD_SELECTORS["declaration_trigger"][0],
            '[data-testid="beast-core-select"]',
        )

    def test_publish_api_path_matches_platform_bundle(self):
        """接口路径取自平台前端常量 `B={VIDEO:"/api/backbone/video/publish"}`。"""
        self.assertEqual(pdd_main.PDD_PUBLISH_API_PATH, "/api/backbone/video/publish")


class PddCookieAuthTests(unittest.TestCase):
    def test_login_redirect_means_invalid(self):
        """被踹到 /login 就是失效——这是拼多多唯一的判据。"""
        page = MagicMock()
        page.url = "https://live.pinduoduo.com/login?isNewCreatorFrom=&referUrl=/creator/short-video"
        page.wait_for_timeout = AsyncMock()

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class _Ctx:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, *exc):
                return False

        # set_init_script 原本要 await 一个真实 context，这里让它原样返回入参——
        # 否则测试会因为"MagicMock 不能 await"而返回 False，看起来通过、其实什么都没验到。
        with patch.object(pdd_main, "async_playwright", lambda: _Ctx()), patch.object(
            pdd_main, "_goto_pdd_page", AsyncMock()
        ), patch.object(
            pdd_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)
        ), patch.object(
            pdd_main, "_upload_page_ready", AsyncMock(return_value=False)
        ) as fake_ready, patch.object(
            pdd_main, "_ensure_upload_page", AsyncMock()
        ) as fake_ensure:
            self.assertFalse(asyncio_run(pdd_main.cookie_auth("cookiesFile/x.json")))
            # 走到"被判失效"的原因必须是登录跳转：后面那两步根本不该被执行
            fake_ready.assert_not_called()
            fake_ensure.assert_not_called()

    def _cookie_auth_with_page(self, page_url: str, *, ready: bool) -> bool:
        """跑一次 cookie_auth，页面 URL 与"上传页是否就绪"可控。"""
        page = MagicMock()
        page.url = page_url
        page.wait_for_timeout = AsyncMock()

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class _Ctx:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, *exc):
                return False

        with patch.object(pdd_main, "async_playwright", lambda: _Ctx()), patch.object(
            pdd_main, "_goto_pdd_page", AsyncMock()
        ), patch.object(
            pdd_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)
        ), patch.object(
            pdd_main, "asyncio"
        ) as fake_asyncio, patch.object(
            pdd_main, "_upload_page_ready", AsyncMock(return_value=ready)
        ):
            fake_asyncio.sleep = AsyncMock()
            return asyncio_run(pdd_main.cookie_auth("cookiesFile/x.json"))

    def test_upload_page_ready_means_valid(self):
        """停在上传页 = 有效。

        真实页面（2026-10-08 核对）里上传页是 `/n-creator/video/home`，
        而 `/n-creator/video/short-video` 只是活动落地页（一个输入框都没有）。
        首次真实登录就是因为走错地址 + 要求"上传入口可见"（文件输入框其实是隐藏的），
        把一次成功的扫码判成了"添加失败"。
        """
        self.assertTrue(
            self._cookie_auth_with_page("https://live.pinduoduo.com/n-creator/video/home", ready=True)
        )

    def test_landing_page_can_still_be_recovered(self):
        """落在活动落地页时，只要能自己走进上传页，也算有效。"""
        page = MagicMock()
        page.url = "https://live.pinduoduo.com/n-creator/video/short-video"
        page.wait_for_timeout = AsyncMock()

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        class _Ctx:
            async def __aenter__(self):
                return playwright

            async def __aexit__(self, *exc):
                return False

        with patch.object(pdd_main, "async_playwright", lambda: _Ctx()), patch.object(
            pdd_main, "_goto_pdd_page", AsyncMock()
        ), patch.object(
            pdd_main, "set_init_script", AsyncMock(side_effect=lambda ctx: ctx)
        ), patch.object(pdd_main, "asyncio") as fake_asyncio, patch.object(
            pdd_main, "_upload_page_ready", AsyncMock(return_value=False)
        ), patch.object(
            pdd_main, "_ensure_upload_page", AsyncMock(return_value=None)
        ):
            fake_asyncio.sleep = AsyncMock()
            self.assertTrue(asyncio_run(pdd_main.cookie_auth("cookiesFile/x.json")))


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
