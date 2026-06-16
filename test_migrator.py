"""
完整功能测试与使用示例
======================

运行: python test_migrator.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrator import (
    MigrationExecutor,
    MigrationParser,
    MigrationStorage,
    MigrationVersion,
    SchemaDiffer,
)
from migrator.diff import ColumnSchema, DatabaseSchema, TableSchema
from migrator.exceptions import MigrationValidationError
from migrator.executor import MigrationStatus
from migrator.version import VersionManager


def log(msg):
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def test_version():
    log("1. 版本管理测试")

    # 时间戳格式
    v1 = MigrationVersion.parse("20240101000000")
    v2 = MigrationVersion.parse("20240102000000")
    assert v1 < v2, "时间戳版本排序失败"
    assert str(v1) == "20240101000000"

    # 语义化版本
    v3 = MigrationVersion.parse("1.0.0")
    v4 = MigrationVersion.parse("1.2.0")
    assert v3 < v4, "语义化版本排序失败"

    # 整数版本
    v5 = MigrationVersion.parse("001")
    v6 = MigrationVersion.parse("010")
    assert v5 < v6, "整数版本排序失败"

    # 混合排序
    versions = ["010", "20240101000000", "1.0.0", "20240102000000", "1"]
    sorted_versions = VersionManager.sort(versions)
    print(f"  排序结果: {[str(v) for v in sorted_versions]}")

    # 从文件名提取版本号
    assert VersionManager.extract_from_filename("20240101000000_create_users.sql") == "20240101000000"
    assert VersionManager.extract_from_filename("1.0.0_init.sql") == "1.0.0"

    print("  [PASS] 版本管理测试通过")


def test_parser(tmpdir):
    log("2. 迁移脚本解析测试")

    parser = MigrationParser("migrations")

    # 解析示例迁移
    scripts = parser.parse_directory("migrations")
    print(f"  找到 {len(scripts)} 个迁移脚本")

    for s in scripts:
        print(f"    版本: {s.version}")
        print(f"      描述: {s.description}")
        print(f"      Up 语句数: {len(s.up_sql)}")
        print(f"      Down 语句数: {len(s.down_sql)}")
        print(f"      校验和: {s.checksum[:16]}...")

    assert len(scripts) >= 2, "至少需要2个迁移脚本"
    assert scripts[0].version < scripts[-1].version, "迁移未按版本排序"

    print("  [PASS] 脚本解析测试通过")


def test_executor(tmpdir):
    log("3. 迁移执行引擎测试")

    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)

    executor = MigrationExecutor(conn, "migrations")
    executor.initialize()

    # 查看初始状态
    status = executor.status()
    print(f"  初始状态: 已应用 {status['applied_count']}, 待应用 {status['pending_count']}")
    assert status["pending_count"] >= 2

    # 钩子
    executed = []

    def on_start(script, direction):
        print(f"  [{direction.value.upper():4}] {script.version}  {script.description}")

    def on_success(result):
        executed.append(result.version)
        print(f"        OK ({result.execution_time_ms}ms, {result.statements_executed} 语句)")

    executor.on_migration_start = on_start
    executor.on_migration_success = on_success

    # 执行第一步迁移
    results = executor.up(steps=1)
    assert len(results) == 1
    assert results[0].status == MigrationStatus.SUCCESS

    status = executor.status()
    print(f"  执行 1 步后: 已应用 {status['applied_count']}")
    assert status["applied_count"] == 1

    # 验证 users 表已创建
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    assert cur.fetchone() is not None, "users 表未创建"
    cur.close()

    # 执行所有剩余迁移
    results = executor.up()
    print(f"  执行全部剩余迁移: {len(results)} 个")
    for r in results:
        assert r.status == MigrationStatus.SUCCESS, f"{r.version} 执行失败: {r.error}"

    status = executor.status()
    print(f"  全部执行后: 已应用 {status['applied_count']}")
    assert status["pending_count"] == 0

    # 校验完整性
    errors = executor.validate()
    assert not errors, f"校验失败: {errors}"
    print("  完整性校验: 通过")

    # 测试回滚
    print("  回滚最近 1 个迁移...")
    results = executor.down(steps=1)
    assert len(results) == 1
    assert results[0].status == MigrationStatus.SUCCESS

    status = executor.status()
    print(f"  回滚后: 已应用 {status['applied_count']}")

    # 测试 redo
    print("  重做最近 1 个迁移...")
    results = executor.redo(steps=1)
    assert len(results) == 2  # down + up
    assert all(r.status == MigrationStatus.SUCCESS for r in results)

    # 完整性校验（篡改测试）
    print("  测试篡改检测...")
    applied = executor.storage.get_applied_migrations()
    if applied:
        # 手动修改数据库中的校验和模拟篡改
        cur = conn.cursor()
        cur.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            ("0" * 64, str(applied[0].version)),
        )
        conn.commit()
        cur.close()

        errors = executor.validate()
        assert len(errors) > 0, "篡改未被检测到"
        print(f"    篡改检测成功，发现 {len(errors)} 个问题")

        # 恢复
        cur = conn.cursor()
        cur.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
            (applied[0].checksum, str(applied[0].version)),
        )
        conn.commit()
        cur.close()

    conn.close()
    print("  [PASS] 执行引擎测试通过")


def test_lock(tmpdir):
    log("4. 并发锁测试")

    db_path = os.path.join(tmpdir, "test_lock.db")
    conn = sqlite3.connect(db_path)

    storage = MigrationStorage(conn)
    storage.initialize()

    # 获取锁
    print("  获取锁...")
    storage.acquire_lock(timeout=5)
    print("  已获取锁")

    # 创建第二个连接尝试获取锁（应该超时）
    conn2 = sqlite3.connect(db_path)
    storage2 = MigrationStorage(conn2)
    storage2.initialize()

    print("  第二个连接尝试获取锁（应超时）...")
    try:
        storage2.acquire_lock(timeout=1, poll_interval=0.2)
        assert False, "应该抛出锁超时异常"
    except Exception as e:
        print(f"    正确捕获超时: {type(e).__name__}")

    # 释放锁
    storage.release_lock()
    print("  第一个连接已释放锁")

    # 现在应该能获取
    storage2.acquire_lock(timeout=2)
    print("  第二个连接成功获取锁")
    storage2.release_lock()

    conn.close()
    conn2.close()
    print("  [PASS] 并发锁测试通过")


def test_diff(tmpdir):
    log("5. Schema 对比与迁移生成测试")

    db_path = os.path.join(tmpdir, "test_diff.db")
    conn = sqlite3.connect(db_path)

    # 创建源 schema（一个简单的 users 表）
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) NOT NULL UNIQUE,
            email VARCHAR(255) NOT NULL
        )
    """)
    conn.commit()
    cur.close()

    # 定义目标 schema（增加列、增加表）
    target = DatabaseSchema()
    target.tables["users"] = TableSchema(
        name="users",
        columns={
            "id": ColumnSchema(name="id", data_type="INTEGER", nullable=False, primary_key=True, auto_increment=True),
            "username": ColumnSchema(name="username", data_type="VARCHAR(50)", nullable=False),
            "email": ColumnSchema(name="email", data_type="VARCHAR(255)", nullable=False),
            "display_name": ColumnSchema(name="display_name", data_type="VARCHAR(100)", nullable=True),
        },
    )
    target.tables["profiles"] = TableSchema(
        name="profiles",
        columns={
            "id": ColumnSchema(name="id", data_type="INTEGER", nullable=False, primary_key=True, auto_increment=True),
            "user_id": ColumnSchema(name="user_id", data_type="INTEGER", nullable=False),
            "bio": ColumnSchema(name="bio", data_type="TEXT", nullable=True),
        },
    )

    differ = SchemaDiffer(conn)
    source = differ.extractor.extract()

    print("  源 schema 表:", list(source.tables.keys()))
    print("  目标 schema 表:", list(target.tables.keys()))

    diff = differ.diff(source, target)
    print(f"  差异: 新增 {len(diff.added_tables)} 表, "
          f"删除 {len(diff.dropped_tables)} 表, "
          f"修改 {len(diff.modified_tables)} 表")

    assert len(diff.added_tables) == 1
    assert diff.added_tables[0].name == "profiles"
    assert "users" in diff.modified_tables
    assert len(diff.modified_tables["users"].added_columns) == 1
    assert diff.modified_tables["users"].added_columns[0].name == "display_name"

    # 生成迁移 SQL
    up_sql, down_sql = differ.generate_migration(source, target, "add_profiles")
    print(f"\n  Up SQL ({len(up_sql)} chars):")
    for line in up_sql.strip().splitlines():
        print(f"    {line}")

    print(f"\n  Down SQL ({len(down_sql)} chars):")
    for line in down_sql.strip().splitlines():
        print(f"    {line}")

    # 生成完整文件内容
    full_content = differ.generate_migration_file_content(source, target, "add_profiles_and_display_name")
    print(f"\n  完整迁移文件内容预览 ({len(full_content)} chars):")
    for line in full_content.strip().splitlines()[:10]:
        print(f"    {line}")

    assert up_sql, "应生成 up SQL"
    assert down_sql, "应生成 down SQL"
    assert "CREATE TABLE" in up_sql
    assert "ALTER TABLE" in up_sql
    assert "DROP TABLE" in down_sql

    conn.close()
    print("  [PASS] Schema 对比测试通过")


def main():
    tmpdir = tempfile.mkdtemp(prefix="migrator_test_")
    print(f"临时目录: {tmpdir}")

    try:
        test_version()
        test_parser(tmpdir)
        test_executor(tmpdir)
        test_lock(tmpdir)
        test_diff(tmpdir)

        log("所有测试通过!")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
