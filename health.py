"""مراقبة صحة مصادر الأسعار.

المشكلة: لو سقط المصدر الأول الساعة ٣ فجراً، البوت ينتقل للاحتياطي بصمت
وما أحد يدري إلا لما يلاحظ أحد فرقاً في الأرقام. هذي الوحدة تكشف الانتقالات
وتنبّه صاحب البوت — مرة عند التدهور ومرة عند التعافي، بلا تكرار مزعج.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


class Health(Enum):
    OK = "سليم"  # المصدر الأول يعمل
    DEGRADED = "متدهور"  # نعمل على مصدر احتياطي
    DOWN = "متوقف"  # كل المصادر فشلت، نعرض قيمة قديمة


@dataclass
class Monitor:
    """آلة حالة بسيطة: تُبلّغ عند تغيّر الحالة فقط، لا عند كل فحص."""

    primary: str
    state: Health = Health.OK
    source: str = ""
    since: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # نمنع تكرار نفس البلاغ إذا تذبذبت الحالة سريعاً
    quiet_for: timedelta = timedelta(minutes=30)
    last_report: datetime | None = None

    def _classify(self, source: str | None, stale: bool) -> Health:
        if source is None or stale:
            return Health.DOWN
        return Health.OK if source == self.primary else Health.DEGRADED

    def observe(
        self,
        source: str | None,
        stale: bool = False,
        now: datetime | None = None,
    ) -> str | None:
        """يسجّل ملاحظة ويرجّع نص البلاغ إذا استحق الإبلاغ، وإلا None."""
        now = now or datetime.now(timezone.utc)
        new_state = self._classify(source, stale)

        if new_state == self.state:
            self.source = source or self.source
            return None

        previous, elapsed = self.state, now - self.since
        self.state = new_state
        self.source = source or ""
        self.since = now

        # الرجوع للوضع السليم يُبلَّغ دائماً — الخبر السار ما يُكتَم
        if new_state is not Health.OK:
            if (
                self.last_report is not None
                and now - self.last_report < self.quiet_for
            ):
                return None

        self.last_report = now
        return self._message(previous, elapsed)

    def _message(self, previous: Health, elapsed: timedelta) -> str:
        minutes = int(elapsed.total_seconds() // 60)
        duration = f"بعد {minutes} دقيقة" if minutes else "الآن"

        if self.state is Health.OK:
            return f"✅ تعافت المصادر — رجعنا إلى {self.source} ({duration})"
        if self.state is Health.DEGRADED:
            return (
                f"⚠️ {self.primary} ما يستجيب — نعمل على {self.source} ({duration})\n"
                f"الأسعار سليمة لكن قد تفقد وقت التسعيرة الحقيقي."
            )
        return (
            f"🚨 كل مصادر الأسعار فشلت ({duration})\n"
            f"البوت يعرض آخر قيمة معروفة موسومة «قديمة»."
        )
