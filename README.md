# Bitbucket to GitHub Migrator

A production-ready, concurrent tool to migrate an entire Bitbucket workspace to a GitHub Organization. 

It handles pagination, rate limit retries, and properly duplicates the source repository exactly by preserving:
- Full commit history
- All Branches
- All Tags

## Prerequisites

- Python 3.7+
- Git CLI installed on the system `git`

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

### 1. Bitbucket App Password
You cannot use your normal Bitbucket login password. You must generate an App Password.
1. Log in to Bitbucket.
2. Click your profile avatar in the bottom left -> **Personal Settings**.
3. Under Access Management, click **App passwords**.
4. Click **Create app password**.
5. Give it a label (e.g., `github-migration`).
6. Grant it the following permissions:
   - **Repositories:** `Read`
7. Copy the generated password instantly and paste it as `BITBUCKET_APP_PASSWORD` in your `.env`.

### 2. GitHub Personal Access Token (PAT)
1. Log in to GitHub.
2. Go to **Settings** -> **Developer settings** -> **Personal access tokens** -> **Tokens (classic)**.
3. Click **Generate new token (classic)**.
4. Give it a note (e.g., `bitbucket-migration`) and choose an expiration date.
5. Select the following scope:
   - `repo` (Full control of private repositories)
6. Depending on organization settings, make sure you authorize the token for the specific organization (Configure SSO).
7. Copy the generated token and paste it as `GITHUB_TOKEN` in your `.env`.

## Usage

Once `.env` is fully populated, simply run:

```bash
python migrate.py
```

### Safety & Idempotency
- **Skipping Execution**: Repositories that already exist inside the target GitHub Organization are automatically skipped, meaning you can safely cancel and rerun the script at any time without duplicating work.
- **Cleanup**: Repositories are cloned to a `./temp_clones` directory, which is automatically deleted step-by-step for each repository to save disk space.
- **Failures**: Any repository that genuinely fails to push (e.g., extremely large individual file limits in GitHub) will be tallied in the final Output Summary for manual review.
