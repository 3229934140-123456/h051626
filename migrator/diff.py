"""
Schema 对比模块
===============

负责:
- 从现有数据库提取 schema 定义（表、列、索引、约束）
- 对比源 schema 与目标 schema 的差异
- 自动生成迁移脚本 (up/down SQL)

支持的数据库对象:
    - 表 (TABLE)
    - 列 (COLUMN) - 数据类型、NULL/NOT NULL、默认值
    - 主键 (PRIMARY KEY)
    - 索引 (INDEX)
    - 外键 (FOREIGN KEY)

注意: 自动生成的迁移脚本需要人工审核，特别是涉及数据丢失的操作（如删除列）。
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .exceptions import MigrationDiffError


# ---------------------------------------------------------------------------
# Schema 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ColumnSchema:
    """列定义"""

    name: str
    data_type: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    auto_increment: bool = False
    comment: Optional[str] = None

    def signature(self) -> str:
        return (
            f"{self.name}:{self.data_type}:nullable={self.nullable}"
            f":default={self.default}:pk={self.primary_key}:autoinc={self.auto_increment}"
        )


@dataclass
class IndexSchema:
    """索引定义"""

    name: str
    columns: List[str]
    unique: bool = False

    def signature(self) -> str:
        return f"{self.name}:unique={self.unique}:columns={','.join(self.columns)}"


@dataclass
class ForeignKeySchema:
    """外键定义"""

    name: str
    columns: List[str]
    ref_table: str
    ref_columns: List[str]
    on_delete: Optional[str] = None
    on_update: Optional[str] = None

    def signature(self) -> str:
        return (
            f"{self.name}:cols={','.join(self.columns)}"
            f":ref={self.ref_table}({','.join(self.ref_columns)})"
        )


@dataclass
class TableSchema:
    """表定义"""

    name: str
    columns: Dict[str, ColumnSchema] = field(default_factory=dict)
    indexes: Dict[str, IndexSchema] = field(default_factory=dict)
    foreign_keys: Dict[str, ForeignKeySchema] = field(default_factory=dict)
    comment: Optional[str] = None

    def primary_key_columns(self) -> List[str]:
        return [c.name for c in self.columns.values() if c.primary_key]


@dataclass
class DatabaseSchema:
    """整个数据库的 schema"""

    tables: Dict[str, TableSchema] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema 差异数据结构
# ---------------------------------------------------------------------------

@dataclass
class SchemaDiff:
    """两个 schema 的差异"""

    added_tables: List[TableSchema] = field(default_factory=list)
    dropped_tables: List[TableSchema] = field(default_factory=list)
    modified_tables: Dict[str, "TableDiff"] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_tables or self.dropped_tables or self.modified_tables)


@dataclass
class TableDiff:
    """单个表的差异"""

    table_name: str
    added_columns: List[ColumnSchema] = field(default_factory=list)
    dropped_columns: List[ColumnSchema] = field(default_factory=list)
    modified_columns: List[Tuple[ColumnSchema, ColumnSchema]] = field(default_factory=list)
    added_indexes: List[IndexSchema] = field(default_factory=list)
    dropped_indexes: List[IndexSchema] = field(default_factory=list)
    added_fks: List[ForeignKeySchema] = field(default_factory=list)
    dropped_fks: List[ForeignKeySchema] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return any([
            self.added_columns, self.dropped_columns, self.modified_columns,
            self.added_indexes, self.dropped_indexes,
            self.added_fks, self.dropped_fks,
        ])


# ---------------------------------------------------------------------------
# Schema 提取器
# ---------------------------------------------------------------------------

class SchemaExtractor:
    """
    从现有数据库提取 schema

    使用标准 SQL (INFORMATION_SCHEMA) 获取元数据，兼容 SQLite、PostgreSQL、MySQL。
    """

    def __init__(self, connection):
        self.conn = connection
        self.dialect = self._detect_dialect()

    def extract(self) -> DatabaseSchema:
        """提取完整 schema"""
        schema = DatabaseSchema()
        table_names = self._list_tables()

        for table_name in table_names:
            # 跳过内部迁移表
            if table_name in ("schema_migrations", "schema_migrations_lock"):
                continue
            table = self._extract_table(table_name)
            schema.tables[table_name] = table

        return schema

    def _list_tables(self) -> List[str]:
        cursor = self.conn.cursor()
        try:
            if self.dialect == "sqlite":
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            elif self.dialect == "mysql":
                cursor.execute(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"
                )
            else:  # postgresql
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                )
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _extract_table(self, table_name: str) -> TableSchema:
        table = TableSchema(name=table_name)
        table.columns = self._extract_columns(table_name)
        table.indexes = self._extract_indexes(table_name)
        table.foreign_keys = self._extract_foreign_keys(table_name)
        return table

    def _extract_columns(self, table_name: str) -> Dict[str, ColumnSchema]:
        cursor = self.conn.cursor()
        columns: Dict[str, ColumnSchema] = {}
        try:
            if self.dialect == "sqlite":
                cursor.execute(f"PRAGMA table_info('{table_name}')")
                for row in cursor.fetchall():
                    # cid, name, type, notnull, dflt_value, pk
                    col = ColumnSchema(
                        name=row[1],
                        data_type=row[2].upper(),
                        nullable=row[3] == 0,
                        default=row[4],
                        primary_key=row[5] > 0,
                        auto_increment=bool(row[5] > 0 and "INT" in row[2].upper()),
                    )
                    columns[col.name] = col

            elif self.dialect == "mysql":
                cursor.execute(
                    """
                    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT,
                           COLUMN_KEY, EXTRA, COLUMN_COMMENT
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (table_name,),
                )
                for row in cursor.fetchall():
                    col = ColumnSchema(
                        name=row[0],
                        data_type=row[1].upper(),
                        nullable=row[2] == "YES",
                        default=row[3],
                        primary_key=row[4] == "PRI",
                        auto_increment="auto_increment" in (row[5] or "").lower(),
                        comment=row[6] or None,
                    )
                    columns[col.name] = col

            else:  # postgresql
                cursor.execute(
                    """
                    SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                           CASE WHEN pk.constraint_name IS NOT NULL THEN TRUE ELSE FALSE END as is_pk,
                           col_description(c.oid, c.ordinal_position) as col_comment
                    FROM information_schema.columns c
                    LEFT JOIN (
                        SELECT kcu.column_name, tc.constraint_name
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                            ON tc.constraint_name = kcu.constraint_name
                        WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
                    ) pk ON c.column_name = pk.column_name
                    WHERE c.table_schema = 'public' AND c.table_name = %s
                    ORDER BY c.ordinal_position
                    """,
                    (table_name, table_name),
                )
                for row in cursor.fetchall():
                    col = ColumnSchema(
                        name=row[0],
                        data_type=row[1].upper(),
                        nullable=row[2] == "YES",
                        default=row[3],
                        primary_key=bool(row[4]),
                        auto_increment=bool(row[3] and "nextval" in str(row[3])),
                        comment=row[5],
                    )
                    columns[col.name] = col

            return columns
        finally:
            cursor.close()

    def _extract_indexes(self, table_name: str) -> Dict[str, IndexSchema]:
        cursor = self.conn.cursor()
        indexes: Dict[str, IndexSchema] = {}
        try:
            if self.dialect == "sqlite":
                cursor.execute(f"PRAGMA index_list('{table_name}')")
                for idx_row in cursor.fetchall():
                    # seq, name, unique, origin, partial
                    idx_name = idx_row[1]
                    is_unique = idx_row[2] == 1
                    if idx_name.startswith("sqlite_autoindex"):
                        continue
                    cols_cursor = self.conn.cursor()
                    cols_cursor.execute(f"PRAGMA index_info('{idx_name}')")
                    cols = [r[2] for r in cols_cursor.fetchall()]
                    cols_cursor.close()
                    indexes[idx_name] = IndexSchema(name=idx_name, columns=cols, unique=is_unique)

            elif self.dialect == "mysql":
                cursor.execute(
                    """
                    SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                      AND INDEX_NAME != 'PRIMARY'
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """,
                    (table_name,),
                )
                idx_cols: Dict[str, Tuple[List[str], bool]] = {}
                for row in cursor.fetchall():
                    idx_name, col_name, non_unique = row[0], row[1], row[2]
                    if idx_name not in idx_cols:
                        idx_cols[idx_name] = ([], non_unique == 0)
                    idx_cols[idx_name][0].append(col_name)
                for idx_name, (cols, unique) in idx_cols.items():
                    indexes[idx_name] = IndexSchema(name=idx_name, columns=cols, unique=unique)

            else:  # postgresql
                cursor.execute(
                    """
                    SELECT i.relname as index_name, a.attname as column_name, ix.indisunique
                    FROM pg_indexes idx
                    JOIN pg_class t ON idx.tablename = t.relname
                    JOIN pg_index ix ON t.oid = ix.indrelid
                    JOIN pg_class i ON ix.indexrelid = i.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
                    WHERE idx.tablename = %s AND idx.indexname NOT LIKE '%_pkey'
                    ORDER BY idx.indexname, array_position(ix.indkey, a.attnum)
                    """,
                    (table_name,),
                )
                idx_cols: Dict[str, Tuple[List[str], bool]] = {}
                for row in cursor.fetchall():
                    idx_name, col_name, is_unique = row[0], row[1], row[2]
                    if idx_name not in idx_cols:
                        idx_cols[idx_name] = ([], is_unique)
                    idx_cols[idx_name][0].append(col_name)
                for idx_name, (cols, unique) in idx_cols.items():
                    indexes[idx_name] = IndexSchema(name=idx_name, columns=cols, unique=unique)

            return indexes
        finally:
            cursor.close()

    def _extract_foreign_keys(self, table_name: str) -> Dict[str, ForeignKeySchema]:
        cursor = self.conn.cursor()
        fks: Dict[str, ForeignKeySchema] = {}
        try:
            if self.dialect == "sqlite":
                cursor.execute(f"PRAGMA foreign_key_list('{table_name}')")
                for row in cursor.fetchall():
                    # id, seq, table, from, to, on_update, on_delete, match
                    fk_id = f"fk_{table_name}_{row[0]}"
                    fks[fk_id] = ForeignKeySchema(
                        name=fk_id,
                        columns=[row[3]],
                        ref_table=row[2],
                        ref_columns=[row[4]],
                        on_delete=row[5] or None,
                        on_update=row[6] or None,
                    )

            elif self.dialect == "mysql":
                cursor.execute(
                    """
                    SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME,
                           k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
                           c.UPDATE_RULE, c.DELETE_RULE
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
                    JOIN INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS c
                         ON k.CONSTRAINT_NAME = c.CONSTRAINT_NAME
                    WHERE k.TABLE_SCHEMA = DATABASE()
                      AND k.TABLE_NAME = %s
                      AND k.REFERENCED_TABLE_NAME IS NOT NULL
                    ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION
                    """,
                    (table_name,),
                )
                fk_data: Dict[str, dict] = {}
                for row in cursor.fetchall():
                    name = row[0]
                    if name not in fk_data:
                        fk_data[name] = {
                            "cols": [], "ref_table": row[2], "ref_cols": [],
                            "on_update": row[4], "on_delete": row[5],
                        }
                    fk_data[name]["cols"].append(row[1])
                    fk_data[name]["ref_cols"].append(row[3])
                for name, d in fk_data.items():
                    fks[name] = ForeignKeySchema(
                        name=name,
                        columns=d["cols"],
                        ref_table=d["ref_table"],
                        ref_columns=d["ref_cols"],
                        on_update=d["on_update"],
                        on_delete=d["on_delete"],
                    )

            else:  # postgresql
                cursor.execute(
                    """
                    SELECT tc.constraint_name, kcu.column_name,
                           ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                         ON tc.constraint_name = kcu.constraint_name
                    JOIN information_schema.constraint_column_usage ccu
                         ON tc.constraint_name = ccu.constraint_name
                    WHERE tc.table_schema = 'public'
                      AND tc.table_name = %s
                      AND tc.constraint_type = 'FOREIGN KEY'
                    ORDER BY tc.constraint_name, kcu.ordinal_position
                    """,
                    (table_name,),
                )
                fk_data: Dict[str, dict] = {}
                for row in cursor.fetchall():
                    name = row[0]
                    if name not in fk_data:
                        fk_data[name] = {"cols": [], "ref_table": row[2], "ref_cols": []}
                    fk_data[name]["cols"].append(row[1])
                    fk_data[name]["ref_cols"].append(row[3])
                for name, d in fk_data.items():
                    fks[name] = ForeignKeySchema(
                        name=name,
                        columns=d["cols"],
                        ref_table=d["ref_table"],
                        ref_columns=d["ref_cols"],
                    )

            return fks
        finally:
            cursor.close()

    def _detect_dialect(self) -> str:
        conn_module = type(self.conn).__module__.lower()
        if "sqlite" in conn_module:
            return "sqlite"
        if "psycopg" in conn_module or "pg" in conn_module:
            return "postgresql"
        if "mysql" in conn_module or "pymysql" in conn_module:
            return "mysql"
        return "sqlite"


# ---------------------------------------------------------------------------
# Schema 对比器
# ---------------------------------------------------------------------------

class SchemaDiffer:
    """
    Schema 对比与迁移生成器

    对比源 schema 与目标 schema，生成 up/down SQL。
    """

    def __init__(self, connection, dialect: Optional[str] = None):
        self.conn = connection
        self.extractor = SchemaExtractor(connection)
        if dialect:
            self.dialect = dialect
        else:
            self.dialect = self.extractor.dialect

    def diff(self, source: DatabaseSchema, target: DatabaseSchema) -> SchemaDiff:
        """对比两个 schema，返回差异"""
        diff = SchemaDiff()

        all_tables = set(source.tables.keys()) | set(target.tables.keys())

        for table_name in all_tables:
            in_source = table_name in source.tables
            in_target = table_name in target.tables

            if in_target and not in_source:
                diff.added_tables.append(target.tables[table_name])
            elif in_source and not in_target:
                diff.dropped_tables.append(source.tables[table_name])
            else:
                table_diff = self._diff_table(
                    source.tables[table_name], target.tables[table_name]
                )
                if table_diff.has_changes:
                    diff.modified_tables[table_name] = table_diff

        return diff

    def _diff_table(self, source: TableSchema, target: TableSchema) -> TableDiff:
        diff = TableDiff(table_name=source.name)

        # 列对比
        all_columns = set(source.columns.keys()) | set(target.columns.keys())
        for col in all_columns:
            in_src = col in source.columns
            in_tgt = col in target.columns
            if in_tgt and not in_src:
                diff.added_columns.append(target.columns[col])
            elif in_src and not in_tgt:
                diff.dropped_columns.append(source.columns[col])
            else:
                src_sig = source.columns[col].signature()
                tgt_sig = target.columns[col].signature()
                if src_sig != tgt_sig:
                    diff.modified_columns.append((source.columns[col], target.columns[col]))

        # 索引对比
        all_indexes = set(source.indexes.keys()) | set(target.indexes.keys())
        for idx in all_indexes:
            in_src = idx in source.indexes
            in_tgt = idx in target.indexes
            if in_tgt and not in_src:
                diff.added_indexes.append(target.indexes[idx])
            elif in_src and not in_tgt:
                diff.dropped_indexes.append(source.indexes[idx])
            else:
                if source.indexes[idx].signature() != target.indexes[idx].signature():
                    diff.dropped_indexes.append(source.indexes[idx])
                    diff.added_indexes.append(target.indexes[idx])

        # 外键对比
        all_fks = set(source.foreign_keys.keys()) | set(target.foreign_keys.keys())
        for fk in all_fks:
            in_src = fk in source.foreign_keys
            in_tgt = fk in target.foreign_keys
            if in_tgt and not in_src:
                diff.added_fks.append(target.foreign_keys[fk])
            elif in_src and not in_tgt:
                diff.dropped_fks.append(source.foreign_keys[fk])
            else:
                if source.foreign_keys[fk].signature() != target.foreign_keys[fk].signature():
                    diff.dropped_fks.append(source.foreign_keys[fk])
                    diff.added_fks.append(target.foreign_keys[fk])

        return diff

    # ------------------------------------------------------------------
    # SQL 生成
    # ------------------------------------------------------------------

    def generate_migration(
        self,
        source: DatabaseSchema,
        target: DatabaseSchema,
        description: str = "auto_generated",
    ) -> Tuple[str, str]:
        """
        根据差异生成迁移脚本内容 (up_sql, down_sql)

        Returns:
            (up_sql, down_sql) - 字符串形式的 SQL
        """
        diff = self.diff(source, target)
        if not diff.has_changes:
            return "", ""

        up_lines: List[str] = []
        down_lines: List[str] = []

        # Up: 删除外键 -> 删除索引 -> 删除列 -> 删除表 -> 建表 -> 加列 -> 改列 -> 加索引 -> 加外键
        # Down: 相反顺序

        # ---- 建表 ----
        for table in diff.added_tables:
            up_lines.append(self._create_table_sql(table))
            down_lines.append(self._drop_table_sql(table))

        # ---- 删除表 (down 中恢复) ----
        for table in diff.dropped_tables:
            up_lines.append(self._drop_table_sql(table))
            down_lines.append(self._create_table_sql(table))

        # ---- 修改表 ----
        for table_name, td in diff.modified_tables.items():
            up_table_sql: List[str] = []
            down_table_sql: List[str] = []

            # 先处理删除操作
            for fk in td.dropped_fks:
                up_table_sql.append(self._drop_fk_sql(table_name, fk))
            for idx in td.dropped_indexes:
                up_table_sql.append(self._drop_index_sql(table_name, idx))
            for col in td.dropped_columns:
                up_table_sql.append(self._drop_column_sql(table_name, col))

            # 修改列
            for old_col, new_col in td.modified_columns:
                up_table_sql.append(self._alter_column_sql(table_name, old_col, new_col))
                down_table_sql.insert(0, self._alter_column_sql(table_name, new_col, old_col))

            # 添加列
            for col in td.added_columns:
                up_table_sql.append(self._add_column_sql(table_name, col))
                down_table_sql.insert(0, self._drop_column_sql(table_name, col))

            # 添加索引
            for idx in td.added_indexes:
                up_table_sql.append(self._create_index_sql(table_name, idx))
                down_table_sql.insert(0, self._drop_index_sql(table_name, idx))

            # 添加外键
            for fk in td.added_fks:
                up_table_sql.append(self._add_fk_sql(table_name, fk))
                down_table_sql.insert(0, self._drop_fk_sql(table_name, fk))

            up_lines.extend(up_table_sql)
            down_lines.extend(down_table_sql)

        up_sql = "\n\n".join(up_lines) + "\n" if up_lines else ""
        down_sql = "\n\n".join(down_lines) + "\n" if down_lines else ""

        return up_sql, down_sql

    # ------------------------------------------------------------------
    # SQL 片段生成
    # ------------------------------------------------------------------

    def _quote(self, identifier: str) -> str:
        if self.dialect == "mysql":
            return f"`{identifier}`"
        return f'"{identifier}"'

    def _column_definition(self, col: ColumnSchema) -> str:
        parts = [self._quote(col.name), col.data_type]

        if col.primary_key:
            parts.append("PRIMARY KEY")
        if col.auto_increment:
            if self.dialect == "mysql":
                parts.append("AUTO_INCREMENT")
            elif self.dialect == "sqlite":
                parts.append("AUTOINCREMENT")
        if not col.nullable and not col.primary_key:
            parts.append("NOT NULL")
        if col.default is not None:
            parts.append(f"DEFAULT {col.default}")

        return " ".join(parts)

    def _create_table_sql(self, table: TableSchema) -> str:
        cols_sql = ",\n    ".join(
            self._column_definition(c) for c in table.columns.values()
        )
        return f"CREATE TABLE {self._quote(table.name)} (\n    {cols_sql}\n);"

    def _drop_table_sql(self, table: TableSchema) -> str:
        return f"DROP TABLE {self._quote(table.name)};"

    def _add_column_sql(self, table_name: str, col: ColumnSchema) -> str:
        return (
            f"ALTER TABLE {self._quote(table_name)} "
            f"ADD COLUMN {self._column_definition(col)};"
        )

    def _drop_column_sql(self, table_name: str, col: ColumnSchema) -> str:
        return (
            f"ALTER TABLE {self._quote(table_name)} "
            f"DROP COLUMN {self._quote(col.name)};"
        )

    def _alter_column_sql(
        self, table_name: str, old: ColumnSchema, new: ColumnSchema
    ) -> str:
        if self.dialect == "sqlite":
            # SQLite 不支持 ALTER COLUMN，需要更复杂的 schema 迁移
            # 这里简化处理，提示人工介入
            return (
                f"-- SQLite 需要特殊处理列变更\n"
                f"-- 请手动处理: {table_name}.{old.name} -> {new.name} "
                f"({old.data_type} -> {new.data_type})"
            )
        elif self.dialect == "mysql":
            return (
                f"ALTER TABLE {self._quote(table_name)} "
                f"MODIFY COLUMN {self._column_definition(new)};"
            )
        else:  # postgresql
            return (
                f"ALTER TABLE {self._quote(table_name)} "
                f"ALTER COLUMN {self._quote(new.name)} TYPE {new.data_type};"
            )

    def _create_index_sql(self, table_name: str, idx: IndexSchema) -> str:
        unique = "UNIQUE " if idx.unique else ""
        cols = ", ".join(self._quote(c) for c in idx.columns)
        return (
            f"CREATE {unique}INDEX {self._quote(idx.name)} "
            f"ON {self._quote(table_name)} ({cols});"
        )

    def _drop_index_sql(self, table_name: str, idx: IndexSchema) -> str:
        if self.dialect == "mysql":
            return (
                f"ALTER TABLE {self._quote(table_name)} "
                f"DROP INDEX {self._quote(idx.name)};"
            )
        return f"DROP INDEX {self._quote(idx.name)};"

    def _add_fk_sql(self, table_name: str, fk: ForeignKeySchema) -> str:
        cols = ", ".join(self._quote(c) for c in fk.columns)
        ref_cols = ", ".join(self._quote(c) for c in fk.ref_columns)
        parts = [
            f"ALTER TABLE {self._quote(table_name)}",
            f"ADD CONSTRAINT {self._quote(fk.name)}",
            f"FOREIGN KEY ({cols})",
            f"REFERENCES {self._quote(fk.ref_table)} ({ref_cols})",
        ]
        if fk.on_delete:
            parts.append(f"ON DELETE {fk.on_delete}")
        if fk.on_update:
            parts.append(f"ON UPDATE {fk.on_update}")
        return " ".join(parts) + ";"

    def _drop_fk_sql(self, table_name: str, fk: ForeignKeySchema) -> str:
        if self.dialect == "mysql":
            return (
                f"ALTER TABLE {self._quote(table_name)} "
                f"DROP FOREIGN KEY {self._quote(fk.name)};"
            )
        return (
            f"ALTER TABLE {self._quote(table_name)} "
            f"DROP CONSTRAINT {self._quote(fk.name)};"
        )

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def generate_migration_from_current(
        self,
        target_schema: DatabaseSchema,
        description: str = "auto_generated",
    ) -> Tuple[str, str]:
        """
        以当前数据库为 source，给定 target_schema 为目标，生成迁移脚本
        """
        source = self.extractor.extract()
        return self.generate_migration(source, target_schema, description)

    def generate_migration_file_content(
        self,
        source: DatabaseSchema,
        target: DatabaseSchema,
        description: str = "auto_generated",
    ) -> str:
        """
        生成完整的迁移脚本文件内容（包含注释标记）
        """
        up_sql, down_sql = self.generate_migration(source, target, description)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        return f"""-- migrate: description={description}
-- migrate: author=auto_generate
-- migrate: transaction=true

-- +migrate Up
{up_sql}

-- +migrate Down
{down_sql}
"""
