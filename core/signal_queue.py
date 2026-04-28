"""
signal_queue.py — 信号消息队列模块

使用 Redis Pub/Sub 解耦信号抓取与执行引擎，支持：
  - 多账户订阅同一信号频道
  - 信号持久化（Redis Stream）
  - 降级方案：Redis 不可用时自动降级为内存队列（threading.Queue）

架构：
  Publisher（信号抓取线程）→ Redis Stream / 内存队列
  Consumer（执行引擎线程）← Redis Stream / 内存队列
"""
import json
import logging
import queue
import threading
import time
from typing import Optional, Callable

from core.config import Config

logger = logging.getLogger("signal_queue")

_STREAM_KEY = "copytrader:signals"
_CONSUMER_GROUP = "engines"


class _InMemoryQueue:
    """内存队列（Redis 不可用时的降级方案）"""

    def __init__(self, maxsize: int = 1000):
        self._q = queue.Queue(maxsize=maxsize)

    def publish(self, signal: dict):
        try:
            self._q.put_nowait(signal)
        except queue.Full:
            logger.warning("内存队列已满，丢弃最旧信号")
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(signal)

    def consume(self, timeout: float = 1.0) -> Optional[dict]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._q.qsize()


class SignalQueue:
    """
    信号队列门面类
    优先使用 Redis Stream，不可用时自动降级为内存队列。
    """

    def __init__(self):
        self._redis = None
        self._fallback = _InMemoryQueue()
        self._use_redis = False
        self._init_redis()

    def _init_redis(self):
        """尝试初始化 Redis 连接"""
        if not Config.REDIS_ENABLED:
            logger.info("Redis 未启用，使用内存队列")
            return
        try:
            import redis
            r = redis.Redis(
                host=Config.REDIS_HOST,
                port=Config.REDIS_PORT,
                db=Config.REDIS_DB,
                password=Config.REDIS_PASSWORD or None,
                socket_connect_timeout=3,
                decode_responses=True,
            )
            r.ping()
            self._redis = r
            self._use_redis = True
            # 创建消费者组（若不存在）
            try:
                self._redis.xgroup_create(
                    _STREAM_KEY, _CONSUMER_GROUP, id="0", mkstream=True
                )
            except Exception:
                pass  # 组已存在
            logger.info(
                f"Redis 连接成功: {Config.REDIS_HOST}:{Config.REDIS_PORT}，"
                f"使用 Redis Stream"
            )
        except Exception as e:
            logger.warning(f"Redis 连接失败: {e}，降级为内存队列")
            self._use_redis = False

    def _try_reconnect_redis(self):
        """[FIX-G01] 尝试重新连接 Redis（降级后每 60 秒尝试恢复）"""
        if not Config.REDIS_ENABLED:
            return
        now = time.time()
        if not hasattr(self, '_last_reconnect_ts'):
            self._last_reconnect_ts = 0.0
        if now - self._last_reconnect_ts < 60:
            return
        self._last_reconnect_ts = now
        try:
            import redis
            r = redis.Redis(
                host=Config.REDIS_HOST, port=Config.REDIS_PORT,
                db=Config.REDIS_DB, password=Config.REDIS_PASSWORD or None,
                socket_connect_timeout=3, decode_responses=True,
            )
            r.ping()
            self._redis = r
            self._use_redis = True
            logger.info("Redis 重连成功，恢复使用 Redis Stream")
        except Exception:
            pass  # 静默失败，继续使用内存队列

    def publish(self, signal: dict):
        """发布信号到队列"""
        if not self._use_redis:
            self._try_reconnect_redis()  # [FIX-G01] 尝试恢复 Redis
        if self._use_redis:
            try:
                self._redis.xadd(
                    _STREAM_KEY,
                    {"data": json.dumps(signal, ensure_ascii=False)},
                    maxlen=10000,
                )
                return
            except Exception as e:
                logger.warning(f"Redis 发布失败: {e}，降级为内存队列")
                self._use_redis = False
        self._fallback.publish(signal)

    def consume(self, consumer_id: str = "engine-0",
                timeout_ms: int = 1000) -> Optional[dict]:
        """消费一条信号"""
        if self._use_redis:
            try:
                results = self._redis.xreadgroup(
                    _CONSUMER_GROUP, consumer_id,
                    {_STREAM_KEY: ">"},
                    count=1, block=timeout_ms
                )
                # [FIX-G02] xreadgroup 超时返回 None 或空列表，需同时处理两种情况
                if results and len(results) > 0:
                    stream_name, messages = results[0]
                    if messages and len(messages) > 0:
                        msg_id, fields = messages[0]
                        raw = fields.get("data")
                        if raw is None:
                            logger.warning(f"Redis 消息缺少 data 字段: {fields}")
                            self._redis.xack(_STREAM_KEY, _CONSUMER_GROUP, msg_id)
                            return None
                        signal = json.loads(raw)
                        self._redis.xack(_STREAM_KEY, _CONSUMER_GROUP, msg_id)
                        return signal
                return None
            except Exception as e:
                logger.warning(f"Redis 消费失败: {e}，降级为内存队列")
                self._use_redis = False
        return self._fallback.consume(timeout=timeout_ms / 1000)

    def publish_batch(self, signals: list):
        """
        批量发布信号
        [FIX-G03] 单条发布异常不影响后续信号，保证不丢失
        """
        for s in signals:
            try:
                self.publish(s)
            except Exception as e:
                logger.error(f"publish_batch 单条信号发布失败: {e}，信号={s}")

    def qsize(self) -> int:
        """获取队列长度（近似值）"""
        if self._use_redis:
            try:
                return self._redis.xlen(_STREAM_KEY)
            except Exception:
                pass
        return self._fallback.qsize()

    @property
    def backend(self) -> str:
        return "redis" if self._use_redis else "memory"
