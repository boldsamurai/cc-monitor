"""Cross-platform Claude OAuth credentials reader and Anthropic /api/oauth/usage client.

Strategy mirrors claude-hud:
  - macOS / Windows: try the system keychain first, then fall back to
    Claude Code's plain-text ~/.claude/.credentials.json (mode 0600).
  - Linux: try the file first (Claude Code's default storage there),
    then fall back to libsecret via the keyring library.

The /api/oauth/usage endpoint is undocumented and gated by the
'oauth-2025-04-20' beta header. It returns five_hour and seven_day
utilization (0..1) plus reset times. Cached locally to avoid hammering
the API on every UI tick.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logger import get_logger

log = get_logger(__name__)

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT = 3.0

API_HOST = "api.anthropic.com"
API_PATH = "/api/oauth/usage"
API_TIMEOUT = 5.0
API_BETA_HEADER = "oauth-2025-04-20"

# Anthropic applies stricter rate-limit buckets to non-Claude-Code user
# agents. Sending 'claude-code/<version>' lands us in the same bucket as
# the official client and avoids spurious 429s. See
# github.com/anthropics/claude-code/issues/30930#issuecomment by @fazxes.
_DEFAULT_CC_VERSION = "2.1.80"

CACHE_TTL_SUCCESS_S = 120  # 2 min: keeps API hits well under any sane rate limit
CACHE_TTL_FAILURE_S = 15
CACHE_TTL_FAILURE_MAX_S = 300  # cap exponential backoff at 5 minutes
KEYCHAIN_BACKOFF_S = 60


def _claude_config_dir() -> Path:
    """Return the Claude Code config directory ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".claude"


def _credentials_file() -> Path:
    return _claude_config_dir() / ".credentials.json"


def _keychain_service_names() -> list[str]:
    """Return service names to try, in priority order.

    Claude Code uses the bare 'Claude Code-credentials' service for
    ~/.claude, and appends a sha256-prefixed suffix for any other
    CLAUDE_CONFIG_DIR. We always include the bare name as a last-resort
    fallback (older Claude Code versions used it unconditionally).
    """
    config_dir = _claude_config_dir()
    default_dir = (Path.home() / ".claude").resolve()
    names: list[str] = []
    if config_dir == default_dir:
        names.append(KEYCHAIN_SERVICE)
    else:
        digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
        names.append(f"{KEYCHAIN_SERVICE}-{digest}")
        names.append(KEYCHAIN_SERVICE)
    return list(dict.fromkeys(names))  # dedupe, preserve order


# ----- credentials loaders -----


@dataclass
class Credentials:
    access_token: str
    expires_at_ms: int | None
    subscription_type: str | None
    rate_limit_tier: str | None
    refresh_token: str | None  # NEVER expose outside this process

    @property
    def is_expired(self) -> bool:
        if self.expires_at_ms is None:
            return False
        return time.time() * 1000 >= self.expires_at_ms


def _parse_credentials_blob(blob: str) -> Credentials | None:
    """Parse the JSON blob Claude Code stores (file or keychain value)."""
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None
    return Credentials(
        access_token=token,
        expires_at_ms=oauth.get("expiresAt"),
        subscription_type=oauth.get("subscriptionType"),
        rate_limit_tier=oauth.get("rateLimitTier"),
        refresh_token=oauth.get("refreshToken"),
    )


def _read_from_file() -> Credentials | None:
    path = _credentials_file()
    try:
        return _parse_credentials_blob(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_from_macos_security() -> Credentials | None:
    """macOS: shell out to /usr/bin/security to avoid blocking on D-Bus."""
    for service in _keychain_service_names():
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if result.returncode != 0:
            continue
        creds = _parse_credentials_blob(result.stdout.strip())
        if creds is not None:
            return creds
    return None


def _read_from_keyring() -> Credentials | None:
    """Use the `keyring` library (covers Windows Credential Manager,
    macOS Keychain via Security framework, Linux libsecret)."""
    try:
        import keyring  # local import keeps startup fast
    except ImportError:
        return None
    for service in _keychain_service_names():
        # Claude Code historically stores under multiple possible accounts.
        # We don't know the username up front, so we try both the well-known
        # 'default' alias and an empty string. Some keyring backends accept
        # 'None' as a wildcard via get_credential().
        try:
            cred = keyring.get_credential(service, None)
            if cred is not None and cred.password:
                parsed = _parse_credentials_blob(cred.password)
                if parsed:
                    return parsed
        except Exception:
            pass
        for account in ("default", ""):
            try:
                value = keyring.get_password(service, account)
            except Exception:
                continue
            if value:
                parsed = _parse_credentials_blob(value)
                if parsed:
                    return parsed
    return None


def read_credentials() -> Credentials | None:
    """Load Claude Code credentials from the platform-appropriate store.

    Returns None if no credentials exist (API user without OAuth, or
    Claude Code never logged in here). Does NOT check expiry — caller
    decides whether to use an expired token.
    """
    system = platform.system()
    if system == "Linux":
        return _read_from_file() or _read_from_keyring()
    if system == "Darwin":
        return _read_from_macos_security() or _read_from_file() or _read_from_keyring()
    if system == "Windows":
        return _read_from_keyring() or _read_from_file()
    # Unknown platform — try everything.
    return _read_from_file() or _read_from_keyring()


# ----- API client -----


@dataclass
class UsageWindow:
    utilization: float  # 0..100 (already a percentage as returned by Anthropic)
    resets_at: datetime


@dataclass
class UsageData:
    five_hour: UsageWindow | None
    seven_day: UsageWindow | None
    plan_name: str | None
    fetched_at: float  # epoch seconds
    api_unavailable: bool = False
    error: str | None = None
    # When the API tells us when to retry (e.g. Retry-After on 429), or when
    # we apply exponential backoff to repeated failures, this is the absolute
    # epoch second to wait until before the next call.
    retry_after_epoch: float | None = None
    # How many consecutive failures we've seen — drives exponential backoff.
    failure_count: int = 0


def _parse_iso8601(value: str) -> datetime | None:
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_usage_response(data: dict[str, Any]) -> tuple[UsageWindow | None, UsageWindow | None]:
    def parse_window(d: Any) -> UsageWindow | None:
        if not isinstance(d, dict):
            return None
        util = d.get("utilization")
        resets = d.get("resets_at")
        if util is None or resets is None:
            return None
        try:
            util_f = float(util)
        except (TypeError, ValueError):
            return None
        # API returns a percentage (0..100). Clamp defensively in case of
        # weird server responses; let actual >100% values clamp to 100 since
        # the API shouldn't legitimately return them.
        if util_f != util_f or util_f in (float("inf"), float("-inf")):
            return None
        util_f = max(0.0, min(100.0, util_f))
        ts = _parse_iso8601(resets) if isinstance(resets, str) else None
        if ts is None:
            return None
        return UsageWindow(utilization=util_f, resets_at=ts)

    return parse_window(data.get("five_hour")), parse_window(data.get("seven_day"))


def fetch_usage(access_token: str) -> tuple[UsageData | None, str | None, float | None]:
    """Make the API call. Returns (usage, error, retry_after_seconds).

    retry_after_seconds is non-None only when the server explicitly told us
    to wait (Retry-After header on 429 / 503).
    """
    url = f"https://{API_HOST}{API_PATH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": API_BETA_HEADER,
            "User-Agent": f"claude-code/{_claude_code_version()}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                retry = _retry_after(resp.headers)
                log.warning(
                    "Anthropic API non-200: status=%s retry=%s",
                    resp.status, retry,
                )
                return None, f"http-{resp.status}", retry
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        retry = _retry_after(e.headers)
        log.warning("Anthropic API HTTPError: code=%s retry=%s", e.code, retry)
        return None, f"http-{e.code}", retry
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Anthropic API network error: %s", e)
        return None, "network", None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("Anthropic API parse error: %s", e)
        return None, "parse", None

    five_hour, seven_day = _parse_usage_response(parsed)
    return (
        UsageData(
            five_hour=five_hour,
            seven_day=seven_day,
            plan_name=None,
            fetched_at=time.time(),
        ),
        None,
        None,
    )


def _claude_code_version() -> str:
    """Try to get the installed Claude Code version. Falls back to a known-
    recent version so we still land in the right rate-limit bucket."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if result.returncode == 0:
            # Output looks like '2.1.80 (Claude Code)' or '2.1.80'.
            first = result.stdout.strip().split()[0]
            if first and first[0].isdigit():
                return first
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, IndexError):
        pass
    return _DEFAULT_CC_VERSION


def _retry_after(headers: Any) -> float | None:
    """Parse a Retry-After header (RFC 7231: either delta-seconds or a
    HTTP-date). Returns the wait in seconds, or None."""
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    raw = str(raw).strip()
    # Numeric form first (most common).
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    # HTTP-date form: parse with email.utils.
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        delta = (dt - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _plan_name_from_subscription(subscription: str | None) -> str | None:
    if not subscription:
        return None
    s = subscription.lower()
    if "max" in s:
        return "Max"
    if "pro" in s:
        return "Pro"
    if "team" in s:
        return "Team"
    return subscription


# ----- cache layer -----


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "cc-monitor" / "usage-cache.json"


def _read_cache() -> UsageData | None:
    path = _cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    fetched_at = raw.get("fetched_at", 0)
    failure_count = int(raw.get("failure_count", 0))
    api_unavailable = bool(raw.get("api_unavailable"))

    # Honor an explicit Retry-After if the server gave us one.
    retry_at = raw.get("retry_after_epoch")
    if api_unavailable and retry_at is not None:
        if time.time() < retry_at:
            ttl_remaining = float(retry_at) - time.time()
        else:
            return None  # we've waited long enough; let the caller refresh
    elif api_unavailable:
        # Exponential backoff: 15s * 2^(failure_count-1), capped at 5min.
        backoff = CACHE_TTL_FAILURE_S * (2 ** max(0, failure_count - 1))
        ttl_remaining = min(backoff, CACHE_TTL_FAILURE_MAX_S) - (time.time() - fetched_at)
        if ttl_remaining <= 0:
            return None
    else:
        age = time.time() - fetched_at
        if age >= CACHE_TTL_SUCCESS_S:
            return None

    def _to_window(d: Any) -> UsageWindow | None:
        if not isinstance(d, dict):
            return None
        try:
            return UsageWindow(
                utilization=float(d["utilization"]),
                resets_at=datetime.fromisoformat(d["resets_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    return UsageData(
        five_hour=_to_window(raw.get("five_hour")),
        seven_day=_to_window(raw.get("seven_day")),
        plan_name=raw.get("plan_name"),
        fetched_at=fetched_at,
        api_unavailable=api_unavailable,
        error=raw.get("error"),
        retry_after_epoch=retry_at,
        failure_count=failure_count,
    )


def _write_cache(data: UsageData) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "fetched_at": data.fetched_at,
            "plan_name": data.plan_name,
            "api_unavailable": data.api_unavailable,
            "error": data.error,
            "retry_after_epoch": data.retry_after_epoch,
            "failure_count": data.failure_count,
        }
        if data.five_hour:
            payload["five_hour"] = {
                "utilization": data.five_hour.utilization,
                "resets_at": data.five_hour.resets_at.isoformat(),
            }
        if data.seven_day:
            payload["seven_day"] = {
                "utilization": data.seven_day.utilization,
                "resets_at": data.seven_day.resets_at.isoformat(),
            }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


# ----- public entry point -----


def get_usage(force_refresh: bool = False) -> UsageData | None:
    """Top-level: returns usage or None.

    Returns None when there are no credentials at all (API user) or the
    token has expired and we have no cached data. Returns UsageData with
    api_unavailable=True when the API call failed but we want callers to
    surface a warning.
    """
    cached = _read_cache()
    if cached is not None and not force_refresh:
        log.debug(
            "usage cache hit: 5h=%s%% 7d=%s%% (api_unavailable=%s)",
            cached.five_hour.utilization if cached.five_hour else "n/a",
            cached.seven_day.utilization if cached.seven_day else "n/a",
            cached.api_unavailable,
        )
        return cached
    log.info("usage cache miss / refresh — calling Anthropic API")

    # Track running failure count across calls so exponential backoff has
    # something to anchor to. We read the most recent cache entry even when
    # it has 'expired' (TTL-wise) just to learn the streak length.
    prev_failures = 0
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
        prev_failures = int(raw.get("failure_count", 0))
    except (OSError, ValueError):
        pass

    creds = read_credentials()
    if creds is None:
        return None
    if creds.is_expired:
        # Don't try to call the API with an expired token; let the caller
        # decide whether to fall back or just surface a warning.
        result = UsageData(
            five_hour=None,
            seven_day=None,
            plan_name=_plan_name_from_subscription(creds.subscription_type),
            fetched_at=time.time(),
            api_unavailable=True,
            error="token-expired",
            failure_count=prev_failures + 1,
        )
        _write_cache(result)
        return result

    data, err, retry_after = fetch_usage(creds.access_token)
    if data is None:
        retry_at = (time.time() + retry_after) if retry_after else None
        result = UsageData(
            five_hour=None,
            seven_day=None,
            plan_name=_plan_name_from_subscription(creds.subscription_type),
            fetched_at=time.time(),
            api_unavailable=True,
            error=err,
            retry_after_epoch=retry_at,
            failure_count=prev_failures + 1,
        )
        _write_cache(result)
        return result

    data.plan_name = _plan_name_from_subscription(creds.subscription_type)
    data.failure_count = 0  # reset on success
    _write_cache(data)
    return data
