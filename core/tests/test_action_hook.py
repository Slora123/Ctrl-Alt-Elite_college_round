import pytest

from crisisgrid_core.action_hook import Action, ActionHook, classify_edit
from crisisgrid_core.harness import HarnessConfig, HarnessGuard


def hook(**cfg):
    return ActionHook(HarnessGuard(HarnessConfig(**cfg)))


@pytest.mark.parametrize("old,new,risk", [
    # safe: imports, comments, paths
    ("from math import floor_div", "from math import floor", "safe"),
    ("import numpy as np", "import numpy as np\nimport os", "safe"),
    ("x = 1", "# the value\nx = 1", "safe"),
    ('data_dir = "/old/cifar"', 'data_dir = "/data/cifar"', "safe"),
    ('train_path = "a/train.csv"', 'train_path = "b/train.csv"', "safe"),
    ('df = pd.read_csv("data/old.csv")', 'df = pd.read_csv("data/train.csv")', "safe"),
    ("", "", "safe"),
    # experiment-changing: anything that could alter the result
    ("lr = 0.1", "lr = 0.01", "experiment_changing"),
    ("epochs = 90", "epochs = 30", "experiment_changing"),
    ("model = ResNet(depth=20)", "model = ResNet(depth=32)", "experiment_changing"),
    ("opt = SGD(m.parameters(), lr=LR)", "opt = Adam(m.parameters(), lr=LR)", "experiment_changing"),
    ("x = x / 255", "x = (x - 0.5) / 0.25", "experiment_changing"),
    ("acc = evaluate(m)", "acc = evaluate(m) + 1", "experiment_changing"),
    ("torch.manual_seed(0)", "torch.manual_seed(1)", "experiment_changing"),
    ('data_dir = "/data/v2"', 'data_dir = "/data/v2"\nbatch_size = 64', "experiment_changing"),
    ('split_path = "data/split_70_30.csv"', 'split_path = "data/split_80_20.csv"', "experiment_changing"),  # a path that is really a split choice
    ("import os", "import os; epochs = 3", "experiment_changing"),                                          # smuggled in behind an import
    ("a = 1", "a = 1\nb = 2", "experiment_changing"),                                                       # unknown code: cautious default
])
def test_edit_classification_is_done_from_the_diff(old, new, risk):
    assert classify_edit(old, new)[0] == risk


@pytest.mark.parametrize("action,decision,risk", [
    (Action(kind="cell_run", author="agent"), "allow", "safe"),
    (Action(kind="install", author="agent", payload={"package": "torchvision==0.16.0"}), "allow", "safe"),
    (Action(kind="pin", author="agent", payload={"package": "numpy<2"}), "allow", "safe"),
    (Action(kind="env_rebuild", author="agent"), "allow", "safe"),
    (Action(kind="edit", author="agent", payload={"old": "from m import a", "new": "from m import b"}), "allow", "safe"),
    (Action(kind="edit", author="agent", payload={"old": "lr = 0.1", "new": "lr = 0.2"}), "needs_approval", "experiment_changing"),
    (Action(kind="edit", author="generator", payload={"old": "lr = 0.1", "new": "lr = 0.2"}), "needs_approval", "experiment_changing"),
    (Action(kind="edit", author="human", payload={"old": "lr = 0.1", "new": "lr = 0.2"}), "allow", "experiment_changing"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "https://huggingface.co/datasets/x"}), "allow", "safe"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "https://cdn-lfs.huggingface.co/f"}), "allow", "safe"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "https://sketchy.example.net/data.zip"}), "needs_approval", "external"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "https://evilhuggingface.co/x"}), "needs_approval", "external"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "file:///etc/passwd"}), "deny", "blocked"),
    (Action(kind="dataset_fetch", author="agent", payload={"url": "https://huggingface.co/x", "mount": "/home"}), "deny", "blocked"),
    (Action(kind="install", author="agent", payload={"package": "git+https://evil.example/x.git"}), "deny", "blocked"),
    (Action(kind="install", author="agent", payload={"package": "--index-url http://evil x"}), "deny", "blocked"),
    (Action(kind="install", author="agent", payload={"package": "../../local/pkg"}), "deny", "blocked"),
    (Action(kind="install", author="agent", payload={"package": "numpy", "index": "https://evil.example/simple"}), "needs_approval", "external"),
])
def test_policy_table(action, decision, risk):
    d = hook().check(action)
    assert (d.decision, d.risk_class) == (decision, risk) and d.reason


def test_budget_exhaustion_denies_even_a_safe_action():
    h = hook(stage_token_budget=10)
    h.guard.charge_tokens("repair", 11)
    d = h.check(Action(kind="cell_run", author="agent"))
    assert d.decision == "deny" and "token budget" in d.reason


def test_allowlist_is_configurable_and_matches_subdomains_exactly():
    h = ActionHook(HarnessGuard(), allowlist={"data.example.org"})
    ok = Action(kind="dataset_fetch", author="agent", payload={"url": "https://data.example.org/x"})
    sub = Action(kind="dataset_fetch", author="agent", payload={"url": "https://mirror.data.example.org/x"})
    other = Action(kind="dataset_fetch", author="agent", payload={"url": "https://example.org/x"})
    assert [h.check(a).decision for a in (ok, sub, other)] == ["allow", "allow", "needs_approval"]


@pytest.mark.parametrize("old,new", [
    ('data_dir = "/data/a"', 'data_dir = "/data/dropoutRate"'),     # camelCase experimental token inside a path
    ('p = "a"', 'train_split = "x"'),
])
def test_camel_case_and_snake_case_experimental_tokens_are_caught(old, new):
    assert classify_edit(old, new)[0] == "experiment_changing"
