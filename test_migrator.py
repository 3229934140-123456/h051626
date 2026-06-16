"""
完整功能测试与使用示例
======================

运行: python test_migrator.py
"""

from __future__ import annotations

import json
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
from migrator.cli import MigratorCLI
from migrator.diff import ColumnSchema, DatabaseSchema, TableSchema
from migrator.exceptions import MigrationValidationError
from migrator.executor import MigrationStatus
from migrator.templates import TEMPLATES, render_template, list_templates
from migrator.version import VersionManager


def log(msg):
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def test_version():
    log("1. 版本管理测试")

    v1 = MigrationVersion.parse("20240101000000")
    v2 = MigrationVersion.parse("20240102000000")
    assert v1 < v2
    assert str(v1) == "20240101000000"

    v3 = MigrationVersion.parse("1.0.0")
    v4 = MigrationVersion.parse("1.2.0")
    assert v3 < v4

    v5 = MigrationVersion.parse("001")
    v6 = MigrationVersion.parse("010")
    assert v5 < v6

    versions = ["010", "20240101000000", "1.0.0", "20240102000000", "1"]
    sorted_versions = VersionManager.sort(versions)
    print(f"  排序结果: {[str(v) for v in sorted_versions]}")

    assert VersionManager.extract_from_filename("20240101000000_create_users.sql") == "20240101000000"
    assert VersionManager.extract_from_filename("1.0.0_init.sql") == "1.0.0"

    print("  [PASS] 版本管理测试通过")


def test_parser(tmpdir):
    log("2. 迁移脚本解析测试")

    parser = MigrationParser("migrations")
    scripts = parser.parse_directory("migrations")
    print(f"  找到 {len(scripts)} 个迁移脚本")

    for s in scripts:
        print(f"    版本: {s.version}  描述: {s.description}  Up: {len(s.up_sql)}  Down: {len(s.down_sql)}")

    assert len(scripts) >= 2
    assert scripts[0].version < scripts[-1].version

    print("  [PASS] 脚本解析测试通过")


def test_executor(tmpdir):
    log("3. 迁移执行引擎测试")

    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)

    executor = MigrationExecutor(conn, "migrations")
    executor.initialize()

    status = executor.status()
    print(f"  初始状态: 已应用 {status['applied_count']}, 待应用 {status['pending_count']}")
    assert status["pending_count"] >= 2

    executed = []

    def on_start(script, direction):
        print(f"  [{direction.value.upper():4}] {script.version}  {script.description}")

    def on_success(result):
        executed.append(result.version)
        print(f"        OK ({result.execution_time_ms}ms, {result.statements_executed} 语句)")

    executor.on_migration_start = on_start
    executor.on_migration_success = on_success

    results = executor.up(steps=1)
    assert len(results) == 1
    assert results[0].status == MigrationStatus.SUCCESS

    status = executor.status()
    assert status["applied_count"] == 1

    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    assert cur.fetchone() is not None
    cur.close()

    results = executor.up()
    for r in results:
        assert r.status == MigrationStatus.SUCCESS, f"{r.version} 失败: {r.error}"

    status = executor.status()
    assert status["pending_count"] == 0

    errors = executor.validate()
    assert not errors

    results = executor.down(steps=1)
    assert len(results) == 1
    assert results[0].status == MigrationStatus.SUCCESS

    results = executor.redo(steps=1)
    assert len(results) == 2
    assert all(r.status == MigrationStatus.SUCCESS for r in results)

    # 篡改检测
    applied = executor.storage.get_applied_migrations()
    if applied:
        cur = conn.cursor()
        cur.execute("UPDATE schema_migrations SET checksum = ? WHERE version = ?", ("0" * 64, str(applied[0].version)))
        conn.commit()
        cur.close()
        errors = executor.validate()
        assert len(errors) > 0
        cur = conn.cursor()
        cur.execute("UPDATE schema_migrations SET checksum = ? WHERE version = ?", (applied[0].checksum, str(applied[0].version)))
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

    storage.acquire_lock(timeout=5)

    conn2 = sqlite3.connect(db_path)
    storage2 = MigrationStorage(conn2)
    storage2.initialize()

    try:
        storage2.acquire_lock(timeout=1, poll_interval=0.2)
        assert False, "应该超时"
    except Exception as e:
        print(f"    正确捕获超时: {type(e).__name__}")

    storage.release_lock()
    storage2.acquire_lock(timeout=2)
    storage2.release_lock()

    conn.close()
    conn2.close()
    print("  [PASS] 并发锁测试通过")


def test_diff(tmpdir):
    log("5. Schema 对比与迁移生成测试")

    db_path = os.path.join(tmpdir, "test_diff.db")
    conn = sqlite3.connect(db_path)

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

    diff = differ.diff(source, target)
    assert len(diff.added_tables) == 1
    assert "users" in diff.modified_tables
    assert len(diff.modified_tables["users"].added_columns) == 1

    up_sql, down_sql = differ.generate_migration(source, target, "add_profiles")
    assert up_sql
    assert down_sql
    assert "CREATE TABLE" in up_sql

    conn.close()
    print("  [PASS] Schema 对比测试通过")


def test_templates():
    log("6. 迁移脚本模板测试")

    # 列出模板
    listing = list_templates()
    assert "create-table" in listing
    assert "add-column" in listing
    print(f"  可用模板: {list(TEMPLATES.keys())}")

    # blank
    content = render_template("blank", "test blank")
    assert "-- +migrate Up" in content
    assert "-- +migrate Down" in content
    print("  blank 模板: OK")

    # create-table
    content = render_template("create-table", "create orders", table_name="orders")
    assert "CREATE TABLE orders" in content
    assert "DROP TABLE IF EXISTS orders" in content
    print("  create-table 模板: OK")

    # add-column
    content = render_template("add-column", "add bio", table_name="users", column_name="bio", column_type="TEXT")
    assert 'ALTER TABLE users ADD COLUMN bio TEXT' in content
    assert 'DROP COLUMN bio' in content
    print("  add-column 模板: OK")

    # create-index (unique)
    content = render_template("create-index", "add email idx", index_name="idx_users_email", table_name="users", column_list="email", unique="UNIQUE ")
    assert "UNIQUE INDEX idx_users_email" in content
    assert "DROP INDEX IF EXISTS idx_users_email" in content
    print("  create-index (unique) 模板: OK")

    # create-index (non-unique)
    content = render_template("create-index", "add name idx", index_name="idx_users_name", table_name="users", column_list="name", unique="")
    assert "INDEX idx_users_name" in content
    assert "UNIQUE" not in content.split("INDEX idx_users_name")[0]
    print("  create-index (non-unique) 模板: OK")

    # add-fk
    content = render_template("add-fk", "add user fk", table_name="posts", fk_name="fk_posts_user", column_name="user_id", ref_table="users", ref_column="id")
    assert "FOREIGN KEY (user_id)" in content
    assert "REFERENCES users (id)" in content
    print("  add-fk 模板: OK")

    # 未知模板
    try:
        render_template("nonexistent", "test")
        assert False, "应抛 ValueError"
    except ValueError:
        print("  未知模板正确抛 ValueError")

    print("  [PASS] 模板测试通过")


def test_cli_create_templates(tmpdir):
    log("7. CLI create 模板测试")

    cli = MigratorCLI()
    cli.migrations_dir = os.path.join(tmpdir, "migrations")
    cli.db_url = f"sqlite:///{os.path.join(tmpdir, 'test.db')}"

    # --list-templates
    rc = cli.run(["create", "dummy", "--list-templates"])
    assert rc == 0
    print("  --list-templates: OK")

    # 建表模板
    rc = cli.run(["create", "create_orders", "-t", "create-table", "--table", "orders", "--version", "0001"])
    assert rc == 0
    fpath = os.path.join(tmpdir, "migrations", "0001_create_orders.sql")
    assert os.path.isfile(fpath)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "CREATE TABLE orders" in content
    assert "DROP TABLE IF EXISTS orders" in content
    print("  建表模板: OK")

    # 加列模板
    rc = cli.run(["create", "add_bio", "-t", "add-column", "--table", "users", "--column", "bio", "--column-type", "TEXT", "--version", "0002"])
    assert rc == 0
    fpath = os.path.join(tmpdir, "migrations", "0002_add_bio.sql")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "ALTER TABLE users ADD COLUMN bio TEXT" in content
    print("  加列模板: OK")

    print("  [PASS] CLI create 模板测试通过")


def test_cli_status_filters(tmpdir):
    log("8. CLI status 分组与过滤测试")

    db_path = os.path.join(tmpdir, "status_test.db")
    cli = MigratorCLI()
    cli.migrations_dir = "migrations"
    cli.db_url = f"sqlite:///{db_path}"

    # init + up
    cli.run(["init"])
    cli.run(["up", "--steps", "2"])

    # --pending
    rc = cli.run(["status", "--pending"])
    assert rc == 0
    print("  --pending 过滤: OK")

    # --applied
    rc = cli.run(["status", "--applied"])
    assert rc == 0
    print("  --applied 过滤: OK")

    # --failed (无失败)
    rc = cli.run(["status", "--failed"])
    assert rc == 0
    print("  --failed 过滤 (无失败): OK")

    # --format json
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.run(["status", "--format", "json"])
    output = buf.getvalue()
    data = json.loads(output)
    assert "summary" in data
    assert "applied" in data
    assert "pending" in data
    assert "failed" in data
    assert data["summary"]["applied_count"] >= 1
    print(f"  JSON status: applied={data['summary']['applied_count']}, pending={data['summary']['pending_count']}")
    print("  --format json: OK")

    print("  [PASS] CLI status 分组与过滤测试通过")


def test_cli_validate_json(tmpdir):
    log("9. CLI validate JSON 输出测试")

    db_path = os.path.join(tmpdir, "validate_test.db")
    cli = MigratorCLI()
    cli.migrations_dir = "migrations"
    cli.db_url = f"sqlite:///{db_path}"

    cli.run(["init"])
    cli.run(["up"])

    # 正常校验 JSON
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.run(["validate", "--format", "json"])
    output = buf.getvalue()
    data = json.loads(output)
    assert data["valid"] is True
    assert data["error_count"] == 0
    assert data["summary"]["failed_migrations"] == 0
    assert data["summary"]["checksum_mismatches"] == 0
    print(f"  正常 validate JSON: valid={data['valid']}, error_count={data['error_count']}")

    # 篡改后校验 JSON
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE schema_migrations SET checksum = ? WHERE version = (SELECT version FROM schema_migrations LIMIT 1)", ("0" * 64,))
    conn.commit()
    cur.close()
    conn.close()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.run(["validate", "--format", "json"])
    output = buf.getvalue()
    data = json.loads(output)
    assert data["valid"] is False
    assert data["error_count"] > 0
    assert data["summary"]["checksum_mismatches"] > 0
    print(f"  篡改后 validate JSON: valid={data['valid']}, checksum_mismatches={data['summary']['checksum_mismatches']}")

    # 插入失败记录后校验 JSON
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE schema_migrations SET success = 0 WHERE version = (SELECT version FROM schema_migrations LIMIT 1)")
    conn.commit()
    cur.close()
    conn.close()

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.run(["validate", "--format", "json"])
    output = buf.getvalue()
    data = json.loads(output)
    assert data["valid"] is False
    assert data["summary"]["failed_migrations"] > 0
    print(f"  失败记录 validate JSON: failed_migrations={data['summary']['failed_migrations']}")

    print("  [PASS] CLI validate JSON 测试通过")


def test_postgresql_smoke():
    log("10. PostgreSQL 连接验收测试 (需要 psycopg2 + PG 实例)")

    pg_url = os.environ.get("PG_TEST_URL")
    if not pg_url:
        print("  跳过: 未设置 PG_TEST_URL 环境变量")
        print("  设置方法: $env:PG_TEST_URL='postgresql://user:pass@host:5432/testdb'")
        print("  安装驱动: pip install psycopg2-binary")
        return

    try:
        import psycopg2
    except ImportError:
        print("  跳过: psycopg2 未安装 (pip install psycopg2-binary)")
        return

    from migrator.storage import MigrationStorage

    print(f"  连接: {pg_url.split('@')[-1] if '@' in pg_url else pg_url}")
    conn = psycopg2.connect(pg_url)
    conn.autocommit = False

    try:
        # 清理上次测试残留
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS schema_migrations_lock CASCADE")
        cur.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        conn.commit()
        cur.close()

        # init
        storage = MigrationStorage(conn)
        storage.initialize()
        print("  [OK] init: 建表成功")

        # 验证表已创建
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN ('schema_migrations','schema_migrations_lock') "
            "ORDER BY table_name"
        )
        tables = [r[0] for r in cur.fetchall()]
        cur.close()
        assert "schema_migrations" in tables, f"schema_migrations 未创建, 实际: {tables}"
        assert "schema_migrations_lock" in tables, f"schema_migrations_lock 未创建, 实际: {tables}"
        print(f"  [OK] 建表验证: {tables}")

        # status (空库)
        applied = storage.get_applied_migrations()
        assert len(applied) == 0
        print(f"  [OK] status: 空库, 已应用 0 条")

        # validate (空库)
        errors = storage.validate_checksums({})
        assert len(errors) == 0
        print(f"  [OK] validate: 空库校验通过")

        # 插入一条模拟记录
        storage.record_migration(
            version=MigrationVersion.parse("20240101000001"),
            name="20240101000001_create_users.sql",
            checksum="abc123",
            execution_time_ms=42,
            success=True,
        )
        applied = storage.get_applied_migrations()
        assert len(applied) == 1
        assert applied[0].success is True
        print(f"  [OK] 插入记录: version={applied[0].version}, success={applied[0].success}")

        # 插入失败记录
        storage.record_migration(
            version=MigrationVersion.parse("20240102000001"),
            name="20240102000001_failed.sql",
            checksum="def456",
            execution_time_ms=99,
            success=False,
        )
        failed = storage.get_failed_migrations()
        assert len(failed) == 1
        print(f"  [OK] 失败记录: version={failed[0].version}, success={failed[0].success}")

        # 删除记录
        storage.delete_migration(MigrationVersion.parse("20240101000001"))
        applied = storage.get_applied_migrations()
        assert len(applied) == 1
        print(f"  [OK] 删除记录后: 剩余 {len(applied)} 条")

        # 锁测试
        storage.acquire_lock(timeout=5)
        print(f"  [OK] 获取锁成功")
        storage.release_lock()
        print(f"  [OK] 释放锁成功")

        # 清理
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS schema_migrations_lock CASCADE")
        cur.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
        conn.commit()
        cur.close()

        print("  [PASS] PostgreSQL 验收测试通过")

    except Exception as e:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    tmpdir = tempfile.mkdtemp(prefix="migrator_test_")
    print(f"临时目录: {tmpdir}")

    try:
        test_version()
        test_parser(tmpdir)
        test_executor(tmpdir)
        test_lock(tmpdir)
        test_diff(tmpdir)
        test_templates()
        test_cli_create_templates(tmpdir)
        test_cli_status_filters(tmpdir)
        test_cli_validate_json(tmpdir)
        test_postgresql_smoke()

        log("所有测试通过!")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
