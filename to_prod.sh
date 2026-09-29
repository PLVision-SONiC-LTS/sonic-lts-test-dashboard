#!/bin/bash

# set -x

PROD_HOST=dashboard.lts.ow.s
PROD_DIR=/opt/dashboard/
USER=anton.putrya
# Paths to ignore during rsync (one per line)
IGNORE_PATHS=(
    .git          # Git repository directory
    .gitignore    # Git ignore file
    .env          # Environment variables file (DO NOT OVERWRITE IN PRODUCTION)
    admins.json   # Local admins file (DO NOT OVERWRITE IN PRODUCTION)
)


IGNORE_ARGS=""
for path in ${IGNORE_PATHS[@]}; do
    echo "> Adding ignore: $path"
    IGNORE_ARGS+=" --exclude=$path"
done

echo "Syncing to production..."
rsync -avz --delete $IGNORE_ARGS . $USER@$PROD_HOST:$PROD_DIR
