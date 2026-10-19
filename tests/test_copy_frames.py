"""素材库里的**静态图**也要能生成文案（`ai_copywriter.extract_key_frames`）。

由来：素材管理能上传图片，「生成文案」按钮对图片一样是亮的，批量生成文案更是会把
选中的图片一起排队（图片不进跳过逻辑，只有换音轨才要求是视频）。但抽首帧用的是

    ffmpeg -ss 0 -i <素材> -frames:v 1 …

——`-ss` 放在 `-i` **之前**表示"先定位再解码"，而静态图只有一帧，定位完就没有帧
可编了，ffmpeg 报 `Output file is empty, nothing was encoded`。于是图片素材生成文案
必然失败：「抽取首帧失败: 」，而 `generate_note_copy` 拿不到首帧就直接返回错误。

这里钉住两条：

1. **图片能抽到首帧**（走的是"去掉 `-ss` 再试一次"的兜底）；
2. **视频的行为没变**（首尾两帧都要有）——兜底只在第一次已经失败时才触发，
   本来能跑的视频走的还是原来那条命令。

真实文件由 ffmpeg 现造（`imageio-ffmpeg` 自带二进制），不依赖仓库里的任何素材。
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from uploader.xiaohongshu_uploader import ai_copywriter as CW


def _ffmpeg():
    return CW.find_ffmpeg()


@unittest.skipUnless(_ffmpeg(), "需要 ffmpeg（requirements.txt 里的 imageio-ffmpeg 自带）")
class ExtractKeyFramesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = _ffmpeg()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sau_frames_test_")
        self.work = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *args) -> None:
        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            self.fail(f"造测试素材失败: {proc.stderr.strip()}")

    def _make_image(self, name="素材.jpg") -> Path:
        path = self.work / name
        self._run("-f", "lavfi", "-i", "color=c=orange:s=64x64", "-frames:v", "1", str(path))
        return path

    def _make_video(self, name="素材.mp4", seconds=2) -> Path:
        path = self.work / name
        # 用 testsrc 而不是纯色：纯色视频的首帧和尾帧一模一样，会被"尾帧与首帧相同"那条
        # 优化合并成一张，尾帧就验不出来了（那是设计如此，不是缺陷）。
        self._run(
            "-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        )
        return path

    def test_still_image_yields_first_frame(self):
        """图片：首帧必须抽得出来——这正是之前必然失败的那条。"""
        frames = CW.extract_key_frames(self._make_image())

        self.assertTrue(frames["first"], f"图片没抽到首帧: {frames['errors']}")
        self.assertTrue(Path(frames["first"]).is_file())
        self.assertGreater(Path(frames["first"]).stat().st_size, 0)
        # 图片的"尾帧"和首帧是同一张，重复发一遍只是白花 token
        self.assertFalse(frames["last"])
        self.assertIn("尾帧与首帧相同", "；".join(frames["errors"]))

    def test_image_extension_does_not_matter(self):
        """png 一样走这条路（判据是"输入只有一帧"，不是扩展名）。"""
        frames = CW.extract_key_frames(self._make_image("素材.png"))
        self.assertTrue(frames["first"], f"png 没抽到首帧: {frames['errors']}")

    def test_video_still_yields_both_frames(self):
        """视频：兜底不能改变原有行为，首尾两帧都要在。"""
        frames = CW.extract_key_frames(self._make_video())

        self.assertTrue(frames["first"], f"视频丢了首帧: {frames['errors']}")
        self.assertTrue(frames["last"], f"视频丢了尾帧: {frames['errors']}")

    def test_missing_file_still_reports_error(self):
        """文件不存在时照旧报错，不能因为加了兜底就"看起来成功"。"""
        frames = CW.extract_key_frames(self.work / "根本没有这个文件.mp4")
        self.assertFalse(frames["first"])
        self.assertTrue(frames["errors"])


if __name__ == "__main__":
    unittest.main()
