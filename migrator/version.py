"""
版本管理模块
============

负责迁移版本的解析、比较和排序。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Union

from .exceptions import MigrationVersionError


@dataclass(frozen=True)
class MigrationVersion:
    """
    迁移版本对象

    支持三种版本格式：
    1. 时间戳 (YYYYMMDDHHMMSS) - 推荐格式，天然有序
    2. 语义化版本 (MAJOR.MINOR.PATCH)
    3. 整数序号

    Examples:
        >>> v1 = MigrationVersion.parse("20240101120000")
        >>> v2 = MigrationVersion.parse("1.0.0")
        >>> v3 = MigrationVersion.parse("001")
        >>> v1 > v2
        True
    """

    raw: str
    parts: tuple
    version_type: str

    @classmethod
    def parse(cls, version_str: str) -> "MigrationVersion":
        """
        从字符串解析版本号

        Args:
            version_str: 版本字符串

        Returns:
            MigrationVersion 实例

        Raises:
            MigrationVersionError: 版本格式无效
        """
        version_str = version_str.strip()
        if not version_str:
            raise MigrationVersionError("版本号不能为空")

        # 时间戳格式: YYYYMMDDHHMMSS (14位数字)
        if re.match(r"^\d{14}$", version_str):
            return cls(
                raw=version_str,
                parts=(int(version_str),),
                version_type="timestamp",
            )

        # 语义化版本: MAJOR.MINOR.PATCH (支持 1-3 段)
        if re.match(r"^\d+(\.\d+){0,2}$", version_str):
            parts = tuple(int(p) for p in version_str.split("."))
            # 补齐到3段以便比较
            while len(parts) < 3:
                parts = parts + (0,)
            return cls(
                raw=version_str,
                parts=parts,
                version_type="semver",
            )

        # 纯整数序号
        if re.match(r"^\d+$", version_str):
            return cls(
                raw=version_str,
                parts=(int(version_str),),
                version_type="integer",
            )

        raise MigrationVersionError(
            f"不支持的版本格式: '{version_str}'。"
            "支持的格式: 时间戳(YYYYMMDDHHMMSS)、语义化版本(1.0.0)、整数序号(001)"
        )

    def __lt__(self, other: "MigrationVersion") -> bool:
        if not isinstance(other, MigrationVersion):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def __le__(self, other: "MigrationVersion") -> bool:
        if not isinstance(other, MigrationVersion):
            return NotImplemented
        return self._sort_key() <= other._sort_key()

    def __gt__(self, other: "MigrationVersion") -> bool:
        if not isinstance(other, MigrationVersion):
            return NotImplemented
        return self._sort_key() > other._sort_key()

    def __ge__(self, other: "MigrationVersion") -> bool:
        if not isinstance(other, MigrationVersion):
            return NotImplemented
        return self._sort_key() >= other._sort_key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MigrationVersion):
            return NotImplemented
        return self.raw == other.raw

    def __hash__(self) -> int:
        return hash(self.raw)

    def __str__(self) -> str:
        return self.raw

    def __repr__(self) -> str:
        return f"MigrationVersion('{self.raw}')"

    def _sort_key(self) -> tuple:
        """
        生成排序键

        排序优先级:
        1. 版本类型: timestamp > semver > integer (通过类型权重)
        2. 版本号数值
        """
        type_order = {"timestamp": 2, "semver": 1, "integer": 0}
        return (type_order[self.version_type],) + self.parts


class VersionManager:
    """
    版本管理器

    负责版本集合的排序、范围查询等操作。
    """

    @staticmethod
    def sort(versions: List[Union[str, MigrationVersion]], ascending: bool = True) -> List[MigrationVersion]:
        """
        对版本列表排序

        Args:
            versions: 版本字符串或 MigrationVersion 对象列表
            ascending: 是否升序排列

        Returns:
            排序后的 MigrationVersion 列表
        """
        parsed = []
        for v in versions:
            if isinstance(v, MigrationVersion):
                parsed.append(v)
            else:
                parsed.append(MigrationVersion.parse(v))
        return sorted(parsed, reverse=not ascending)

    @staticmethod
    def filter_range(
        versions: List[MigrationVersion],
        start: Union[str, MigrationVersion, None] = None,
        end: Union[str, MigrationVersion, None] = None,
    ) -> List[MigrationVersion]:
        """
        过滤版本范围 [start, end]

        Args:
            versions: 已排序的版本列表
            start: 起始版本 (包含)，None 表示从最开始
            end: 结束版本 (包含)，None 表示到最后

        Returns:
            过滤后的版本列表
        """
        if start is not None and not isinstance(start, MigrationVersion):
            start = MigrationVersion.parse(start)
        if end is not None and not isinstance(end, MigrationVersion):
            end = MigrationVersion.parse(end)

        result = []
        for v in versions:
            if start is not None and v < start:
                continue
            if end is not None and v > end:
                continue
            result.append(v)
        return result

    @staticmethod
    def extract_from_filename(filename: str) -> str:
        """
        从迁移脚本文件名中提取版本号

        文件名格式: {version}_{description}.sql

        Args:
            filename: 文件名，如 "20240101120000_create_users.sql"

        Returns:
            版本号字符串

        Raises:
            MigrationVersionError: 文件名格式不正确
        """
        import os

        basename = os.path.basename(filename)
        name, _ = os.path.splitext(basename)

        # 匹配第一个下划线之前的部分作为版本号
        match = re.match(r"^([^_]+)_", name)
        if match:
            return match.group(1)

        # 如果没有下划线，整个名字作为版本号
        if name:
            return name

        raise MigrationVersionError(f"无法从文件名提取版本号: '{filename}'")
