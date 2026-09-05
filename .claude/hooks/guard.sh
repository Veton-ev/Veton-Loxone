#!/usr/bin/env bash
# .claude/hooks/guard.sh — Claude Code PreToolUse hook for the Bash tool (guardrail tier G6).
# PUBLIC-REPO VARIANT (kit/claude/hooks/guard.public.sh, installed by kit/apply.sh --public): identical to the
# private hook except that remote-shell commands are blocked as a whole family — no host is named here,
# because this file is published with the repo.
#
# Contract (verified against code.claude.com/docs/en/hooks, 2026-08-26):
#   stdin  : JSON {"tool_name":"Bash","tool_input":{"command":"..."},"cwd":"...",...}
#   exit 0 : no decision — normal permission rules apply
#   exit 2 : BLOCK — stderr is shown to the agent as the reason. A blocking hook wins over allow rules.
#   other  : non-blocking error (the call proceeds!) — so every internal failure here must be
#            either a clean exit 0 or a deliberate exit 2, never a stray exit 1.
#
# It is the second fence behind permissions.deny in .claude/settings.json. The deny list matches
# command PREFIXES; this script re-checks the INTENT after splitting compound commands, stripping
# quotes and wrappers (bash -c, sudo, env, timeout, ...), so a re-phrased command is still caught:
#   git push origin HEAD:refs/heads/main · bash -c 'make deploy-pi' · sh deploy.sh · cd x; ./deploy/deploy.sh
#
# What is blocked (see README "Agent fences" for the rationale):
#   1. git push that lands on main/master (explicit refspec, HEAD:main, or the CURRENT branch when
#      no refspec is given — the current branch is read with `git rev-parse` in the command's cwd)
#   2. git push --force / -f / --force-with-lease / --force-if-includes / --mirror / --all / '+ref'
#   3. git push --delete / -d / ':branch'
#   4. git branch -D/-d/--delete/-f main|master
#   5. ssh / scp / rsync / sftp / mosh / autossh — ANY invocation, whatever the target. A public integration
#      repo has nothing to deploy and no host to reach; a host-specific rule would publish the host.
#   6. docker aimed at a remote daemon (-H / --host / --context / DOCKER_HOST=ssh|tcp) running
#      compose up/down/restart/stop/rm/exec/run, and `docker context use <non-default>`
#   7. make deploy* / make push-firmware
#   8. running deploy.sh, deploy/deploy.sh, canary-rollout.sh, canary-deploy.sh (any path, any shell)
#   9. gh workflow run · gh api ... dispatches
#  10. prisma migrate deploy|reset|resolve · prisma db push|execute
#  11. odoo-bin / odoo with -u/--update/-i/--init
#  12. git push of a RELEASE TAG: any refspec naming (or globbing) a v<digit>… tag — v1.2.3, refs/tags/v1.2.3,
#      "v*", refs/tags/*, `origin tag v1.2.3` — and --tags / --follow-tags (they carry every local v* tag).
#      A pushed v* tag is a release trigger, so a release tag is a human action. `git tag v1.2.3` itself
#      stays allowed (local, harmless); non-v tags (docs-2026) push.
#
# Known gaps (documented, not hidden): a command fed to a shell via a file or heredoc
# (`bash < script`), aliases/functions defined earlier in the same session, and `git config` tricks
# (e.g. push.default / url rewrites) are not analysed. Branch protection (G1) is the fence that
# binds humans and covers those.
#
# Test: kit/test-guard-public.sh  (env GUARD_COMMAND / GUARD_CWD bypass stdin for tests;
#       GUARD_JSON_PARSER=jq|python3|bash forces one extractor)
set -euo pipefail

# ------------------------------------------------------------------ input
TOOL="" CMD="" CWD=""
json_unescape() { # decode a JSON string body (without the surrounding quotes)
  local s="$1" out="" i c n
  n=${#s}
  for ((i = 0; i < n; i++)); do
    c=${s:i:1}
    if [ "$c" = '\' ]; then
      i=$((i + 1)); c=${s:i:1}
      case "$c" in
        n) out+=$'\n' ;; t) out+=$'\t' ;; r) out+=$'\r' ;; b) out+=$'\b' ;; f) out+=$'\f' ;;
        u) out+="$(printf '%b' "\\u${s:i+1:4}" 2>/dev/null || printf '?')"; i=$((i + 4)) ;;
        *) out+="$c" ;;
      esac
    else
      out+="$c"
    fi
  done
  printf '%s' "$out"
}
json_field_bash() { # $1 = key ; first occurrence of "key": "..." in $INPUT
  local m
  m="$(printf '%s' "$INPUT" | tr '\n' ' ' | grep -oE "\"$1\"[[:space:]]*:[[:space:]]*\"(\\\\.|[^\"\\\\])*\"" | head -1 || true)"
  [ -n "$m" ] || { printf ''; return 0; }
  m="${m#*:}"; m="${m#"${m%%[!\ ]*}"}"   # drop key + colon + leading spaces
  m="${m#\"}"; m="${m%\"}"
  json_unescape "$m"
}

if [ -n "${GUARD_COMMAND:-}" ]; then
  CMD="$GUARD_COMMAND"; CWD="${GUARD_CWD:-$PWD}"; TOOL="Bash"
else
  INPUT="$(cat || true)"
  parser="${GUARD_JSON_PARSER:-auto}"
  if [ "$parser" = auto ]; then
    if command -v jq >/dev/null 2>&1; then parser=jq
    elif command -v python3 >/dev/null 2>&1; then parser=python3
    else parser=bash; fi
  fi
  case "$parser" in
    jq)
      TOOL="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""' 2>/dev/null || true)"
      CMD="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null || true)"
      CWD="$(printf '%s' "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || true)" ;;
    python3)
      TOOL="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_name",""))' 2>/dev/null || true)"
      CMD="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(str((d.get("tool_input") or {}).get("command","")))' 2>/dev/null || true)"
      CWD="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("cwd",""))' 2>/dev/null || true)" ;;
    *)
      TOOL="$(json_field_bash tool_name)"
      CMD="$(json_field_bash command)"
      CWD="$(json_field_bash cwd)" ;;
  esac
  # jq/python may have failed on odd input → fall back to the bash extractor once
  if [ -z "$CMD" ] && [ "$parser" != bash ]; then CMD="$(json_field_bash command)"; TOOL="${TOOL:-$(json_field_bash tool_name)}"; CWD="${CWD:-$(json_field_bash cwd)}"; fi
fi
[ -n "$CWD" ] || CWD="$PWD"
if [ -n "$TOOL" ] && [ "$TOOL" != "Bash" ]; then exit 0; fi
[ -n "$CMD" ] || exit 0

# ------------------------------------------------------------------ block
block() {
  local short="${CMD:0:200}"
  cat >&2 <<EOF
🛑 guard.sh blocked this command — $1
   command: ${short//$'\n'/ ⏎ }
   Production changes only via a reviewed PR merged to main and the GitHub Actions deploy
   workflow (CI green → deploy → smoke → auto-rollback). Agents never push to main, force-push,
   run deploy scripts, drive remote docker, or ssh/rsync/scp anywhere from this repo.
   Do instead: commit on a feature branch, \`git push -u origin <branch>\`, open a PR with
   \`gh pr create\`, and let CI + the deploy workflow do the rest. If a human must act, say so and stop.
EOF
  exit 2
}

DEPLOY_SCRIPT_RE='(^|/)(deploy|canary-rollout|canary-deploy|canary-broadcast|canary-restore)\.sh$'
READONLY_RE='^(grep|rg|egrep|fgrep|cat|echo|printf|less|more|head|tail|awk|sed|ls|wc|diff|stat|file|which|type|man|vim|vi|nano|code|bat|tree|jq|yq|sort|uniq|cut|tr|xxd|hexdump|md5sum|sha256sum|shellcheck|test|\[)$'
SHELL_RE='^(bash|sh|zsh|dash|ksh|source|\.|eval)$'
WRAPPER_RE='^(sudo|doas|env|nohup|exec|command|builtin|time|stdbuf|setsid|ionice|noglob|nocorrect|xargs|chronic|unbuffer|nice|timeout|flock)$'

CD_DIR=""          # directory a preceding `cd` moved into (for the current-branch check)
PIPED_SHELL=0      # a bare `bash`/`sh` appeared → something is being piped into a shell

# ------------------------------------------------------------------ helpers
declare -a T=()
shift_t() { T=("${T[@]:${1:-1}}"); }

strip_wrappers() { # peel sudo/env/bash -c/... until T[0] is the real program
  local first
  while [ "${#T[@]}" -gt 0 ]; do
    first="${T[0]}"
    if [[ "$first" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then shift_t; continue; fi
    if [[ "$first" =~ $SHELL_RE ]]; then
      shift_t
      while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do
        case "${T[0]}" in
          -o) shift_t 2 ;;
          -n|-n[a-z]*) T=(); return 0 ;;   # `bash -n file` = syntax check only, nothing runs
          *) shift_t ;;
        esac
      done
      [ "${#T[@]}" -eq 0 ] && PIPED_SHELL=1
      continue
    fi
    if [[ "$first" =~ $WRAPPER_RE ]]; then
      shift_t
      case "$first" in
        timeout)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do case "${T[0]}" in -s|-k|--signal|--kill-after) shift_t 2 ;; *) shift_t ;; esac; done
          [ "${#T[@]}" -gt 0 ] && shift_t ;;   # the duration
        nice|ionice)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do case "${T[0]}" in -n|-c|-p|--adjustment|--class|--classdata) shift_t 2 ;; *) shift_t ;; esac; done ;;
        sudo|doas)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do case "${T[0]}" in -u|-g|-h|-p|-C|-D|-r|-t|-U|-T) shift_t 2 ;; *) shift_t ;; esac; done ;;
        flock)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do case "${T[0]}" in -w|-E|--timeout|--conflict-exit-code) shift_t 2 ;; *) shift_t ;; esac; done
          [ "${#T[@]}" -gt 0 ] && shift_t ;;   # the lock file
        xargs)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do case "${T[0]}" in -n|-P|-L|-s|-I|-d|-a|-E|--max-args|--max-procs|--replace|--delimiter) shift_t 2 ;; *) shift_t ;; esac; done ;;
        *)
          while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do shift_t; done ;;
      esac
      continue
    fi
    break
  done
  return 0
}

check_head_policy() { # `git push` with no refspec / HEAD: pushes the CURRENT branch
  local dir="${CD_DIR:-$CWD}" br=""
  br="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  case "$br" in
    main|master) block "this pushes the current branch, which is '$br'. Create a feature branch first." ;;
    ""|HEAD) block "it pushes the current branch but the branch cannot be determined in '$dir' (not a git repo, or detached HEAD). Name the branch: git push -u origin <feature-branch>" ;;
  esac
  return 0
}

TAG_BLOCK_MSG="release tags are pushed by a human — see the internal handbook"

is_release_tag_ref() { # $1 = one side of a refspec (quotes already stripped). True when it names, or a
  local r="$1" t          # glob could match, a v<digit>… tag: v1.2.3 · refs/tags/v1.2.3 · v* · refs/tags/* · refs/*
  case "$r" in
    refs/tags/*) t="${r#refs/tags/}"; [[ "$t" =~ ^v[0-9] ]] || [[ "$t" =~ ^v?[*?\[] ]] ;;
    refs/\**|refs/\?*|refs/\[*) return 0 ;;            # refs/* covers refs/tags/*
    refs/*) return 1 ;;                                 # refs/heads/…, refs/remotes/… — not a tag
    *) [[ "$r" =~ ^v[0-9] ]] || [[ "$r" =~ ^v[*?\[] ]] ;;
  esac
}

check_git_push() {
  local -a A=("$@")
  local i=0 n=${#A[@]} a remote="" src dest next_is_tag=0
  local -a refspecs=()
  while [ "$i" -lt "$n" ]; do
    a="${A[$i]}"; i=$((i + 1))
    case "$a" in
      --force|--force-with-lease|--force-with-lease=*|--force-if-includes|--mirror|--all|--branches|--delete|--prune|-d)
        block "git push $a (force / delete / mirror is never done from an agent session)" ;;
      -o|--push-option|--repo|--receive-pack|--exec) i=$((i + 1)); continue ;;
      --tags|--follow-tags) block "git push $a would carry every local v* tag — $TAG_BLOCK_MSG" ;;
      --*) continue ;;
      -*) [[ "$a" =~ ^-[A-Za-z]*[fd] ]] && block "git push $a contains -f (force) or -d (delete)"; continue ;;
      *) if [ -z "$remote" ]; then remote="$a"
         elif [ "$a" = tag ] && [ "$next_is_tag" = 0 ]; then next_is_tag=1   # `git push origin tag v1.2.3`
         elif [ "$next_is_tag" = 1 ]; then next_is_tag=0; refspecs+=("refs/tags/$a")
         else refspecs+=("$a"); fi ;;
    esac
  done
  if [ "${#refspecs[@]}" -eq 0 ]; then check_head_policy; return 0; fi
  for a in "${refspecs[@]}"; do
    [[ "$a" == +* ]] && block "git push with a '+' refspec ($a) is a force push"
    if [[ "$a" == *:* ]]; then src="${a%%:*}"; dest="${a#*:}"; else src="$a"; dest="$a"; fi
    [ -z "$src" ] && block "git push '$a' (empty source) deletes the remote branch '$dest'"
    [ -z "$dest" ] && block "git push '$a' has an empty destination"
    if is_release_tag_ref "$src" || is_release_tag_ref "$dest"; then
      block "git push of release tag '$a' — $TAG_BLOCK_MSG"
    fi
    dest="${dest#refs/heads/}"
    [[ "$dest" =~ ^(main|master)$ ]] && block "git push to '$dest' — main is changed only by a merged PR"
    if [ "$src" = HEAD ] && [[ "$a" != *:* ]]; then check_head_policy; fi
  done
  return 0
}

check_git_branch() {
  local a flag=0 target=0
  for a in "$@"; do
    case "$a" in -D|-d|--delete|-f|--force|-M) flag=1 ;; main|master) target=1 ;; esac
  done
  [ "$flag" = 1 ] && [ "$target" = 1 ] && block "git branch delete/force on main or master"
  return 0
}

check_git() {
  local i=1 n=${#T[@]} sub="" a
  while [ "$i" -lt "$n" ]; do
    a="${T[$i]}"
    case "$a" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--super-prefix|--config-env) i=$((i + 2)); continue ;;
      -*) i=$((i + 1)); continue ;;
      *) sub="$a"; i=$((i + 1)); break ;;
    esac
  done
  case "$sub" in
    push) check_git_push "${T[@]:$i}" ;;
    branch) check_git_branch "${T[@]:$i}" ;;
  esac
  return 0
}

check_remote_host() { # ssh/scp/rsync/sftp/mosh/autossh: blocked outright, whatever the target
  block "'${T[0]}' — remote-shell commands are never run from this repo (it has nothing to deploy; if a host must be touched, a human does it)."
}

check_docker() {
  local j=" ${T[*]} " remote=0
  [[ "$j" =~ \ (-H|--host|--context)(=|\ ) ]] && remote=1
  [ "${SEG_REMOTE_DAEMON:-0}" = 1 ] && remote=1
  if [ "$remote" = 1 ] && [[ "$j" =~ \ compose\  ]] && [[ "$j" =~ \ (up|down|restart|stop|rm|kill|exec|run)(\ |$) ]]; then
    block "docker compose lifecycle against a REMOTE daemon"
  fi
  if [ "${T[1]:-}" = context ] && [ "${T[2]:-}" = use ] && [ "${T[3]:-default}" != default ]; then
    block "docker context use '${T[3]}' points later docker commands at a remote daemon"
  fi
  return 0
}

check_segment() { # $1 = 1 → ignore the read-only exemption
  local c="${T[0]}" j=" ${T[*]} " a odoo=0 upd=0
  if [ "$1" != 1 ] && [[ "$c" =~ $READONLY_RE ]]; then return 0; fi
  if [ "$1" = 1 ]; then
    # piped-shell rescan: the TEXT printed by echo/printf/cat is what the shell will run
    while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" =~ ^(echo|printf|cat)$ ]]; do
      shift_t; while [ "${#T[@]}" -gt 0 ] && [[ "${T[0]}" == -* ]]; do shift_t; done
    done
    strip_wrappers
    [ "${#T[@]}" -gt 0 ] || return 0
    c="${T[0]}"; j=" ${T[*]} "
  fi
  case "$c" in
    git) check_git ;;
    ssh|scp|rsync|sftp|mosh|autossh) check_remote_host ;;
    docker|docker-compose|podman) check_docker ;;
    make|gmake)
      for a in "${T[@]:1}"; do [[ "$a" =~ ^(deploy|push-firmware) ]] && block "make target '$a' ships to a device or host"; done ;;
    gh)
      [[ "$j" =~ \ workflow\ (run|enable|disable)\  ]] && block "gh workflow run/enable/disable triggers or changes CI/CD from an agent"
      [[ "${T[1]:-}" = api ]] && [[ "$j" =~ dispatches ]] && block "gh api ... dispatches triggers a workflow" ;;
  esac
  [[ "$c" =~ $DEPLOY_SCRIPT_RE ]] && block "running deploy script '$c'"
  [[ "$j" =~ \ prisma\ migrate\ (deploy|reset|resolve)(\ |$) ]] && block "prisma migrate $(printf '%s' "$j" | sed -E 's/.* prisma migrate ([a-z]+).*/\1/') — migrations run inside the deploy workflow only"
  [[ "$j" =~ \ prisma\ db\ (push|execute)(\ |$) ]] && block "prisma db push/execute changes a database schema outside a migration"
  for a in "${T[@]}"; do
    [[ "$a" =~ (^|/)(odoo-bin|odoo)$ ]] && odoo=1
    [[ "$a" =~ ^(-u|--update|-i|--init)(=|$) ]] && upd=1
  done
  [ "$odoo" = 1 ] && [ "$upd" = 1 ] && block "odoo-bin -u/-i upgrades modules on a live Odoo database"
  return 0
}

# ------------------------------------------------------------------ split + scan
# join backslash-newline continuations, then cut on shell separators and subshell openers/closers.
SPLIT="$(printf '%s\n' "$CMD" | tr '\r' '\n' | sed -e ':a' -e 'N' -e '$!ba' -e 's/\\\n/ /g' \
        | sed -E 's/&&|\|\||;|\||&|\$\(|`|\(|\)/\n/g')"

scan_all() { # $1 = ignore read-only exemption?
  local seg raw
  CD_DIR=""
  while IFS= read -r raw; do
    seg="$(printf '%s' "$raw" | tr -d "\"'\\\\" | tr '\t' ' ')"
    seg="${seg#"${seg%%[! ]*}"}"
    [ -n "$seg" ] || continue
    read -r -a T <<<"$seg" || true
    [ "${#T[@]}" -gt 0 ] || continue
    SEG_REMOTE_DAEMON=0
    local t
    for t in "${T[@]}"; do
      [[ "$t" =~ ^DOCKER_HOST=(ssh|tcp):// ]] && block "DOCKER_HOST=$(printf '%s' "$t" | cut -d= -f2) points docker at a remote daemon"
      [[ "$t" =~ ^DOCKER_HOST=. ]] && SEG_REMOTE_DAEMON=1
    done
    if [ "${T[0]}" = cd ]; then
      if [ "${#T[@]}" -ge 2 ] && [ "${T[1]}" != - ]; then
        case "${T[1]}" in /*) CD_DIR="${T[1]}" ;; '~'*) CD_DIR="$HOME${T[1]#\~}" ;; *) CD_DIR="${CD_DIR:-$CWD}/${T[1]}" ;; esac
      else
        CD_DIR=""
      fi
      continue
    fi
    strip_wrappers
    [ "${#T[@]}" -gt 0 ] || continue
    check_segment "$1"
  done <<<"$SPLIT"
  return 0
}

scan_all 0
# something is piped into a bare shell (`... | bash`): the text of every segment may be code,
# so re-scan with the read-only exemption switched off.
[ "$PIPED_SHELL" = 1 ] && scan_all 1
exit 0
