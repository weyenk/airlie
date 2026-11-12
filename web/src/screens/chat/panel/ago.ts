const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const startOfDay = (d: Date): number => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** How long ago `updated` (epoch seconds) was, as a row's short time: "now", "5m", "2h", "yesterday", a weekday
 * within the week, else the date. */
export function ago(updated: number, nowMs: number = Date.now()): string {
  const then = new Date(updated * 1000);
  const diff = nowMs - then.getTime();
  const days = Math.round((startOfDay(new Date(nowMs)) - startOfDay(then)) / 86_400_000);
  if (days <= 0) {
    if (diff < 60_000) return "now";
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
    return `${Math.floor(diff / 3_600_000)}h`;
  }
  if (days === 1) return "yesterday";
  if (days < 7) return DAYS[then.getDay()];
  return `${MONTHS[then.getMonth()]} ${then.getDate()}`;
}
