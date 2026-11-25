"""Summary of the RTCP receiver reports the far end sent about our stream."""
from __future__ import annotations


def summarize_rtcp(reports: list[dict], rtts: list[float], sr_received: int) -> dict:
    """reports: report blocks the peer sent about OUR stream (the peer's view)."""
    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None
    loss = [r["fraction_lost"] * 100.0 for r in reports]
    jit = [r["jitter_ms"] for r in reports]
    return {
        "rr_count": len(reports),
        "sr_received": sr_received,
        "rtt_ms_avg": avg(rtts),
        "rtt_ms_max": round(max(rtts), 2) if rtts else None,
        "remote_loss_pct_avg": avg(loss),
        "remote_loss_pct_max": round(max(loss), 2) if loss else None,
        "remote_cum_lost": reports[-1]["cum_lost"] if reports else None,
        "remote_jitter_ms_avg": avg(jit),
        "remote_jitter_ms_max": round(max(jit), 2) if jit else None,
    }
