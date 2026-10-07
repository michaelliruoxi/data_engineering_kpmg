# GitHub tutorial: work on your branch, submit a PR to main

**Team rule: commit and push only to your own branch. Submit pull requests to `main`. Never commit or push directly to `main` or another teammate's branch.**

The workflow is: **create your branch → edit → commit → push → open a pull request → review → merge**.

A **commit** records your changes. A **push** uploads local commits to GitHub. A **pull request (PR)** asks the team to review and merge your branch into `main`; opening one does not change `main` immediately.

## 1. Get access and create your branch on GitHub

1. Sign in to GitHub and accept the repository owner's collaborator invitation. You need write access.
2. Open [data_engineering_kpmg](https://github.com/michaelliruoxi/data_engineering_kpmg) and select **Code**.
3. Open the branch selector near the file list, then choose **View all branches → New branch**.
4. Enter a branch name using `your-name/short-task`, such as `alex/document-filing-columns`.
5. Set **Source** to **main**, then click **Create new branch**.
6. Return to **Code** and select your new branch. Confirm the selector shows your branch name before editing.

Use a new branch for each task. **Replace `alex/document-filing-columns` in every command below with your own branch name.** Branch names should have no spaces.

See GitHub's [branch creation guide](https://docs.github.com/en/pull-requests/how-tos/commit-changes/managing-branches-within-your-repository) if your screen differs.

## 2. Choose how to work

### Option A: small edits in your browser

This is the easiest route for a small documentation change; no installation is needed.

1. Confirm GitHub's branch selector shows your personal branch.
2. Open a file and click the pencil icon to edit it. To add a file, use **Add file → Create new file** or **Upload files**.
3. Make your change and use **Preview** when available.
4. Click **Commit changes...** and enter a message describing your work.
5. Check that the destination is **your personal branch**, then confirm the commit. If it says `main`, cancel, copy your edits somewhere safe, and reapply them on your branch.

Browser commits are already saved on GitHub, so **there is no separate push step**. Confirm the change appears on your branch, then skip to [step 5: open a pull request](#5-open-a-pull-request-to-main).

### Option B: work on your computer

Use this route for code, notebooks, or several related files.

**Install and clone once**

Install [Git](https://git-scm.com/install/), then open a new PowerShell, Terminal, or VS Code terminal. Check installation with `git --version`.

Open the terminal in the folder where you keep projects and run:

```text
git clone https://github.com/michaelliruoxi/data_engineering_kpmg.git
cd data_engineering_kpmg
```

If you already have a clone, open that folder instead of cloning again. Do not use **Download ZIP**: it does not include the Git history needed for this workflow.

Complete the browser sign-in if prompted. Your normal GitHub password is not a Git HTTPS password; see the [authentication guide](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git) if needed.

Set your commit identity once inside the repository, replacing both placeholders:

```text
git config --local user.name "Your Name"
git config --local user.email "YOUR_GITHUB_COMMIT_EMAIL"
```

Use your GitHub account email or your exact private `noreply` address from **GitHub Settings → Emails**. These settings identify your commits; they do not sign you in.

**Switch to your branch**

Selecting a branch on GitHub does not switch the branch on your computer. First run:

```text
git status
git remote -v
```

Confirm `origin` points to `michaelliruoxi/data_engineering_kpmg`. Before switching, the working tree should be clean. If you have unfinished changes, preserve them and get help moving them; do not discard them.

Fetch the branch you created online and switch to it:

```text
git fetch origin
git switch --track origin/alex/document-filing-columns
```

If that branch already exists locally, use `git switch alex/document-filing-columns` instead of the second command.

Now check:

```text
git branch --show-current
```

**Continue only if it prints your personal branch name.** If it prints `main`, another person's branch, or nothing, stop and correct the branch selection.

Before editing, download any existing changes from your own GitHub branch:

```text
git pull --ff-only origin alex/document-filing-columns
```

Run commands one at a time. If a command fails, stop and check the troubleshooting table below.

## 3. Edit, review, and commit your work

Open the repository in your editor, make your changes, and save the files. Preview documentation or run the relevant checks in [developer usage](developer-usage.md).

Review what changed:

```text
git status --short
git diff
```

`M` means modified, `??` means a new file, and `D` means deleted. `git diff` shows unstaged edits to tracked files; review new files in your editor. Press `q` to exit a long diff.

Stage only the files you want in the commit. For example:

```text
git add -- docs/filing-notes.md
```

**Replace this example path with your actual file.** List additional paths if needed. Avoid `git add .`, which may include unrelated work.

Check exactly what will be committed:

```text
git diff --staged
git status
git branch --show-current
```

Confirm the branch is yours and the staged changes belong to your task. Keep passwords, tokens, `.env`, `.env.aws`, backups, and private materials out of commits. Preserve the approved SEC source files and manifest unless your task specifically requires changing them.

If you staged the wrong file, `git restore --staged -- docs/filing-notes.md` removes it from the next commit while keeping your edits; substitute the actual path.

Then commit with a clear message:

```text
git commit -m "Document filing catalog columns"
git status
```

Replace the message with a description of your change. **The commit is still only on your computer.**

## 4. Push only to your branch

Check your branch again:

```text
git branch --show-current
```

When it shows your personal branch, push it explicitly:

```text
git push -u origin alex/document-filing-columns
```

This uploads your branch to GitHub; `-u` connects it to the matching remote branch. You can use this same command for later pushes.

**Never replace the branch name with `main`. Do not force-push or use `--all`.** On GitHub, select your branch and confirm your latest commit and files appear.

## 5. Open a pull request to main

1. On GitHub, open **Pull requests → New pull request**.
2. Choose the branches carefully:

   | Field | Choose |
   | --- | --- |
   | **base** — destination | **`main`** |
   | **compare** — your changes | **Your personal branch** |

3. Review the changed files. Only your intended task should be included.
4. Click **Create pull request**.
5. Write a clear title and a short description covering **what changed, why, and how you checked it**. State any checks you could not run.
6. Submit the PR and request a teammate or maintainer under **Reviewers**, if available. Use a draft PR if the work is unfinished.

**Verify the submitted PR proposes your branch → `main`.** Do not target a teammate's branch or reverse the direction. See GitHub's [PR guide](https://docs.github.com/en/pull-requests/how-tos/create-pull-requests/creating-a-pull-request).

A documentation PR might use the title **Clarify manual sample queries** and explain which query or field was confusing and how the change fixes it. In the checks, report what you actually did, such as reviewing Markdown links or running the six read-only queries with your assigned reader login. Do not claim database queries passed if you only reviewed the SQL text; state any connection or test limitation.

## 6. Make review fixes and finish

Make requested fixes on **the same personal branch**, then commit and push again. The existing PR updates automatically; you do not need a new PR for each correction.

Wait for review and required checks. The designated maintainer merges through the PR. **Never push to `main` to finish the task.** The PR's **Merged** label confirms completion.

For your next task, create a new branch from GitHub's latest `main` and repeat the workflow. Do not keep working on a branch whose PR was already merged.

## Keeping your branch up to date

Before continuing an existing task, use `git status` to confirm your work is committed and the working tree is clean. Switch to your personal branch, verify it with `git branch --show-current`, then run:

```text
git pull --ff-only origin alex/document-filing-columns
```

This also brings browser edits into your local copy. If Git cannot fast-forward, get help combining the two versions; do not force-push.

If you need teammates' newly merged work, stay on your personal branch and run:

```text
git fetch origin
git merge --no-edit origin/main
```

This brings `main` into **your branch**, not your work into `main`. Check the combined result and push your personal branch. If conflicts appear, resolve them with the relevant teammate before committing or pushing.

## Common problems

| Problem | What to do |
| --- | --- |
| Repository not found or permission denied | Check your GitHub account, collaborator invitation, and repository URL. |
| `not a git repository` | Open the terminal inside the cloned project folder. |
| Branch not found | Check its spelling on GitHub and run `git fetch origin`. |
| Local changes would be overwritten | Stop and preserve your files. Ask for help moving unfinished work safely. |
| Nothing to commit | Check that your files are saved and staged in the correct clone. |
| Push rejected | Pull your own branch with `--ff-only` as above. If that fails, ask for help; do not force-push. |
| Nothing to compare in a PR | Check base = `main`, compare = your branch, and that you committed and pushed your changes. |
| Accidentally edited or committed on local `main` | Do not push. Preserve the work on a new personal branch with `git switch -c your-name/rescue-work`, then get help checking it before submission. Your local `main` may still need repair. |
| Accidentally pushed to `main` or exposed a secret | Stop and tell the maintainer. Do not rewrite shared history yourself. Exposed credentials must be revoked or rotated. |

Before submitting, remember: **your branch, only your intended files, no secrets, PR destination = `main`.**

**For the owner:** this guide does not enable enforcement. Configure [protection for `main`](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) to require reviewed PRs and prevent direct-push bypasses, force pushes, and deletion where supported.
