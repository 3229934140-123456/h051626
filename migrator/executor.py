"""
执行引擎模块
============

负责迁移的实际执行、事务包裹、失败处理。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Union

from .exceptions import MigrationExecutionError, MigrationValidationError
from .parser import MigrationParser, MigrationScript
from .storage import AppliedMigration, MigrationStorage
from .version import MigrationVersion, VersionManager


class MigrationDirection(str, Enum):
    """迁移方向"""

    UP = "up"
    DOWN = "down"


class MigrationStatus(str, Enum):
    """迁移执行状态"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class MigrationResult:
    """单个迁移执行结果"""

    version: MigrationVersion
    name: str
    direction: MigrationDirection
    status: MigrationStatus
    execution_time_ms: int = 0
    error: Optional[str] = None
    statements_executed: int = 0


@dataclass
class MigrationPlan:
    """迁移执行计划"""

    direction: MigrationDirection
    scripts: List[MigrationScript] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.scripts)

    def __repr__(self) -> str:
        return f"MigrationPlan(direction={self.direction.value}, count={self.count})"


class MigrationExecutor:
    """
    迁移执行引擎

    工作流程:
        1. 解析迁移目录，加载所有脚本
        2. 校验已应用脚本的完整性（校验和）
        3. 计算迁移计划（待应用的版本集合）
        4. 按序执行每一个迁移：
           a. 开启事务
           b. 执行 up/down SQL
           c. 提交事务
           d. 更新 schema_migrations 记录
        5. 如果失败：
           - 如果支持 DDL 事务：回滚事务
           - 否则：标记迁移为 failed 状态
    """

    def __init__(
        self,
        connection,
        migrations_dir: str = "migrations",
    ):
        """
        Args:
            connection: DB-API 2.0 兼容数据库连接
            migrations_dir: 迁移脚本目录
        """
        self.conn = connection
        self.storage = MigrationStorage(connection)
        self.parser = MigrationParser(migrations_dir)
        self.migrations_dir = migrations_dir

        # 钩子回调
        self.on_migration_start: Optional[Callable[[MigrationScript, MigrationDirection], None]] = None
        self.on_migration_success: Optional[Callable[[MigrationResult], None]] = None
        self.on_migration_failure: Optional[Callable[[MigrationResult], None]] = None
        self.on_statement: Optional[Callable[[str, MigrationDirection], None]] = None

        # 配置
        self.dry_run = False
        self.single_transaction = False
        self.history_source: Optional[str] = None

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """初始化存储（创建状态表等）"""
        self.storage.initialize()
        # 第一次初始化时，如果 history_source 没设置, 填一个默认值
        if self.history_source is None:
            import socket
            try:
                self.history_source = socket.gethostname()
            except Exception:
                self.history_source = "unknown"

    # ------------------------------------------------------------------
    # 计划生成
    # ------------------------------------------------------------------

    def plan_up(
        self,
        target_version: Optional[Union[str, MigrationVersion]] = None,
        steps: Optional[int] = None,
    ) -> MigrationPlan:
        """
        生成 up 迁移计划

        Args:
            target_version: 目标版本（包含），None 表示到最新
            steps: 迁移步数，与 target_version 互斥

        Returns:
            MigrationPlan
        """
        return self._build_plan(MigrationDirection.UP, target_version, steps)

    def plan_down(
        self,
        target_version: Optional[Union[str, MigrationVersion]] = None,
        steps: Optional[int] = None,
    ) -> MigrationPlan:
        """
        生成 down 回滚计划

        Args:
            target_version: 回滚到该版本之后（即该版本保留），None 回滚全部
            steps: 回滚步数，与 target_version 互斥

        Returns:
            MigrationPlan
        """
        return self._build_plan(MigrationDirection.DOWN, target_version, steps)

    def _build_plan(
        self,
        direction: MigrationDirection,
        target_version: Optional[Union[str, MigrationVersion]],
        steps: Optional[int],
    ) -> MigrationPlan:
        scripts = self.parser.parse_directory()
        applied = {m.version for m in self.storage.get_applied_migrations()}

        if target_version is not None and not isinstance(target_version, MigrationVersion):
            target_version = MigrationVersion.parse(target_version)

        if steps is not None and steps <= 0:
            raise MigrationExecutionError("steps 必须大于 0")

        plan = MigrationPlan(direction=direction)

        if direction == MigrationDirection.UP:
            # 待应用 = 所有未应用的脚本，按版本升序
            pending = [s for s in scripts if s.version not in applied]
            pending.sort(key=lambda s: s.version)

            if target_version is not None:
                pending = [s for s in pending if s.version <= target_version]

            if steps is not None:
                pending = pending[:steps]

            plan.scripts = pending
        else:
            # 待回滚 = 所有已应用的脚本，按版本降序
            applied_scripts = [s for s in scripts if s.version in applied]
            applied_scripts.sort(key=lambda s: s.version, reverse=True)

            if target_version is not None:
                # 保留 <= target_version 的版本
                applied_scripts = [s for s in applied_scripts if s.version > target_version]

            if steps is not None:
                applied_scripts = applied_scripts[:steps]

            plan.scripts = applied_scripts

        return plan

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def up(
        self,
        target_version: Optional[Union[str, MigrationVersion]] = None,
        steps: Optional[int] = None,
    ) -> List[MigrationResult]:
        """
        执行 up 迁移

        Args:
            target_version: 目标版本
            steps: 步数

        Returns:
            执行结果列表
        """
        plan = self.plan_up(target_version, steps)
        return self._execute_plan(plan)

    def down(
        self,
        target_version: Optional[Union[str, MigrationVersion]] = None,
        steps: Optional[int] = None,
    ) -> List[MigrationResult]:
        """
        执行 down 回滚

        Args:
            target_version: 回滚到该版本之后
            steps: 步数

        Returns:
            执行结果列表
        """
        plan = self.plan_down(target_version, steps)
        return self._execute_plan(plan)

    def redo(
        self,
        steps: int = 1,
    ) -> List[MigrationResult]:
        """
        回滚并重新应用最近 N 个迁移

        Args:
            steps: 重新执行的步数

        Returns:
            执行结果列表 (down + up)
        """
        results: List[MigrationResult] = []
        down_results = self.down(steps=steps)
        results.extend(down_results)

        # 如果有任何 down 失败，停止
        if any(r.status == MigrationStatus.FAILED for r in down_results):
            return results

        up_results = self.up(steps=steps)
        results.extend(up_results)
        return results

    def validate(self) -> List[str]:
        """
        校验迁移完整性

        Returns:
            错误信息列表，空列表表示全部通过
        """
        scripts = self.parser.parse_directory()
        expected = {s.version: s.checksum for s in scripts}
        errors = self.storage.validate_checksums(expected)

        # 检查缺失的已应用迁移（已在 validate_checksums 中处理）
        # 额外检查：数据库中有失败的迁移
        failed = self.storage.get_failed_migrations()
        for f in failed:
            errors.append(
                f"版本 {f.version} 标记为失败状态，需要人工介入处理"
            )

        return errors

    # ------------------------------------------------------------------
    # 内部执行逻辑
    # ------------------------------------------------------------------

    def _execute_plan(self, plan: MigrationPlan) -> List[MigrationResult]:
        results: List[MigrationResult] = []

        # 执行前先校验
        if plan.direction == MigrationDirection.UP:
            errors = self.validate()
            if errors:
                raise MigrationValidationError(
                    "迁移完整性校验失败:\n" + "\n".join(errors)
                )

        for script in plan.scripts:
            result = self._execute_single(script, plan.direction)
            results.append(result)

            if result.status == MigrationStatus.FAILED:
                # 失败时停止后续执行
                break

        return results

    def _execute_single(
        self,
        script: MigrationScript,
        direction: MigrationDirection,
    ) -> MigrationResult:
        start_time = time.perf_counter()
        result = MigrationResult(
            version=script.version,
            name=script.filename,
            direction=direction,
            status=MigrationStatus.RUNNING,
        )

        if self.on_migration_start:
            self.on_migration_start(script, direction)

        sql_statements = script.up_sql if direction == MigrationDirection.UP else script.down_sql

        # 如果是 down 且没有提供 down 语句
        if direction == MigrationDirection.DOWN and not sql_statements:
            result.status = MigrationStatus.FAILED
            result.error = f"迁移 {script.version} 未提供 down 脚本，无法回滚"
            if self.on_migration_failure:
                self.on_migration_failure(result)
            return result

        try:
            executed_count = 0

            if self.dry_run:
                # Dry-run 模式：只打印 SQL 不执行
                for sql in sql_statements:
                    if self.on_statement:
                        self.on_statement(sql, direction)
                    executed_count += 1
            else:
                use_tx = script.use_transaction and not self.single_transaction
                executed_count = self._run_statements(
                    sql_statements,
                    script,
                    direction,
                    use_transaction=use_tx,
                )

            # 成功，更新状态表
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            result.execution_time_ms = elapsed_ms
            result.statements_executed = executed_count

            if not self.dry_run:
                if direction == MigrationDirection.UP:
                    self.storage.record_migration(
                        version=script.version,
                        name=script.filename,
                        checksum=script.checksum,
                        execution_time_ms=elapsed_ms,
                        success=True,
                    )
                else:
                    self.storage.delete_migration(script.version)

                # 追加历史记录
                try:
                    self.storage.record_history(
                        version=script.version,
                        name=script.filename,
                        direction=direction.value,
                        status="success",
                        execution_time_ms=elapsed_ms,
                        checksum=script.checksum,
                        source=self.history_source,
                    )
                except Exception:
                    pass

            result.status = MigrationStatus.SUCCESS
            if self.on_migration_success:
                self.on_migration_success(result)

        except Exception as e:
            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            result.execution_time_ms = elapsed_ms
            result.status = MigrationStatus.FAILED
            result.error = str(e)

            if not self.dry_run and direction == MigrationDirection.UP:
                # 标记为失败（即使事务回滚，也保留失败记录便于排查）
                try:
                    self.storage.record_migration(
                        version=script.version,
                        name=script.filename,
                        checksum=script.checksum,
                        execution_time_ms=elapsed_ms,
                        success=False,
                    )
                except Exception:
                    pass

                # 追加失败历史
                try:
                    self.storage.record_history(
                        version=script.version,
                        name=script.filename,
                        direction=direction.value,
                        status="failed",
                        execution_time_ms=elapsed_ms,
                        checksum=script.checksum,
                        source=self.history_source,
                    )
                except Exception:
                    pass

            if not self.dry_run and direction == MigrationDirection.DOWN:
                # 回滚失败也记一条历史
                try:
                    self.storage.record_history(
                        version=script.version,
                        name=script.filename,
                        direction=direction.value,
                        status="failed",
                        execution_time_ms=elapsed_ms,
                        checksum=script.checksum,
                        source=self.history_source,
                    )
                except Exception:
                    pass

            if self.on_migration_failure:
                self.on_migration_failure(result)

        return result

    def _run_statements(
        self,
        statements: List[str],
        script: MigrationScript,
        direction: MigrationDirection,
        use_transaction: bool,
    ) -> int:
        """
        执行 SQL 语句列表

        Args:
            statements: SQL 语句列表
            script: 所属迁移脚本
            direction: 执行方向
            use_transaction: 是否使用事务包裹

        Returns:
            执行的语句数量

        Raises:
            MigrationExecutionError: 执行失败
        """
        cursor = self.conn.cursor()
        executed = 0
        try:
            if use_transaction:
                # 某些数据库不支持 DDL 事务（如 MySQL），这里统一尝试
                # 失败时依赖上层捕获并标记
                pass

            for sql in statements:
                sql_stripped = sql.strip()
                if not sql_stripped:
                    continue

                if self.on_statement:
                    self.on_statement(sql_stripped, direction)

                try:
                    cursor.execute(sql_stripped)
                    executed += 1
                except Exception as e:
                    # 发生错误时，如果支持事务则整体回滚
                    if use_transaction:
                        self.conn.rollback()
                    raise MigrationExecutionError(
                        f"执行迁移 {script.version} 失败\n"
                        f"SQL 语句: {sql_stripped[:200]}\n"
                        f"错误: {e}"
                    ) from e

            # 全部成功后提交
            if use_transaction:
                self.conn.commit()

        finally:
            cursor.close()

        return executed

    # ------------------------------------------------------------------
    # 查询状态
    # ------------------------------------------------------------------

    def status(self) -> Dict:
        """
        获取当前迁移状态概览

        Returns:
            状态字典
        """
        scripts = self.parser.parse_directory()
        applied_list = self.storage.get_applied_migrations()
        all_applied_map = {m.version: m for m in applied_list}
        success_applied = [m for m in applied_list if m.success]
        failed = [m for m in applied_list if not m.success]
        success_applied_map = {m.version: m for m in success_applied}

        pending_versions = [s.version for s in scripts if s.version not in all_applied_map]

        return {
            "total_scripts": len(scripts),
            "applied_count": len(success_applied),
            "pending_count": len(pending_versions),
            "failed_count": len(failed),
            "latest_version": max(success_applied_map.keys()) if success_applied_map else None,
            "success_applied": success_applied,
            "failed": failed,
            "pending_versions": pending_versions,
        }
