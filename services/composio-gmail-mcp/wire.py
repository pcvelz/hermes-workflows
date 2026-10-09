#!/usr/bin/env python3
"""Wire a read-only Composio Gmail MCP server into a Hermes profile.

Adds an `mcp_servers.composio_gmail` block to <profile>/config.yaml and the API
key to <profile>/.env. Run it by hand; it is not a launchd job. Use the Hermes
venv python so ruamel / PyYAML resolve:

    <hermes-home>/hermes-agent/venv/bin/python3 services/composio-gmail-mcp/wire.py --profile <name>

Account-bound values come from key files, never from the command line:
  host       keys/composio-gmail-mcp/host       (the Composio MCP host)
  server-id  keys/composio-gmail-mcp/server-id  (the MCP server id)
  user-id    keys/composio-gmail-mcp/user-id    (the Gmail connected account)
  api-key    keys/composio-gmail-mcp/api-key    (the Composio project key)
Each can be redirected with KEY_HOST, KEY_SERVER_ID, KEY_USER_ID, KEY_API_KEY.

The endpoint is https://<host>/v3/mcp/<server-id>?user_id=<user-id>. Composio
authenticates the caller with an `x-api-key` header, not Authorization/Bearer.

Read-only is enforced fail-closed at two layers:
  1. the Composio MCP server is created with the 7 read-only tools as its
     allowlist (set at create time, outside this repo), and
  2. tools.include below, so Hermes exposes only those 7.

The key is written to <profile>/.env as MCP_COMPOSIO_GMAIL_API_KEY; config.yaml
references it as ${MCP_COMPOSIO_GMAIL_API_KEY}. An existing value in .env is
left as-is: remove that line to rotate.

Verify afterwards:  hermes -p <profile> mcp test composio_gmail
Restart required: config is cached by mtime/size, so start a NEW session.
"""
import argparse
import datetime
import os
import pathlib
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "_shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from keyfile import KeyFileError, read_key_env  # noqa: E402

READ_ONLY_TOOLS = [
    'GMAIL_FETCH_EMAILS',
    'GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID',
    'GMAIL_FETCH_MESSAGE_BY_THREAD_ID',
    'GMAIL_LIST_THREADS',
    'GMAIL_LIST_LABELS',
    'GMAIL_GET_PROFILE',
    'GMAIL_GET_ATTACHMENT',
]
ENV_VAR = 'MCP_COMPOSIO_GMAIL_API_KEY'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True, help='Hermes profile name to wire')
    ap.add_argument('--home', default=os.environ.get('HERMES_HOME', '').strip() or '~/.hermes',
                    help='Hermes home root (default: $HERMES_HOME or ~/.hermes)')
    args = ap.parse_args()

    try:
        host = read_key_env('KEY_HOST', 'keys/composio-gmail-mcp/host')
        server_id = read_key_env('KEY_SERVER_ID', 'keys/composio-gmail-mcp/server-id')
        user_id = read_key_env('KEY_USER_ID', 'keys/composio-gmail-mcp/user-id')
        api_key = read_key_env('KEY_API_KEY', 'keys/composio-gmail-mcp/api-key')
    except KeyFileError as exc:
        print(f'wire: {exc}', file=sys.stderr)
        return 2
    url = f'https://{host}/v3/mcp/{server_id}?user_id={user_id}'

    home = pathlib.Path(os.path.expanduser(args.home)) / 'profiles' / args.profile
    env = home / '.env'
    cfg = home / 'config.yaml'

    envtext = env.read_text() if env.exists() else ''
    if ENV_VAR not in envtext:
        with open(env, 'a') as f:
            if envtext and not envtext.endswith('\n'):
                f.write('\n')
            f.write(f'{ENV_VAR}={api_key}\n')
        print('env: key appended')
    else:
        print('env: key already present (left as-is)')

    shutil.copy2(cfg, f'{cfg}.bak-' + datetime.datetime.fromtimestamp(cfg.stat().st_mtime).strftime('%Y%m%d_%H%M%S'))
    block = {
        'url': url,
        'headers': {'x-api-key': '${' + ENV_VAR + '}'},
        'timeout': 180,
        'connect_timeout': 60,
        'enabled': True,
        'tools': {'include': READ_ONLY_TOOLS},
    }
    try:
        from ruamel.yaml import YAML
        y = YAML()
        y.preserve_quotes = True
        with open(cfg) as f:
            c = y.load(f) or {}
        c.setdefault('mcp_servers', {})['composio_gmail'] = block
        with open(cfg, 'w') as f:
            y.dump(c, f)
        print('config: composio_gmail written (ruamel, comments preserved)')
    except Exception as e:
        import yaml
        with open(cfg) as f:
            c = yaml.safe_load(f) or {}
        c.setdefault('mcp_servers', {})['composio_gmail'] = block
        with open(cfg, 'w') as f:
            yaml.safe_dump(c, f, sort_keys=False)
        print(f'config: composio_gmail written (pyyaml; ruamel unavailable: {type(e).__name__})')

    print(f'\nNext: hermes -p {args.profile} mcp test composio_gmail   (expect connect + 7 tools)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
