import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.express as px
import requests
import time

# הגדרת תעודות הסל המייצגות את הסקטורים בארה"ב
SECTORS = {
    'XLK': 'טכנולוגיה (Technology)',
    'XLF': 'פיננסים (Financials)',
    'XLV': 'בריאות (Health Care)',
    'XLE': 'אנרגיה (Energy)',
    'XLY': 'צריכה מחזורית (Consumer Discretionary)',
    'XLP': 'צריכה בסיסית (Consumer Staples)',
    'XLI': 'תעשייה (Industrials)',
    'XLB': 'חומרי גלם (Materials)',
    'XLU': 'תשתיות (Utilities)',
    'XLRE': 'נדל"ן (Real Estate)',
    'XLC': 'תקשורת (Communication Services)'
}

# פונקציה לחישוב RSI
def calculate_rsi(data, window=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def _fetch_ticker_history(ticker, period="2y", max_retries=3):
    """משיכת נתוני טיקר עם ניסיונות חוזרים וטיפול בשגיאות אימות (401)."""
    for attempt in range(max_retries):
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            stock = yf.Ticker(ticker, session=session)
            hist = stock.history(period=period)
            return hist
        except (requests.exceptions.HTTPError, Exception) as e:
            error_msg = str(e).lower()
            is_auth_error = any(
                keyword in error_msg
                for keyword in ['401', 'unauthorized', 'authentication', 'token', 'expired', 'oauth']
            )
            if is_auth_error and attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                time.sleep(wait_time)
                continue
            raise
    return pd.DataFrame()


@st.cache_data(ttl=3600)
def load_sector_data():
    performance = []
    auth_errors = 0

    for ticker, name in SECTORS.items():
        try:
            hist = _fetch_ticker_history(ticker)

            if not hist.empty and len(hist) > 50:
                # מחירי סגירה
                close_prices = hist['Close']
                first_price = close_prices.iloc[0]
                last_price = close_prices.iloc[-1]

                # חישוב תשואה (שנתיים)
                pct_change = ((last_price - first_price) / first_price) * 100

                # חישוב RSI נוכחי
                rsi_series = calculate_rsi(close_prices)
                current_rsi = rsi_series.iloc[-1]

                # חישוב מרחק מממוצע נע 50
                sma50 = close_prices.rolling(window=50).mean().iloc[-1]
                dist_sma50 = ((last_price - sma50) / sma50) * 100

                performance.append({
                    'סימול': ticker,
                    'שם הסקטור': name,
                    'תשואה בשנתיים (%)': round(pct_change, 2),
                    'RSI (14 ימים)': round(current_rsi, 2),
                    'מרחק מ-SMA 50 (%)': round(dist_sma50, 2)
                })
        except Exception as e:
            error_msg = str(e).lower()
            if any(k in error_msg for k in ['401', 'unauthorized', 'authentication', 'token', 'expired', 'oauth']):
                auth_errors += 1
                st.warning(f"שגיאת אימות (401) עבור {ticker} – הטוקן פג תוקף. מנסה שוב בטעינה הבאה.")
            else:
                st.warning(f"שגיאה במשיכת נתונים עבור {ticker}: {e}")

    if auth_errors > len(SECTORS) // 2:
        st.error("רוב הסקטורים נכשלו עקב שגיאת אימות. נסה לרענן את הדף.")
        st.cache_data.clear()

    # יצירת DataFrame וסידור לפי התשואה
    df = pd.DataFrame(performance)
    if not df.empty:
        df = df.sort_values(by='תשואה בשנתיים (%)', ascending=False).reset_index(drop=True)
        df.index = df.index + 1
        df.index.name = 'דירוג'

    return df

# --- עיצוב ממשק המשתמש בדשבורד ---
st.set_page_config(page_title="דירוג וניתוח סקטורים בארה\"ב", layout="wide")

st.title('📊 דשבורד דירוג סקטורים ואינדיקטורים טכניים')
st.markdown('השוואת 11 הסקטורים של מדד ה-S&P 500 על סמך תשואות העבר (שנתיים) ומדדי מומנטום טכניים (RSI וממוצעים נעים).')

# טעינת הנתונים
with st.spinner('מנתח נתונים ואינדיקטורים מהבורסה...'):
    df_sectors = load_sector_data()

# חלוקה לעמודות עבור התצוגה
col1, col2 = st.columns([1, 1])

with col1:
    st.subheader('טבלת דירוג ומומנטום')
    # עיצוב הטבלה (צביעת ערכים חיובים בירוק ושליליים באדום)
    st.dataframe(
        df_sectors.style.applymap(
            lambda x: 'color: green' if x > 0 else 'color: red', 
            subset=['תשואה בשנתיים (%)', 'מרחק מ-SMA 50 (%)']
        ).applymap(
            lambda x: 'color: red' if x > 70 else ('color: green' if x < 30 else 'color: orange'),
            subset=['RSI (14 ימים)']
        ),
        use_container_width=True, height=450
    )
    st.caption("* RSI מעל 70 נחשב לקניית יתר (אדום), מתחת ל-30 למכירת יתר (ירוק).")

with col2:
    st.subheader('השוואת סקטורים: תשואה מול RSI')
    # יצירת גרף בועות/פיזור צבעוני
    fig_scatter = px.scatter(
        df_sectors,
        x='RSI (14 ימים)',
        y='תשואה בשנתיים (%)',
        color='שם הסקטור',
        size=[15]*len(df_sectors), # גודל בועות קבוע
        hover_name='שם הסקטור',
        text='סימול',
        title='מיפוי סקטורים: מומנטום נוכחי (RSI) לעומת תשואה ארוכת טווח',
        template='plotly_white'
    )
    
    # הוספת קווי עזר ל-RSI
    fig_scatter.add_vline(x=70, line_dash="dash", line_color="red", annotation_text="קניית יתר")
    fig_scatter.add_vline(x=30, line_dash="dash", line_color="green", annotation_text="מכירת יתר")
    fig_scatter.update_traces(textposition='top center')
    
    st.plotly_chart(fig_scatter, use_container_width=True)

st.divider()

# גרף עמודות צבעוני להשוואת המרחק מהממוצע הנע
st.subheader('מגמה לטווח הקצר: מרחק מממוצע נע 50 ימים (SMA 50)')
fig_bar = px.bar(
    df_sectors.sort_values(by='מרחק מ-SMA 50 (%)', ascending=False),
    x='שם הסקטור',
    y='מרחק מ-SMA 50 (%)',
    text='מרחק מ-SMA 50 (%)',
    color='מרחק מ-SMA 50 (%)',
    color_continuous_scale='Geyser', # סקאלת צבעים שמתאימה לחיובי/שלילי
    title='איזה סקטור "ברח" כלפי מעלה ואיזה נשאר מאחור?'
)
fig_bar.update_traces(textposition='outside')
fig_bar.update_layout(xaxis_tickangle=-45)

st.plotly_chart(fig_bar, use_container_width=True)