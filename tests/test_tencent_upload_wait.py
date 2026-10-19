# Language: 中文
"""视频号「等上传完成」的上限要**跟着素材大小走**。

由来（真机现象）：一轮 4 个平台同时发、机器负载 40 的时候，一条 **1.3MB** 的视频
在页面上"上传中"停了 13 分钟——文件早就传完了，卡住的是平台侧（「发表」按钮一直不可用）。
而这条任务**占着这个账号的通道**，后面十几条视频号任务全被它堵着，直到 30 分钟看门狗
才被掐掉。用户看到的就是"卡住了"。

原来的上限是写死的 1 小时（为"172MB + 上行 0.5Mbps"那种慢网络留的），小文件也照等。
现在按大小算：小文件几分钟就放弃并把通道让出来，大文件的上限不变。

这里钉三件事：
1. 小文件的上限明显小于大文件（1.3MB 不该等一小时）；
2. 大文件仍然拿得到足够长的时间（别把慢网络上的大文件误杀）；
3. 拿不到文件大小时退回原来的保守值，不因为读不到 stat 就提前放弃。
"""

import tempfile
import unittest
from pathlib import Path

from uploader.tencent_uploader import main as tencent_main


def _file_of(mb: float) -> Path:
    path = Path(tempfile.mkdtemp()) / "素材.mp4"
    with open(path, "wb") as fh:
        fh.truncate(int(mb * 1024 * 1024))
    return path


class UploadTimeoutBySizeTest(unittest.TestCase):
    def test_small_file_does_not_wait_an_hour(self):
        timeout = tencent_main.upload_timeout_for(_file_of(1.3))
        self.assertLessEqual(timeout, 600, f"1.3MB 等了 {timeout}s，太久了")
        self.assertGreaterEqual(timeout, 120, "再快也要给平台留出收下+处理的时间")

    def test_bigger_file_gets_more_time(self):
        small = tencent_main.upload_timeout_for(_file_of(1.3))
        big = tencent_main.upload_timeout_for(_file_of(50))
        self.assertGreater(big, small)

    def test_huge_file_keeps_the_old_ceiling(self):
        """慢网络上的大文件不能被误杀：上限仍然是原来的 1 小时。"""
        self.assertEqual(tencent_main.upload_timeout_for(_file_of(500)), 3600)

    def test_missing_file_falls_back_to_the_conservative_value(self):
        missing = Path(tempfile.mkdtemp()) / "根本没有这个文件.mp4"
        self.assertEqual(tencent_main.upload_timeout_for(missing), 3600)


if __name__ == "__main__":
    unittest.main()
