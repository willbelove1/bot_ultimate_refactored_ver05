import streamlit as st
import pandas as pd
import json
import logging
import requests
import re
import os

gemini_api_key = os.environ.get('GEMINI_API_KEY')
telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
telegram_chat_id = os.environ.get('TELEGRAM_GROUP_ID')

# === CẤU HÌNH === #
logging.basicConfig(
    filename='bot_optimizer.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
load_dotenv()

cg = CoinGeckoAPI()
gemini_api_key = os.getenv('GEMINI_API_KEY')
telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
telegram_chat_id = os.getenv('TELEGRAM_GROUP_ID')

# === HÀM HỖ TRỢ === #
def normalize_symbol(symbol: str) -> str:
    return symbol.lower().strip().replace('/', '')

def infer_trend_from_series(series: pd.Series, threshold: float = 1.0) -> str:
    if len(series) < 2:
        return "không đủ dữ liệu"
    delta = series.iloc[-1] - series.iloc[0]
    percent_change = delta / series.iloc[0] * 100
    if percent_change > threshold:
        return 'tăng'
    elif percent_change < -threshold:
        return 'giảm'
    return 'đi ngang'

def check_price_range(current, low, high):
    if current < low:
        return f"⚠️ Giá {current} thấp hơn vùng hoạt động ({low} - {high})"
    if current > high:
        return f"⚠️ Giá {current} cao hơn vùng hoạt động ({low} - {high})"
    return "✅ Giá nằm trong vùng bot hoạt động."

def fetch_market_data(symbol='bitcoin', vs_currency='usdt'):
    try:
        try:
            data = cg.get_price(ids=symbol, vs_currencies=vs_currency, include_market_cap=True, include_24hr_vol=True)
            price = data[symbol][vs_currency]
        except Exception as e:
            logging.warning(f"Error with {vs_currency}, switching to usd: {e}")
            vs_currency = 'usd'
            data = cg.get_price(ids=symbol, vs_currencies=vs_currency)
            price = data[symbol]['usd']
        chart = cg.get_coin_market_chart_by_id(symbol, vs_currency, days=1)
        df = pd.DataFrame(chart['prices'], columns=['timestamp', 'price'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df, price, vs_currency
    except Exception as e:
        logging.error(f"Lỗi lấy dữ liệu CoinGecko: {e}")
        return None, None, None

def call_gemini_api(user_data, market_data, current_price):
    trend = infer_trend_from_series(market_data['price'])
    headers = {'Content-Type': 'application/json'}
    prompt = f"""
Bạn là chuyên gia phân tích tài chính tiền điện tử cho người Việt Nam. Dựa trên dữ liệu sau, tối ưu hóa bot spot lưới:
- Dữ liệu người dùng: {json.dumps(user_data)}
- Dữ liệu thị trường: Giá hiện tại {current_price} {user_data.get('vs_currency', 'usdt').upper()}, xu hướng {trend}.

Trả lời dưới dạng JSON với cấu trúc chính xác sau:
{{
    "optimization_recommendation": {{
        "action": "mô tả hành động cần thực hiện",
        "reasoning": "giải thích chi tiết lý do",
        "recommended_parameters": {{
            "coin_symbol": "tên coin",
            "capital_allocation_usd": số vốn đề xuất,
            "vs_currency": "tiền tệ",
            "range_low": giá thấp nhất,
            "range_high": giá cao nhất,
            "number_of_grids": số ô lưới,
            "strategy_type": "loại chiến lược",
            "take_profit_target_percent": % lợi nhuận mục tiêu,
            "stop_loss_percent": % cắt lỗ,
            "notes": "ghi chú thêm"
        }}
    }}
}}
"""
    payload = {'contents': [{'parts': [{'text': prompt}]}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={gemini_api_key}"

    try:
        resp = requests.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logging.error(f"Gemini API error: {resp.status_code} - {resp.text}")
            return None
        response_text = resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
        response_text = re.sub(r'^```json\s*|\s*```$', '', response_text).strip()
        return json.loads(response_text)
    except Exception as e:
        logging.warning(f"Lỗi khi phân tích JSON từ Gemini: {e}")
        return None

def send_telegram_message(message: str):
    if not telegram_token or not telegram_chat_id:
        logging.warning("Thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_GROUP_ID")
        return
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    data = {
        "chat_id": telegram_chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, data=data)
        if r.status_code != 200:
            logging.warning(f"Lỗi gửi Telegram: {r.status_code} - {r.text}")
    except Exception as e:
        logging.error(f"Lỗi kết nối Telegram: {e}")

def send_structured_telegram_update(json_data: dict):
    try:
        # Kiểm tra nhiều trường có thể có từ AI response
        rec = None
        if "optimization_recommendation" in json_data:
            rec = json_data["optimization_recommendation"]
        elif "recommendation" in json_data:
            rec = json_data["recommendation"]
        elif "suggestions" in json_data:
            rec = json_data["suggestions"]
        elif "analysis" in json_data:
            rec = json_data["analysis"]
        else:
            # Nếu không tìm thấy trường nào, thử gửi toàn bộ JSON
            logging.warning("Không tìm thấy trường recommendation, gửi raw data")
            send_telegram_message(f"🤖 *Phản hồi từ AI:*\n```json\n{json.dumps(json_data, indent=2, ensure_ascii=False)}\n```")
            return

        action = rec.get("action", "Không rõ")
        reasoning = rec.get("reasoning", "_Không có giải thích nào được cung cấp._")
        params = rec.get("recommended_parameters", rec.get("parameters", {}))

        message = f"""📈 *Gợi ý tối ưu hóa bot* 📊

*🎯 Hành động*: `{action}`
*🧠 Lý do*:
_{reasoning}_

*⚙️ Tham số đề xuất:*
- Coin: `{params.get("coin_symbol", "Không rõ")}`
- Vốn: `{params.get("capital_allocation_usd", "Không rõ")} {params.get("vs_currency", "USD").upper()}`
- Range: `{params.get("range_low", "N/A")} - {params.get("range_high", "N/A")}`
- Grid: `{params.get("number_of_grids", "N/A")} ô | {params.get("strategy_type", "N/A")}`
- Lợi nhuận mục tiêu: `{params.get("take_profit_target_percent", "N/A")}%`
- Cắt lỗ: `{params.get("stop_loss_percent", "N/A")}%`
- Ghi chú: _{params.get("notes", "Không có ghi chú.")}_"""

        send_telegram_message(message)
    except Exception as e:
        logging.warning(f"Lỗi định dạng gửi telegram có cấu trúc: {e}")
        # Fallback: gửi raw JSON
        try:
            send_telegram_message(f"❗ *Lỗi định dạng, raw data:*\n```json\n{json.dumps(json_data, indent=2, ensure_ascii=False)}\n```")
        except:
            send_telegram_message("❗ Không thể phân tích JSON phản hồi từ AI.")

# === GIAO DIỆN STREAMLIT === #
st.title("🤖 Ultra Ultimate Spot Bot")

with st.expander("📲 Kiểm tra kết nối Telegram"):
    if st.button("🚀 Gửi test message"):
        send_telegram_message("✅ Bot Telegram đã kết nối thành công!")
        st.success("Đã gửi test message tới Telegram.")

with st.expander("🛠️ Tạo bot mới"):
    col1, col2 = st.columns(2)
    
    with col1:
        symbol = normalize_symbol(st.text_input("Tên coin (VD: bitcoin)", "bitcoin"))
        vs_currency = st.text_input("Tiền tệ (VD: usdt)", "usdt").lower()
    
    with col2:
        initial_capital = st.number_input("Vốn ban đầu", value=100.0, min_value=1.0, step=10.0)
        st.info("💡 AI sẽ đưa ra gợi ý dựa trên số vốn này")

    if st.button("📊 Lấy dữ liệu và phân tích"):
        df, price, currency = fetch_market_data(symbol, vs_currency)
        if df is not None:
            st.success(f"Giá hiện tại: {price} {currency.upper()}")
            st.line_chart(df.set_index('timestamp')['price'])

            user_data = {
                'mode': 'new_bot',
                'coin_symbol': symbol,
                'initial_capital': initial_capital,
                'vs_currency': currency,
                'current_price': price
            }
            suggestions = call_gemini_api(user_data, df, price)
            if suggestions:
                st.json(suggestions)
                send_structured_telegram_update(suggestions)
            else:
                st.error("Không nhận được phản hồi AI.")

with st.expander("📈 Phân tích bot hiện tại"):
    col1, col2 = st.columns(2)
    
    with col1:
        coin_symbol = normalize_symbol(st.text_input("Coin đang chạy", "bitcoin"))
        capital = st.number_input("Vốn", value=100.0)
        range_low = st.number_input("Giá thấp nhất", value=100.0)
        range_high = st.number_input("Giá cao nhất", value=110.0)
    
    with col2:
        pnl = st.number_input("Tổng PNL", value=1.0)
        profit_percent = st.number_input("Lợi nhuận %", value=1.0)
        open_orders = st.number_input("Số lệnh mở", value=5)
        vs_currency_existing = st.text_input("Tiền tệ bot hiện tại", "usdt").lower()

    user_data = {
        'mode': 'existing_bot',
        'coin_symbol': coin_symbol,
        'capital': capital,
        'range_low': range_low,
        'range_high': range_high,
        'pnl': pnl,
        'profit_percent': profit_percent,
        'open_orders': open_orders,
        'vs_currency': vs_currency_existing
    }

    if st.button("🔍 Phân tích bot"):
        df, price, currency = fetch_market_data(user_data['coin_symbol'], user_data['vs_currency'])
        if df is not None:
            st.success(f"Giá hiện tại: {price} {currency.upper()}")
            st.line_chart(df.set_index('timestamp')['price'])

            deviation_msg = check_price_range(price, user_data['range_low'], user_data['range_high'])
            st.info(deviation_msg)

            user_data['vs_currency'] = currency
            user_data['current_price'] = price
            suggestions = call_gemini_api(user_data, df, price)
            if suggestions:
                st.json(suggestions)
                send_structured_telegram_update(suggestions)
            else:
                st.error("AI chưa trả lời hợp lệ.")

if __name__ == '__main__':
    st.sidebar.header("📅 Thông tin")
    st.sidebar.write(f"🕒 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}")
