#!/bin/bash
# One-time: store the Steam password for the farm account, then log in on a
# terminal so Steam Guard can be answered. Leaves a refresh token behind.
set -e
cd /opt/aic
export AIC_ICARUS_ROOT=/opt/umabot AIC_PROFILE=aic AIC_MASTER_MDB=/opt/umabot/master.mdb
[ -n "$STEAM_USER" ] || read -rp "Steam account name: " STEAM_USER
read -rsp "Steam password for $STEAM_USER: " PW; echo
STEAM_USER="$STEAM_USER" PW="$PW" "$HOME/.venvs/aic/bin/python" - <<"EOF"
import os, sys
sys.path.insert(0, "/opt/aic")
from aic.config import load_settings
from aic.auth import load_auth_config, save_auth_config
s = load_settings("aic"); cfg = load_auth_config(s)
cfg["steam_username"] = os.environ["STEAM_USER"]; cfg["steam_password_seed"] = os.environ["PW"]
save_auth_config(s, cfg); print("Password stored (obfuscated). Logging in - answer the Steam Guard prompt:")
EOF
unset PW
./aic.sh login || true
"$HOME/.venvs/aic/bin/python" -c "
import sys; sys.path.insert(0,\"/opt/aic\")
from aic.config import load_settings; from aic.auth import load_auth_config
c=load_auth_config(load_settings(\"aic\")); print(\"STEAM TOKEN STORED\" if c.get(\"steam_refresh_token\") else \"no token yet - Steam login did not complete\")"
