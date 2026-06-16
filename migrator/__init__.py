"""
SQL 数据库迁移工具
==================

一个完整的数据库迁移框架，支持：
- 版本化的 up/down 迁移脚本管理
- 迁移状态追踪与校验
- 按序应用与回滚
- 并发锁机制
- Schema 对比与迁移生成
"""

from .version import MigrationVersion
from .parser import MigrationScript, MigrationParser
from .storage import MigrationStorage
from .executor import MigrationExecutor
from .diff import SchemaDiffer
from .templates import TEMPLATES, render_template, list_templates

__version__ = "1.0.0"
__all__ = [
    "MigrationVersion",
    "MigrationScript",
    "MigrationParser",
    "MigrationStorage",
    "MigrationExecutor",
    "SchemaDiffer",
    "TEMPLATES",
    "render_template",
    "list_templates",
]
