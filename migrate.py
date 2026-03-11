import os
import sys
import shutil
import logging
import subprocess
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus
import requests
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# Configuration
BITBUCKET_WORKSPACE = os.getenv("BITBUCKET_WORKSPACE")
BITBUCKET_USERNAME = os.getenv("BITBUCKET_USERNAME")
BITBUCKET_APP_PASSWORD = os.getenv("BITBUCKET_APP_PASSWORD")
GITHUB_ORG = os.getenv("GITHUB_ORG")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "5"))

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API endpoints
BB_API_BASE = "https://api.bitbucket.org/2.0"
GH_API_BASE = "https://api.github.com"

# Common GitHub headers (Bearer works for both classic and fine-grained PATs)
GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

# Transient HTTP status codes worth retrying
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Migrate repositories from a Bitbucket workspace to a GitHub organization."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List repositories that would be migrated without actually creating or pushing anything."
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-attempt migration for repos that exist on GitHub but are empty (orphaned from a previous failed push)."
    )
    return parser.parse_args()


def validate_config():
    """Validate that all required environment variables are set."""
    required = {
        "BITBUCKET_WORKSPACE": BITBUCKET_WORKSPACE,
        "BITBUCKET_USERNAME": BITBUCKET_USERNAME,
        "BITBUCKET_APP_PASSWORD": BITBUCKET_APP_PASSWORD,
        "GITHUB_ORG": GITHUB_ORG,
        "GITHUB_TOKEN": GITHUB_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}. Please check your .env file.")
        sys.exit(1)


def retry_request(func, *args, **kwargs):
    """Retry logic for HTTP requests handling rate limits, 5xx, and transient errors."""
    retries = 5
    for i in range(retries):
        try:
            response = func(*args, **kwargs)
            if response.status_code in RETRYABLE_STATUS_CODES:
                if response.status_code == 429:
                    wait = int(response.headers.get('Retry-After', 10))
                    logger.warning(f"Rate limited (429). Waiting {wait} seconds...")
                    time.sleep(wait)
                else:
                    wait = 2 ** i
                    logger.warning(f"Transient error ({response.status_code}). Retrying in {wait}s... (attempt {i+1}/{retries})")
                    time.sleep(wait)
                if i == retries - 1:
                    response.raise_for_status()
                continue
            response.raise_for_status()
            return response
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            wait = 2 ** i
            logger.warning(f"Network error: {e}. Retrying in {wait}s... (attempt {i+1}/{retries})")
            time.sleep(wait)
            if i == retries - 1:
                raise
        except requests.exceptions.HTTPError:
            raise  # Non-retryable HTTP errors (4xx except 429) bubble up immediately


def get_bitbucket_repos():
    """Fetches all repositories from the specified Bitbucket workspace."""
    logger.info(f"Fetching repositories from Bitbucket workspace: {BITBUCKET_WORKSPACE}")
    repos = []
    url = f"{BB_API_BASE}/repositories/{BITBUCKET_WORKSPACE}"
    auth = (BITBUCKET_USERNAME, BITBUCKET_APP_PASSWORD)
    
    while url:
        response = retry_request(requests.get, url, auth=auth)
        data = response.json()
        for repo in data.get("values", []):
            repos.append({
                "name": repo["name"],
                "slug": repo["slug"],
                "description": repo.get("description", ""),
                "is_private": repo.get("is_private", True)
            })
        url = data.get("next")
    
    logger.info(f"Found {len(repos)} repositories in Bitbucket.")
    return repos


def get_github_repos(include_empty=False):
    """Fetches all existing repositories from the target GitHub organization.
    
    Args:
        include_empty: If True, also returns a set of repos that are empty (0 size),
                       useful for --retry-failed mode to detect orphaned repos.
    
    Returns:
        A tuple of (all_repos: set, empty_repos: set).
    """
    logger.info(f"Fetching existing repositories from GitHub org: {GITHUB_ORG}")
    all_repos = set()
    empty_repos = set()
    url = f"{GH_API_BASE}/orgs/{GITHUB_ORG}/repos"
    params = {"per_page": 100}
    
    while url:
        response = retry_request(requests.get, url, headers=GH_HEADERS, params=params)
        data = response.json()
        for repo in data:
            repo_name = repo["name"].lower()
            all_repos.add(repo_name)
            # A repo with size 0 and no default_branch is likely an orphan from a failed push
            if include_empty and repo.get("size", 1) == 0:
                empty_repos.add(repo_name)
        
        # Check for pagination in Headers
        url = None
        if "link" in response.headers:
            links = response.headers["link"].split(", ")
            for link in links:
                if 'rel="next"' in link:
                    url = link[link.find("<")+1:link.find(">")]
                    params = None  # Parameters are included in the next URL directly
                    break
                    
    logger.info(f"Found {len(all_repos)} repositories already in GitHub ({len(empty_repos)} empty/orphaned).")
    return all_repos, empty_repos


def create_github_repo(repo_data):
    """Creates an empty repository on GitHub with matching metadata."""
    url = f"{GH_API_BASE}/orgs/{GITHUB_ORG}/repos"
    payload = {
        "name": repo_data["slug"],
        "description": repo_data["description"] or "",
        "private": repo_data["is_private"],
        "has_issues": True,
        "has_projects": False,  # Classic Projects is being phased out by GitHub
        "has_wiki": True
    }
    
    response = retry_request(requests.post, url, headers=GH_HEADERS, json=payload)
    return response.json()["clone_url"]


def delete_github_repo(repo_name):
    """Deletes a repository from GitHub. Used to clean up orphaned repos on push failure."""
    url = f"{GH_API_BASE}/repos/{GITHUB_ORG}/{repo_name}"
    try:
        retry_request(requests.delete, url, headers=GH_HEADERS)
        logger.info(f"[{repo_name}] Cleaned up orphaned GitHub repo after push failure.")
    except Exception as e:
        logger.warning(f"[{repo_name}] Failed to delete orphaned repo: {e}. Manual cleanup required.")


def run_git_command(command, cwd=None):
    """Runs a shell command and captures output securely."""
    try:
        process = subprocess.run(command, cwd=cwd, shell=False, check=True, capture_output=True, text=True)
        return True, process.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def migrate_repo(repo, github_repos, empty_repos, retry_failed=False):
    """Worker function to process a single repository migration.
    
    Args:
        repo: Bitbucket repository metadata dict.
        github_repos: Set of repo names already on GitHub (lowercase).
        empty_repos: Set of repo names on GitHub that are empty (orphaned).
        retry_failed: If True, re-attempt push for orphaned (empty) repos.
    """
    repo_name = repo["slug"]
    repo_name_lower = repo_name.lower()
    
    # Check if repo already exists on GitHub
    if repo_name_lower in github_repos:
        # If --retry-failed is active, check if this is an orphaned empty repo
        if retry_failed and repo_name_lower in empty_repos:
            logger.info(f"[{repo_name}] Found empty/orphaned repo on GitHub. Retrying push...")
        else:
            logger.info(f"[{repo_name}] SKIPPED: Already exists on GitHub.")
            return "skipped", repo_name
        
    logger.info(f"[{repo_name}] Starting migration...")
    clone_dir = f"./temp_clones/{repo_name}"
    created_new_repo = False
    
    try:
        # 1. Create GH Repo (skip if retrying a previously orphaned repo)
        if repo_name_lower not in github_repos:
            logger.info(f"[{repo_name}] Creating repository on GitHub...")
            gh_clone_url = create_github_repo(repo)
            created_new_repo = True
        else:
            # Repo exists (orphaned retry case) — construct the clone URL manually
            gh_clone_url = f"https://github.com/{GITHUB_ORG}/{repo_name}.git"
        
        # Construct auth URLs securely (these won't be printed to logs)
        bb_auth_url = f"https://{quote_plus(BITBUCKET_USERNAME)}:{quote_plus(BITBUCKET_APP_PASSWORD)}@bitbucket.org/{BITBUCKET_WORKSPACE}/{repo_name}.git"
        gh_auth_url = gh_clone_url.replace("https://", f"https://{quote_plus(GITHUB_TOKEN)}@")
        
        # 2. Clone from BB (--mirror preserves ALL commit history, branches, tags)
        logger.info(f"[{repo_name}] Cloning from Bitbucket...")
        success, out = run_git_command(["git", "clone", "--mirror", bb_auth_url, clone_dir])
        if not success:
            logger.error(f"[{repo_name}] Clone failed: {out}")
            # Clean up orphaned GitHub repo if we just created it
            if created_new_repo:
                delete_github_repo(repo_name)
            return "failed", repo_name
            
        # 3. Push to GH (--mirror)
        logger.info(f"[{repo_name}] Pushing to GitHub...")
        success, out = run_git_command(["git", "push", "--mirror", gh_auth_url], cwd=clone_dir)
        if not success:
            logger.error(f"[{repo_name}] Push failed: {out}")
            # Clean up orphaned GitHub repo if we just created it
            if created_new_repo:
                delete_github_repo(repo_name)
            return "failed", repo_name
            
        logger.info(f"[{repo_name}] SUCCESS: Migration complete.")
        return "success", repo_name
        
    except Exception as e:
        logger.error(f"[{repo_name}] FAILED with exception: {str(e)}")
        # Clean up orphaned GitHub repo if we just created it
        if created_new_repo:
            delete_github_repo(repo_name)
        return "failed", repo_name
        
    finally:
        # 4. Clean up the local clone to save disk space
        if os.path.exists(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)


def main():
    args = parse_args()
    validate_config()
    
    # Ensure clone temp directory exists
    os.makedirs("./temp_clones", exist_ok=True)
    
    try:
        bb_repos = get_bitbucket_repos()
        gh_repos, empty_repos = get_github_repos(include_empty=args.retry_failed)
        
        # --- DRY RUN MODE ---
        if args.dry_run:
            to_migrate = []
            to_skip = []
            to_retry = []
            for repo in bb_repos:
                name = repo["slug"].lower()
                if name in gh_repos:
                    if args.retry_failed and name in empty_repos:
                        to_retry.append(repo["slug"])
                    else:
                        to_skip.append(repo["slug"])
                else:
                    to_migrate.append(repo["slug"])
            
            print("\n" + "="*60)
            print("DRY RUN — No changes will be made")
            print("="*60)
            print(f"Total Bitbucket Repositories:  {len(bb_repos)}")
            print(f"Would Migrate (new):           {len(to_migrate)}")
            print(f"Would Skip (already exist):    {len(to_skip)}")
            if args.retry_failed:
                print(f"Would Retry (empty/orphaned):  {len(to_retry)}")
            print("="*60)
            
            if to_migrate:
                print("\nRepositories to migrate:")
                for r in to_migrate:
                    print(f"  + {r}")
            if to_retry:
                print("\nOrphaned repos to retry:")
                for r in to_retry:
                    print(f"  ~ {r}")
            if to_skip:
                print("\nRepositories to skip:")
                for r in to_skip:
                    print(f"  - {r}")
            return
        
        # --- LIVE MIGRATION ---
        results = {"success": [], "skipped": [], "failed": []}
        
        logger.info(f"Starting migration with {MAX_WORKERS} concurrent workers...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_repo = {
                executor.submit(migrate_repo, repo, gh_repos, empty_repos, args.retry_failed): repo
                for repo in bb_repos
            }
            
            for future in as_completed(future_to_repo):
                status, repo_name = future.result()
                results[status].append(repo_name)
                
        # Final Summary Output
        print("\n" + "="*60)
        print("MIGRATION SUMMARY")
        print("="*60)
        print(f"Total Repositories Found:    {len(bb_repos)}")
        print(f"Successfully Migrated:       {len(results['success'])}")
        print(f"Skipped (Already Exist):     {len(results['skipped'])}")
        print(f"Failed to Migrate:           {len(results['failed'])}")
        print("="*60)
        
        if results["success"]:
            print("\nSuccessfully Migrated:")
            for repo in results["success"]:
                print(f"  ✓ {repo}")
        
        if results["failed"]:
            print("\nFailed Repositories (check logs for details):")
            for repo in results["failed"]:
                print(f"  ✗ {repo}")
                
    except Exception as e:
        logger.error(f"Migration script aborted due to error: {e}")
    finally:
        # Final safety cleanup
        if os.path.exists("./temp_clones"):
            shutil.rmtree("./temp_clones", ignore_errors=True)


if __name__ == "__main__":
    main()
