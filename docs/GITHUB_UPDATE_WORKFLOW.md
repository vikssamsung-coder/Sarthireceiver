# GitHub Update Workflow

This document defines the standard workflow for future updates to the **SarthiReceiver** repository.

## Tools and responsibilities

| Tool | Purpose |
|---|---|
| Local `git` | Inspect branches and status, compare files, isolate changes, review diffs, and verify commits locally. |
| Local editor and test commands | Modify the code and run the relevant regression, unit, integration, and smoke tests. |
| Connected GitHub app/connector | Read the remote repository, upload changed files, create remote commits, update the target branch, and verify the published content. |
| `gh` CLI | Optional alternative only. It is not required when the connected GitHub integration can perform the necessary remote operations. |

## Standard process

1. Confirm the target repository and branch:
   - Repository: `vikssamsung-coder/Sarthireceiver`
   - Default target: `main`, unless the user requests a feature branch or pull request.
2. Inspect the latest remote branch and current commit before making changes.
3. Use local `git` commands to inspect branches, status, history, and diffs.
4. Work from a clean, current checkout or an explicitly verified repository snapshot.
5. Edit only the files required for the requested change.
6. Run the relevant tests locally.
7. Review the final diff and confirm that unrelated user changes are not included.
8. Use the connected GitHub app/connector to:
   - upload the changed files;
   - create the commit;
   - update the intended remote branch.
9. Verify the resulting remote commit SHA.
10. Fetch the published files from GitHub and confirm that their contents match the tested local versions.
11. Report the commit SHA, branch, changed files, and test results to the user.

## Important operating rules

- Do not treat a missing `gh` installation as a blocker when the connected GitHub integration is available.
- Do not claim that code was published until the remote commit and file contents have been verified.
- Never overwrite remote work blindly. Re-read the latest target-branch state before publishing.
- Preserve unrelated user changes.
- Use a dedicated branch and draft pull request when the user requests reviewable delivery or when the change should not go directly to `main`.
- Direct updates to `main` are allowed only when explicitly requested or already established as the agreed workflow for the task.
- Local `git` and the GitHub connector are complementary:
  - local `git` validates the change;
  - the connector publishes and verifies the remote result.

## Proven repository precedent

Previous SarthiReceiver updates, including commits such as `b42daec` and `d9c2f14`, were published through the connected GitHub integration. They demonstrate that `gh` is not a prerequisite for updating this repository.
