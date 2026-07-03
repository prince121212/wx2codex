#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLOUD_DIR="$ROOT_DIR/cloud"
DB_NAME="${WX2CODEX_D1_NAME:-wx2codex-db}"
WRANGLER="${WRANGLER:-npx --yes wrangler}"

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  echo "请先设置 CLOUDFLARE_API_TOKEN" >&2
  exit 1
fi

cd "$CLOUD_DIR"

if [[ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
  CLOUDFLARE_ACCOUNT_ID="$(python3 - <<'PY'
import json, os, urllib.request
req = urllib.request.Request('https://api.cloudflare.com/client/v4/accounts', headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN']})
data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
print((data.get('result') or [{}])[0].get('id', ''))
PY
)"
  export CLOUDFLARE_ACCOUNT_ID
fi

echo "Cloudflare account: ...${CLOUDFLARE_ACCOUNT_ID: -8}"

DB_ID="$(python3 - <<'PY'
import json, os, urllib.request
account = os.environ['CLOUDFLARE_ACCOUNT_ID']
token = os.environ['CLOUDFLARE_API_TOKEN']
name = os.environ.get('WX2CODEX_D1_NAME', 'wx2codex-db')
req = urllib.request.Request(f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database', headers={'Authorization': 'Bearer ' + token})
data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
for item in data.get('result') or []:
    if item.get('name') == name:
        print(item.get('uuid') or item.get('id') or '')
        break
PY
)"

if [[ -z "$DB_ID" ]]; then
  echo "创建 D1 数据库：$DB_NAME"
  DB_ID="$(python3 - <<'PY'
import json, os, urllib.request
account = os.environ['CLOUDFLARE_ACCOUNT_ID']
token = os.environ['CLOUDFLARE_API_TOKEN']
name = os.environ.get('WX2CODEX_D1_NAME', 'wx2codex-db')
body = json.dumps({'name': name}).encode()
req = urllib.request.Request(f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database', data=body, method='POST', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
if not data.get('success'):
    raise SystemExit(data)
res = data.get('result') or {}
print(res.get('uuid') or res.get('id') or '')
PY
)"
else
  echo "复用 D1 数据库：$DB_NAME (...${DB_ID: -8})"
fi

export DB_ID
python3 - <<'PY'
from pathlib import Path
import os, re
p = Path('wrangler.toml')
text = p.read_text()
db_id = os.environ['DB_ID'] if 'DB_ID' in os.environ else ''
if not db_id:
    raise SystemExit('DB_ID missing')
text = re.sub(r'database_id = "[^"]+"', f'database_id = "{db_id}"', text)
p.write_text(text)
PY

# shellcheck disable=SC2086
$WRANGLER d1 execute "$DB_NAME" --remote --file=./migrations/0001_init.sql

WORKER_EXISTS="$(python3 - <<'PY'
import json, os, urllib.request, urllib.error
account = os.environ['CLOUDFLARE_ACCOUNT_ID']
token = os.environ['CLOUDFLARE_API_TOKEN']
try:
    req = urllib.request.Request(f'https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/wx2codex-cloud', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=30):
        print('1')
except urllib.error.HTTPError as e:
    print('0' if e.code == 404 else 'unknown')
PY
)"

if [[ -n "${WX2CODEX_CIPHER_KEY:-}" ]]; then
  printf '%s' "$WX2CODEX_CIPHER_KEY" | $WRANGLER secret put WX2CODEX_CIPHER_KEY
elif [[ "$WORKER_EXISTS" == "1" && "${WX2CODEX_ROTATE_CIPHER_KEY:-}" != "1" ]]; then
  echo "检测到 wx2codex-cloud 已存在：跳过 WX2CODEX_CIPHER_KEY 设置，避免覆盖旧加密密钥。"
  echo "如确需轮换密钥，请设置 WX2CODEX_ROTATE_CIPHER_KEY=1 和 WX2CODEX_CIPHER_KEY。"
else
  GENERATED_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  printf '%s' "$GENERATED_KEY" | $WRANGLER secret put WX2CODEX_CIPHER_KEY
  echo "已自动生成并设置 WX2CODEX_CIPHER_KEY。请不要丢失生产密钥；重新设置会导致旧加密 token 无法解密。"
fi

# shellcheck disable=SC2086
$WRANGLER deploy
