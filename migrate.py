import os
import sys
import shutil
import logging
import subprocess
import time
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

# Validate config
if not all([BITBUCKET_WORKSPACE, BITBUCKET_USERNAME, BITBUCKET_APP_PASSWORD, GITHUB_ORG, GITHUB_TOKEN]):
    logger.error("Missing required environment variables. Please check your .env file.")
    sys.exit(1)

# API endpoints
BB_API_BASE = "https://api.bitbucket.org/2.0"
GH_API_BASE = "https://api.github.com"


def retry_request(func, *args, **kwargs):
    """Simple retry logic for HTTP requests handling rate limits."""
    retries = 3
    for i in range(retries):
        try:
            response = func(*args, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429: # Too many requests
                wait = int(e.response.headers.get('Retry-After', 5))
                logger.warning(f"Rate limited. Waiting {wait} seconds...")
                time.sleep(wait)
            elif i == retries - 1:
                raise
            else:
                time.sleep(2 ** i)
        except requests.exceptions.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2 ** i)


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


def get_github_repos():
    """Fetches all existing repositories from the target GitHub organization to handle skipping."""
    logger.info(f"Fetching existing repositories from GitHub org: {GITHUB_ORG}")
    repos = set()
    url = f"{GH_API_BASE}/orgs/{GITHUB_ORG}/repos"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    params = {"per_page": 100}
    
    while url:
        response = retry_request(requests.get, url, headers=headers, params=params)
        data = response.json()
        for repo in data:
            repos.add(repo["name"].lower())
        
        # Check for pagination in Headers
        url = None
        if "link" in response.headers:
            links = response.headers["link"].split(", ")
            for link in links:
                if 'rel="next"' in link:
                    url = link[link.find("<")+1:link.find(">")]
                    params = None # Parameters are included in the next URL directly
                    break
                    
    logger.info(f"Found {len(repos)} repositories already in GitHub.")
    return repos


def create_github_repo(repo_data):
    """Creates an empty repository on GitHub with matching metadata."""
    url = f"{GH_API_BASE}/orgs/{GITHUB_ORG}/repos"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    payload = {
        "name": repo_data["slug"], # Keep slug consistency
        "description": repo_data["description"] or "",
        "private": repo_data["is_private"],
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True
    }
    
    response = retry_request(requests.post, url, headers=headers, json=payload)
    return response.json()["clone_url"]


def run_git_command(command, cwd=None):
    """Runs a shell command and captures output securely."""
    try:
        process = subprocess.run(command, cwd=cwd, shell=False, check=True, capture_output=True, text=True)
        return True, process.stdout
    except subprocess.CalledProcessError as e:
        return False, e.stderr


def migrate_repo(repo, github_repos):
    """Worker function to process a single repository migration."""
    repo_name = repo["slug"]
    
    # Check if repo already exists on Target GitHub
    if repo_name.lower() in github_repos:
        logger.info(f"[{repo_name}] SKIPPED: Already exists on GitHub.")
        return "skipped", repo_name
        
    logger.info(f"[{repo_name}] Starting migration...")
    clone_dir = f"./temp_clones/{repo_name}"
    
    try:
        # 1. Create GH Repo
        logger.info(f"[{repo_name}] Creating repository on GitHub...")
        gh_clone_url = create_github_repo(repo)
        
        # Construct auth URLs securely (these won't be printed)
        bb_auth_url = f"https://{quote_plus(BITBUCKET_USERNAME)}:{quote_plus(BITBUCKET_APP_PASSWORD)}@bitbucket.org/{BITBUCKET_WORKSPACE}/{repo_name}.git"
        gh_auth_url = gh_clone_url.replace("https://", f"https://{quote_plus(GITHUB_TOKEN)}@")
        
        # 2. Clone from BB (--mirror preserves ALL commit history, branches, tags)
        logger.info(f"[{repo_name}] Cloning from Bitbucket...")
        success, out = run_git_command(["git", "clone", "--mirror", bb_auth_url, clone_dir])
        if not success:
            logger.error(f"[{repo_name}] Clone failed: {out}")
            return "failed", repo_name
            
        # 3. Push to GH (--mirror)
        logger.info(f"[{repo_name}] Pushing to GitHub...")
        success, out = run_git_command(["git", "push", "--mirror", gh_auth_url], cwd=clone_dir)
        if not success:
            logger.error(f"[{repo_name}] Push failed: {out}")
            return "failed", repo_name
            
        logger.info(f"[{repo_name}] SUCCESS: Migration complete.")
        return "success", repo_name
        
    except Exception as e:
        logger.error(f"[{repo_name}] FAILED with exception: {str(e)}")
        return "failed", repo_name
        
    finally:
        # 4. Clean up the disk to save space
        if os.path.exists(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)


def main():
    # Ensure clone temp directory exists
    os.makedirs("./temp_clones", exist_ok=True)
    
    try:
        bb_repos = get_bitbucket_repos()
        gh_repos = get_github_repos()
        
        results = {"success": [], "skipped": [], "failed": []}
        
        logger.info(f"Starting execution with {MAX_WORKERS} concurrent workers...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all migration tasks to the thread pool
            future_to_repo = {executor.submit(migrate_repo, repo, gh_repos): repo for repo in bb_repos}
            
            # Process results as tasks complete
            for future in as_completed(future_to_repo):
                status, repo_name = future.result()
                results[status].append(repo_name)
                
        # Final Summary Output
        print("\n" + "="*50)
        print("MIGRATION SUMMARY")
        print("="*50)
        print(f"Total Repositories Found: {len(bb_repos)}")
        print(f"Successfully Migrated:    {len(results['success'])}")
        print(f"Skipped (Already Exist):  {len(results['skipped'])}")
        print(f"Failed to Migrate:        {len(results['failed'])}")
        print("="*50)
        
        if results["failed"]:
            print("\nFailed Repositories (check logs for details):")
            for repo in results["failed"]:
                print(f" - {repo}")
                
    except Exception as e:
        logger.error(f"Migration script aborted due to error: {e}")
    finally:
        # Final safety cleanup
        if os.path.exists("./temp_clones"):
            shutil.rmtree("./temp_clones", ignore_errors=True)

if __name__ == "__main__":
    main()
