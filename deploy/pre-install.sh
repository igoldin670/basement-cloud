#!/bin/sh
set -eu
# Runs after the official image copied its Redis/APCu/proxy configuration.
cp /run/cloud-config/basement.config.php /var/www/html/config/basement.config.php
chown www-data:www-data /var/www/html/config/basement.config.php
chmod 640 /var/www/html/config/basement.config.php
