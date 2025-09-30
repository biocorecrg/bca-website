#!/bin/bash
set -e

if [ -d "/pg_backup_dir" ] && [ "$(psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc 'SELECT 1 FROM pg_tables WHERE schemaname = '\''public'\'' LIMIT 1')" != "1" ]; then
    echo "Restoring database from directory backup..."
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --create /pg_backup_dir
else
    echo "No restore needed or backup directory missing."
fi
