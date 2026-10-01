#!/bin/bash
# Scratch evidence driver for #2063. Never merged.
# usage: rerun.sh <label> <count> <target> <basetemp-mode: default|unique> [load]
set -u
label="$1"; count="$2"; target="$3"; mode="$4"; load="${5:-}"
pass=0; fail=0
load_pids=()
if [[ "$load" == "load" ]]; then
  n=$(nproc)
  for _ in $(seq 1 "$n"); do (while :; do :; done) & load_pids+=($!); done
  echo "[$label] background CPU load: $n busy loops"
fi
echo "[$label] ls /tmp/pytest-of-* BEFORE"; ls -la /tmp/pytest-of-* 2>&1 | tail -8
for i in $(seq 1 "$count"); do
  args=(-q -p no:cacheprovider -p printbt "$target")
  if [[ "$mode" == "unique" ]]; then
    bt="$RUNNER_TEMP/bt-$GITHUB_RUN_ID-$GITHUB_JOB-$label-$i"
    args+=(--basetemp "$bt")
  fi
  out="$RUNNER_TEMP/out-$label-$i.txt"
  start=$(date +%s.%N)
  python3 -m pytest "${args[@]}" > "$out" 2>&1
  rc=$?
  end=$(date +%s.%N)
  bt_seen=$(grep -o "^BASETEMP .*" "$out" | head -1)
  summary=$(grep -E "[0-9]+ (passed|failed|error)" "$out" | tail -1)
  if (( rc == 0 )); then pass=$((pass+1)); r=PASS; else fail=$((fail+1)); r=FAIL; fi
  echo "[$label] iter $i $r rc=$rc secs=$(awk "BEGIN{print $end-$start}") basetemp=${bt_seen:-${bt:-default(not printed on pass)}} :: $summary"
  if (( rc != 0 )); then
    echo "----- [$label] iter $i failure output -----"
    grep -n "^E \|Error\|assert\|FAILED\|tmp_path =" "$out" | head -40
    echo "-------------------------------------------"
  fi
done
for p in "${load_pids[@]}"; do kill "$p" 2>/dev/null; done
echo "[$label] ls /tmp/pytest-of-* AFTER"; ls -la /tmp/pytest-of-* 2>&1 | tail -8
echo "RESULT [$label] mode=$mode load=${load:-none} pass=$pass fail=$fail of $count"
