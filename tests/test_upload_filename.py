# -*- coding: utf-8 -*-
"""素材上传的文件名清洗（`sau_backend.py::_safe_upload_name`）。

由来（2026-10-09 用户反馈）
--------------------------
「上传素材的时候文件名怎么只剩下前面的数字了，后面的中文没了」。

原因是用了 werkzeug 的 `secure_filename`——它只保留 ASCII。要命的是它**不是全丢**：
`12345城市海岸线的日落.mp4` 会被清成 `12345.mp4`，主名和扩展名都还在，于是旧代码里那句
"清洗结果合法就直接用"的判断把它放行了，中文被**静默**删掉。
纯中文名反而没事：那种会被清成 `mp4`（连点都没了），恰好触发旧的兜底分支，
退化成 `video_<时间戳>.mp4`。所以症状只在"ASCII 开头 + 中文"的名字上出现，很难猜。

这里钉住两件事：

1. **中文必须留下**（这次反馈的正面要求）；
2. 换掉 `secure_filename` 之后，它原本挡住的那些东西（路径穿越、`..`、开头的 `-`）
   一个都不能漏——清洗是换了实现，不是放松了要求。
"""

import inspect
import re
import unittest

import sau_backend
from sau_backend import _UPLOAD_NAME_MAX, _safe_upload_name


class SafeUploadNameTest(unittest.TestCase):
    def test_leading_digits_do_not_eat_the_chinese(self):
        """回归：数字开头的名字，中文必须留着（旧实现会静默删掉）。"""
        self.assertEqual(
            _safe_upload_name("12345城市海岸线的日落.mp4"), "12345城市海岸线的日落.mp4"
        )
        self.assertEqual(
            _safe_upload_name("20261009_城市海岸线的日落.mp4"), "20261009_城市海岸线的日落.mp4"
        )
        self.assertEqual(_safe_upload_name("我的视频 1.mp4"), "我的视频1.mp4")

    def test_pure_chinese_name_is_kept_instead_of_degraded(self):
        """纯中文名以前会被整体清空、退化成 video_<时间戳>，现在应当原样保留。"""
        self.assertEqual(_safe_upload_name("城市海岸线的日落.mp4"), "城市海岸线的日落.mp4")

    def test_spaces_and_punctuation_drop_out_but_the_name_stays_readable(self):
        self.assertEqual(_safe_upload_name("日落 #洞头 航拍.mp4"), "日落洞头航拍.mp4")
        # 连续分隔符收敛成一个，首尾不留
        self.assertEqual(_safe_upload_name("我的视频 - - 01.mp4"), "我的视频_01.mp4")

    def test_ascii_names_are_left_alone(self):
        """英文/数字名是常态（相机、剪辑软件导出的名字），一个字符都不该动。"""
        for name in ("VID_20240101_123456.mp4", "DJI_0001.MOV", "clip-final-2.webm"):
            self.assertEqual(_safe_upload_name(name), name)

    def test_path_traversal_is_neutralised(self):
        """清洗换实现，但安全要求不变：拼不出 `..`、也跑不到别的目录。"""
        for name in ("../../etc/passwd.mp4", "..\\..\\windows\\system32\\x.mp4", "/etc/passwd.mp4"):
            out = _safe_upload_name(name)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)
            self.assertNotIn("..", out)
            self.assertEqual(out, "passwd.mp4" if "passwd" in name else out)

    def test_windows_full_path_is_reduced_to_basename(self):
        """浏览器在 Windows 上会送来 `C:\\fakepath\\xx.mp4` 这种整条路径。"""
        self.assertEqual(
            _safe_upload_name("C:\\fakepath\\高空餐厅俯瞰城市天际线.mp4"),
            "高空餐厅俯瞰城市天际线.mp4",
        )

    def test_leading_dash_is_stripped(self):
        """开头的 `-` 必须去掉：这种文件名传给 ffmpeg 之类的命令会被当成选项。"""
        out = _safe_upload_name("-rf 危险.mp4")
        self.assertFalse(out.startswith("-"))
        self.assertEqual(out, "rf危险.mp4")

    def test_emoji_only_name_falls_back_to_a_timestamp(self):
        """洗不出主名才退化成时间戳名（扩展名要保住，否则前端认不出是视频）。"""
        out = _safe_upload_name("🎬🎬🎬.mp4")
        self.assertRegex(out, r"^video_\d{8}_\d{6}\.mp4$")

    def test_extensionless_upload_gets_mp4(self):
        self.assertEqual(_safe_upload_name("no-extension"), "no-extension.mp4")
        # 不像扩展名的后缀（太长/带奇怪字符）也按 .mp4 处理
        self.assertEqual(_safe_upload_name("素材.这是一个很长的后缀"), "素材.mp4")

    def test_long_chinese_name_stays_within_the_filesystem_limit(self):
        """中文一个字 3 字节，Linux/APFS 的文件名上限是 255 **字节**（还要拼 uuid）。

        不收一收的话，长标题会在 `file.save()` 那一刻抛 ENAMETOOLONG，
        表现成"上传失败"，而错误原因看着跟文件名毫无关系。
        """
        out = _safe_upload_name("城市海岸线的日落" * 12 + ".mp4")
        self.assertLessEqual(len(out), _UPLOAD_NAME_MAX + len(".mp4"))
        # 磁盘名是 `<36 字符 uuid>_<清洗后的名字>`
        self.assertLess(len(out.encode("utf-8")) + 37, 255)

    def test_both_upload_routes_share_this_one_cleaner(self):
        """两个入口（素材管理的 `/uploadSave`、工作台的 `/upload`）口径必须一致。

        各写一份的话，迟早出现"同一个文件从不同入口上传、名字不一样"。
        """
        for view in (sau_backend.upload_file, sau_backend.upload_save):
            self.assertIn("_safe_upload_name", inspect.getsource(view))
        # 别再退回 werkzeug 那个只保留 ASCII 的清洗函数
        self.assertNotIn("secure_filename(", inspect.getsource(_safe_upload_name))


if __name__ == "__main__":
    unittest.main()
