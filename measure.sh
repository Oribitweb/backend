#!/usr/bin/env bash
#
# measure.sh - Measure real CPU / RAM usage of Docker containers during a test
#              and suggest the server size needed to run them.
#
# How it works:
#   - Samples every container once per INTERVAL seconds from its cgroup v2
#     files (cpu.stat, memory.current, memory.stat, memory.events).
#   - CPU usage is computed from the delta of usage_usec between samples,
#     so you get avg / p95 / peak in "cores" (1.0 = one full core).
#   - RAM is tracked as "anon" (real process memory) and total cgroup memory
#     (anon + reclaimable page cache).
#   - Throttling and OOM kills are counted as deltas over the test window.
#   - At the end it prints a per-container report and a sizing suggestion.
#
# Usage:
#   ./measure.sh start     # begin sampling in the background
#   ...run your load test (Locust, video uploads, ...)...
#   ./measure.sh stop      # stop sampling and print the report
#   ./measure.sh report    # re-print the report for the last run
#
# Options (environment variables):
#   CONTAINERS  containers to measure       (default: "backend celery_worker")
#   INTERVAL    seconds between samples     (default: 1)
#   HEADROOM    safety multiplier for sizing (default: 1.3 = +30%)
#   OUT_DIR     where samples are stored    (default: .measure)
#
# Example:
#   CONTAINERS="backend celery_worker" HEADROOM=1.25 ./measure.sh start
#
# Requirements: Linux host, Docker with cgroup v2, GNU date, awk, sort.

set -euo pipefail
export LC_ALL=C

read -r -a CONTAINERS <<< "${CONTAINERS:-backend celery_worker}"
INTERVAL="${INTERVAL:-1}"
HEADROOM="${HEADROOM:-1.3}"
OUT_DIR="${OUT_DIR:-.measure}"
CSV="$OUT_DIR/samples.csv"
PIDS="$OUT_DIR/sampler.pids"
CG=/sys/fs/cgroup

# CSV columns:
#  1 timestamp  2 container  3 usage_usec  4 nr_periods  5 nr_throttled
#  6 throttled_usec  7 mem_current  8 anon  9 file  10 oom_kill  11 max_hits

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

# --------------------------------------------------------------------------
# Sampler: runs in the background, one process per container.
# --------------------------------------------------------------------------
sampler() {
  local c="$1" ts raw
  while true; do
    ts=$(date +%s.%N)
    # One docker exec per sample: dump all needed cgroup files.
    if raw=$(docker exec "$c" sh -c \
        "echo \"mem_current \$(cat $CG/memory.current)\"; cat $CG/cpu.stat $CG/memory.stat $CG/memory.events" \
        2>/dev/null); then
      echo "$raw" | awk -v ts="$ts" -v c="$c" '
        { v[$1] = $2 }
        END {
          printf "%s,%s,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f\n", ts, c,
            v["usage_usec"], v["nr_periods"], v["nr_throttled"], v["throttled_usec"],
            v["mem_current"], v["anon"], v["file"], v["oom_kill"], v["max"]
        }' >> "$CSV"
    fi
    sleep "$INTERVAL"
  done
}

# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------
cmd_start() {
  if [[ -f "$PIDS" ]]; then
    echo "A measurement is already running. Run '$0 stop' first." >&2
    exit 1
  fi

  # Validate all containers before starting anything.
  for c in "${CONTAINERS[@]}"; do
    docker inspect "$c" >/dev/null 2>&1 \
      || { echo "Container '$c' not found." >&2; exit 1; }
    docker exec "$c" test -r "$CG/memory.current" \
      || { echo "Container '$c' is not running or cgroup v2 files are not readable." >&2; exit 1; }
  done

  mkdir -p "$OUT_DIR"
  : > "$CSV"
  : > "$PIDS"

  for c in "${CONTAINERS[@]}"; do
    # Try to reset the cgroup peak counter (kernel >= 6.0; often read-only
    # inside containers). Sampled peaks below are per-run either way.
    if docker exec "$c" sh -c "echo 0 > $CG/memory.peak" 2>/dev/null; then
      echo "[$c] memory.peak reset"
    else
      echo "[$c] memory.peak could not be reset (cgroup value is since container start; sampled peaks are still per-run)"
    fi
    nohup "$0" _sampler "$c" >/dev/null 2>&1 &
    echo $! >> "$PIDS"
  done

  echo "Sampling every ${INTERVAL}s: ${CONTAINERS[*]}"
  echo "Run your test now, then: $0 stop"
}

# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------
cmd_stop() {
  if [[ ! -f "$PIDS" ]]; then
    echo "No measurement is running. Start one with '$0 start'." >&2
    exit 1
  fi
  while read -r pid; do
    pkill -P "$pid" 2>/dev/null || true   # the sleep child
    kill "$pid" 2>/dev/null || true
  done < "$PIDS"
  rm -f "$PIDS"
  cmd_report
}

# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
TOTAL_CORES=0
TOTAL_MEM_MIB=0

mib() { awk -v b="$1" 'BEGIN { printf "%.0f", b / 1048576 }'; }

report_container() {
  local c="$1" data rates
  local samples duration cpu_avg cpu_max periods throttled throttled_sec
  local mem_max_cur anon_max anon_avg oom mem_limit_hits
  local quota period limit_cores mem_limit peak_cg cpu_p95 thr_pct near_cap
  local rec_cores rec_mem

  data=$(awk -F, -v c="$c" '$2 == c' "$CSV" | sort -t, -k1,1n)
  if [[ -z "$data" ]]; then
    printf '\n[%s] no samples collected.\n' "$c"
    return
  fi

  rates=$(mktemp)
  eval "$(echo "$data" | awk -F, -v rates="$rates" '
    NR == 1 { t0=$1; u0=$3; p0=$4; n0=$5; th0=$6; o0=$10; m0=$11 }
    {
      if (NR > 1) {
        dt = $1 - pt
        if (dt > 0) { r = ($3 - pu) / dt / 1e6; print r > rates; if (r > maxr) maxr = r }
      }
      pt = $1; pu = $3
      if ($7 > maxcur)  maxcur  = $7
      if ($8 > maxanon) maxanon = $8
      sumanon += $8
      t1=$1; u1=$3; p1=$4; n1=$5; th1=$6; o1=$10; m1=$11
    }
    END {
      avg = (t1 > t0) ? (u1 - u0) / (t1 - t0) / 1e6 : 0
      printf "samples=%d\n", NR
      printf "duration=%.1f\n", t1 - t0
      printf "cpu_avg=%.3f\ncpu_max=%.3f\n", avg, maxr
      printf "periods=%.0f\nthrottled=%.0f\nthrottled_sec=%.2f\n", p1 - p0, n1 - n0, (th1 - th0) / 1e6
      printf "mem_max_cur=%.0f\nanon_max=%.0f\nanon_avg=%.0f\n", maxcur, maxanon, sumanon / NR
      printf "oom=%.0f\nmem_limit_hits=%.0f\n", o1 - o0, m1 - m0
    }')"

  if [[ "$samples" -lt 2 ]]; then
    printf '\n[%s] not enough samples (%s). Run the test for longer.\n' "$c" "$samples"
    rm -f "$rates"
    return
  fi

  cpu_p95=$(sort -n "$rates" | awk '{ a[NR] = $1 } END { i = int(NR * 0.95); if (i < 1) i = 1; printf "%.3f", a[i] }')
  # Current limits and the cgroup-level peak (since container start).
  read -r quota period < <(docker exec "$c" cat "$CG/cpu.max" 2>/dev/null || echo "max 100000")
  mem_limit=$(docker exec "$c" cat "$CG/memory.max" 2>/dev/null || echo max)
  peak_cg=$(docker exec "$c" cat "$CG/memory.peak" 2>/dev/null || echo 0)
  limit_cores=$(awk -v q="$quota" -v p="$period" 'BEGIN { if (q == "max") print 0; else printf "%.2f", q / p }')
  # Share of sampling intervals spent within 5% of the CPU limit (a single brief
  # spike must not count as "capped").
  near_cap=$(awk -v l="$limit_cores" '{ if (l > 0 && $1 >= 0.95 * l) n++ } END { printf "%.0f", (NR ? 100 * n / NR : 0) }' "$rates")
  rm -f "$rates"
  thr_pct=$(awk -v t="$throttled" -v p="$periods" 'BEGIN { if (p > 0) printf "%.1f", 100 * t / p; else print "0.0" }')

  printf '\n================ %s ================\n' "$c"
  printf 'Test window : %.0fs (%d samples)\n' "$duration" "$samples"

  echo
  if awk -v l="$limit_cores" 'BEGIN { exit !(l > 0) }'; then
    printf 'CPU (limit: %s cores)\n' "$limit_cores"
  else
    echo 'CPU (limit: none)'
  fi
  printf '  average            : %.2f cores\n' "$cpu_avg"
  printf '  p95                : %.2f cores\n' "$cpu_p95"
  printf '  peak               : %.2f cores\n' "$cpu_max"
  printf '  throttled periods  : %s of %s (%s%%), total wait %ss\n' "$throttled" "$periods" "$thr_pct" "$throttled_sec"

  echo
  if [[ "$mem_limit" == "max" ]]; then
    echo 'RAM (limit: none)'
  else
    printf 'RAM (limit: %s MiB)\n' "$(mib "$mem_limit")"
  fi
  printf '  anon average       : %s MiB\n' "$(mib "$anon_avg")"
  printf '  anon peak          : %s MiB   <- real process memory\n' "$(mib "$anon_max")"
  printf '  total peak         : %s MiB   (anon + page cache, sampled)\n' "$(mib "$mem_max_cur")"
  printf '  cgroup peak        : %s MiB   (since container start)\n' "$(mib "$peak_cg")"
  printf '  hit memory limit   : %s times | OOM kills: %s\n' "$mem_limit_hits" "$oom"

  # Sizing: peak * HEADROOM, rounded up (cores to an integer, RAM to 256 MiB).
  rec_cores=$(awk -v x="$cpu_max" -v h="$HEADROOM" 'BEGIN { y = x * h; r = int(y); if (r < y) r++; if (r < 1) r = 1; print r }')
  rec_mem=$(awk -v a="$anon_max" -v h="$HEADROOM" 'BEGIN { m = a * h / 1048576; r = int(m / 256); if (r * 256 < m) r++; if (r < 1) r = 1; print r * 256 }')
  # If CPU was capped by the limit, the measured peak is meaningless (it only
  # reflects the cap plus sampling jitter), so only report the limit as a floor.
  local cpu_capped=0
  if awk -v t="$thr_pct" -v n="$near_cap" -v l="$limit_cores" \
       'BEGIN { exit !(t > 5 || (l > 0 && n > 10)) }'; then
    cpu_capped=1
    rec_cores=$(awk -v l="$limit_cores" -v r="$rec_cores" 'BEGIN { if (l > 0) { x = int(l); if (x < l) x++; print x } else print r }')
  fi
  TOTAL_CORES=$((TOTAL_CORES + rec_cores))
  TOTAL_MEM_MIB=$((TOTAL_MEM_MIB + rec_mem))

  echo
  if [[ "$cpu_capped" -eq 1 ]]; then
    printf 'Suggested for %s: CPU unknown (capped), RAM %s MiB (peak x %s)\n' "$c" "$rec_mem" "$HEADROOM"
    echo '  ! CPU hit its limit, so true CPU demand cannot be sized from this run.'
    echo '    Decide CPU by job duration instead: total CPU-seconds / acceptable seconds per job.'
    printf '    CPU-seconds used in this window: %.0f\n' "$(awk -v a="$cpu_avg" -v d="$duration" 'BEGIN { print a * d }')"
  else
    printf 'Suggested for %s: %s vCPU, %s MiB RAM (peak x %s)\n' "$c" "$rec_cores" "$rec_mem" "$HEADROOM"
  fi
  if [[ "$oom" -gt 0 || "$mem_limit_hits" -gt 0 ]]; then
    echo '  ! RAM hit its limit / OOM kills happened: real demand is HIGHER than measured.'
    echo '    Raise the memory limit and run the test again.'
  fi
}

cmd_report() {
  if [[ ! -s "$CSV" ]]; then
    echo "No samples found in $CSV. Run '$0 start' first." >&2
    exit 1
  fi
  TOTAL_CORES=0
  TOTAL_MEM_MIB=0
  # Report on the containers that were actually sampled, so CONTAINERS only
  # needs to be set for `start` (not for `stop` / `report`).
  local names
  mapfile -t names < <(cut -d, -f2 "$CSV" | sort -u)
  for c in "${names[@]}"; do
    report_container "$c"
  done

  echo
  echo '=========================================='
  echo 'Suggested server for all measured services'
  echo '=========================================='
  printf '  %s vCPU, %s MiB RAM (sum of per-container peaks: conservative)\n' "$TOTAL_CORES" "$TOTAL_MEM_MIB"
  echo '  Add capacity for Postgres, Redis, MinIO and the OS (roughly +1 vCPU and'
  echo '  +1-2 GiB RAM), or run them on separate hosts.'
  echo '  Re-run with your worst case: biggest allowed video, max concurrent jobs.'
}

case "${1:-}" in
  start)    cmd_start ;;
  stop)     cmd_stop ;;
  report)   cmd_report ;;
  _sampler) sampler "${2:?container required}" ;;
  *)        usage ;;
esac