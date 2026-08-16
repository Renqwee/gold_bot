"""قياس سلوك مصادر الأسعار أثناء التداول الفعلي.

كل ما بنيناه اختُبر والسوق مقفل، فثلاثة أشياء ما تأكدت:
  • هل فحص القفزة ٣٪ يرفض حركة مشروعة عند الافتتاح؟
  • هل «تسعيرة لحظية» تظهر فعلاً لما تتحرك الأسعار؟
  • كم يتأخر كل مصدر عن الآخر، وهل يتفقون؟

الأداة تنتظر افتتاح السوق بنفسها ثم تسجّل، فتقدر تشغّلها في أي وقت.

    python measure_sources.py                  # ينتظر الفتح ثم يسجّل ساعة
    python measure_sources.py --minutes 30     # مدة أقصر
    python measure_sources.py --now            # ابدأ فوراً بلا انتظار
    python measure_sources.py --report out.csv # حلّل تسجيلاً سابقاً
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

import market
import rates

INTERVAL = 15  # ثانية بين كل عيّنة
OUTPUT = Path(__file__).with_name("source_measurements.csv")
COLUMNS = ["ts", "source", "price", "quoted_at", "latency_ms", "error"]


async def sample_one(client: httpx.AsyncClient, source) -> dict:
    """عيّنة واحدة من مصدر واحد — لا ترفع استثناء، تسجّل الخطأ."""
    name, url, parser = source
    started = datetime.now(timezone.utc)
    row = {"ts": started.isoformat(), "source": name, "price": "",
           "quoted_at": "", "latency_ms": "", "error": ""}
    try:
        response = await client.get(url)
        response.raise_for_status()
        value, quoted_at = parser(response.json())
        row["price"] = str(value)
        row["quoted_at"] = quoted_at.isoformat() if quoted_at else ""
    except Exception as exc:  # noqa: BLE001 — الفشل بيانات كمان
        row["error"] = str(exc)[:120]
    row["latency_ms"] = f"{(datetime.now(timezone.utc) - started).total_seconds() * 1000:.0f}"
    return row


async def wait_for_open() -> None:
    """ينتظر حتى يفتح السوق فعلاً — بالتسعيرة لا بالساعة."""
    swissquote = rates.SPOT_SOURCES[0]
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            row = await sample_one(client, swissquote)
            quoted = row["quoted_at"]
            state = market.status(
                quoted_at=datetime.fromisoformat(quoted) if quoted else None
            )
            if state.is_open:
                print(f"🟢 السوق فتح — نبدأ التسجيل ({state.basis})")
                return

            remaining = state.until_open
            print(
                f"⏳ السوق مغلق، يفتح بعد {market.humanize(remaining)}"
                f" — أفحص كل ٥ دقائق…",
                flush=True,
            )
            # قرب الفتح نقلّل الفاصل عشان ما نفوّت أول دقائق التداول
            await asyncio.sleep(60 if remaining < timedelta(minutes=10) else 300)


async def record(minutes: int) -> Path:
    deadline = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    samples = 0

    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()

        async with httpx.AsyncClient(timeout=10) as client:
            while datetime.now(timezone.utc) < deadline:
                rows = await asyncio.gather(
                    *(sample_one(client, s) for s in rates.SPOT_SOURCES)
                )
                for row in rows:
                    writer.writerow(row)
                handle.flush()
                samples += 1

                shown = " │ ".join(
                    f"{r['source']}: {r['price'][:9] or '—'}" for r in rows
                )
                print(f"[{samples:>3}] {shown}", flush=True)
                await asyncio.sleep(INTERVAL)

    print(f"\n✅ {samples} عيّنة في {OUTPUT.name}")
    return OUTPUT


def report(path: Path) -> None:
    """يحلّل التسجيل ويجاوب على الأسئلة الثلاثة."""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    if not rows:
        sys.exit("الملف فاضي")

    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    print(f"\n{'=' * 62}\nتقرير: {len(rows)} عيّنة من {len(by_source)} مصادر\n{'=' * 62}")

    print("\n— الموثوقية والسرعة —")
    for name, entries in by_source.items():
        ok = [e for e in entries if not e["error"]]
        latencies = [float(e["latency_ms"]) for e in ok if e["latency_ms"]]
        rate = len(ok) / len(entries) * 100
        median = statistics.median(latencies) if latencies else 0
        worst = max(latencies) if latencies else 0
        print(
            f"  {name:<12} نجاح {rate:5.1f}%  │  وسيط {median:5.0f}ms  │"
            f"  أسوأ {worst:6.0f}ms"
        )

    print("\n— كم يتحرك السعر فعلاً؟ —")
    for name, entries in by_source.items():
        prices = [Decimal(e["price"]) for e in entries if e["price"]]
        if len(prices) < 2:
            continue
        changes = [
            abs(b - a) / a * 100 for a, b in zip(prices, prices[1:]) if a
        ]
        moved = sum(1 for c in changes if c > 0)
        biggest = max(changes) if changes else Decimal(0)
        print(
            f"  {name:<12} تغيّر في {moved}/{len(changes)} فترة  │"
            f"  أكبر قفزة {biggest:.4f}%  │  المدى "
            f"${min(prices):.2f}–${max(prices):.2f}"
        )
        if biggest > rates.MAX_JUMP_PCT:
            print(
                f"    ⚠️ تجاوزت حد الرفض ({rates.MAX_JUMP_PCT}%) — "
                f"الحد يحتاج رفعاً وإلا رفضنا حركة مشروعة"
            )

    print("\n— هل المصادر متفقة؟ —")
    names = list(by_source)
    base = names[0]
    base_prices = {e["ts"][:16]: Decimal(e["price"]) for e in by_source[base] if e["price"]}
    for name in names[1:]:
        diffs = []
        for entry in by_source[name]:
            key = entry["ts"][:16]
            if entry["price"] and key in base_prices:
                other, ref = Decimal(entry["price"]), base_prices[key]
                diffs.append(abs(other - ref) / ref * 100)
        if diffs:
            print(
                f"  {name:<12} مقابل {base}: وسيط الفرق "
                f"{statistics.median(diffs):.4f}%  │  أقصى {max(diffs):.4f}%"
            )

    print("\n— طزاجة التسعيرة (من يعطي وقتاً حقيقياً؟) —")
    for name, entries in by_source.items():
        stamped = [e for e in entries if e["quoted_at"]]
        if not stamped:
            print(f"  {name:<12} لا يعطي وقت تسعيرة — نعتمد وقت الجلب")
            continue
        ages = [
            (
                datetime.fromisoformat(e["ts"])
                - datetime.fromisoformat(e["quoted_at"])
            ).total_seconds()
            for e in stamped
        ]
        print(
            f"  {name:<12} عمر التسعيرة: وسيط {statistics.median(ages):.0f}ث  │"
            f"  أقصى {max(ages):.0f}ث"
        )
        if max(ages) > market.QUOTE_STALE_AFTER.total_seconds():
            print(
                f"    ⚠️ تجاوز حد «السوق مغلق» ({market.QUOTE_STALE_AFTER}) —"
                f" البوت قد يقول مغلق والسوق شغال"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60, help="مدة التسجيل")
    parser.add_argument("--now", action="store_true", help="ابدأ فوراً بلا انتظار")
    parser.add_argument("--report", type=Path, help="حلّل ملفاً موجوداً فقط")
    args = parser.parse_args()

    if args.report:
        report(args.report)
        return

    async def run():
        if not args.now:
            await wait_for_open()
        path = await record(args.minutes)
        report(path)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nتوقف — التقرير على ما سُجّل:")
        if OUTPUT.exists():
            report(OUTPUT)


if __name__ == "__main__":
    main()
