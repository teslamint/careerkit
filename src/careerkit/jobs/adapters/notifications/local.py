from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from subprocess import SubprocessError
import subprocess
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationTarget:
    channel: str
    target: str
    account: str | None = None


def _notification_target(config: dict[str, Any]) -> NotificationTarget | None:
    notifications = config.get("notifications", {})
    channel = notifications.get("channel")
    target = notifications.get("target")
    account = notifications.get("account")
    if not channel or not target:
        return None
    return NotificationTarget(str(channel), str(target), str(account) if account else None)


def send_notification(message: str, config: dict[str, Any]) -> bool:
    target = _notification_target(config)
    if target is None:
        logger.warning("Notification channel/target not configured")
        return False
    command = [
        "openclaw",
        "message",
        "send",
        "--channel",
        target.channel,
        "--target",
        target.target,
        "--message",
        message,
    ]
    if target.account:
        command.extend(["--account", target.account])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        logger.warning("openclaw command not found; skipping notification")
        return False
    except (OSError, SubprocessError) as exc:
        logger.warning("Notification error: %s", exc)
        return False
    if result.returncode == 0:
        return True
    error_output = result.stderr.strip() or result.stdout.strip() or "unknown error"
    logger.warning("Notification send failed: %s", error_output)
    return False


def format_notification(results: Iterable[Any], summary: Any) -> str:
    lines = [
        "🔔 **JD 자동 파이프라인 결과**",
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"✨ 신규 URL: {getattr(summary, 'new', 0)}개",
        f"✅ 처리 완료: {getattr(summary, 'processed', 0)}개",
        f"🟢 추천: {getattr(summary, 'recommended', 0)}개",
        f"🟡 보류: {getattr(summary, 'hold', 0)}개",
        f"🔴 패스: {getattr(summary, 'passed', 0)}개",
        "",
    ]
    recommended = [row for row in results if getattr(row, "verdict", None) == "지원 추천"]
    if recommended:
        lines.append("**🟢 지원 추천 공고:**")
        for row in recommended[:5]:
            title = getattr(row, "title", None) or getattr(row, "job_id", "unknown")
            company = getattr(row, "company", None) or "unknown"
            lines.append(f"• [{company}] {title}")
            lines.append(f"  {getattr(row, 'url', '')}")
        if len(recommended) > 5:
            lines.append(f"  ... 외 {len(recommended) - 5}개")
    return "\n".join(lines)
