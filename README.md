# NEAR Market Agent

Fully autonomous AI agent for [market.near.ai](https://market.near.ai).
Discovers jobs, bids with smart pricing, registers as a service for instant jobs, executes work via OpenRouter AI, submits deliverables, handles revisions, and learns from outcomes.
No human involvement required after launch.

## Live stats

| Metric | Value |
|---|---|
| Bids placed (first 24h) | **800+** |
| Job types handled | Standard + Competition + Instant (Services) |
| Uptime | Continuous (VPS) |
| AI Model | DeepSeek V3 via OpenRouter (8K tokens) |
| Revision support | Up to 3 rounds per job |
| Real-time | WebSocket notifications |

## Quick start

### Option 1: VPS / bare metal

```bash
git clone https://github.com/hg90zxt/near-market-agent
cd near-market-agent
pip install -r requirements.txt
```

Add keys to `~/.bashrc` once:
```bash
echo 'export NEAR_KEY="your_near_api_key"' >> ~/.bashrc
echo 'export OPENROUTER_KEY="your_openrouter_key"' >> ~/.bashrc
echo 'export TG_BOT_TOKEN="your_tg_token"' >> ~/.bashrc
echo 'export TG_CHAT_ID="your_chat_id"' >> ~/.bashrc
echo 'export AGENT_ID="your_agent_id"' >> ~/.bashrc
echo 'export NPM_TOKEN="your_npm_token"' >> ~/.bashrc
echo 'export GITHUB_TOKEN="your_github_token"' >> ~/.bashrc
echo 'export NEAR_DEPLOY_ACCOUNT="yourname.testnet"' >> ~/.bashrc
source ~/.bashrc
```

Start / restart:
```bash
pkill -f autobot.py; sleep 2; nohup python3 autobot.py >> autobot.log 2>&1 &
```

### Option 2: Docker

```bash
git clone https://github.com/hg90zxt/near-market-agent
cd near-market-agent
docker build -t near-market-agent .
docker run -d --restart=always \
  -e NEAR_KEY="your_near_api_key" \
  -e OPENROUTER_KEY="your_openrouter_key" \
  -e TG_BOT_TOKEN="your_tg_token" \
  -e TG_CHAT_ID="your_chat_id" \
  -e AGENT_ID="your_agent_id" \
  -e NPM_TOKEN="your_npm_token" \
  -e GITHUB_TOKEN="your_github_token" \
  -e NEAR_DEPLOY_ACCOUNT="yourname.testnet" \
  --name near-market-agent near-market-agent
```

## How it works

```
Every 5 minutes (or instantly via WebSocket):
  1. Fetch all open jobs       (paginated, sorted by competition)
  2. Score & rank jobs         (budget x competition x bid pressure x tag fit x job memory)
  3. For each top job:
     - Standard job  -> POST /v1/jobs/{id}/bids  with smart proposal + adaptive price
     - Competition   -> generate deliverable via OpenRouter -> POST /v1/jobs/{id}/entries
  4. Check bid statuses        -> if accepted -> generate deliverable -> submit
  5. Handle assigned jobs      -> instant jobs from service registry -> auto-deliver
  6. Handle revision requests  -> read feedback -> regenerate -> resubmit (up to 3x)
  7. Perform real actions      -> npm publish / GitHub repo / NEAR contract deploy
  8. Update job memory         -> learn which tags win/lose
  9. Save state to SQLite
```

## Real action execution

Beyond generating text deliverables, the agent performs real external actions:

| Action | Tool | Required env var |
|---|---|---|
| Publish npm package | npmjs.com | `NPM_TOKEN` |
| Create GitHub repository | GitHub API | `GITHUB_TOKEN` |
| Publish GitHub Gist | GitHub API | `GITHUB_TOKEN` |
| Deploy NEAR smart contract | near-cli + Rust | `NEAR_DEPLOY_ACCOUNT` |

Jobs requiring actions the bot cannot perform (Twitter, Google Docs, app stores) are automatically skipped. High-budget skipped jobs (≥5 NEAR) trigger a Telegram alert for manual review.

## Service registry

The bot auto-registers as a service on market.near.ai at startup.
When someone creates an instant job matching the bot's profile, it gets auto-assigned and delivers work immediately.

```
POST /v1/agents/me/services
  -> name, category, pricing_model, price, tags, response_time
  -> Appears in https://market.near.ai/services
```

## Job scoring

Each job gets a score `0.0-1.15`:

| Factor | Weight | Description |
|---|---|---|
| Budget | 45% | Higher budget -> higher score |
| Bid pressure | 35% | Fewer competitors -> higher score |
| Tag fit | 20% | Code/docs/research tags boost score |
| Job memory | +/- | Tags from won jobs get boosted, lost tags get penalized |
| Competition bonus | +0.15 | Extra weight for competition jobs |

Only jobs with `score >= 0.25` are bid on.

## Smart bid pricing

| Job type | Bid % of budget |
|---|---|
| Budget ≤ 2 NEAR | 100% (no room to discount) |
| Blog / SEO / Newsletter / Writing | 95% |
| Research / Analysis / Audit | 90% |
| High score (>= 0.90) | 90% |
| Medium score (>= 0.75) | 85% |
| Default | 80% |

## Smart deliverables

Output format is auto-detected from job tags and description:

| Tags / Keywords | Format |
|---|---|
| code, smart-contract, solidity | Markdown with code blocks + GitHub Gist link |
| blog, seo, article, copywriting | Full blog post (headline, sections, CTA) |
| newsletter, email | Newsletter with 3 subject line options |
| research, analysis, report | Structured report with sections |
| documentation, docs, tutorial | Technical documentation |
| meme, marketing, social, tweet | Marketing copy with variations |
| Default | General professional deliverable |

## Features

- **Full job lifecycle** — discover, bid, execute, submit, track, revise
- **WebSocket** — real-time job notifications, instant response to new assignments
- **Real action execution** — npm publish, GitHub repo/gist, NEAR contract deploy
- **Smart filtering** — skips jobs requiring unavailable actions, alerts on high-budget skips
- **Zero-balance filter** — skips jobs where requester has no funds
- **Client blacklist** — blocks known non-paying or abusive job posters
- **Service registry** — auto-registers on market.near.ai for instant job matching
- **Assignment messaging** — private communication channel with job requesters
- **Smart proposals** — 5 templates matched to job tags (code / docs / research / marketing / NEAR)
- **Smart bid pricing** — bid % based on job type (blog/seo=95%, research=90%, etc.)
- **Revision handling** — reads feedback via assignment messages, regenerates up to 3 times
- **Job memory** — tracks which tags lead to wins/losses, adjusts scoring over time
- **Competition support** — detects `job_type: competition`, generates and submits full entry
- **Earnings monitoring** — live stats from marketplace API, notifies on new payments
- **Unpaid job alerts** — Telegram alert if accepted job not paid after 6h
- **Auto-dispute** — optionally opens dispute for unpaid jobs after 24h
- **Auto-withdraw** — withdraws balance above threshold to your NEAR wallet
- **Telegram commands** — `/status`, `/balance`, `/dispute`, `/withdraw`, `/help`
- **Daily summary** — Telegram report every 8 hours with stats and balance
- **Stats tracking** — bids, wins, losses, win rate, revenue earned, revisions handled
- **Retry / backoff** — handles `429` and `5xx` errors automatically (up to 4 retries)
- **SQLite state** — persistent across restarts, with JSON fallback
- **Dry-run mode** — test without placing real bids: `DRY_RUN=true python3 autobot.py`
- **Path traversal protection** — LLM-generated filenames are sandboxed

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `NEAR_KEY` | — | NEAR market API key (required) |
| `OPENROUTER_KEY` | — | OpenRouter API key (required) |
| `TG_BOT_TOKEN` | — | Telegram bot token |
| `TG_CHAT_ID` | — | Telegram chat ID |
| `AGENT_ID` | — | Your agent ID on market.near.ai |
| `NPM_TOKEN` | — | npm automation token for package publishing |
| `GITHUB_TOKEN` | — | GitHub token (repo scope) for repo/gist creation |
| `NEAR_DEPLOY_ACCOUNT` | — | NEAR testnet account for contract deployment (e.g. `yourname.testnet`) |
| `DRY_RUN` | `false` | Safe testing mode |
| `USE_SQLITE` | `true` | Use SQLite instead of JSON |
| `MIN_JOB_SCORE` | `0.25` | Minimum score to bid |
| `MAX_ACTIONS_PER_CYCLE` | `100` | Max bids/entries per cycle |
| `AUTO_SUBMIT` | `true` | Auto-generate and submit deliverables |
| `MAX_REVISIONS` | `3` | Max revision rounds per job |
| `SERVICE_PRICE` | `3.5` | Service price in NEAR |

## Get API keys

- **NEAR_KEY** — [market.near.ai](https://market.near.ai) → Settings → API Keys
- **OPENROUTER_KEY** — [openrouter.ai](https://openrouter.ai)
- **NPM_TOKEN** — [npmjs.com](https://npmjs.com) → Access Tokens → Automation
- **GITHUB_TOKEN** — GitHub → Settings → Developer settings → Personal access tokens → `repo` scope
- **NEAR testnet account** — `near create-account yourname.testnet --useFaucet --networkId testnet`

## Architecture

```
autobot.py
├── fetch_all_jobs()              -- paginated job discovery
├── score_job()                   -- multi-factor scoring + job memory
├── auto_bid()                    -- bid on top ranked jobs
├── check_bid_statuses()          -- track accepted/rejected, trigger submit
├── auto_submit_job()             -- generate + submit deliverable
├── generate_deliverable()        -- DeepSeek V3 via OpenRouter (8K tokens)
├── generate_revision()           -- revise based on feedback
├── publish_npm_package()         -- compile and publish to npmjs.com
├── create_github_repo()          -- create repo and push code via GitHub API
├── create_github_gist()          -- publish gist via GitHub API
├── deploy_near_contract()        -- compile Rust + deploy to NEAR testnet
├── register_service()            -- POST /v1/agents/me/services
├── handle_assigned_jobs()        -- process instant jobs + assignments
├── check_agent_earnings()        -- monitor earnings via agent API
├── send_daily_summary()          -- Telegram summary every 8h
├── _ws_listener()                -- WebSocket real-time notifications
└── load/save_state()             -- SQLite + JSON fallback
```

## License

MIT
