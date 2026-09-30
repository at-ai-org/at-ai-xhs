import sqlite3
import json
import os

# 数据库文件路径（如果不存在会自动创建）
db_file = './database.db'

# 如果数据库已存在，则删除旧的表（可选）
# if os.path.exists(db_file):
#     os.remove(db_file)

# 连接到SQLite数据库（如果文件不存在则会自动创建）
conn = sqlite3.connect(db_file)
cursor = conn.cursor()

# 创建账号记录表
cursor.execute('''
CREATE TABLE IF NOT EXISTS user_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type INTEGER NOT NULL,
    filePath TEXT NOT NULL,  -- 存储文件路径
    userName TEXT NOT NULL,
    status INTEGER DEFAULT 0
)
''')

# 创建文件记录表
cursor.execute('''CREATE TABLE IF NOT EXISTS file_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 唯一标识每条记录
    filename TEXT NOT NULL,               -- 文件名
    filesize REAL,                     -- 文件大小（单位：MB）
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP, -- 上传时间，默认当前时间
    file_path TEXT                        -- 文件路径
)
''')

# 创建发布记录表（at-ai-xhs 改造新增）
# 注意：后端启动时（sau_backend_publish.py 的 _ensure_tables）也会建这张表，
# 老库不用重跑本脚本。这里补上是为了新建库时结构一眼可见、和实际一致。
cursor.execute('''CREATE TABLE IF NOT EXISTS publish_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT,      -- 一次批量提交对应一个批次（工作台单条发布为空）
    task_id        TEXT,      -- 一个「素材 × 账号」任务
    source         TEXT,      -- publish_center / xhs_workbench
    platform       INTEGER,   -- 1 小红书 2 视频号 3 抖音 4 快手
    platform_name  TEXT,
    account_id     INTEGER,
    account_name   TEXT,
    account_file   TEXT,
    material       TEXT,      -- 素材磁盘文件名
    material_title TEXT,      -- 素材展示名
    title          TEXT,
    desc           TEXT,
    tags           TEXT,      -- JSON 数组
    status         TEXT,      -- queued/running/success/failed/cancelled/interrupted
    stage          TEXT,
    stage_text     TEXT,
    action         TEXT,      -- 最后一次「当前动作」
    message        TEXT,      -- 结果说明 / 失败原因
    scheduled      INTEGER DEFAULT 0,
    schedule_at    TEXT,
    is_draft       INTEGER DEFAULT 0,
    is_original    INTEGER DEFAULT 0,
    created_at     TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    duration       REAL,
    steps          TEXT,      -- JSON：小红书页面操作层逐步结果
    log            TEXT       -- JSON：日志尾巴
)
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_publish_records_created ON publish_records(created_at DESC)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_publish_records_platform ON publish_records(platform, account_name)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_publish_records_status ON publish_records(status)')

# 背景音乐库（at-ai-xhs 改造新增）
# 音乐文件本身放 bgmFile/，这张表只记元数据。合成后拼进素材文件名的是 name 字段。
# 后端启动时（sau_backend.py 的 ensure_music_tables）也会建，老库不用重跑本脚本。
cursor.execute('''CREATE TABLE IF NOT EXISTS background_music (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,               -- 展示名（不含扩展名）
    file_name   TEXT NOT NULL,               -- bgmFile/ 下的磁盘文件名
    filesize    REAL,                        -- MB
    duration    REAL,                        -- 秒
    upload_time DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')

# 素材的「背景音乐」状态（at-ai-xhs 改造新增）
# 主键是素材磁盘文件名；素材改名（合成后必然改名）时同步改 key。
# **没有行 = 待合成**，所以不用为每条素材预插一行。
cursor.execute('''CREATE TABLE IF NOT EXISTS material_bgm (
    file_path  TEXT PRIMARY KEY,
    music_id   INTEGER,
    music_name TEXT,
    status     TEXT,                         -- running / done / failed
    progress   INTEGER DEFAULT 0,
    message    TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')


# 提交更改
conn.commit()
print("✅ 表创建成功")
# 关闭连接
conn.close()