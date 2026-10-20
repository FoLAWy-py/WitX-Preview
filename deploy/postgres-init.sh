#!/bin/sh
# Runs once, when the PostgreSQL volume is first created. Two login roles: the owner
# (runs migrations; table owners bypass row-level security) and the application role,
# which only gets privileges through witx_app (granted by the migrations).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" \
  -v owner_password="$WITX_OWNER_PASSWORD" -v app_password="$WITX_APP_PASSWORD" <<'SQL'
CREATE ROLE witx_owner LOGIN CREATEROLE PASSWORD :'owner_password';
CREATE ROLE witx_app NOLOGIN;
CREATE ROLE witx_app_login LOGIN PASSWORD :'app_password' IN ROLE witx_app;
CREATE DATABASE witx OWNER witx_owner;
SQL
