#!/bin/sh
# at-ai-xhs 容器入口：先把"每次启动都该补一遍"的事做完，再把进程交给后端。
#
# 为什么建表放在启动时而不是只在构建时做：
#   * `db/*.db` 被 .dockerignore 排除了，镜像里本来就没有现成的库文件；
#   * 而 `db/` 又最常被挂成卷（数据库要持久化）——卷会把镜像里那份 database.db
#     连 createTable.py 一起遮住，所以脚本在构建时另存了一份到 /app/scripts/；
#   * 脚本里全是 `CREATE TABLE IF NOT EXISTS`，重复执行没有副作用（幂等）。
set -e

if [ -f /app/scripts/createTable.py ]; then
    mkdir -p /app/db
    # createTable.py 的路径写死成 './database.db'（相对当前目录），必须 cd 到 db/ 再跑
    if ! ( cd /app/db && python /app/scripts/createTable.py ); then
        echo "❌ 建表失败：/app/db 可能是只读挂载或权限不对（容器里以 root 运行，bind mount 到 Linux 宿主时注意 UID）" >&2
        exit 1
    fi
fi

# 运行目录：挂成空卷时正好在这里补上（日志、素材、背景音乐、账号 cookie、视频号 profile 都落在这些目录）
mkdir -p /app/videoFile /app/bgmFile /app/cookiesFile /app/cookies /app/logs

# exec：让 python 成为 PID 1，`docker stop` 的信号才能直接送到它手上。
# 否则信号只到 shell，正在跑的浏览器任务收不到中断，容易留下野 chromium 进程。
cd /app
exec "$@"
