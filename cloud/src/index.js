import { AGENT_BUNDLE_BASE64, AGENT_BUNDLE_SHA256, AGENT_BUNDLE_VERSION } from './agent_bundle.js';
const DEFAULT_CONTEXT_TTL_HOURS = 24;
const DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS = 3600;
const DEFAULT_TRIAL_DAYS = 7;
const DEFAULT_ANNUAL_PRICE_CENTS = 990;
const DEFAULT_ANNUAL_DAYS = 365;
const DEFAULT_PAYMENT_EXPIRE_MINUTES = 5;
const DEFAULT_WECHAT_PAY_BASE_URL = 'https://api.mch.weixin.qq.com';
const ILINK_CDN_BASE = 'https://novac2c.cdn.weixin.qq.com/c2c';
const MAX_TEXT_LENGTH = 4096;
const EXPIRED_CODES = new Set([-14, 40014, 1002]);
const EXPIRED_RET_CODES = new Set([-2]);

export default {
  async fetch(request, env) {
    try {
      if (request.method === 'OPTIONS') return cors(new Response(null, { status: 204 }));
      const url = new URL(request.url);
      const path = normalizePath(url.pathname);

      if (request.method === 'GET' && path === '/health') {
        return json({ ok: true, service: 'wx2codex-cloud', time: new Date().toISOString() });
      }

      if (request.method === 'GET' && path === '/install.sh') {
        return installScriptResponse(url.origin);
      }

      if (request.method === 'GET' && path === '/agent/wx2codex-agent.tar.gz') {
        return agentBundleResponse();
      }

      if (request.method === 'GET' && path === '/agent/manifest.json') {
        return json({ ok: true, version: AGENT_BUNDLE_VERSION, sha256: AGENT_BUNDLE_SHA256, archive: `${url.origin}/agent/wx2codex-agent.tar.gz` });
      }

      if (path === '/' && request.method === 'GET') {
        return redirect('/admin');
      }

      if (request.method === 'GET' && path.startsWith('/pay/')) {
        const orderId = decodeURIComponent(path.slice('/pay/'.length));
        return html(await paymentPageHtml(env, url.origin, orderId));
      }

      if (path === '/admin' && request.method === 'GET') {
        const admin = await getAdminSession(request, env);
        if (!admin) return redirect('/admin/login');
        return html(adminDashboardHtml(admin.username));
      }

      if (path === '/admin/login' && request.method === 'GET') {
        const admin = await getAdminSession(request, env);
        if (admin) return redirect('/admin');
        return html(adminLoginHtml());
      }

      if (path === '/admin/login' && request.method === 'POST') {
        return handleAdminLogin(request, env);
      }

      if (path === '/admin/logout') {
        return handleAdminLogout();
      }

      if (path === '/admin/api/overview' && request.method === 'GET') {
        await requireAdmin(request, env);
        return json(await adminOverview(env));
      }

      if (path === '/admin/api/table' && request.method === 'GET') {
        await requireAdmin(request, env);
        return json(await adminTable(env, url.searchParams));
      }

      if (path === '/admin/api/users' && request.method === 'GET') {
        await requireAdmin(request, env);
        return json(await adminUsers(env, url.searchParams));
      }

      if (path === '/admin/api/membership/extend' && request.method === 'POST') {
        await requireAdmin(request, env);
        return json(await adminExtendMembership(request, env));
      }

      if (path === '/admin/api/membership/update' && request.method === 'POST') {
        await requireAdmin(request, env);
        return json(await adminUpdateMembership(request, env));
      }

      if (request.method === 'POST' && path === '/v1/ilink/register') {
        return cors(await handleRegister(request, env));
      }

      if (request.method === 'POST' && path === '/v1/ilink/context') {
        const auth = await requireDevice(request, env);
        return cors(await handleContext(request, env, auth));
      }

      if (request.method === 'POST' && path === '/v1/device/heartbeat') {
        const auth = await requireDevice(request, env);
        return cors(await handleHeartbeat(env, auth));
      }

      if (request.method === 'POST' && path === '/v1/notify/wechat') {
        const auth = await requireDevice(request, env);
        return cors(await handleNotify(request, env, auth));
      }

      if (request.method === 'POST' && path === '/v1/pay/wechat/native') {
        const auth = await requireDevice(request, env);
        return cors(await handleCreateWechatPayNativeOrder(request, env, auth));
      }

      if (request.method === 'POST' && path === '/v1/pay/wechat/notify') {
        return await handleWechatPayNotify(request, env);
      }

      if (request.method === 'GET' && path.startsWith('/v1/pay/orders/') && path.endsWith('/status')) {
        const orderId = decodeURIComponent(path.slice('/v1/pay/orders/'.length, -'/status'.length));
        return cors(json(await handlePaymentOrderStatus(env, orderId)));
      }

      if (request.method === 'GET' && path === '/v1/me') {
        const auth = await requireDevice(request, env);
        return cors(await handleMe(env, auth));
      }

      return cors(json({ ok: false, error: 'not_found' }, 404));
    } catch (error) {
      const status = error.status || 500;
      const message = status >= 500 ? 'internal_error' : error.message;
      return cors(json({ ok: false, error: message }, status));
    }
  }
};

function normalizePath(pathname) {
  if (pathname.length > 1 && pathname.endsWith('/')) return pathname.slice(0, -1);
  return pathname;
}

function cors(response) {
  const headers = new Headers(response.headers);
  headers.set('Access-Control-Allow-Origin', '*');
  headers.set('Access-Control-Allow-Methods', 'GET,POST,OPTIONS');
  headers.set('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }
  });
}

function html(body, status = 200, extraHeaders = {}) {
  return new Response(body, {
    status,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
      ...extraHeaders
    }
  });
}

function installScriptResponse(origin) {
  const script = String.raw`#!/usr/bin/env bash
set -euo pipefail

CLOUD_URL="${origin}"
if printenv WX2CODEX_CLOUD_URL >/dev/null 2>&1; then CLOUD_URL="$(printenv WX2CODEX_CLOUD_URL)"; fi
INSTALL_DIR="$HOME/.wx2codex"
if printenv WX2CODEX_HOME >/dev/null 2>&1; then INSTALL_DIR="$(printenv WX2CODEX_HOME)"; fi
SRC_DIR="$INSTALL_DIR/source"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="$HOME/.local/bin"
if printenv WX2CODEX_BIN_DIR >/dev/null 2>&1; then BIN_DIR="$(printenv WX2CODEX_BIN_DIR)"; fi
ARCHIVE="$INSTALL_DIR/wx2codex-agent.tar.gz"
WX_BIN="$VENV_DIR/bin/wx2codex"
EXPECTED_SHA="${AGENT_BUNDLE_SHA256}"

say() { printf '\033[1;34m[wx2codex]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[wx2codex]\033[0m %s\n' "$*" >&2; }
fail_install() { printf '\033[1;31m[wx2codex]\033[0m %s\n' "$*" >&2; exit 1; }

say "开始安装 wx2codex Agent ${AGENT_BUNDLE_VERSION}"

if [ "$(uname -s)" != "Darwin" ]; then
  warn "当前安装脚本主要面向 macOS；其他系统暂不保证可用。"
fi

command -v curl >/dev/null 2>&1 || fail_install "缺少 curl"
command -v python3 >/dev/null 2>&1 || fail_install "缺少 python3，请先安装 Python 3"
command -v tar >/dev/null 2>&1 || fail_install "缺少 tar"

mkdir -p "$INSTALL_DIR" "$SRC_DIR" "$BIN_DIR"

say "下载 Agent 包：$CLOUD_URL/agent/wx2codex-agent.tar.gz"
curl -fsSL "$CLOUD_URL/agent/wx2codex-agent.tar.gz" -o "$ARCHIVE"

ACTUAL_SHA="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
  fail_install "安装包校验失败：$ACTUAL_SHA != $EXPECTED_SHA"
fi

rm -rf "$SRC_DIR"
mkdir -p "$SRC_DIR"
tar -xzf "$ARCHIVE" -C "$SRC_DIR"

say "创建 Python 虚拟环境"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install "$SRC_DIR"
ln -sf "$WX_BIN" "$BIN_DIR/wx2codex"

say "配置云端：$CLOUD_URL"
if command -v codex >/dev/null 2>&1; then
  "$WX_BIN" configure --cloud-url "$CLOUD_URL" --codex-provider desktop --codex-binary "$(command -v codex)"
else
  "$WX_BIN" configure --cloud-url "$CLOUD_URL" --codex-provider desktop
  warn "没有在 PATH 中找到 codex CLI。默认会连接 Codex 桌面 App；如需回退 app_server，再配置 codex CLI 路径。"
fi

say "命令已安装：$WX_BIN"
if ! printf '%s' "$PATH" | grep -q "$BIN_DIR"; then
  warn "建议把 $BIN_DIR 加入 PATH：export PATH=\"$BIN_DIR:\$PATH\""
fi

SKIP_CONNECT="0"
if printenv WX2CODEX_SKIP_CONNECT >/dev/null 2>&1; then SKIP_CONNECT="$(printenv WX2CODEX_SKIP_CONNECT)"; fi
if [ "$SKIP_CONNECT" = "1" ]; then
  say "已跳过扫码连接。稍后运行：$WX_BIN connect && $WX_BIN install-service"
  exit 0
fi

if [ -r /dev/tty ]; then
  say "接下来会显示微信二维码，请用微信扫码确认。"
  if "$WX_BIN" connect < /dev/tty; then
    say "微信连接完成，安装后台服务。"
    "$WX_BIN" install-service
    say "安装完成。以后可直接在微信里给 Codex 发消息。"
    say "查看状态：$WX_BIN status"
  else
    warn "扫码连接未完成。你可以稍后运行：$WX_BIN connect && $WX_BIN install-service"
  fi
else
  warn "当前环境没有可交互终端，已完成安装但未扫码连接。"
  warn "稍后运行：$WX_BIN connect && $WX_BIN install-service"
fi
`;
  return new Response(script, {
    headers: {
      'Content-Type': 'text/x-shellscript; charset=utf-8',
      'Cache-Control': 'no-store'
    }
  });
}

function agentBundleResponse() {
  const bytes = base64UrlDecode(AGENT_BUNDLE_BASE64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, ''));
  return new Response(bytes, {
    headers: {
      'Content-Type': 'application/gzip',
      'Content-Disposition': 'attachment; filename="wx2codex-agent.tar.gz"',
      'Cache-Control': 'public, max-age=300',
      'X-Agent-Version': AGENT_BUNDLE_VERSION,
      'X-Agent-SHA256': AGENT_BUNDLE_SHA256
    }
  });
}

function redirect(location, status = 302, extraHeaders = {}) {
  return new Response(null, { status, headers: { Location: location, ...extraHeaders } });
}

function fail(status, message) {
  const error = new Error(message);
  error.status = status;
  throw error;
}

async function readJson(request) {
  const raw = await request.text();
  if (!raw.trim()) return {};
  try {
    return JSON.parse(raw);
  } catch (_) {
    fail(400, 'invalid_json');
  }
}

function nowIso() {
  return new Date().toISOString();
}

function addHours(dateIso, hours) {
  const t = Date.parse(dateIso || nowIso());
  if (!Number.isFinite(t)) fail(400, 'invalid_last_inbound_at');
  return new Date(t + hours * 60 * 60 * 1000).toISOString();
}

function addMinutes(dateIso, minutes) {
  const t = Date.parse(dateIso || nowIso());
  if (!Number.isFinite(t)) fail(400, 'invalid_date');
  return new Date(t + minutes * 60 * 1000).toISOString();
}

function addDays(dateIso, days) {
  const t = Date.parse(dateIso || nowIso());
  if (!Number.isFinite(t)) fail(400, 'invalid_date');
  return new Date(t + days * 24 * 60 * 60 * 1000).toISOString();
}

function envNumber(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function requireString(value, field, maxLen = 8192) {
  if (typeof value !== 'string' || !value.trim()) fail(400, `missing_${field}`);
  const result = value.trim();
  if (result.length > maxLen) fail(400, `${field}_too_long`);
  return result;
}

function optionalString(value, maxLen = 8192) {
  if (typeof value !== 'string') return '';
  return value.trim().slice(0, maxLen);
}

async function handleRegister(request, env) {
  const body = await readJson(request);
  const ilinkUserId = requireString(body.ilink_user_id, 'ilink_user_id', 256);
  const botToken = requireString(body.bot_token, 'bot_token', 2048);
  const botId = optionalString(body.bot_id, 256);
  const deviceId = optionalString(body.device_id, 256) || crypto.randomUUID();
  const deviceName = optionalString(body.device_name, 256);
  const toUserId = optionalString(body.to_user_id, 256);
  const now = nowIso();

  let user = await env.DB.prepare('SELECT * FROM users WHERE ilink_user_id = ?').bind(ilinkUserId).first();
  if (!user) {
    user = { id: `usr_${crypto.randomUUID()}`, ilink_user_id: ilinkUserId };
    await env.DB.prepare(
      'INSERT INTO users (id, ilink_user_id, default_to_user_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)'
    ).bind(user.id, ilinkUserId, toUserId || null, 'active', now, now).run();
  } else {
    await env.DB.prepare('UPDATE users SET updated_at = ?, default_to_user_id = COALESCE(NULLIF(?, \'\'), default_to_user_id) WHERE id = ?')
      .bind(now, toUserId, user.id).run();
  }

  const agentToken = makeToken('wxa');
  const agentTokenHash = await sha256Hex(agentToken);
  const existingDevice = await env.DB.prepare('SELECT * FROM devices WHERE user_id = ? AND device_id = ?')
    .bind(user.id, deviceId).first();
  if (existingDevice) {
    await env.DB.prepare(
      'UPDATE devices SET device_name = ?, agent_token_hash = ?, status = ?, updated_at = ?, last_seen_at = ? WHERE id = ?'
    ).bind(deviceName, agentTokenHash, 'active', now, now, existingDevice.id).run();
  } else {
    await env.DB.prepare(
      'INSERT INTO devices (id, user_id, device_id, device_name, agent_token_hash, status, created_at, updated_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
    ).bind(`dev_${crypto.randomUUID()}`, user.id, deviceId, deviceName, agentTokenHash, 'active', now, now, now).run();
  }

  await storeCredential(env, user.id, botId, botToken, now);
  const membership = await ensureMembership(env, user.id, now);

  return json({ ok: true, user_id: user.id, ilink_user_id: ilinkUserId, device_id: deviceId, agent_token: agentToken, membership });
}

async function storeCredential(env, userId, botId, botToken, now) {
  const tokenHash = await sha256Hex(botToken);
  const encrypted = await encryptString(env, botToken);
  await env.DB.prepare('UPDATE ilink_credentials SET active = 0, updated_at = ? WHERE user_id = ?')
    .bind(now, userId).run();
  const existing = await env.DB.prepare('SELECT id FROM ilink_credentials WHERE bot_token_hash = ?').bind(tokenHash).first();
  if (existing) {
    await env.DB.prepare(
      'UPDATE ilink_credentials SET user_id = ?, bot_id = ?, bot_token_encrypted = ?, active = 1, updated_at = ? WHERE id = ?'
    ).bind(userId, botId, encrypted, now, existing.id).run();
  } else {
    await env.DB.prepare(
      'INSERT INTO ilink_credentials (id, user_id, bot_id, bot_token_hash, bot_token_encrypted, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)'
    ).bind(`cred_${crypto.randomUUID()}`, userId, botId, tokenHash, encrypted, now, now).run();
  }
}

async function handleContext(request, env, auth) {
  const body = await readJson(request);
  const toUserId = requireString(body.to_user_id, 'to_user_id', 256);
  const contextToken = requireString(body.context_token, 'context_token', 4096);
  const now = nowIso();
  const lastInboundAt = optionalString(body.last_inbound_at, 64) || now;
  const ttlHours = envNumber(env.CONTEXT_TTL_HOURS, DEFAULT_CONTEXT_TTL_HOURS);
  const expiresAt = addHours(lastInboundAt, ttlHours);
  const encrypted = await encryptString(env, contextToken);
  const contextHash = await sha256Hex(contextToken);

  const existing = await env.DB.prepare('SELECT id FROM ilink_contexts WHERE user_id = ? AND to_user_id = ?')
    .bind(auth.user.id, toUserId).first();
  if (existing) {
    await env.DB.prepare(
      'UPDATE ilink_contexts SET context_token_hash = ?, context_token_encrypted = ?, last_inbound_at = ?, context_expires_at = ?, updated_at = ? WHERE id = ?'
    ).bind(contextHash, encrypted, lastInboundAt, expiresAt, now, existing.id).run();
  } else {
    await env.DB.prepare(
      'INSERT INTO ilink_contexts (id, user_id, to_user_id, context_token_hash, context_token_encrypted, last_inbound_at, context_expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
    ).bind(`ctx_${crypto.randomUUID()}`, auth.user.id, toUserId, contextHash, encrypted, lastInboundAt, expiresAt, now).run();
  }
  await env.DB.prepare('UPDATE users SET default_to_user_id = ?, updated_at = ? WHERE id = ?')
    .bind(toUserId, now, auth.user.id).run();
  const membership = await ensureMembership(env, auth.user.id, now);
  let payment = null;
  let notice = null;
  if (!membership.is_active) {
    try {
      payment = await getOrCreateWechatNativeOrder(env, auth.user.id, {
        origin: new URL(request.url).origin,
        toUserId,
        reason: 'membership_expired_context'
      });
    } catch (e) {
      console.log('create context payment failed', e?.message || e);
    }
    notice = await sendMembershipExpiredWechatNotice(env, {
      userId: auth.user.id,
      deviceId: auth.device.device_id,
      toUserId,
      contextToken,
      membership,
      payment,
      logError: 'membership_expired_context'
    });
  }

  return json({ ok: true, to_user_id: toUserId, context_expires_at: expiresAt, membership, payment, notice });
}

async function handleHeartbeat(env, auth) {
  const now = nowIso();
  const membership = await ensureMembership(env, auth.user.id, now);
  const minIntervalSeconds = envNumber(env.HEARTBEAT_MIN_INTERVAL_SECONDS, DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS);
  const lastSeenMs = Date.parse(auth.device.last_seen_at || '');
  if (Number.isFinite(lastSeenMs) && Date.now() - lastSeenMs < minIntervalSeconds * 1000) {
    return json({ ok: true, time: now, skipped: true, last_seen_at: auth.device.last_seen_at, membership });
  }
  await env.DB.prepare('UPDATE devices SET last_seen_at = ?, updated_at = ? WHERE id = ?')
    .bind(now, now, auth.device.id).run();
  return json({ ok: true, time: now, skipped: false, membership });
}

async function handleMe(env, auth) {
  const membership = await ensureMembership(env, auth.user.id, nowIso());
  const contexts = await env.DB.prepare(
    'SELECT to_user_id, last_inbound_at, context_expires_at, updated_at FROM ilink_contexts WHERE user_id = ? ORDER BY updated_at DESC LIMIT 20'
  ).bind(auth.user.id).all();
  return json({
    ok: true,
    user: {
      id: auth.user.id,
      ilink_user_id: auth.user.ilink_user_id,
      default_to_user_id: auth.user.default_to_user_id,
      status: auth.user.status
    },
    device: {
      id: auth.device.id,
      device_id: auth.device.device_id,
      device_name: auth.device.device_name,
      last_seen_at: auth.device.last_seen_at
    },
    membership,
    pricing: pricingConfig(env),
    contexts: contexts.results || []
  });
}

async function handleNotify(request, env, auth) {
  const body = await readJson(request);
  const text = requireString(body.text, 'text', MAX_TEXT_LENGTH);
  const requestedToUserId = optionalString(body.to_user_id, 256);
  const now = nowIso();

  const context = requestedToUserId
    ? await env.DB.prepare('SELECT * FROM ilink_contexts WHERE user_id = ? AND to_user_id = ?').bind(auth.user.id, requestedToUserId).first()
    : await env.DB.prepare(
      'SELECT * FROM ilink_contexts WHERE user_id = ? ORDER BY CASE WHEN to_user_id = ? THEN 0 ELSE 1 END, updated_at DESC LIMIT 1'
    ).bind(auth.user.id, auth.user.default_to_user_id || '').first();

  if (!context) fail(409, 'no_active_context');
  if (Date.parse(context.context_expires_at) <= Date.now()) fail(409, 'context_expired');

  const credential = await env.DB.prepare(
    'SELECT * FROM ilink_credentials WHERE user_id = ? AND active = 1 ORDER BY updated_at DESC LIMIT 1'
  ).bind(auth.user.id).first();
  if (!credential) fail(409, 'no_active_credential');

  const botToken = await decryptString(env, credential.bot_token_encrypted);
  const contextToken = await decryptString(env, context.context_token_encrypted);
  const membership = await ensureMembership(env, auth.user.id, now);
  if (!membership.is_active) {
    let payment = null;
    try {
      payment = await getOrCreateWechatNativeOrder(env, auth.user.id, {
        origin: new URL(request.url).origin,
        toUserId: context.to_user_id,
        reason: 'membership_expired'
      });
    } catch (e) {
      console.log('create membership payment failed', e?.message || e);
    }
    const notice = await sendMembershipExpiredWechatNotice(env, {
      userId: auth.user.id,
      deviceId: auth.device.device_id,
      toUserId: context.to_user_id,
      contextToken,
      botToken,
      membership,
      payment,
      originalTextPreview: text.slice(0, 160),
      logError: 'membership_expired'
    });
    return json({ ok: true, membership_expired: true, membership, payment, notice_sent: notice.sent, notice });
  }
  const ilinkResult = await sendIlinkMessage(env, botToken, context.to_user_id, contextToken, text);
  const ok = isIlinkOk(ilinkResult);

  const expiredByIlink = isExpiredIlinkResult(ilinkResult);
  if (!ok && expiredByIlink) {
    await env.DB.prepare('UPDATE ilink_contexts SET context_expires_at = ?, updated_at = ? WHERE id = ?')
      .bind(now, now, context.id).run();
  }

  await env.DB.prepare(
    'INSERT INTO notify_logs (id, user_id, device_id, to_user_id, text_preview, ok, ilink_ret, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).bind(
    `log_${crypto.randomUUID()}`,
    auth.user.id,
    auth.device.device_id,
    context.to_user_id,
    text.slice(0, 160),
    ok ? 1 : 0,
    JSON.stringify(ilinkResult).slice(0, 1000),
    ok ? '' : String(ilinkResult.errmsg || ilinkResult.error || 'send_failed'),
    now
  ).run();

  if (!ok && expiredByIlink) return json({ ok: false, error: 'context_invalid_or_expired', ilink: ilinkResult }, 409);
  if (!ok) return json({ ok: false, error: 'ilink_send_failed', ilink: ilinkResult }, 502);
  return json({ ok: true, to_user_id: context.to_user_id, ilink: ilinkResult });
}

function trialDays(env) {
  return Math.max(1, Math.floor(envNumber(env.TRIAL_DAYS, DEFAULT_TRIAL_DAYS)));
}

function annualPriceCents(env) {
  return Math.max(1, Math.floor(envNumber(env.ANNUAL_PRICE_CENTS, DEFAULT_ANNUAL_PRICE_CENTS)));
}

function pricingConfig(env) {
  const cents = annualPriceCents(env);
  return {
    trial_days: trialDays(env),
    annual_price_cents: cents,
    annual_price_yuan: formatCny(cents),
    annual_days: annualDays(env)
  };
}

function formatCny(cents) {
  return (cents / 100).toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
}

function annualDays(env) {
  return Math.max(1, Math.floor(envNumber(env.ANNUAL_DAYS, DEFAULT_ANNUAL_DAYS)));
}

function paymentExpireMinutes(env) {
  return Math.max(1, Math.min(120, Math.floor(envNumber(env.PAYMENT_EXPIRE_MINUTES, DEFAULT_PAYMENT_EXPIRE_MINUTES))));
}

async function ensureMembership(env, userId, now = nowIso()) {
  let row = await env.DB.prepare('SELECT * FROM memberships WHERE user_id = ?').bind(userId).first();
  if (!row) {
    const expiresAt = addDays(now, trialDays(env));
    await env.DB.prepare(
      'INSERT INTO memberships (user_id, plan, status, trial_started_at, expires_at, paid_until, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)'
    ).bind(userId, 'trial', 'active', now, expiresAt, null, now).run();
    row = { user_id: userId, plan: 'trial', status: 'active', trial_started_at: now, expires_at: expiresAt, paid_until: null, updated_at: now };
  }
  return normalizeMembership(row, now);
}

function normalizeMembership(row, now = nowIso()) {
  const expiresMs = Date.parse(row?.expires_at || '');
  const nowMs = Date.parse(now);
  const disabled = row?.status === 'disabled';
  const validDate = Number.isFinite(expiresMs) && Number.isFinite(nowMs);
  const isActive = !disabled && validDate && expiresMs > nowMs;
  const daysLeft = validDate ? Math.ceil((expiresMs - nowMs) / (24 * 60 * 60 * 1000)) : null;
  return {
    user_id: row?.user_id || '',
    plan: row?.plan || 'trial',
    status: row?.status || 'active',
    computed_status: disabled ? 'disabled' : (isActive ? (row?.plan === 'trial' ? 'trialing' : 'active') : 'expired'),
    is_active: isActive,
    trial_started_at: row?.trial_started_at || '',
    expires_at: row?.expires_at || '',
    paid_until: row?.paid_until || '',
    updated_at: row?.updated_at || '',
    days_left: daysLeft
  };
}

function membershipExpiredNotice(env, membership, payment = null, options = {}) {
  const price = pricingConfig(env).annual_price_yuan;
  const expiredAt = membership?.expires_at ? membership.expires_at.slice(0, 10) : '';
  const hasPaymentPage = Boolean(payment?.pay_url);
  const lines = [
    'wx2codex 会员已到期。',
    expiredAt ? `到期时间：${expiredAt}` : '',
    `续费价格：${price} 元 / 年。`,
  ];
  if (hasPaymentPage) {
    lines.push('请点击下面链接打开支付页面，在页面里长按/识别二维码完成微信支付，支付成功后会自动开通。');
    lines.push(payment.pay_url);
  } else {
    lines.push('付款链接暂时生成失败，请联系管理员开通。');
  }
  return lines.filter(Boolean).join('\n');
}

async function sendMembershipExpiredWechatNotice(env, options) {
  const now = nowIso();
  const userId = options.userId || '';
  const deviceId = options.deviceId || null;
  const toUserId = options.toUserId || '';
  const contextToken = options.contextToken || '';
  const logError = options.logError || 'membership_expired';
  const textPreview = options.originalTextPreview || 'membership_expired_payment_notice';
  let ilinkResult = null;
  let sent = false;
  let error = '';

  try {
    if (!userId) throw new Error('missing_user_id');
    if (!toUserId) throw new Error('missing_to_user_id');
    if (!contextToken) throw new Error('missing_context_token');

    let botToken = options.botToken || '';
    if (!botToken) {
      const credential = await env.DB.prepare(
        'SELECT * FROM ilink_credentials WHERE user_id = ? AND active = 1 ORDER BY updated_at DESC LIMIT 1'
      ).bind(userId).first();
      if (!credential) throw new Error('no_active_credential');
      botToken = await decryptString(env, credential.bot_token_encrypted);
    }

    const payment = options.payment || {};
    const noticeText = membershipExpiredNotice(env, options.membership, payment);
    const textResult = await sendIlinkMessage(env, botToken, toUserId, contextToken, noticeText);
    ilinkResult = textResult;
    sent = isIlinkOk(textResult);

    if (!sent) {
      error = error || String(ilinkResult?.errmsg || ilinkResult?.error || 'ilink_send_failed');
      if (isExpiredIlinkResult(ilinkResult)) {
        await env.DB.prepare(
          'UPDATE ilink_contexts SET context_expires_at = ?, updated_at = ? WHERE user_id = ? AND to_user_id = ?'
        ).bind(now, now, userId, toUserId).run();
      }
    }
  } catch (e) {
    error = String(e?.message || e || 'send_failed');
    ilinkResult = { error };
    console.log('membership expired notice failed', error);
  }

  try {
    await env.DB.prepare(
      'INSERT INTO notify_logs (id, user_id, device_id, to_user_id, text_preview, ok, ilink_ret, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
    ).bind(
      `log_${crypto.randomUUID()}`,
      userId,
      deviceId,
      toUserId,
      String(textPreview).slice(0, 160),
      sent ? 1 : 0,
      JSON.stringify(ilinkResult).slice(0, 1000),
      sent ? '' : (error || logError),
      now
    ).run();
  } catch (e) {
    console.log('membership expired notice log failed', e?.message || e);
  }

  return {
    sent,
    ok: sent,
    image_sent: Boolean(ilinkResult?.image && isIlinkOk(ilinkResult.image)),
    fallback_sent: Boolean(ilinkResult?.fallback && isIlinkOk(ilinkResult.fallback)),
    error: sent ? '' : (error || logError),
    ilink: ilinkResult
  };
}

async function handleCreateWechatPayNativeOrder(request, env, auth) {
  const body = await readJson(request);
  const toUserId = optionalString(body.to_user_id, 256) || auth.user.default_to_user_id || '';
  const payment = await getOrCreateWechatNativeOrder(env, auth.user.id, {
    origin: new URL(request.url).origin,
    toUserId,
    reason: optionalString(body.reason, 64) || 'manual'
  });
  return json({ ok: true, payment, pricing: pricingConfig(env) });
}

async function getOrCreateWechatNativeOrder(env, userId, options = {}) {
  const now = nowIso();
  const amountCents = annualPriceCents(env);
  const expireMinutes = paymentExpireMinutes(env);
  const pending = await env.DB.prepare(
    `SELECT * FROM orders
     WHERE user_id = ? AND provider = ? AND status = ? AND amount_cents = ? AND created_at > ?
     ORDER BY created_at DESC LIMIT 5`
  ).bind(userId, 'wechat_native', 'pending', amountCents, addMinutes(now, -expireMinutes)).all();
  for (const row of pending.results || []) {
    const raw = parseJsonSafe(row.raw);
    if (raw.code_url && raw.expire_minutes === expireMinutes && (!raw.expires_at || Date.parse(raw.expires_at) > Date.now())) {
      return publicPaymentFromOrder(env, options.origin || '', row, raw);
    }
  }

  const outTradeNo = makeOutTradeNo();
  const expiresAt = addMinutes(now, expireMinutes);
  const rawOrder = {
    appid: wechatPayAppId(env),
    mchid: wechatPayMchId(env),
    description: 'wx2codex会员年费',
    out_trade_no: outTradeNo,
    notify_url: env.WECHAT_PAY_NOTIFY_URL || `${String(options.origin || '').replace(/\/$/, '')}/v1/pay/wechat/notify`,
    time_expire: wechatPayTime(expiresAt),
    amount: { total: amountCents, currency: 'CNY' }
  };
  if (!rawOrder.notify_url || rawOrder.notify_url.startsWith('/')) fail(500, 'missing_wechat_pay_notify_url');
  const result = await wechatPayRequest(env, 'POST', '/v3/pay/transactions/native', rawOrder);
  if (!result.code_url) fail(502, 'wechat_pay_no_code_url');

  const orderId = `ord_${crypto.randomUUID()}`;
  const raw = {
    code_url: result.code_url,
    pay_url: paymentUrl(options.origin || '', orderId),
    expires_at: expiresAt,
    expire_minutes: expireMinutes,
    to_user_id: options.toUserId || '',
    reason: options.reason || '',
    wechat: { appid: rawOrder.appid, mchid: rawOrder.mchid }
  };
  await env.DB.prepare(
    'INSERT INTO orders (id, user_id, plan, amount_cents, currency, provider, provider_order_id, status, paid_at, created_at, updated_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).bind(
    orderId,
    userId,
    'annual',
    amountCents,
    'CNY',
    'wechat_native',
    outTradeNo,
    'pending',
    null,
    now,
    now,
    JSON.stringify(raw)
  ).run();
  return publicPaymentFromOrder(env, options.origin || '', {
    id: orderId,
    user_id: userId,
    plan: 'annual',
    amount_cents: amountCents,
    currency: 'CNY',
    provider: 'wechat_native',
    provider_order_id: outTradeNo,
    status: 'pending',
    created_at: now,
    updated_at: now
  }, raw);
}

function publicPaymentFromOrder(env, origin, row, raw = null) {
  const data = raw || parseJsonSafe(row.raw);
  return {
    order_id: row.id,
    out_trade_no: row.provider_order_id,
    status: row.status,
    amount_cents: row.amount_cents,
    amount_yuan: formatCny(row.amount_cents || annualPriceCents(env)),
    currency: row.currency || 'CNY',
    pay_url: data.pay_url || paymentUrl(origin, row.id),
    code_url: data.code_url || '',
    expires_at: data.expires_at || '',
    expires_in_minutes: data.expire_minutes || paymentExpireMinutes(env),
    expires_in_seconds: data.expires_at ? Math.max(0, Math.floor((Date.parse(data.expires_at) - Date.now()) / 1000)) : null,
    created_at: row.created_at || ''
  };
}

async function paymentPageHtml(env, origin, orderId) {
  const row = await env.DB.prepare('SELECT id, user_id, plan, amount_cents, currency, provider, provider_order_id, status, paid_at, created_at, updated_at, raw FROM orders WHERE id = ? LIMIT 1')
    .bind(orderId).first();
  if (!row || row.provider !== 'wechat_native') {
    return `<!doctype html><meta charset="utf-8"><title>订单不存在</title>${paymentStyle()}<main class="pay"><h1>订单不存在</h1><p>请回到微信重新获取支付链接。</p></main>`;
  }
  const raw = parseJsonSafe(row.raw);
  const amount = formatCny(row.amount_cents || annualPriceCents(env));
  const codeUrl = raw.code_url || '';
  const paid = row.status === 'paid';
  const expired = raw.expires_at && Date.parse(raw.expires_at) <= Date.now() && !paid;
  const expireMinutes = raw.expire_minutes || paymentExpireMinutes(env);
  const expireText = raw.expires_at ? `请在 ${expireMinutes} 分钟内完成支付；本二维码到期时间：${new Date(raw.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}` : `请在 ${expireMinutes} 分钟内完成支付。`;
  const qrSrc = codeUrl ? `https://api.qrserver.com/v1/create-qr-code/?size=280x280&data=${encodeURIComponent(codeUrl)}` : '';
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>wx2codex 会员支付</title>${paymentStyle()}</head>
<body><main class="pay">
  <div class="brand">wx2codex</div>
  <h1>${paid ? '支付成功' : '会员年费支付'}</h1>
  <p class="sub">订单号：${escapeHtml(row.provider_order_id || row.id)}</p>
  <div class="amount">￥${escapeHtml(amount)}</div>
  ${paid ? `<div class="ok">已支付，会员已自动开通。</div>` : ''}
  ${!paid && expired ? `<div class="warn">这个支付二维码可能已过期，请回到微信重新发送消息获取新的支付链接。</div>` : ''}
  ${!paid && codeUrl ? `<p class="tip">${escapeHtml(expireText)}</p><img class="qr" src="${qrSrc}" alt="微信支付二维码"><p class="tip">请用微信扫码/长按识别二维码支付。支付成功后本页会自动刷新状态。</p>` : ''}
  ${!paid && !codeUrl ? `<div class="warn">订单缺少支付二维码，请回到微信重新获取。</div>` : ''}
  <p class="tiny">支付成功后，系统会自动给 wx2codex 会员延长 ${annualDays(env)} 天。</p>
</main>
<script>
const orderId = ${JSON.stringify(row.id)};
async function poll(){
  try{
    const r = await fetch('/v1/pay/orders/'+encodeURIComponent(orderId)+'/status');
    const d = await r.json();
    if(d.ok && d.order && d.order.status === 'paid') location.reload();
  }catch(e){}
}
${paid ? '' : 'setInterval(poll, 3000);'}
</script></body></html>`;
}

function paymentStyle() {
  return `<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#e0f2fe,#f5f3ff);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif;color:#0f172a}.pay{width:min(420px,calc(100vw - 32px));background:white;border:1px solid #e5e7eb;border-radius:24px;padding:28px;text-align:center;box-shadow:0 24px 80px #0f172a20}.brand{display:inline-flex;padding:7px 12px;border-radius:999px;background:#eff6ff;color:#2563eb;font-weight:800}h1{margin:18px 0 6px}.sub,.tip,.tiny{color:#64748b;line-height:1.7}.amount{font-size:42px;font-weight:900;color:#16a34a;margin:18px 0}.qr{width:280px;height:280px;max-width:100%;border:1px solid #e5e7eb;border-radius:18px;padding:10px;background:#fff}.btn{display:block;background:#07c160;color:white;text-decoration:none;padding:14px 16px;border-radius:14px;font-weight:800;margin:18px 0}.ok{background:#dcfce7;color:#166534;border-radius:14px;padding:14px;margin:18px 0}.warn{background:#fef3c7;color:#92400e;border-radius:14px;padding:14px;margin:18px 0}.tiny{font-size:12px}
</style>`;
}

async function handlePaymentOrderStatus(env, orderId) {
  const row = await env.DB.prepare('SELECT id, amount_cents, currency, provider_order_id, status, paid_at, created_at, updated_at, raw FROM orders WHERE id = ? LIMIT 1')
    .bind(orderId).first();
  if (!row) fail(404, 'order_not_found');
  return { ok: true, order: publicPaymentFromOrder(env, '', row) };
}

async function handleWechatPayNotify(request, env) {
  const rawText = await request.text();
  let payload;
  try {
    payload = rawText.trim() ? JSON.parse(rawText) : {};
  } catch (_) {
    return wechatNotifyFail('invalid_json');
  }
  try {
    if (payload.event_type !== 'TRANSACTION.SUCCESS') {
      return wechatNotifySuccess();
    }
    const decrypted = await decryptWechatPayResource(env, payload.resource || {});
    const outTradeNo = decrypted.out_trade_no || '';
    if (!outTradeNo) return wechatNotifyFail('missing_out_trade_no');

    const order = await env.DB.prepare('SELECT * FROM orders WHERE provider = ? AND provider_order_id = ? LIMIT 1')
      .bind('wechat_native', outTradeNo).first();
    if (!order) return wechatNotifyFail('order_not_found');

    const verified = await queryWechatPayOrder(env, outTradeNo);
    if (verified.trade_state !== 'SUCCESS') {
      await appendOrderRaw(env, order, { last_notify: decrypted, last_query: verified, last_notify_at: nowIso() });
      return wechatNotifySuccess();
    }
    await markWechatOrderPaidAndGrant(env, order, verified, decrypted);
    return wechatNotifySuccess();
  } catch (e) {
    console.log('wechat pay notify failed', e?.message || e);
    return wechatNotifyFail('processing_failed');
  }
}

function wechatNotifySuccess() {
  return json({ code: 'SUCCESS', message: '成功' });
}

function wechatNotifyFail(message) {
  return json({ code: 'FAIL', message }, 500);
}

async function markWechatOrderPaidAndGrant(env, order, verified, notifyData) {
  const now = nowIso();
  const fresh = await env.DB.prepare('SELECT * FROM orders WHERE id = ? LIMIT 1').bind(order.id).first();
  const raw = parseJsonSafe(fresh.raw);
  if (fresh.status === 'paid' && raw.membership_granted_at) return;

  if (String(verified.mchid || '') !== wechatPayMchId(env)) throw new Error('mchid_mismatch');
  if (String(verified.appid || '') !== wechatPayAppId(env)) throw new Error('appid_mismatch');
  const paidAmount = Number(verified.amount?.total ?? notifyData.amount?.total ?? 0);
  if (paidAmount !== Number(fresh.amount_cents)) throw new Error('amount_mismatch');

  const membership = await ensureMembership(env, fresh.user_id, now);
  const baseMs = Math.max(Date.parse(membership.expires_at || '') || 0, Date.parse(now));
  const expiresAt = new Date(baseMs + annualDays(env) * 24 * 60 * 60 * 1000).toISOString();
  const nextRaw = {
    ...raw,
    transaction_id: verified.transaction_id || notifyData.transaction_id || '',
    payer: verified.payer || notifyData.payer || null,
    trade_state: verified.trade_state,
    paid_amount: paidAmount,
    notify_data: notifyData,
    query_data: verified,
    membership_granted_at: now,
    membership_expires_at: expiresAt
  };
  await env.DB.batch([
    env.DB.prepare('UPDATE memberships SET plan = ?, status = ?, expires_at = ?, paid_until = ?, updated_at = ? WHERE user_id = ?')
      .bind('annual', 'active', expiresAt, expiresAt, now, fresh.user_id),
    env.DB.prepare('UPDATE orders SET status = ?, paid_at = ?, updated_at = ?, raw = ? WHERE id = ?')
      .bind('paid', now, now, JSON.stringify(nextRaw).slice(0, 8000), fresh.id)
  ]);

  await sendPaymentSuccessWechatNotice(env, fresh.user_id, raw.to_user_id || '', expiresAt);
}

async function sendPaymentSuccessWechatNotice(env, userId, preferredToUserId, expiresAt) {
  const now = nowIso();
  const context = preferredToUserId
    ? await env.DB.prepare('SELECT * FROM ilink_contexts WHERE user_id = ? AND to_user_id = ? AND context_expires_at > ? LIMIT 1')
      .bind(userId, preferredToUserId, now).first()
    : await env.DB.prepare('SELECT * FROM ilink_contexts WHERE user_id = ? AND context_expires_at > ? ORDER BY updated_at DESC LIMIT 1')
      .bind(userId, now).first();
  if (!context) return;
  const credential = await env.DB.prepare('SELECT * FROM ilink_credentials WHERE user_id = ? AND active = 1 ORDER BY updated_at DESC LIMIT 1')
    .bind(userId).first();
  if (!credential) return;
  try {
    const botToken = await decryptString(env, credential.bot_token_encrypted);
    const contextToken = await decryptString(env, context.context_token_encrypted);
    const text = `wx2codex 续费成功，会员已延长至 ${expiresAt.slice(0, 10)}。现在可以继续使用了。`;
    const result = await sendIlinkMessage(env, botToken, context.to_user_id, contextToken, text);
    await env.DB.prepare(
      'INSERT INTO notify_logs (id, user_id, device_id, to_user_id, text_preview, ok, ilink_ret, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
    ).bind(
      `log_${crypto.randomUUID()}`,
      userId,
      null,
      context.to_user_id,
      text.slice(0, 160),
      isIlinkOk(result) ? 1 : 0,
      JSON.stringify(result).slice(0, 1000),
      isIlinkOk(result) ? '' : 'payment_success_notice_failed',
      now
    ).run();
  } catch (e) {
    console.log('payment success notice failed', e?.message || e);
  }
}

async function appendOrderRaw(env, order, extra) {
  const raw = { ...parseJsonSafe(order.raw), ...extra };
  await env.DB.prepare('UPDATE orders SET updated_at = ?, raw = ? WHERE id = ?')
    .bind(nowIso(), JSON.stringify(raw).slice(0, 8000), order.id).run();
}

async function queryWechatPayOrder(env, outTradeNo) {
  const path = `/v3/pay/transactions/out-trade-no/${encodeURIComponent(outTradeNo)}?mchid=${encodeURIComponent(wechatPayMchId(env))}`;
  return wechatPayRequest(env, 'GET', path);
}

async function decryptWechatPayResource(env, resource) {
  const apiKey = wechatPayApiV3Key(env);
  const ciphertext = requireString(resource.ciphertext, 'ciphertext', 10000);
  const nonce = requireString(resource.nonce, 'nonce', 128);
  const aad = optionalString(resource.associated_data, 512);
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(apiKey), 'AES-GCM', false, ['decrypt']);
  const decrypted = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: new TextEncoder().encode(nonce), additionalData: new TextEncoder().encode(aad), tagLength: 128 },
    key,
    base64Decode(ciphertext)
  );
  return JSON.parse(new TextDecoder().decode(decrypted));
}

async function wechatPayRequest(env, method, pathWithQuery, body = null) {
  const base = (env.WECHAT_PAY_BASE_URL || DEFAULT_WECHAT_PAY_BASE_URL).replace(/\/$/, '');
  const bodyText = body ? JSON.stringify(body, null, 0) : '';
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomUUID().replace(/-/g, '');
  const message = `${method}\n${pathWithQuery}\n${timestamp}\n${nonce}\n${bodyText}\n`;
  const signature = await signWechatPayMessage(env, message);
  const auth = [
    `mchid="${wechatPayMchId(env)}"`,
    `nonce_str="${nonce}"`,
    `timestamp="${timestamp}"`,
    `serial_no="${wechatPayMerchantSerialNo(env)}"`,
    `signature="${signature}"`
  ].join(',');
  const resp = await fetch(`${base}${pathWithQuery}`, {
    method,
    headers: {
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'Authorization': `WECHATPAY2-SHA256-RSA2048 ${auth}`,
      'User-Agent': 'wx2codex-cloud/0.1'
    },
    body: bodyText || undefined
  });
  const text = await resp.text();
  let data = {};
  try {
    data = text.trim() ? JSON.parse(text) : {};
  } catch (_) {
    data = { raw: text.slice(0, 1000) };
  }
  if (!resp.ok) {
    const error = new Error(`wechat_pay_${data.code || resp.status}`);
    error.status = 502;
    error.detail = data;
    throw error;
  }
  return data;
}

async function signWechatPayMessage(env, message) {
  const key = await crypto.subtle.importKey(
    'pkcs8',
    pemToDer(wechatPayPrivateKeyPem(env)),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign']
  );
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(message));
  return base64Encode(new Uint8Array(sig));
}

function wechatPayMchId(env) {
  return requireEnv(env.WECHAT_PAY_MCH_ID, 'missing_wechat_pay_mch_id');
}

function wechatPayAppId(env) {
  return requireEnv(env.WECHAT_PAY_APP_ID, 'missing_wechat_pay_app_id');
}

function wechatPayMerchantSerialNo(env) {
  return requireEnv(env.WECHAT_PAY_MERCHANT_SERIAL_NO, 'missing_wechat_pay_serial_no');
}

function wechatPayPrivateKeyPem(env) {
  return requireEnv(env.WECHAT_PAY_PRIVATE_KEY_PEM, 'missing_wechat_pay_private_key');
}

function wechatPayApiV3Key(env) {
  const key = requireEnv(env.WECHAT_PAY_API_V3_KEY, 'missing_wechat_pay_api_v3_key');
  if (new TextEncoder().encode(key).length !== 32) fail(500, 'invalid_wechat_pay_api_v3_key');
  return key;
}

function requireEnv(value, message) {
  if (typeof value !== 'string' || !value.trim()) fail(500, message);
  return value.trim();
}

function pemToDer(pem) {
  const b64 = String(pem || '').replace(/-----BEGIN [^-]+-----/g, '').replace(/-----END [^-]+-----/g, '').replace(/\s+/g, '');
  return base64Decode(b64);
}

function base64Encode(bytes) {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function base64Decode(text) {
  const binary = atob(String(text || '').replace(/\s+/g, ''));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function makeOutTradeNo() {
  const d = new Date();
  const stamp = d.toISOString().replace(/[-:TZ.]/g, '').slice(0, 14);
  return `wx2c${stamp}${crypto.randomUUID().replace(/-/g, '').slice(0, 10)}`;
}

function paymentUrl(origin, orderId) {
  const base = String(origin || 'https://codex.292828.xyz').replace(/\/$/, '');
  return `${base}/pay/${encodeURIComponent(orderId)}`;
}

function wechatPayTime(dateIso) {
  const d = new Date(dateIso);
  return d.toISOString().replace(/\.\d{3}Z$/, '+00:00');
}

function parseJsonSafe(text) {
  try {
    return text ? JSON.parse(text) : {};
  } catch (_) {
    return {};
  }
}

async function requireDevice(request, env) {
  const header = request.headers.get('Authorization') || '';
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (!match) fail(401, 'missing_agent_token');
  const tokenHash = await sha256Hex(match[1].trim());
  const row = await env.DB.prepare(
    `SELECT
      d.id AS d_id, d.user_id AS d_user_id, d.device_id AS d_device_id, d.device_name AS d_device_name,
      d.status AS d_status, d.last_seen_at AS d_last_seen_at,
      u.id AS u_id, u.ilink_user_id AS u_ilink_user_id, u.default_to_user_id AS u_default_to_user_id, u.status AS u_status
     FROM devices d JOIN users u ON d.user_id = u.id
     WHERE d.agent_token_hash = ? LIMIT 1`
  ).bind(tokenHash).first();
  if (!row) fail(401, 'invalid_agent_token');
  if (row.d_status !== 'active' || row.u_status !== 'active') fail(403, 'inactive_user_or_device');
  return {
    device: {
      id: row.d_id,
      user_id: row.d_user_id,
      device_id: row.d_device_id,
      device_name: row.d_device_name,
      status: row.d_status,
      last_seen_at: row.d_last_seen_at
    },
    user: {
      id: row.u_id,
      ilink_user_id: row.u_ilink_user_id,
      default_to_user_id: row.u_default_to_user_id,
      status: row.u_status
    }
  };
}


async function handleAdminLogin(request, env) {
  ensureAdminConfigured(env);
  let username = '';
  let password = '';
  const contentType = request.headers.get('Content-Type') || '';
  if (contentType.includes('application/json')) {
    const body = await readJson(request);
    username = optionalString(body.username, 128);
    password = optionalString(body.password, 512);
  } else {
    const form = await request.formData();
    username = optionalString(form.get('username'), 128);
    password = optionalString(form.get('password'), 512);
  }

  const usernameOk = timingSafeEqualString(username, env.ADMIN_USERNAME || '');
  const passwordHash = await sha256Hex(password);
  const passwordOk = timingSafeEqualString(passwordHash, env.ADMIN_PASSWORD_SHA256 || '');
  if (!usernameOk || !passwordOk) {
    return html(adminLoginHtml('账号或密码错误'), 401);
  }

  const session = await signAdminSession(env, { u: username, exp: Math.floor(Date.now() / 1000) + 12 * 60 * 60, n: crypto.randomUUID() });
  return redirect('/admin', 302, {
    'Set-Cookie': adminCookie(session, 12 * 60 * 60)
  });
}

function handleAdminLogout() {
  return redirect('/admin/login', 302, {
    'Set-Cookie': 'wx2codex_admin=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'
  });
}

async function requireAdmin(request, env) {
  const session = await getAdminSession(request, env);
  if (!session) fail(401, 'admin_login_required');
  return session;
}

async function getAdminSession(request, env) {
  if (!env.ADMIN_USERNAME || !env.ADMIN_PASSWORD_SHA256 || !env.ADMIN_SESSION_SECRET) return null;
  const cookie = request.headers.get('Cookie') || '';
  const token = parseCookie(cookie).wx2codex_admin;
  if (!token) return null;
  const payload = await verifyAdminSession(env, token);
  if (!payload || payload.u !== env.ADMIN_USERNAME) return null;
  return { username: payload.u };
}

function ensureAdminConfigured(env) {
  if (!env.ADMIN_USERNAME || !env.ADMIN_PASSWORD_SHA256 || !env.ADMIN_SESSION_SECRET) {
    fail(500, 'admin_not_configured');
  }
}

function adminCookie(value, maxAge) {
  return `wx2codex_admin=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

function parseCookie(cookieHeader) {
  const out = {};
  for (const part of cookieHeader.split(';')) {
    const idx = part.indexOf('=');
    if (idx <= 0) continue;
    out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }
  return out;
}

async function signAdminSession(env, payload) {
  const payloadText = base64Url(new TextEncoder().encode(JSON.stringify(payload)));
  const sig = await hmacSha256(env.ADMIN_SESSION_SECRET, payloadText);
  return `${payloadText}.${base64Url(new Uint8Array(sig))}`;
}

async function verifyAdminSession(env, token) {
  const [payloadText, sigText] = String(token || '').split('.');
  if (!payloadText || !sigText) return null;
  const expected = base64Url(new Uint8Array(await hmacSha256(env.ADMIN_SESSION_SECRET, payloadText)));
  if (!timingSafeEqualString(expected, sigText)) return null;
  try {
    const payload = JSON.parse(new TextDecoder().decode(base64UrlDecode(payloadText)));
    if (!payload.exp || payload.exp < Math.floor(Date.now() / 1000)) return null;
    return payload;
  } catch (_) {
    return null;
  }
}

async function hmacSha256(secret, text) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', key, new TextEncoder().encode(text));
}

function timingSafeEqualString(a, b) {
  a = String(a || '');
  b = String(b || '');
  let diff = a.length ^ b.length;
  const max = Math.max(a.length, b.length);
  for (let i = 0; i < max; i += 1) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

async function adminOverview(env) {
  const now = nowIso();
  const [users, devices, credentials, contexts, notifyLogs, activeContexts, expiredContexts, membersActive, membersExpired, trialMembers, paidMembers, orders] = await Promise.all([
    countTable(env, 'users'),
    countTable(env, 'devices'),
    countTable(env, 'ilink_credentials'),
    countTable(env, 'ilink_contexts'),
    countTable(env, 'notify_logs'),
    countWhere(env, 'ilink_contexts', 'context_expires_at > ?', now),
    countWhere(env, 'ilink_contexts', 'context_expires_at <= ?', now),
    countWhere(env, 'memberships', 'status <> ? AND expires_at > ?', 'disabled', now),
    countWhere(env, 'memberships', 'status <> ? AND expires_at <= ?', 'disabled', now),
    countWhere(env, 'memberships', 'plan = ?', 'trial'),
    countWhere(env, 'memberships', 'plan <> ?', 'trial'),
    countTable(env, 'orders')
  ]);
  const recentUsers = await env.DB.prepare(`
    SELECT
      u.id, u.ilink_user_id, u.default_to_user_id, u.status, u.created_at, u.updated_at,
      m.plan, m.status AS membership_status, m.expires_at,
      (SELECT MAX(last_seen_at) FROM devices d WHERE d.user_id = u.id) AS last_seen_at
    FROM users u
    LEFT JOIN memberships m ON m.user_id = u.id
    ORDER BY u.updated_at DESC
    LIMIT 10
  `).all();
  const recentLogs = await env.DB.prepare('SELECT id, user_id, device_id, to_user_id, text_preview, ok, error, created_at FROM notify_logs ORDER BY created_at DESC LIMIT 10').all();
  return {
    ok: true,
    time: now,
    pricing: pricingConfig(env),
    counts: {
      users,
      devices,
      credentials,
      contexts,
      notify_logs: notifyLogs,
      active_contexts: activeContexts,
      expired_contexts: expiredContexts,
      members_active: membersActive,
      members_expired: membersExpired,
      members_trial: trialMembers,
      members_paid: paidMembers,
      orders
    },
    recent_users: (recentUsers.results || []).map(row => ({ ...row, membership: normalizeMembership({
      user_id: row.id,
      plan: row.plan,
      status: row.membership_status,
      expires_at: row.expires_at
    }, now) })),
    recent_notify_logs: recentLogs.results || []
  };
}

async function countTable(env, table) {
  const row = await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table}`).first();
  return row?.n || 0;
}

async function countWhere(env, table, where, value) {
  const values = Array.isArray(value) ? value : Array.prototype.slice.call(arguments, 3);
  const row = await env.DB.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE ${where}`).bind(...values).first();
  return row?.n || 0;
}

const ADMIN_TABLES = {
  users: 'SELECT id, ilink_user_id, default_to_user_id, status, created_at, updated_at FROM users ORDER BY updated_at DESC LIMIT ?',
  memberships: 'SELECT user_id, plan, status, trial_started_at, expires_at, paid_until, updated_at FROM memberships ORDER BY updated_at DESC LIMIT ?',
  orders: 'SELECT id, user_id, plan, amount_cents, currency, provider, provider_order_id, status, paid_at, created_at, updated_at FROM orders ORDER BY created_at DESC LIMIT ?',
  devices: 'SELECT id, user_id, device_id, device_name, status, created_at, updated_at, last_seen_at FROM devices ORDER BY updated_at DESC LIMIT ?',
  ilink_credentials: 'SELECT id, user_id, bot_id, substr(bot_token_hash, 1, 16) || \'...\' AS bot_token_hash, active, created_at, updated_at FROM ilink_credentials ORDER BY updated_at DESC LIMIT ?',
  ilink_contexts: 'SELECT id, user_id, to_user_id, substr(context_token_hash, 1, 16) || \'...\' AS context_token_hash, last_inbound_at, context_expires_at, updated_at FROM ilink_contexts ORDER BY updated_at DESC LIMIT ?',
  notify_logs: 'SELECT id, user_id, device_id, to_user_id, text_preview, ok, error, created_at FROM notify_logs ORDER BY created_at DESC LIMIT ?'
};

async function adminTable(env, params) {
  const name = params.get('name') || 'users';
  if (!ADMIN_TABLES[name]) fail(400, 'invalid_table');
  const limit = Math.min(Math.max(Number(params.get('limit') || 50), 1), 200);
  const data = await env.DB.prepare(ADMIN_TABLES[name]).bind(limit).all();
  return { ok: true, table: name, limit, rows: data.results || [] };
}

async function adminUsers(env, params) {
  const limit = Math.min(Math.max(Number(params.get('limit') || 100), 1), 500);
  const q = optionalString(params.get('q'), 256);
  const like = `%${q}%`;
  const now = nowIso();
  const data = await env.DB.prepare(`
    SELECT
      u.id,
      u.ilink_user_id,
      u.default_to_user_id,
      u.status AS user_status,
      u.created_at,
      u.updated_at,
      m.plan,
      m.status AS membership_status,
      m.trial_started_at,
      m.expires_at,
      m.paid_until,
      m.updated_at AS membership_updated_at,
      (SELECT COUNT(*) FROM devices d WHERE d.user_id = u.id) AS device_count,
      (SELECT MAX(last_seen_at) FROM devices d WHERE d.user_id = u.id) AS last_seen_at,
      (SELECT COUNT(*) FROM ilink_contexts c WHERE c.user_id = u.id) AS context_count,
      (SELECT COUNT(*) FROM notify_logs n WHERE n.user_id = u.id) AS notify_count,
      (SELECT COUNT(*) FROM notify_logs n WHERE n.user_id = u.id AND n.ok = 1) AS notify_ok_count
    FROM users u
    LEFT JOIN memberships m ON m.user_id = u.id
    WHERE (? = '' OR u.id LIKE ? OR u.ilink_user_id LIKE ? OR COALESCE(u.default_to_user_id, '') LIKE ?)
    ORDER BY u.updated_at DESC
    LIMIT ?
  `).bind(q, like, like, like, limit).all();
  const rows = (data.results || []).map(row => ({
    ...row,
    membership: normalizeMembership({
      user_id: row.id,
      plan: row.plan,
      status: row.membership_status,
      trial_started_at: row.trial_started_at,
      expires_at: row.expires_at,
      paid_until: row.paid_until,
      updated_at: row.membership_updated_at
    }, now)
  }));
  return { ok: true, time: now, rows, pricing: pricingConfig(env) };
}

async function adminExtendMembership(request, env) {
  const body = await readJson(request);
  const userId = requireString(body.user_id, 'user_id', 128);
  const days = Math.min(Math.max(Number(body.days || 365), 1), 3650);
  const plan = normalizePlan(optionalString(body.plan, 32) || 'annual');
  const now = nowIso();
  const user = await env.DB.prepare('SELECT id FROM users WHERE id = ?').bind(userId).first();
  if (!user) fail(404, 'user_not_found');
  const before = await ensureMembership(env, userId, now);
  const baseMs = Math.max(Date.parse(before.expires_at || '') || 0, Date.parse(now));
  const expiresAt = new Date(baseMs + days * 24 * 60 * 60 * 1000).toISOString();
  const paidUntil = plan === 'trial' ? (before.paid_until || null) : expiresAt;
  const explicitAmount = Number(body.amount_cents);
  const amountCents = Number.isFinite(explicitAmount) && explicitAmount >= 0
    ? Math.floor(explicitAmount)
    : (plan === 'annual' ? annualPriceCents(env) : 0);
  await env.DB.prepare(
    'UPDATE memberships SET plan = ?, status = ?, expires_at = ?, paid_until = ?, updated_at = ? WHERE user_id = ?'
  ).bind(plan, 'active', expiresAt, paidUntil, now, userId).run();
  await env.DB.prepare(
    'INSERT INTO orders (id, user_id, plan, amount_cents, currency, provider, provider_order_id, status, paid_at, created_at, updated_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).bind(
    `ord_${crypto.randomUUID()}`,
    userId,
    plan,
    amountCents,
    'CNY',
    'manual_admin',
    optionalString(body.provider_order_id, 128) || null,
    'paid',
    now,
    now,
    now,
    JSON.stringify({ action: 'extend', days, note: optionalString(body.note, 500) }).slice(0, 2000)
  ).run();
  return { ok: true, membership: await ensureMembership(env, userId, now) };
}

async function adminUpdateMembership(request, env) {
  const body = await readJson(request);
  const userId = requireString(body.user_id, 'user_id', 128);
  const now = nowIso();
  const user = await env.DB.prepare('SELECT id FROM users WHERE id = ?').bind(userId).first();
  if (!user) fail(404, 'user_not_found');
  await ensureMembership(env, userId, now);
  const plan = normalizePlan(optionalString(body.plan, 32) || 'trial');
  const status = normalizeMembershipStatus(optionalString(body.status, 32) || 'active');
  const expiresAt = normalizeAdminDate(body.expires_at);
  const paidUntil = plan === 'trial' ? null : expiresAt;
  await env.DB.prepare(
    'UPDATE memberships SET plan = ?, status = ?, expires_at = ?, paid_until = ?, updated_at = ? WHERE user_id = ?'
  ).bind(plan, status, expiresAt, paidUntil, now, userId).run();
  await env.DB.prepare(
    'INSERT INTO orders (id, user_id, plan, amount_cents, currency, provider, provider_order_id, status, paid_at, created_at, updated_at, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
  ).bind(
    `ord_${crypto.randomUUID()}`,
    userId,
    plan,
    0,
    'CNY',
    'manual_admin',
    null,
    'adjusted',
    null,
    now,
    now,
    JSON.stringify({ action: 'update', expires_at: expiresAt, status, note: optionalString(body.note, 500) }).slice(0, 2000)
  ).run();
  return { ok: true, membership: await ensureMembership(env, userId, now) };
}

function normalizePlan(value) {
  const plan = String(value || '').trim().toLowerCase();
  if (['trial', 'annual', 'manual', 'lifetime'].includes(plan)) return plan;
  fail(400, 'invalid_plan');
}

function normalizeMembershipStatus(value) {
  const status = String(value || '').trim().toLowerCase();
  if (['active', 'disabled'].includes(status)) return status;
  fail(400, 'invalid_membership_status');
}

function normalizeAdminDate(value) {
  const raw = String(value || '').trim();
  if (!raw) fail(400, 'missing_expires_at');
  const parsed = Date.parse(raw);
  if (!Number.isFinite(parsed)) fail(400, 'invalid_expires_at');
  return new Date(parsed).toISOString();
}

function adminLoginHtml(error = '') {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>wx2codex 管理员登录</title>${adminStyle()}</head>
<body class="login-body"><main class="login-card">
  <h1>wx2codex</h1><p>管理员后台</p>
  ${error ? `<div class="error">${escapeHtml(error)}</div>` : ''}
  <form method="post" action="/admin/login">
    <label>账号<input name="username" autocomplete="username" required autofocus></label>
    <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">登录</button>
  </form>
  <a class="muted" href="/health">health check</a>
</main></body></html>`;
}

function adminDashboardHtml(username) {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>wx2codex 后台</title>${adminStyle()}</head>
<body>
<header class="top"><div><b>wx2codex 后台</b><span>管理员：${escapeHtml(username)}</span></div><nav><a href="/health" target="_blank">Health</a><a href="/install.sh" target="_blank">install.sh</a><a href="/admin/logout">退出</a></nav></header>
<main class="wrap">
  <section class="hero">
    <div>
      <h1>用户与会员管理</h1>
      <p>新用户默认赠送 7 天试用；正式价格 9.9 元 / 年。当前后台先支持管理员手动开通/续期，后续可接入已认证小程序微信支付。</p>
    </div>
    <button id="refreshAllBtn">刷新全部</button>
  </section>
  <section class="cards" id="cards"></section>
  <section class="grid2">
    <section class="panel">
      <div class="panel-head">
        <div><h2>会员用户</h2><p>搜索 iLink User ID / 内部 User ID，快速续费或调整到期时间。</p></div>
        <div class="tools"><input id="userSearch" placeholder="搜索用户..." autocomplete="off"><button id="searchBtn">搜索</button></div>
      </div>
      <div id="usersBox" class="table-box">加载中...</div>
    </section>
    <aside class="panel">
      <h2>会员调整</h2>
      <div id="editHint" class="hint">请先在左侧选择一个用户。</div>
      <form id="memberForm" class="form">
        <input type="hidden" id="editUserId">
        <label>用户 ID<input id="editUserLabel" disabled></label>
        <label>套餐<select id="editPlan"><option value="trial">trial 试用</option><option value="annual">annual 年费</option><option value="manual">manual 手动</option><option value="lifetime">lifetime 长期</option></select></label>
        <label>状态<select id="editStatus"><option value="active">active 可用</option><option value="disabled">disabled 禁用</option></select></label>
        <label>到期时间<input id="editExpires" type="datetime-local"></label>
        <label>备注<input id="editNote" placeholder="可选，写入订单 raw"></label>
        <button type="submit">保存调整</button>
      </form>
      <div class="quick-actions">
        <button data-days="7">加 7 天</button>
        <button data-days="30">加 30 天</button>
        <button data-days="365">加 365 天（9.9/年）</button>
      </div>
    </aside>
  </section>
  <section class="panel"><div class="panel-head"><div><h2>原始数据表</h2><p>排查问题时使用，敏感 token 只显示 hash 截断。</p></div><select id="tableSelect">
    <option value="users">users</option><option value="memberships">memberships</option><option value="orders">orders</option><option value="devices">devices</option><option value="ilink_credentials">ilink_credentials</option><option value="ilink_contexts">ilink_contexts</option><option value="notify_logs">notify_logs</option>
  </select><button id="refreshTableBtn">刷新表</button></div><div id="tableBox" class="table-box"></div></section>
  <section class="panel"><h2>最近通知日志</h2><pre id="logs">加载中...</pre></section>
</main>
<script>
const qs = s => document.querySelector(s);
let currentRows = [];
async function getJson(url){ const r = await fetch(url, {credentials:'same-origin'}); const t = await r.text(); if(!r.ok) throw new Error(t || r.statusText); return t ? JSON.parse(t) : {}; }
async function postJson(url, body){ const r = await fetch(url, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); const t = await r.text(); if(!r.ok) throw new Error(t || r.statusText); return t ? JSON.parse(t) : {}; }
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function fmtDate(v){ if(!v) return '-'; try { return new Date(v).toLocaleString('zh-CN', {hour12:false}); } catch(e){ return v; } }
function short(v){ v = String(v || ''); return v.length > 24 ? v.slice(0, 10) + '…' + v.slice(-8) : (v || '-'); }
function badge(m){ const cls = m.is_active ? (m.plan === 'trial' ? 'trial' : 'active') : (m.computed_status === 'disabled' ? 'disabled' : 'expired'); const text = m.computed_status + ' / ' + m.plan; return '<span class="badge '+cls+'">'+esc(text)+'</span>'; }
function renderRows(rows){ if(!rows.length) return '<div class="empty">暂无数据</div>'; const cols = Object.keys(rows[0]); return '<table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(typeof r[c] === 'object' ? JSON.stringify(r[c]) : r[c])+'</td>').join('')+'</tr>').join('')+'</tbody></table>'; }
function renderUsers(rows){
  if(!rows.length) return '<div class="empty">暂无用户</div>';
  return '<table class="users"><thead><tr><th>用户</th><th>会员</th><th>到期/剩余</th><th>设备/会话</th><th>通知</th><th>最后在线</th><th>操作</th></tr></thead><tbody>'+
    rows.map((r, i) => {
      const m = r.membership || {};
      const days = m.days_left === null || m.days_left === undefined ? '-' : m.days_left + ' 天';
      return '<tr>'+
        '<td><b>'+esc(short(r.ilink_user_id))+'</b><small>'+esc(r.id)+'</small></td>'+
        '<td>'+badge(m)+'</td>'+
        '<td>'+esc(fmtDate(m.expires_at))+'<small>剩余：'+esc(days)+'</small></td>'+
        '<td>'+esc(r.device_count || 0)+' 台 / '+esc(r.context_count || 0)+' 会话</td>'+
        '<td>'+esc(r.notify_ok_count || 0)+' / '+esc(r.notify_count || 0)+'</td>'+
        '<td>'+esc(fmtDate(r.last_seen_at))+'</td>'+
        '<td class="actions"><button onclick="selectUser('+i+')">编辑</button><button onclick="extendUser('+i+',365)">续 365 天</button></td>'+
      '</tr>';
    }).join('')+'</tbody></table>';
}
async function loadOverview(){ const d = await getJson('/admin/api/overview'); const labels = {users:'用户',devices:'设备',members_active:'有效会员',members_expired:'过期会员',members_trial:'试用',members_paid:'付费/手动',notify_logs:'通知日志',orders:'订单'}; const keys = ['users','devices','members_active','members_expired','members_trial','members_paid','notify_logs','orders']; qs('#cards').innerHTML = keys.map(k=>'<div class="card"><div>'+esc(d.counts[k] ?? 0)+'</div><span>'+esc(labels[k] || k)+'</span></div>').join(''); qs('#logs').textContent = JSON.stringify(d.recent_notify_logs, null, 2); }
async function loadUsers(){ qs('#usersBox').textContent='加载中...'; const q = qs('#userSearch').value.trim(); const d = await getJson('/admin/api/users?limit=200&q='+encodeURIComponent(q)); currentRows = d.rows || []; qs('#usersBox').innerHTML = renderUsers(currentRows); }
async function loadTable(){ const name = qs('#tableSelect').value; qs('#tableBox').textContent='加载中...'; const d = await getJson('/admin/api/table?name='+encodeURIComponent(name)+'&limit=100'); qs('#tableBox').innerHTML = renderRows(d.rows || []); }
function selectUser(i){ const r = currentRows[i]; if(!r) return; const m = r.membership || {}; qs('#editHint').textContent = '正在编辑：' + (r.ilink_user_id || r.id); qs('#editUserId').value = r.id; qs('#editUserLabel').value = r.id + ' / ' + (r.ilink_user_id || '-'); qs('#editPlan').value = m.plan || 'trial'; qs('#editStatus').value = m.status === 'disabled' ? 'disabled' : 'active'; qs('#editExpires').value = m.expires_at ? new Date(m.expires_at).toISOString().slice(0,16) : ''; qs('#editNote').value = ''; }
async function extendUser(i, days){ const r = currentRows[i]; if(!r) return; if(!confirm('确认给用户 '+(r.ilink_user_id || r.id)+' 延长 '+days+' 天？')) return; await postJson('/admin/api/membership/extend', {user_id:r.id, days:days, plan:days >= 365 ? 'annual' : 'manual', note:'admin quick extend'}); await refreshAll(); }
async function updateUser(e){ e.preventDefault(); const user_id = qs('#editUserId').value; if(!user_id) return alert('请先选择用户'); await postJson('/admin/api/membership/update', {user_id, plan:qs('#editPlan').value, status:qs('#editStatus').value, expires_at:qs('#editExpires').value, note:qs('#editNote').value}); await refreshAll(); alert('已保存'); }
async function refreshAll(){ try { await Promise.all([loadOverview(), loadUsers(), loadTable()]); } catch(e){ alert(e.message); } }
qs('#tableSelect').addEventListener('change', loadTable);
qs('#refreshTableBtn').addEventListener('click', loadTable);
qs('#refreshAllBtn').addEventListener('click', refreshAll);
qs('#searchBtn').addEventListener('click', loadUsers);
qs('#userSearch').addEventListener('keydown', e => { if(e.key === 'Enter') loadUsers(); });
qs('#memberForm').addEventListener('submit', updateUser);
document.querySelectorAll('.quick-actions button').forEach(btn => btn.addEventListener('click', () => { const id = qs('#editUserId').value; const idx = currentRows.findIndex(r => r.id === id); if(idx >= 0) extendUser(idx, Number(btn.dataset.days)); }));
refreshAll();
</script>
</body></html>`;
}

function adminStyle() {
  return `<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif;color:#172033;background:#f5f7fb}body{margin:0;background:#f5f7fb}.top{height:64px;background:#0f172a;color:#fff;display:flex;align-items:center;justify-content:space-between;padding:0 24px;box-shadow:0 10px 30px #0206172b;position:sticky;top:0;z-index:5}.top b{font-size:18px}.top span{margin-left:14px;color:#cbd5e1;font-size:13px}.top a{color:#e0f2fe;text-decoration:none;margin-left:16px}.wrap{max-width:1380px;margin:24px auto 60px;padding:0 18px}.hero{display:flex;justify-content:space-between;gap:18px;align-items:center;background:linear-gradient(135deg,#2563eb,#7c3aed);color:#fff;border-radius:24px;padding:26px 28px;margin-bottom:18px;box-shadow:0 18px 40px #1d4ed82b}.hero h1{margin:0 0 8px;font-size:30px}.hero p{margin:0;max-width:820px;color:#dbeafe;line-height:1.7}.hero button{background:#fff;color:#1d4ed8;border-color:#fff}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:14px;margin-bottom:18px}.card{background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:18px;box-shadow:0 10px 28px #0f172a0a}.card div{font-size:30px;font-weight:850;color:#2563eb;letter-spacing:-.03em}.card span{font-size:13px;color:#64748b}.grid2{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:18px;align-items:start}.panel{background:#fff;border:1px solid #e5e7eb;border-radius:18px;margin:18px 0;padding:18px;box-shadow:0 10px 28px #0f172a0a}.panel h2{margin:0 0 4px;font-size:19px}.panel p{margin:0;color:#64748b;font-size:13px}.panel-head{display:flex;gap:14px;align-items:center;justify-content:space-between;margin-bottom:14px}.tools{display:flex;gap:8px;align-items:center}select,button,input{border:1px solid #cbd5e1;border-radius:12px;padding:10px 12px;font-size:14px;background:#fff}input:disabled{background:#f1f5f9;color:#64748b}button{background:#2563eb;color:white;border-color:#2563eb;cursor:pointer;font-weight:650;white-space:nowrap}button:hover{filter:brightness(.96)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid #e5e7eb;text-align:left;padding:10px 11px;vertical-align:middle;max-width:380px;overflow-wrap:anywhere}th{background:#f8fafc;color:#475569;font-size:12px;text-transform:uppercase;letter-spacing:.03em}.table-box{overflow:auto;border:1px solid #eef2f7;border-radius:14px}.users small{display:block;color:#64748b;margin-top:4px;font-size:11px}.actions{display:flex;gap:8px;flex-wrap:wrap}.actions button{padding:7px 10px;font-size:12px}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:800}.badge.active{background:#dcfce7;color:#166534}.badge.trial{background:#dbeafe;color:#1d4ed8}.badge.expired{background:#fee2e2;color:#991b1b}.badge.disabled{background:#e5e7eb;color:#374151}.form{display:grid;gap:12px}.form label{font-size:13px;color:#334155}.form input,.form select,.form button{box-sizing:border-box;width:100%;margin-top:6px}.quick-actions{display:grid;grid-template-columns:1fr;gap:8px;margin-top:12px}.quick-actions button{background:#0f172a;border-color:#0f172a}.hint,.empty,.error{padding:14px;border-radius:12px;background:#f8fafc;color:#475569;margin-bottom:12px}.error{background:#fef2f2;color:#b91c1c}.login-body{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#1d4ed8,#9333ea)}.login-card{width:min(380px,calc(100vw - 32px));background:#fff;border-radius:22px;padding:28px;box-shadow:0 22px 70px #0005}.login-card h1{margin:0;font-size:30px}.login-card p{color:#64748b}.login-card label{display:block;margin:14px 0;color:#334155}.login-card input{box-sizing:border-box;width:100%;margin-top:8px}.login-card button{width:100%;margin-top:12px}.muted{display:block;margin-top:18px;color:#64748b;text-align:center;text-decoration:none}pre{white-space:pre-wrap;background:#0f172a;color:#dbeafe;padding:14px;border-radius:12px;overflow:auto}@media(max-width:980px){.grid2{grid-template-columns:1fr}.panel-head,.hero{align-items:stretch;flex-direction:column}.tools{width:100%}.tools input{flex:1}.top{padding:0 14px}.top nav a{margin-left:8px}}
</style>`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

async function sendIlinkMessage(env, botToken, toUserId, contextToken, text) {
  return postIlinkBotEndpoint(env, botToken, 'sendmessage', {
    msg: {
      from_user_id: '',
      to_user_id: toUserId,
      client_id: `wx2codex:${Date.now()}-${crypto.randomUUID().slice(0, 8)}`,
      message_type: 2,
      message_state: 2,
      context_token: contextToken,
      item_list: [{ type: 1, text_item: { text } }]
    }
  });
}

async function sendIlinkImageMessage(env, botToken, toUserId, contextToken, imageBytes, filename = 'image.png') {
  const uploaded = await uploadIlinkMedia(env, botToken, toUserId, imageBytes, filename, 1);
  return postIlinkBotEndpoint(env, botToken, 'sendmessage', {
    msg: {
      from_user_id: '',
      to_user_id: toUserId,
      client_id: `wx2codex:image:${Date.now()}-${crypto.randomUUID().slice(0, 8)}`,
      message_type: 2,
      message_state: 2,
      context_token: contextToken,
      item_list: [{
        type: 2,
        image_item: {
          media: uploaded.media,
          aeskey: uploaded.aesKeyHex,
          mid_size: uploaded.encryptedSize
        }
      }]
    }
  });
}

async function uploadIlinkMedia(env, botToken, toUserId, fileBytes, filename, mediaType = 1) {
  if (!(fileBytes instanceof Uint8Array)) fileBytes = new Uint8Array(fileBytes);
  if (!fileBytes.length) throw new Error('empty_image');
  const aesKeyHex = randomHex(16);
  const encrypted = await aesEcbEncryptPkcs7(fileBytes, hexToBytes(aesKeyHex));
  const filekey = randomHex(16);
  const uploadInfo = await postIlinkBotEndpoint(env, botToken, 'getuploadurl', {
    filekey,
    media_type: mediaType,
    to_user_id: toUserId,
    rawsize: fileBytes.length,
    rawfilemd5: md5HexBytes(fileBytes),
    filesize: encrypted.length,
    no_need_thumb: true,
    aeskey: aesKeyHex,
    filename
  });
  if (!isIlinkOk(uploadInfo)) throw new Error(`getuploadurl_failed:${JSON.stringify(uploadInfo).slice(0, 300)}`);
  const uploadParam = uploadInfo.upload_param || '';
  if (!uploadParam) throw new Error('missing_upload_param');

  const cdnUrl = `${ILINK_CDN_BASE}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(filekey)}`;
  const uploadResp = await fetch(cdnUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      'User-Agent': 'wx2codex-cloud/0.1'
    },
    body: encrypted
  });
  const encryptedParam = uploadResp.headers.get('x-encrypted-param') || '';
  if (!uploadResp.ok || !encryptedParam) {
    const preview = await uploadResp.text().catch(() => '');
    throw new Error(`cdn_upload_failed:${uploadResp.status}:${preview.slice(0, 200)}`);
  }
  return {
    filekey,
    aesKeyHex,
    rawSize: fileBytes.length,
    encryptedSize: encrypted.length,
    media: {
      encrypt_query_param: encryptedParam,
      aes_key: base64Encode(new TextEncoder().encode(aesKeyHex)),
      encrypt_type: 1
    }
  };
}

async function postIlinkBotEndpoint(env, botToken, endpoint, body) {
  const baseUrl = (env.ILINK_BASE_URL || 'https://ilinkai.weixin.qq.com').replace(/\/$/, '');
  const payload = { ...body, base_info: { channel_version: '1.0.3' } };
  const resp = await fetch(`${baseUrl}/ilink/bot/${endpoint.replace(/^\/+/, '')}`, {
    method: 'POST',
    headers: buildIlinkHeaders(botToken),
    body: JSON.stringify(payload)
  });
  return parseIlinkResponse(resp);
}

async function parseIlinkResponse(resp) {
  const raw = await resp.text();
  let data;
  try {
    data = raw.trim() ? JSON.parse(raw) : { ret: 0 };
  } catch (_) {
    data = { ret: -1, errmsg: raw.slice(0, 500) };
  }
  if (!resp.ok) data.http_status = resp.status;
  if (raw.trim() === '{}') data = { ret: 0 };
  return data;
}

async function fetchPaymentQrPng(codeUrl) {
  const qrApi = `https://api.qrserver.com/v1/create-qr-code/?size=360x360&margin=18&format=png&data=${encodeURIComponent(codeUrl)}`;
  const resp = await fetch(qrApi, {
    headers: { 'User-Agent': 'wx2codex-cloud/0.1' },
    cf: { cacheTtl: 0, cacheEverything: false }
  });
  if (!resp.ok) throw new Error(`qr_image_fetch_failed:${resp.status}`);
  const bytes = new Uint8Array(await resp.arrayBuffer());
  if (bytes.length < 100) throw new Error('qr_image_too_small');
  if (bytes.length > 1024 * 1024) throw new Error('qr_image_too_large');
  return bytes;
}

async function aesEcbEncryptPkcs7(plainBytes, keyBytes) {
  const padded = pkcs7Pad(plainBytes, 16);
  // WebCrypto has no AES-ECB. For each block B, AES-CTR with counter=B and
  // an all-zero plaintext block returns AES_K(B), which is exactly the ECB
  // block encryption we need. length=128 prevents counter increment effects
  // inside the single block.
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'AES-CTR' }, false, ['encrypt']);
  const zeroBlock = new Uint8Array(16);
  const out = new Uint8Array(padded.length);
  for (let offset = 0; offset < padded.length; offset += 16) {
    const block = padded.slice(offset, offset + 16);
    const encryptedBlock = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-CTR', counter: block, length: 128 }, key, zeroBlock));
    if (encryptedBlock.length !== 16) throw new Error(`unexpected_aes_block_size:${encryptedBlock.length}`);
    out.set(encryptedBlock, offset);
  }
  return out;
}

function pkcs7Pad(bytes, blockSize) {
  const padLen = blockSize - (bytes.length % blockSize || blockSize);
  const actualPadLen = padLen === 0 ? blockSize : padLen;
  const out = new Uint8Array(bytes.length + actualPadLen);
  out.set(bytes);
  out.fill(actualPadLen, bytes.length);
  return out;
}

function randomHex(byteLength) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

function hexToBytes(hex) {
  const clean = String(hex || '').replace(/\s+/g, '');
  if (clean.length % 2) throw new Error('invalid_hex');
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = Number.parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function bytesToHex(bytes) {
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}

function md5HexBytes(input) {
  const bytes = input instanceof Uint8Array ? input : new Uint8Array(input);
  const s = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21
  ];
  const k = [
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391
  ];
  const bitLen = bytes.length * 8;
  const totalLen = (((bytes.length + 8) >> 6) + 1) << 6;
  const msg = new Uint8Array(totalLen);
  msg.set(bytes);
  msg[bytes.length] = 0x80;
  const view = new DataView(msg.buffer);
  view.setUint32(totalLen - 8, bitLen >>> 0, true);
  view.setUint32(totalLen - 4, Math.floor(bitLen / 0x100000000) >>> 0, true);

  let a0 = 0x67452301;
  let b0 = 0xefcdab89;
  let c0 = 0x98badcfe;
  let d0 = 0x10325476;

  for (let offset = 0; offset < totalLen; offset += 64) {
    const m = new Array(16);
    for (let j = 0; j < 16; j += 1) m[j] = view.getUint32(offset + j * 4, true);
    let a = a0;
    let b = b0;
    let c = c0;
    let d = d0;
    for (let i = 0; i < 64; i += 1) {
      let f;
      let g;
      if (i < 16) {
        f = (b & c) | (~b & d);
        g = i;
      } else if (i < 32) {
        f = (d & b) | (~d & c);
        g = (5 * i + 1) % 16;
      } else if (i < 48) {
        f = b ^ c ^ d;
        g = (3 * i + 5) % 16;
      } else {
        f = c ^ (b | ~d);
        g = (7 * i) % 16;
      }
      const temp = d;
      d = c;
      c = b;
      b = (b + leftRotate((a + f + k[i] + m[g]) >>> 0, s[i])) >>> 0;
      a = temp;
    }
    a0 = (a0 + a) >>> 0;
    b0 = (b0 + b) >>> 0;
    c0 = (c0 + c) >>> 0;
    d0 = (d0 + d) >>> 0;
  }

  const out = new Uint8Array(16);
  const outView = new DataView(out.buffer);
  outView.setUint32(0, a0, true);
  outView.setUint32(4, b0, true);
  outView.setUint32(8, c0, true);
  outView.setUint32(12, d0, true);
  return bytesToHex(out);
}

function leftRotate(value, amount) {
  return ((value << amount) | (value >>> (32 - amount))) >>> 0;
}

function buildIlinkHeaders(botToken) {
  const randomUin = Math.floor(Math.random() * 0xffffffff).toString();
  return {
    'Content-Type': 'application/json',
    'AuthorizationType': 'ilink_bot_token',
    'Authorization': `Bearer ${botToken}`,
    'X-WECHAT-UIN': btoa(randomUin)
  };
}

function isIlinkOk(result) {
  const ret = result.ret;
  const errcode = result.errcode;
  return (ret === undefined || ret === null || ret === 0) && (errcode === undefined || errcode === null || errcode === 0);
}

function isExpiredIlinkResult(result) {
  return EXPIRED_CODES.has(Number(result.errcode)) || EXPIRED_RET_CODES.has(Number(result.ret));
}

function makeToken(prefix) {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return `${prefix}_${base64Url(bytes)}`;
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

async function aesKey(env) {
  const secret = env.WX2CODEX_CIPHER_KEY;
  if (!secret || secret.length < 16) fail(500, 'missing_cipher_key');
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret));
  return crypto.subtle.importKey('raw', digest, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

async function encryptString(env, plaintext) {
  const iv = new Uint8Array(12);
  crypto.getRandomValues(iv);
  const key = await aesKey(env);
  const encrypted = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, new TextEncoder().encode(plaintext));
  return `v1.${base64Url(iv)}.${base64Url(new Uint8Array(encrypted))}`;
}

async function decryptString(env, payload) {
  const [version, ivText, dataText] = String(payload || '').split('.');
  if (version !== 'v1' || !ivText || !dataText) fail(500, 'invalid_encrypted_payload');
  const key = await aesKey(env);
  const iv = base64UrlDecode(ivText);
  const data = base64UrlDecode(dataText);
  const decrypted = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, key, data);
  return new TextDecoder().decode(decrypted);
}

function base64Url(bytes) {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function base64UrlDecode(text) {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - text.length % 4) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
