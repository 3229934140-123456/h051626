"""
迁移配置模块
============

支持 migrator.toml 配置文件，命令行参数与环境变量可覆盖。

优先级: CLI 参数 > 环境变量 > 配置文件 > 默认值
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ------------------------------------------------------------------
# TOML 解析: 优先 tomllib (Python 3.11+), 其次 tomli, 最后最小化实现
# ------------------------------------------------------------------

def _parse_toml(content: str) -> Dict[str, Any]:
    """
    最小化 TOML 解析器，支持:
      - key = "value"  (字符串)
      - key = 123      (整数)
      - key = true/false (布尔)
      - [section]
    不支持数组、嵌套表、多行字符串等复杂语法。
    对于真实项目, 推荐使用 tomllib / tomli。
    """
    result: Dict[str, Any] = {}
    current: Dict[str, Any] = result

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current = result.setdefault(section, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            current[key] = _parse_toml_value(value)

    return result


def _parse_toml_value(value: str) -> Any:
    # 字符串
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    # 布尔
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    # 整数
    try:
        return int(value)
    except ValueError:
        pass
    # 浮点数
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _load_toml_file(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            # Python 3.11+ 内置 tomllib
            import tomllib  # type: ignore
            return tomllib.load(f)
    except ImportError:
        pass
    try:
        with open(path, "rb") as f:
            import tomli  # type: ignore
            return tomli.load(f)
    except ImportError:
        pass
    # 回退到最小实现
    with open(path, "r", encoding="utf-8") as f:
        return _parse_toml(f.read())


# ------------------------------------------------------------------
# 配置数据类
# ------------------------------------------------------------------

@dataclass
class MigratorConfig:
    """
    迁移配置

    Attributes:
        db_url: 数据库 URL (sqlite:///path, postgresql://..., mysql://...)
        migrations_dir: 迁移脚本目录
        lock_timeout: 锁等待超时秒数
        allow_dirty: 是否允许脏库继续迁移 (存在失败记录时仍允许 up)
        config_path: 实际加载的配置文件路径 (None 表示未使用)
    """

    db_url: str = "sqlite:///./app.db"
    migrations_dir: str = "migrations"
    lock_timeout: int = 30
    allow_dirty: bool = False
    config_path: Optional[str] = None

    # 预留扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# 配置加载逻辑
# ------------------------------------------------------------------

# 候选配置文件 (按优先级从高到低)
CONFIG_CANDIDATES = [
    "migrator.toml",
    ".migrator.toml",
]

ENV_MAPPING = {
    "DATABASE_URL": "db_url",
    "MIGRATIONS_DIR": "migrations_dir",
    "MIGRATOR_LOCK_TIMEOUT": "lock_timeout",
    "MIGRATOR_ALLOW_DIRTY": "allow_dirty",
    "MIGRATOR_CONFIG": "config_path",
}


def _apply_env(config: MigratorConfig) -> None:
    """用环境变量覆盖配置"""
    for env_key, cfg_key in ENV_MAPPING.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if cfg_key == "lock_timeout":
            try:
                config.lock_timeout = int(val)
            except ValueError:
                pass
        elif cfg_key == "allow_dirty":
            config.allow_dirty = val.lower() in ("1", "true", "yes", "on")
        else:
            setattr(config, cfg_key, val)


def _apply_file(config: MigratorConfig, file_cfg: Dict[str, Any]) -> None:
    """
    将文件配置应用到 config 对象。

    支持两种布局:
    1. 扁平布局:  db_url = "..."
    2. [migrator] section:
         [migrator]
         db_url = "..."
    """
    section = file_cfg.get("migrator", file_cfg)
    for key, value in section.items():
        if key == "lock_timeout" and isinstance(value, int):
            config.lock_timeout = value
        elif key == "allow_dirty" and isinstance(value, bool):
            config.allow_dirty = value
        elif key in ("db_url", "migrations_dir") and isinstance(value, str):
            setattr(config, key, value)
        else:
            config.extra[key] = value


def load_config(
    config_path: Optional[str] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
) -> MigratorConfig:
    """
    加载并合并配置。

    优先级: CLI > 环境变量 > 配置文件 > 默认值
    """
    config = MigratorConfig()

    # 1. 配置文件
    if config_path is None:
        config_path = os.environ.get("MIGRATOR_CONFIG")
    if config_path:
        file_cfg = _load_toml_file(config_path)
        if file_cfg is not None:
            config.config_path = config_path
            _apply_file(config, file_cfg)
    else:
        for candidate in CONFIG_CANDIDATES:
            file_cfg = _load_toml_file(candidate)
            if file_cfg is not None:
                config.config_path = candidate
                _apply_file(config, file_cfg)
                break

    # 2. 环境变量覆盖
    _apply_env(config)

    # 3. CLI 覆盖
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)

    return config
