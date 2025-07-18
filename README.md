# GitHub Branch Sync Automation Tool

## 🚀 Overview

This Python script automates the process of keeping branches in sync within a GitHub repository. It works by creating pull requests to merge changes from a `base` branch into one or more `destination` branches, based on a JSON configuration file.

This tool is designed to be run in a CI/CD environment (like GitHub Actions) or locally to streamline development workflows, especially for managing release branches.

## ✨ Features

- **Configuration-driven**: Define all your branch sync logic in a simple JSON file.
- **Automated PR Creation**: Automatically creates pull requests for branches that are out of sync.
- **Idempotent**: The script can be run multiple times without creating duplicate pull requests. It will update existing PRs by force-pushing the sync branch.
- **Conflict Detection**: If a merge conflict occurs, the script will log the error and skip that branch pair, requiring manual intervention. It will not leave the repository in a conflicted state.
- **Clean Syncs Only**: It first checks if a sync is necessary before performing any operations, saving time and resources.
- **Automatic Cleanup**: If a `base` branch is already fully merged into a `destination`, the script will automatically close any old, open sync PRs between them.
- **Dry Run Mode**: A `--dry-run` flag allows you to see what the script *would* do without making any actual changes to your repository.
- **Secure**: Uses a `.env` file to manage your GitHub Personal Access Token, keeping it out of your codebase.

## 📋 Requirements

- Python 3.9+
- Git installed on the system running the script.
- A GitHub Personal Access Token with `repo` scope.

## 🛠️ Setup

1.  **Install Dependencies**:
    Install the required Python packages from the project root.

    ```bash
    pip install -r requirements.txt
    ```

2.  **Create Environment File**:
    Create a file named `.env` in the project root. This file will securely store your GitHub token.

    **`.env`**
    ```
    # Generate a token here: https://github.com/settings/tokens
    # Make sure it has the "repo" scope.
    GITHUB_TOKEN="your_github_personal_access_token_here"
    ```
    > **Note**: The `.gitignore` file is already configured to ignore `.env` files, preventing your token from being accidentally committed.

3.  **Create Configuration File**:
    Create a file named `config.json` in the project root. This file defines which branches to sync.

    **`config.json`**
    ```json
    {
      "repo_url": "git@github.com:YourUsername/YourRepo.git",
      "conflict_branch_prefix": "sync/",
      "branches": [
        {
          "base": "release-1.0",
          "destinations": ["release-1.1", "release-1.2"]
        },
        {
          "base": "release-1.1",
          "destinations": ["release-1.2"]
        },
        {
          "base": "develop",
          "destinations": ["main"]
        }
      ]
    }
    ```

## ⚙️ Configuration Details

- `repo_url`: **(Required)** The SSH URL of the repository you want to sync. The script uses this to clone the repo.
- `conflict_branch_prefix`: **(Optional)** A prefix for the temporary branches created by the script. Defaults to `sync/`. For example, syncing `develop` into `main` would create a branch named `sync/develop-into-main`.
- `branches`: **(Required)** An array of objects, where each object defines a sync relationship.
    - `base`: The source branch containing the changes.
    - `destinations`: An array of target branches that the `base` branch should be merged into.

## 🏃‍♀️ Usage

Run the script from the project root directory.

### Standard Run
This will execute the sync process based on your `config.json`.

```bash
python scripts/auto_sync.py
```

### Dry Run
To see what actions the script would take without creating/updating PRs or pushing to the remote repository, use the `--dry-run` flag.

```bash
python scripts/auto_sync.py --dry-run
```

### Using a Custom Config File
You can specify a different path for your configuration file using the `--config` flag.

```bash
python scripts/auto_sync.py --config /path/to/my_special_config.json
```

## 🗓️ Automated Scheduling (Optional)

You can run this script automatically on a schedule using `cron` (on macOS and Linux). This is useful for keeping branches in sync without manual intervention.

### Cron Setup (macOS & Linux)

1.  **Open your user's crontab file for editing:**
    ```bash
    crontab -e
    ```

2.  **Add a new line to schedule the script.** The following example runs the script every hour.

    Make sure to replace `/path/to/your/project/root` with the **absolute path** to your project's root directory.

    ```cron
    # Run the branch sync script every hour
    0 * * * * cd /path/to/your/project/root && python3 scripts/auto_sync.py >> cron.log 2>&1
    ```

    **Breakdown of the cron job:**
    - `0 * * * *`: This is the schedule. It means "at minute 0 of every hour of every day".
    - `cd /path/to/your/project/root`: Navigates to the project's root directory. **This is crucial for the script to find its configuration files (`.env`, `config.json`).**
    - `python3 scripts/auto_sync.py`: Executes the script. You may need to use the full path to your Python executable (e.g., `/usr/bin/python3`).
    - `>> cron.log 2>&1`: This redirects all output (both standard output and errors) to a file named `cron.log` inside your project root. This allows you to review the script's execution history.

    > **Note on Python Virtual Environments:** If you installed dependencies in a virtual environment, you must use the Python interpreter from that environment.
    >
    > For example:
    > ```cron
    > # Using a python venv
    > 0 * * * * cd /path/to/your/project/root && /path/to/your/venv/bin/python scripts/auto_sync.py >> cron.log 2>&1
    > ```

## 🧠 How It Works

For each `base` -> `destination` pair defined in the configuration:
1.  **Setup**: The script clones the specified repository into a temporary directory. If it already exists, it fetches the latest changes from `origin`.
2.  **Check for Diffs**: It checks if there are any commits in `base` that are not yet in `destination`. If `destination` is up-to-date, it closes any existing sync PRs for that pair and moves on.
3.  **Merge**: If changes are found, it creates a new local branch (e.g., `sync/base-into-destination`) from the latest `destination` branch. It then attempts to merge `base` into this new branch.
4.  **Handle Conflicts**: If the merge fails due to a conflict, the script logs an error, aborts the merge, and moves to the next branch pair. Manual resolution is required.
5.  **Push and Create PR**: If the merge is successful, the script force-pushes the sync branch to `origin` and creates a pull request from the sync branch to the `destination` branch. If a PR for this pair already exists, the force-push automatically updates it.
6.  **Cleanup**: The script checks out the repository's default branch before processing the next pair to ensure a clean state.
