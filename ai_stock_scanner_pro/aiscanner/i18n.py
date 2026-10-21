"""Hebrew rendering of structured explanation codes + UI strings (single language in the MVP)."""
from __future__ import annotations

import math
from string import Formatter

from .models import Reason

# ---- explanation templates (reason code -> Hebrew) ---------------------------------
REASONS: dict[str, str] = {
    # eligibility gates
    "gate_data_quality": "איכות נתונים לא תקינה ({codes})",
    "gate_history": "היסטוריה קצרה מדי: {bars} ימים (נדרש {need})",
    "gate_price": "מחיר {price:.2f}$ נמוך מהמינימום {need:.2f}$",
    "gate_liquidity": "נזילות נמוכה: מחזור יומי ממוצע {dv:,.0f}$ (נדרש {need:,.0f}$)",
    "gate_spread": "אומדן ספרד גבוה: {spread:.2%} (מקסימום {need:.2%})",
    "gate_risk_uncomputable": "לא ניתן לחשב ATR/סיכון",
    # trend
    "trend_struct": "מבנה ממוצעים נעים: {v:.0%} מהתנאים מתקיימים",
    "trend_slope": "שיפוע ממוצעים נעים (איכות {v:.0%})",
    "trend_weekly": "יישור שבועי: {aligned}",
    "trend_slope200": "שיפוע SMA200 ב-20 ימים: {v:+.1%}",
    "trend_dist_high": "מרחק משיא 52 שבועות: {v:+.1%}",
    # momentum
    "mom_rsi": "RSI(14) = {v:.0f}",
    "mom_macd": "MACD: היסטוגרמה חיובית={pos}, בעלייה={rising}",
    "mom_accel": "האצת מומנטום: {v:+.1%}",
    "mom_ret": "תשואה 6 חודשים {r126:+.0%}, 12 חודשים {r252:+.0%}",
    "mom_persist": "התמדת מומנטום חודשי: {v:.0%} מהחודשים חיוביים",
    # volume
    "vol_rel": "נפח יחסי: {v:.2f}x",
    "vol_accum": "איכות צבירה (OBV / נפח עלייה מול ירידה): {v:.0%}",
    "vol_liq": "מחזור יומי ממוצע: {dv:,.0f}$",
    # setup
    "setup_present": "תבנית: {name}",
    "setup_none": "אין תבנית כניסה מזוהה",
    "setup_trend_only": "מגמה תקינה ללא תבנית כניסה ספציפית",
    # relative strength
    "rs_spy": "חוזק יחסי מול SPY: {v:+.1%}",
    "rs_sector": "חוזק יחסי מול הסקטור: {v:+.1%}",
    "rs_line": "קו חוזק יחסי מול ממוצע 50: {v:+.1%}",
    # volatility
    "volat_atr": "ATR באחוזים: {v:.2%}",
    "volat_rv": "תנודתיות ממומשת שנתית: {v:.0%}",
    "volat_dd": "ירידה מקסימלית (6 חודשים): {v:.0%}",
    # risk / reward
    "rr_none": "לא ניתן לחשב רמות סטופ/יעד",
    "rr_ratio": "סיכוי/סיכון {rr:.2f} (סטופ {stop:.2f}, יעד {target:.2f}, בסיס יעד: {basis})",
    "rr_stop_atr": "מרחק סטופ {v:.1f} ATR (בסיס: {basis})",
    # validation failures
    "val_no_levels": "לא ניתן לחשב רמות סיכון",
    "val_below_sma50": "המחיר מתחת ל-SMA50",
    "val_atr_range": "ATR% מחוץ לטווח 1%-8% ({v:.2%})",
    "val_overbought": "RSI גבוה מדי ({v:.0f})",
    "val_extended": "המחיר רחוק מדי מ-SMA20 (מתוח)",
    "val_no_setup": "אין תבנית כניסה",
    "val_breakout_volume": "פריצה ללא אישור נפח ({v:.2f}x)",
    "val_min_rr": "סיכוי/סיכון {rr:.2f} נמוך מהמינימום {need:.2f}",
    "val_below_sma200": "המחיר מתחת ל-SMA200",
    "val_sma50_below_200": "SMA50 מתחת ל-SMA200",
    "val_sma200_falling": "SMA200 יורד",
    "val_deep_drawdown": "ירידה עמוקה משיא ({v:.0%})",
    "val_atr_high": "תנודתיות גבוהה מדי להשקעה ארוכת טווח ({v:.2%})",
    "val_rs_negative": "חוזק יחסי שלילי מול SPY ({v:+.1%})",
    "val_lt_liquidity": "נזילות נמוכה להשקעה ארוכת טווח",
    # regime
    "rg_insufficient_data": "אין מספיק נתונים לקביעת משטר שוק – ברירת מחדל נייטרלית",
    "rg_spy_vs_200": "SPY {close:.2f} מול SMA200 {sma:.2f}",
    "rg_spy_vs_50": "SPY {close:.2f} מול SMA50 {sma:.2f}",
    "rg_qqq_vs_200": "QQQ {close:.2f} מול SMA200 {sma:.2f}",
    "rg_breadth50": "רוחב שוק: {v:.0%} מתוך {n} מניות מעל SMA50",
    "rg_breadth200": "{v:.0%} מהמניות מעל SMA200",
    "rg_vix": "VIX = {v:.1f}",
    "rg_realized_vol": "תנודתיות ממומשת של SPY: {v:.0%}",
    "rg_drawdown": "SPY {v:+.1%} משיא 52 שבועות",
    "rg_correlation": "קורלציה ממוצעת בין מניות: {v:.2f}",
    "inv_spy_below": "SPY נסגר מתחת ל-SMA50 ({level:.2f})",
    "inv_spy_below200": "SPY נסגר מתחת ל-SMA200 ({level:.2f})",
    "inv_vix_above": "VIX מעל {level:.0f}",
    "inv_breadth_below": "פחות מ-{level:.0%} מהמניות מעל SMA50",
    "inv_spy_reclaim": "SPY חוזר מעל SMA200 ({level:.2f})",
    "inv_breadth_above": "יותר מ-{level:.0%} מהמניות מעל SMA50",
    "inv_up_to_on": "ציון המשטר עולה מעל {level:.0f} → Risk-On",
    "inv_down_to_off": "ציון המשטר יורד מתחת ל-{level:.0f} → Risk-Off",
    # portfolio / risk
    "rk_short_unsupported": "פוזיציות שורט אינן נתמכות בגרסה זו",
    "rk_invalid_stop": "סטופ לא תקין: חייב להיות מתחת למחיר הכניסה (כניסה {entry:.2f}, סטופ {stop:.2f})",
    "rk_invalid_input": "קלט לא תקין: {what}",
    "rk_max_positions": "הגעת למקסימום {n} פוזיציות פתוחות",
    "rk_duplicate": "כבר קיימת פוזיציה ב-{symbol}",
    "rk_zero_shares": "סכום הסיכון קטן מדי למניה אחת (סטופ רחוק מדי)",
    "rk_cash": "אין מספיק מזומן: נדרש {need:,.0f}$, זמין {have:,.0f}$",
    "rk_position_size": "גודל פוזיציה {pct:.1%} מההון חורג מהמקסימום {max:.0%}",
    "rk_total_risk": "סיכון פתוח מצטבר {pct:.1%} יחרוג מהמקסימום {max:.1%}",
    "rk_sector": "חשיפה לסקטור {sector} {pct:.1%} חורגת מהמקסימום {max:.0%}",
    "rk_stale_data": "נתוני שוק ישנים או לא אמינים ({detail})",
    "rk_entry_deviation": "מחיר הכניסה סוטה {pct:.1%} מהמחיר האחרון ({last:.2f})",
    "rk_gap_stress": "הפסד במצב קיצון (גאפ+עלויות) {loss:,.0f}$ חורג מ-{mult:.1f}x תקציב הסיכון",
    "rk_override_too_big": "כמות מבוקשת חורגת מהכמות המחושבת לפי כלל הסיכון ({calc})",
    # monitor alerts
    "al_stop_breach": "{symbol}: מחיר {price:.2f} ירד אל/מתחת לסטופ {stop:.2f}",
    "al_near_stop": "{symbol}: המחיר {price:.2f} קרוב לסטופ {stop:.2f} ({dist:.1f} ATR)",
    "al_trend_break": "{symbol}: שבירת מגמה – {detail}",
    "al_trailing": "{symbol}: הצעת סטופ נגרר {new:.2f} (נוכחי {old:.2f})",
    "al_drawdown": "ירידה בהון התיק {dd:.1%} משיא (סף {limit:.0%})",
    "al_total_risk": "סיכון פתוח מצטבר {pct:.1%} מעל הסף {max:.1%}",
    "al_sector": "ריכוז בסקטור {sector}: {pct:.1%} (סף {max:.0%})",
    "al_correlation": "קורלציה ממוצעת בתיק {v:.2f} (סף {limit:.2f})",
    "al_position_size": "{symbol}: משקל {pct:.1%} מעל המקסימום {max:.0%}",
    "al_stale": "{symbol}: נתונים ישנים ({detail})",
    "al_new_setup": "{symbol} ({strategy}): ציון {score:.0f} – תבנית חדשה שאומתה",
}

COMPONENTS = {"trend": "מגמה", "momentum": "מומנטום", "volume": "נפח", "setup": "תבנית כניסה",
              "relative_strength": "חוזק יחסי", "volatility": "תנודתיות", "risk_reward": "סיכוי/סיכון"}
PATTERNS = {
    "breakout": "פריצה", "near_breakout": "קרוב לפריצה", "tight_consolidation": "התכנסות הדוקה",
    "pullback_support": "נסיגה לתמיכה", "sma20_reclaim": "חזרה מעל SMA20",
    "volatility_contraction": "התכווצות תנודתיות", "new_high": "שיא חדש", "lt_pullback": "נסיגה לתמיכה (ארוך)",
    "reclaim_sma50": "חזרה מעל SMA50", "triangle_ascending": "משולש עולה (ניסיוני)",
    "triangle_descending": "משולש יורד (ניסיוני)", "triangle_symmetrical": "משולש סימטרי (ניסיוני)",
    "wedge_rising": "נס עולה (ניסיוני)", "wedge_falling": "נס יורד (ניסיוני)", "cup_and_handle": "כוס וידית (ניסיוני)"}
STRATEGY = {"swing": "סווינג", "longterm": "טווח ארוך"}
KLASS = {"high": "עדיפות גבוהה", "strong": "מועמד חזק", "monitor": "מעקב", "low": "עדיפות נמוכה"}
KLASS_MARK = {"high": "▲▲ ", "strong": "▲ ", "monitor": "● ", "low": "▽ "}   # colour-independent markers
REGIME = {"RISK_ON": "Risk-On (תיאבון סיכון)", "NEUTRAL": "נייטרלי", "RISK_OFF": "Risk-Off (הימנעות מסיכון)"}
POSTURE = {"full": "חשיפה מלאה לפי כללי הסיכון", "selective": "חשיפה סלקטיבית – רק מועמדים מובילים",
           "defensive": "עמדה defensive – צמצום חשיפה והימנעות מכניסות חדשות"}
BASIS = {"resistance": "התנגדות", "atr_projection": "הקרנת ATR", "trend_projection": "הקרנת מגמה",
         "support": "תמיכה", "min_1atr": "מינימום 1 ATR", "2atr": "2 ATR", "max_3atr": "מקסימום 3 ATR",
         "4atr": "4 ATR", "sma200": "SMA200", "min_1.5atr": "מינימום 1.5 ATR"}
SEVERITY = {"critical": "▲ קריטי", "warning": "■ אזהרה", "info": "● מידע"}


class _Fmt(Formatter):
    def format_field(self, value, spec):
        if isinstance(value, bool):
            return "כן" if value else "לא"
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "n/a"
        return super().format_field(value, spec)


def render(r: Reason) -> str:
    tpl = REASONS.get(r.code)
    if tpl is None:
        return f"{r.code} {r.params}"
    params = dict(r.params)
    if r.code == "setup_present":
        params["name"] = PATTERNS.get(params.get("name"), params.get("name"))
    if "basis" in params:
        params["basis"] = BASIS.get(params["basis"], params["basis"])
    try:
        return _Fmt().vformat(tpl, (), params)
    except (KeyError, ValueError, IndexError, TypeError):
        return f"{r.code} {r.params}"


def mark(r: Reason) -> str:
    return {True: "✔ ", False: "✖ ", None: "• "}[r.positive]


def render_dicts(items: list[dict]) -> list[str]:
    return [mark(Reason(i["code"], i["params"], i["positive"])) + render(Reason(i["code"], i["params"], i["positive"]))
            for i in items]
