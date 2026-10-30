# Night Hacks

Our Night Hacks hackathon project.

> **Status:** Tech stack and project structure are still being decided. No application code yet.

## Getting started

```bash
git clone https://github.com/nickzjl/night-hacks.git
cd night-hacks
git config pull.rebase true
```

## Workflow

We both commit straight to `main`. No PRs or reviews needed.

1. `git pull` before you start working.
2. Commit small and often.
3. `git pull` again, then `git push`.

`pull.rebase` is set to `true`, so pulling replays your commits on top of your teammate's and avoids merge commits. If a rebase hits a conflict, fix the files, `git add` them, and run `git rebase --continue`.

## Environment variables

Keep secrets in `.env`, which Git ignores. Commit a `.env.example` with the variable names but no real values.
