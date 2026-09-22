#!/bin/bash
# codex-safe.sh — strip credentials and bound the run before invoking an
# external agent CLI.
#
# Usage: codex-safe.sh exec -m <model> -s read-only -o out.md "prompt"
#
# Three guards, all enforced HERE rather than at each call site, because every
# call site that had to remember one of them eventually forgot.
#
#   1. CREDENTIAL STRIPPING. A hardcoded unset-list silently rots as new
#      secrets are added to your .env, so this strips by NAME PATTERN first and
#      then unsets known-name exceptions that don't match (e.g. DATABASE_URL).
#
#   2. STDIN CLOSED (`</dev/null`) when stdin is a non-TTY pipe. `codex exec`
#      reads instructions from stdin when stdin is piped and appends them to
#      any positional prompt. Inside a coding-agent subagent the shell's stdin
#      is an open pipe that never reaches EOF, so codex blocks forever waiting
#      for input that never arrives — observed in production as a ~25-minute
#      hang producing 0 bytes, which silently forced the fallback reviewer in
#      every review loop. Redirecting from /dev/null gives an immediate EOF.
#      An interactive TTY is left alone, and `CODEX_SAFE_STDIN=inherit` opts
#      back into real stdin for a caller that genuinely pipes input.
#
#   3. WALL-CLOCK TIMEOUT. macOS ships no `timeout`/`gtimeout`, so a `timeout`
#      written into an agent definition is INERT and a hang is never actually
#      bounded. perl's alarm is always present; we kill the whole process group
#      so a wedged child can't outlive the parent.
#
# Exit codes follow shell convention so a caller can tell outcomes apart:
#   124        timed out (GNU timeout's convention)
#   128+signal codex died from a signal — NOT reported as success
#   otherwise  codex's own exit code
#
# Documented in review-loops.md §4; keep the two in sync.
set -euo pipefail

# Override with CODEX_TIMEOUT_SECS for a long review; 0 disables the bound.
CODEX_TIMEOUT_SECS="${CODEX_TIMEOUT_SECS:-900}"

# Any var whose name looks credential-bearing.
PATTERN='(SECRET|TOKEN|API_?KEY|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|ACCESS_KEY|AUTH)'

# Explicit names that carry secrets but don't match the pattern above.
EXTRA=(
  DATABASE_URL TEST_DATABASE_URL REDIS_URL
  AUTH_GOOGLE_ID AWS_SESSION_TOKEN
  CODEX_SAFE_SELF
)

strip_env() {
  local unset_args=()
  local name
  while IFS='=' read -r name _; do
    [[ "$name" =~ $PATTERN ]] && unset_args+=(-u "$name")
  done < <(env)

  for name in "${EXTRA[@]}"; do
    unset_args+=(-u "$name")
  done

  env "${unset_args[@]}" codex "$@"
}

run_codex() {
  # The DEFAULT is to discard stdin, because the common case — an agent passing
  # the prompt positionally from a shell whose stdin is a pipe that never EOFs —
  # otherwise hangs forever, and a hang is far worse than a discarded pipe.
  if [[ "${CODEX_SAFE_STDIN:-}" == "inherit" ]] || [[ -t 0 ]]; then
    strip_env "$@"
  else
    strip_env "$@" </dev/null
  fi
}

if [[ "$CODEX_TIMEOUT_SECS" == "0" ]]; then
  run_codex "$@"          # documented, explicit opt-out of the bound
  exit $?
fi

# The perl guard below re-execs THIS script for the child, which must run codex
# directly rather than nesting a second timeout. The handoff is a leading ARGV
# sentinel, not an env var: the caller's environment cannot forge an argument,
# so this cannot be spoofed into silently skipping the bound.
# `CODEX_TIMEOUT_SECS=0` above remains the one supported way to opt out.
readonly CODEX_SAFE_SENTINEL="--codex-safe-internal-child"
if [[ "${1:-}" == "$CODEX_SAFE_SENTINEL" ]]; then
  shift
  run_codex "$@"
  exit $?
fi

# `setsid`-free process-group kill: perl forks, the child becomes its own group
# leader via POSIX::setpgid, and on alarm we signal the negated pgid so codex and
# anything it spawned all die. Without the group kill, killing perl's immediate
# child can leave codex running and holding the terminal.
export CODEX_SAFE_SELF="$0"
perl -e '
  use POSIX qw(setpgid);
  my $secs = shift @ARGV;
  my $pid = fork();
  die "fork failed: $!" unless defined $pid;
  if ($pid == 0) {
    setpgid(0, 0);
    exec($ENV{CODEX_SAFE_SELF}, @ARGV) or die "exec failed: $!";
  }
  setpgid($pid, $pid);

  my $reap = sub {
    my ($sig) = @_;
    kill("TERM", -$pid);
    sleep 2;
    kill("KILL", -$pid);
    return $sig;
  };

  local $SIG{ALRM} = sub {
    $reap->("ALRM");
    print STDERR "codex-safe: TIMED OUT after ${secs}s\n";
    exit 124;
  };
  # Forward interactive/terminating signals to the whole group, else killing this
  # wrapper leaves codex running detached in its own process group.
  for my $sig (qw(INT TERM HUP)) {
    $SIG{$sig} = sub {
      $reap->($sig);
      my %num = (INT => 2, TERM => 15, HUP => 1);
      exit(128 + $num{$sig});
    };
  }

  alarm($secs);
  waitpid($pid, 0);
  my $status = $?;
  alarm(0);

  # A signalled death must NOT look like success: `$? >> 8` is 0 for signal
  # exits, so report 128+signal the way a shell does.
  my $signal = $status & 127;
  exit($signal ? 128 + $signal : $status >> 8);
' "$CODEX_TIMEOUT_SECS" "$CODEX_SAFE_SENTINEL" "$@"
