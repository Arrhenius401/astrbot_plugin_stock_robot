"""跨平台文件锁、路径边界及共享日志轮转；不引入运行依赖。"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path


def safe_path(path: Path) -> bool:
    """拒绝路径各层符号链接和 Windows 重解析点，包括尚未创建的叶节点。"""
    path = path.absolute()
    try:
        for part in (*reversed(path.parents), path):
            if not os.path.lexists(part):
                continue
            info = part.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                return False
        return True
    except OSError:
        return False


class FileLock:
    """内核锁随进程退出释放；锁文件不删除，避免 inode 被替换。"""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> bool:
        if not safe_path(self.path):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.file = stream
        return True

    def close(self) -> None:
        if self.file is not None:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


_log_guard = threading.RLock()


class RotatingLog:
    """每次写入持有独占锁并关闭文件，兼容 Windows 轮转。"""

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, count: int = 5):
        self.path, self.max_bytes, self.count = path, max_bytes, count

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        pass

    def flush(self):
        pass

    def write(self, text: str) -> int:
        from astrbot.api import logger

        with _log_guard:
            lock = FileLock(self.path.with_name(self.path.name + ".lock"))
            if not lock.acquire():
                logger.warning("日志锁不可用，跳过本次落盘")
                return len(text)
            try:
                self._write(text.encode("utf-8"))
            finally:
                lock.close()
        return len(text)

    def _write(self, data: bytes) -> None:
        paths = [self.path] + [
            self.path.with_name(f"{self.path.name}.{i}") for i in range(1, self.count)
        ]
        if not all(safe_path(path) for path in paths):
            raise OSError("日志路径包含链接或重解析点")
        now = time.time()
        # 首次迁移按预算分块保存尾部，不将历史日志全部载入内存。
        for path in paths:
            if path.exists() and path.stat().st_size > self.max_bytes:
                from astrbot.api import logger

                logger.warning("历史日志超过轮转预算，仅保留尾部: %s", path.name)
                stamp = path.stat().st_mtime
                temporary = path.with_name(path.name + ".migration")
                if not safe_path(temporary):
                    raise OSError("日志迁移路径不安全")
                try:
                    with path.open("rb") as source, temporary.open("wb") as output:
                        source.seek(-self.max_bytes, os.SEEK_END)
                        # 尾部可能落在 UTF-8 字符中间，跳过开头的续字节。
                        while first := source.read(1):
                            if first[0] & 0xC0 != 0x80:
                                source.seek(-1, os.SEEK_CUR)
                                break
                        while chunk := source.read(64 * 1024):
                            output.write(chunk)
                    temporary.replace(path)
                    os.utime(path, (stamp, stamp))
                finally:
                    temporary.unlink(missing_ok=True)
        for path in paths[1:]:
            if path.exists() and now - path.stat().st_mtime >= 7 * 86400:
                path.unlink()
        for offset in range(0, len(data), self.max_bytes):
            chunk = data[offset : offset + self.max_bytes]
            if (
                self.path.exists()
                and self.path.stat().st_size + len(chunk) > self.max_bytes
            ):
                if self.count == 1:
                    self.path.unlink()
                else:
                    paths[-1].unlink(missing_ok=True)
                    for i in range(self.count - 2, -1, -1):
                        if paths[i].exists():
                            paths[i].replace(paths[i + 1])
            with self.path.open("ab") as output:
                output.write(chunk)
