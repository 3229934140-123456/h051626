"""
迁移配置模块
============

支持 migrator.toml / migrator.yaml 配置文件，按环境分组 (dev/staging/prod)，
命令行参数与环境变量可覆盖。

优先级: CLI 参数 > 环境变量 > 配置文件 [env] section > 配置文件顶层 > 默认值
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ------------------------------------------------------------------
# TOML 解析
# ------------------------------------------------------------------

def _parse_toml(content: str) -> Dict[str, Any]:
    """
    最小化 TOML 解析器，支持:
      - key = "value"  (字符串)
      - key = 123      (整数)
      - key = true/false (布尔)
      - [section]
      - [envs.dev]  嵌套 section (单级点号)
    不支持数组、多行字符串等复杂语法。
    """
    result: Dict[str, Any] = {}
    current: Dict[str, Any] = result

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            # 支持 [envs.dev] 这种点号嵌套
            if "." in section:
                parts = section.split(".")
                node = result
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                current = node.setdefault(parts[-1], {})
            else:
                current = result.setdefault(section, {})
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            current[key] = _parse_toml_value(value)

    return result


def _parse_toml_value(value: str) -> Any:
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
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
    with open(path, "r", encoding="utf-8") as f:
        return _parse_toml(f.read())


# ------------------------------------------------------------------
# YAML 解析
# ------------------------------------------------------------------

def _parse_yaml(content: str) -> Dict[str, Any]:
    """
    最小化 YAML 解析器，支持:
      - key: value
      - 2 空格缩进的嵌套 (envs / dev / db_url 这种二级结构)
      - # 注释
    不支持数组、锚点等复杂语法。
    """
    result: Dict[str, Any] = {}
    # 栈: [(indent_level, dict_node)]
    stack: list = [(-1, result)]

    for raw_line in content.splitlines():
        # 去掉注释 (不在字符串内的 #)
        stripped_full = raw_line.rstrip()
        if not stripped_full.strip() or stripped_full.strip().startswith("#"):
            continue

        # 计算缩进
        indent = len(raw_line) - len(raw_line.lstrip(" "))

        # 找对应层级的父节点
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            stack = [(-1, result)]
        parent = stack[-1][1]

        line = stripped_full.strip()
        if ":" not in line:
            continue

        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()

        if val == "":
            # 嵌套对象
            node: Dict[str, Any] = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _parse_toml_value(val)

    return result


def _load_yaml_file(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise ValueError(f"YAML 根必须是对象: {path}")
            return data
    except ImportError:
        pass
    # 回退到最小实现
    with open(path, "r", encoding="utf-8") as f:
        return _parse_yaml(f.read())


def _load_any_file(path: str) -> Optional[Dict[str, Any]]:
    """根据扩展名自动选择 TOML 或 YAML 解析器"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        return _load_yaml_file(path)
    # 默认尝试 TOML
    return _load_toml_file(path)


# ------------------------------------------------------------------
# 配置数据类
# ------------------------------------------------------------------

@dataclass
class MigratorConfig:
    """
    迁移配置

    Attributes:
        env: 当前环境名 (dev/staging/prod 等)
        db_url: 数据库 URL
        migrations_dir: 迁移脚本目录
        lock_timeout: 锁等待超时秒数
        allow_dirty: 是否允许脏库继续迁移
        config_path: 实际加载的配置文件路径
        env_from_file: 是否从配置文件的环境 section 读取了值
    """

    env: str = "default"
    db_url: str = "sqlite:///./app.db"
    migrations_dir: str = "migrations"
    lock_timeout: int = 30
    allow_dirty: bool = False
    config_path: Optional[str] = None
    env_from_file: bool = False

    extra: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# 配置加载逻辑
# ------------------------------------------------------------------

# 候选配置文件 (按优先级从高到低)
CONFIG_CANDIDATES = [
    "migrator.toml",
    ".migrator.toml",
    "migrator.yaml",
    ".migrator.yaml",
    "migrator.yml",
    ".migrator.yml",
]

ENV_MAPPING = {
    "DATABASE_URL": "db_url",
    "MIGRATIONS_DIR": "migrations_dir",
    "MIGRATOR_LOCK_TIMEOUT": "lock_timeout",
    "MIGRATOR_ALLOW_DIRTY": "allow_dirty",
    "MIGRATOR_ENV": "env",
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


def _apply_dict(config: MigratorConfig, section: Dict[str, Any]) -> bool:
    """
    将字典配置应用到 config 对象。

    Returns:
        True 表示本次应用至少设置了一个核心值 (db_url / migrations_dir 等)
    """
    applied_any = False
    for key, value in section.items():
        if key == "lock_timeout" and isinstance(value, int):
            config.lock_timeout = value
            applied_any = True
        elif key == "allow_dirty" and isinstance(value, bool):
            config.allow_dirty = value
            applied_any = True
        elif key == "env" and isinstance(value, str):
            config.env = value
            applied_any = True
        elif key in ("db_url", "migrations_dir") and isinstance(value, str):
            setattr(config, key, value)
            applied_any = True
        else:
            config.extra[key] = value
    return applied_any


def _resolve_config_path(explicit_path: Optional[str]) -> Optional[str]:
    """
    解析配置文件路径。

    - 显式 --config: 只尝试该文件，找不到返回 None（调用方应报错）
    - 未显式指定: 自动从当前目录发现候选文件
    """
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        # 显式传但不存在 - 返回 None，调用方处理
        return None
    # 自动发现
    for candidate in CONFIG_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config(
    config_path: Optional[str] = None,
    cli_overrides: Optional[Dict[str, Any]] = None,
    env_name: Optional[str] = None,
) -> MigratorConfig:
    """
    加载并合并配置。

    优先级（从低到高）：
      1. 默认值
      2. 配置文件顶层 [migrator] 或 migrator 对象
      3. 配置文件 [envs.<env>] section
      4. 环境变量
      5. CLI 参数 (cli_overrides)

    Args:
        config_path: 配置文件路径。显式传入时若文件不存在则抛 FileNotFoundError。
        cli_overrides: CLI 参数覆盖。
        env_name: 环境名 (dev/staging/prod)。若为 None，则依次看 cli_overrides.env >
                  MIGRATOR_ENV > 配置文件顶层 default_env > "default"。

    Raises:
        FileNotFoundError: 显式 config_path 但文件不存在。
    """
    config = MigratorConfig()

    # ---- 1. 先确认 env_name (决定用哪个 envs section) ----
    # 先从环境变量读 env (用于找 section)
    provisional_env = env_name
    if not provisional_env and cli_overrides and "env" in cli_overrides:
        provisional_env = cli_overrides.get("env")  # type: ignore
    if not provisional_env:
        provisional_env = os.environ.get("MIGRATOR_ENV")

    # ---- 2. 加载配置文件 ----
    explicit_path = config_path or os.environ.get("MIGRATOR_CONFIG")
    resolved_path = _resolve_config_path(explicit_path)

    if explicit_path and not resolved_path:
        # 显式指定了文件但不存在
        raise FileNotFoundError(
            f"配置文件不存在: {explicit_path}。"
            "显式传 --config 时只加载指定文件，不会自动搜索默认配置。"
        )

    file_cfg: Dict[str, Any] = {}
    if resolved_path:
        loaded = _load_any_file(resolved_path)
        if loaded is not None:
            file_cfg = loaded
            config.config_path = resolved_path

    # ---- 3. 配置文件顶层 (migrator section 或扁平) ----
    migrator_section = file_cfg.get("migrator", {})
    if isinstance(migrator_section, dict):
        _apply_dict(config, migrator_section)
    # 扁平结构也支持 (顶层直接放 db_url 等)
    _apply_dict(config, file_cfg)

    # 确定最终 env_name: 看顶层 default_env
    if not provisional_env and "default_env" in file_cfg:
        provisional_env = str(file_cfg["default_env"])
    if not provisional_env and isinstance(migrator_section, dict) and "default_env" in migrator_section:
        provisional_env = str(migrator_section["default_env"])

    # ---- 4. 应用 envs.<env> section ----
    envs_map = file_cfg.get("envs", {})
    if not isinstance(envs_map, dict):
        envs_map = {}

    final_env = provisional_env or config.env or "default"
    config.env = final_env

    env_section = envs_map.get(final_env) if isinstance(envs_map, dict) else None
    if isinstance(env_section, dict):
        if _apply_dict(config, env_section):
            config.env_from_file = True

    # ---- 5. 环境变量覆盖 ----
    _apply_env(config)

    # 再次确认 env (环境变量也可能覆盖)
    if not config.env:
        config.env = final_env

    # ---- 6. CLI 覆盖最高 ----
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)

    return config
