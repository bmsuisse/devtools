"""The I/O around `ci_plan.make_plan`: the Azure Pipelines environment, PR labels, git, and pipeline variable output."""

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import requests

from . import ci_plan
from .ci_plan import BranchFlowError, Context

PR_MERGE_REF = re.compile(r"refs/pull/(\d+)/merge")
HTTP_TIMEOUT_SECS = 20
GIT_TIMEOUT_SECS = 120


def _branch(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def _api_url(env: Mapping[str, str], path: str) -> str:
    try:
        return (
            f"{env['SYSTEM_COLLECTIONURI']}{env['SYSTEM_TEAMPROJECTID']}/_apis/git/repositories/"
            f"{env['BUILD_REPOSITORY_ID']}/{path}api-version=7.1"
        )
    except KeyError as missing:
        raise ValueError(f"{missing.args[0]} is not set") from missing


def _get_json(url: str, env: Mapping[str, str]) -> object:
    token = env.get("SYSTEM_ACCESSTOKEN")
    if not token:
        raise ValueError(
            "SYSTEM_ACCESSTOKEN is not set (map $(System.AccessToken) into the step's env)"
        )
    response = requests.get(url, auth=("", token), timeout=HTTP_TIMEOUT_SECS)
    response.raise_for_status()
    return response.json()


def parse_labels(payload: object) -> list[str]:
    """Names of the active labels in an Azure DevOps `pullrequests/<id>/labels` response."""
    items = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("unexpected response from the PR labels API")
    names: list[str] = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(name, str):
            raise ValueError("unexpected label in the PR labels API response")
        if item.get("active", True) is True:
            names.append(name)
    return names


def fetch_labels(env: Mapping[str, str], pr_id: int) -> list[str]:
    return parse_labels(_get_json(_api_url(env, f"pullrequests/{pr_id}/labels?"), env))


def context_from_env(env: Mapping[str, str]) -> Context:
    """PR validation builds, and manual runs of a `refs/pull/<id>/merge` ref (planned like that PR); else a branch build."""
    reason = env.get("BUILD_REASON", "")
    ref = env.get("BUILD_SOURCEBRANCH", "")
    pr_id = env.get("SYSTEM_PULLREQUEST_PULLREQUESTID", "")
    if reason == "PullRequest" and pr_id.isdecimal():
        return Context(
            branch=_branch(ref),
            pr_id=int(pr_id),
            source=_branch(env.get("SYSTEM_PULLREQUEST_SOURCEBRANCH", "")),
            target=_branch(env.get("SYSTEM_PULLREQUEST_TARGETBRANCH", "")),
            is_fork=env.get("SYSTEM_PULLREQUEST_ISFORK", "").lower() == "true",
        )
    merge_ref = PR_MERGE_REF.fullmatch(ref)
    if reason == "Manual" and merge_ref:
        number = int(merge_ref[1])
        pr = _get_json(_api_url(env, f"pullrequests/{number}?"), env)
        source = pr.get("sourceRefName") if isinstance(pr, dict) else None
        target = pr.get("targetRefName") if isinstance(pr, dict) else None
        if not isinstance(source, str) or not isinstance(target, str):
            raise ValueError("unexpected response from the pull request API")
        return Context(
            branch=ref, pr_id=number, source=_branch(source), target=_branch(target)
        )
    return Context(branch=_branch(ref))


def _git(
    *args: str,
    extra_env: Mapping[str, str] | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    try:
        done = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECS,
            env={**os.environ, **(extra_env or {})},
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {args[0]} timed out") from error
    if done.returncode not in ok_codes:
        raise RuntimeError(
            f"git {' '.join(args[:2])} failed: {done.stderr.strip()[:200]}"
        )
    return done


def changed_files(target: str) -> list[str]:
    """Files the PR changes: the merge commit CI checks out against its first parent, else against `origin/<target>`."""
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    diff = ("HEAD^1", "HEAD") if len(parents) == 3 else (f"origin/{target}...HEAD",)
    return [
        line for line in _git("diff", "--name-only", *diff).stdout.splitlines() if line
    ]


def verify_fix_base(env: Mapping[str, str]) -> None:
    """A fix for `main` must contain `main` and no unreleased `dev` commits (so cut it from `main`, never from `dev`).

    Needs the PR merge commit as HEAD and fetches `origin/dev` and `origin/main`; raises RuntimeError when that is not
    possible, which the plan reports as a warning instead of failing.
    """
    parents = _git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    if len(parents) != 3:
        raise RuntimeError("HEAD is not a PR merge commit")
    token = env.get("SYSTEM_ACCESSTOKEN")
    auth = (
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: bearer {token}",
        }
        if token
        else {}
    )
    _git(
        "fetch",
        "--no-tags",
        "origin",
        "+refs/heads/dev:refs/remotes/origin/dev",
        "+refs/heads/main:refs/remotes/origin/main",
        extra_env=auth,
    )
    _, main, fix = parents
    if _git("merge-base", "--is-ancestor", main, fix, ok_codes=(0, 1)).returncode:
        raise BranchFlowError(
            "the fix branch must contain the target main commit; update it from main, never dev"
        )
    dev_only = set(
        _git("rev-list", "refs/remotes/origin/dev", "--not", main).stdout.split()
    )
    fix_only = set(_git("rev-list", fix, "--not", main).stdout.split())
    if dev_only & fix_only:
        raise BranchFlowError(
            "the fix branch contains unreleased dev commits; cut the fix from main"
        )


def emit(ado: bool, name: str, value: str) -> None:
    print(
        f"##vso[task.setvariable variable={name};isOutput=true]{value}"
        if ado
        else f"{name}={value}"
    )


def log(ado: bool, level: str, message: str) -> None:
    print(
        f"##vso[task.logissue type={level}]{message}" if ado else f"{level}: {message}"
    )


def run(
    source: str | None = None,
    target: str | None = None,
    labels: list[str] | None = None,
    *,
    start: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Plan the current CI run and print it. `source`/`target`/`labels` (the CLI's --pr-source/--pr-target/--label) simulate a PR for a local dry run."""
    env = os.environ if env is None else env
    ado = bool(env.get("TF_BUILD"))
    config = ci_plan.load_plan_config(start)
    if config is None:
        log(
            ado,
            "error",
            "no [tool.bdt.ci.plan] table in pyproject.toml (an empty table selects the defaults)",
        )
        return 2
    if source and target:
        ctx = Context(pr_id=0, source=source, target=target)
        label_provider = lambda: list(labels or [])  # noqa: E731
    else:
        try:
            ctx = context_from_env(env)
        except (OSError, ValueError, KeyError) as error:
            log(
                ado,
                "warning",
                f"the build context could not be read ({error}), running everything",
            )
            ctx = Context()
        label_provider = lambda: fetch_labels(env, ctx.pr_id or 0)  # noqa: E731
    plan = ci_plan.make_plan(
        ctx,
        config,
        labels=label_provider,
        changed_files=lambda: changed_files(ctx.target),
        verify_fix_base=lambda: verify_fix_base(env),
    )
    for warning in plan.warnings:
        log(ado, "warning", warning)
    if plan.kind == "error":
        log(ado, "error", plan.reason)
        return 1
    print(f"test plan: {plan.kind} ({plan.reason})")
    for name, value in ci_plan.outputs(plan, config).items():
        emit(ado, name, value)
    return 0
