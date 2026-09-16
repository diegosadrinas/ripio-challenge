import threading
import time
from datetime import datetime, timezone


class InMemoryMetricsCollector:
    def __init__(self) -> None:
        self._started_at = time.time()
        self._lock = threading.Lock()
        self._requests_total = 0
        self._requests_by_status_family: dict[str, int] = {
            "1xx": 0,
            "2xx": 0,
            "3xx": 0,
            "4xx": 0,
            "5xx": 0,
        }
        self._request_metrics: dict[tuple[str, str, int], dict[str, int]] = {}

    def observe_request(self, *, method: str, path: str, status_code: int, duration_ms: int) -> None:
        status_family = f"{status_code // 100}xx"
        key = (method.upper(), path, status_code)

        with self._lock:
            self._requests_total += 1
            if status_family in self._requests_by_status_family:
                self._requests_by_status_family[status_family] += 1

            if key not in self._request_metrics:
                self._request_metrics[key] = {"count": 0, "total_duration_ms": 0}

            self._request_metrics[key]["count"] += 1
            self._request_metrics[key]["total_duration_ms"] += max(duration_ms, 0)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            metric_rows = []
            for (method, path, status_code), stats in self._request_metrics.items():
                count = stats["count"]
                total_duration_ms = stats["total_duration_ms"]
                avg_duration_ms = round(total_duration_ms / count, 2) if count else 0.0
                metric_rows.append(
                    {
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "count": count,
                        "total_duration_ms": total_duration_ms,
                        "avg_duration_ms": avg_duration_ms,
                    }
                )

            metric_rows.sort(key=lambda row: (-int(row["count"]), str(row["path"]), str(row["method"])))

            return {
                "generated_at": datetime.now(timezone.utc),
                "uptime_seconds": int(max(time.time() - self._started_at, 0)),
                "requests_total": self._requests_total,
                "requests_by_status_family": dict(self._requests_by_status_family),
                "request_metrics": metric_rows,
            }
