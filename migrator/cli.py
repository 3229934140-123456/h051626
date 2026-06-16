"""
命令行接口
==========

迁移工具命令行入口。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional

from .diff import DatabaseSchema, SchemaDiffer, TableSchema, ColumnSchema
from .executor import MigrationExecutor, MigrationStatus
from .parser import MigrationParser
from .storage import MigrationStorage
from .version import MigrationVersion
from .exceptions import MigrationValidationError, MigrationLockError
from .templates import TEMPLATES, render_template, list_templates
from .config import MigratorConfig, load_config


class MigratorCLI:
    """命令行控制器"""

    def __init__(self):
        self.parser = self._build_parser()
        # 预先加载默认值，便于测试时直接覆盖属性
        self.config: MigratorConfig = load_config()
        self.migrations_dir = self.config.migrations_dir
        self.db_url = self.config.db_url
        self.lock_timeout = self.config.lock_timeout
        self.allow_dirty = self.config.allow_dirty

    # ------------------------------------------------------------------
    # 参数解析
    # ------------------------------------------------------------------

    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="migrator",
            description="SQL 数据库迁移工具",
        )

        # 全局参数 (配置文件/连接/目录)
        parser.add_argument("--config", "-c", dest="config_path", help="配置文件路径 (默认 migrator.toml)")
        parser.add_argument("--db-url", dest="db_url", help="数据库 URL (覆盖配置文件)")
        parser.add_argument("--dir", dest="migrations_dir", help="迁移脚本目录 (覆盖配置文件)")
        parser.add_argument("--lock-timeout", type=int, dest="lock_timeout", help="锁超时秒数 (覆盖配置文件)")
        parser.add_argument("--allow-dirty", action="store_true", dest="allow_dirty", help="允许脏库继续迁移 (存在失败记录时仍允许 up)")

        subparsers = parser.add_subparsers(dest="command", required=True)

        # init
        subparsers.add_parser("init", help="初始化迁移环境")

        # create
        p_create = subparsers.add_parser("create", help="创建新迁移脚本")
        p_create.add_argument("description", nargs="?", default="unnamed", help="迁移描述 (如 create_users)")
        p_create.add_argument(
            "--version", dest="version",
            help="指定版本号，默认使用时间戳",
        )
        p_create.add_argument(
            "--template", "-t", dest="template",
            choices=list(TEMPLATES.keys()),
            help="脚本模板 (blank/create-table/add-column/create-index/drop-table/drop-column/add-fk)",
        )
        p_create.add_argument("--table", dest="table_name", help="模板变量: 表名")
        p_create.add_argument("--column", dest="column_name", help="模板变量: 列名")
        p_create.add_argument("--column-type", dest="column_type", default="VARCHAR(255)", help="模板变量: 列类型 (默认 VARCHAR(255))")
        p_create.add_argument("--index", dest="index_name", help="模板变量: 索引名")
        p_create.add_argument("--columns", dest="column_list", help="模板变量: 索引列 (逗号分隔)")
        p_create.add_argument("--unique", action="store_true", help="模板变量: 唯一索引")
        p_create.add_argument("--fk", dest="fk_name", help="模板变量: 外键名")
        p_create.add_argument("--ref-table", dest="ref_table", help="模板变量: 引用表名")
        p_create.add_argument("--ref-column", dest="ref_column", help="模板变量: 引用列名")
        p_create.add_argument("--list-templates", action="store_true", help="列出可用模板")

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
        p_status = subparsers.add_parser("status", help="查看迁移状态")
        p_status.add_argument("--pending", action="store_true", help="只看待执行迁移")
        p_status.add_argument("--failed", action="store_true", help="只看失败迁移")
        p_status.add_argument("--applied", action="store_true", help="只看已应用迁移")
        p_status.add_argument("--format", dest="format", choices=["text", "json"], default="text", help="输出格式 (text/json)")

        # plan
        p_plan = subparsers.add_parser("plan", help="预览将要执行的 up/down 迁移计划 (dry-run 不执行)")
        p_plan.add_argument("direction", nargs="?", default="up", choices=["up", "down"], help="迁移方向 (默认 up)")
        p_plan.add_argument("--steps", type=int, help="执行步数")
        p_plan.add_argument("--to", dest="target", help="目标版本")
        p_plan.add_argument("--format", dest="format", choices=["text", "json"], default="text", help="输出格式 (text/json)")
        p_plan.add_argument("--sql-lines", type=int, default=3, dest="sql_lines", help="每个方向显示多少条 SQL 摘要 (默认 3, 0 表示不显示)")

        # validate
        p_validate = subparsers.add_parser("validate", help="校验迁移完整性")
        p_validate.add_argument("--format", dest="format", choices=["text", "json"], default="text", help="输出格式 (text/json)")

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

        # 记录调用前已被外部代码显式设置的值 (用于保持向后兼容的测试方式)
        _default = MigratorConfig()
        existing_overrides = {}
        if self.db_url != _default.db_url:
            existing_overrides["db_url"] = self.db_url
        if self.migrations_dir != _default.migrations_dir:
            existing_overrides["migrations_dir"] = self.migrations_dir
        if self.lock_timeout != _default.lock_timeout:
            existing_overrides["lock_timeout"] = self.lock_timeout
        if self.allow_dirty != _default.allow_dirty:
            existing_overrides["allow_dirty"] = self.allow_dirty

        # 用 CLI 全局参数覆盖配置
        cli_overrides = {
            "db_url": getattr(args, "db_url", None),
            "migrations_dir": getattr(args, "migrations_dir", None),
            "lock_timeout": getattr(args, "lock_timeout", None),
        }
        # CLI 参数优先于 existing_overrides
        for k, v in cli_overrides.items():
            if v is not None:
                existing_overrides[k] = v

        allow_dirty_flag = getattr(args, "allow_dirty", False)
        if allow_dirty_flag:
            existing_overrides["allow_dirty"] = True

        config_path = getattr(args, "config_path", None) or self.config.config_path
        self.config = load_config(config_path, existing_overrides if existing_overrides else None)
        if allow_dirty_flag:
            self.config.allow_dirty = True

        self.migrations_dir = self.config.migrations_dir
        self.db_url = self.config.db_url
        self.lock_timeout = self.config.lock_timeout
        self.allow_dirty = self.config.allow_dirty

        try:
            return getattr(self, f"cmd_{args.command}")(args)
        except MigrationValidationError as e:
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

    def _format_applied_at(self, val) -> str:
        if hasattr(val, "strftime"):
            return val.strftime("%Y-%m-%d %H:%M:%S")
        return str(val)

    # ------------------------------------------------------------------
    # 命令实现
    # ------------------------------------------------------------------

    def cmd_init(self, args) -> int:
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
        if args.list_templates:
            print(list_templates())
            return 0

        os.makedirs(self.migrations_dir, exist_ok=True)

        version = args.version or datetime.now().strftime("%Y%m%d%H%M%S")
        desc = args.description.strip().replace(" ", "_").lower()
        filename = f"{version}_{desc}.sql"
        filepath = os.path.join(self.migrations_dir, filename)

        if args.template:
            tmpl_vars = {
                "table_name": args.table_name or "<table_name>",
                "column_name": args.column_name or "<column_name>",
                "column_type": args.column_type or "VARCHAR(255)",
                "index_name": args.index_name or "<index_name>",
                "column_list": args.column_list or "<column_list>",
                "unique": "UNIQUE " if args.unique else "",
                "fk_name": args.fk_name or "<fk_name>",
                "ref_table": args.ref_table or "<ref_table>",
                "ref_column": args.ref_column or "<ref_column>",
            }
            content = render_template(args.template, args.description, **tmpl_vars)
        else:
            content = (
                f"-- migrate: description={args.description}\n"
                f"-- migrate: author=dev\n"
                f"-- migrate: transaction=true\n"
                f"\n"
                f"-- +migrate Up\n"
                f"-- 在此写入升级 SQL\n\n"
                f"-- +migrate Down\n"
                f"-- 在此写入回滚 SQL\n"
            )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        print(f"[OK] 创建迁移脚本: {filepath}")
        if args.template:
            print(f"    模板: {args.template}")
        return 0

    def cmd_up(self, args) -> int:
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            executor.dry_run = args.dry_run

            failed = executor.storage.get_failed_migrations()
            if failed and not self.allow_dirty:
                print("[STOP] 存在失败的迁移记录，已拒绝执行新的迁移。")
                self._print_failed_banner(failed)
                return 1
            if failed and self.allow_dirty:
                print(f"[WARN] 允许脏库继续: 检测到 {len(failed)} 条失败迁移记录 (--allow-dirty)")

            def on_start(script, direction):
                print(f"[UP ] {script.version}  {script.description}")

            def on_success(result):
                print(f"      OK ({result.execution_time_ms}ms)")

            def on_failure(result):
                print(f"[FAIL] 版本 {result.version} 执行失败:")
                if result.error:
                    for line in result.error.splitlines():
                        print(f"       {line}")

            executor.on_migration_start = on_start
            executor.on_migration_success = on_success
            executor.on_migration_failure = on_failure

            executor.storage.acquire_lock(timeout=self.lock_timeout)
            try:
                results = executor.up(target_version=args.target, steps=args.steps)
            finally:
                executor.storage.release_lock()

            success_count = sum(1 for r in results if r.status == MigrationStatus.SUCCESS)
            failed_count = sum(1 for r in results if r.status == MigrationStatus.FAILED)
            dry_tag = " (dry-run)" if args.dry_run else ""

            print(f"\n完成{dry_tag}: {success_count} 成功, {failed_count} 失败")
            if failed_count:
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
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            executor.dry_run = args.dry_run

            def on_start(script, direction):
                print(f"[DOWN] {script.version}  {script.description}")

            def on_success(result):
                print(f"      OK ({result.execution_time_ms}ms)")

            def on_failure(result):
                print(f"[FAIL] 版本 {result.version} 回滚失败:")
                if result.error:
                    for line in result.error.splitlines():
                        print(f"       {line}")

            executor.on_migration_start = on_start
            executor.on_migration_success = on_success
            executor.on_migration_failure = on_failure

            executor.storage.acquire_lock(timeout=self.lock_timeout)
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
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()

            failed = executor.storage.get_failed_migrations()
            if failed and not self.allow_dirty:
                print("[STOP] 存在失败的迁移记录，已拒绝执行 redo。请先处理失败记录。")
                self._print_failed_banner(failed)
                return 1
            if failed and self.allow_dirty:
                print(f"[WARN] 允许脏库继续: 检测到 {len(failed)} 条失败迁移记录 (--allow-dirty)")

            executor.storage.acquire_lock(timeout=self.lock_timeout)
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

    # ------------------------------------------------------------------
    # plan (迁移计划预览)
    # ------------------------------------------------------------------

    def _sql_summary(self, statements: list, max_lines: int) -> str:
        if max_lines <= 0:
            return ""
        shown = statements[:max_lines]
        lines = []
        for s in shown:
            snippet = s.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            lines.append(snippet)
        if len(statements) > max_lines:
            lines.append(f"... 省略 {len(statements) - max_lines} 条语句")
        return "\n".join(lines)

    def cmd_plan(self, args) -> int:
        from .executor import MigrationDirection

        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()

            direction = MigrationDirection.UP if args.direction == "up" else MigrationDirection.DOWN

            if direction == MigrationDirection.UP:
                plan = executor.plan_up(target_version=args.target, steps=args.steps)
            else:
                plan = executor.plan_down(target_version=args.target, steps=args.steps)

            # ---- 构建 JSON 数据 ----
            items = []
            for script in plan.scripts:
                up_summary = self._sql_summary(script.up_sql, args.sql_lines)
                down_summary = self._sql_summary(script.down_sql, args.sql_lines)
                items.append({
                    "version": str(script.version),
                    "description": script.description,
                    "filename": script.filename,
                    "direction": direction.value,
                    "checksum": script.checksum,
                    "up_statements": len(script.up_sql),
                    "down_statements": len(script.down_sql),
                    "up_sql_preview": up_summary,
                    "down_sql_preview": down_summary,
                })

            json_data = {
                "direction": direction.value,
                "count": plan.count,
                "items": items,
            }

            if args.format == "json":
                print(json.dumps(json_data, ensure_ascii=False, indent=2))
                return 0

            # ---- 文本输出 ----
            print(f"迁移计划预览: {direction.value.upper()} (共 {plan.count} 个)")
            print("-" * 70)
            if plan.count == 0:
                print("  (无待执行迁移)")
                return 0

            for i, script in enumerate(plan.scripts, 1):
                arrow = "[UP ]" if direction == MigrationDirection.UP else "[DOWN]"
                print(f"\n  {i:>2}. {arrow} {script.version}  {script.description}")
                print(f"      文件: {script.filename}")
                print(f"      up: {len(script.up_sql)} 语句, down: {len(script.down_sql)} 语句")

                up_preview = self._sql_summary(script.up_sql, args.sql_lines)
                if up_preview:
                    print(f"      up SQL 预览:")
                    for line in up_preview.splitlines():
                        print(f"        {line}")

                down_preview = self._sql_summary(script.down_sql, args.sql_lines)
                if down_preview:
                    print(f"      down SQL 预览:")
                    for line in down_preview.splitlines():
                        print(f"        {line}")

            print()
            print(f"总计: {plan.count} 个迁移待 {direction.value}")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # status (增强: 分组 + 过滤 + JSON)
    # ------------------------------------------------------------------

    def cmd_status(self, args) -> int:
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            st = executor.status()
            applied = executor.storage.get_applied_migrations()
            failed = executor.storage.get_failed_migrations()
            success_applied = [m for m in applied if m.success]

            scripts = executor.parser.parse_directory()
            all_applied_versions = {m.version for m in applied}
            pending = sorted(
                [s for s in scripts if s.version not in all_applied_versions],
                key=lambda s: s.version,
            )

            # ---- 构建 JSON 数据 ----
            json_data = {
                "summary": {
                    "total_scripts": st["total_scripts"],
                    "applied_count": st["applied_count"],
                    "pending_count": st["pending_count"],
                    "failed_count": st["failed_count"],
                    "latest_version": str(st["latest_version"]) if st["latest_version"] else None,
                },
                "applied": [
                    {
                        "version": str(m.version),
                        "name": m.name,
                        "checksum": m.checksum,
                        "applied_at": self._format_applied_at(m.applied_at),
                        "execution_time_ms": m.execution_time_ms,
                        "success": m.success,
                    }
                    for m in success_applied
                ],
                "pending": [
                    {
                        "version": str(s.version),
                        "description": s.description,
                        "filename": s.filename,
                    }
                    for s in pending
                ],
                "failed": [
                    {
                        "version": str(m.version),
                        "name": m.name,
                        "applied_at": self._format_applied_at(m.applied_at),
                        "execution_time_ms": m.execution_time_ms,
                    }
                    for m in failed
                ],
            }

            if args.format == "json":
                print(json.dumps(json_data, ensure_ascii=False, indent=2))
                return 1 if json_data["summary"]["failed_count"] > 0 else 0

            # ---- 文本输出 ----
            show_pending_only = args.pending
            show_failed_only = args.failed
            show_applied_only = args.applied
            show_all = not (show_pending_only or show_failed_only or show_applied_only)

            # 概览 (仅在全局视图时显示)
            if show_all:
                latest = st["latest_version"]
                latest_str = str(latest) if latest else "(无)"
                pending_str = str(st["pending_count"]) if st["pending_count"] else "0 (已是最新)"
                failed_str = f"{st['failed_count']}  [!] 需要处理!" if st["failed_count"] else "0"

                print("迁移状态概览")
                print("-" * 40)
                print(f"总脚本数:   {st['total_scripts']}")
                print(f"已应用:     {st['applied_count']}")
                print(f"待应用:     {pending_str}")
                print(f"失败记录:   {failed_str}")
                print(f"当前版本:   {latest_str}")

                if failed:
                    self._print_failed_banner(failed)

            # --failed 过滤
            if show_failed_only or show_all:
                if failed:
                    header = "失败迁移" if show_failed_only else "\n失败迁移"
                    print(f"{header}:")
                    for m in failed:
                        t = self._format_applied_at(m.applied_at)
                        print(f"  [FAIL]  {str(m.version):<20}  {t}  ({m.execution_time_ms}ms)  {m.name}")
                    if show_failed_only and not failed:
                        print("  (无失败迁移)")

            # --applied 过滤
            if show_applied_only or show_all:
                success_applied = [m for m in applied if m.success]
                if success_applied or show_applied_only:
                    header = "已应用迁移 (成功)" if show_applied_only else "\n已应用迁移 (成功)"
                    print(f"{header}:")
                    if success_applied:
                        for m in success_applied[-20:]:
                            t = self._format_applied_at(m.applied_at)
                            print(f"  [OK]   {str(m.version):<20}  {t}  ({m.execution_time_ms}ms)  {m.name}")
                        if len(success_applied) > 20:
                            print(f"  ... 还有 {len(success_applied) - 20} 条")
                    else:
                        print("  (无)")

            # --pending 过滤
            if show_pending_only or show_all:
                header = "待应用迁移" if show_pending_only else "\n待应用迁移 (按执行顺序)"
                print(f"{header}:")
                if pending:
                    for s in pending:
                        print(f"  [->]  {s.version}  {s.description}")
                else:
                    print("  (无，已是最新)")

            return 1 if json_data["summary"]["failed_count"] > 0 else 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # validate (增强: JSON 输出)
    # ------------------------------------------------------------------

    def cmd_validate(self, args) -> int:
        conn = self._connect()
        try:
            executor = MigrationExecutor(conn, self.migrations_dir)
            executor.initialize()
            errors = executor.validate()
            failed_records, missing_scripts, checksum_errors = self._classify_validation_errors(errors)

            # ---- JSON 数据 ----
            json_data = {
                "valid": len(errors) == 0,
                "error_count": len(errors),
                "errors": {
                    "failed_migrations": [
                        {"message": e} for e in failed_records
                    ],
                    "missing_scripts": [
                        {"message": e} for e in missing_scripts
                    ],
                    "checksum_mismatches": [
                        {"message": e} for e in checksum_errors
                    ],
                },
                "summary": {
                    "failed_migrations": len(failed_records),
                    "missing_scripts": len(missing_scripts),
                    "checksum_mismatches": len(checksum_errors),
                },
            }

            if args.format == "json":
                print(json.dumps(json_data, ensure_ascii=False, indent=2))
                return 1 if not json_data["valid"] else 0

            # ---- 文本输出 ----
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
        conn = self._connect()
        try:
            differ = SchemaDiffer(conn)

            if args.schema_file:
                import importlib.util
                spec = importlib.util.spec_from_file_location("schema_def", args.schema_file)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                target = mod.get_target_schema()
            else:
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
