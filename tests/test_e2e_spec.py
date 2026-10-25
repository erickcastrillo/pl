"""pl e2e's .pl/e2e.yaml loader: defaults, validation with key paths, ${VAR} resolution, masking, safe loading."""
import pytest

from pl.e2e_spec import SpecError, load, mask

MIN = "version: 1\ntests:\n  run: npx playwright test\n"

FULL = """
version: 1
vm:      { image: "24.04", cpus: 4, memory: 8G, disk: 30G }
timeout: 3600
packages: [docker.io, docker-compose-v2, curl]
env:
  ASPNETCORE_ENVIRONMENT: Development
  OPENAI_API_KEY: ${APP_OPENAI_KEY}
setup:
  - name: infra
    run: sudo docker compose up -d postgres redis azurite
  - name: dotnet
    run: |
      curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 10.0
services:
  - name: api
    run: dotnet run --project YourApp.WebApi
    ready: http://localhost:5000/health
    timeout: 300
tests:
  dir: YourApp.WebApi/Frontend
  run: npx playwright test --trace=on --reporter=list,html,json
  report: test-results/results.json
  timeout: 1800
artifacts: [YourApp.WebApi/Frontend/playwright-report, YourApp.WebApi/Frontend/test-results]
"""


def errors(text, env=None):
    with pytest.raises(SpecError) as e:
        load(text, env or {})
    return e.value.problems


def test_defaults():
    s = load(MIN, {})
    assert s.version == 1
    assert (s.vm.image, s.vm.cpus, s.vm.memory, s.vm.disk) == ("24.04", 4, "8G", "30G")
    assert s.timeout == 3600
    assert s.packages == [] and s.env == {} and s.setup == [] and s.services == [] and s.artifacts == []
    assert (s.tests.dir, s.tests.run, s.tests.report, s.tests.timeout) == (".", "npx playwright test", None, 1800)
    assert s.secrets == []


def test_full_example():
    s = load(FULL, {"APP_OPENAI_KEY": "sk-secret"})
    assert s.vm.cpus == 4 and s.vm.memory == "8G"
    assert s.packages == ["docker.io", "docker-compose-v2", "curl"]
    assert s.env == {"ASPNETCORE_ENVIRONMENT": "Development", "OPENAI_API_KEY": "sk-secret"}
    assert [x.name for x in s.setup] == ["infra", "dotnet"]
    assert s.setup[1].run.startswith("curl -fsSL")
    svc = s.services[0]
    assert (svc.name, svc.ready, svc.timeout) == ("api", "http://localhost:5000/health", 300)
    assert s.tests.dir == "YourApp.WebApi/Frontend" and s.tests.report == "test-results/results.json"
    assert s.artifacts[1] == "YourApp.WebApi/Frontend/test-results"
    assert s.secrets == ["sk-secret"]


def test_vm_partial_overrides_keep_defaults():
    s = load(MIN + "vm: {cpus: 2, memory: 512M}\n", {})
    assert (s.vm.image, s.vm.cpus, s.vm.memory, s.vm.disk) == ("24.04", 2, "512M", "30G")


def test_service_timeout_default():
    s = load(MIN + "services:\n  - {name: db, run: x, ready: 'tcp:5432'}\n", {})
    assert s.services[0].timeout == 300 and s.services[0].ready == "tcp:5432"


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n", "version: [\n"])
def test_not_a_mapping_or_bad_yaml(text):
    assert errors(text)


def test_version_must_be_1():
    assert errors("version: 2\ntests: {run: x}\n") == ["version: must be 1"]
    assert errors("tests: {run: x}\n") == ["version: required"]
    assert errors("version: 1.0\ntests: {run: x}\n") == ["version: must be 1"]


def test_tests_run_required():
    assert errors("version: 1\n") == ["tests: required"]
    assert errors("version: 1\ntests: {dir: .}\n") == ["tests.run: required"]


@pytest.mark.parametrize("extra,problem", [
    ("vmm: {}\n", "vmm: unknown key"),
    ("vm: {cpu: 2}\n", "vm.cpu: unknown key"),
    ("setup:\n  - {name: a, run: x, rn: y}\n", "setup[0].rn: unknown key"),
    ("services:\n  - {name: a, run: x, ready: 'tcp:1', redy: z}\n", "services[0].redy: unknown key"),
])
def test_unknown_keys(extra, problem):
    assert problem in errors(MIN + extra)


def test_unknown_key_in_tests():
    assert errors("version: 1\ntests: {run: x, timout: 5}\n") == ["tests.timout: unknown key"]


@pytest.mark.parametrize("extra,problem", [
    ("timeout: 0\n", "timeout: must be a positive integer"),
    ("timeout: '60'\n", "timeout: must be a positive integer"),
    ("timeout: true\n", "timeout: must be a positive integer"),
    ("vm: {cpus: -1}\n", "vm.cpus: must be a positive integer"),
    ("vm: {memory: 8GB}\n", "vm.memory: must look like 8G or 512M"),
    ("vm: {disk: 30}\n", "vm.disk: must look like 8G or 512M"),
    ("vm: {image: 24.04}\n", "vm.image: must be a string"),
    ("vm: []\n", "vm: must be a mapping"),
    ("packages: curl\n", "packages: must be a list"),
    ("packages: [Curl]\n", "packages[0]: not a valid apt package name"),
    ("packages: ['curl; rm -rf /']\n", "packages[0]: not a valid apt package name"),
    ("env: [A]\n", "env: must be a mapping"),
    ("env: []\n", "env: must be a mapping"),
    ("vm: 'big'\n", "vm: must be a mapping"),
    ("env: {A: 1}\n", "env.A: must be a string"),
    ("env: {'BAD-NAME': x}\n", "env.BAD-NAME: not a valid variable name"),
    ("setup: {name: a}\n", "setup: must be a list"),
    ("setup: [x]\n", "setup[0]: must be a mapping"),
    ("setup:\n  - {run: x}\n", "setup[0].name: required"),
    ("setup:\n  - {name: a}\n", "setup[0].run: required"),
    ("setup:\n  - {name: 'a b', run: x}\n", "setup[0].name: only letters, digits, '.', '_' and '-'"),
    ("setup:\n  - {name: ../x, run: x}\n", "setup[0].name: only letters, digits, '.', '_' and '-'"),
    ("services:\n  - {name: a, run: x}\n", "services[0].ready: required"),
    ("services:\n  - {name: a, run: x, ready: 'localhost:80'}\n",
     "services[0].ready: must start with http://, https:// or tcp:"),
    ("services:\n  - {name: a, run: x, ready: 'tcp:abc'}\n", "services[0].ready: must be tcp:PORT or tcp:HOST:PORT"),
    ("services:\n  - {name: a, run: x, ready: 'tcp:70000'}\n", "services[0].ready: must be tcp:PORT or tcp:HOST:PORT"),
    ("services:\n  - {name: a, run: x, ready: 'tcp:1', timeout: 0}\n", "services[0].timeout: must be a positive integer"),
    ("artifacts: out\n", "artifacts: must be a list"),
    ("artifacts: [/etc]\n", "artifacts[0]: must be a relative path inside the checkout"),
    ("artifacts: [a/../../b]\n", "artifacts[0]: must be a relative path inside the checkout"),
    ("artifacts: ['']\n", "artifacts[0]: must be a relative path inside the checkout"),
])
def test_rules(extra, problem):
    assert errors(MIN + extra) == [problem]


def test_ready_forms_accepted():
    for r in ("http://localhost:5000/health", "https://x/", "tcp:5432", "tcp:db.local:5432", "tcp:127.0.0.1:6379"):
        assert load(MIN + f"services:\n  - {{name: a, run: x, ready: '{r}'}}\n", {}).services[0].ready == r


def test_package_names_accepted():
    s = load(MIN + "packages: [g++, libssl3, python3.12-venv, 'nodejs=20.1~1', 'lib:amd64']\n", {})
    assert len(s.packages) == 5


@pytest.mark.parametrize("tests,problem", [
    ("{run: x, dir: /abs}", "tests.dir: must be a relative path inside the checkout"),
    ("{run: x, dir: ..}", "tests.dir: must be a relative path inside the checkout"),
    ("{run: x, report: ../r.json}", "tests.report: must be a relative path inside the checkout"),
    ("{run: x, timeout: -5}", "tests.timeout: must be a positive integer"),
    ("{run: 5}", "tests.run: must be a string"),
    ("{run: '  '}", "tests.run: required"),
])
def test_tests_rules(tests, problem):
    assert errors(f"version: 1\ntests: {tests}\n") == [problem]


def test_unique_names_across_setup_and_services():
    text = MIN + "setup:\n  - {name: a, run: x}\n  - {name: a, run: y}\nservices:\n  - {name: a, run: z, ready: 'tcp:1'}\n"
    assert errors(text) == ["setup[1].name: duplicate name 'a'", "services[0].name: duplicate name 'a'"]


def test_all_problems_reported_together():
    text = "version: 3\nvmm: 1\nvm: {cpus: 0}\nservices:\n  - {name: a, run: x, ready: 'ftp://x'}\ntests: {}\n"
    with pytest.raises(SpecError) as e:
        load(text, {})
    p = e.value.problems
    assert p == ["version: must be 1", "vmm: unknown key", "vm.cpus: must be a positive integer",
                 "services[0].ready: must start with http://, https:// or tcp:", "tests.run: required"]
    assert all(x in str(e.value) for x in p)


def test_var_resolution():
    s = load(MIN + "env:\n  A: ${X}\n  B: pre-${X}-${Y}\n  C: plain\n  D: cost $$5\n  E: ${EMPTY}\n",
             {"X": "one", "Y": "two", "EMPTY": ""})
    assert s.env == {"A": "one", "B": "pre-one-two", "C": "plain", "D": "cost $5", "E": ""}
    assert sorted(s.secrets) == ["one", "two"]


def test_missing_var_names_key_and_var_not_values():
    p = errors(MIN + "env:\n  A: ${NOPE}\n  B: ${SET}\n", {"SET": "hunter2"})
    assert p == ["env.A: ${NOPE} is not set on the host"]
    assert "hunter2" not in str(p)


def test_vars_only_expanded_in_env():
    s = load("version: 1\ntests: {run: 'echo ${X}'}\nsetup:\n  - {name: a, run: 'echo ${X}'}\n", {"X": "v"})
    assert s.tests.run == "echo ${X}" and s.setup[0].run == "echo ${X}" and s.secrets == []


def test_safe_load_rejects_python_tags():
    assert errors("!!python/object/apply:os.system ['echo hi']\n")
    assert errors(MIN + "env: {A: !!python/object:builtins.object {}}\n")


def test_mask():
    assert mask("key=abcdef and abc", ["abc", "abcdef"]) == "key=*** and ***"
    assert mask("nothing here", []) == "nothing here"
    assert mask("x", [""]) == "x"
