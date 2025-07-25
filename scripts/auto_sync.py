import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from git import GitCommandError, Repo
from github import Github, GithubException

# --- Basic configuration ---
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"



class BranchSyncer:
    """Manages syncing of branches by creating or updating pull requests."""

    def __init__(self, config: dict, github_token: str, work_dir: str, dry_run: bool = False, merge_prs: bool = False, auto_resolve_docs: bool = False):
        self.config = config
        self.repo_url = config["repo_url"]
        self.repo_name = self.repo_url.split("/")[-1].replace(".git", "")
        self.conflict_prefix = config.get("conflict_branch_prefix", "sync/")
        self.dry_run = dry_run
        self.merge_prs = merge_prs
        self.auto_resolve_docs = auto_resolve_docs
        self.work_dir = Path(work_dir)
        self.repo_path = self.work_dir / self.repo_name

        try:
            self.github = Github(github_token)
            gh_repo_path = self.repo_url.split(":")[-1].replace(".git", "")
            self.gh_repo = self.github.get_repo(gh_repo_path)
        except GithubException as e:
            logging.error(f"Failed to connect to GitHub. Check token and repo URL. Error: {e}")
            sys.exit(1)

        self.repo = self._setup_repo()

    def _setup_repo(self) -> Repo:
        """Clones the repository if it doesn't exist, or opens and fetches updates."""
        if self.repo_path.exists():
            logging.info(f"Repository already exists at {self.repo_path}. Fetching updates.")
            repo = Repo(self.repo_path)
            repo.remotes.origin.fetch()
        else:
            logging.info(f"Cloning {self.repo_url} into {self.repo_path}...")
            repo = Repo.clone_from(self.repo_url, self.repo_path)
        return repo

    def sync_all(self):
        """Iterates through the configuration and syncs all specified branch pairs."""
        logging.info("Starting branch sync process.")
        for branch_config in self.config["branches"]:
            base = branch_config["base"]
            for dest in branch_config["destinations"]:
                self._sync_pair(base, dest)
        logging.info("Branch sync process finished.")

    def _sync_pair(self, base_branch: str, dest_branch: str):
        """Performs a sync operation from a base branch to a destination branch."""
        logging.info(f"--- Syncing '{base_branch}' -> '{dest_branch}' ---")
        pr_title = f"[Automated Sync] Sync {base_branch} into {dest_branch}"

        try:
            # 1. Fetch latest changes from remote
            self.repo.remotes.origin.fetch()
            for branch in [base_branch, dest_branch]:
                if branch not in self.repo.remotes.origin.refs:
                    logging.warning(f"Branch '{branch}' not found in remote 'origin'. Skipping.")
                    return

            # 2. Check if base is already merged into dest
            self.repo.git.checkout(dest_branch)
            self.repo.git.pull('origin', dest_branch)

            commits_to_merge = self.repo.git.rev_list(f"origin/{base_branch}", f"^origin/{dest_branch}")
            if not commits_to_merge:
                logging.info(f"'{base_branch}' is already fully merged into '{dest_branch}'. No action needed.")
                self._close_existing_pr_if_needed(base_branch, dest_branch)
                return

            # 3. Perform a test merge to check for conflicts before creating a PR
            try:
                logging.info("Performing test-merge to check for conflicts...")
                # Attempt a merge without committing to check for conflicts
                self.repo.git.merge(f"origin/{base_branch}", "--no-commit", "--no-ff")
                # If it succeeds, abort the merge to leave the branch clean
                self.repo.git.merge("--abort")
                logging.info("No conflicts detected. Proceeding with PR creation.")
            except GitCommandError:
                conflicting_files_str = self.repo.git.diff('--name-only', '--diff-filter=U')
                conflicting_files = [f for f in conflicting_files_str.split('\n') if f]
                self.repo.git.merge("--abort")  # Abort test merge

                if self.auto_resolve_docs and conflicting_files and all(f.startswith('docs/api/v2/') for f in conflicting_files):
                    logging.info(
                        f"All conflicts are in 'docs/api/v2/'. Attempting to auto-resolve for "
                        f"'{base_branch}' -> '{dest_branch}'"
                    )
                    self._auto_resolve_docs_conflict(base_branch, dest_branch, conflicting_files)
                else:
                    logging.error(
                        f"CONFLICT: Merge conflict detected between '{base_branch}' and '{dest_branch}'. "
                        f"Conflicting files: [{', '.join(conflicting_files)}]. Skipping PR creation."
                    )
                return  # Skip this pair

            # 4. Create or update pull request directly
            self._create_or_update_pr(base_branch, dest_branch, pr_title)

        except GitCommandError as e:
            # This will catch git errors from checkout, pull etc.
            logging.error(f"A git command failed for '{base_branch}' -> '{dest_branch}': {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred for '{base_branch}' -> '{dest_branch}': {e}")
        finally:
            # Go back to a clean state for the next pair
            self.repo.git.checkout(self.gh_repo.default_branch)

    def _auto_resolve_docs_conflict(self, base_branch: str, dest_branch: str, conflicting_files: list):
        """
        Resolves a documentation merge conflict by creating a new branch, merging with
        'ours' strategy, and creating a PR.
        """
        if self.dry_run:
            logging.info(f"[DRY RUN] Would auto-resolve docs conflict for '{base_branch}' -> '{dest_branch}'.")
            return

        resolution_branch = f"{self.conflict_prefix}{base_branch.replace('/', '_')}-into-{dest_branch.replace('/', '_')}"
        pr_title = f"[Automated Sync with Fix] Sync {base_branch} into {dest_branch}"

        files_list_md = "\n".join(f"- `{f}`" for f in conflicting_files)
        pr_body = (
            "This is an automated pull request to sync changes from "
            f"`{base_branch}` into `{dest_branch}`.\n\n"
            "The following conflicts in `docs/api/v2/` were automatically resolved by "
            "taking the version from the destination branch (`--ours` strategy):\n"
            f"{files_list_md}"
        )

        try:
            # Get a clean start on the destination branch
            self.repo.git.checkout(dest_branch)
            self.repo.git.pull('origin', dest_branch)

            # Create/reset the resolution branch from the latest state of the destination
            self.repo.git.checkout('-B', resolution_branch)

            # Merge using 'ours' strategy. This accepts 'our' changes for all conflicted files.
            logging.info(f"Merging '{base_branch}' into '{resolution_branch}' with '-X ours' strategy.")
            self.repo.git.merge(f'origin/{base_branch}', '-Xours', '--no-ff', '--no-edit')

            # Push the new branch with the resolved merge
            logging.info(f"Pushing resolved branch '{resolution_branch}' to origin.")
            self.repo.remotes.origin.push(resolution_branch, force=True)

            # Create a PR from the resolution branch to the original destination
            self._create_or_update_pr(resolution_branch, dest_branch, pr_title, body=pr_body)

        except GitCommandError as e:
            logging.error(f"Failed to auto-resolve docs conflict for '{base_branch}' -> '{dest_branch}': {e}")
            # Clean up local state on failure
            self.repo.git.checkout(self.gh_repo.default_branch)
            try:
                self.repo.git.branch('-D', resolution_branch)
            except GitCommandError:
                pass  # Branch might not exist if checkout failed early

    def _create_or_update_pr(self, head_branch: str, base_branch: str, title: str, body: str = None):
        """Creates a new PR or logs if one already exists."""
        existing_pr = self._find_existing_pr(head_branch, base_branch)

        if existing_pr:
            logging.info(f"PR already exists for '{head_branch}' -> '{base_branch}'. URL: {existing_pr.html_url}")
            if self.merge_prs:
                self._merge_pr(existing_pr)
            return

        if self.dry_run:
            logging.info(f"[DRY RUN] Would create PR: '{title}' from '{head_branch}' -> '{base_branch}'.")
            return

        logging.info(f"Creating new pull request: '{title}'")

        if not body:
            body = (
                "This is an automated pull request to sync changes from "
                f"`{head_branch}` into `{base_branch}`."
            )

        try:
            pr = self.gh_repo.create_pull(title=title, body=body, head=head_branch, base=base_branch)
            logging.info(f"Successfully created PR: {pr.html_url}")
            if self.merge_prs:
                self._merge_pr(pr)
        except GithubException as e:
            if e.status == 422 and "A pull request already exists" in str(e.data):
                logging.warning("PR already exists, but was not found initially. This can happen in race conditions.")
            elif e.status == 422 and "No commits between" in str(e.data):
                logging.warning(f"GitHub reports no commits to merge between '{head_branch}' and '{base_branch}'. This can happen after a recent merge.")
            else:
                logging.error(f"Failed to create PR for '{head_branch}' -> '{base_branch}': {e}")

    def _merge_pr(self, pr):
        """Attempts to merge a given pull request."""
        if self.dry_run:
            logging.info(f"[DRY RUN] Would merge PR #{pr.number}: {pr.title}")
            return

        logging.info(f"Checking merge status for PR #{pr.number}...")
        pr.update()  # Refresh PR data to get the latest mergeable state

        if pr.state != 'open':
            logging.warning(f"PR #{pr.number} is not open, skipping merge.")
            return

        state = pr.mergeable_state
        if state == 'clean':
            logging.info(f"PR #{pr.number} is clean. Attempting to merge...")
            try:
                status = pr.merge()
                if status.merged:
                    logging.info(f"Successfully merged PR #{pr.number} with SHA: {status.sha}")
                else:
                    logging.warning(f"Merge command failed for PR #{pr.number}. Reason: {status.message}")
            except GithubException as e:
                if e.status == 405:  # Method Not Allowed
                    logging.error(
                        f"Failed to merge PR #{pr.number}. GitHub API denied the merge. Reason: {e.data.get('message', 'Not allowed')}")
                else:
                    logging.error(f"Failed to merge PR #{pr.number} due to an unknown API error: {e}")
        elif state == 'blocked':
            logging.warning(f"PR #{pr.number} is blocked from merging. It may require approvals or for checks to pass.")
        elif state == 'dirty':
            logging.error(f"PR #{pr.number} has merge conflicts and cannot be merged.")
        elif state == 'draft':
            logging.warning(f"PR #{pr.number} is a draft and cannot be merged.")
        elif state == 'unknown':
            logging.warning(f"Merge status for PR #{pr.number} is unknown. GitHub may still be checking. Skipping merge.")
        else:  # unstable, etc.
            logging.warning(f"PR #{pr.number} is not in a mergeable state ('{state}'). Skipping merge.")

    def _find_existing_pr(self, head_branch: str, base_branch: str):
        """Finds an open pull request for a given head and base branch."""
        prs = self.gh_repo.get_pulls(state='open', head=f"{self.gh_repo.owner.login}:{head_branch}", base=base_branch)
        return prs[0] if prs.totalCount > 0 else None

    def _close_existing_pr_if_needed(self, head_branch: str, base_branch: str):
        """If a sync PR exists but is no longer needed, close it."""
        existing_pr = self._find_existing_pr(head_branch, base_branch)
        if existing_pr:
            logging.info(f"Branches are in sync. Closing obsolete PR: {existing_pr.html_url}")
            if not self.dry_run:
                existing_pr.edit(state='closed')


def setup_logging(log_file: str = None):
    """Configures logging to console and optionally to a file."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear existing handlers to avoid duplicates
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler
    if log_file:
        try:
            # Ensure the directory for the log file exists
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file, mode='a')
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            # Use basicConfig for this error as our handlers might not be set up
            logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
            logging.error(f"Failed to set up log file at {log_file}: {e}")
            sys.exit(1)


def main():
    """Main function to parse arguments and run the synchronizer."""
    parser = argparse.ArgumentParser(description="Automate syncing of GitHub release branches via Pull Requests.")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the JSON configuration file. Defaults to 'config.json' in the current directory."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without pushing branches or creating/modifying pull requests."
    )
    parser.add_argument(
        "--merge-prs",
        action="store_true",
        help="Attempt to merge existing pull requests that are in a clean state."
    )
    parser.add_argument(
        "--auto-resolve-docs",
        action="store_true",
        help="Attempt to auto-resolve merge conflicts in 'docs/api/v2/' by using the destination branch's version."
    )
    parser.add_argument(
        "--log-file",
        default=".log",
        help="Path to a file where logs will be stored. Logs are appended to the file."
    )
    args = parser.parse_args()

    setup_logging(args.log_file)

    if args.dry_run:
        logging.info("--- Starting in DRY RUN mode. No changes will be pushed to GitHub. ---")

    # Load environment variables from .env file
    load_dotenv()
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        logging.error("GITHUB_TOKEN not found in environment variables. Please create a .env file or export it.")
        sys.exit(1)

    # Load configuration
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        logging.error(f"Configuration file not found at '{args.config}'")
        sys.exit(1)
    except json.JSONDecodeError:
        logging.error(f"Invalid JSON in configuration file '{args.config}'")
        sys.exit(1)

    # Define a persistent local directory for the repository clone to speed up subsequent runs.
    script_dir = Path(__file__).resolve().parent
    work_dir = Path(".tmp")
    work_dir.mkdir(exist_ok=True)
    logging.info(f"Using working directory: {work_dir}")

    syncer = BranchSyncer(config, github_token, str(work_dir), args.dry_run, args.merge_prs, args.auto_resolve_docs)
    syncer.sync_all()


if __name__ == "__main__":
    main()
