# Use pl with GitHub

This guide is for the common case: your code is on GitHub and you want the cards there too. pl talks to GitHub only through the `gh` command-line tool and its login. pl stores no GitHub token.

You can keep cards in a **GitHub Project** (a board) or as plain **Issues** in one repo. Pull requests are read the same way in both cases.

## 1. Prerequisites

- `gh` installed (https://cli.github.com) and signed in with `gh auth login`.
- A Project needs the `project` scope. The default login does not include it:

  ```
  gh auth refresh -s project
  ```

- Issues, labels and pull requests need the `repo` scope. `gh auth login` grants it by default.
- `tmux`, and at least one harness CLI (`claude`, `codex` or `agy`) installed and signed in. pl runs it on your own subscription and needs no API key.

Check with `gh auth status`. It lists the scopes your token has.

## 2. Create a profile

A profile is one folder, `~/.pl-<name>`, with a `config.toml`. The usual way to make one is `pl setup`, which works with a new or an existing Project and writes everything below, with stage prompts that run pl's built-in skills: see [INSTALL.md](../INSTALL.md). The rest of this section is the older `pl profiles new` path.

This command creates a new GitHub Project and a profile that uses it:

```
pl profiles new work --github-project create --owner your-org
```

`--owner` is the user or organisation that owns the project. `--title` sets the project title (default `pl work`). pl then:

1. runs `gh project create`,
2. adds a single-select field named `pl stage` with one option per pl column,
3. writes `~/.pl-work/config.toml` (mode 0600) and prints a shell alias.

pl never edits the project's built-in Status field. If adding the field fails, nothing is saved and the error names the new project's URL, so you can delete it or add the field by hand.

To use a project you already have, run `pl setup --project-number N` ([INSTALL.md](../INSTALL.md), step 6), or run `pl profiles new work` and edit the `[tracker]` table by hand.

If `[tracker]` has `type = "github-project"` but no `number`, the console's Settings tab shows a **Create project** button under Connections. It does the same as the flag and saves the number into the profile.

The tracker table pl writes, with every key the Project tracker reads:

```toml
[tracker]
type = "github-project"
owner = "your-org"          # user or organisation that owns the project
number = 7                  # the project number from its URL
status_field = "pl stage"   # the single-select field that holds the stage (default "Status")
repo = "your-org/your-repo" # where new cards are opened as issues; you must add this key
# columns = [...]           # optional: the column names pl checks for (default: pl's eight columns)
```

`pl setup` writes `repo`; `pl profiles new` writes it only with `--repo`. Without it pl can read and move cards but cannot create one.

Then add at least one harness account. `pl profiles new` writes stage prompts that run pl's built-in skills (see "Built-in stages" in the README); to use your own, give each stage a prompt. `{id}` is replaced by the card id.

```toml
[accounts.main]
config_dir = "~/.claude"      # the harness's own config folder
harness = "claude"            # "claude" (default), "codex" or "agy"

[stages.spec]
prompt = "Write the spec for card {id}."
[stages.plan]
prompt = "Write the plan for card {id}."
[stages.run]
prompt = "Build card {id} and open a pull request."
```

You can also edit all of this in the console's Settings tab. Press `t` there to test the connection.

## 3. How cards map to GitHub

**Columns are options of the stage field.** pl's columns are Inbox, Spec ready, Plan for review, Manual, Approved, In progress, PR open and Done. Each must exist as an option of `status_field`. A missing one gives:

```
pl: add a 'Inbox' option to the pl stage field
```

`pl setup` adds missing options to an existing field in one update that keeps every existing option and its id, after you agree (`--no-add-missing-stages` to decline). Otherwise add them in GitHub. `pl board init` only checks that Inbox exists.

**Views.** `pl setup` also adds a "Pipeline" board and a "Needs you" table filtered to `"pl stage":"Spec ready","Plan for review"` (`--no-views` to skip). With `--sprint` it adds a "Sprint" iteration field (2 weeks, `--sprint-weeks N`) and a "This sprint" board filtered to `sprint:@current`. Views and fields that already have those names are left alone, so re-running setup changes nothing. GitHub's API cannot set a board's "Column by", so set it to "pl stage" on the Pipeline view yourself.

**Each card is an issue on the project.** Draft items and pull requests on the project are skipped. A card id looks like `your-org/your-repo#42`.

**Spec and plan text live in the issue body.** The body is split into sections, each opened by a line like `# PIPELINE: SPEC`. See the card contract in the README.

**Metadata lives on the body's last line**, as a hidden comment:

```
<!-- pl:meta {"pipeline_mode":"auto","profile":"main"} -->
```

pl rewrites that line on each update and merges keys on its side. Anyone who can edit the issue can edit this line, and so can change which account or plan path pl uses. Only give edit rights to people you trust with the pipeline. pl only reads plan files inside its own plans folder, whatever the metadata says.

Labels on the issue become the card's tags. The first assignee is the card's assignee.

## 4. The Issues tracker instead

Without a Project, cards are the open issues of one repo, and the stage is a label:

```toml
[tracker]
type = "github-issues"
repo = "your-org/your-repo"
label_prefix = "pl:"        # default; the stage label is "pl:Inbox", "pl:Spec ready", ...
```

pl creates a stage label when it first needs it. Moving a card swaps its stage label. Deleting a card closes the issue. Only open issues are read.

Which to pick:

| | Project | Issues |
| --- | --- | --- |
| Board view in GitHub | yes | no, filter by label |
| Setup | `--github-project create`, add `repo` | one table, nothing to create |
| Cards from several repos | yes, all issues on the project | no, one repo |
| Needs the `project` scope | yes | no |

## 5. Pull requests

pl does not open pull requests. The run-stage agent does, from your prompt. pl reads open pull requests assigned to you with `gh search prs` and shows them on the Pull requests tab. It sorts them by labels that your review tools set:

```toml
[code_host]
owner = "your-org"            # limits the search to this owner; unset searches every repo you can see
labels = { review = "pl:auto-review", ready = "pl:ready-for-review", merge_ready = "pl:merge-ready", rework = "pl:needs-rework", failed = "pl:review-failed" }
```

`pl setup` writes these defaults when a GitHub repo is known, and asks before creating the ones the repo lacks. It never edits or deletes a label that exists. The labels it creates:

| Key | Default | Color | Description |
| --- | --- | --- | --- |
| `review` | `pl:auto-review` | `1D76DB` | PR opened by a pl agent, waiting for auto review |
| `ready` | `pl:ready-for-review` | `0E8A16` | Reviewed by pl, ready for a person |
| `merge_ready` | `pl:merge-ready` | `5319E7` | Passed every check, ready to merge |
| `rework` | `pl:needs-rework` | `FBCA04` | Needs more work before review |
| `failed` | `pl:review-failed` | `B60205` | Auto review gave up; needs a person |

Flags: `--label-<key> NAME` changes one (`--label-merge-ready` for `merge_ready`), `--no-create-labels` writes them without creating any, `--no-labels` writes none.

| Label key | What pl does |
| --- | --- |
| `ready` | Required. pl tracks only open PRs with this label. Unset means pl does not track PRs. |
| `failed` | Listed as "need your decision". |
| `rework` | Listed as "need rework". |
| `merge_ready` | Listed as "to merge". |
| `review` | Named in the hint to add it back after you decide on a failed review. |

A `ready` PR with neither `rework` nor `merge_ready` shows as awaiting a merge check. Results are cached for 60 seconds. The Dashboard also counts PRs opened and merged in the last 14 days.

`pl intent <PR URL>` prints the spec and plan scope of the card behind a PR, for reviewers. It finds the card through the card's `pr_urls` metadata.

## 5a. Defaults for a GitHub Project profile

A profile whose `[tracker]` is `github-project` with a `repo` gets two things without any config.

**Issue intake.** Each dispatcher pass lists up to 100 open issues of `repo` that are not on the project yet, newest update first, in one `gh issue list` call. An issue joins the funnel when it is assigned to your `[user] login` or carries the `pl:start` label. pl adds that issue to the project in Inbox with `pipeline_mode` auto. It never opens a second issue.

- Issues already assigned to you on the first pass are only remembered, like the board intake. Changing `[user] login` starts a new first pass.
- `pl:start` joins an issue even when it is not assigned to you, and even when it had the label on the first pass. pl removes the label when it adopts the issue.
- Labels in `[intake] skip_tags` win over `pl:start`: a skipped issue never joins.
- `pl setup` creates the `pl:start` label.

**Auto-review loop.** When `[code_host] labels` has `review` and `ready`, the dispatcher keeps an `auto-review` loop running on the profile's first account. If that account is parked at its usage limit, the loop runs on another account of the same harness until its own is back (README: Parked accounts). Its prompt names the repo and your labels. The agent reviews open PRs labelled `review` and always removes `review`. A clean review adds `ready`. Findings go in one PR comment and add both `ready` and `rework`, so pl lists the PR as needing rework. A review that cannot run adds `failed`; you decide, then add `review` back. It never merges.

Turn either off, or replace it:

```toml
[intake]
enabled = false            # or set type = "mcp" etc.: an explicit intake wins

[loops.auto-review]
enabled = false            # or give your own prompt = "..."
```

## 6. First run

```
# pl setup as in INSTALL.md (it writes the [stages.*] prompts)
pl --profile work list                 # the board by column; checks gh and the stage field
pl --profile work                      # open the console
```

In the console:

1. Press `2` for Ideas and `n` for a new idea. The harness asks questions until the brief is clear. `A` approves it and creates a card in Inbox.
2. Start the dispatcher in another terminal. Try a dry run first:

   ```
   pl --profile work dispatch --dry-run --once
   pl --profile work dispatch
   ```

   Each waiting card gets an agent in the tmux session `pl-work`.
3. When a spec or plan is ready, press `3` for the Pipeline (`n` shows only the cards that need you) and select it. `a` approves (confirm with `y`). `x` opens the review screen and sends it back with what you wrote in the notes box. `e` opens it in `$EDITOR`.

From the shell, `pl --profile work approve <id>` and `pl --profile work reject <id> "notes"` do the same.

`pl idea "text"` files the idea in the pipeline board's Inbox when the profile has no `[intake]` type, which is the normal GitHub-only case. With an `[intake]` tracker configured, it goes to that board's Triage column to be assessed instead. The Ideas tab works either way.

## 7. Several GitHub accounts

If you use more than one GitHub account, give each pl profile its own gh sign-in folder. Sign in once per account, into its own folder:

```
GH_CONFIG_DIR=~/.config/gh-work gh auth login -s project
```

Then set the folder in the profile, either in `config.toml` or with `pl setup --gh-config-dir ~/.config/gh-work`:

```toml
[code_host]
gh_config_dir = "~/.config/gh-work"
```

pl passes that folder to every `gh` call and to every agent window it starts, so cards, pull request checks and the agents' own `gh` commands all use that account.

This covers `gh` only. SSH keys and host aliases only decide who `git push` and `git pull` connect as. The name and email on your commits come from git's own `includeIf` setting, not from pl.

## 8. Troubleshooting

| Message | Fix |
| --- | --- |
| `gh is missing the project scope` | `gh auth refresh -s project` |
| `gh is not installed` | Install gh and run `gh auth login`. |
| `project your-org/7 has no 'pl stage' field` | Wrong `owner`, `number` or `status_field`. Check the project URL and the field name. |
| `add a '<column>' option to the pl stage field` | Add the option in GitHub. |
| `set repo in the github-project tracker settings` | Add `repo` to `[tracker]`. |
| `gh timed out after 60s` | GitHub is slow or unreachable. The dispatcher tries again next pass. |

**Rate limits.** GitHub gives each user 5,000 GraphQL points an hour, shared by every profile, console and agent signed in as that user. pl reads the board with its own query, 2 points per 100 cards (gh's `project item-list` costs about 1 point per card), and reads one card by itself with one issue lookup of about 1 point: `pl card`, `pl section` and the check after a write never read the whole board. Each of pl's queries also brings the points left and the reset time, kept in one ledger per GitHub user under the machine folder (`github/<login>@github.com.json`). From it:

- below half the budget, each profile's periodic reads (the dispatcher's pass, the console's refresh) keep to a fair share, the limit split among the profiles that spent points this hour; below 20 % they stop until the reset;
- agents' and your commands stop below 5 %; writes (edits, moves, new cards) go on until the budget is spent;
- passes and console refreshes come up to 4 times further apart as the budget falls;
- when GitHub refuses a call for the rate limit, every profile of the same user waits for the reset it named. Profiles signed in as another GitHub user, or on another tracker, are never held by it.
- after a reset that ended a wait, each process makes its first call a random few seconds late (up to 30 s for commands and agents, 2 minutes for the dispatcher and console), so they do not all call in the same second;
- the points left come from GraphQL itself (the `rateLimit` field, or the headers of a refused GraphQL call), never from REST `/rate_limit`, which can show 5,000 left while GraphQL refuses every call.

A pass that waits prints `pass waits: GitHub budget: ...` once, not a failure. `pl usage --github` shows the points left, the reset, and what each profile and caller spent; `pl manager status` shows each profile's part. The console reuses the dispatcher's board read for up to 5.5 minutes (any write through pl ends that sooner). The open PR search is cached for 5 minutes and the 14-day PR activity for 30 minutes. `r` in the console reads the board and both searches again.

## Not supported yet

- A flag to point `pl profiles new` at an existing project. Use `pl setup --project-number N`, or edit `[tracker]` by hand.
- Draft items as cards.
- More than 500 items on a project, or 500 open issues in a repo.
