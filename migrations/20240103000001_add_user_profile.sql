-- migrate: description=给用户表添加个人资料字段
-- migrate: author=dev
-- migrate: transaction=true

-- +migrate Up
ALTER TABLE users ADD COLUMN display_name VARCHAR(100);
ALTER TABLE users ADD COLUMN bio TEXT;
ALTER TABLE users ADD COLUMN avatar_url VARCHAR(500);

-- +migrate Down
-- SQLite 不支持 DROP COLUMN，这里使用注释标记需人工处理
-- 如需真正回滚，需创建新表、拷贝数据、删除旧表、重命名
-- 简化处理：此处仅作为示例，实际生产需谨慎
ALTER TABLE users RENAME COLUMN avatar_url TO _deprecated_avatar_url;
ALTER TABLE users RENAME COLUMN bio TO _deprecated_bio;
ALTER TABLE users RENAME COLUMN display_name TO _deprecated_display_name;
