# curmon24 → AWS Lambda (Stage) — Design

**Date:** 2026-07-16
**Status:** Approved

## Goal

Move the `curmon24` UAH/USD rate monitor from a GitHub Actions cron job to an
AWS Lambda invoked by EventBridge cron rules, deployed via CloudFormation to the
**Stage account only**. Provide a `CloudFormation/Template.yaml` and a
standalone `deploy.sh`, following the conventions of
`~/vhosts/www/lambda/image-uploader/` (but lighter — no Docker/ant).

## Decisions

| Question | Decision |
|----------|----------|
| State + rate-log storage | Single S3 data bucket holding `state.json` + `rates.csv` |
| Telegram secrets | Plain Lambda env vars (via `init.stage.args`, gitignored) |
| Environments | Stage only (Stage account) |
| Build/deploy | Lightweight standalone `deploy.sh`: pip → zip → s3 cp → cloudformation deploy |
| AWS profile / region | `dev-ven-com` / `us-east-1` (overridable at top of `deploy.sh`) |

## Architecture

```
EventBridge cron rules (4) ──▶ Lambda: Curmon24-Stage-monitor ──▶ Telegram API
                                     │  (fetch Privat / minfin, notify)
                                     └──▶ S3 data bucket (state.json + rates.csv)
```

## Components

### monitor.py
- `DATA_DIR` becomes env-configurable: `os.environ.get("DATA_DIR", <repo>/data)`.
  Lambda sets `DATA_DIR=/tmp/data` (only writable path on Lambda).
- New `lambda_handler(event, context)`:
  1. Remove any stale `/tmp/data` files, then download `state.json` + `rates.csv`
     from the S3 data bucket (missing object → fresh start).
  2. Call the existing `main()` unchanged.
  3. Upload `state.json` + `rates.csv` back to S3.
- S3 is the source of truth (download at start, upload at end, every invocation).
- `boto3` imported lazily inside the handler so local `--debug` runs need no boto3.
- `--debug` local mode unchanged (no `DATA_BUCKET` → uses repo `data/`).

### CloudFormation/Template.yaml
- **S3 data bucket** — `DeletionPolicy: Retain`, AES256, versioning enabled.
- **IAM role** — `AWSLambdaBasicExecutionRole` + `s3:GetObject`/`s3:PutObject` on
  the data bucket only.
- **Lambda** — `python3.13`, handler `monitor.lambda_handler`, code from the CI
  deployment bucket zip, timeout 60 s, 256 MB. Env vars: `TELEGRAM_BOT_TOKEN`
  (NoEcho), `TELEGRAM_CHAT_ID`, `ALERT_THRESHOLD`, `DATA_BUCKET`, `DATA_DIR=/tmp/data`.
- **4 EventBridge rules** (UTC) replicating the GitHub schedule, each with a
  Lambda invoke permission:
  - `cron(0/15 6-15 ? * MON-FRI *)` — interbank session, every 15 min
  - `cron(7 16-23/2 ? * * *)` — evenings, every 2 h
  - `cron(7 0-5/2 ? * * *)` — nights, every 2 h
  - `cron(7 6-15/2 ? * SUN,SAT *)` — weekends, every 2 h

### deploy.sh (Stage-only)
- `AWS_PROFILE=dev-ven-com`, `REGION=us-east-1` (overridable at top).
- pip install `requirements.txt` into `build/`, copy `monitor.py`, zip to
  `Lambda.zip`, `aws s3 cp` to `ven-ci-<account>-<region>/Curmon24-Stage/<ts>/`,
  `aws cloudformation deploy` with `--parameter-overrides` from `init.stage.args`.
- Stack name `Curmon24-Stage`.

### init.stage.args
- `S3DataBucketName`, `TelegramChatId`, `AlertThreshold`, `TelegramBotToken`.
- **Gitignored** — carries the token, and this repo's history was public.

### requirements
- Lambda build adds `tzdata` so `zoneinfo` `Europe/Kyiv` resolves on Lambda.
- `requests`, `beautifulsoup4` are pure-Python → plain `pip install --target`,
  no manylinux wheel handling needed.

## Trade-offs / notes
- `rates.csv` is rewritten wholesale each run (S3 has no append). Fine at this
  volume; grows slowly.
- No explicit concurrency lock; EventBridge fires one invocation per schedule
  tick at this cadence — matches the old workflow `concurrency` group.
