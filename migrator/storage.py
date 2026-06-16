"""
状态存储模块
============

负责在数据库中创建和维护迁移状态表，记录已应用的迁移，提供并发锁。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .exceptions import MigrationLockError, MigrationStorageError, MigrationValidationError
from .version import MigrationVersion


@dataclass
class AppliedMigration:
    """已应用的迁移记录"""

    version: MigrationVersion
    name: str
    checksum: str
    applied_at: datetime
    execution_time_ms: int
    success: bool = True

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAILED"
        return f"AppliedMigration(version='{self.version}', status={status})"


class MigrationStorage:
    """
    迁移状态存储

    使用 Python DB-API 2.0 兼容的连接进行操作，支持 SQLite、PostgreSQL、MySQL 等。
    """

    SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
    MIGRATION_LOCK_TABLE = "schema_migrations_lock"
    LOCK_ID = 1

    def __init__(self, connection):
        """
        Args:
            connection: DB-API 2.0 兼容的数据库连接对象
        """
        self.conn = connection
        self._lock_acquired = False
        self._lock_owner: Optional[str] = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """
        初始化存储结构

        创建 schema_migrations 表和 schema_migrations_lock 表（如果不存在）。
        """
        cursor = self.conn.cursor()
        try:
            dialect = self._detect_dialect()

            # 创建迁移状态表
            self._create_migrations_table(cursor, dialect)

            # 创建锁表
            self._create_lock_table(cursor, dialect)

            # 初始化锁记录
            self._init_lock_record(cursor, dialect)

            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise MigrationStorageError(f"初始化存储失败: {e}") from e
        finally:
            cursor.close()

    def _create_migrations_table(self, cursor, dialect: str) -> None:
        """创建 schema_migrations 表"""
        q = "`" if dialect == "mysql" else '"'
        table = f"{q}{self.SCHEMA_MIGRATIONS_TABLE}{q}"

        if dialect == "mysql":
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                `version` VARCHAR(64) NOT NULL PRIMARY KEY,
                `name` VARCHAR(255) NOT NULL,
                `checksum` CHAR(64) NOT NULL,
                `applied_at` DATETIME NOT NULL,
                `execution_time` INT NOT NULL DEFAULT 0,
                `success` TINYINT(1) NOT NULL DEFAULT 1,
                INDEX `idx_success` (`success`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            cursor.execute(create_table)

        elif dialect == "postgresql":
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                version VARCHAR(64) NOT NULL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMP NOT NULL,
                execution_time INTEGER NOT NULL DEFAULT 0,
                success BOOLEAN NOT NULL DEFAULT TRUE
            )
            """
            cursor.execute(create_table)
            create_index = f"""
            CREATE INDEX IF NOT EXISTS idx_{self.SCHEMA_MIGRATIONS_TABLE}_success
                ON {table} (success)
            """
            cursor.execute(create_index)

        else:
            # SQLite
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                version TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMP NOT NULL,
                execution_time INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1 CHECK (success IN (0, 1))
            )
            """
            cursor.execute(create_table)
            create_index = f"""
            CREATE INDEX IF NOT EXISTS idx_schema_migrations_success
                ON {table} (success)
            """
            cursor.execute(create_index)

    def _create_lock_table(self, cursor, dialect: str) -> None:
        """创建锁表"""
        q = "`" if dialect == "mysql" else '"'
        table = f"{q}{self.MIGRATION_LOCK_TABLE}{q}"

        if dialect == "mysql":
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                `id` INTEGER NOT NULL PRIMARY KEY,
                `locked` TINYINT(1) NOT NULL DEFAULT 0,
                `owner` VARCHAR(64),
                `locked_at` DATETIME,
                INDEX `idx_locked` (`locked`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
            cursor.execute(create_table)

        elif dialect == "postgresql":
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER NOT NULL PRIMARY KEY,
                locked BOOLEAN NOT NULL DEFAULT FALSE,
                owner VARCHAR(64),
                locked_at TIMESTAMP
            )
            """
            cursor.execute(create_table)

        else:
            # SQLite
            create_table = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER NOT NULL PRIMARY KEY,
                locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
                owner TEXT,
                locked_at TIMESTAMP
            )
            """
            cursor.execute(create_table)

    def _init_lock_record(self, cursor, dialect: str) -> None:
        """确保锁表中有一条初始记录"""
        q = "`" if dialect == "mysql" else '"'
        table = f"{q}{self.MIGRATION_LOCK_TABLE}{q}"

        if dialect == "mysql":
            cursor.execute(
                f"INSERT IGNORE INTO {table} (id, locked) VALUES (%s, 0)",
                (self.LOCK_ID,),
            )
        elif dialect == "postgresql":
            cursor.execute(
                f"""
                INSERT INTO {table} (id, locked)
                VALUES (%s, FALSE)
                ON CONFLICT (id) DO NOTHING
                """,
                (self.LOCK_ID,),
            )
        else:
            # SQLite
            cursor.execute(
                f"""
                INSERT OR IGNORE INTO {table} (id, locked)
                VALUES (?, 0)
                """,
                (self.LOCK_ID,),
            )

    # ------------------------------------------------------------------
    # 并发锁
    # ------------------------------------------------------------------

    def acquire_lock(self, timeout: float = 30.0, poll_interval: float = 0.5) -> None:
        """
        获取迁移锁

        使用数据库行级锁 + CAS 更新确保同一时刻只有一个实例在执行迁移。

        Args:
            timeout: 获取锁的超时时间(秒)
            poll_interval: 轮询间隔(秒)

        Raises:
            MigrationLockError: 超时未获取到锁
        """
        if self._lock_acquired:
            return

        dialect = self._detect_dialect()
        owner = uuid.uuid4().hex
        self._lock_owner = owner
        quote = "`" if dialect == "mysql" else '"'

        deadline = time.time() + timeout

        while time.time() < deadline:
            cursor = self.conn.cursor()
            try:
                if dialect == "mysql":
                    cursor.execute(
                        f"""
                        UPDATE `{self.MIGRATION_LOCK_TABLE}`
                        SET locked = 1, owner = %s, locked_at = NOW()
                        WHERE id = %s AND locked = 0
                        """,
                        (owner, self.LOCK_ID),
                    )
                elif dialect == "postgresql":
                    cursor.execute(
                        f"""
                        UPDATE "{self.MIGRATION_LOCK_TABLE}"
                        SET locked = TRUE, owner = %s, locked_at = NOW()
                        WHERE id = %s AND locked = FALSE
                        """,
                        (owner, self.LOCK_ID),
                    )
                else:
                    cursor.execute(
                        f"""
                        UPDATE "{self.MIGRATION_LOCK_TABLE}"
                        SET locked = 1, owner = ?, locked_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND locked = 0
                        """,
                        (owner, self.LOCK_ID),
                    )

                self.conn.commit()

                if cursor.rowcount > 0:
                    self._lock_acquired = True
                    return

            except Exception:
                self.conn.rollback()
            finally:
                cursor.close()

            time.sleep(poll_interval)

        raise MigrationLockError(
            f"获取迁移锁超时 ({timeout}s)。可能有其他实例正在执行迁移。"
        )

    def release_lock(self) -> None:
        """释放迁移锁"""
        if not self._lock_acquired:
            return

        dialect = self._detect_dialect()
        quote = "`" if dialect == "mysql" else '"'
        cursor = self.conn.cursor()
        try:
            if dialect == "mysql":
                cursor.execute(
                    f"""
                    UPDATE `{self.MIGRATION_LOCK_TABLE}`
                    SET locked = 0, owner = NULL, locked_at = NULL
                    WHERE id = %s AND owner = %s
                    """,
                    (self.LOCK_ID, self._lock_owner),
                )
            elif dialect == "postgresql":
                cursor.execute(
                    f"""
                    UPDATE "{self.MIGRATION_LOCK_TABLE}"
                    SET locked = FALSE, owner = NULL, locked_at = NULL
                    WHERE id = %s AND owner = %s
                    """,
                    (self.LOCK_ID, self._lock_owner),
                )
            else:
                cursor.execute(
                    f"""
                    UPDATE "{self.MIGRATION_LOCK_TABLE}"
                    SET locked = 0, owner = NULL, locked_at = NULL
                    WHERE id = ? AND owner = ?
                    """,
                    (self.LOCK_ID, self._lock_owner),
                )
            self.conn.commit()
            self._lock_acquired = False
            self._lock_owner = None
        except Exception:
            self.conn.rollback()
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # 迁移记录 CRUD
    # ------------------------------------------------------------------

    def record_migration(
        self,
        version: MigrationVersion,
        name: str,
        checksum: str,
        execution_time_ms: int,
        success: bool = True,
    ) -> None:
        """
        记录一次迁移应用

        Args:
            version: 迁移版本
            name: 迁移文件名
            checksum: 脚本校验和
            execution_time_ms: 执行耗时(毫秒)
            success: 是否成功
        """
        dialect = self._detect_dialect()
        cursor = self.conn.cursor()
        try:
            if dialect == "mysql":
                cursor.execute(
                    f"""
                    REPLACE INTO `{self.SCHEMA_MIGRATIONS_TABLE}`
                    (version, name, checksum, applied_at, execution_time, success)
                    VALUES (%s, %s, %s, NOW(), %s, %s)
                    """,
                    (str(version), name, checksum, execution_time_ms, 1 if success else 0),
                )
            elif dialect == "postgresql":
                cursor.execute(
                    f"""
                    INSERT INTO "{self.SCHEMA_MIGRATIONS_TABLE}"
                    (version, name, checksum, applied_at, execution_time, success)
                    VALUES (%s, %s, %s, NOW(), %s, %s)
                    ON CONFLICT (version) DO UPDATE SET
                        name = EXCLUDED.name,
                        checksum = EXCLUDED.checksum,
                        applied_at = EXCLUDED.applied_at,
                        execution_time = EXCLUDED.execution_time,
                        success = EXCLUDED.success
                    """,
                    (str(version), name, checksum, execution_time_ms, success),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT OR REPLACE INTO "{self.SCHEMA_MIGRATIONS_TABLE}"
                    (version, name, checksum, applied_at, execution_time, success)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                    """,
                    (str(version), name, checksum, execution_time_ms, 1 if success else 0),
                )
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise MigrationStorageError(f"记录迁移状态失败: {e}") from e
        finally:
            cursor.close()

    def delete_migration(self, version: MigrationVersion) -> None:
        """删除一条迁移记录（回滚时使用）"""
        dialect = self._detect_dialect()
        quote = "`" if dialect == "mysql" else '"'
        param = "%s" if dialect in ("mysql", "postgresql") else "?"
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                f"DELETE FROM {quote}{self.SCHEMA_MIGRATIONS_TABLE}{quote} WHERE version = {param}",
                (str(version),),
            )
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            raise MigrationStorageError(f"删除迁移记录失败: {e}") from e
        finally:
            cursor.close()

    def get_applied_migrations(self) -> List[AppliedMigration]:
        """获取所有已应用的迁移记录（按版本升序）"""
        dialect = self._detect_dialect()
        quote = "`" if dialect == "mysql" else '"'
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT version, name, checksum, applied_at, execution_time, success
                FROM {quote}{self.SCHEMA_MIGRATIONS_TABLE}{quote}
                ORDER BY version ASC
                """
            )
            rows = cursor.fetchall()
            return [
                AppliedMigration(
                    version=MigrationVersion.parse(row[0]),
                    name=row[1],
                    checksum=row[2],
                    applied_at=row[3] if isinstance(row[3], datetime) else datetime.fromisoformat(str(row[3])),
                    execution_time_ms=int(row[4]),
                    success=bool(row[5]) if dialect != "mysql" else bool(row[5]),
                )
                for row in rows
            ]
        except Exception as e:
            raise MigrationStorageError(f"获取已应用迁移失败: {e}") from e
        finally:
            cursor.close()

    def get_applied_versions(self) -> List[MigrationVersion]:
        """获取所有已应用的版本号列表"""
        return [m.version for m in self.get_applied_migrations()]

    def get_failed_migrations(self) -> List[AppliedMigration]:
        """获取所有失败的迁移记录"""
        return [m for m in self.get_applied_migrations() if not m.success]

    # ------------------------------------------------------------------
    # 完整性校验
    # ------------------------------------------------------------------

    def validate_checksums(self, expected: Dict[MigrationVersion, str]) -> List[str]:
        """
        校验已应用迁移的校验和

        防止已应用的迁移脚本被篡改。

        Args:
            expected: {version: checksum} 预期校验和字典（来自脚本解析）

        Returns:
            校验失败的错误信息列表，空列表表示全部通过
        """
        errors: List[str] = []
        applied = self.get_applied_migrations()

        for record in applied:
            if record.version not in expected:
                errors.append(
                    f"版本 {record.version} 已在数据库中应用，"
                    f"但在迁移目录中找不到对应脚本"
                )
                continue
            actual = expected[record.version]
            if record.checksum != actual:
                errors.append(
                    f"版本 {record.version} 校验和不匹配！\n"
                    f"  已记录: {record.checksum}\n"
                    f"  当前文件: {actual}\n"
                    f"  (已应用的脚本可能被篡改)"
                )

        return errors

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _detect_dialect(self) -> str:
        """检测数据库方言"""
        conn_module = type(self.conn).__module__.lower()
        if "sqlite" in conn_module:
            return "sqlite"
        if "psycopg" in conn_module or "pg" in conn_module:
            return "postgresql"
        if "mysql" in conn_module or "pymysql" in conn_module or "mysqlclient" in conn_module:
            return "mysql"
        # fallback: 检查连接属性
        try:
            if hasattr(self.conn, "paramstyle"):
                if self.conn.paramstyle == "pyformat":
                    return "postgresql"
                if self.conn.paramstyle == "qmark":
                    return "sqlite"
        except Exception:
            pass
        # 默认 SQLite
        return "sqlite"
