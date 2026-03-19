#!/usr/bin/env python3
"""
NEAR Market AutoBot v1
- Smart proposal selection by job tags
- Full lifecycle handling: discover -> bid/entry -> accepted -> submit
- Service Registry: registers as a service for instant jobs
- Assignment messaging: private communication with job creators
- Optional auto-submit for accepted bids via Claude API
- Retry/backoff networking, pagination, and dry-run mode
- Persistent state via SQLite (default) with JSON fallback
- Stats tracking with win rate analytics
- Job memory: learns which job types win more often
- Revision handling: responds to request-changes automatically
- Health dashboard: writes status.json every cycle
"""

import copy
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ========================
# CONFIG
# ========================
NEAR_API_KEY = os.environ.get("NEAR_KEY", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_KEY", "")
_LLM_KEY = OPENROUTER_API_KEY or CLAUDE_API_KEY
_LLM_USE_OPENROUTER = bool(OPENROUTER_API_KEY)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
AGENT_ID = os.environ.get("AGENT_ID", "")
NPM_TOKEN = os.environ.get("NPM_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
NEAR_DEPLOY_ACCOUNT = os.environ.get("NEAR_DEPLOY_ACCOUNT", "e2248.testnet")


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


USE_SQLITE = parse_bool(os.environ.get("USE_SQLITE", "true"), default=True)
DRY_RUN = parse_bool(os.environ.get("DRY_RUN", "false"), default=False)

REQUEST_MAX_RETRIES = int(os.environ.get("REQUEST_MAX_RETRIES", "4"))
REQUEST_BACKOFF_BASE = float(os.environ.get("REQUEST_BACKOFF_BASE", "1.0"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))
MAX_ACTIONS_PER_CYCLE = int(os.environ.get("MAX_ACTIONS_PER_CYCLE", "100"))
MIN_JOB_SCORE = float(os.environ.get("MIN_JOB_SCORE", "0.25"))
MAX_REVISIONS = int(os.environ.get("MAX_REVISIONS", "3"))
ENABLE_SERVICES = parse_bool(os.environ.get("ENABLE_SERVICES", "true"), default=True)

# Service registration config
SERVICE_NAME = os.environ.get("SERVICE_NAME", "NEAR AutoBot - AI Agent")
SERVICE_DESCRIPTION = os.environ.get(
    "SERVICE_DESCRIPTION",
    "Autonomous AI agent for code review, research, documentation, content creation, "
    "and blockchain development. Powered by Claude Sonnet 4.6. Delivers high-quality "
    "work with smart formatting, revision handling, and fast turnaround."
)
SERVICE_CATEGORY = os.environ.get("SERVICE_CATEGORY", "development")
SERVICE_PRICE = float(os.environ.get("SERVICE_PRICE", "3.5"))
SERVICE_TAGS = os.environ.get("SERVICE_TAGS", "code-review,research,documentation,near,ai-agent,blockchain,api,sdk").split(",")
SERVICE_RESPONSE_TIME = int(os.environ.get("SERVICE_RESPONSE_TIME", "3600"))

# Auto-withdraw settings
AUTO_WITHDRAW = parse_bool(os.environ.get("AUTO_WITHDRAW", "false"), default=False)
AUTO_WITHDRAW_THRESHOLD = float(os.environ.get("AUTO_WITHDRAW_THRESHOLD", "10.0"))
AUTO_WITHDRAW_TO = os.environ.get("AUTO_WITHDRAW_TO", "")  # NEAR account to withdraw to

# Dispute / unpaid alerts
DISPUTE_AUTO = parse_bool(os.environ.get("DISPUTE_AUTO", "false"), default=False)
DISPUTE_AFTER_HOURS = int(os.environ.get("DISPUTE_AFTER_HOURS", "24"))
UNPAID_ALERT_HOURS = float(os.environ.get("UNPAID_ALERT_HOURS", "6"))

# Additional services to register for different categories
EXTRA_SERVICES = [
    {
        "name": "NEAR AutoBot - Research & Writing",
        "description": "AI agent for research, technical writing, blog posts, documentation, and content creation. Fast, high-quality delivery.",
        "category": "writing",
        "price": 3.5,
        "tags": ["research", "writing", "content", "blog", "documentation", "technical-writing", "analysis"],
    },
    {
        "name": "NEAR AutoBot - Healthcare & Medical",
        "description": "AI agent for medical coding, claims analysis, healthcare documentation, and clinical data processing.",
        "category": "healthcare",
        "price": 5.0,
        "tags": ["healthcare", "medical", "claims", "coding", "analysis", "bcode", "insurance"],
    },
]

# Job filters
MAX_BID_COUNT = 50
MIN_BUDGET = 1.0
MAX_BUDGET = 9999.0

# Tags skipped by default (typically non-text-heavy work)
SKIP_TAGS = [
    "video", "image", "meme", "art", "design", "logo",
    "animation", "photo", "illustration", "graphic", "music", "audio",
]

# Keywords in title/description that indicate the job requires real-world actions
# (sign up on sites, create accounts, post on social media, etc.)
# Bot cannot do these — skip them to avoid disputes
SKIP_ACTION_KEYWORDS = [
    "sign up", "signup", "create account", "create your account",
    "register on", "register at",
    # Twitter/X — нет API
    "post on twitter", "post on x.com", "tweet from", "post a tweet",
    "twitter thread", "post to twitter", "post to x", "twitter account",
    "x.com account", "tweet about", "twitter post",
    # Google — нет OAuth
    "google docs", "google sheet", "google drive", "google form",
    "create a google", "share on google", "post on google",
    "google workspace", "gmail",
    # Прочее
    "follow us on", "like and retweet", "repost on",
    "fill out form", "fill the form", "submit application",
    "record video", "record a video", "take screenshot",
    "download and install", "install the app",
]
ONLY_TAGS: List[str] = []

# Blacklisted client IDs — known bad actors (test agents, non-paying clients)
# Checks against any of: created_by, requester_id, owner_id, account_id fields
BLOCKED_CLIENT_IDS: List[str] = [
    "ed3eec9a",  # test_job_poster — creates fake jobs, never pays
    "5cdaee04",  # dispute abuser — wins disputes despite not reviewing submissions
]

# Timing
CHECK_INTERVAL = 300
BID_DELAY = 0.5

# Auto-submit
AUTO_SUBMIT = True
SUBMIT_DELAY = 10

STATE_FILE = os.path.expanduser("~/near-market-bot/state.json")
STATE_DB_FILE = os.path.expanduser("~/near-market-bot/state.db")
LOG_FILE = os.path.expanduser("~/near-market-bot/autobot.log")
CONFIG_FILE = os.path.expanduser("~/near-market-bot/config.json")
STATUS_FILE = os.path.expanduser("~/near-market-bot/status.json")

if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    MAX_BID_COUNT = cfg.get("MAX_BID_COUNT", MAX_BID_COUNT)
    MIN_BUDGET = cfg.get("MIN_BUDGET", MIN_BUDGET)
    MAX_BUDGET = cfg.get("MAX_BUDGET", MAX_BUDGET)
    CHECK_INTERVAL = cfg.get("CHECK_INTERVAL", CHECK_INTERVAL)
    AUTO_SUBMIT = cfg.get("AUTO_SUBMIT", AUTO_SUBMIT)
    SKIP_TAGS = cfg.get("SKIP_TAGS", SKIP_TAGS)
    ONLY_TAGS = cfg.get("ONLY_TAGS", ONLY_TAGS)

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger(__name__)

NEAR_HEADERS = {
    "Authorization": f"Bearer {NEAR_API_KEY}",
    "Content-Type": "application/json",
}

DEFAULT_STATE = {
    "bid_jobs": [],
    "bid_statuses": {},
    "submitted_jobs": [],
    "competition_entries": [],
    "competition_skipped": [],
    "operation_statuses": {},
    "stats": {
        "total_bids": 0,
        "total_entries": 0,
        "accepted": 0,
        "rejected": 0,
        "completed": 0,
        "submitted": 0,
        "revisions_handled": 0,
        "revenue_near": 0.0,
        "started_at": "",
    },
    "job_memory": {},
    "revision_counts": {},
    "service_id": None,
    "extra_service_ids": {},
    "last_total_earned": 0.0,
    "last_jobs_completed": 0,
    "last_daily_summary": "",
    "last_profile_update": "",
    "notified_skip_jobs": [],
    "accepted_timestamps": {},
    "unpaid_notified": [],
}


# ========================
# PROPOSALS
# ========================
PROPOSAL_TEMPLATES = {
    "code": (
        "Expert AI agent specializing in blockchain development and NEAR Protocol. "
        "I deliver clean, tested, production-ready code with documentation. "
        "Proficient in Rust, Python, JavaScript, NEAR SDK, and AI agent development."
    ),
    "docs": (
        "Technical writer and documentation specialist for Web3. "
        "I create clear, structured documentation with examples and best practices. "
        "Experienced with NEAR Protocol, AI agents, and developer tooling."
    ),
    "research": (
        "AI research agent with deep analytical capabilities. "
        "I provide comprehensive, well-sourced research with actionable insights. "
        "Specialized in blockchain ecosystems, AI/ML applications, and market analysis."
    ),
    "marketing": (
        "Marketing and content creation specialist for Web3/blockchain. "
        "I craft compelling narratives, engaging copy, and social media content. "
        "Experienced in NEAR Protocol ecosystem promotion and community building."
    ),
    "near": (
        "NEAR Protocol specialist with deep ecosystem knowledge. "
        "I understand NEAR's architecture, tooling, and community standards. "
        "Ready to deliver high-quality work that meets NEAR ecosystem requirements."
    ),
    "default": (
        "Expert AI agent delivering high-quality work on market.near.ai. "
        "Specialized in NEAR Protocol, blockchain development, technical writing, "
        "AI agent architecture, and research. Fast, reliable, professional delivery."
    ),
}


def get_proposal(tags: List[str]) -> str:
    tag_lower = {t.lower() for t in (tags or [])}
    if tag_lower & {"rust", "python", "javascript", "solidity", "code", "smart-contract", "nearai", "agent", "sdk", "api"}:
        return PROPOSAL_TEMPLATES["code"]
    if tag_lower & {"docs", "documentation", "technical-writing", "writing", "content", "tutorial"}:
        return PROPOSAL_TEMPLATES["docs"]
    if tag_lower & {"research", "analysis", "data", "report", "survey"}:
        return PROPOSAL_TEMPLATES["research"]
    if tag_lower & {"marketing", "social", "twitter", "x", "community", "promotion", "tweet"}:
        return PROPOSAL_TEMPLATES["marketing"]
    if tag_lower & {"near", "blockchain", "web3", "crypto", "defi", "nft"}:
        return PROPOSAL_TEMPLATES["near"]
    return PROPOSAL_TEMPLATES["default"]


def _tag_fit_score(tags: List[str]) -> float:
    tag_lower = {t.lower() for t in (tags or [])}
    score = 0.0
    if tag_lower & {"rust", "python", "javascript", "code", "agent", "api", "sdk"}:
        score += 1.0
    if tag_lower & {"docs", "documentation", "research", "analysis"}:
        score += 0.8
    if tag_lower & {"near", "web3", "blockchain"}:
        score += 0.6
    if tag_lower & {"marketing", "content", "social", "twitter", "x"}:
        score += 0.5
    return min(score / 1.6, 1.0)


def _job_memory_boost(job: Dict[str, Any], job_memory: Dict[str, Any]) -> float:
    """Boost score for job types that historically win more often."""
    tags = {t.lower() for t in (job.get("tags") or [])}
    if not tags or not job_memory:
        return 0.0

    boosts = []
    for tag in tags:
        mem = job_memory.get(tag)
        if mem and isinstance(mem, dict):
            wins = mem.get("wins", 0)
            total = mem.get("total", 0)
            if total >= 3:
                win_rate = wins / total
                boosts.append(win_rate * 0.10)

    return max(boosts) if boosts else 0.0


def _has_requester_balance(job: Dict[str, Any]) -> bool:
    """Return False only if we can confirm requester has zero balance (no funds)."""
    for field in ("requester_balance", "budget_locked", "escrow_amount", "client_balance"):
        val = job.get(field)
        if val is not None:
            try:
                return float(val) > 0
            except (ValueError, TypeError):
                pass
    return True  # unknown — allow


def job_requires_real_action(job: Dict[str, Any]) -> bool:
    """Check if job requires real-world actions the bot cannot perform."""
    title = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    text = title + " " + description
    for kw in SKIP_ACTION_KEYWORDS:
        if kw in text:
            return True
    return False


def score_job(job: Dict[str, Any], job_memory: Optional[Dict[str, Any]] = None) -> float:
    budget = float(job.get("budget_amount") or 0)
    bid_count = int(job.get("bid_count") or 0)
    tags = job.get("tags", [])
    is_competition = job.get("job_type") == "competition"

    budget_score = min(budget / max(MIN_BUDGET, 1.0), 10.0) / 10.0
    pressure_score = 1.0 - min(bid_count / max(MAX_BID_COUNT, 1), 1.0)
    tag_score = _tag_fit_score(tags)
    competition_bonus = 0.15 if is_competition else 0.0
    memory_bonus = _job_memory_boost(job, job_memory or {})
    return round(
        (0.45 * budget_score) + (0.35 * pressure_score) + (0.20 * tag_score)
        + competition_bonus + memory_bonus,
        4,
    )


def choose_bid_terms(job: Dict[str, Any], score: float) -> Tuple[str, int]:
    budget = float(job.get("budget_amount") or 0)
    tags = {t.lower() for t in (job.get("tags") or [])}

    # Bid as % of budget — competitive pricing
    if score >= 0.9:
        bid_pct = 0.90
    elif score >= 0.75:
        bid_pct = 0.85
    else:
        bid_pct = 0.80

    amount = max(1.0, round(budget * bid_pct, 1))
    amount = min(amount, budget)

    eta_seconds = 3600
    if tags & {"rust", "python", "javascript", "code", "sdk", "api"}:
        eta_seconds = 7200
    elif tags & {"docs", "documentation", "research", "analysis"}:
        eta_seconds = 5400
    elif tags & {"marketing", "social", "twitter", "x"}:
        eta_seconds = 3600

    if score >= 0.9:
        eta_seconds = max(1800, int(eta_seconds * 0.8))

    amount_str = str(int(amount)) if float(amount).is_integer() else f"{amount:.1f}"
    return amount_str, eta_seconds


def op_key(kind: str, job_id: str) -> str:
    return f"{kind}:{job_id}"


def op_status(state: Dict[str, Any], key: str) -> str:
    return str(state.get("operation_statuses", {}).get(key, ""))


def set_op_status(state: Dict[str, Any], key: str, status: str) -> None:
    state.setdefault("operation_statuses", {})[key] = status


def update_job_memory(state: Dict[str, Any], tags: List[str], won: bool) -> None:
    """Track win/loss per tag for adaptive scoring."""
    memory = state.setdefault("job_memory", {})
    for tag in (tags or []):
        tag_lower = tag.lower()
        if tag_lower not in memory:
            memory[tag_lower] = {"wins": 0, "losses": 0, "total": 0}
        memory[tag_lower]["total"] += 1
        if won:
            memory[tag_lower]["wins"] += 1
        else:
            memory[tag_lower]["losses"] += 1


def bump_stat(state: Dict[str, Any], key: str, amount: float = 1) -> None:
    stats = state.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + amount


# ========================
# RETRY/HTTP HELPERS
# ========================
def should_retry_http(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def backoff_delay(attempt_index: int) -> float:
    return REQUEST_BACKOFF_BASE * (2 ** attempt_index)


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[int] = None,
    expected_statuses: Optional[set] = None,
) -> Dict[str, Any]:
    expected = expected_statuses or {200}

    for attempt in range(REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                timeout=timeout or REQUEST_TIMEOUT,
            )

            if response.status_code in expected:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        return data
                    return {"data": data}
                except Exception:
                    return {"ok": True}

            if should_retry_http(response.status_code) and attempt < REQUEST_MAX_RETRIES:
                time.sleep(backoff_delay(attempt))
                continue

            try:
                data = response.json()
                if isinstance(data, dict):
                    data.setdefault("error", f"http {response.status_code}")
                    return data
            except Exception:
                pass
            return {"error": f"http {response.status_code}: {response.text[:300]}"}

        except requests.RequestException as e:
            if attempt < REQUEST_MAX_RETRIES:
                time.sleep(backoff_delay(attempt))
                continue
            return {"error": str(e)}

    return {"error": "unexpected request failure"}


def request_list(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
    expected_statuses: Optional[set] = None,
) -> List[Dict[str, Any]]:
    expected = expected_statuses or {200}

    for attempt in range(REQUEST_MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                timeout=timeout or REQUEST_TIMEOUT,
            )

            if response.status_code in expected:
                data = response.json()
                if isinstance(data, list):
                    return data
                return []

            if should_retry_http(response.status_code) and attempt < REQUEST_MAX_RETRIES:
                time.sleep(backoff_delay(attempt))
                continue

            return []

        except requests.RequestException:
            if attempt < REQUEST_MAX_RETRIES:
                time.sleep(backoff_delay(attempt))
                continue
            return []

    return []


# ========================
# TELEGRAM
# ========================
def tg_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    _ = request_json(
        "POST",
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        payload=payload,
        expected_statuses={200},
        timeout=10,
    )


# ========================
# STATE STORAGE
# ========================
def _ensure_state_shape(state: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_STATE)
    merged.update(state or {})
    for key in ("bid_jobs", "submitted_jobs", "competition_entries", "competition_skipped"):
        if not isinstance(merged.get(key), list):
            merged[key] = []
    if not isinstance(merged.get("bid_statuses"), dict):
        merged["bid_statuses"] = {}
    if not isinstance(merged.get("operation_statuses"), dict):
        merged["operation_statuses"] = {}
    if not isinstance(merged.get("stats"), dict):
        merged["stats"] = dict(DEFAULT_STATE["stats"])
    if not isinstance(merged.get("job_memory"), dict):
        merged["job_memory"] = {}
    if not isinstance(merged.get("revision_counts"), dict):
        merged["revision_counts"] = {}
    if "service_id" not in merged:
        merged["service_id"] = None
    return merged


def _sqlite_load_state() -> Dict[str, Any]:
    os.makedirs(os.path.dirname(STATE_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(STATE_DB_FILE)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = conn.execute("SELECT value FROM bot_state WHERE key = 'global'").fetchone()
        if not row:
            return copy.deepcopy(DEFAULT_STATE)
        return _ensure_state_shape(json.loads(row[0]))
    finally:
        conn.close()


def _sqlite_save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_DB_FILE), exist_ok=True)
    payload = json.dumps(_ensure_state_shape(state), ensure_ascii=False)
    conn = sqlite3.connect(STATE_DB_FILE)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO bot_state(key, value) VALUES('global', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (payload,),
        )
        conn.commit()
    finally:
        conn.close()


def _json_load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return _ensure_state_shape(json.load(f))
    return copy.deepcopy(DEFAULT_STATE)


def _json_save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_ensure_state_shape(state), f, indent=2, ensure_ascii=False)


def load_state() -> Dict[str, Any]:
    if USE_SQLITE:
        try:
            return _sqlite_load_state()
        except Exception as e:
            log.error(f"SQLite load failed, fallback to JSON: {e}")
    return _json_load_state()


def save_state(state: Dict[str, Any]) -> None:
    if USE_SQLITE:
        try:
            _sqlite_save_state(state)
            return
        except Exception as e:
            log.error(f"SQLite save failed, fallback to JSON: {e}")
    _json_save_state(state)


# ========================
# WALLET
# ========================
def check_wallet_balance() -> Optional[float]:
    """Fetch earned NEAR balance from Verifier contract (job earnings)."""
    data = request_json(
        "GET",
        "https://market.near.ai/v1/wallet/balance",
        headers=NEAR_HEADERS,
        timeout=10,
        expected_statuses={200},
    )
    if data.get("error"):
        log.warning(f"Wallet balance check failed: {data['error']}")
        return None
    # Prefer earned balance from Verifier (balances array) — top-level "balance" is native gas only
    for b in (data.get("balances") or []):
        sym = (b.get("symbol") or "").upper()
        tid = (b.get("token_id") or "")
        if sym == "NEAR" or "wrap.near" in tid:
            try:
                return float(b["balance"])
            except (ValueError, TypeError, KeyError):
                pass
    # Fallback: top-level balance (native NEAR)
    native = data.get("balance") or data.get("amount") or data.get("data")
    if native is not None:
        try:
            return float(native)
        except (ValueError, TypeError):
            pass
    log.info(f"Wallet response: {json.dumps(data)[:200]}")
    return None


# ========================
# HEALTH DASHBOARD
# ========================
def write_health_status(state: Dict[str, Any], cycle_count: int, wallet_balance: Optional[float] = None) -> None:
    """Write status.json with live bot stats."""
    stats = state.get("stats", {})
    total_bids = stats.get("total_bids", 0)
    accepted = stats.get("accepted", 0)
    rejected = stats.get("rejected", 0)
    completed = stats.get("completed", 0)

    win_rate = 0.0
    decided = accepted + rejected
    if decided > 0:
        win_rate = round(accepted / decided * 100, 1)

    status = {
        "bot_version": "v1",
        "model": "claude-sonnet-4-6",
        "uptime_cycles": cycle_count,
        "last_cycle": datetime.now(timezone.utc).isoformat(),
        "wallet_balance_near": wallet_balance,
        "stats": {
            "total_bids": total_bids,
            "total_entries": stats.get("total_entries", 0),
            "accepted": accepted,
            "rejected": rejected,
            "completed": completed,
            "submitted": stats.get("submitted", 0),
            "revisions_handled": stats.get("revisions_handled", 0),
            "revenue_near": stats.get("revenue_near", 0.0),
            "win_rate_pct": win_rate,
        },
        "job_memory_tags": len(state.get("job_memory", {})),
        "state_store": "SQLite" if USE_SQLITE else "JSON",
        "mode": "DRY-RUN" if DRY_RUN else "LIVE",
    }

    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Failed to write status.json: {e}")


# ========================
# AGENT EARNINGS MONITOR
# ========================
def check_agent_earnings(state: Dict[str, Any]) -> None:
    """Check agent total_earned and jobs_completed, notify on changes."""
    if not AGENT_ID:
        return
    data = request_json(
        "GET",
        f"https://market.near.ai/v1/agents/{AGENT_ID}",
        headers=NEAR_HEADERS,
        timeout=10,
        expected_statuses={200},
    )
    if data.get("error"):
        return

    total_earned = float(data.get("total_earned") or 0)
    jobs_completed = int(data.get("jobs_completed") or 0)

    last_earned = float(state.get("last_total_earned") or 0)
    last_completed = int(state.get("last_jobs_completed") or 0)

    if total_earned > last_earned:
        earned_diff = total_earned - last_earned
        tg_send(
            f"💸 <b>Payment received!</b>\n"
            f"+{earned_diff:.2f} NEAR\n"
            f"Total earned: {total_earned:.2f} NEAR\n"
            f"Jobs completed: {jobs_completed}"
        )
        log.info(f"Earnings update: +{earned_diff:.2f} NEAR, total={total_earned:.2f}")
        state["last_total_earned"] = total_earned

    if jobs_completed > last_completed:
        new_count = jobs_completed - last_completed
        if total_earned <= last_earned:  # avoid duplicate if already notified above
            tg_send(
                f"🏆 <b>Job accepted by client!</b>\n"
                f"+{new_count} job(s) completed\n"
                f"Total completed: {jobs_completed}"
            )
        state["last_jobs_completed"] = jobs_completed
        # Immediately refresh service profile with new count
        refresh_service_descriptions(state, jobs_completed)


def send_daily_summary(state: Dict[str, Any], balance: Optional[float]) -> None:
    """Send daily stats summary to Telegram."""
    stats = state.get("stats", {})
    total_bids = stats.get("total_bids", 0)
    accepted = stats.get("accepted", 0)
    rejected = stats.get("rejected", 0)
    completed = state.get("last_jobs_completed", stats.get("completed", 0))
    submitted = stats.get("submitted", 0)
    revenue = state.get("last_total_earned", stats.get("revenue_near", 0.0))

    decided = accepted + rejected
    win_rate = round(accepted / decided * 100, 1) if decided > 0 else 0.0
    balance_str = f"{balance:.2f} NEAR" if balance is not None else "unavailable"

    tg_send(
        f"📊 <b>Daily Summary</b>\n"
        f"💰 Wallet balance: {balance_str}\n"
        f"📈 Total earned: {revenue:.2f} NEAR\n"
        f"🎯 Bids placed: {total_bids}\n"
        f"✅ Accepted: {accepted} ({win_rate}% win rate)\n"
        f"📦 Submitted: {submitted}\n"
        f"🏆 Completed: {completed}\n"
        f"❌ Rejected: {rejected}"
    )
    state["last_daily_summary"] = datetime.now(timezone.utc).isoformat()


# ========================
# AUTO-WITHDRAW
# ========================
def auto_withdraw_if_needed(balance: Optional[float]) -> None:
    """If balance exceeds threshold, withdraw excess to AUTO_WITHDRAW_TO account."""
    if not AUTO_WITHDRAW or not AUTO_WITHDRAW_TO:
        return
    if balance is None or balance < AUTO_WITHDRAW_THRESHOLD:
        return
    amount = round(balance - 1.0, 2)  # keep 1 NEAR for fees
    if amount <= 0:
        return
    log.info(f"AUTO-WITHDRAW: {amount:.2f} NEAR -> {AUTO_WITHDRAW_TO}")
    result = request_json(
        "POST",
        "https://market.near.ai/v1/wallet/withdraw",
        headers=NEAR_HEADERS,
        payload={"to_account_id": AUTO_WITHDRAW_TO, "amount": str(amount), "token_id": "NEAR",
                  "idempotency_key": f"auto-withdraw-{int(time.time() * 1000)}"},
        expected_statuses={200, 201},
        timeout=20,
    )
    if result.get("error"):
        log.error(f"AUTO-WITHDRAW failed: {result['error']}")
        tg_send(f"⚠️ <b>Auto-withdraw failed</b>\n{tg_escape(result['error'])}")
    else:
        tg_send(
            f"💸 <b>Auto-withdraw done</b>\n"
            f"{amount:.2f} NEAR → <code>{tg_escape(AUTO_WITHDRAW_TO)}</code>"
        )


# ========================
# UNPAID ALERTS + DISPUTES
# ========================
def open_dispute(job_id: str, reason: str = "") -> Dict[str, Any]:
    """Open a dispute for a job where payment was not received."""
    if DRY_RUN:
        log.info(f"DRY-RUN DISPUTE: {job_id[:8]}")
        return {"ok": True, "dry_run": True}
    payload = {"reason": reason or "Work was completed and accepted, but payment has not been received."}
    for endpoint in [
        f"https://market.near.ai/v1/jobs/{job_id}/dispute",
        f"https://market.near.ai/v1/jobs/{job_id}/claim",
    ]:
        result = request_json(
            "POST", endpoint, headers=NEAR_HEADERS, payload=payload,
            expected_statuses={200, 201}, timeout=15,
        )
        if not result.get("error"):
            return result
    return result


def check_unpaid_accepted_jobs(state: Dict[str, Any]) -> None:
    """Alert (and optionally dispute) if accepted jobs haven't paid after threshold hours."""
    accepted_ts = state.get("accepted_timestamps", {})
    if not accepted_ts:
        return

    notified = state.setdefault("unpaid_notified", [])
    disputed = state.setdefault("disputed_jobs", [])
    now = datetime.now(timezone.utc)

    for job_id, ts_str in list(accepted_ts.items()):
        try:
            accepted_time = datetime.fromisoformat(ts_str)
            if accepted_time.tzinfo is None:
                accepted_time = accepted_time.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        elapsed_hours = (now - accepted_time).total_seconds() / 3600

        if elapsed_hours >= UNPAID_ALERT_HOURS and job_id not in notified:
            notified.append(job_id)
            tg_send(
                f"⏰ <b>Принятая работа без выплаты!</b>\n"
                f"Job: <code>{job_id}</code>\n"
                f"Принято {elapsed_hours:.0f}ч назад — оплата не поступила\n"
                f"🔗 <a href='https://market.near.ai/jobs/{job_id}'>Проверить вручную</a>"
            )
            log.info(f"UNPAID ALERT: {job_id[:8]} accepted {elapsed_hours:.0f}h ago")

        if DISPUTE_AUTO and elapsed_hours >= DISPUTE_AFTER_HOURS and job_id not in disputed:
            disputed.append(job_id)
            result = open_dispute(job_id)
            if result.get("error"):
                tg_send(
                    f"⚠️ <b>Dispute failed</b>\nJob: <code>{job_id}</code>\n"
                    f"Error: {tg_escape(result['error'])}"
                )
            else:
                tg_send(f"⚖️ <b>Dispute opened</b>\nJob: <code>{job_id}</code>")
            log.info(f"DISPUTE: opened for {job_id[:8]}")

    if len(notified) > 200:
        state["unpaid_notified"] = notified[-100:]
    if len(disputed) > 200:
        state["disputed_jobs"] = disputed[-100:]


# ========================
# TELEGRAM COMMANDS
# ========================
_tg_last_update_id: int = 0
_tg_state_ref: List[Dict[str, Any]] = []


def _rotate_api_key() -> str:
    result = request_json(
        "POST",
        "https://market.near.ai/v1/agents/rotate-key",
        headers=NEAR_HEADERS,
        expected_statuses={200, 201},
        timeout=15,
    )
    if result.get("error"):
        return f"❌ Key rotation failed: {tg_escape(result['error'])}"
    new_key = result.get("api_key") or result.get("key") or result.get("token") or ""
    if new_key:
        return f"🔑 <b>Key rotated!</b>\nNew key: <code>{tg_escape(new_key)}</code>\nUpdate NEAR_KEY in ~/.bashrc"
    return "🔑 Key rotation requested — check dashboard for new key"


def handle_tg_command(text: str) -> str:
    state = _tg_state_ref[0] if _tg_state_ref else {}
    cmd_parts = text.strip().split()
    cmd = cmd_parts[0].lower()

    if cmd == "/rotatekey":
        return _rotate_api_key()

    if cmd == "/status":
        stats = state.get("stats", {})
        decided = stats.get("accepted", 0) + stats.get("rejected", 0)
        win_rate = round(stats.get("accepted", 0) / decided * 100, 1) if decided > 0 else 0.0
        return (
            f"📊 <b>Bot status</b>\n"
            f"Bids: {stats.get('total_bids', 0)}\n"
            f"Accepted: {stats.get('accepted', 0)} ({win_rate}%)\n"
            f"Submitted: {stats.get('submitted', 0)}\n"
            f"Completed: {state.get('last_jobs_completed', 0)}\n"
            f"Earned: {state.get('last_total_earned', 0.0):.2f} NEAR\n"
            f"Job memory: {len(state.get('job_memory', {}))} tags"
        )

    if cmd == "/balance":
        bal = check_wallet_balance()
        bal_str = f"{bal:.4f} NEAR" if bal is not None else "unavailable"
        return f"💰 Wallet balance: {bal_str}"

    if cmd == "/dispute" and len(cmd_parts) >= 2:
        job_id = cmd_parts[1]
        result = open_dispute(job_id, "Manually triggered dispute from bot operator.")
        if result.get("error"):
            return f"❌ Dispute failed: {tg_escape(result['error'])}"
        return f"⚖️ Dispute opened for <code>{tg_escape(job_id)}</code>"

    if cmd == "/withdraw" and len(cmd_parts) >= 2:
        target = cmd_parts[1]
        bal = check_wallet_balance()
        if bal is None or bal <= 1:
            return "❌ Balance too low to withdraw"
        amount = round(bal - 1.0, 2)
        result = request_json(
            "POST", "https://market.near.ai/v1/wallet/withdraw",
            headers=NEAR_HEADERS,
            payload={"to_account_id": target, "amount": str(amount), "token_id": "NEAR",
                     "idempotency_key": f"manual-withdraw-{int(time.time() * 1000)}"},
            expected_statuses={200, 201}, timeout=20,
        )
        if result.get("error"):
            return f"❌ Withdraw failed: {tg_escape(result['error'])}"
        return f"💸 Withdrew {amount:.2f} NEAR → <code>{tg_escape(target)}</code>"

    if cmd == "/help":
        return (
            "🤖 <b>Bot commands</b>\n"
            "/status — current stats\n"
            "/balance — wallet balance\n"
            "/rotatekey — rotate API key\n"
            "/dispute &lt;job_id&gt; — open dispute for job\n"
            "/withdraw &lt;account.near&gt; — withdraw balance\n"
            "/help — this message"
        )

    return f"❓ Unknown command: {tg_escape(cmd)}\nSend /help for commands."


def _tg_command_listener() -> None:
    """Poll Telegram for incoming commands from the owner chat."""
    global _tg_last_update_id
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    log.info("TG command listener started")
    while True:
        try:
            data = request_json(
                "GET",
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
                f"?offset={_tg_last_update_id + 1}&timeout=30",
                timeout=35,
                expected_statuses={200},
            )
            updates = data.get("result") or []
            if isinstance(updates, list):
                for upd in updates:
                    _tg_last_update_id = max(_tg_last_update_id, upd.get("update_id", 0))
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    if chat_id == str(TG_CHAT_ID) and text.startswith("/"):
                        log.info(f"TG CMD: {text}")
                        reply = handle_tg_command(text)
                        tg_send(reply)
        except Exception as e:
            log.warning(f"TG command listener error: {e}")
        time.sleep(5)


# ========================
# SERVICE REGISTRY
# ========================
def register_service(state: Dict[str, Any]) -> Optional[str]:
    """Register bot as a service on market.near.ai for instant jobs."""
    existing_id = state.get("service_id")
    if existing_id:
        log.info(f"Service already registered: {existing_id[:8]}")
        return existing_id

    if DRY_RUN:
        log.info("DRY-RUN: Would register service")
        return None

    payload = {
        "name": SERVICE_NAME,
        "description": SERVICE_DESCRIPTION,
        "category": SERVICE_CATEGORY,
        "pricing_model": "fixed",
        "price_amount": SERVICE_PRICE,
        "tags": SERVICE_TAGS,
        "enabled": True,
        "response_time_seconds": SERVICE_RESPONSE_TIME,
    }

    data = request_json(
        "POST",
        "https://market.near.ai/v1/agents/me/services",
        headers=NEAR_HEADERS,
        payload=payload,
        expected_statuses={200, 201},
        timeout=20,
    )

    if data.get("error"):
        log.warning(f"Service registration failed: {data['error']}")
        return None

    service_id = data.get("service_id") or data.get("id")
    if service_id:
        state["service_id"] = service_id
        save_state(state)
        log.info(f"Service registered: {service_id}")
        tg_send(
            f"🔧 <b>Service registered</b>\n"
            f"Name: {tg_escape(SERVICE_NAME)}\n"
            f"Category: {SERVICE_CATEGORY}\n"
            f"Price: {SERVICE_PRICE} NEAR\n"
            f"ID: <code>{service_id}</code>"
        )
    return service_id


def list_my_services() -> List[Dict[str, Any]]:
    """List our registered services."""
    return request_list(
        "GET",
        "https://market.near.ai/v1/agents/me/services",
        headers=NEAR_HEADERS,
        timeout=10,
        expected_statuses={200},
    )


def update_service(service_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Update a service registration."""
    return request_json(
        "PATCH",
        f"https://market.near.ai/v1/services/{service_id}",
        headers=NEAR_HEADERS,
        payload=updates,
        expected_statuses={200},
        timeout=15,
    )


def ensure_service_registered(state: Dict[str, Any]) -> None:
    """Check if service exists, register if not, re-enable if disabled."""
    if not ENABLE_SERVICES:
        return

    existing_id = state.get("service_id")

    if existing_id:
        # Verify it still exists
        services = list_my_services()
        found = False
        for s in services:
            sid = s.get("service_id") or s.get("id")
            if sid == existing_id:
                found = True
                if not s.get("enabled", True):
                    log.info(f"Re-enabling service {existing_id[:8]}")
                    update_service(existing_id, {"enabled": True})
                break
        if not found:
            log.info("Service not found, re-registering...")
            state.pop("service_id", None)
            register_service(state)
    else:
        register_service(state)

    # Register extra services (writing, healthcare)
    _ensure_extra_services_registered(state)


def _ensure_extra_services_registered(state: Dict[str, Any]) -> None:
    """Register additional services for writing and healthcare categories."""
    if DRY_RUN:
        return
    extra_ids = state.setdefault("extra_service_ids", {})
    services = list_my_services()
    existing_cats = {s.get("category") for s in services}

    for svc in EXTRA_SERVICES:
        cat = svc["category"]
        if cat in existing_cats:
            continue  # already registered
        if cat in extra_ids:
            continue  # we registered it before
        payload = {
            "name": svc["name"],
            "description": svc["description"],
            "category": cat,
            "pricing_model": "fixed",
            "price_amount": svc["price"],
            "tags": svc["tags"],
            "enabled": True,
            "response_time_seconds": SERVICE_RESPONSE_TIME,
        }
        data = request_json(
            "POST",
            "https://market.near.ai/v1/agents/me/services",
            headers=NEAR_HEADERS,
            payload=payload,
            expected_statuses={200, 201},
            timeout=20,
        )
        if not data.get("error"):
            sid = data.get("service_id") or data.get("id")
            if sid:
                extra_ids[cat] = sid
                log.info(f"Extra service registered: {cat} ({sid})")
                tg_send(
                    f"🔧 <b>Extra service registered</b>\n"
                    f"Category: {cat}\n"
                    f"Price: {svc['price']} NEAR"
                )


# ========================
# SERVICE PROFILE AUTO-UPDATE
# ========================
_BASE_SERVICE_DESCRIPTION = (
    "Autonomous AI agent for code review, research, documentation, content creation, "
    "and blockchain development. Powered by Claude Sonnet 4.6. Delivers high-quality "
    "work with smart formatting, revision handling, and fast turnaround."
)
_BASE_EXTRA_DESCRIPTIONS = {
    "writing": "AI agent for research, technical writing, blog posts, documentation, and content creation. Fast, high-quality delivery.",
    "healthcare": "AI agent for medical coding, claims analysis, healthcare documentation, and clinical data processing.",
}


def _build_profile_description(base: str, jobs_completed: int, win_rate_pct: float) -> str:
    """Append live stats badge to service description."""
    badge_parts = []
    if jobs_completed > 0:
        badge_parts.append(f"✅ {jobs_completed} jobs completed")
    if win_rate_pct > 0:
        badge_parts.append(f"{win_rate_pct:.0f}% acceptance rate")
    if not badge_parts:
        return base
    return base + " | " + " · ".join(badge_parts)


def refresh_service_descriptions(state: Dict[str, Any], jobs_completed: int) -> None:
    """Update all registered service descriptions with current stats."""
    if DRY_RUN:
        return

    stats = state.get("stats", {})
    accepted = stats.get("accepted", 0)
    rejected = stats.get("rejected", 0)
    decided = accepted + rejected
    win_rate = round(accepted / decided * 100, 1) if decided > 0 else 0.0

    updated = 0

    # Main service
    service_id = state.get("service_id")
    if service_id:
        new_desc = _build_profile_description(_BASE_SERVICE_DESCRIPTION, jobs_completed, win_rate)
        result = update_service(service_id, {"description": new_desc})
        if not result.get("error"):
            updated += 1
            log.info(f"Service description updated: {jobs_completed} jobs, {win_rate:.0f}% win rate")
        else:
            log.warning(f"Failed to update main service description: {result['error']}")

    # Extra services
    extra_ids = state.get("extra_service_ids", {})
    for cat, sid in extra_ids.items():
        base = _BASE_EXTRA_DESCRIPTIONS.get(cat, SERVICE_DESCRIPTION)
        new_desc = _build_profile_description(base, jobs_completed, win_rate)
        result = update_service(sid, {"description": new_desc})
        if not result.get("error"):
            updated += 1
        else:
            log.warning(f"Failed to update {cat} service description: {result['error']}")

    if updated > 0:
        state["last_profile_update"] = datetime.now(timezone.utc).isoformat()
        log.info(f"Refreshed {updated} service description(s)")
        tg_send(
            f"📋 <b>Service profile updated</b>\n"
            f"✅ {jobs_completed} jobs completed · {win_rate:.0f}% acceptance rate\n"
            f"Services updated: {updated}"
        )


# ========================
# ASSIGNMENT MESSAGING
# ========================
def get_my_assignments(job_id: str) -> List[Dict[str, Any]]:
    """Get my assignments for a job (from job detail response)."""
    job = get_job_details(job_id)
    if not job:
        return []
    return job.get("my_assignments") or []


def send_assignment_message(assignment_id: str, body: str) -> Dict[str, Any]:
    """Send a private message on an assignment."""
    if DRY_RUN:
        log.info(f"DRY-RUN MESSAGE: assignment={assignment_id[:8]} body={body[:50]}")
        return {"ok": True, "dry_run": True}

    return request_json(
        "POST",
        f"https://market.near.ai/v1/assignments/{assignment_id}/messages",
        headers=NEAR_HEADERS,
        payload={"body": body},
        expected_statuses={200, 201},
        timeout=15,
    )


def read_assignment_messages(assignment_id: str) -> List[Dict[str, Any]]:
    """Read private messages on an assignment."""
    return request_list(
        "GET",
        f"https://market.near.ai/v1/assignments/{assignment_id}/messages",
        headers=NEAR_HEADERS,
        timeout=10,
        expected_statuses={200},
    )


def fetch_my_assigned_jobs() -> List[Dict[str, Any]]:
    """Fetch jobs where we are the assigned worker (in_progress status)."""
    bids = fetch_all_bids()
    assigned = []
    for bid in bids:
        status = bid.get("status", "")
        if status == "accepted":
            assigned.append(bid)
    return assigned


def handle_assigned_jobs(state: Dict[str, Any]) -> None:
    """Check for jobs assigned to us (instant or awarded) and auto-submit."""
    assigned = fetch_my_assigned_jobs()
    if not assigned:
        return

    for bid in assigned:
        job_id = bid.get("job_id")
        if not job_id:
            continue

        # Skip already submitted
        if job_id in state.get("submitted_jobs", []):
            continue

        submit_op = op_key("submit", job_id)
        if op_status(state, submit_op) in ("done", "in_progress"):
            continue

        # Check job details and assignments
        job = get_job_details(job_id)
        if not job:
            continue

        # Skip jobs requiring real-world actions
        if job_requires_real_action(job):
            log.info(f"SKIP assigned job {job_id[:8]}: requires real-world action")
            tg_send(
                f"⏭ <b>Skipped assigned job</b>\n"
                f"<b>{tg_escape((job.get('title') or '')[:60])}</b>\n"
                f"Reason: requires real-world action (sign up, post, etc.)"
            )
            continue

        my_assignments = job.get("my_assignments") or []
        for assignment in my_assignments:
            a_status = assignment.get("status", "")
            assignment_id = assignment.get("assignment_id")

            if a_status == "in_progress" and assignment_id:
                title = job.get("title", "")
                description = job.get("description", "")
                tags = job.get("tags", [])

                log.info(f"ASSIGNED JOB: {job_id[:8]} - {title[:50]}")
                tg_send(
                    f"📋 <b>Assigned job detected</b>\n"
                    f"<b>{tg_escape(title[:60])}</b>\n"
                    f"Generating deliverable..."
                )

                # Send a progress message
                send_assignment_message(
                    assignment_id,
                    "Working on your request. I'll submit the deliverable shortly."
                )

                deliverable = generate_deliverable(title, description, tags)
                if not deliverable:
                    tg_send(f"⚠️ Deliverable generation failed for assigned job {job_id[:8]}")
                    send_assignment_message(
                        assignment_id,
                        "I encountered an issue generating the deliverable. Working on it..."
                    )
                    continue

                set_op_status(state, submit_op, "in_progress")
                result = submit_work(job_id, deliverable)

                if result.get("error"):
                    log.error(f"Submit error (assigned): {result['error']}")
                    set_op_status(state, submit_op, "failed")
                else:
                    state["submitted_jobs"].append(job_id)
                    set_op_status(state, submit_op, "done")
                    bump_stat(state, "submitted")
                    save_state(state)

                    send_assignment_message(
                        assignment_id,
                        "Deliverable submitted! Please review and let me know if any changes are needed."
                    )
                    tg_send(
                        f"🚀 <b>Assigned job submitted</b>\n"
                        f"Job: <code>{job_id}</code>\n"
                        f"<b>{tg_escape(title[:60])}</b>"
                    )

            elif a_status in ("request-changes", "request_changes") and assignment_id:
                # Handle revision for assigned jobs too
                handle_assignment_revision(job, assignment, state)


def handle_assignment_revision(job: Dict[str, Any], assignment: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Handle revision request on an assignment using private messages for feedback."""
    job_id = job.get("job_id")
    assignment_id = assignment.get("assignment_id")
    if not job_id or not assignment_id:
        return

    revision_counts = state.setdefault("revision_counts", {})
    current_count = revision_counts.get(job_id, 0)

    if current_count >= MAX_REVISIONS:
        log.info(f"REVISION SKIP: {job_id[:8]} max revisions reached")
        return

    revision_op = op_key("revision", f"{job_id}_{current_count + 1}")
    if op_status(state, revision_op) == "done":
        return

    # Read assignment messages for feedback
    messages = read_assignment_messages(assignment_id)
    feedback = ""
    if messages:
        # Get last few messages from the requester
        for msg in reversed(messages[-5:]):
            body = msg.get("body", "").strip()
            if body:
                feedback = body
                break

    if not feedback:
        feedback = "Please improve the quality and completeness of the deliverable."

    title = job.get("title", "")
    description = job.get("description", "")
    tags = job.get("tags", [])
    previous = assignment.get("deliverable") or ""

    log.info(f"ASSIGNMENT REVISION {current_count + 1}/{MAX_REVISIONS}: {job_id[:8]}")
    tg_send(
        f"🔄 <b>Revision requested (assignment)</b> ({current_count + 1}/{MAX_REVISIONS})\n"
        f"Job: <b>{tg_escape(title[:60])}</b>\n"
        f"Feedback: {tg_escape(feedback[:100])}"
    )

    send_assignment_message(assignment_id, "Working on the revision based on your feedback...")

    set_op_status(state, revision_op, "in_progress")
    revised = generate_revision(title, description, tags, previous, feedback)
    if not revised:
        set_op_status(state, revision_op, "failed")
        return

    result = submit_work(job_id, revised)
    if result.get("error"):
        log.error(f"Revision submit error: {result['error']}")
        set_op_status(state, revision_op, "failed")
    else:
        revision_counts[job_id] = current_count + 1
        set_op_status(state, revision_op, "done")
        bump_stat(state, "revisions_handled")
        bump_stat(state, "submitted")
        save_state(state)

        send_assignment_message(
            assignment_id,
            f"Revision {current_count + 1} submitted. Please review the updated deliverable."
        )
        tg_send(
            f"✅ <b>Assignment revision submitted</b> ({current_count + 1}/{MAX_REVISIONS})\n"
            f"Job: <code>{job_id}</code>"
        )


# ========================
# LLM API (Claude / OpenRouter)
# ========================
def _call_llm(prompt: str, max_tokens: int = 8192, timeout: int = 120) -> Optional[str]:
    """Call LLM via OpenRouter or Anthropic depending on available key."""
    if _LLM_USE_OPENROUTER:
        data = request_json(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {_LLM_KEY}",
                "content-type": "application/json",
            },
            payload={
                "model": "anthropic/claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            expected_statuses={200},
            timeout=timeout,
        )
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            log.error(f"OpenRouter API error: {data.get('error', 'unexpected response')}")
            return None
    else:
        data = request_json(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _LLM_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload={
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            expected_statuses={200},
            timeout=timeout,
        )
        try:
            return data["content"][0]["text"]
        except Exception:
            log.error(f"Claude API error: {data.get('error', 'unexpected response')}")
            return None
def _detect_output_format(tags: List[str], title: str, description: str) -> str:
    """Detect the expected output format based on job tags and content."""
    tag_lower = {t.lower() for t in (tags or [])}
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()

    if tag_lower & {"rust", "python", "javascript", "solidity", "code", "smart-contract", "sdk", "api", "agent"}:
        return "code"
    if tag_lower & {"docs", "documentation", "technical-writing", "tutorial"}:
        return "markdown_doc"
    if tag_lower & {"research", "analysis", "data", "report", "survey"}:
        return "research_report"
    if tag_lower & {"marketing", "social", "twitter", "x", "tweet"}:
        return "social_content"

    if any(kw in title_lower or kw in desc_lower for kw in ["write code", "build", "implement", "develop", "create a script"]):
        return "code"
    if any(kw in title_lower or kw in desc_lower for kw in ["write a report", "research", "analyze", "analysis"]):
        return "research_report"
    if any(kw in title_lower or kw in desc_lower for kw in ["documentation", "docs", "guide", "tutorial"]):
        return "markdown_doc"

    return "general"


FORMAT_INSTRUCTIONS = {
    "code": (
        "OUTPUT FORMAT: Provide working, production-ready code.\n"
        "- Start with a brief explanation of your approach\n"
        "- Write the complete code in appropriate language (detect from context)\n"
        "- Include inline comments for complex logic\n"
        "- Add usage examples at the end\n"
        "- Use proper code blocks with language tags"
    ),
    "markdown_doc": (
        "OUTPUT FORMAT: Write structured Markdown documentation.\n"
        "- Use clear headings (##, ###) for sections\n"
        "- Include a table of contents if longer than 3 sections\n"
        "- Add code examples where relevant\n"
        "- Use tables for structured data\n"
        "- Include a summary/TL;DR at the top"
    ),
    "research_report": (
        "OUTPUT FORMAT: Write a professional research report.\n"
        "- Executive summary at the top\n"
        "- Clear sections with findings\n"
        "- Data-driven insights with specific numbers where possible\n"
        "- Actionable recommendations\n"
        "- Sources and methodology notes"
    ),
    "social_content": (
        "OUTPUT FORMAT: Create social media / marketing content.\n"
        "- Multiple variations (at least 3-5 options)\n"
        "- Keep within platform character limits\n"
        "- Include relevant hashtags\n"
        "- Engaging hooks and CTAs\n"
        "- Emoji usage where appropriate"
    ),
    "general": (
        "OUTPUT FORMAT: Provide a clear, well-structured deliverable.\n"
        "- Use Markdown formatting\n"
        "- Be thorough and professional\n"
        "- Include concrete examples where helpful"
    ),
}


def generate_deliverable(job_title: str, job_description: str, tags: List[str]) -> Optional[str]:
    if DRY_RUN:
        return (
            "[DRY-RUN] Deliverable placeholder\n\n"
            f"Title: {job_title}\n"
            f"Tags: {', '.join(tags) if tags else 'none'}\n"
            "This is a dry-run output. No external LLM call was made."
        )

    tag_str = ", ".join(tags) if tags else "none"
    output_format = _detect_output_format(tags, job_title, job_description)
    format_instructions = FORMAT_INSTRUCTIONS.get(output_format, FORMAT_INSTRUCTIONS["general"])

    prompt = f"""You are an expert AI agent working on the market.near.ai marketplace.
You have been hired to complete the following job:

TITLE: {job_title}
TAGS: {tag_str}

DESCRIPTION:
{job_description}

{format_instructions}

Be thorough, professional, and deliver real value. Write the complete deliverable now:"""

    return _call_llm(prompt, max_tokens=8192, timeout=120)


def _detect_unknown_action(tags: List[str], title: str, description: str) -> Optional[str]:
    """Detect if job requires an external action that bot cannot perform. Returns action name or None."""
    text = (title + " " + description).lower()
    unknown_actions = {
        "post on linkedin": "LinkedIn post",
        "linkedin post": "LinkedIn post",
        "post on reddit": "Reddit post",
        "reddit post": "Reddit post",
        "submit to app store": "App Store submission",
        "publish to app store": "App Store submission",
        "google play": "Google Play submission",
        "upload to youtube": "YouTube upload",
        "youtube video": "YouTube upload",
        "send email": "Email sending",
        "send an email": "Email sending",
        "deploy to aws": "AWS deployment",
        "deploy to heroku": "Heroku deployment",
        "deploy to vercel": "Vercel deployment",
        "docker hub": "Docker Hub publish",
        "publish docker": "Docker Hub publish",
        "create figma": "Figma design",
        "figma design": "Figma design",
    }
    for signal, action_name in unknown_actions.items():
        if signal in text:
            return action_name
    return None


def _is_npm_job(tags: List[str], title: str, description: str) -> bool:
    """Detect if job requires publishing an npm package."""
    npm_signals = {"npm", "package", "node", "nodejs", "typescript", "javascript", "library"}
    publish_signals = {"publish", "npm package", "npm publish", "package.json", "npmjs"}
    text = (title + " " + description).lower()
    has_npm = any(s in tags or s in text for s in npm_signals)
    has_publish = any(s in text for s in publish_signals)
    return has_npm and has_publish


def _is_near_deploy_job(tags: List[str], title: str, description: str) -> bool:
    """Detect if job requires deploying a NEAR smart contract."""
    text = (title + " " + description).lower()
    deploy_signals = {"deploy contract", "deploy a contract", "deploy to testnet", "deploy to near",
                      "near contract", "smart contract deploy", "wasm deploy", "deploy wasm"}
    return any(s in text for s in deploy_signals)


def deploy_near_contract(job_id: str, title: str, description: str, tags: List[str]) -> Optional[str]:
    """Generate a NEAR smart contract via AI, compile and deploy to testnet. Returns explorer URL or None."""
    import subprocess
    import tempfile
    import re

    log.info(f"NEAR DEPLOY: generating contract for {job_id[:8]}")

    tag_str = ", ".join(tags) if tags else "none"
    prompt = f"""You are an expert NEAR Protocol smart contract developer.
Generate a complete, compilable NEAR smart contract in Rust for this job.

TITLE: {title}
TAGS: {tag_str}
DESCRIPTION:
{description}

Output ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "contract_name": "near_<short_name>",
  "files": {{
    "Cargo.toml": "...full Cargo.toml content...",
    "src/lib.rs": "...full Rust contract code..."
  }}
}}

Rules:
- Use near-sdk = "5.1.0" in Cargo.toml
- crate-type must be ["cdylib"]
- Contract must compile with: cargo build --target wasm32-unknown-unknown --release
- Include at least 2-3 public methods
- Output ONLY the JSON, nothing else"""

    raw = _call_llm(prompt, max_tokens=4096, timeout=120)
    if not raw:
        return None

    try:
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            return None
        contract_data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        log.error(f"NEAR DEPLOY: JSON parse error: {e}")
        return None

    contract_name = contract_data.get("contract_name", f"near_contract_{job_id[:6]}")
    files = contract_data.get("files", {})

    if not files:
        log.error("NEAR DEPLOY: no files in response")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write contract files
        for filepath, content in files.items():
            full_path = os.path.join(tmpdir, filepath)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content, indent=2))

        # Compile
        log.info("NEAR DEPLOY: compiling contract...")
        env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":/root/.cargo/bin"}
        result = subprocess.run(
            ["cargo", "build", "--target", "wasm32-unknown-unknown", "--release"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )

        if result.returncode != 0:
            log.error(f"NEAR DEPLOY: compile failed:\n{result.stderr[-1000:]}")
            return None

        # Find wasm file
        wasm_path = os.path.join(tmpdir, "target", "wasm32-unknown-unknown", "release", f"{contract_name}.wasm")
        if not os.path.exists(wasm_path):
            # Try to find any wasm file
            for root, dirs, fnames in os.walk(os.path.join(tmpdir, "target")):
                for fname in fnames:
                    if fname.endswith(".wasm"):
                        wasm_path = os.path.join(root, fname)
                        break

        if not os.path.exists(wasm_path):
            log.error("NEAR DEPLOY: wasm file not found after compile")
            return None

        # Deploy to testnet
        log.info(f"NEAR DEPLOY: deploying {wasm_path} to {NEAR_DEPLOY_ACCOUNT}")
        try:
            deploy_result = subprocess.run(
                ["near", "deploy", NEAR_DEPLOY_ACCOUNT, wasm_path, "--networkId", "testnet"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        except OSError as e:
            log.error(f"NEAR DEPLOY: near CLI not found or failed to run: {e}")
            return None

        if deploy_result.returncode != 0:
            log.error(f"NEAR DEPLOY: deploy failed: {deploy_result.stderr[:500]}")
            return None

        # Extract tx hash from output
        tx_hash = None
        for line in deploy_result.stdout.splitlines():
            if "Transaction Id" in line or "txHash" in line:
                parts = line.split()
                if parts:
                    tx_hash = parts[-1]
                    break

        explorer_url = f"https://testnet.nearblocks.io/address/{NEAR_DEPLOY_ACCOUNT}"
        log.info(f"NEAR DEPLOYED: {explorer_url}")
        tg_send(f"🚀 <b>NEAR contract deployed!</b>\n"
                f"Account: <code>{NEAR_DEPLOY_ACCOUNT}</code>\n"
                f"<a href='{explorer_url}'>View on Explorer</a>")
        return explorer_url


def _is_gist_job(tags: List[str], title: str, description: str) -> bool:
    """Detect if job requires publishing a GitHub Gist."""
    text = (title + " " + description).lower()
    return "gist" in text or ("share code" in text and "github" in text)


def create_github_gist(job_id: str, title: str, description: str, tags: List[str]) -> Optional[str]:
    """Publish code as a GitHub Gist. Returns gist URL or None."""
    if not GITHUB_TOKEN:
        return None

    log.info(f"GIST: creating for {job_id[:8]}")
    deliverable_code = _call_llm(
        f"Write complete working code for this task. Output ONLY the code, no explanations:\n\nTITLE: {title}\nDESCRIPTION: {description}",
        max_tokens=4096, timeout=120
    )
    if not deliverable_code:
        return None

    resp = requests.post(
        "https://api.github.com/gists",
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
        json={
            "description": title,
            "public": True,
            "files": {f"solution_{job_id[:8]}.md": {"content": deliverable_code}}
        },
        timeout=30,
    )
    if resp.ok:
        url = resp.json().get("html_url", "")
        log.info(f"GIST PUBLISHED: {url}")
        tg_send(f"📎 <b>GitHub Gist published!</b>\n<a href='{url}'>{title[:50]}</a>")
        return url
    log.error(f"GIST: failed: {resp.text[:200]}")
    return None


def _is_github_job(tags: List[str], title: str, description: str) -> bool:
    """Detect if job requires creating a GitHub repository."""
    text = (title + " " + description).lower()
    github_signals = {"github", "git repo", "repository", "github repo", "push to github", "create repo"}
    return any(s in text for s in github_signals)


def create_github_repo(job_id: str, title: str, description: str, tags: List[str]) -> Optional[str]:
    """Generate code via AI and push to a new GitHub repo. Returns repo URL or None."""
    import subprocess
    import tempfile
    import re

    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set, skipping github repo creation")
        return None

    log.info(f"GITHUB: creating repo for {job_id[:8]}")

    tag_str = ", ".join(tags) if tags else "none"
    prompt = f"""You are an expert developer. Generate a complete GitHub repository for this job.

TITLE: {title}
TAGS: {tag_str}
DESCRIPTION:
{description}

Output ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "repo_name": "near-<short-name>",
  "description": "one line description",
  "files": {{
    "README.md": "...full README content...",
    "index.js": "...or main file content...",
    "package.json": "...if node project..."
  }}
}}

Rules:
- repo_name must be lowercase, hyphenated, start with "near-"
- Include real working implementation
- README must have description, installation, usage examples
- Output ONLY the JSON, nothing else"""

    raw = _call_llm(prompt, max_tokens=4096, timeout=120)
    if not raw:
        return None

    try:
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            return None
        repo_data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        log.error(f"GITHUB: JSON parse error: {e}")
        return None

    repo_name = repo_data.get("repo_name", f"near-project-{job_id[:8]}")
    files = repo_data.get("files", {})
    repo_description = repo_data.get("description", title)

    # Create repo via GitHub API
    create_resp = requests.post(
        "https://api.github.com/user/repos",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "name": repo_name,
            "description": repo_description,
            "private": False,
            "auto_init": False,
        },
        timeout=30,
    )

    if create_resp.status_code == 422:
        # Repo already exists — append job_id suffix
        repo_name = f"{repo_name}-{job_id[:6]}"
        create_resp = requests.post(
            "https://api.github.com/user/repos",
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"name": repo_name, "description": repo_description, "private": False, "auto_init": False},
            timeout=30,
        )

    if create_resp.status_code not in (200, 201):
        log.error(f"GITHUB: repo creation failed: {create_resp.text[:300]}")
        return None

    repo_info = create_resp.json()
    repo_url = repo_info.get("html_url", "")
    clone_url = repo_info.get("clone_url", "")

    # Get github username
    user_resp = requests.get(
        "https://api.github.com/user",
        headers={"Authorization": f"token {GITHUB_TOKEN}"},
        timeout=15,
    )
    github_username = user_resp.json().get("login", "user") if user_resp.ok else "user"

    # Push files via git in temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "bot@near.ai"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "NEAR Bot"], cwd=tmpdir, check=True, capture_output=True)

            # Write files (path traversal protection)
            for filename, content in files.items():
                filepath = _safe_filename(tmpdir, filename)
                if not filepath:
                    log.warning(f"GITHUB: skipping unsafe filename: {filename!r}")
                    continue
                parent = os.path.dirname(filepath)
                if parent and not os.path.exists(parent):
                    os.makedirs(parent)
                with open(filepath, "w") as f:
                    f.write(content if isinstance(content, str) else json.dumps(content, indent=2))

            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True, capture_output=True)

            # Push using GIT_ASKPASS to avoid token in URL/process list
            askpass = os.path.join(tmpdir, "_askpass.sh")
            with open(askpass, "w") as _f:
                _f.write(f"#!/bin/sh\necho '{GITHUB_TOKEN}'\n")
            os.chmod(askpass, 0o700)
            push_env = {**os.environ, "GIT_ASKPASS": askpass, "GIT_USERNAME": github_username}
            subprocess.run(
                ["git", "push", clone_url, "HEAD:main"],
                cwd=tmpdir, check=True, capture_output=True, timeout=60, env=push_env,
            )

            log.info(f"GITHUB PUBLISHED: {repo_url}")
            tg_send(f"🐙 <b>GitHub repo created!</b>\n<a href='{repo_url}'>{repo_name}</a>")
            return repo_url

        except (subprocess.CalledProcessError, OSError) as e:
            stderr = getattr(e, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            log.error(f"GITHUB: git error: {stderr or e}")
            return None


def publish_npm_package(job_id: str, title: str, description: str, tags: List[str]) -> Optional[str]:
    """Generate npm package files via AI and publish to npmjs.com. Returns npm URL or None."""
    import subprocess
    import tempfile
    import re

    if not NPM_TOKEN:
        log.warning("NPM_TOKEN not set, skipping npm publish")
        return None

    log.info(f"NPM PUBLISH: generating package for {job_id[:8]}")

    tag_str = ", ".join(tags) if tags else "none"
    prompt = f"""You are an expert Node.js developer. Generate a complete, publishable npm package for this job.

TITLE: {title}
TAGS: {tag_str}
DESCRIPTION:
{description}

Output ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "package_name": "near-<short-name>",
  "version": "1.0.0",
  "description": "one line description",
  "files": {{
    "package.json": "{{...full package.json content as string...}}",
    "index.js": "...full index.js content...",
    "README.md": "...full README content..."
  }}
}}

Rules:
- package_name must be lowercase, hyphenated, start with "near-"
- Include real, working implementation code in index.js
- package.json must have name, version, description, main, keywords, license fields
- README must explain what the package does and show usage examples
- Output ONLY the JSON, nothing else"""

    raw = _call_llm(prompt, max_tokens=4096, timeout=120)
    if not raw:
        log.error("NPM: LLM returned empty response")
        return None

    # Extract JSON from response
    try:
        # Try to find JSON block
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            log.error("NPM: No JSON found in LLM response")
            return None
        pkg_data = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        log.error(f"NPM: JSON parse error: {e}")
        return None

    pkg_name = pkg_data.get("package_name", f"near-util-{job_id[:8]}")
    files = pkg_data.get("files", {})

    if not files:
        log.error("NPM: No files in package data")
        return None

    # Create temp directory and write files
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write .npmrc with token
        npmrc_path = os.path.join(tmpdir, ".npmrc")
        with open(npmrc_path, "w") as f:
            f.write(f"//registry.npmjs.org/:_authToken={NPM_TOKEN}\n")

        # Write package files
        for filename, content in files.items():
            filepath = os.path.join(tmpdir, filename)
            os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
            with open(filepath, "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content, indent=2))

        # Ensure package.json exists and is valid
        pkg_json_path = os.path.join(tmpdir, "package.json")
        if not os.path.exists(pkg_json_path):
            pkg_json = {
                "name": pkg_name,
                "version": pkg_data.get("version", "1.0.0"),
                "description": pkg_data.get("description", title),
                "main": "index.js",
                "keywords": tags,
                "license": "MIT"
            }
            with open(pkg_json_path, "w") as f:
                json.dump(pkg_json, f, indent=2)
        else:
            # Validate and fix package.json
            try:
                with open(pkg_json_path) as f:
                    pj = json.loads(f.read())
                pj["name"] = pkg_name
                with open(pkg_json_path, "w") as f:
                    json.dump(pj, f, indent=2)
            except Exception:
                pass

        # Run npm publish
        try:
            result = subprocess.run(
                ["npm", "publish", "--access", "public"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, "NPM_TOKEN": NPM_TOKEN}
            )
            if result.returncode == 0:
                npm_url = f"https://www.npmjs.com/package/{pkg_name}"
                log.info(f"NPM PUBLISHED: {npm_url}")
                tg_send(f"📦 <b>npm package published!</b>\n<a href='{npm_url}'>{pkg_name}</a>")
                return npm_url
            else:
                # Handle "already exists" — bump patch version and retry
                if "already exists" in result.stderr or "cannot publish over" in result.stderr:
                    log.warning("NPM: version conflict, bumping version")
                    try:
                        with open(pkg_json_path) as f:
                            pj = json.load(f)
                        parts = pj.get("version", "1.0.0").split(".")
                        try:
                            parts[-1] = str(int(parts[-1]) + 1)
                        except ValueError:
                            parts[-1] = "1"
                        pj["version"] = ".".join(parts)
                        with open(pkg_json_path, "w") as f:
                            json.dump(pj, f, indent=2)
                        result2 = subprocess.run(
                            ["npm", "publish", "--access", "public"],
                            cwd=tmpdir,
                            capture_output=True,
                            text=True,
                            timeout=60,
                            env={**os.environ, "NPM_TOKEN": NPM_TOKEN}
                        )
                        if result2.returncode == 0:
                            npm_url = f"https://www.npmjs.com/package/{pkg_name}"
                            log.info(f"NPM PUBLISHED (retry): {npm_url}")
                            tg_send(f"📦 <b>npm package published!</b>\n<a href='{npm_url}'>{pkg_name}</a>")
                            return npm_url
                    except Exception as e:
                        log.error(f"NPM retry error: {e}")
                log.error(f"NPM publish failed: {result.stderr[:500]}")
                return None
        except subprocess.TimeoutExpired:
            log.error("NPM: publish timed out")
            return None
        except Exception as e:
            log.error(f"NPM: unexpected error: {e}")
            return None


def generate_revision(job_title: str, job_description: str, tags: List[str],
                      previous_deliverable: str, feedback: str) -> Optional[str]:
    """Generate a revised deliverable based on client feedback."""
    if DRY_RUN:
        return f"[DRY-RUN] Revised deliverable for: {job_title}"

    tag_str = ", ".join(tags) if tags else "none"

    prompt = f"""You are an expert AI agent working on the market.near.ai marketplace.
A client has requested changes to your previous deliverable.

JOB TITLE: {job_title}
TAGS: {tag_str}

JOB DESCRIPTION:
{job_description}

YOUR PREVIOUS DELIVERABLE:
{previous_deliverable[:4000]}

CLIENT FEEDBACK / REQUESTED CHANGES:
{feedback}

Please revise your deliverable to address ALL the client's feedback.
Keep what was good, fix what was requested. Be thorough and professional."""

    return _call_llm(prompt, max_tokens=8192, timeout=120)


def generate_comment_reply(job_title: str, comment_text: str) -> Optional[str]:
    """Generate a professional reply to a client comment on a bid."""
    if DRY_RUN:
        return f"[DRY-RUN] Reply to comment on: {job_title}"

    prompt = f"""You are an expert AI agent on market.near.ai.
A client left a comment on your bid for this job: "{job_title}"

CLIENT COMMENT:
{comment_text}

Write a brief, professional reply (2-4 sentences). Be helpful, confident, and address their specific points.
If they ask a question, answer it directly. If they express concern, reassure them with specifics.
Do NOT be generic — reference their actual comment."""

    return _call_llm(prompt, max_tokens=1024, timeout=60)


# ========================
# MARKET API
# ========================
def is_job_open(job: Dict[str, Any]) -> bool:
    state_value = str(job.get("state") or job.get("status") or "").strip().lower()
    if not state_value:
        # Some endpoints may omit state; allow and rely on API response on submit/bid.
        return True
    return state_value in {"open", "filling", "in_progress", "assigned"}


def fetch_all_jobs(state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100

    while True:
        batch = request_list(
            "GET",
            f"https://market.near.ai/v1/jobs?limit={page_size}&offset={offset}&order=bid_count_asc",
            headers=NEAR_HEADERS,
            timeout=15,
            expected_statuses={200},
        )
        if not batch:
            break

        for j in batch:
            if not is_job_open(j):
                continue
            if int(j.get("bid_count") or 0) > MAX_BID_COUNT:
                continue

            is_competition = j.get("job_type") == "competition"
            budget = float(
                j.get("budget_amount") or j.get("reward") or
                j.get("price") or j.get("payment_amount") or
                j.get("budget") or 0
            )
            if not is_competition and (budget < MIN_BUDGET or budget > MAX_BUDGET):
                continue

            tags = j.get("tags", [])
            tag_set = {t.lower() for t in tags}
            if SKIP_TAGS and any(t.lower() in tag_set for t in SKIP_TAGS):
                continue
            if ONLY_TAGS and not any(t.lower() in tag_set for t in ONLY_TAGS):
                continue
            if job_requires_real_action(j):
                # Notify for high-budget jobs so user can decide manually
                try:
                    budget = float(
                        j.get("budget_amount") or j.get("reward") or
                        j.get("price") or j.get("payment_amount") or
                        j.get("budget") or 0
                    )
                except (ValueError, TypeError):
                    budget = 0
                jid = j.get("job_id", "")
                if budget >= 5 and state is not None and jid not in state.get("notified_skip_jobs", []):
                    state.setdefault("notified_skip_jobs", []).append(jid)
                    tg_send(
                        f"👀 <b>Пропущена задача — нужна помощь</b>\n"
                        f"<b>{tg_escape((j.get('title') or '')[:60])}</b>\n"
                        f"💰 Бюджет: {budget:.0f} NEAR\n"
                        f"🔗 <a href='https://market.near.ai/jobs/{jid}'>Открыть и решить вручную</a>"
                    )
                continue

            # Skip jobs from blacklisted clients
            creator = (
                j.get("created_by") or j.get("requester_id") or
                j.get("owner_id") or j.get("account_id") or ""
            )
            if any(blocked in str(creator) for blocked in BLOCKED_CLIENT_IDS):
                continue

            # Skip jobs where requester has no funds (if API exposes this)
            if not _has_requester_balance(j):
                log.info(f"SKIP: requester has 0 balance for job {j.get('job_id', '')[:8]}")
                continue

            jobs.append(j)

        offset += len(batch)
        if len(batch) < page_size:
            break

    return jobs


def get_job_details(job_id: str) -> Optional[Dict[str, Any]]:
    data = request_json(
        "GET",
        f"https://market.near.ai/v1/jobs/{job_id}",
        headers=NEAR_HEADERS,
        timeout=15,
        expected_statuses={200},
    )
    if data.get("error"):
        log.error(f"Failed to fetch job {job_id}: {data['error']}")
        return None
    return data


def fetch_bid_comments(bid_id: str) -> List[Dict[str, Any]]:
    """Fetch comments on a specific bid."""
    return request_list(
        "GET",
        f"https://market.near.ai/v1/bids/{bid_id}/comments",
        headers=NEAR_HEADERS,
        timeout=10,
        expected_statuses={200},
    )


def post_bid_comment(bid_id: str, text: str) -> Dict[str, Any]:
    """Post a reply comment on a bid."""
    if DRY_RUN:
        log.info(f"DRY-RUN COMMENT: bid={bid_id[:8]} text={text[:50]}")
        return {"ok": True, "dry_run": True}

    return request_json(
        "POST",
        f"https://market.near.ai/v1/bids/{bid_id}/comments",
        headers=NEAR_HEADERS,
        payload={"text": text},
        expected_statuses={200, 201},
        timeout=15,
    )


def place_bid(
    job_id: str,
    tags: Optional[List[str]] = None,
    amount: str = "1",
    eta_seconds: int = 3600,
) -> Dict[str, Any]:
    proposal = get_proposal(tags or [])
    payload = {"proposal": proposal, "amount": str(amount), "eta_seconds": eta_seconds}

    if DRY_RUN:
        log.info(f"DRY-RUN BID: {job_id[:8]} amount={amount} eta={eta_seconds}")
        return {"status": "pending", "dry_run": True}

    return request_json(
        "POST",
        f"https://market.near.ai/v1/jobs/{job_id}/bids",
        headers=NEAR_HEADERS,
        payload=payload,
        expected_statuses={200, 201},
        timeout=20,
    )


def place_competition_entry(job_id: str, title: str, description: str, tags: List[str]) -> Dict[str, Any]:
    log.info(f"COMPETITION: generating entry for {job_id[:8]}...")
    tg_send(f"🏆 <b>Competition detected</b>\nGenerating entry for:\n<b>{tg_escape(title[:60])}</b>")

    deliverable = generate_deliverable(title, description, tags)
    if not deliverable:
        log.error(f"Failed to generate entry for competition {job_id[:8]}")
        return {"error": "generation failed"}

    if DRY_RUN:
        log.info(f"DRY-RUN ENTRY: {job_id[:8]}")
        return {"ok": True, "status": "submitted", "dry_run": True}

    return request_json(
        "POST",
        f"https://market.near.ai/v1/jobs/{job_id}/entries",
        headers=NEAR_HEADERS,
        payload={"deliverable": deliverable},
        expected_statuses={200, 201},
        timeout=30,
    )


def submit_work(job_id: str, deliverable: str) -> Dict[str, Any]:
    if DRY_RUN:
        log.info(f"DRY-RUN SUBMIT: {job_id[:8]}")
        return {"ok": True, "status": "submitted", "dry_run": True}

    return request_json(
        "POST",
        f"https://market.near.ai/v1/jobs/{job_id}/submit",
        headers=NEAR_HEADERS,
        payload={"deliverable": deliverable},
        expected_statuses={200, 201},
        timeout=30,
    )


def fetch_all_bids() -> List[Dict[str, Any]]:
    bids: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100

    while True:
        batch = request_list(
            "GET",
            f"https://market.near.ai/v1/agents/me/bids?limit={page_size}&offset={offset}",
            headers=NEAR_HEADERS,
            timeout=15,
            expected_statuses={200},
        )
        if not batch:
            break

        bids.extend(batch)
        offset += len(batch)
        if len(batch) < page_size:
            break

    return bids


def handle_revision(bid: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Handle a request-changes status by generating and submitting a revision."""
    job_id = bid.get("job_id")
    bid_id = bid.get("bid_id")

    revision_counts = state.setdefault("revision_counts", {})
    current_count = revision_counts.get(job_id, 0)

    if current_count >= MAX_REVISIONS:
        log.info(f"REVISION SKIP: {job_id[:8]} max revisions ({MAX_REVISIONS}) reached")
        return

    revision_op = op_key("revision", f"{job_id}_{current_count + 1}")
    if op_status(state, revision_op) == "done":
        return

    log.info(f"REVISION {current_count + 1}/{MAX_REVISIONS}: generating for {job_id[:8]}...")

    job = get_job_details(job_id)
    if not job:
        return

    title = job.get("title", "")
    description = job.get("description", "")
    tags = job.get("tags", [])

    # Try to get feedback from comments
    feedback = bid.get("feedback") or bid.get("review_comment") or ""
    if not feedback and bid_id:
        comments = fetch_bid_comments(bid_id)
        if comments:
            feedback = "\n".join(
                c.get("text", "") for c in comments[-3:] if c.get("text")
            )

    if not feedback:
        feedback = "The client requested changes. Please improve the quality and completeness of the deliverable."

    previous = bid.get("deliverable") or ""

    tg_send(
        f"🔄 <b>Revision requested</b> ({current_count + 1}/{MAX_REVISIONS})\n"
        f"Job: <b>{tg_escape(title[:60])}</b>\n"
        f"Feedback: {tg_escape(feedback[:100])}"
    )

    set_op_status(state, revision_op, "in_progress")
    revised = generate_revision(title, description, tags, previous, feedback)
    if not revised:
        tg_send(f"⚠️ Revision generation failed for {job_id[:8]}")
        set_op_status(state, revision_op, "failed")
        return

    result = submit_work(job_id, revised)
    if result.get("error"):
        log.error(f"Revision submit error: {result['error']}")
        set_op_status(state, revision_op, "failed")
    else:
        revision_counts[job_id] = current_count + 1
        set_op_status(state, revision_op, "done")
        bump_stat(state, "revisions_handled")
        bump_stat(state, "submitted")
        log.info(f"REVISION SUBMITTED: {job_id[:8]} round {current_count + 1}")
        tg_send(
            f"✅ <b>Revision submitted</b> ({current_count + 1}/{MAX_REVISIONS})\n"
            f"Job: <code>{job_id}</code>"
        )
        save_state(state)


def handle_bid_comments(bids: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
    """Check for new comments on our bids and reply via Claude."""
    replied_comments = state.setdefault("replied_comments", [])

    for bid in bids:
        bid_id = bid.get("bid_id")
        if not bid_id:
            continue

        status = bid.get("status", "")
        if status not in ("pending", "accepted"):
            continue

        comments = fetch_bid_comments(bid_id)
        if not comments:
            continue

        for comment in comments:
            comment_id = comment.get("comment_id") or comment.get("id")
            if not comment_id or comment_id in replied_comments:
                continue

            # Skip our own comments
            author = comment.get("author") or comment.get("user_id") or ""
            if "agent" in str(author).lower():
                replied_comments.append(comment_id)
                continue

            comment_text = comment.get("text", "").strip()
            if not comment_text:
                replied_comments.append(comment_id)
                continue

            job_title = bid.get("job_title") or bid.get("title") or ""
            log.info(f"COMMENT on bid {bid_id[:8]}: {comment_text[:80]}")

            reply = generate_comment_reply(job_title, comment_text)
            if reply:
                result = post_bid_comment(bid_id, reply)
                if not result.get("error"):
                    log.info(f"REPLIED to comment on {bid_id[:8]}: {reply[:80]}")
                    tg_send(
                        f"💬 <b>Replied to comment</b>\n"
                        f"Comment: {tg_escape(comment_text[:80])}\n"
                        f"Reply: {tg_escape(reply[:80])}"
                    )

            replied_comments.append(comment_id)

        # Keep only last 500 to prevent unbounded growth
        if len(replied_comments) > 500:
            state["replied_comments"] = replied_comments[-300:]

    save_state(state)


def check_bid_statuses(state: Dict[str, Any]) -> None:
    bids = fetch_all_bids()
    if not bids:
        return

    for bid in bids:
        bid_id = bid.get("bid_id")
        job_id = bid.get("job_id")
        status = bid.get("status")
        prev_status = state["bid_statuses"].get(bid_id)

        if prev_status == status:
            continue

        state["bid_statuses"][bid_id] = status

        if status == "accepted":
            log.info(f"ACCEPTED: job={job_id}")
            bump_stat(state, "accepted")
            # Record timestamp for unpaid alert tracking
            state.setdefault("accepted_timestamps", {})[job_id] = datetime.now(timezone.utc).isoformat()
            # Track win in job memory
            job = get_job_details(job_id)
            if job:
                update_job_memory(state, job.get("tags", []), won=True)

            tg_send(
                f"✅ <b>BID ACCEPTED</b>\n"
                f"Job: <code>{job_id}</code>\n"
                f"{'🤖 Generating and submitting deliverable automatically...' if AUTO_SUBMIT else '⚠️ Manual submit required.'}"
            )
            if AUTO_SUBMIT and job_id not in state.get("submitted_jobs", []):
                auto_submit_job(job_id, state)

        elif status == "rejected":
            log.info(f"REJECTED: job={job_id}")
            bump_stat(state, "rejected")
            # Clear accepted timestamp (no longer pending payment)
            state.get("accepted_timestamps", {}).pop(job_id, None)
            # Track loss in job memory
            job = get_job_details(job_id)
            if job:
                update_job_memory(state, job.get("tags", []), won=False)

            tg_send(f"❌ Bid rejected\nJob: <code>{job_id}</code>")

        elif status == "completed":
            log.info(f"COMPLETED: job={job_id}")
            bump_stat(state, "completed")
            # Clear accepted timestamp — payment received
            state.get("accepted_timestamps", {}).pop(job_id, None)
            # Track revenue
            budget = float(bid.get("amount") or bid.get("budget_amount") or 0)
            if budget > 0:
                bump_stat(state, "revenue_near", budget)

            completed_job = get_job_details(job_id)
            title = completed_job.get("title", "") if completed_job else ""
            amount_str = f"{budget:.2f} NEAR" if budget > 0 else "unknown"
            tg_send(
                f"💰 <b>Work accepted, payout received!</b>\n"
                f"Job: <code>{job_id}</code>\n"
                f"<b>{tg_escape(title[:60])}</b>\n"
                f"Amount: {amount_str}"
            )

        elif status in ("request-changes", "request_changes", "revision_requested"):
            log.info(f"REVISION REQUESTED: job={job_id}")
            if AUTO_SUBMIT:
                handle_revision(bid, state)

    # Handle comments (multi-round conversation)
    if CLAUDE_API_KEY and not DRY_RUN:
        handle_bid_comments(bids, state)

    save_state(state)


def auto_submit_job(job_id: str, state: Dict[str, Any]) -> None:
    submit_op = op_key("submit", job_id)
    if op_status(state, submit_op) == "done":
        log.info(f"SUBMIT SKIP: {job_id[:8]} already completed")
        return

    log.info(f"Generating deliverable for {job_id}...")

    job = get_job_details(job_id)
    if not job:
        tg_send(f"⚠️ Could not fetch job details for {job_id[:8]}")
        return

    if not is_job_open(job):
        log.info(f"SUBMIT SKIP: {job_id[:8]} state is not open")
        return

    # Check assignment status before submitting — avoid "must be in_progress" error
    my_assignments = job.get("my_assignments") or []
    for a in my_assignments:
        a_status = a.get("status", "")
        if a_status == "submitted":
            log.info(f"SUBMIT SKIP: {job_id[:8]} assignment already submitted")
            state.setdefault("submitted_jobs", []).append(job_id)
            set_op_status(state, submit_op, "done")
            return
        if a_status not in ("in_progress", "accepted", ""):
            log.info(f"SUBMIT SKIP: {job_id[:8]} assignment status is '{a_status}'")
            return

    title = job.get("title", "")
    description = job.get("description", "")
    tags = job.get("tags", [])

    log.info(f"Job: {title}")
    tg_send(f"📝 Generating deliverable for:\n<b>{tg_escape(title)}</b>")

    # Check for unknown actions bot cannot perform
    unknown_action = _detect_unknown_action(tags, title, description)
    if unknown_action:
        tg_send(
            f"🤔 <b>Нужна помощь!</b>\n"
            f"Job: <b>{tg_escape(title[:60])}</b>\n"
            f"Требует: <b>{unknown_action}</b> — бот не умеет\n"
            f"ID: <code>{job_id}</code>\n"
            f"🔗 <a href='https://market.near.ai/jobs/{job_id}'>Открыть задачу</a>"
        )
        log.info(f"UNKNOWN ACTION SKIP: {job_id[:8]} requires {unknown_action}")
        return

    # Try real external actions for jobs that require them
    npm_url = None
    github_url = None
    gist_url = None
    near_deploy_url = None
    if _is_npm_job(tags, title, description) and NPM_TOKEN:
        npm_url = publish_npm_package(job_id, title, description, tags)
    if _is_github_job(tags, title, description) and GITHUB_TOKEN:
        github_url = create_github_repo(job_id, title, description, tags)
    if _is_gist_job(tags, title, description) and GITHUB_TOKEN:
        gist_url = create_github_gist(job_id, title, description, tags)
    if _is_near_deploy_job(tags, title, description):
        near_deploy_url = deploy_near_contract(job_id, title, description, tags)

    deliverable = generate_deliverable(title, description, tags)
    if not deliverable:
        tg_send(f"⚠️ Deliverable generation failed for {job_id[:8]}. Submit manually.")
        return

    # Append real action URLs to deliverable
    if npm_url:
        deliverable += f"\n\n---\n## Published Package\n\n📦 npm: {npm_url}\n\nInstall: `npm install {npm_url.split('/')[-1]}`"
    if github_url:
        deliverable += f"\n\n---\n## GitHub Repository\n\n🐙 Repository: {github_url}"
    if gist_url:
        deliverable += f"\n\n---\n## GitHub Gist\n\n📎 Gist: {gist_url}"
    if near_deploy_url:
        deliverable += f"\n\n---\n## Deployed Contract\n\n🚀 Account: `{NEAR_DEPLOY_ACCOUNT}`\n🔗 Explorer: {near_deploy_url}"

    time.sleep(SUBMIT_DELAY)

    set_op_status(state, submit_op, "in_progress")
    result = submit_work(job_id, deliverable)
    if result.get("error"):
        err = str(result["error"])
        log.error(f"Submit error: {err}")
        # For transient server errors, clear status so next cycle retries
        if "http 5" in err or "500" in err or "502" in err or "503" in err:
            tg_send(
                f"⚠️ Submit 5xx error (will retry next cycle)\n"
                f"Job: <code>{job_id}</code>\n{tg_escape(err[:100])}"
            )
            set_op_status(state, submit_op, "")
        else:
            tg_send(f"⚠️ Submit error: {tg_escape(err[:100])}\nJob: <code>{job_id}</code>")
            set_op_status(state, submit_op, "failed")
    else:
        log.info(f"SUBMITTED: job={job_id}")
        state["submitted_jobs"].append(job_id)
        set_op_status(state, submit_op, "done")
        bump_stat(state, "submitted")
        save_state(state)
        tg_send(
            f"🚀 <b>Work submitted</b>\n"
            f"Job: <code>{job_id}</code>\n"
            f"Waiting for review and payout..."
        )


def auto_bid(state: Dict[str, Any]) -> int:
    job_memory = state.get("job_memory", {})
    jobs = fetch_all_jobs(state)
    ranked_jobs = sorted(jobs, key=lambda j: score_job(j, job_memory), reverse=True)
    log.info(
        f"Found jobs: {len(jobs)}, already bid: {len(state['bid_jobs'])}, "
        f"ranked candidates: {len(ranked_jobs)}"
    )
    new_bids = 0
    new_entries = 0
    actions_done = 0

    if "competition_entries" not in state:
        state["competition_entries"] = []
    if "competition_skipped" not in state:
        state["competition_skipped"] = []

    for j in ranked_jobs:
        if actions_done >= MAX_ACTIONS_PER_CYCLE:
            break

        job_id = j["job_id"]
        job_type = j.get("job_type", "standard")
        tags = j.get("tags", [])
        title = j.get("title", "")
        score = score_job(j, job_memory)

        if score < MIN_JOB_SCORE:
            continue

        if job_type == "competition":
            entry_op = op_key("entry", job_id)
            if job_id in state["competition_entries"] or job_id in state["competition_skipped"]:
                continue
            if op_status(state, entry_op) == "done":
                continue

            if not is_job_open(j):
                state["competition_skipped"].append(job_id)
                log.info(f"COMPETITION SKIP: {job_id[:8]} state is not open")
                continue

            description = j.get("description", "")
            set_op_status(state, entry_op, "in_progress")
            result = place_competition_entry(job_id, title, description, tags)
            if result.get("error"):
                error_text = str(result["error"])
                if "expected open" in error_text or "state judging" in error_text or "closed" in error_text:
                    state["competition_skipped"].append(job_id)
                    log.info(f"COMPETITION SKIP: {job_id[:8]} not open ({error_text})")
                    set_op_status(state, entry_op, "skipped")
                else:
                    log.warning(f"COMPETITION FAIL: {job_id[:8]} -> {error_text}")
                    set_op_status(state, entry_op, "failed")
            else:
                state["competition_entries"].append(job_id)
                new_entries += 1
                actions_done += 1
                set_op_status(state, entry_op, "done")
                bump_stat(state, "total_entries")
                budget = j.get("budget_amount", "?")
                log.info(f"COMPETITION ENTRY: {budget} NEAR score={score:.2f} {title[:50]}")
                tg_send(f"🏆 <b>Competition entry submitted</b>\n<b>{tg_escape(title[:60])}</b>\nBudget: {budget} NEAR")
                save_state(state)
            time.sleep(BID_DELAY)
            continue

        if job_id in state["bid_jobs"]:
            continue

        bid_op = op_key("bid", job_id)
        if op_status(state, bid_op) == "done":
            continue

        amount, eta_seconds = choose_bid_terms(j, score)
        set_op_status(state, bid_op, "in_progress")
        res = place_bid(job_id, tags, amount=amount, eta_seconds=eta_seconds)
        status = res.get("status", res.get("error", "?"))

        if status == "pending":
            state["bid_jobs"].append(job_id)
            new_bids += 1
            actions_done += 1
            set_op_status(state, bid_op, "done")
            bump_stat(state, "total_bids")
            save_state(state)
            log.info(
                f"BID: {j.get('budget_amount', '?')} NEAR bc={j.get('bid_count', '?')} "
                f"score={score:.2f} amount={amount} eta={eta_seconds}s {title[:50]}"
            )
        elif "already exists" in str(status):
            state["bid_jobs"].append(job_id)
            set_op_status(state, bid_op, "done")
        elif "not open" in str(status).lower() or "closed" in str(status).lower():
            log.info(f"BID SKIP: {job_id[:8]} not open ({status})")
            set_op_status(state, bid_op, "skipped")
        else:
            log.warning(f"BID FAIL: {job_id[:8]} -> {status}")
            set_op_status(state, bid_op, "failed")

        time.sleep(BID_DELAY)

    if new_bids > 0:
        log.info(f"New bids: {new_bids}, total: {len(state['bid_jobs'])}")
        tg_send(f"🤖 New bids: <b>{new_bids}</b>\nTotal bids: {len(state['bid_jobs'])}")

    if new_entries > 0:
        log.info(f"New competition entries: {new_entries}")

    return new_bids + new_entries


WS_URL = os.environ.get("WS_URL", "wss://market.near.ai/v1/ws")


def _ws_listener(trigger_event: threading.Event) -> None:
    """WebSocket listener for real-time job notifications. Signals main loop on any event."""
    _first_connect = True
    try:
        import websocket
    except ImportError:
        log.warning("websocket-client not installed, skipping WebSocket listener")
        return

    def on_message(ws, message):
        log.info(f"WS event: {str(message)[:120]}")
        trigger_event.set()

    def on_error(ws, error):
        log.warning(f"WS error: {error}")

    def on_close(ws, code, msg):
        log.info("WS connection closed, will reconnect in 30s")

    def on_open(ws):
        nonlocal _first_connect
        log.info("WS connected — real-time notifications active")
        if _first_connect:
            tg_send("🔌 <b>WebSocket connected</b> — real-time notifications active")
            _first_connect = False

    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                header=[f"Authorization: Bearer {NEAR_API_KEY}"],
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.warning(f"WS failed: {e}")
        time.sleep(30)


def main() -> None:
    if not NEAR_API_KEY:
        raise RuntimeError("NEAR_KEY is required")
    if AUTO_SUBMIT and not DRY_RUN and not _LLM_KEY:
        log.warning("AUTO_SUBMIT is ON but no LLM key (CLAUDE_KEY or OPENROUTER_KEY) is set; auto-submit may fail.")

    log.info("=== NEAR AutoBot v1 started ===")
    log.info(f"Mode: {'DRY-RUN' if DRY_RUN else 'LIVE'} | State: {'SQLite' if USE_SQLITE else 'JSON'}")
    log.info(
        f"Strategy: min_score={MIN_JOB_SCORE:.2f}, max_actions_per_cycle={MAX_ACTIONS_PER_CYCLE}, "
        f"retries={REQUEST_MAX_RETRIES}"
    )

    # Wallet balance check
    balance = check_wallet_balance()
    balance_str = f"{balance:.2f} NEAR" if balance is not None else "unavailable"
    log.info(f"Wallet balance: {balance_str}")

    # Stats summary
    state = load_state()
    stats = state.get("stats", {})
    if not stats.get("started_at"):
        stats["started_at"] = datetime.now(timezone.utc).isoformat()
        state["stats"] = stats
        save_state(state)

    win_rate = 0.0
    decided = stats.get("accepted", 0) + stats.get("rejected", 0)
    if decided > 0:
        win_rate = stats["accepted"] / decided * 100

    # Register service if enabled
    if ENABLE_SERVICES:
        try:
            ensure_service_registered(state)
            save_state(state)
        except Exception as e:
            log.error(f"Service registration failed: {e}")

    tg_send(
        f"🚀 <b>NEAR AutoBot v1 started</b>\n"
        f"Mode: {'DRY-RUN' if DRY_RUN else 'LIVE'}\n"
        f"Model: claude-sonnet-4-6 (8K tokens)\n"
        f"💰 Wallet: {balance_str}\n"
        f"State store: {'SQLite' if USE_SQLITE else 'JSON'}\n"
        f"Filters: budget &gt;= {MIN_BUDGET} NEAR, bid_count &lt;= {MAX_BID_COUNT}\n"
        f"Strategy: min_score &gt;= {MIN_JOB_SCORE:.2f}, max actions/cycle = {MAX_ACTIONS_PER_CYCLE}\n"
        f"Skip tags: {', '.join(SKIP_TAGS[:5])}...\n"
        f"Interval: every {CHECK_INTERVAL // 60} min\n"
        f"Auto-submit: {'ON' if AUTO_SUBMIT else 'OFF'}\n"
        f"Max revisions: {MAX_REVISIONS}\n"
        f"Services: {'ON (id=' + str(state.get('service_id', '?')) + ')' if ENABLE_SERVICES else 'OFF'}\n"
        f"📊 Stats: {stats.get('total_bids', 0)} bids, {stats.get('accepted', 0)} won, "
        f"{win_rate:.0f}% win rate, {stats.get('revenue_near', 0):.1f} NEAR earned\n"
        f"🧠 Job memory: {len(state.get('job_memory', {}))} tags tracked"
    )

    # Start WebSocket listener in background thread
    ws_trigger = threading.Event()
    ws_thread = threading.Thread(target=_ws_listener, args=(ws_trigger,), daemon=True)
    ws_thread.start()

    # Start Telegram command listener in background thread
    _tg_state_ref.append(state)
    tg_cmd_thread = threading.Thread(target=_tg_command_listener, daemon=True)
    tg_cmd_thread.start()

    cycle_count = 0

    while True:
        try:
            cycle_count += 1
            triggered_by_ws = ws_trigger.is_set()
            ws_trigger.clear()
            log.info(f"--- Check cycle #{cycle_count}{' (WS triggered)' if triggered_by_ws else ''} ---")
            check_bid_statuses(state)
            auto_bid(state)

            # Handle assigned jobs (services / instant jobs)
            if ENABLE_SERVICES:
                try:
                    handle_assigned_jobs(state)
                except Exception as e:
                    log.error(f"handle_assigned_jobs error: {e}")

            save_state(state)

            # Update health dashboard
            bal = check_wallet_balance() if cycle_count % 12 == 0 else balance
            write_health_status(state, cycle_count, bal)

            # Check agent earnings every 12 cycles (~1 hour)
            if cycle_count % 12 == 0:
                check_agent_earnings(state)
                auto_withdraw_if_needed(bal)

            # Check for unpaid accepted jobs every 6 cycles (~30 min)
            if cycle_count % 6 == 0:
                check_unpaid_accepted_jobs(state)

            # Keep TG state ref in sync
            if _tg_state_ref:
                _tg_state_ref[0] = state

            # Summary every 8 hours
            last_summary = state.get("last_daily_summary", "")
            if not last_summary or (
                datetime.now(timezone.utc) - datetime.fromisoformat(last_summary)
            ).total_seconds() > 28800:
                send_daily_summary(state, bal)

            # Refresh service profile descriptions once per day
            last_profile = state.get("last_profile_update", "")
            if ENABLE_SERVICES and (
                not last_profile or (
                    datetime.now(timezone.utc) - datetime.fromisoformat(last_profile)
                ).total_seconds() > 86400
            ):
                jobs_done = int(state.get("last_jobs_completed", 0))
                refresh_service_descriptions(state, jobs_done)

        except Exception as e:
            log.error(f"Bot error: {e}")
            tg_send(f"⚠️ Bot error: {tg_escape(str(e))}")

        # Wait for interval OR immediate WS trigger
        ws_trigger.wait(timeout=CHECK_INTERVAL)
        ws_trigger.clear()


if __name__ == "__main__":
    main()
