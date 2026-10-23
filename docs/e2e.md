# pl e2e: end-to-end tests in a throwaway VM

```
pl e2e [REPO] [--ref REF] [--out DIR] [--keep-vm]
```

`pl e2e` starts a fresh [Multipass](https://multipass.run) VM, sets it up from the repo's `.pl/e2e.yaml`, starts the app, runs the repo's own Playwright suite, copies the recordings and logs back, writes a report and deletes the VM. It needs no pl profile.

- `REPO`: the app repository (default: the git root of the current folder).
- `--ref`: the commit, branch or tag to test (default `HEAD`). The YAML and the code both come from that commit (`git show REF:.pl/e2e.yaml`, `git archive REF`), so uncommitted changes are not tested.
- `--out`: where results go (default `~/pl-e2e/<repo>/<YYYYmmdd-HHMMSS>/`).
- `--keep-vm`: keep the VM afterwards and print `multipass shell <name>` to look around. Remove it with `multipass delete --purge <name>`.

**The out folder must be a plain folder.** Multipass is a snap, and snap confinement cannot read `/tmp` or hidden folders (`~/.something`). pl refuses such an `--out` before it starts a VM.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every step went well and the tests passed |
| 1 | the tests failed (or ran out of time) |
| 2 | the YAML is missing or invalid, a setup step or a service failed, the VM could not be started, or the run was interrupted |

## What you get

```
~/pl-e2e/<repo>/<time>/
  report.md       verdict, each step's time, a failed step's log tail, each failing test with file:line, its error and links
  summary.json    the same as data
  logs/           setup-<n>-<name>.log, service-<name>.log, tests.log, pl-e2e-boot.log
  artifacts/      the artifacts paths, at their place in the checkout (for example artifacts/web/test-results/...)
  stage/          what was copied into the VM (app.tar.gz, the scripts, cloud-init.yaml); the env file is removed at the end
```

Artifacts and logs are copied out even when a step fails or you press Ctrl-C, and the VM is deleted afterwards. Secret values (`${VAR}` in `env`) are replaced by `***` in the logs, in text artifacts and in the report. They cannot be masked inside compressed or binary files such as `trace.zip` or videos, so do not print secrets in a test.

## .pl/e2e.yaml

```yaml
version: 1                          # required, must be 1
vm: { image: "24.04", cpus: 4, memory: 8G, disk: 30G }   # optional; these are the defaults
timeout: 3600                       # the whole run, seconds (default 3600)
packages: [docker.io, docker-compose-v2, curl]           # apt packages, installed by cloud-init
env:                                # exported to every step
  APP_ENV: Development
  API_KEY: ${YOUR_APP_API_KEY}      # read from the host's environment; masked in logs and report
setup:                              # run once, in order; stops at the first failure
  - name: infra
    run: sudo docker compose up -d postgres redis
  - name: build
    run: |
      npm ci
      npm run build
services:                           # long-running, started in the background
  - name: api
    run: npm run start
    ready: http://localhost:5000/health   # or tcp:5432 or tcp:HOST:PORT
    timeout: 300                    # seconds to wait for ready (default 300)
tests:                              # required
  dir: web                          # where the tests run, relative to the checkout (default .)
  run: npx playwright test --trace=on --reporter=list,html,json
  report: test-results/results.json # Playwright's JSON report, relative to dir
  timeout: 1800                     # seconds (default 1800)
artifacts: [web/playwright-report, web/test-results]     # copied back, relative to the checkout
```

Rules (pl lists every problem at once, each with its key path, and starts no VM):

- Unknown keys are errors, so a typo is never silently ignored.
- `vm.cpus`, `timeout`, `services[].timeout`, `tests.timeout`: positive integers. `vm.memory`, `vm.disk`: like `8G` or `512M`.
- `packages`: apt package names.
- `env`: keys are shell variable names, values strings. `${VAR}` must be set on the host; write `$$` for a literal `$`.
- `setup[].name` and `services[].name`: letters, digits, `.`, `_`, `-`, unique across both lists (they name the log files).
- `ready`: `http://...`, `https://...`, `tcp:PORT` or `tcp:HOST:PORT`, polled from inside the VM.
- `tests.dir`, `tests.report`, `artifacts`: relative paths that stay inside the checkout.

Each `run` is a bash script (`bash -euo pipefail`) run as the `ubuntu` user. Setup steps and services start in the checkout (`/home/ubuntu/app`); the tests start in `tests.dir`. When `tests.report` is set, pl sets `PLAYWRIGHT_JSON_OUTPUT_NAME` to it, so add `json` to the reporters. Without a readable JSON report the verdict comes from the test command's exit code.

## Recording traces, videos and screenshots

- Pass `--trace=on` in `tests.run`: many configs only record a trace `on-first-retry`, which records nothing when `retries` is 0.
- For videos and screenshots, set them in `playwright.config`:

  ```ts
  use: { trace: 'on', video: 'on', screenshot: 'on' },
  ```

- List the output folders (`test-results`, `playwright-report`) under `artifacts`, or nothing comes back.
- Open a trace with `npx playwright show-trace artifacts/<path>/trace.zip`, or drop the file on https://trace.playwright.dev. The report links each failing test's trace, video and screenshot.

## Debugging a failed run

1. Read `report.md`: the failed step and the last lines of its log, or the failing tests.
2. Run again with `--keep-vm`, then `multipass shell <name>`. The checkout is in `/home/ubuntu/app`, the scripts and logs in `/home/ubuntu/.pl-e2e/`.
3. Delete the VM when done: `multipass delete --purge <name>`.
