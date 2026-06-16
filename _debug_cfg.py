from migrator.config import _parse_toml, load_config
import tempfile, os
content = """
[migrator]
db_url = "postgresql://localhost/test"
migrations_dir = "db/migrations"
lock_timeout = 60
allow_dirty = true
"""
print("parsed:", _parse_toml(content))

tmp = tempfile.mkdtemp()
p = os.path.join(tmp, 'x.toml')
open(p,'w',encoding='utf-8').write(content)
cfg = load_config(config_path=p)
print("cfg:", cfg)
