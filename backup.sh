#!/bin/bash
# Mini App 自动备份 - 保留最近 10 个版本
BACKUP_DIR="/home/ubuntu/www/mini-app/backups"
mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
cp /home/ubuntu/www/mini-app/index.html "$BACKUP_DIR/index.html.$TIMESTAMP"
cp /home/ubuntu/www/mini-app/server.py "$BACKUP_DIR/server.py.$TIMESTAMP"
# 保留最近 10 个
ls -t "$BACKUP_DIR"/index.html.* 2>/dev/null | tail -n +11 | xargs -r rm
ls -t "$BACKUP_DIR"/server.py.* 2>/dev/null | tail -n +11 | xargs -r rm
