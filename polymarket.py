import requests
import time
import os
import json
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

# ==================== KONFIGURASI ====================
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

AUTO_EXECUTE = True           # True = kirim order sungguhan
DRY_RUN = False                # True = simulasi saja (disarankan saat testing)
TRADE_AMOUNT = 1.0            # ukuran order dalam token/share

STRATEGY = "LIMIT_SELL"       # Pilihan: "LIMIT_SELL", "MARKET_SELL", "MARKET_BUY"

# Parameter untuk LIMIT_SELL (strategi paling realistis saat ini)
MIN_PROFIT_TARGET = 0.012     # minimal profit yang diinginkan (1.2%)
TARGET_PRICE_OFFSET = 0.003   # berapa jauh di atas best bid saat ini

PRICE_FETCH_DELAY = 0.45
MAX_ORDERS_PER_RUN = 4

# =============================================================

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if not PRIVATE_KEY:
    print("⚠️ PRIVATE_KEY tidak ditemukan → mode READ-ONLY")
    AUTO_EXECUTE = False

client = ClobClient(
    host=CLOB_HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=2,
    funder="isi adress polymarket"  # sesuaikan jika perlu
)

if PRIVATE_KEY:
    try:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("✓ Client authenticated ✓\n")
    except Exception as e:
        print(f"✗ Gagal autentikasi: {e}")
        AUTO_EXECUTE = False
else:
    AUTO_EXECUTE = False

# =============================================================
# Helper Functions
# =============================================================

def fetch_active_markets(max_markets=200):
    markets = []
    offset = 0
    print("Mengambil daftar market aktif...")
    
    while len(markets) < max_markets:
        try:
            url = f"{GAMMA_API_BASE}/markets?closed=false&limit=100&offset={offset}"
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            markets.extend(batch)
            offset += 100
            time.sleep(0.4)
        except Exception as e:
            print(f"Error fetch markets: {e}")
            break
            
    print(f"→ Total market aktif: {len(markets)}")
    return markets


def is_binary_yes_no_market(market):
    try:
        outcomes = json.loads(market.get('outcomes', '[]'))
        return len(outcomes) == 2 and set(o.lower().strip() for o in outcomes) == {'yes', 'no'}
    except:
        return False


def get_yes_no_tokens(market):
    try:
        outcomes = json.loads(market.get('outcomes', '[]'))
        tokens = json.loads(market.get('clobTokenIds', '[]'))
        
        yes_token = no_token = None
        for o, t in zip(outcomes, tokens):
            if o.lower().strip() == 'yes':
                yes_token = t
            elif o.lower().strip() == 'no':
                no_token = t
                
        return yes_token, no_token
    except:
        return None, None


def get_best_price(token_id, side):  # side: "BUY" atau "SELL"
    try:
        price = client.get_price(token_id=token_id, side=side)
        if isinstance(price, dict) and 'price' in price:
            return float(price['price'])
        if isinstance(price, (str, float)):
            return float(price)
        return None
    except Exception as e:
        print(f"  Gagal ambil harga {side} untuk {token_id}: {e}")
        return None


def place_limit_order(token_id, price, amount, side):
    if DRY_RUN:
        print(f"[DRY-RUN] Limit {side} {amount:.2f} @ {price:.4f}")
        return True, "dry-run-id"

    try:
        # Pastikan tipe data benar
        price_f = float(price)
        amount_f = float(amount)
        
        # Safety check
        if not (0 < price_f <= 1):
            raise ValueError(f"Harga tidak valid: {price_f}")
        if amount_f <= 0:
            raise ValueError("Jumlah order harus positif")

        order_args = OrderArgs(
            token_id=str(token_id),      # pastikan string
            price=price_f,
            size=amount_f,
            side=side
        )
        
        print(f"  Mengirim LIMIT {side} {amount_f:.2f} @ {price_f:.4f}...")
        
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order)
        
        status = resp.get('status', 'unknown')
        order_id = resp.get('orderID', resp.get('id', 'unknown'))
        
        print(f"  → Status: {status} | Order ID: {order_id}")
        return True, order_id
    
    except Exception as e:
        print(f"  ✗ Gagal pasang LIMIT {side}: {str(e)}")
        return False, None


# =============================================================
# Strategi Arbitrage
# =============================================================

def try_limit_sell_arb(yes_token, no_token, question):
    yes_bid = get_best_price(yes_token, "SELL")   # best price someone willing to buy Yes
    no_bid  = get_best_price(no_token,  "SELL")   # best price someone willing to buy No

    if yes_bid is None or no_bid is None:
        return False, "tidak bisa mendapatkan harga bid"

    # Kita coba jual sedikit lebih tinggi dari best bid saat ini
    sell_yes_at = round(yes_bid + TARGET_PRICE_OFFSET, 4)
    sell_no_at  = round(no_bid  + TARGET_PRICE_OFFSET, 4)

    total_receive = sell_yes_at + sell_no_at
    profit_per_unit = total_receive - 1.0

    if profit_per_unit < MIN_PROFIT_TARGET:
        return False, f"profit terlalu kecil ({profit_per_unit:.4f})"

    print(f"\n→ Potensi LIMIT SELL arbitrage ditemukan!")
    print(f"   Sell Yes @ {sell_yes_at:.4f}  (best bid saat ini: {yes_bid:.4f})")
    print(f"   Sell No  @ {sell_no_at:.4f}   (best bid saat ini: {no_bid:.4f})")
    print(f"   Total receive estimasi: {total_receive:.4f} → profit: +${profit_per_unit:.4f}\n")

    if not AUTO_EXECUTE:
        return True, "simulasi saja (AUTO_EXECUTE=False)"

    ok1, _ = place_limit_order(yes_token, sell_yes_at, TRADE_AMOUNT, SELL)
    time.sleep(1.5)
    ok2, _ = place_limit_order(no_token,  sell_no_at,  TRADE_AMOUNT, SELL)

    if ok1 and ok2:
        return True, "kedua limit order berhasil dikirim"
    else:
        return False, "gagal mengirim salah satu atau kedua order"


# =============================================================
# Main Loop
# =============================================================

def main_loop():
    print(f"\n=== Polymarket Arbitrage Bot ===  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Strategi: {STRATEGY} | Amount: {TRADE_AMOUNT} | Auto: {AUTO_EXECUTE} | Dry-run: {DRY_RUN}\n")

    markets = fetch_active_markets()
    binary_markets = [m for m in markets 
                     if is_binary_yes_no_market(m) 
                     and m.get('enableOrderBook', False)]

    print(f"Binary tradable markets ditemukan: {len(binary_markets)}\n")

    count_executed = 0

    for market in binary_markets:
        if count_executed >= MAX_ORDERS_PER_RUN:
            print("Mencapai batas maksimal order per run")
            break

        question = market.get('question', '???')[:70]
        yes_token, no_token = get_yes_no_tokens(market)

        if not (yes_token and no_token):
            continue

        time.sleep(PRICE_FETCH_DELAY)

        success = False
        message = ""

        if STRATEGY == "LIMIT_SELL":
            success, message = try_limit_sell_arb(yes_token, no_token, question)
        else:
            message = f"Strategi {STRATEGY} belum diimplementasikan"

        if success:
            count_executed += 1
            print(f"✓ {count_executed}/{MAX_ORDERS_PER_RUN} → {question[:50]}... {message}")
        else:
            print(f"  - {question[:50]}... → {message}")

        time.sleep(2.5)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nDihentikan oleh user.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
