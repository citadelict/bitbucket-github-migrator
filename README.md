# Bitbucket to GitHub Migrator

A production-ready, concurrent tool to migrate an entire Bitbucket workspace to a GitHub Organization. 

It handles pagination, rate limit retries, and properly duplicates the source repository exactly by preserving:
- Full commit history
- All Branches
- All Tags

## Features

- **Idempotent** — Safe to re-run at any time; already-migrated repos are skipped.
- **Orphan cleanup** — If a GitHub repo is created but the push fails, the empty repo is automatically deleted so it doesn't block future runs.
- **`--retry-failed`** — Detects empty/orphaned repos on GitHub (from previous failed runs) and re-attempts the push.
- **`--dry-run`** — Lists exactly what would be migrated, skipped, or retried without making any changes.
- **Concurrent** — Configurable parallel workers (default 5) for fast bulk migration.
- **Robust retries** — Automatically retries on rate limits (429), server errors (5xx), and transient network issues with exponential backoff.

## Prerequisites

- Python 3.7+
- Git CLI installed on the system (`git`)

## Setup

1. **Install Python Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment:**
   Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
   Fill in the variables in the `.env` file according to the instructions below.

## Obtaining Credentials

### 1. Bitbucket API Token
Bitbucket has deprecated App Passwords in favor of API tokens. You must generate an API token from your Atlassian account.
1. Go to [https://id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens).
2. Click **Create API token**.
3. Give it a label (e.g., `github-migration`) and set an expiry date.
4. Select **Bitbucket** as the application.
5. Grant the following permissions:
   - **Repositories:** `Read`
6. Copy the generated token immediately and paste it as `BITBUCKET_API_TOKEN` in your `.env`.
7. Set `BITBUCKET_EMAIL` in your `.env` to the **email address** of your Atlassian account (not your Bitbucket username).

### 2. GitHub Personal Access Token (PAT)
Both **classic** and **fine-grained** tokens are supported (the script uses `Bearer` auth).

**Classic Token:**
1. Log in to GitHub.
2. Go to **Settings** -> **Developer settings** -> **Personal access tokens** -> **Tokens (classic)**.
3. Click **Generate new token (classic)**.
4. Give it a note (e.g., `bitbucket-migration`) and choose an expiration date.
5. Select the following scopes:
   - `repo` (Full control of private repositories)
   - `delete_repo` (Required for orphan cleanup on push failures)
6. Depending on organization settings, make sure you authorize the token for the specific organization (Configure SSO).
7. Copy the generated token and paste it as `GITHUB_TOKEN` in your `.env`.

**Fine-grained Token:**
1. Go to **Settings** -> **Developer settings** -> **Personal access tokens** -> **Fine-grained tokens**.
2. Set the resource owner to your GitHub Organization.
3. Grant repository permissions: **Administration: Read and Write**, **Contents: Read and Write**.
4. Copy the token and paste it as `GITHUB_TOKEN` in your `.env`.

## Usage

### Standard Migration
Once `.env` is fully populated, simply run:

```bash
python migrate.py
```

### Dry Run (recommended first step)
Preview what will happen without making any changes:

```bash
python migrate.py --dry-run
```

### Retry Failed Migrations
If a previous run left orphaned (empty) repos on GitHub due to push failures, retry them:

```bash
python migrate.py --retry-failed
```

You can combine flags:
```bash
python migrate.py --retry-failed --dry-run
```

## Repository Naming

> **Note:** The GitHub repository name will match the **Bitbucket slug**, not the display name.
> Bitbucket slugs can differ from the display name (e.g., after a rename the slug keeps the original).
> For example, a Bitbucket repo displayed as "My Cool Project" may have a slug of `my-cool-project`.

## Safety & Idempotency
- **Skipping**: Repositories that already exist inside the target GitHub Organization are automatically skipped, meaning you can safely cancel and rerun the script at any time without duplicating work.
- **Orphan cleanup**: If the script creates a GitHub repo but the subsequent `git push --mirror` fails, the empty repo is automatically deleted so it won't be falsely skipped on the next run.
- **`--retry-failed`**: For any edge case where an orphaned repo still exists, this flag detects repos with `size == 0` and re-attempts the push.
- **Cleanup**: Repositories are cloned to a `./temp_clones` directory, which is automatically deleted per-repo and on exit to save disk space.
- **Failures**: Any repository that genuinely fails to push (e.g., extremely large individual file limits in GitHub) will be tallied in the final summary for manual review.
