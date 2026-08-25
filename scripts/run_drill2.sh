#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== 1. Reset state & Don dep log Drill 2 cu ==="
python3 chaos/kill_region.py restore --region a --backend bare || true
printf a > edge/active_region
rm -f reports/drill-2-withdr.jsonl reports/health-events.jsonl reports/failover-events.jsonl reports/runbook-run.jsonl

echo "=== 2. Khoi dong Ingest & Replicate ==="
python3 state/ingest.py --region a --rate 0.5 --duration 150 >/dev/null 2>&1 &
PID_INGEST=$!
python3 state/replicate.py --every 30 --duration 150 --backend fs >/dev/null 2>&1 &
PID_REP=$!

echo "Cho 5 giay de snapshot dau tien hoan tat..."
sleep 5

echo "=== 3. Khoi dong Traffic Loadgen & Health Checker ==="
python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
PID_LOAD=$!
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
PID_HC=$!

echo "Cho 12 giay phat traffic binh thuong truoc khi gay su co (t0)..."
sleep 12

echo "=== 4. Kich hoat Chaos Kill Region A (t0) ==="
python3 chaos/kill_region.py --region a --mode netblock --mock

echo "Cho den khi Health Checker phat hien va ghi UNHEALTHY vao health-events.jsonl..."
while true; do
  if [ -f reports/health-events.jsonl ] && grep -q '"to": "UNHEALTHY"' reports/health-events.jsonl && grep -q '"region": "a"' reports/health-events.jsonl; then
    echo "-> Da nhan Alert UNHEALTHY tu Health Checker!"
    break
  fi
  sleep 1
done

echo "=== 5. Kich hoat Runbook Failover sau khi co Alert ==="
python3 dr/runbook.py --primary a --target b --backend fs --auto

echo "Cho Traffic loadgen chay het cua so thoi gian..."
wait $PID_LOAD 2>/dev/null || true

echo "=== 6. Ket qua do RTO/RPO tu dong ==="
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
