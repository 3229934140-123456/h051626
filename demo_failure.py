"""演示失败迁移后 CLI 的友好输出"""
from __future__ import annotations

import os
import sqlite3
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrator.cli import MigratorCLI

tmpdir = tempfile.mkdtemp(prefix="migrator_fail_demo_")
db_path = os.path.join(tmpdir, "demo.db")
url = f"sqlite:///{db_path}"
print(f"临时库: {db_path}\n")

os.environ["DATABASE_URL"] = url

try:
    cli = MigratorCLI()
    cli.db_url = url

    print(">>> 步骤 1: init")
    assert cli.run(["init"]) == 0

    print("\n>>> 步骤 2: 先 up 2 个迁移")
    assert cli.run(["up", "--steps", "2"]) == 0

    # 人为制造一条失败记录
    print("\n>>> 步骤 3: 模拟第 3 个迁移执行失败（手动插入 success=0）")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 0)",
        ("20240103000001", "20240103000001_add_user_profile.sql", "deadbeef" * 8, 999),
    )
    conn.commit()
    conn.close()

    print("\n>>> 步骤 4: 此时尝试再 up —— 应该看到失败横幅而不是堆栈")
    rc = cli.run(["up"])
    print(f"    返回码: {rc}")

    print("\n>>> 步骤 5: status 应该高亮失败记录")
    assert cli.run(["status"]) == 0

    print("\n>>> 步骤 6: validate 应该分类给出处理建议")
    rc = cli.run(["validate"])
    print(f"    返回码: {rc}")

    print("\n>>> 步骤 7: redo 也应该被阻止")
    rc = cli.run(["redo"])
    print(f"    返回码: {rc}")

finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
