# Reviewed offline lab workflow. Source only after the C0 blob/hash gates in section 10.1.
lab_refuse() { print -r -- "{\"code\":\"$1\"}"; return 2; }
lab_ready() {
  [[ -x "${LAB_PY-}" ]] || { lab_refuse runbook:interpreter_unavailable; return 2; }
  [[ -f "${LAB_TOOL-}" && ! -L "$LAB_TOOL" && -f "${LAB_SHELL-}" && ! -L "$LAB_SHELL" ]] || {
    lab_refuse runbook:tool_missing; return 2
  }
  [[ "$(/usr/bin/shasum -a 256 "$LAB_TOOL")" = "$LAB_TOOL_SHA  $LAB_TOOL" &&
     "$(/usr/bin/shasum -a 256 "$LAB_SHELL")" = "$LAB_SHELL_SHA  $LAB_SHELL" ]] || {
    lab_refuse runbook:tool_hash_mismatch; return 2
  }
}
lab_tool() {
  lab_ready || return 2
  "$LAB_PY" -I -S -B "$LAB_TOOL" "$@"
}
lab_identity() { lab_tool identity "$LAB_CHECKOUT"; }
lab_identity_check() {
  lab_capture "$1-identity.json" lab_identity || return 2
  lab_capture "$1-identity-compare.txt" cmp "$LAB_EVIDENCE/$STEP1_PREFIX-identity.json" \
    "$LAB_EVIDENCE/$1-identity.json"
}
lab_save_session() {
  local label="$1"
  (
    print -r -- 'set -C -u'
    print -r -- 'export PATH=/usr/bin:/bin:/usr/sbin:/sbin'
    typeset -p LAB_CHECKOUT LAB_HOME LAB_PARENT LAB_DATA LAB_STATE LAB_CONTRACT LAB_EVIDENCE LAB_PY LAB_BOOTSTRAP
    typeset -p LAB_TOOL LAB_TOOL_SHA LAB_SHELL LAB_SHELL_SHA LAB_TOOL_BLOB LAB_SHELL_BLOB
    typeset -p C0 C0_TREE C1 C1_TREE BASIC_SHA L2_INDEX_SHA CONTRACT_SHA CERT_KEY ATTEMPT_ID RUN_ID
    typeset -p LAB_LOCK LAB_LEDGER_LOCK L2_SPACE_BUDGET_KIB C1_BUNDLE C1_BUNDLE_SHA
    typeset -p STEP1_PREFIX L1_PREFIX L2_PREFIX L3_PREFIX STEP5_FETCH_PREFIX
    typeset -p STEP5A_PREFIX STEP5B_PREFIX STEP6_PREFIX STEP7_PREFIX STEP8_PREFIX STEP9_PREFIX
    typeset -p STEP9_VERIFY_PREFIX STEP9_RECHECK_PREFIX STEP9_LEDGER_PREFIX STEP9_RUNNER_PREFIX
    typeset -p RETURN_PREFIX RECOVERY_CENSUS_PREFIX
    print -r -- '[[ -x "$LAB_PY" ]] || { print -r -- "runbook:interpreter_unavailable"; return 2; }'
    print -r -- '[[ -f "$LAB_SHELL" && ! -L "$LAB_SHELL" ]] || return 2'
    print -r -- '[[ "$(/usr/bin/shasum -a 256 "$LAB_SHELL")" = "$LAB_SHELL_SHA  $LAB_SHELL" ]] || return 2'
    print -r -- 'source "$LAB_SHELL" || return 2'
    print -r -- 'lab_ready || return 2'
    print -r -- 'if [[ "${LAB_PUBLICATION_ONLY-0}" != 1 ]]; then'
    print -r -- '  cd "$LAB_CHECKOUT" || { lab_refuse restore:cwd; return 2; }'
    print -r -- '  [[ "$(pwd -P)" = "$LAB_CHECKOUT" && -o noclobber && -o nounset ]] || return 2'
    print -r -- 'fi'
  ) > "$LAB_EVIDENCE/$label.zsh" || return 2
  /usr/bin/shasum -a 256 "$LAB_EVIDENCE/$label.zsh"
}
lab_restore_identity() {
  lab_ready || return 2
  lab_new_prefix "$RESTORE_PREFIX" restore || { lab_refuse restore:prefix_refused; return 2; }
  lab_capture "$RESTORE_PREFIX-identity.json" lab_identity || { lab_refuse restore:identity_refused; return 2; }
  lab_capture "$RESTORE_PREFIX-compare.txt" cmp "$LAB_EVIDENCE/$STEP1_PREFIX-identity.json" \
    "$LAB_EVIDENCE/$RESTORE_PREFIX-identity.json" || { lab_refuse restore:identity_drift; return 2; }
}
lab_publish() { lab_tool publish "$@"; }
lab_publish_attempt() {
  local -a options
  options=()
  if [[ "$1" = "$STEP1_PREFIX" ]]; then options+=(--bootstrap "$LAB_BOOTSTRAP"); fi
  if [[ "$1" = "$STEP9_PREFIX" ]]; then options+=(--recovery-run "$LAB_EVIDENCE/$STEP8_PREFIX-run.json"); fi
  lab_tool publish-attempt "$LAB_EVIDENCE" "$1" "$BASIC_SHA" "$LAB_LOCK" "${options[@]}"
}
lab_final_job_gate() {
  local prefix="$1" checkpoint receipt receipt_rc live suffix
  [[ "$prefix" =~ '^(l2|step6|step8)-[0-9]+$' ]] || return 0
  checkpoint="$LAB_EVIDENCE/session-$prefix-job.zsh"
  [[ -e "$checkpoint" || -L "$checkpoint" ]] || return 0
  [[ -f "$checkpoint" && ! -L "$checkpoint" ]] || {
    lab_refuse publication:job_checkpoint_refused; return 2
  }
  receipt="$LAB_EVIDENCE/$prefix-job.block.exit"
  if [[ -f "$receipt" && ! -L "$receipt" ]]; then
    receipt_rc="$(cat "$receipt")" || { lab_refuse publication:job_receipt_refused; return 2; }
    if [[ "$receipt_rc" =~ '^(0|[1-9][0-9]{0,2})$' ]] && (( receipt_rc <= 255 )); then return 0; fi
  fi
  # Check without consuming a label while a job is pending; capture a fresh observation only once it is gone.
  live="$(lab_liveness "$prefix-job")" || {
    lab_refuse publication:liveness_observation_refused; return 2
  }
  print -r -- "$live" | /usr/bin/grep -Fxq 'job_pgrep_exit=1 writer_pgrep_exit=1' || {
    lab_refuse publication:job_pending; return 2
  }
  for suffix in '' .stderr .exit; do
    [[ ! -e "$LAB_EVIDENCE/$prefix-final-liveness.txt$suffix" &&
       ! -L "$LAB_EVIDENCE/$prefix-final-liveness.txt$suffix" ]] || {
      lab_refuse publication:final_liveness_already_used; return 2
    }
  done
  lab_capture "$prefix-final-liveness.txt" lab_liveness "$prefix-job" || {
    lab_refuse publication:liveness_refused; return 2
  }
  /usr/bin/grep -Fxq 'job_pgrep_exit=1 writer_pgrep_exit=1' "$LAB_EVIDENCE/$prefix-final-liveness.txt" || {
    lab_refuse publication:liveness_changed; return 2
  }
}
lab_publication() {
  local prefix="$1" phase="$2" label bundle_rc digest suffix
  lab_ready || return 2
  [[ "$phase" = final || "$phase" = review ]] || { lab_refuse publication:phase; return 2; }
  if [[ "$phase" = review && ! "$prefix" =~ '^l2-[0-9]+$' ]]; then
    lab_refuse publication:review_prefix_refused; return 2
  fi
  label="$prefix-$phase-publication.jsonl"
  for suffix in '' .stderr .exit; do
    [[ ! -e "$LAB_EVIDENCE/$label$suffix" && ! -L "$LAB_EVIDENCE/$label$suffix" ]] || {
      lab_refuse publication:already_exists; return 2
    }
  done
  lab_final_job_gate "$prefix" || return 2
  if lab_capture "$label" lab_publish_attempt "$prefix"; then bundle_rc=0; else bundle_rc=$?; fi
  [[ -f "$LAB_EVIDENCE/$label" && ! -L "$LAB_EVIDENCE/$label" ]] || {
    lab_refuse publication:missing_bundle; return 2
  }
  cat "$LAB_EVIDENCE/$label" || return 2
  digest="$(/usr/bin/shasum -a 256 "$LAB_EVIDENCE/$label")" || return 2
  printf '{"publication_rc":%s,"bundle_sha256":"%s"}\n' "$bundle_rc" "${digest%% *}"
  return "$bundle_rc"
}
lab_finished_publication() {
  local prefix="$1" receipt code poll_rc bundle_rc
  [[ "$prefix" =~ '^(l2|step6|step8)-[0-9]+$' ]] || { lab_refuse publication:job_prefix_refused; return 2; }
  receipt="$LAB_EVIDENCE/$prefix-job.block.exit"
  [[ -f "$receipt" && ! -L "$receipt" ]] || { lab_refuse publication:job_pending; return 2; }
  code="$(cat "$receipt")" || return 2
  [[ "$code" =~ '^(0|[1-9][0-9]{0,2})$' ]] && (( code <= 255 )) || {
    lab_refuse publication:job_receipt_refused; return 2
  }
  if lab_poll "$prefix-job"; then poll_rc=0; else poll_rc=$?; fi
  if lab_publication "$prefix" final; then bundle_rc=0; else bundle_rc=$?; fi
  (( bundle_rc == 0 )) || return "$bundle_rc"
  return "$poll_rc"
}
lab_reprint() {
  local prefix="$1" phase="$2" expected="$3" filename code
  lab_ready || return 2
  [[ "$prefix" =~ '^[a-z0-9][a-z0-9-]*-[0-9]+$' && "$expected" =~ '^[0-9a-f]{64}$' ]] || return 2
  [[ "$phase" = final || "$phase" = review ]] || return 2
  filename="$LAB_EVIDENCE/$prefix-$phase-publication.jsonl"
  [[ -f "$filename" && ! -L "$filename" && -f "$filename.exit" && ! -L "$filename.exit" ]] || return 2
  [[ "$(/usr/bin/shasum -a 256 "$filename")" = "$expected  $filename" ]] || {
    lab_refuse publication:reprint_hash_mismatch; return 2
  }
  code="$(cat "$filename.exit")" || return 2
  [[ "$code" =~ '^(0|[1-9][0-9]{0,2})$' ]] && (( code <= 255 )) || return 2
  cat "$filename" || return 2
  printf '{"publication_rc":%s,"bundle_sha256":"%s"}\n' "$code" "$expected"
  return "$code"
}
lab_identifier_token() {
  local name="$1" value="$2" filename="$3" count
  count="$(/usr/bin/grep -c "^$name=" "$filename")" || return 2
  [[ "$count" = 1 ]] || return 2
  /usr/bin/grep -Fxq -- "$name=$value" "$filename"
}
lab_notice() {
  local prefix="$1" name
  shift
  [[ -f "$LAB_EVIDENCE/$prefix-notice.md" && ! -L "$LAB_EVIDENCE/$prefix-notice.md" ]] || {
    lab_refuse notice:missing; return 2
  }
  lab_capture "$prefix-notice-check.txt" lab_tool notice "$LAB_EVIDENCE/$prefix-notice.md" || return 2
  for name in "$@"; do
    lab_capture "$prefix-token-$name.txt" lab_identifier_token "$name" "${(P)name}" \
      "$LAB_EVIDENCE/$prefix-notice.md" || { lab_refuse notice:token_mismatch; return 2; }
  done
  lab_capture "$prefix-notice.sha" /usr/bin/shasum -a 256 "$LAB_EVIDENCE/$prefix-notice.md"
}
lab_init() {
  local name
  [[ -o noclobber && -o nounset && "${STEP1_PREFIX-}" =~ '^step1-[0-9]+$' &&
     "${LAB_CHECKOUT-}" = /* ]] || { lab_refuse step1:settings_refused; return 2; }
  [[ -n "${HOME-}" && -d "$HOME" ]] || { lab_refuse step1:home_refused; return 2; }
  LAB_HOME="$(cd "$HOME" && pwd -P)" || { lab_refuse step1:home_refused; return 2; }
  LAB_PARENT="$LAB_HOME/betangel-data"
  LAB_DATA="$LAB_PARENT/historic"
  LAB_STATE="$LAB_PARENT/lab-state"
  LAB_CONTRACT="$LAB_CHECKOUT/research/experiments/backtest-p0-bsp-v1.json"
  LAB_EVIDENCE="$LAB_HOME/betangel-runbook-evidence-$STEP1_PREFIX"
  LAB_BOOTSTRAP="$LAB_HOME/betangel-runbook-bootstrap-$STEP1_PREFIX"
  for name in C1 C1_TREE CONTRACT_SHA L2_INDEX_SHA CERT_KEY ATTEMPT_ID RUN_ID LAB_LOCK LAB_LEDGER_LOCK \
    L1_PREFIX L2_PREFIX L3_PREFIX STEP5A_PREFIX STEP5B_PREFIX STEP6_PREFIX STEP7_PREFIX STEP8_PREFIX \
    STEP9_PREFIX STEP9_VERIFY_PREFIX STEP9_RECHECK_PREFIX STEP9_LEDGER_PREFIX STEP9_RUNNER_PREFIX \
    RETURN_PREFIX RECOVERY_CENSUS_PREFIX L2_SPACE_BUDGET_KIB STEP5_FETCH_PREFIX C1_BUNDLE C1_BUNDLE_SHA; do
    typeset -g "$name="
  done
}
lab_bootstrap() {
  local bootstrap_rc source_tool="$LAB_TOOL" source_shell="$LAB_SHELL"
  lab_ready || return 2
  (
    lab_tool paths "$LAB_CHECKOUT" "$LAB_DATA" "$LAB_STATE" "$LAB_EVIDENCE" "$LAB_BOOTSTRAP" || exit 2
    lab_census "$LAB_EVIDENCE" || exit 2
    [[ ! -e "$LAB_EVIDENCE" && ! -L "$LAB_EVIDENCE" ]] || exit 2
    mkdir "$LAB_EVIDENCE" || exit 2
    realpath "$LAB_EVIDENCE" || exit 2
  ) > "$LAB_BOOTSTRAP.stdout" 2> "$LAB_BOOTSTRAP.stderr"
  bootstrap_rc=$?
  printf '%s\n' "$bootstrap_rc" > "$LAB_BOOTSTRAP.exit" || { lab_refuse step1:receipt_failed; return 2; }
  (( bootstrap_rc == 0 )) || { lab_refuse step1:bootstrap_failed; return 2; }
  LAB_TOOL="$LAB_EVIDENCE/$STEP1_PREFIX-lab_runbook.py"
  LAB_SHELL="$LAB_EVIDENCE/$STEP1_PREFIX-lab_runbook.zsh"
  cat "$source_tool" > "$LAB_TOOL" || { lab_refuse step1:tool_copy_failed; return 2; }
  cat "$source_shell" > "$LAB_SHELL" || { lab_refuse step1:shell_copy_failed; return 2; }
  lab_ready || return 2
  source "$LAB_SHELL" || { lab_refuse step1:helper_refused; return 2; }
  lab_capture "$STEP1_PREFIX-tool-sha.txt" printf '%s\n' "$LAB_TOOL_SHA" || {
    lab_refuse step1:tool_sha_failed; return 2
  }
  lab_capture "$STEP1_PREFIX-shell-sha.txt" printf '%s\n' "$LAB_SHELL_SHA" || {
    lab_refuse step1:shell_sha_failed; return 2
  }
  lab_capture "$STEP1_PREFIX-git.txt" lab_preflight "$C0" "$C0_TREE" || {
    lab_refuse step1:preflight_refused; return 2
  }
  lab_capture "$STEP1_PREFIX-worktrees.txt" git -C "$LAB_CHECKOUT" worktree list --porcelain || {
    lab_refuse step1:worktrees_refused; return 2
  }
  lab_capture "$STEP1_PREFIX-identity.json" lab_identity || { lab_refuse step1:identity_refused; return 2; }
  lab_capture "$STEP1_PREFIX-identity-gate.txt" lab_tool identity-gate \
    "$LAB_EVIDENCE/$STEP1_PREFIX-identity.json" || { lab_refuse step1:identity_gate_refused; return 2; }
  lab_checkpoint "$STEP1_PREFIX" || { lab_refuse step1:checkpoint_refused; return 2; }
}
lab_census() { lab_tool census "$1"; }

lab_capture() {
  local label="$1"
  lab_ready || return 2
  shift
  [[ "$label" =~ '^[a-zA-Z0-9][a-zA-Z0-9_.-]*$' ]] || { lab_refuse capture:label_refused; return 2; }
  local suffix rc
  for suffix in '' .stderr .exit; do
    [[ ! -e "$LAB_EVIDENCE/$label$suffix" && ! -L "$LAB_EVIDENCE/$label$suffix" ]] || {
      lab_refuse capture:already_exists; return 2
    }
  done
  if "$@" > "$LAB_EVIDENCE/$label" 2> "$LAB_EVIDENCE/$label.stderr"; then rc=0; else rc=$?; fi
  printf '%s\n' "$rc" > "$LAB_EVIDENCE/$label.exit" || return 2
  return "$rc"
}

lab_prefix_valid() {
  local value="$1" stem="$2"
  [[ "$value" =~ "^$stem-[0-9]+$" ]] || { lab_refuse prefix:invalid; return 2; }
}
lab_new_prefix() {
  lab_prefix_valid "$1" "$2" || return 2
  lab_tool new-prefix "$LAB_EVIDENCE" "$1"
}
lab_require_success() {
  lab_prefix_valid "$1" "$2" || return 2
  [[ -f "$LAB_EVIDENCE/session-$1.zsh" && ! -L "$LAB_EVIDENCE/session-$1.zsh" ]] || {
    lab_refuse checkpoint:required; return 2
  }
}
lab_checkpoint() {
  lab_capture "$1-session.sha" lab_save_session "session-$1" || return 2
  lab_publish checkpoint "$LAB_EVIDENCE/session-$1.zsh" "$LAB_LOCK" "session-$1.zsh"
}

lab_preflight() {
  local commit="$1" tree="$2" dirty caches
  [[ "$commit" =~ '^[0-9a-f]{40}$' && "$tree" =~ '^[0-9a-f]{40}$' ]] || return 2
  [[ "$(pwd -P)" = "$LAB_CHECKOUT" && -o noclobber && -o nounset ]] || return 2
  [[ "$(git -C "$LAB_CHECKOUT" rev-parse HEAD)" = "$commit" ]] || return 2
  [[ "$(git -C "$LAB_CHECKOUT" rev-parse 'HEAD^{tree}')" = "$tree" ]] || return 2
  dirty="$(git --no-optional-locks -C "$LAB_CHECKOUT" status --porcelain --ignored)" || return 2
  caches="$(find "$LAB_CHECKOUT" -name __pycache__ -print)" || return 2
  [[ -z "$dirty" && -z "$caches" ]] || return 2
  printf '%s tree %s\n' "$commit" "$tree"
}
lab_no_writer() {
  local label="$1" rc
  if lab_capture "$label" pgrep -fl -- '-m betangel_research[.]betfair[.](backtest|readiness)( |$)'; then
    return 2
  else
    rc=$?
  fi
  [[ "$rc" = 1 && ! -s "$LAB_EVIDENCE/$label" ]]
}
lab_lock_census() {
  local label="$1" target="$LAB_STATE/contracts/$CONTRACT_SHA"
  if [[ ! -e "$LAB_STATE/contracts" && ! -L "$LAB_STATE/contracts" ]]; then
    target="$LAB_STATE/contracts"
  fi
  lab_capture "$label-census.jsonl" lab_census "$target" || return 2
  lab_capture "$label-entries.json" lab_tool lock-entries "$LAB_STATE/contracts/$CONTRACT_SHA"
}
lab_lock_note() {
  local label="$1" lock_path="$2"
  [[ -f "$lock_path" && ! -L "$lock_path" ]] || { lab_refuse recovery:lock_not_regular; return 2; }
  lab_capture "${label}.stat" /usr/bin/stat -f '%HT %z %Sm' "$lock_path" || return 2
  [[ "$(/usr/bin/stat -f '%HT' "$lock_path")" = 'Regular File' ]] || return 2
  lab_capture "${label}.txt" cat "$lock_path" || return 2
}

lab_lock_match() {
  lab_capture "$1-check.txt" lab_tool lock-match "$2" "$RUN_ID" "$C1 tree $C1_TREE" && return 0
  if [[ -f "$LAB_EVIDENCE/$1-check.txt" && ! -L "$LAB_EVIDENCE/$1-check.txt" ]] &&
     /usr/bin/grep -Fxq 'lock_note_mismatch' "$LAB_EVIDENCE/$1-check.txt"; then
    if [[ "$2" = "$LAB_LEDGER_LOCK" ]]; then lab_refuse recovery:ledger_lock_note_mismatch
    else lab_refuse recovery:lock_note_mismatch; fi
  else
    lab_refuse recovery:lock_check_refused
  fi
  return 2
}

lab_no_locks() {
  lab_tool no-locks "$LAB_STATE" "$CONTRACT_SHA"
}
lab_inputs() {
  local label="$1" expected="$2" part
  for part in '' /manifests /index "/index/$BASIC_SHA"; do
    lab_capture "$label-census-${part//\//_}.jsonl" lab_census "$LAB_DATA$part" || return 2
  done
  lab_capture "$label-files.json" lab_tool inputs "$LAB_DATA" "$BASIC_SHA" "$expected"
}
lab_detach() {
  local prefix="$1" checkpoint="$2" job_function="$3" expected suffix child_pid
  [[ "$prefix" =~ '^[a-z0-9][a-z0-9_.-]*$' && "$job_function" =~ '^lab_[a-z0-9_]+_job$' ]] || return 2
  (( $+functions[$job_function] )) || return 2
  [[ -f "$checkpoint" && ! -L "$checkpoint" && "$checkpoint" = "$LAB_EVIDENCE/"* ]] || return 2
  [[ -x /usr/bin/nohup && -x /usr/bin/caffeinate ]] || return 2
  expected="$(/usr/bin/shasum -a 256 "$checkpoint")" || return 2
  expected="${expected%% *}"
  [[ "$expected" =~ '^[0-9a-f]{64}$' ]] || return 2
  for suffix in launch.stdout launch.stderr block.stdout block.stderr block.exit pid; do
    [[ ! -e "$LAB_EVIDENCE/$prefix.$suffix" && ! -L "$LAB_EVIDENCE/$prefix.$suffix" ]] || return 2
  done
  setopt LOCAL_OPTIONS LOCAL_TRAPS POSIX_TRAPS
  trap "" HUP
  /usr/bin/nohup /usr/bin/caffeinate -i /bin/zsh -f -c '
set -C -u
setopt POSIX_TRAPS
trap "" HUP
(
  [[ -f "$1" && ! -L "$1" ]] || exit 2
  [[ "$(/usr/bin/shasum -a 256 "$1")" = "$2  $1" ]] || exit 2
  LAB_PUBLICATION_ONLY=0
  source "$1" || exit 2
  (( $+functions[$3] )) || exit 2
  "$3"
) </dev/null > "$4.block.stdout" 2> "$4.block.stderr"
block_rc=$?
printf "%s\n" "$block_rc" > "$4.block.exit" || exit 2
exit "$block_rc"
' runbook-job "$checkpoint" "$expected" "$job_function" "$LAB_EVIDENCE/$prefix" \
    </dev/null > "$LAB_EVIDENCE/$prefix.launch.stdout" 2> "$LAB_EVIDENCE/$prefix.launch.stderr" &!
  child_pid=$!
  printf '%s\n' "$child_pid" > "$LAB_EVIDENCE/$prefix.pid" || return 2
  printf 'started %s pid %s; poll %s/%s.block.exit\n' "$prefix" "$child_pid" "$LAB_EVIDENCE" "$prefix"
}
lab_poll() {
  local receipt="$LAB_EVIDENCE/$1.block.exit" code
  [[ ! -L "$receipt" ]] || return 2
  if [[ ! -e "$receipt" ]]; then print -r -- "job=$1 pending; do not re-invoke"; return 3; fi
  [[ -f "$receipt" ]] || return 2
  code="$(cat "$receipt")" || return 2
  if [[ -z "$code" ]]; then print -r -- "job=$1 receipt not complete; do not re-invoke"; return 3; fi
  [[ "$code" =~ '^(0|[1-9][0-9]{0,2})$' ]] || return 2
  (( code <= 255 )) || return 2
  print -r -- "job=$1 block_exit=$code"
  return "$code"
}
lab_liveness() {
  local prefix="$1" pattern job_pids writer_pids job_rc writer_rc recorded
  [[ "$prefix" =~ '^(l2|step6|step8)-[0-9]+-job$' ]] || { lab_refuse liveness:prefix_refused; return 2; }
  if [[ -e "$LAB_EVIDENCE/$prefix.pid" || -L "$LAB_EVIDENCE/$prefix.pid" ]]; then
    [[ -f "$LAB_EVIDENCE/$prefix.pid" && ! -L "$LAB_EVIDENCE/$prefix.pid" ]] || {
      lab_refuse liveness:pid_refused; return 2
    }
    recorded="$(cat "$LAB_EVIDENCE/$prefix.pid")" || { lab_refuse liveness:pid_refused; return 2; }
    [[ "$recorded" =~ '^[0-9]+$' ]] || { lab_refuse liveness:pid_refused; return 2; }
    print -r -- "recorded_wrapper_pid=$recorded"
  else
    print -r -- 'recorded_wrapper_pid=absent'
  fi
  pattern="$(lab_tool job-pattern "$LAB_EVIDENCE/session-$prefix.zsh")" || {
    lab_refuse liveness:pattern_refused; return 2
  }
  job_pids="$(pgrep -d, -f "$pattern")"; job_rc=$?
  writer_pids="$(pgrep -d, -f -- '-m betangel_research[.]betfair[.](backtest|readiness)( |$)')"; writer_rc=$?
  (( (job_rc == 0 || job_rc == 1) && (writer_rc == 0 || writer_rc == 1) )) || {
    lab_refuse liveness:pgrep_refused; return 2
  }
  print -r -- "job_pgrep_exit=$job_rc writer_pgrep_exit=$writer_rc"
  if (( job_rc == 0 )); then
    [[ "$job_pids" =~ '^[0-9]+(,[0-9]+)*$' ]] || { lab_refuse liveness:job_pids_refused; return 2; }
    ps -o pid=,ppid=,stat=,etime=,comm= -p "$job_pids" || { lab_refuse liveness:ps_refused; return 2; }
  fi
  if (( writer_rc == 0 )); then
    [[ "$writer_pids" =~ '^[0-9]+(,[0-9]+)*$' ]] || { lab_refuse liveness:writer_pids_refused; return 2; }
    ps -o pid=,ppid=,stat=,etime=,comm= -p "$writer_pids" || { lab_refuse liveness:ps_refused; return 2; }
  fi
  [[ ! -L "$LAB_EVIDENCE/$prefix.block.stdout" ]] || { lab_refuse liveness:stdout_refused; return 2; }
  if [[ -f "$LAB_EVIDENCE/$prefix.block.stdout" ]]; then
    print -r -- 'block_stdout=present'
  else
    print -r -- 'block_stdout=missing'
  fi
}
lab_recovery_recheck() {
  local label="$1"
  lab_capture "$label-git.txt" lab_preflight "$C1" "$C1_TREE" || return 2
  lab_no_writer "$label-writers.txt" || { lab_refuse recovery:writer_present; return 2; }
  lab_capture "$label-liveness.txt" lab_liveness "$STEP8_PREFIX-job" || {
    lab_refuse recovery:liveness_refused; return 2
  }
  /usr/bin/grep -Fxq 'job_pgrep_exit=1 writer_pgrep_exit=1' "$LAB_EVIDENCE/$label-liveness.txt" || {
    lab_refuse recovery:job_or_writer_live; return 2
  }
  lab_lock_census "$label" || return 2
  lab_capture "$label-census-compare.txt" cmp "$LAB_EVIDENCE/$RECOVERY_CENSUS_PREFIX-census.jsonl" \
    "$LAB_EVIDENCE/$label-census.jsonl" || return 2
  lab_capture "$label-entries-compare.txt" cmp "$LAB_EVIDENCE/$RECOVERY_CENSUS_PREFIX-entries.json" \
    "$LAB_EVIDENCE/$label-entries.json" || return 2
  lab_lock_note "$label-runner-lock" "$LAB_LOCK" || return 2
  lab_lock_match "$label-runner-lock" "$LAB_LOCK" || return 2
  lab_capture "$label-runner-stat.txt" cmp "$LAB_EVIDENCE/$STEP9_PREFIX-runner-lock.stat" \
    "$LAB_EVIDENCE/$label-runner-lock.stat" || return 2
  lab_capture "$label-runner-note.txt" cmp "$LAB_EVIDENCE/$STEP9_PREFIX-runner-lock.txt" \
    "$LAB_EVIDENCE/$label-runner-lock.txt" || return 2
  if [[ -e "$LAB_LEDGER_LOCK" || -L "$LAB_LEDGER_LOCK" ]]; then
    lab_require_success "$STEP9_VERIFY_PREFIX" step9-verify || return 2
    lab_lock_note "$label-ledger-lock" "$LAB_LEDGER_LOCK" || return 2
    lab_lock_match "$label-ledger-lock" "$LAB_LEDGER_LOCK" || return 2
    lab_capture "$label-ledger-stat.txt" cmp "$LAB_EVIDENCE/$STEP9_VERIFY_PREFIX-ledger-lock.stat" \
      "$LAB_EVIDENCE/$label-ledger-lock.stat" || return 2
    lab_capture "$label-ledger-note.txt" cmp "$LAB_EVIDENCE/$STEP9_VERIFY_PREFIX-ledger-lock.txt" \
      "$LAB_EVIDENCE/$label-ledger-lock.txt" || return 2
  fi
}




lab_l2_job() {
(( $+functions[lab_inputs] )) || exit 2
lab_notice "$L2_PREFIX" C0 C0_TREE BASIC_SHA L2_INDEX_SHA L2_SPACE_BUDGET_KIB || exit 2
lab_capture "$L2_PREFIX-run-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_identity_check "$L2_PREFIX-run" || exit 2
lab_no_writer "$L2_PREFIX-run-writers.txt" || exit 2
lab_inputs "$L2_PREFIX-run-before" - || exit 2
lab_capture "$L2_PREFIX-run-input-compare.txt" cmp "$LAB_EVIDENCE/$L2_PREFIX-before-files.json" \
  "$LAB_EVIDENCE/$L2_PREFIX-run-before-files.json" || exit 2
lab_capture "$L2_PREFIX-job-space.txt" lab_tool space "$LAB_DATA" "$BASIC_SHA" \
  "$L2_SPACE_BUDGET_KIB" || exit 2
lab_capture "$L2_PREFIX-index.json" "$LAB_PY" -S -B -m betangel_research.betfair.backtest archive-index \
  --data-root "$LAB_DATA" --archive "$BASIC_SHA" || exit 2
lab_capture "$L2_PREFIX-state-check.txt" lab_tool l2-check "$LAB_EVIDENCE/$L2_PREFIX-index.json" "$BASIC_SHA"
[[ "$?" = 0 ]] || exit 2
lab_inputs "$L2_PREFIX-after" - || exit 2
lab_capture "$L2_PREFIX-input-compare.txt" cmp "$LAB_EVIDENCE/$L2_PREFIX-before-files.json" \
  "$LAB_EVIDENCE/$L2_PREFIX-after-files.json" || exit 2
lab_capture "$L2_PREFIX-index-sha.txt" lab_tool extract-digest \
  "$LAB_EVIDENCE/$L2_PREFIX-before-files.json" index || exit 2
L2_INDEX_SHA="$(<"$LAB_EVIDENCE/$L2_PREFIX-index-sha.txt")"
[[ "$L2_INDEX_SHA" =~ '^[0-9a-f]{64}$' ]] || exit 2
lab_checkpoint "$L2_PREFIX" || exit 2
}






lab_cert_job() {
lab_capture "$STEP6_PREFIX-git.txt" lab_preflight "$C1" "$C1_TREE" || exit 2
lab_identity_check "$STEP6_PREFIX" || exit 2
lab_no_writer "$STEP6_PREFIX-writers.txt" || exit 2
lab_capture "$STEP6_PREFIX-before.jsonl" lab_census "$LAB_STATE" || exit 2
lab_capture "$STEP6_PREFIX-empty.txt" lab_tool empty-state "$LAB_EVIDENCE/$STEP6_PREFIX-before.jsonl" || exit 2
lab_capture "$STEP6_PREFIX-certify.json" "$LAB_PY" -S -B -m betangel_research.betfair.backtest certify-engine \
  --lab-state-root "$LAB_STATE" --attempt-id "$ATTEMPT_ID" || exit 2
}




lab_run_binding() {
lab_capture "$STEP8_PREFIX-binding.json" lab_tool run-binding "$LAB_EVIDENCE" "$LAB_STATE" \
  "$LAB_CONTRACT" "$CONTRACT_SHA" "$RUN_ID" "$C1 tree $C1_TREE" "$1" \
  "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" "$LAB_EVIDENCE/$STEP6_PREFIX-certificate-check.json" \
  "$LAB_EVIDENCE/$STEP8_PREFIX-run.json"
}
lab_run_job() {
  local run_rc writer_rc inputs_rc compare_rc census_rc locks_rc classify_rc run_status
  lab_capture "$STEP8_PREFIX-git.txt" lab_preflight "$C1" "$C1_TREE" || exit 2
  [[ "$CONTRACT_SHA" =~ '^[0-9a-f]{64}$' && "$RUN_ID" =~ '^[a-z0-9][a-z0-9_.-]{0,40}$' ]] || exit 2
  lab_capture "$STEP8_PREFIX-identity.json" lab_identity || exit 2
  lab_capture "$STEP8_PREFIX-identity-compare.txt" cmp "$LAB_EVIDENCE/$STEP1_PREFIX-identity.json" \
    "$LAB_EVIDENCE/$STEP8_PREFIX-identity.json" || exit 2
  lab_no_writer "$STEP8_PREFIX-writers.txt" || exit 2
  lab_inputs "$STEP8_PREFIX-before" "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" || exit 2
  lab_capture "$STEP8_PREFIX-state.jsonl" lab_census "$LAB_STATE" || exit 2
  lab_capture "$STEP8_PREFIX-state-check.txt" lab_tool run-state "$LAB_STATE"
  [[ "$?" = 0 ]] || exit 2
  lab_capture "$STEP8_PREFIX-run.json" "$LAB_PY" -S -B -m betangel_research.betfair.backtest run-backtest \
    --contract "$LAB_CONTRACT" --data-root "$LAB_DATA" --lab-state-root "$LAB_STATE" \
    --run-id "$RUN_ID" --code-identity "$C1 tree $C1_TREE"; run_rc=$?
  # No early exit on run rc 4/2: collect every post-run input/lock check before deciding.
  lab_no_writer "$STEP8_PREFIX-after-writers.txt"; writer_rc=$?
  lab_inputs "$STEP8_PREFIX-after" "$LAB_EVIDENCE/$L3_PREFIX-inventory.json"; inputs_rc=$?
  lab_capture "$STEP8_PREFIX-input-compare.txt" cmp "$LAB_EVIDENCE/$STEP8_PREFIX-before-files.json" \
    "$LAB_EVIDENCE/$STEP8_PREFIX-after-files.json"; compare_rc=$?
  lab_lock_census "$STEP8_PREFIX-locks"; census_rc=$?
  lab_capture "$STEP8_PREFIX-no-locks.txt" lab_no_locks; locks_rc=$?
  lab_capture "$STEP8_PREFIX-status.txt" lab_tool run-status "$LAB_EVIDENCE/$STEP8_PREFIX-run.json"
  classify_rc=$?
  (( classify_rc == 0 )) || exit 2
  run_status="$(cat "$LAB_EVIDENCE/$STEP8_PREFIX-status.txt")" || exit 2
  case "$run_status" in
    completed)
      (( run_rc == 0 || run_rc == 4 )) || exit 2
      lab_run_binding "$run_rc" || exit 2 ;;
    failed|refused) (( run_rc == 2 )) || exit 2 ;;
    *) lab_refuse run:status_unresolved; exit 2 ;;
  esac
  (( writer_rc == 0 && inputs_rc == 0 && compare_rc == 0 && census_rc == 0 && locks_rc == 0 )) || {
    lab_refuse run:postcheck_refused; exit 2
  }
  if (( run_rc == 0 )); then lab_checkpoint "$STEP8_PREFIX" || exit 2; fi
  return "$run_rc"
}






lab_l1() {
local location root name
lab_require_success "$STEP1_PREFIX" step1 || exit 2
lab_new_prefix "$L1_PREFIX" l1 || exit 2
lab_checkpoint "$L1_PREFIX-pre" || exit 2
lab_capture "$L1_PREFIX-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_capture "$L1_PREFIX-parent.jsonl" lab_census "$LAB_PARENT" || exit 2
lab_capture "$L1_PREFIX-state.jsonl" lab_census "$LAB_STATE" || exit 2
lab_capture "$L1_PREFIX-data.jsonl" lab_census "$LAB_DATA" || exit 2
lab_capture "$L1_PREFIX-layout-check.txt" lab_tool l1-layout "$LAB_EVIDENCE/$L1_PREFIX-state.jsonl" \
  "$LAB_EVIDENCE/$L1_PREFIX-data.jsonl" "$LAB_EVIDENCE/$L1_PREFIX-parent.jsonl" || exit 2
for location in parent data; do
  if [[ "$location" = parent ]]; then root="$LAB_PARENT"; else root="$LAB_DATA"; fi
  for name in contracts runs certifications; do
    lab_capture "$L1_PREFIX-$location-$name.jsonl" lab_census "$root/$name" || exit 2
    [[ ! -e "$root/$name" && ! -L "$root/$name" ]] || exit 2
  done
done
lab_checkpoint "$L1_PREFIX" || exit 2
}

lab_l2_pre() {
lab_require_success "$L1_PREFIX" l1 || exit 2
lab_new_prefix "$L2_PREFIX" l2 || exit 2
lab_checkpoint "$L2_PREFIX-pre" || exit 2
lab_capture "$L2_PREFIX-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_identity_check "$L2_PREFIX" || exit 2
lab_no_writer "$L2_PREFIX-writers.txt" || exit 2
lab_inputs "$L2_PREFIX-before" - || exit 2
lab_capture "$L2_PREFIX-before-index-sha.txt" lab_tool extract-digest \
  "$LAB_EVIDENCE/$L2_PREFIX-before-files.json" index || exit 2
L2_INDEX_SHA="$(<"$LAB_EVIDENCE/$L2_PREFIX-before-index-sha.txt")"
[[ "$L2_INDEX_SHA" =~ '^[0-9a-f]{64}$' ]] || exit 2
lab_capture "$L2_PREFIX-space.txt" lab_tool space "$LAB_DATA" "$BASIC_SHA" || exit 2
lab_checkpoint "$L2_PREFIX-review" || exit 2
}

lab_l2_launch() {
local name
[[ -f "$LAB_EVIDENCE/session-$L2_PREFIX-review.zsh" && ! -L "$LAB_EVIDENCE/session-$L2_PREFIX-review.zsh" ]] || {
  lab_refuse l2:review_checkpoint_missing; exit 2
}
[[ "$L2_SPACE_BUDGET_KIB" =~ '^[1-9][0-9]{0,14}$' ]] || { lab_refuse space:budget_required; exit 2; }
[[ -f "$LAB_EVIDENCE/$L2_PREFIX-notice.md" && ! -L "$LAB_EVIDENCE/$L2_PREFIX-notice.md" ]] || {
  lab_refuse notice:missing; exit 2
}
lab_tool notice "$LAB_EVIDENCE/$L2_PREFIX-notice.md" || exit 2
for name in C0 C0_TREE BASIC_SHA L2_INDEX_SHA L2_SPACE_BUDGET_KIB; do
  lab_identifier_token "$name" "${(P)name}" "$LAB_EVIDENCE/$L2_PREFIX-notice.md" || {
    lab_refuse notice:token_mismatch; exit 2
  }
done
lab_tool space "$LAB_DATA" "$BASIC_SHA" "$L2_SPACE_BUDGET_KIB" || exit 2
lab_checkpoint "$L2_PREFIX-job" || exit 2
lab_capture "$L2_PREFIX-launch.txt" lab_detach "$L2_PREFIX-job" \
  "$LAB_EVIDENCE/session-$L2_PREFIX-job.zsh" lab_l2_job || exit 2
}

lab_l3_pre() {
lab_require_success "$L2_PREFIX" l2 || exit 2
[[ "$L2_INDEX_SHA" =~ '^[0-9a-f]{64}$' ]] || { lab_refuse l3:index_digest_refused; exit 2; }
lab_new_prefix "$L3_PREFIX" l3 || exit 2
lab_checkpoint "$L3_PREFIX-pre" || exit 2
}
lab_l3_run() {
lab_prefix_valid "$L3_PREFIX" l3 || exit 2
[[ -f "$LAB_EVIDENCE/session-$L3_PREFIX-pre.zsh" && ! -L "$LAB_EVIDENCE/session-$L3_PREFIX-pre.zsh" ]] || {
  lab_refuse l3:pre_checkpoint_missing; exit 2
}
lab_notice "$L3_PREFIX" C0 C0_TREE BASIC_SHA L2_INDEX_SHA || exit 2
lab_capture "$L3_PREFIX-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_identity_check "$L3_PREFIX" || exit 2
[[ "$L2_INDEX_SHA" =~ '^[0-9a-f]{64}$' ]] || { lab_refuse l3:index_digest_refused; exit 2; }
lab_no_writer "$L3_PREFIX-writers.txt" || exit 2
lab_capture "$L3_PREFIX-inventory.json" "$LAB_PY" -S -B -m betangel_research.betfair.readiness \
  --contract "$LAB_CONTRACT" --data-root "$LAB_DATA" \
  --expect-archive "$BASIC_SHA" --expect-index-sha256 "$L2_INDEX_SHA" \
  --code-identity "$C0 tree $C0_TREE" || exit 2
lab_capture "$L3_PREFIX-check.txt" lab_tool l3-check \
  "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" "$LAB_EVIDENCE/$L3_PREFIX-identity.json" "$C0 tree $C0_TREE"
[[ "$?" = 0 ]] || exit 2
lab_inputs "$L3_PREFIX-after" "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" || exit 2
lab_checkpoint "$L3_PREFIX" || exit 2
}

lab_root_pre() {
lab_require_success "$L1_PREFIX" l1 || exit 2
lab_require_success "$L3_PREFIX" l3 || exit 2
lab_new_prefix "$STEP5A_PREFIX" step5a || exit 2
lab_checkpoint "$STEP5A_PREFIX-pre" || exit 2
lab_capture "$STEP5A_PREFIX-parent.jsonl" lab_census "$LAB_PARENT" || exit 2
lab_capture "$STEP5A_PREFIX-parent-compare.txt" cmp "$LAB_EVIDENCE/$L1_PREFIX-parent.jsonl" \
  "$LAB_EVIDENCE/$STEP5A_PREFIX-parent.jsonl" || exit 2
lab_capture "$STEP5A_PREFIX-before.jsonl" lab_census "$LAB_STATE" || exit 2
lab_capture "$STEP5A_PREFIX-state-compare.txt" cmp "$LAB_EVIDENCE/$L1_PREFIX-state.jsonl" \
  "$LAB_EVIDENCE/$STEP5A_PREFIX-before.jsonl" || exit 2
}

lab_root_mkdir() {
lab_prefix_valid "$STEP5A_PREFIX" step5a || exit 2
lab_root_prechecked || exit 2
[[ ! -e "$LAB_STATE" && ! -L "$LAB_STATE" ]] || exit 2
lab_capture "$STEP5A_PREFIX-mkdir.txt" mkdir "$LAB_STATE" || exit 2
}

lab_root_prechecked() {
  local suffix receipt
  for suffix in parent-compare.txt state-compare.txt; do
    receipt="$LAB_EVIDENCE/$STEP5A_PREFIX-$suffix.exit"
    [[ -f "$receipt" && ! -L "$receipt" && "$(cat "$receipt")" = 0 ]] || {
      lab_refuse state:precheck_incomplete; return 2
    }
  done
}

lab_root_finish() {
lab_prefix_valid "$STEP5A_PREFIX" step5a || exit 2
lab_root_prechecked || exit 2
lab_capture "$STEP5A_PREFIX-realpath.txt" realpath "$LAB_STATE" || exit 2
[[ "$(<"$LAB_EVIDENCE/$STEP5A_PREFIX-realpath.txt")" = "$LAB_STATE" ]] || exit 2
lab_capture "$STEP5A_PREFIX-after.jsonl" lab_census "$LAB_STATE" || exit 2
lab_capture "$STEP5A_PREFIX-empty.txt" lab_tool empty-state "$LAB_EVIDENCE/$STEP5A_PREFIX-after.jsonl" || exit 2
lab_checkpoint "$STEP5A_PREFIX" || exit 2
}

lab_c1_transfer() {
lab_require_success "$STEP5A_PREFIX" step5a || exit 2
[[ "$C1" =~ '^[0-9a-f]{40}$' && "$C1_TREE" =~ '^[0-9a-f]{40}$' ]] || {
  lab_refuse transfer:c1_identity_refused; exit 2
}
[[ "$C1_BUNDLE" = /* && -f "$C1_BUNDLE" && ! -L "$C1_BUNDLE" &&
   "$C1_BUNDLE_SHA" =~ '^[0-9a-f]{64}$' ]] || { lab_refuse transfer:bundle_refused; exit 2; }
[[ "$(/usr/bin/shasum -a 256 "$C1_BUNDLE")" = "$C1_BUNDLE_SHA  $C1_BUNDLE" ]] || {
  lab_refuse transfer:bundle_sha_mismatch; exit 2
}
lab_new_prefix "$STEP5_FETCH_PREFIX" step5-fetch || exit 2
lab_checkpoint "$STEP5_FETCH_PREFIX-pre" || exit 2
lab_capture "$STEP5_FETCH_PREFIX-before-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_no_writer "$STEP5_FETCH_PREFIX-writers.txt" || exit 2
lab_capture "$STEP5_FETCH_PREFIX-bundle-check.txt" git -C "$LAB_CHECKOUT" bundle verify "$C1_BUNDLE" || exit 2
lab_capture "$STEP5_FETCH_PREFIX-bundle-sha.txt" printf '%s\n' "$C1_BUNDLE_SHA" || exit 2
lab_capture "$STEP5_FETCH_PREFIX-fetch.txt" git -C "$LAB_CHECKOUT" fetch "$C1_BUNDLE" "$C1" || exit 2
lab_capture "$STEP5_FETCH_PREFIX-tree.txt" git -C "$LAB_CHECKOUT" rev-parse "${C1}^{tree}" || exit 2
[[ "$(<"$LAB_EVIDENCE/$STEP5_FETCH_PREFIX-tree.txt")" = "$C1_TREE" ]] || exit 2
lab_capture "$STEP5_FETCH_PREFIX-after-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_checkpoint "$STEP5_FETCH_PREFIX" || exit 2
}

lab_return_c0() {
[[ "${RESTORE_FILE##*/}" = "session-$STEP5B_PREFIX-pre.zsh" ]] || {
  lab_refuse return:wrong_checkpoint; exit 2
}
lab_new_prefix "$RETURN_PREFIX" step5b-return || exit 2
lab_checkpoint "$RETURN_PREFIX-pre" || exit 2
lab_no_writer "$RETURN_PREFIX-writers.txt" || exit 2
lab_capture "$RETURN_PREFIX-before-git.txt" lab_preflight "$C1" "$C1_TREE" || exit 2
lab_capture "$RETURN_PREFIX-checkout.txt" git -C "$LAB_CHECKOUT" checkout --detach "$C0" || exit 2
lab_capture "$RETURN_PREFIX-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_checkpoint "$RETURN_PREFIX" || exit 2
}

lab_freeze_checkout() {
[[ "$C1" =~ '^[0-9a-f]{40}$' && "$C1_TREE" =~ '^[0-9a-f]{40}$' ]] || {
  lab_refuse freeze:c1_identity_refused; exit 2
}
lab_require_success "$STEP5_FETCH_PREFIX" step5-fetch || exit 2
lab_require_success "$STEP5A_PREFIX" step5a || exit 2
lab_require_success "$L3_PREFIX" l3 || exit 2
lab_new_prefix "$STEP5B_PREFIX" step5b || exit 2
lab_checkpoint "$STEP5B_PREFIX-attempt" || exit 2
lab_capture "$STEP5B_PREFIX-before-git.txt" lab_preflight "$C0" "$C0_TREE" || exit 2
lab_no_writer "$STEP5B_PREFIX-writers.txt" || exit 2
lab_capture "$STEP5B_PREFIX-tree.txt" git -C "$LAB_CHECKOUT" rev-parse "${C1}^{tree}" || exit 2
[[ "$(<"$LAB_EVIDENCE/$STEP5B_PREFIX-tree.txt")" = "$C1_TREE" ]] || exit 2
lab_capture "$STEP5B_PREFIX-paths.txt" git -C "$LAB_CHECKOUT" diff --name-only "$C0" "$C1" || exit 2
[[ "$(<"$LAB_EVIDENCE/$STEP5B_PREFIX-paths.txt")" = research/experiments/backtest-p0-bsp-v1.json ]] || exit 2
lab_capture "$STEP5B_PREFIX-diff.txt" git -C "$LAB_CHECKOUT" diff "$C0" "$C1" -- \
  research/experiments/backtest-p0-bsp-v1.json || exit 2
lab_capture "$STEP5B_PREFIX-c0-contract.json" git -C "$LAB_CHECKOUT" show \
  "${C0}:research/experiments/backtest-p0-bsp-v1.json" || exit 2
lab_capture "$STEP5B_PREFIX-c1-contract.json" git -C "$LAB_CHECKOUT" show \
  "${C1}:research/experiments/backtest-p0-bsp-v1.json" || exit 2
lab_capture "$STEP5B_PREFIX-settings.txt" lab_tool freeze-diff \
  "$LAB_EVIDENCE/$STEP5B_PREFIX-c0-contract.json" "$LAB_EVIDENCE/$STEP5B_PREFIX-c1-contract.json"
[[ "$?" = 0 ]] || exit 2
lab_checkpoint "$STEP5B_PREFIX-pre" || exit 2
lab_capture "$STEP5B_PREFIX-checkout.txt" git -C "$LAB_CHECKOUT" checkout --detach "$C1" || exit 2
lab_capture "$STEP5B_PREFIX-git.txt" lab_preflight "$C1" "$C1_TREE" || exit 2
lab_capture "$STEP5B_PREFIX-blob-compare.txt" cmp \
  "$LAB_EVIDENCE/$STEP5B_PREFIX-c1-contract.json" "$LAB_CONTRACT" || exit 2
lab_capture "$STEP5B_PREFIX-contract.json" "$LAB_PY" -S -B -m betangel_research.betfair.backtest validate-contract \
  --contract "$LAB_CONTRACT" --require-frozen || exit 2
lab_capture "$STEP5B_PREFIX-contract-sha.txt" lab_tool extract-digest \
  "$LAB_EVIDENCE/$STEP5B_PREFIX-contract.json" contract || exit 2
CONTRACT_SHA="$(<"$LAB_EVIDENCE/$STEP5B_PREFIX-contract-sha.txt")"
[[ "$CONTRACT_SHA" =~ '^[0-9a-f]{64}$' ]] || exit 2
lab_identity_check "$STEP5B_PREFIX" || exit 2
lab_capture "$STEP5B_PREFIX-cert-key.txt" lab_tool extract-digest \
  "$LAB_EVIDENCE/$STEP5B_PREFIX-identity.json" certificate-key || exit 2
CERT_KEY="$(<"$LAB_EVIDENCE/$STEP5B_PREFIX-cert-key.txt")"
[[ "$CERT_KEY" =~ '^[0-9a-f]{64}$' ]] || exit 2
LAB_LOCK="$LAB_STATE/contracts/$CONTRACT_SHA/runner.lock"
LAB_LEDGER_LOCK="$LAB_STATE/contracts/$CONTRACT_SHA/trial-ledger-v1.jsonl.lock"
lab_checkpoint "$STEP5B_PREFIX" || exit 2
}

lab_cert_launch() {
lab_require_success "$STEP5B_PREFIX" step5b || exit 2
[[ "$ATTEMPT_ID" =~ '^[a-z0-9][a-z0-9_.-]{0,40}$' ]] || { lab_refuse step6:attempt_id_refused; exit 2; }
lab_new_prefix "$STEP6_PREFIX" step6 || exit 2
lab_checkpoint "$STEP6_PREFIX-pre" || exit 2
lab_checkpoint "$STEP6_PREFIX-job" || exit 2
lab_capture "$STEP6_PREFIX-launch.txt" lab_detach "$STEP6_PREFIX-job" \
  "$LAB_EVIDENCE/session-$STEP6_PREFIX-job.zsh" lab_cert_job || exit 2
}

lab_cert_finish() {
lab_poll "$STEP6_PREFIX-job" || exit 2
lab_capture "$STEP6_PREFIX-certifications.jsonl" lab_census "$LAB_STATE/certifications" || exit 2
lab_capture "$STEP6_PREFIX-certificate-check.json" lab_tool certificate-check \
  "$LAB_STATE" "$CERT_KEY" "$ATTEMPT_ID" "$LAB_EVIDENCE/$STEP6_PREFIX-certify.json" \
  "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" "$LAB_EVIDENCE/$STEP1_PREFIX-identity.json"
[[ "$?" = 0 ]] || exit 2
lab_checkpoint "$STEP6_PREFIX" || exit 2
}

lab_consent_pre() {
lab_require_success "$STEP6_PREFIX" step6 || exit 2
[[ "$RUN_ID" =~ '^[a-z0-9][a-z0-9_.-]{0,40}$' ]] || { lab_refuse step7:run_id_refused; exit 2; }
lab_new_prefix "$STEP7_PREFIX" step7 || exit 2
lab_checkpoint "$STEP7_PREFIX-pre" || exit 2
}

lab_consent_finish() {
local name
lab_prefix_valid "$STEP7_PREFIX" step7 || exit 2
[[ -f "$LAB_EVIDENCE/$STEP7_PREFIX-notice-and-consent.md" && \
   ! -L "$LAB_EVIDENCE/$STEP7_PREFIX-notice-and-consent.md" ]] || { lab_refuse consent:missing; exit 2; }
[[ "$C1" =~ '^[0-9a-f]{40}$' && "$C1_TREE" =~ '^[0-9a-f]{40}$' && \
   "$CONTRACT_SHA" =~ '^[0-9a-f]{64}$' && "$CERT_KEY" =~ '^[0-9a-f]{64}$' ]] || {
  lab_refuse consent:identifiers_unset; exit 2
}
lab_capture "$STEP7_PREFIX-notice-check.txt" lab_tool consent \
  "$LAB_EVIDENCE/$STEP7_PREFIX-notice-and-consent.md" "$LAB_EVIDENCE/$L3_PREFIX-inventory.json" \
  "$LAB_EVIDENCE/$STEP6_PREFIX-certificate-check.json" || exit 2
for name in RUN_ID C1 C1_TREE CONTRACT_SHA CERT_KEY; do
  lab_capture "$STEP7_PREFIX-token-$name.txt" lab_identifier_token "$name" "${(P)name}" \
    "$LAB_EVIDENCE/$STEP7_PREFIX-notice-and-consent.md" || { lab_refuse consent:token_mismatch; exit 2; }
done
lab_capture "$STEP7_PREFIX-consent.sha" /usr/bin/shasum -a 256 \
  "$LAB_EVIDENCE/$STEP7_PREFIX-notice-and-consent.md" || exit 2
lab_checkpoint "$STEP7_PREFIX" || exit 2
}

lab_run_launch() {
lab_require_success "$STEP7_PREFIX" step7 || exit 2
lab_require_success "$STEP6_PREFIX" step6 || exit 2
lab_new_prefix "$STEP8_PREFIX" step8 || exit 2
lab_checkpoint "$STEP8_PREFIX-pre" || exit 2
lab_checkpoint "$STEP8_PREFIX-job" || exit 2
lab_capture "$STEP8_PREFIX-launch.txt" lab_detach "$STEP8_PREFIX-job" \
  "$LAB_EVIDENCE/session-$STEP8_PREFIX-job.zsh" lab_run_job || exit 2
}

lab_recovery_begin() {
[[ "${RESTORE_FILE##*/}" = "session-$STEP8_PREFIX-job.zsh" ]] || {
  lab_refuse recovery:wrong_checkpoint; exit 2
}
lab_tool recovery-attempt "$LAB_EVIDENCE" "$STEP8_PREFIX" || exit 2
lab_new_prefix "$STEP9_PREFIX" step9 || exit 2
lab_checkpoint "$STEP9_PREFIX-pre" || exit 2
[[ -f "$LAB_EVIDENCE/session-$STEP8_PREFIX-job.zsh" ]] || exit 2
[[ "$CONTRACT_SHA" =~ '^[0-9a-f]{64}$' ]] || exit 2
LAB_LOCK="$LAB_STATE/contracts/$CONTRACT_SHA/runner.lock"
LAB_LEDGER_LOCK="$LAB_STATE/contracts/$CONTRACT_SHA/trial-ledger-v1.jsonl.lock"
lab_capture "$STEP9_PREFIX-git.txt" lab_preflight "$C1" "$C1_TREE" || exit 2
lab_identity_check "$STEP9_PREFIX" || exit 2
lab_no_writer "$STEP9_PREFIX-writers.txt" || { lab_refuse recovery:writer_present; exit 2; }
lab_capture "$STEP9_PREFIX-liveness.txt" lab_liveness "$STEP8_PREFIX-job" || {
  lab_refuse recovery:liveness_refused; exit 2
}
/usr/bin/grep -Fxq 'job_pgrep_exit=1 writer_pgrep_exit=1' "$LAB_EVIDENCE/$STEP9_PREFIX-liveness.txt" || {
  lab_refuse recovery:job_or_writer_live; exit 2
}
lab_lock_census "$STEP9_PREFIX-before" || exit 2
[[ -e "$LAB_LOCK" || -L "$LAB_LOCK" ]] || { lab_refuse recovery:runner_lock_absent; exit 2; }
lab_lock_note "$STEP9_PREFIX-runner-lock" "$LAB_LOCK" || exit 2
lab_lock_match "$STEP9_PREFIX-runner-lock" "$LAB_LOCK" || exit 2
lab_capture "$STEP9_PREFIX-recovery-check.json" lab_publish recovery \
  "$LAB_EVIDENCE/$STEP8_PREFIX-run.json" "$LAB_LOCK" "$STEP8_PREFIX-run.json" || {
    lab_refuse recovery:run_projection_refused; exit 2
  }
RECOVERY_CENSUS_PREFIX="$STEP9_PREFIX-before"
lab_checkpoint "$STEP9_PREFIX" || exit 2
}

lab_ledger_verify() {
lab_require_success "$STEP9_PREFIX" step9 || exit 2
lab_new_prefix "$STEP9_VERIFY_PREFIX" step9-verify || exit 2
lab_checkpoint "$STEP9_VERIFY_PREFIX-pre" || exit 2
lab_lock_note "$STEP9_VERIFY_PREFIX-ledger-lock" "$LAB_LEDGER_LOCK" || exit 2
lab_lock_match "$STEP9_VERIFY_PREFIX-ledger-lock" "$LAB_LEDGER_LOCK" || exit 2
lab_capture "$STEP9_VERIFY_PREFIX-ledger-verify.json" lab_tool ledger-verify "$LAB_CHECKOUT" \
  "$LAB_STATE/contracts/$CONTRACT_SHA/trial-ledger-v1.jsonl"
[[ "$?" = 0 ]] || exit 2
lab_checkpoint "$STEP9_VERIFY_PREFIX" || exit 2
}

lab_recovery_review() {
lab_require_success "$STEP9_PREFIX" step9 || exit 2
if [[ -e "$LAB_LEDGER_LOCK" || -L "$LAB_LEDGER_LOCK" ]]; then
  [[ "$STEP9_VERIFY_PREFIX" =~ '^step9-verify-[0-9]+$' &&
     -f "$LAB_EVIDENCE/session-$STEP9_VERIFY_PREFIX.zsh" &&
     ! -L "$LAB_EVIDENCE/session-$STEP9_VERIFY_PREFIX.zsh" ]] || {
    lab_refuse recovery:verify_required; exit 2
  }
fi
lab_new_prefix "$STEP9_RECHECK_PREFIX" step9-recheck || exit 2
lab_checkpoint "$STEP9_RECHECK_PREFIX-pre" || exit 2
lab_recovery_recheck "$STEP9_RECHECK_PREFIX" || exit 2
lab_checkpoint "$STEP9_RECHECK_PREFIX" || exit 2
}

lab_ledger_remove() {
lab_require_success "$STEP9_RECHECK_PREFIX" step9-recheck || exit 2
lab_require_success "$STEP9_VERIFY_PREFIX" step9-verify || exit 2
lab_new_prefix "$STEP9_LEDGER_PREFIX" step9-ledger || exit 2
lab_checkpoint "$STEP9_LEDGER_PREFIX-pre" || exit 2
lab_recovery_recheck "$STEP9_LEDGER_PREFIX-recheck" || exit 2
lab_capture "$STEP9_LEDGER_PREFIX-rm-ledger-lock.txt" rm "$LAB_LEDGER_LOCK" || exit 2
lab_lock_census "$STEP9_LEDGER_PREFIX-after" || exit 2
RECOVERY_CENSUS_PREFIX="$STEP9_LEDGER_PREFIX-after"
lab_checkpoint "$STEP9_LEDGER_PREFIX" || exit 2
}

lab_runner_remove() {
lab_require_success "$STEP9_RECHECK_PREFIX" step9-recheck || exit 2
lab_new_prefix "$STEP9_RUNNER_PREFIX" step9-runner || exit 2
lab_checkpoint "$STEP9_RUNNER_PREFIX-pre" || exit 2
lab_recovery_recheck "$STEP9_RUNNER_PREFIX-recheck" || exit 2
[[ ! -e "$LAB_LEDGER_LOCK" && ! -L "$LAB_LEDGER_LOCK" ]] || exit 2
lab_capture "$STEP9_RUNNER_PREFIX-rm-runner-lock.txt" rm "$LAB_LOCK" || exit 2
lab_capture "$STEP9_RUNNER_PREFIX-reconcile.json" "$LAB_PY" -S -B -m betangel_research.betfair.backtest \
  reconcile-trials \
  --contract "$LAB_CONTRACT" --lab-state-root "$LAB_STATE" --code-identity "$C1 tree $C1_TREE"
reconcile_rc=$?
lab_lock_census "$STEP9_RUNNER_PREFIX-after" || exit 2
lab_capture "$STEP9_RUNNER_PREFIX-no-locks.txt" lab_no_locks || exit 2
(( reconcile_rc == 0 )) || exit 2
lab_capture "$STEP9_RUNNER_PREFIX-reconciled.txt" lab_tool reconciled \
  "$LAB_EVIDENCE/$STEP9_RUNNER_PREFIX-reconcile.json" || exit 2
lab_checkpoint "$STEP9_RUNNER_PREFIX" || exit 2
}
