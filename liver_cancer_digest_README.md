# Liver Cancer Daily Digest

Pure code version of the daily workflow:

1. Query PubMed for liver cancer papers in the last 24 hours
2. Fetch paper metadata and abstracts
3. Score relevance with a simple screening pass
4. Ask the configured AI model to write a Chinese digest from the screened set
5. Send the result to Gmail

## Environment variables

You can either export them in your shell or create a `.env` file next to the script.

```bash
export AI_API_KEY='...'
export AI_BASE_URL='https://integrate.api.nvidia.com/v1'
export AI_MODEL='deepseek-ai/deepseek-v4-pro-0813'
export GMAIL_CLIENT_ID='...'
export GMAIL_CLIENT_SECRET='...'
export GMAIL_REFRESH_TOKEN='...'
export GMAIL_REDIRECT_URI='http://localhost:8765'
export GMAIL_FROM='8611qq@gmail.com'
export EMAIL1='hackwit1024@qq.com'
export EMAIL2='8611qq@gmail.com'
```

Optional:

```bash
export LOOKBACK_HOURS=24
export PUBMED_RETMAX=100
export PUBMED_QUERY='("Liver Neoplasms"[MeSH Terms] OR hepatocellular carcinoma[Title/Abstract] OR HCC[Title/Abstract] OR cholangiocarcinoma[Title/Abstract] OR "liver cancer"[Title/Abstract] OR "hepatic cancer"[Title/Abstract])'
export STATE_FILE="$HOME/.liver_cancer_digest_state.json"
export LOG_FILE="$HOME/.liver_cancer_digest.log"
export SCHEDULE_TIME="08:00"
export SCHEDULE_TZ="Asia/Shanghai"
```

## Run

```bash
/Users/mac/Documents/Codex/2026-08-28/https-github-com-brycewang-stanford-auto/outputs/run_liver_cancer_digest.sh
```

Use `-a` (or `--all`) to force a send even when no new PMIDs are found:

```bash
python3 /Users/mac/Documents/Codex/2026-08-28/https-github-com-brycewang-stanford-auto/outputs/liver_cancer_digest.py -a
```

Use `--daemon` to run on the daily schedule.

## One-time Gmail OAuth setup

```bash
python3 /Users/mac/Documents/Codex/2026-08-28/https-github-com-brycewang-stanford-auto/outputs/liver_cancer_digest.py --gmail-oauth-setup
```

This prints a refresh token you can export as `GMAIL_REFRESH_TOKEN`.
Make sure the redirect URI matches the one registered in Google Cloud exactly, including whether it has a trailing slash.

Each run appends one readable line to `LOG_FILE`, including time, recipient, counts, and success/failure.

## .env file

Copy [`.env.example`](/Users/mac/Documents/Codex/2026-08-28/https-github-com-brycewang-stanford-auto/outputs/.env.example) to `.env` and fill in the values.

## Schedule

Run once daily from a long-running process:

```bash
python3 /Users/mac/Documents/Codex/2026-08-28/https-github-com-brycewang-stanford-auto/outputs/liver_cancer_digest.py --daemon
```

`SCHEDULE_TIME` controls the daily run time in `HH:MM` format. `SCHEDULE_TZ` controls the timezone.

You can also use macOS `launchd`, cron, or systemd to run it daily.

## GitHub Actions

The repository includes `.github/workflows/liver-cancer-digest.yml`. It runs daily at
08:00 Asia/Shanghai (00:00 UTC) and can also be started manually from the
Actions tab.

Add these GitHub Actions Secrets under `Settings -> Secrets and variables -> Actions`:

```text
AI_API_KEY
AI_BASE_URL
AI_MODEL
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
GMAIL_FROM
EMAIL1 / EMAIL2
```

The workflow stores `state.json` in the repository so PMID deduplication
survives between runs. The run log is uploaded as a 30-day artifact instead of
being committed, because it contains recipient information. Enable the workflow's default
`GITHUB_TOKEN` write permission if repository settings restrict workflow writes.

The manual `workflow_dispatch` form has a `force_send` option. Set it to true
to run with `--all` and send even when no new PMID is found.
