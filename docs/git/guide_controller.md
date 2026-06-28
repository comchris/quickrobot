

## Controller Host:

Debian Trixie:

sudo apt install ansible-core python3-flask python3-jinja2 python3-markupsafe python3-yaml python3-psutil python3-requests python3-requests-ntlm python3-flask-cors python3-flask-socketio

sudo apt install git tmux jq curl nano  

optionally for MCP: apt install pipx && pipx install mcp    # 1.27.2

git pull https://github.com/comchris/quickrobot && cd quickrobot 

nano .quickrobot.env

# Start the API server:
python3 ./quickrobot.py

# Available flags (run `python3 quickrobot.py --help` for full list):
#   --mode {dev,prod,dev-update,exit}   Operation mode (default: prod)
#   --port PORT                         API server port override
#   --db-path PATH                      Custom DB file path
#   --host HOST                         Bind address (default: 127.0.0.1)
#   --no-webui                          Disable WebUI auto-start
#   --replace                           Kill existing instance on same port
#   --init                              (deprecated, no-op — DB created automatically)
#   --webui-detach                      (deprecated — use QUICKROBOT_WEBUI_AUTOSTART in .env)
