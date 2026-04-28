"""
state.py — 状态持久化模块
将引擎运行状态保存到 JSON 文件，重启后自动恢复。

审计修复记录（v2）：
  [FIX-08] 原 write_text() 非原子操作，进程崩溃时可能写出损坏的 JSON
           → 改为先写临时文件再原子替换（os.replace）
  [FIX-09] 加载失败时记录更详细的错误信息，便于排障
"""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("state")


class StateManager:
    def __init__(self, filepath: str = "data/state.json"):
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, data: dict):
        """
        原子写入：先写临时文件，再用 os.replace 原子替换目标文件。
        即使进程在写入中途崩溃，也不会损坏原有状态文件。
        """
        try:
            # 在同一目录下创建临时文件（确保与目标在同一文件系统，支持原子 rename）
            dir_path = self.path.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False, indent=2))
                    f.flush()
                    os.fsync(f.fileno())  # 确保数据落盘
                os.replace(tmp_path, str(self.path))  # 原子替换
            except Exception:
                # 写入失败时清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"状态保存失败: {e}")

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            content = self.path.read_text(encoding="utf-8")
            if not content.strip():
                logger.warning("状态文件为空，返回默认值")
                return {}
            return json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(f"状态文件 JSON 解析失败（可能已损坏）: {e}，返回默认值")
            # 备份损坏的文件，便于人工排查
            backup = self.path.with_suffix(".json.bak")
            try:
                self.path.rename(backup)
                logger.info(f"已将损坏的状态文件备份至: {backup}")
            except OSError:
                pass
            return {}
        except Exception as e:
            logger.warning(f"状态加载失败: {e}")
            return {}
