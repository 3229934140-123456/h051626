"""
命令行接口
==========

使用示例:
    python -m migrator init                    # 初始化迁移环境
    python -m migrator create create_users     # 创建新迁移脚本
    python -m migrator up                      # 执行所有待应用迁移
    python -m migrator up --steps 2            # 应用接下来 2 个迁移
    python -m migrator up --to 20240101120000  # 应用到指定版本
    python -m migrator down                    # 回滚最后一个迁移
    python -m migrator down --steps 3          # 回滚最近 3 个迁移
    python -m migrator down --to 20240101120000
    python -m migrator redo                    # 回滚并重做最后一个迁移
    python -m migrator status                  # 查看迁移状态
    python -m migrator validate                # 校验迁移完整性
    python -m migrator generate                # 从现有 schema 生成迁移
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from typing import List, Optional

from .diff import DatabaseSchema, SchemaDiffer, TableSchema, ColumnSchema
from .executor import MigrationExecutor, MigrationStatus
from .parser import MigrationParser
from .storage import MigrationStorage
from .version import MigrationVersion


class MigratorCLI:
    """命令行控制器"""

    def __init__(self):
        self.parser = self._build_parser()
        self.migrations_dir = os.environ.get("MIGRATIONS_DIR", "migrations")
        self.db_url = os.environ.get("DATABASE_URL", "sqlite:///./app.db")

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="migrator",
            description="SQL 数据库迁移工具",
        )
        subparsers = parser.add_subparsers(dest="command", required=True)

        # init
        subparsers.add_parser("init", help="初始化迁移环境")

        # create
        p_create = subparsers.add_parser("create", help="创建新迁移脚本")
        p_create.add_argument("description", help="迁移描述 (如 create_users)")
        p_create.add_argument(
            "--version", dest="version",
            help="指定版本号，默认使用时间戳",
        )

        # up
        p_up = subparsers.add_parser("up", help="执行待应用迁移")
        p_up.add_argument("--steps", type=int, help="执行步数")
        p_up.add_argument("--to", dest="target", help="目标版本")
        p_up.add_argument("--dry-run", action="store_true", help="仅显示 SQL，不执行")

        # down
        p_down = subparsers.add_parser("down", help="回滚迁移")
        p_down.add_argument("--steps", type=int, default=1, help="回滚步数")
        p_down.add_argument("--to", dest="target", help="回滚到该版本之后")
        p_down.add_argument("--dry-run", action="store_true")

        # redo
        p_redo = subparsers.add_parser("redo", help="回滚并重做迁移")
        p_redo.add_argument("--steps", type=int, default=1, help="重做步数")

        # status
        subparsers.add_parser("status", help="查看迁移状态")

        # validate
        subparsers.add_parser("validate", help="校验迁移完整性")

        # generate
        p_gen = subparsers.add_parser("generate", help="从现有 schema 生成迁移")
        p_gen.add_argument("description", help="迁移描述")
        p_gen.add_argument(
            "--schema-file", dest="schema_file",
            help="目标 schema 定义的 Python 文件路径",
        )

        return parser

    # ------------------------------------------------------------------
    # 运行入口
    # ------------------------------------------------------------------

    def run(self, argv: Optional[List[str]] = None) -> int:
        args = self.parser.parse_args(argv)
        try:
            return getattr(self, f"cmd_{args.command}")(args)
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            return 1

    # ------------------------------------------------------------------
    # 数据库连接
    # ------------------------------------------------------------------

    def _connect(self):
        """根据 DATABASE_URL 建立连接（简化版：仅 SQLite）"""
        if self.db_url.startswith("sqlite:///"):
            db_path = self.db_url[len("sqlite:///"):]
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        # 其他数据库需扩展
        raise NotImplementedError(
            f"暂不支持的数据库 URL: {self.db_url}。请手动扩展 _connect() 方法。"
        )

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------

    def cmd_init(self, args) -> int:
        """初始化迁移环境"""
        os.makedirs(self.migrations_dir, exist_ok=True)
        conn = self._connect()
        try:
            storage = MigrationStorage(conn)
            storage.initialize()
            print(f"[OK] 迁移目录: {self.migrations_dir}/")
            print(f"[OK] 状态表已初始化")
        finally:
            conn.close()
        return 0

    def cmd_create(self, args) -> int:
        """创建新迁移脚本"""
        os.makedirs(self.migrations_dir, exist_ok=True)

        version = args.version or datetime.now().strftime("%Y%m%d%H%M%S")
        desc = args.description.strip().replace(" ", "_").lower()
        filename = f"{version}_{desc}.sql"
        filepath = os.path.join(self.migrations_dir, filename)

        content = f"""-- migrate: description={args.description}
-- migrate: author=dev
-- migrate: transaction=true

-- +migrate Up
-- 在此写入升级 SQL，例如:
-- CREATE TABLE users (
--     id INTEGER PRIMARY KEY,
--     name VARCHAR(255) NOT NULL
-- );

-- +migrate Down
-- 在此写入回滚 SQL，例如:
-- DROP TABLE users;
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"[OK] 创建迁移脚本: {filepath}")
        return 0

    def cmd_up(self, args) -> int:
        """执行 up 迁移"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            executor.dry_run = args.dry_run

            # 注册钩子
            def on_start(script, direction):
                print(f"[UP ] {script.version}  {script.description}")

            def on_success(result):
                t = result.execution_time_ms
                print(f"      OK ({t}ms)")

            def on_failure(result):
                print(f"[FAIL] {result.error}")

            executor.on_migration_start = on_start
            executor.on_migration_success = on_success
            executor.on_migration_failure = on_failure

            # 获取锁
            executor.storage.acquire_lock()
            try:
                target = args.target
                results = executor.up(target_version=target, steps=args.steps)
            finally:
                executor.storage.release_lock()

            failed = [r for r in results if r.status == MigrationStatus.FAILED]
            print(f"\n完成: {len(results) - len(failed)} 成功, {len(failed)} 失败")
            return 1 if failed else 0

        finally:
            conn.close()

    def cmd_down(self, args) -> int:
        """执行 down 回滚"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            executor.dry_run = args.dry_run

            def on_start(script, direction):
                print(f"[DOWN] {script.version}  {script.description}")

            def on_success(result):
                t = result.execution_time_ms
                print(f"      OK ({t}ms)")

            def on_failure(result):
                print(f"[FAIL] {result.error}")

            executor.on_migration_start = on_start
            executor.on_migration_success = on_success
            executor.on_migration_failure = on_failure

            executor.storage.acquire_lock()
            try:
                results = executor.down(target_version=args.target, steps=args.steps)
            finally:
                executor.storage.release_lock()

            failed = [r for r in results if r.status == MigrationStatus.FAILED]
            print(f"\n完成: {len(results) - len(failed)} 成功, {len(failed)} 失败")
            return 1 if failed else 0

        finally:
            conn.close()

    def cmd_redo(self, args) -> int:
        """重新执行最近的迁移"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()

            executor.storage.acquire_lock()
            try:
                results = executor.redo(steps=args.steps)
            finally:
                executor.storage.release_lock()

            for r in results:
                print(f"[{r.direction.value.upper():4}] {r.version}  {r.status.value}")

            failed = [r for r in results if r.status == MigrationStatus.FAILED]
            return 1 if failed else 0

        finally:
            conn.close()

    def cmd_status(self, args) -> int:
        """查看状态"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            st = executor.status()

            print(f"总脚本数:    {st['total_scripts']}")
            print(f"已应用:      {st['applied_count']}")
            print(f"待应用:      {st['pending_count']}")
            print(f"失败:        {st['failed_count']}")
            print(f"当前版本:    {st['latest_version'] or '(无)'}")

            # 列出最近几个已应用的
            applied = executor.storage.get_applied_migrations()
            if applied:
                print("\n已应用迁移:")
                for m in applied[-10:]:
                    status = "OK" if m.success else "FAILED"
                    print(f"  {m.version}  {status:6}  {m.name}")

            if st["pending_versions"]:
                print("\n待应用迁移:")
                scripts = executor.parser.parse_directory()
                applied_versions = {m.version for m in applied}
                for s in scripts:
                    if s.version not in applied_versions:
                        print(f"  {s.version}  {s.description}")

            return 0
        finally:
            conn.close()

    def cmd_validate(self, args) -> int:
        """校验完整性"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            errors = executor.validate()
            if errors:
                print("[FAIL] 校验失败:")
                for e in errors:
                    print(f"  - {e}")
                return 1
            else:
                print("[OK] 所有迁移校验通过")
                return 0
        finally:
            conn.close()

    def cmd_generate(self, args) -> int:
        """从现有 schema 生成迁移"""
        conn = self._connect()
        try:
            differ = SchemaDiffer(conn)

            # 简化：以当前数据库为 source，target 需用户提供 schema 定义
            # 这里演示：如果没有提供 schema_file，则从现有库生成基线
            if args.schema_file:
                # 导入用户定义的 target schema
                import importlib.util
                spec = importlib.util.spec_from_file_location("schema_def", args.schema_file)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                target = mod.get_target_schema()
            else:
                # 生成基线迁移（第一次）
                target = DatabaseSchema()
                source = differ.extractor.extract()
                # 从 source 拷贝作为 target，差异为 0 —— 这里改为演示一个示例
                print("[INFO] 未提供 target schema，将生成一个示例空迁移")
                target = DatabaseSchema()

            up_sql, down_sql = differ.generate_migration(differ.extractor.extract(), target)

            if not up_sql and not down_sql:
                print("[INFO] 未检测到 schema 差异")
                return 0

            os.makedirs(self.migrations_dir, exist_ok=True)
            version = datetime.now().strftime("%Y%m%d%H%M%S")
            desc = args.description.strip().replace(" ", "_").lower()
            filename = f"{version}_{desc}.sql"
            filepath = os.path.join(self.migrations_dir, filename)

            content = differ.generate_migration_file_content(
                differ.extractor.extract(), target, args.description
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)

            print(f"[OK] 生成迁移脚本: {filepath}")
            print("[WARN] 请人工审核自动生成的迁移脚本，特别是涉及数据删除的操作")
            return 0

        finally:
            conn.close()


def main():
    cli = MigratorCLI()
    sys.exit(cli.run())


if __name__ == "__main__":
    main()
