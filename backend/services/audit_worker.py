"""审计内存队列 + 后台批量写线程。

业务线程 ``submit()`` 仅入队（微秒级，不阻塞事件循环）；daemon 线程
drain 队列攒批，经 ``MysqlClient.insert_audit_many`` 批量落库。

语义：审计尽力而为，宁丢不阻塞——队列满丢弃新文档并计数；批量写
失败丢弃该批并计数；进程退出时 atexit 尽力 drain 剩余队列。
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# close 哨兵：唤醒阻塞在 get(timeout) 的后台线程（task_done 后丢弃）
_SENTINEL = object()


class AuditBatchWorker:
    """内存队列 → 后台线程批量写；``submit`` 永不抛异常、永不阻塞。"""

    def __init__(
        self,
        client: Any,
        *,
        batch_size: int = 50,
        max_queue: int = 10000,
        poll_interval: float = 0.5,
    ) -> None:
        self._client = client
        self._batch_size = max(1, batch_size)
        self._poll_interval = poll_interval
        self._q: queue.Queue[dict] = queue.Queue(maxsize=max(1, max_queue))
        self._stop = threading.Event()
        self._closed = False
        self._stat_lock = threading.Lock()
        self._dropped = 0
        self._reported_dropped = 0
        self._written = 0
        self._failed_batches = 0
        self._thread = threading.Thread(
            target=self._run, name="audit-batch-writer", daemon=True,
        )
        self._thread.start()
        atexit.register(self.close)

    # ── 业务侧接口 ────────────────────────────────────────────────
    def submit(self, doc: dict[str, Any]) -> bool:
        """审计文档入队；队列满或已 close 则丢弃并返回 False（不抛异常）。"""
        if self._closed:
            with self._stat_lock:
                self._dropped += 1
            return False
        try:
            self._q.put_nowait(doc)
            return True
        except queue.Full:
            with self._stat_lock:
                self._dropped += 1
            return False

    def flush(self, timeout: float = 10.0) -> bool:
        """阻塞至队列全部处理完（写入或丢弃）；超时返回 False。

        轮询 ``unfinished_tasks``（CPython Queue 稳定属性）：含正在
        写入的批次，返回时该批必已落库或已丢弃。
        """
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def stats(self) -> dict[str, int]:
        """运行统计（监控/测试用）：排队数 / 已写入 / 丢弃 / 失败批数。"""
        with self._stat_lock:
            return {
                "queued": self._q.qsize(),
                "written": self._written,
                "dropped": self._dropped,
                "failed_batches": self._failed_batches,
            }

    def close(self, timeout: float = 10.0) -> None:
        """停止后台线程并尽力 drain 剩余队列；幂等。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        # 唤醒可能阻塞在 get(timeout) 的线程；队列满时重试至超时（尽力而为）
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._q.put_nowait(_SENTINEL)
                break
            except queue.Full:
                time.sleep(0.01)
        self._thread.join(timeout)

    # ── 后台线程 ──────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._q.get(timeout=self._poll_interval)
            except queue.Empty:
                continue
            if first is _SENTINEL:
                self._q.task_done()
                break
            # drain-available 攒批：有货就尽量取，不等待凑满
            batch = [first]
            stop_hit = False
            while len(batch) < self._batch_size:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is _SENTINEL:
                    self._q.task_done()
                    stop_hit = True
                    break
                batch.append(item)
            self._write_batch(batch)
            if stop_hit:
                break
        # 退出前 drain 剩余（close / 进程退出路径）
        self._drain_remaining()

    def _drain_remaining(self) -> None:
        batch: list[dict] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                self._q.task_done()
                continue
            batch.append(item)
        if batch:
            self._write_batch(batch)

    def _write_batch(self, batch: list[dict]) -> None:
        try:
            n = self._client.insert_audit_many(batch)
            with self._stat_lock:
                self._written += int(n or 0)
        except Exception:  # noqa: BLE001
            with self._stat_lock:
                self._failed_batches += 1
            logger.warning(
                "[audit] 批量写失败，丢弃 %d 条审计文档", len(batch), exc_info=True,
            )
        finally:
            for _ in batch:
                self._q.task_done()
        self._report_dropped()

    def _report_dropped(self) -> None:
        """每批写完报告新增丢弃数（增量），避免 warning 刷屏。"""
        with self._stat_lock:
            delta = self._dropped - self._reported_dropped
            self._reported_dropped = self._dropped
        if delta > 0:
            logger.warning("[audit] 队列已满，新增丢弃 %d 条审计文档", delta)
