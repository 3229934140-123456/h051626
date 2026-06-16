"""
命令行接口
==========

迁移工具命令行入口。
"""

from __future__ import annotations

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
from .exceptions import MigrationValidationError, MigrationLockError


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
        except MigrationValidationError as e:
            # 把 validator 抛的异常拆分成人能读的清单
            msg = str(e).replace("迁移完整性校验失败:\n", "").strip()
            lines = [ln for ln in msg.splitlines() if ln.strip()]
            print("\n[FAIL] 无法继续，迁移完整性校验未通过:", file=sys.stderr)
            for ln in lines:
                print(f"  - {ln.lstrip()}", file=sys.stderr)
            print("\n提示: 先运行 `migrator status` 和 `migrator validate` 查看详情。", file=sys.stderr)
            return 1
        except MigrationLockError as e:
            print(f"\n[LOCK] {e}", file=sys.stderr)
            return 2
        except NotImplementedError as e:
            print(f"\n[UNSUPPORTED] {e}", file=sys.stderr)
            return 3
        except RuntimeError as e:
            print(f"\n[ERROR] {e}", file=sys.stderr)
            return 4
        except Exception as e:
            print(f"\n[ERROR] 未处理异常: {type(e).__name__}: {e}", file=sys.stderr)
            return 99

    # ------------------------------------------------------------------
    # 数据库连接
    # ------------------------------------------------------------------

    def _connect(self):
        """
        根据 DATABASE_URL 建立连接

        支持的 URL 格式:
            sqlite:///./app.db
            postgresql://user:pass@host:5432/dbname
            postgres://user:pass@host:5432/dbname
            mysql://user:pass@host:3306/dbname
        """
        url = self.db_url

        if url.startswith("sqlite:///"):
            db_path = url[len("sqlite:///"):]
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn

        if url.startswith("postgresql://") or url.startswith("postgres://"):
            try:
                import psycopg2
            except ImportError as e:
                raise RuntimeError(
                    "需要 psycopg2 库: pip install psycopg2-binary"
                ) from e
            # psycopg2 的 connect 接受 libpq 风格的 URI，原生支持 postgresql://
            conn = psycopg2.connect(url)
            conn.autocommit = False
            return conn

        if url.startswith("mysql://") or url.startswith("mysql+pymysql://"):
            try:
                import pymysql
                from urllib.parse import urlparse
                parsed = urlparse(url)
                conn = pymysql.connect(
                    host=parsed.hostname or "127.0.0.1",
                    port=parsed.port or 3306,
                    user=parsed.username or "root",
                    password=parsed.password or "",
                    database=parsed.path.lstrip("/"),
                    charset="utf8mb4",
                )
                return conn
            except ImportError as e:
                raise RuntimeError(
                    "需要 PyMySQL 库: pip install PyMySQL"
                ) from e

        raise NotImplementedError(
            f"暂不支持的数据库 URL: {url}。"
            "支持: sqlite:///、postgresql://、mysql://"
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _print_failed_banner(self, failed: list) -> None:
        """打印失败迁移的醒目横幅"""
        if not failed:
            return
        print()
        print("+" + "-" * 68 + "+")
        print("|  [!] 检测到失败的迁移记录！后续迁移已暂停")
        print("|")
        for m in failed:
            print(f"|  [X] 版本 {m.version}  {m.name}")
        print("|")
        print("|  处理方式:")
        print("|    1. 修复失败的脚本后，手动在数据库中 UPDATE schema_migrations")
        print("|       将 success 置为 1，或者删除该记录后重新执行")
        print("|    2. 或者手动回滚该版本造成的部分变更，然后删掉这条失败记录")
        print("|    3. 修复完成后再执行 up 继续后续迁移")
        print("+" + "-" * 68 + "+")
        print()

    def _classify_validation_errors(self, errors: list):
        """把 validate 的错误分类"""
        failed_records = []
        checksum_errors = []
        missing_scripts = []
        for e in errors:
            if "标记为失败状态" in e:
                failed_records.append(e)
            elif "找不到对应脚本" in e:
                missing_scripts.append(e)
            else:
                checksum_errors.append(e)
        return failed_records, missing_scripts, checksum_errors

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

            # 先检查有没有失败的迁移，有就直接停下并提示
            failed = executor.storage.get_failed_migrations()
            if failed:
                print("[STOP] 存在失败的迁移记录，已拒绝执行新的迁移。")
                self._print_failed_banner(failed)
                return 1

            # 注册钩子
            def on_start(script, direction):
                print(f"[UP ] {script.version}  {script.description}")

            def on_success(result):
                t = result.execution_time_ms
                print(f"      OK ({t}ms)")

            def on_failure(result):
                print(f"[FAIL] 版本 {result.version} 执行失败:")
                if result.error:
                    # 按行缩进，方便人眼定位
                    for line in result.error.splitlines():
                        print(f"       {line}")

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

            success_count = sum(1 for r in results if r.status == MigrationStatus.SUCCESS)
            failed_count = sum(1 for r in results if r.status == MigrationStatus.FAILED)
            dry_tag = " (dry-run)" if args.dry_run else ""

            print(f"\n完成{dry_tag}: {success_count} 成功, {failed_count} 失败")
            if failed_count:
                failed_results = [r for r in results if r.status == MigrationStatus.FAILED]
                # 从状态表里取出失败记录，打印统一的横幅
                try:
                    failed = executor.storage.get_failed_migrations()
                    if failed:
                        self._print_failed_banner(failed)
                except Exception:
                    pass
                return 1
            return 0

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
                print(f"[FAIL] 版本 {result.version} 回滚失败:")
                if result.error:
                    for line in result.error.splitlines():
                        print(f"       {line}")

            executor.on_migration_start = on_start
            executor.on_migration_success = on_success
            executor.on_migration_failure = on_failure

            executor.storage.acquire_lock()
            try:
                results = executor.down(target_version=args.target, steps=args.steps)
            finally:
                executor.storage.release_lock()

            success_count = sum(1 for r in results if r.status == MigrationStatus.SUCCESS)
            failed_count = sum(1 for r in results if r.status == MigrationStatus.FAILED)
            dry_tag = " (dry-run)" if args.dry_run else ""
            print(f"\n完成{dry_tag}: {success_count} 成功, {failed_count} 失败")
            return 1 if failed_count else 0

        finally:
            conn.close()

    def cmd_redo(self, args) -> int:
        """重新执行最近的迁移"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()

            failed = executor.storage.get_failed_migrations()
            if failed:
                print("[STOP] 存在失败的迁移记录，已拒绝执行 redo。请先处理失败记录。")
                self._print_failed_banner(failed)
                return 1

            executor.storage.acquire_lock()
            try:
                results = executor.redo(steps=args.steps)
            finally:
                executor.storage.release_lock()

            for r in results:
                tag = "OK" if r.status == MigrationStatus.SUCCESS else "FAIL"
                print(f"[{r.direction.value.upper():4}] {r.version}  {tag} ({r.execution_time_ms}ms)")
                if r.status == MigrationStatus.FAILED and r.error:
                    for line in r.error.splitlines():
                        print(f"       {line}")

            failed_results = [r for r in results if r.status == MigrationStatus.FAILED]
            return 1 if failed_results else 0

        finally:
            conn.close()

    def cmd_status(self, args) -> int:
        """查看状态"""
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            st = executor.status()
            applied = executor.storage.get_applied_migrations()
            failed = executor.storage.get_failed_migrations()

            # 摘要
            latest = st['latest_version']
            latest_str = f"{latest}" if latest else "(无)"
            pending_str = f"{st['pending_count']}" if st['pending_count'] else "0 (已是最新)"
            failed_str = f"{st['failed_count']}  [!] 需要处理!" if st['failed_count'] else "0"

            print("迁移状态概览")
            print("-" * 40)
            print(f"总脚本数:   {st['total_scripts']}")
            print(f"已应用:     {st['applied_count']}")
            print(f"待应用:     {pending_str}")
            print(f"失败记录:   {failed_str}")
            print(f"当前版本:   {latest_str}")

            # 失败记录优先展示，最醒目
            if failed:
                self._print_failed_banner(failed)

            # 已应用迁移 (最多显示最近 20 条)
            if applied:
                print("\n已应用迁移 (最近20条):")
                for m in applied[-20:]:
                    tag = "[OK] " if m.success else "[FAIL]"
                    time_str = m.applied_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(m.applied_at, "strftime") else str(m.applied_at)
                    print(
                        f"  {tag}  {str(m.version):<20}  "
                        f"{time_str}  ({m.execution_time_ms}ms)  {m.name}"
                    )
                if len(applied) > 20:
                    print(f"  ... 还有 {len(applied) - 20} 条历史迁移")

            if st["pending_versions"]:
                print("\n待应用迁移 (按执行顺序):")
                scripts = executor.parser.parse_directory()
                applied_versions = {m.version for m in applied}
                pending = [s for s in scripts if s.version not in applied_versions]
                pending.sort(key=lambda s: s.version)
                for s in pending:
                    print(f"  [->] {s.version}  {s.description}")

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
            failed_records, missing_scripts, checksum_errors = self._classify_validation_errors(errors)

            total = len(errors)
            if total == 0:
                print("[OK] 所有迁移校验通过")
                print("   - 已应用脚本 checksum 与磁盘文件一致")
                print("   - 无失败的迁移记录")
                return 0

            print("[FAIL] 迁移完整性校验失败\n")

            if failed_records:
                print(f"[!] 失败迁移 ({len(failed_records)} 条):")
                for e in failed_records:
                    print(f"    - {e}")
                print("  -> 需要先处理失败记录（手动回滚部分变更 + 删除/修正失败记录）")
                print()

            if missing_scripts:
                print(f"[!] 已应用但缺少脚本文件 ({len(missing_scripts)} 条):")
                for e in missing_scripts:
                    print(f"    - {e}")
                print("  -> 请恢复对应版本的脚本文件，或确认该迁移可以删除后手动清库")
                print()

            if checksum_errors:
                print(f"[!] 脚本内容被篡改/不匹配 ({len(checksum_errors)} 条):")
                for e in checksum_errors:
                    for i, line in enumerate(e.splitlines()):
                        prefix = "    - " if i == 0 else "      "
                        print(f"{prefix}{line}")
                print("  -> 恢复原始脚本内容；如果确实需要修改，请先回滚该版本再重新应用")
                print()

            print("详细故障排查请运行: migrator status")
            return 1
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
