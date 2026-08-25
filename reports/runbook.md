# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` hoặc `/readyz` fail 3 lần liên tiếp trong `reports/health-events.jsonl` | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | ts incident mở được ghi vào `reports/runbook-run.jsonl:2` | on-call |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Dòng `step:2_restore_snapshot` xuất hiện trong `reports/failover-events.jsonl:2` | SRE / DR Lead |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` | `/readyz` của b trả 200 sau khi hết warmup (`reports/failover-events.jsonl:4`) | SRE / DR Lead |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region=b` (`reports/failover-events.jsonl:5`) | Network / SRE |
| 6 | Verify golden signals | `python3 -c "import httpx; [print(httpx.get('http://127.0.0.1:8080/v1/infer').status_code) for _ in range(10)]"` | 10 request test đều trả 200, p95 < 500ms, error rate = 0% (`reports/runbook-run.jsonl:6`) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` = PASS, RTO <= 300s (`reports/runbook-run.jsonl:7`) | Incident Commander |

**Rollback (failover ngược):** điều kiện nào thì trả traffic về region A? Ai quyết định?
(§4 Anti-Patterns: full-auto không có circuit breaker → 2 region flap qua lại.)

- **Điều kiện Rollback về Region A**:
  1. Region A đã được khôi phục triệt để và duy trì trạng thái `HEALTHY` (endpoint `/readyz` trả HTTP 200) ổn định liên tục ít nhất 15 phút (vượt qua observation window để tránh flapping).
  2. Toàn bộ dữ liệu mới (delta writes) được ghi vào Region B trong thời gian sự cố đã được đồng bộ ngược (reverse replication) về Region A thành công (`state/snapshot.py put --region b` và restore sang Region A), đảm bảo RPO rollback = 0.
  3. Kiểm tra Golden signals tại Region A (sau khi warm-up GPU pool về `full`) đạt p95 latency < 300ms và error rate = 0%.
- **Thẩm quyền quyết định (Authority)**:
  - Chỉ **Incident Commander (Chỉ huy sự cố)** hoặc **DR Lead / Head of Infrastructure** mới có quyền phê duyệt lệnh rollback thủ công (manual gate). Tuyệt đối cấm cơ chế tự động rollback không có circuit breaker.
