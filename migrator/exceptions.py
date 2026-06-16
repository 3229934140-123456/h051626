"""
迁移工具异常定义
"""


class MigrationError(Exception):
    """迁移基础异常"""
    pass


class MigrationVersionError(MigrationError):
    """版本相关异常"""
    pass


class MigrationParseError(MigrationError):
    """脚本解析异常"""
    pass


class MigrationStorageError(MigrationError):
    """状态存储异常"""
    pass


class MigrationExecutionError(MigrationError):
    """执行异常"""
    pass


class MigrationValidationError(MigrationError):
    """校验异常"""
    pass


class MigrationLockError(MigrationError):
    """锁相关异常"""
    pass


class MigrationDiffError(MigrationError):
    """Schema 对比异常"""
    pass
