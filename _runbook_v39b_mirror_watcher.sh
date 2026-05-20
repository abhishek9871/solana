#!/bin/bash
# Remote watcher: streams matching log lines, exits when MIRROR_END count hits 10
# or on any hard-stop signature.
RUNID=$(cat /root/piggy/current_pgg2_live_mirror_runid.txt)
LOG="/root/piggy/logs/${RUNID}.log"
end_count=0
tail -n 0 -F "$LOG" | grep -E --line-buffered 'PGG2-LIVE-MIRROR-START|PGG2-V39-ENTRY-ROUTER|PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-TOKEN-RECONCILE|PGG2-V39-LIVE-WALLET-DELTA|PGG2-V39-LIVE-PNL-RECONCILE|PGG2-V39-LIVE-MIRROR-END|PGG2-POSITION-TOKEN-MISMATCH-FATAL|PGG2-RISK-CLOSE-FAIL|Traceback|sim_needed=1|actual_all_in_pnl=-|PGG2-LIVE-BUY ' | while IFS= read -r line; do
  echo "$line"
  case "$line" in
    *PGG2-POSITION-TOKEN-MISMATCH-FATAL*) echo "STOP=token_mismatch_fatal"; exit 0 ;;
    *PGG2-RISK-CLOSE-FAIL*)               echo "STOP=risk_close_fail"; exit 0 ;;
    *Traceback*)                          echo "STOP=traceback"; exit 0 ;;
    *actual_all_in_pnl=-*)                echo "STOP=negative_close"; exit 0 ;;
    *"PGG2-LIVE-BUY "*lane=priced_snap*)  echo "STOP=non_v39_lane_priced_snap"; exit 0 ;;
    *"PGG2-LIVE-BUY "*lane=curve*)        echo "STOP=non_v39_lane_curve"; exit 0 ;;
    *"PGG2-LIVE-BUY "*lane=raw*)          echo "STOP=non_v39_lane_raw"; exit 0 ;;
  esac
  case "$line" in
    *PGG2-V39-LIVE-MIRROR-END*)
      end_count=$((end_count+1))
      echo "MIRROR_END_COUNT=$end_count"
      if [ "$end_count" -ge 10 ]; then echo "STOP=target_10_reached"; exit 0; fi
      ;;
  esac
done
