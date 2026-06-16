"""
迁移脚本模板
============

为 create 命令提供预置模板，生成可编辑的 up/down 骨架脚本。

支持模板:
    blank          空白迁移
    create-table   建表（含主键、时间戳、索引）
    add-column     加列
    create-index   建索引
    drop-table     删表
    drop-column    删列
    add-fk         加外键
"""

from __future__ import annotations

from typing import Dict


TEMPLATES: Dict[str, Dict[str, str]] = {
    "blank": {
        "help": "空白迁移，只有骨架",
        "up": "",
        "down": "",
    },
    "create-table": {
        "help": "建表（含主键、时间戳）",
        "up": (
            "CREATE TABLE {{table_name}} (\n"
            "    id SERIAL PRIMARY KEY,\n"
            "    name VARCHAR(255) NOT NULL,\n"
            "    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n"
            "    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP\n"
            ");\n"
            "\n"
            "CREATE INDEX idx_{{table_name}}_name ON {{table_name}} (name);"
        ),
        "down": (
            "DROP INDEX IF EXISTS idx_{{table_name}}_name;\n"
            "DROP TABLE IF EXISTS {{table_name}};"
        ),
    },
    "add-column": {
        "help": "给已有表加列",
        "up": (
            "ALTER TABLE {{table_name}} ADD COLUMN {{column_name}} {{column_type}};"
        ),
        "down": (
            "-- SQLite 不支持 DROP COLUMN (3.35.0 之前)\n"
            "-- PostgreSQL / MySQL:\n"
            "ALTER TABLE {{table_name}} DROP COLUMN {{column_name}};"
        ),
    },
    "create-index": {
        "help": "建索引",
        "up": (
            "CREATE {{unique}}INDEX {{index_name}} ON {{table_name}} ({{column_list}});"
        ),
        "down": (
            "DROP INDEX IF EXISTS {{index_name}};"
        ),
    },
    "drop-table": {
        "help": "删表",
        "up": (
            "DROP TABLE IF EXISTS {{table_name}};"
        ),
        "down": (
            "-- 请手动补充建表语句\n"
            "-- CREATE TABLE {{table_name}} (\n"
            "--     ...\n"
            "-- );"
        ),
    },
    "drop-column": {
        "help": "删列",
        "up": (
            "-- SQLite 不支持 DROP COLUMN (3.35.0 之前)\n"
            "-- PostgreSQL / MySQL:\n"
            "ALTER TABLE {{table_name}} DROP COLUMN {{column_name}};"
        ),
        "down": (
            "ALTER TABLE {{table_name}} ADD COLUMN {{column_name}} {{column_type}};"
        ),
    },
    "add-fk": {
        "help": "加外键",
        "up": (
            "ALTER TABLE {{table_name}}\n"
            "ADD CONSTRAINT {{fk_name}}\n"
            "FOREIGN KEY ({{column_name}})\n"
            "REFERENCES {{ref_table}} ({{ref_column}});"
        ),
        "down": (
            "-- PostgreSQL:\n"
            "ALTER TABLE {{table_name}} DROP CONSTRAINT {{fk_name}};\n"
            "-- MySQL:\n"
            "-- ALTER TABLE {{table_name}} DROP FOREIGN KEY {{fk_name}};\n"
            "-- SQLite: 不支持 DROP CONSTRAINT，需重建表"
        ),
    },
}


def list_templates() -> str:
    """列出所有可用模板"""
    lines = ["可用模板:", ""]
    for name, tmpl in TEMPLATES.items():
        lines.append(f"  {name:<16} {tmpl['help']}")
    lines.append("")
    lines.append("用法: python -m migrator create <描述> --template create-table --table users")
    return "\n".join(lines)


def render_template(
    template_name: str,
    description: str,
    **kwargs,
) -> str:
    """
    根据模板名和参数渲染迁移脚本内容

    Args:
        template_name: 模板名称
        description: 迁移描述
        **kwargs: 模板变量 (table_name, column_name, etc.)

    Returns:
        完整的迁移脚本内容字符串
    """
    if template_name not in TEMPLATES:
        raise ValueError(
            f"未知模板: '{template_name}'。可用: {', '.join(TEMPLATES.keys())}"
        )

    tmpl = TEMPLATES[template_name]

    up_sql = tmpl["up"]
    down_sql = tmpl["down"]

    for key, value in kwargs.items():
        placeholder = "{{" + key + "}}"
        up_sql = up_sql.replace(placeholder, str(value))
        down_sql = down_sql.replace(placeholder, str(value))

    # 清理未替换的占位符
    import re
    up_sql = re.sub(r"\{\{(\w+)\}\}", r"<\1>", up_sql)
    down_sql = re.sub(r"\{\{(\w+)\}\}", r"<\1>", down_sql)

    # unique 索引特殊处理: 如果不是 unique 则清空前缀
    if kwargs.get("unique") is None or kwargs.get("unique") == "":
        up_sql = up_sql.replace("<unique>", "")

    return (
        f"-- migrate: description={description}\n"
        f"-- migrate: author=dev\n"
        f"-- migrate: transaction=true\n"
        f"\n"
        f"-- +migrate Up\n"
        f"{up_sql}\n"
        f"\n"
        f"-- +migrate Down\n"
        f"{down_sql}\n"
    )
