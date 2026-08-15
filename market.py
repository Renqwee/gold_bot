"""حالة سوق الذهب — مفتوح ولا مغلق.

سوق الذهب الفوري (XAU/USD) يشتغل ٢٤ ساعة من الأحد مساءً للجمعة مساءً،
ويقفل نهاية الأسبوع. أي سعر تشوفه والسوق مغلق = سعر إغلاق الجمعة، ما يتحرك.

المواعيد تقريبية بتوقيت UTC (السوق يتبع توقيت نيويورك فيتزحلق ساعة مع التوقيت الصيفي).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# أيام الأسبوع: الاثنين = 0 … الأحد = 6
FRIDAY, SATURDAY, SUNDAY = 4, 5, 6

CLOSE_HOUR_UTC = 21  # الجمعة ٢١:٠٠ UTC
OPEN_HOUR_UTC = 22  # الأحد ٢٢:٠٠ UTC


# تسعيرة أقدم من هذا وقت التداول تعني أن السوق واقف فعلاً
QUOTE_STALE_AFTER = timedelta(minutes=20)


@dataclass(frozen=True)
class MarketStatus:
    is_open: bool
    reopens_at: datetime | None  # وقت الفتح القادم (فقط إذا كان مغلقاً)
    basis: str = "الجدول"  # «التسعيرة» = من بيانات السوق، «الجدول» = تخمين بالساعة

    @property
    def until_open(self) -> timedelta | None:
        if self.reopens_at is None:
            return None
        return self.reopens_at - datetime.now(timezone.utc)


def _next_sunday_open(now: datetime) -> datetime:
    """أقرب أحد الساعة ٢٢:٠٠ UTC من الآن."""
    days_ahead = (SUNDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=OPEN_HOUR_UTC, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def status(
    now: datetime | None = None,
    quoted_at: datetime | None = None,
) -> MarketStatus:
    """هل سوق الذهب مفتوح الآن؟

    إذا توفّر وقت التسعيرة الحقيقي (`quoted_at`) نحكم به — لأن السوق يقول عن نفسه
    أصدق من ساعة مبرمجة عندنا تتأثر بالتوقيت الصيفي والعطلات الرسمية.
    وإلا نرجع للجدول التقريبي.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if quoted_at is not None:
        fresh = (now - quoted_at.astimezone(timezone.utc)) < QUOTE_STALE_AFTER
        return MarketStatus(
            is_open=fresh,
            reopens_at=None if fresh else _next_sunday_open(now),
            basis="التسعيرة",
        )

    weekday, hour = now.weekday(), now.hour
    closed = (
        (weekday == FRIDAY and hour >= CLOSE_HOUR_UTC)
        or weekday == SATURDAY
        or (weekday == SUNDAY and hour < OPEN_HOUR_UTC)
    )

    if not closed:
        return MarketStatus(is_open=True, reopens_at=None)
    return MarketStatus(is_open=False, reopens_at=_next_sunday_open(now))


def humanize(delta: timedelta) -> str:
    """مدة زمنية بصيغة عربية مختصرة."""
    total = int(delta.total_seconds())
    if total <= 0:
        return "الآن"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60

    parts = []
    if days:
        parts.append(f"{days} يوم")
    if hours:
        parts.append(f"{hours} ساعة")
    if minutes and not days:
        parts.append(f"{minutes} دقيقة")
    return " و".join(parts) if parts else "أقل من دقيقة"
