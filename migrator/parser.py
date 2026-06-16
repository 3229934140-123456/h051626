"""
迁移脚本解析模块
================

负责解析迁移脚本文件，提取 up/down SQL 语句、描述、校验和等元数据。

迁移脚本格式:
    -- migrate: description=创建用户表
    -- migrate: author=dev
    -- migrate: transaction=true

    -- +migrate Up
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- +migrate Down
    DROP TABLE users;

指令说明:
    -- migrate: key=value           元数据注释
    -- +migrate Up                  标记 up 脚本开始
    -- +migrate Down                标记 down 脚本开始
    -- +migrate StatementBegin      标记复合语句开始 (含分号的存储过程等)
    -- +migrate StatementEnd        标记复合语句结束
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .exceptions import MigrationParseError
from .version import MigrationVersion, VersionManager


@dataclass
class MigrationScript:
    """
    迁移脚本数据类

    Attributes:
        version: 版本号
        description: 描述
        file_path: 文件路径
        up_sql: up SQL 语句列表
        down_sql: down SQL 语句列表
        checksum: 脚本内容校验和 (SHA-256)
        metadata: 元数据字典
        author: 作者
        use_transaction: 是否使用事务包裹
    """

    version: MigrationVersion
    description: str
    file_path: str
    up_sql: List[str]
    down_sql: List[str]
    checksum: str
    metadata: Dict[str, str] = field(default_factory=dict)
    author: Optional[str] = None
    use_transaction: bool = True

    @property
    def filename(self) -> str:
        return os.path.basename(self.file_path)

    def __repr__(self) -> str:
        return f"MigrationScript(version='{self.version}', description='{self.description}')"


class MigrationParser:
    """
    迁移脚本解析器

    支持:
    - 元数据解析 (-- migrate: key=value)
    - Up/Down 分段识别
    - 复合语句处理 (StatementBegin/StatementEnd)
    - 校验和计算
    """

    MIGRATE_COMMENT_RE = re.compile(r"^--\s*migrate:\s*(\w+)\s*=\s*(.+?)\s*$", re.IGNORECASE)
    UP_MARKER_RE = re.compile(r"^--\s*\+migrate\s+Up\s*$", re.IGNORECASE)
    DOWN_MARKER_RE = re.compile(r"^--\s*\+migrate\s+Down\s*$", re.IGNORECASE)
    STATEMENT_BEGIN_RE = re.compile(r"^--\s*\+migrate\s+StatementBegin\s*$", re.IGNORECASE)
    STATEMENT_END_RE = re.compile(r"^--\s*\+migrate\s+StatementEnd\s*$", re.IGNORECASE)

    def __init__(self, migrations_dir: str = "migrations"):
        self.migrations_dir = migrations_dir

    def parse_file(self, file_path: str) -> MigrationScript:
        """
        解析单个迁移脚本文件

        Args:
            file_path: 脚本文件绝对路径

        Returns:
            MigrationScript 实例

        Raises:
            MigrationParseError: 解析失败
        """
        if not os.path.isfile(file_path):
            raise MigrationParseError(f"迁移脚本不存在: '{file_path}'")

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        return self.parse_content(content, file_path)

    def parse_content(self, content: str, file_path: str = "<memory>") -> MigrationScript:
        """
        解析迁移脚本内容

        Args:
            content: 脚本内容
            file_path: 文件路径 (用于错误信息和元数据)

        Returns:
            MigrationScript 实例
        """
        lines = content.splitlines()
        metadata: Dict[str, str] = {}
        up_sql: List[str] = []
        down_sql: List[str] = []

        # 状态: None=未开始, 'up'=在 up 段, 'down'=在 down 段
        current_section: Optional[str] = None
        in_statement_block = False
        current_statement_lines: List[str] = []

        for line_no, line in enumerate(lines, 1):
            stripped = line.strip()

            # 元数据注释 (仅在脚本头部，任何分段之前)
            if current_section is None:
                meta_match = self.MIGRATE_COMMENT_RE.match(stripped)
                if meta_match:
                    key = meta_match.group(1).lower()
                    value = meta_match.group(2).strip()
                    metadata[key] = value
                    continue

            # Up 分段标记
            if self.UP_MARKER_RE.match(stripped):
                if current_section is not None:
                    raise MigrationParseError(
                        f"{file_path}:{line_no}: 重复的 Up 标记"
                    )
                current_section = "up"
                continue

            # Down 分段标记
            if self.DOWN_MARKER_RE.match(stripped):
                if current_section == "down":
                    raise MigrationParseError(
                        f"{file_path}:{line_no}: 重复的 Down 标记"
                    )
                # 先保存 up 段最后一条语句
                if current_section == "up" and current_statement_lines:
                    self._flush_statement(current_statement_lines, up_sql)
                    current_statement_lines = []
                current_section = "down"
                continue

            # 复合语句开始
            if self.STATEMENT_BEGIN_RE.match(stripped):
                if current_section is None:
                    raise MigrationParseError(
                        f"{file_path}:{line_no}: StatementBegin 必须在 Up 或 Down 段内"
                    )
                in_statement_block = True
                continue

            # 复合语句结束
            if self.STATEMENT_END_RE.match(stripped):
                if not in_statement_block:
                    raise MigrationParseError(
                        f"{file_path}:{line_no}: 没有匹配的 StatementBegin"
                    )
                in_statement_block = False
                if current_section == "up":
                    self._flush_statement(current_statement_lines, up_sql)
                elif current_section == "down":
                    self._flush_statement(current_statement_lines, down_sql)
                current_statement_lines = []
                continue

            # 跳过空行和普通注释
            if not stripped or stripped.startswith("--"):
                continue

            # 内容行 - 必须在 up 或 down 段内
            if current_section is None:
                raise MigrationParseError(
                    f"{file_path}:{line_no}: SQL 语句必须在 Up 或 Down 段内"
                )

            current_statement_lines.append(line)

            # 非复合语句模式下，分号作为语句分隔符
            if not in_statement_block and ";" in stripped:
                # 找到分号位置，拆分可能的多条语句
                self._split_and_flush_statements(current_statement_lines, current_section, up_sql, down_sql)
                current_statement_lines = []

        # 处理最后一条未结束的语句
        if current_statement_lines:
            if in_statement_block:
                raise MigrationParseError(
                    f"{file_path}: 未闭合的 StatementBegin，缺少 StatementEnd"
                )
            if current_section == "up":
                self._flush_statement(current_statement_lines, up_sql)
            elif current_section == "down":
                self._flush_statement(current_statement_lines, down_sql)

        # 必须至少有 Up 段
        if current_section is None:
            raise MigrationParseError(
                f"{file_path}: 未找到 '-- +migrate Up' 标记"
            )

        # 从文件名提取版本号
        filename = os.path.basename(file_path)
        try:
            version_str = VersionManager.extract_from_filename(filename)
            version = MigrationVersion.parse(version_str)
        except Exception as e:
            raise MigrationParseError(f"{file_path}: {e}")

        # 计算校验和 (包含完整原始内容)
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return MigrationScript(
            version=version,
            description=metadata.get("description", self._default_description(filename)),
            file_path=file_path,
            up_sql=up_sql,
            down_sql=down_sql,
            checksum=checksum,
            metadata=metadata,
            author=metadata.get("author"),
            use_transaction=metadata.get("transaction", "true").lower() in ("true", "1", "yes"),
        )

    def parse_directory(self, directory: Optional[str] = None) -> List[MigrationScript]:
        """
        解析迁移目录下所有脚本

        Args:
            directory: 迁移目录，默认使用构造时的 migrations_dir

        Returns:
            按版本升序排列的 MigrationScript 列表
        """
        target_dir = directory or self.migrations_dir
        if not os.path.isdir(target_dir):
            raise MigrationParseError(f"迁移目录不存在: '{target_dir}'")

        scripts: List[MigrationScript] = []
        for filename in sorted(os.listdir(target_dir)):
            if not filename.endswith(".sql"):
                continue
            file_path = os.path.join(target_dir, filename)
            scripts.append(self.parse_file(file_path))

        return sorted(scripts, key=lambda s: s.version)

    def _flush_statement(self, lines: List[str], target: List[str]) -> None:
        """将累积的语句行合并并加入目标列表"""
        stmt = "\n".join(lines).strip()
        if stmt:
            # 去除尾部分号
            stmt = stmt.rstrip(";").strip()
            if stmt:
                target.append(stmt)

    def _split_and_flush_statements(
        self,
        lines: List[str],
        section: str,
        up_target: List[str],
        down_target: List[str],
    ) -> None:
        """
        按分号拆分语句行并刷新到目标列表。
        处理一行中有多段分号的情况。
        """
        full_text = "\n".join(lines)
        # 简单分号分割（在非复合语句块中）
        parts = full_text.split(";")
        target = up_target if section == "up" else down_target
        for part in parts:
            stripped = part.strip()
            if stripped:
                target.append(stripped)

    def _default_description(self, filename: str) -> str:
        """从文件名生成默认描述"""
        name, _ = os.path.splitext(filename)
        # 去掉版本前缀
        if "_" in name:
            name = name.split("_", 1)[1]
        # 下划线转空格
        return name.replace("_", " ").strip() or name
