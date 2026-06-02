# PROMPT.md workspaceExecPwsh review notes

This document is for another AI/code maintainer to review. It does **not** modify `PROMPT.md` directly. It records observed issues from using `workspaceExecPwsh` through this gateway, plus recommended prompt changes and optional backend hardening ideas.

## Context

The assistant prompt in this repository is `PROMPT.md` at the repository root. During maintenance work in another repository through this gateway, `workspaceExecPwsh` was used to read files, search code, run tests, inspect diffs, and commit changes.

The workspace shell is PowerShell 7 (`pwsh`), not Bash.

No severe mojibake or UTF-8 data loss was observed. Chinese Markdown and UTF-8 file writes worked correctly. The main issues were usability/noise issues and one security-scanner false positive caused by forbidden command strings appearing in explanatory text.

## Observed issues

### 1. ANSI/control sequence noise in command output

Observed output sometimes included ANSI color/control sequences, especially around PowerShell formatted object/table output, for example sequences like:

```text
\x1b[32;1mMode \x1b[0m
```

Impact:

- Not data corruption.
- Makes tool output harder for an AI reviewer to read.
- Can obscure real warnings in long command output.

Likely cause:

- PowerShell formatting/color output is captured literally by the gateway or rendered without stripping ANSI escapes.

Prompt-level mitigation:

```powershell
$ProgressPreference = 'SilentlyContinue'
if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
```

Also prefer explicit string formatting over `Format-Table` for content the model must inspect.

Backend hardening option:

- Force plain output in `workspaceExecPwsh` startup.
- Or strip ANSI escape sequences from captured stdout/stderr before returning responses.

### 2. PowerShell is not Bash; Bash heredoc syntax fails

Observed failed pattern:

```powershell
python - <<'PY'
print('hello')
PY
```

PowerShell reports a parser error similar to:

```text
Missing file specification after redirection operator.
```

Prompt-level mitigation:

Use PowerShell here-strings and pipe to Python:

```powershell
$code = @'
print('hello')
'@
$code | python -
```

Recommended prompt change:

- Explicitly say `workspaceExecPwsh` scripts are PowerShell 7.
- Warn not to use Bash heredoc syntax.
- Provide the here-string pattern above.

### 3. Git CRLF/LF warnings on Windows workspaces

Observed warnings:

```text
warning: in the working copy of 'file.py', LF will be replaced by CRLF the next time Git touches it
```

Impact:

- Not necessarily a failure.
- Adds noise to validation output.
- May hide more important warnings.

Prompt-level mitigation:

- Prefer `workspaceWriteFile(..., line_ending='lf')` for new full-file writes when the target repository uses LF.
- Do not treat CRLF warnings as failures unless `git diff --check`, tests, or actual content checks fail.
- Encourage repositories to add `.gitattributes`, for example:

```gitattributes
*.py text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.json text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
```

Backend hardening option:

- Configure workspace Git line endings consistently.
- Or document that target repositories should own line-ending policy through `.gitattributes`.

### 4. UTF-8 output should be made explicit

No severe UTF-8 issue was observed, but explicit encoding is still safer because the gateway may run on Windows workspaces and the user may ask for Chinese documentation or logs.

Prompt-level mitigation prelude:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'
$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'
```

Also continue to prefer `workspaceWriteFile` for complete UTF-8 text files, because it explicitly supports UTF-8 and line-ending control.

Backend hardening option:

- Start `pwsh` with UTF-8-friendly console/output configuration.
- Add tests that emit Chinese text and verify returned stdout/stderr is valid UTF-8.

### 5. Security scanner can reject forbidden command strings in explanatory text

When trying to write prompt guidance with `workspaceExecPwsh`, the script was rejected because explanatory Markdown contained a forbidden publish command string. The command was not intended to run; it was inside text content.

Impact:

- Good from a conservative safety perspective.
- But it can surprise the assistant when writing documentation about forbidden commands.

Mitigation:

- Use `workspaceApplyPatch` or `workspaceWriteFile` for documentation content rather than embedding large Markdown in a PowerShell script.
- Avoid spelling forbidden command strings in executable script payloads when not needed.
- Mention this behavior in prompt guidance so future assistants understand the failure mode.

Backend hardening option:

- Keep the current conservative scanner, but improve error messages to indicate whether the match was in script text and suggest using `workspaceWriteFile` for documentation-only content.

### 6. Structured Gateway tools are more reliable than parsing shell output

Observed outcome:

- `workspaceStatus`, `workspaceDiff`, `workspaceCommitAndPush`, and PR/CI tools returned structured data and were easier to trust than shell-formatted output.

Recommended prompt emphasis:

- Use `workspaceStatus` instead of parsing `git status` for authoritative workspace state.
- Use `workspaceDiff` before committing.
- Use `workspaceCommitAndPush` for publishing commits.
- Use CI/log Gateway tools instead of ad-hoc GitHub CLI commands.
- Use `workspaceExecPwsh` primarily for reading files, searching, running tests, and local validation.

## Recommended `PROMPT.md` addition

Add a section after the current `workspaceExecPwsh` tool description and before `Context gathering for code changes`.

Suggested text:

```markdown
## workspaceExecPwsh PowerShell 7 guidance

`workspaceExecPwsh` runs PowerShell 7 (`pwsh`) from the repository root. Treat scripts as PowerShell, not Bash.

For non-trivial commands, prefer this prelude:

```powershell
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'
$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'
```

Known pitfalls:

- Avoid Bash heredoc syntax such as `python - <<'PY'`; use PowerShell here-strings and pipe to the process instead.
- PowerShell table output can include ANSI/control sequences; use the prelude above and prefer explicit string formatting when output must be read by the model.
- Windows workspaces can print CRLF/LF Git warnings. Treat them as noise unless `git diff --check`, tests, or content checks fail.
- Prefer `workspaceWriteFile` for complete UTF-8 files and line-ending control.
- Prefer structured Gateway tools for Git/PR/CI state rather than parsing shell output.
- Security scanning may reject forbidden command strings even when they appear in explanatory text inside a PowerShell script; use `workspaceApplyPatch` or `workspaceWriteFile` for documentation-heavy edits.

PowerShell multi-line Python pattern:

```powershell
$code = @'
from pathlib import Path
print(Path.cwd())
'@
$code | python -
```
```

Note: the nested Markdown fence above may need escaping or different fence lengths when copied into `PROMPT.md`.

## Optional backend improvements

These are runtime/backend improvements, not prompt-only changes:

1. Inject the recommended PowerShell prelude automatically for `workspaceExecPwsh`, or provide an opt-in `plain_output` mode.
2. Strip ANSI escape sequences from captured stdout/stderr.
3. Set UTF-8 console/output encoding at `pwsh` process startup.
4. Add tests for:
   - Chinese stdout/stderr round trip.
   - ANSI-colored output stripping/plain mode.
   - CRLF/LF-sensitive file writes.
   - Documentation text containing forbidden command strings, with a clear suggested workaround.
5. Consider making line-ending policy explicit in workspace initialization or docs.

## Review checklist

A reviewer should decide:

- Whether `PROMPT.md` should include the full section or a shorter version.
- Whether the gateway should handle plain output and UTF-8 automatically instead of relying on prompt guidance.
- Whether security-scan false positives on explanatory text should remain strict or get a documentation-specific path.
- Whether `.gitattributes` should be added to this repository itself.

## Validation performed for this document

When this document was created:

- The previous direct edit to `PROMPT.md` was reverted.
- This file was added as a separate root-level review document.
- `git diff --check` should be run before commit.
- Full test suite can be run with `python -m pytest -q`; the previous prompt-only edit passed `86 passed`, but this document-only revision should still be checked before final PR update.
