# Copyright (c) 2026 Hao Yin. All rights reserved.

"""爬虫基类"""

import logging
from abc import ABC, abstractmethod

from storage import AudioRecord

logger = logging.getLogger(__name__)


class BaseSpider(ABC):
    name: str = "base"

    def __init__(self):
        self.logger = logging.getLogger(f"spider.{self.name}")
        self.collected: list[AudioRecord] = []

    @abstractmethod
    async def crawl(self, on_batch=None) -> list[AudioRecord]:
        """执行爬取，返回发现的音频记录列表。

        on_batch: 可选增量入库回调。实现可以忽略返回值；需要按数据库
        真实新增量控制批次时，可读取 ``(added, backfilled)`` 返回值。
        """
        ...

    def _make_record(self, url: str, title: str = "", file_format: str = "") -> AudioRecord:
        return AudioRecord(url=url, source=self.name, title=title, file_format=file_format)
