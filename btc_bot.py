import os
import json
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import pytz
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ACTIVE_TRADE_PATH = os.path.join(BASE_DIR, "active_trade.json")
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://api.alpaca.markets")
HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}
SYMBOL = "BTC/USD"
def log(msg):
    timestamp = datetime.now(pytz.timezone('America/New_York')).strftime("%Y-%m-%d %H:%M:%S EST")
    print(f"[{timestamp}] {msg}")
def check_credentials():
    if not API_KEY or not SECRET_KEY:
        log("ERROR: Las variables de entorno ALPACA_API_KEY o ALPACA_SECRET_KEY no están configuradas.")
        return False
    return True
def get_alpaca_account():
    url = f"{BASE_URL}/v2/account"
    r = requests.get(url, headers=HEADERS, verify=False)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            raise ValueError(f"Error decodificando cuenta JSON: {r.text[:200]}")
    raise ValueError(f"Error consultando cuenta Alpaca: {r.text[:200]}")
def get_positions():
    url = f"{BASE_URL}/v2/positions"
    r = requests.get(url, headers=HEADERS, verify=False)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return []
    return []
def get_btc_position():
    positions = get_positions()
    for pos in positions:
        if pos['symbol'] in ['BTCUSD', 'BTC/USD']:
            return pos
    return None
def submit_order(symbol, qty, side, order_type="market", limit_price=None):
    url = f"{BASE_URL}/v2/orders"
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": "gtc"
    }
    if limit_price is not None:
        payload["limit_price"] = f"{limit_price:.2f}"
        
    r = requests.post(url, headers=HEADERS, json=payload, verify=False)
    if r.status_code in [200, 201]:
        return r.json()
    else:
        raise ValueError(f"Error enviando orden a Alpaca ({symbol}): {r.text}")
def cancel_order(order_id):
    url = f"{BASE_URL}/v2/orders/{order_id}"
    requests.delete(url, headers=HEADERS, verify=False)
def get_open_orders():
    url = f"{BASE_URL}/v2/orders?status=open&symbols=BTCUSD,BTC/USD"
    r = requests.get(url, headers=HEADERS, verify=False)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return []
    return []
def get_current_data(period="2d", interval="5m"):
    session = requests.Session()
    session.verify = False
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    
    df = yf.download("BTC-USD", period=period, interval=interval, session=session)
    if df.empty:
        raise ValueError("No se pudieron descargar datos de BTC-USD de Yahoo Finance.")
        
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    if df.index.tz is None:
        df = df.tz_localize('UTC').tz_convert('America/New_York')
    else:
        df = df.tz_convert('America/New_York')
    return df.sort_index()
def main():
    if not check_credentials():
        return
        
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    else:
        config = {
            "profit_target_pct": 3.0,
            "risk_per_trade_pct": 2.0,
            "active": True
        }
    if not config.get("active", True):
        log("El bot de Bitcoin está desactivado en config.json. Saliendo.")
        return
    log("Ejecutando escaneo continuo 24/7 de Bitcoin...")
    
    # 1. Consultar si tenemos posición activa de Bitcoin en Alpaca
    pos = get_btc_position()
    open_orders = get_open_orders()
    
    active_trade = {}
    if os.path.exists(ACTIVE_TRADE_PATH):
        try:
            with open(ACTIVE_TRADE_PATH, 'r') as f:
                active_trade = json.load(f)
        except Exception:
            active_trade = {}
    # A) GESTIÓN DE POSICIÓN ACTIVA: MODO "SOLO GANANCIA"
    if pos is not None and float(pos.get('qty', 0)) > 0:
        qty_held = float(pos['qty'])
        avg_entry = float(pos['avg_entry_price'])
        
        # Descargar último precio
        try:
            df = get_current_data(period="1d")
            current_price = float(df['Close'].iloc[-1])
        except Exception as e:
            log(f"Error descargando precio actual: {e}")
            return
            
        target_pct = config.get("profit_target_pct", 3.0)
        tp_price = active_trade.get("tp_price", avg_entry * (1 + target_pct / 100.0))
        unrealized_pct = ((current_price - avg_entry) / avg_entry) * 100.0
        
        log(f"Posición BTC Activa: {qty_held:.5f} BTC | Entrada: ${avg_entry:.2f} | Actual: ${current_price:.2f} ({unrealized_pct:+.2f}%) | Objetivo TP: ${tp_price:.2f}")
        
        # Comprobar si alcanzamos la ganancia objetivo
        if current_price >= tp_price:
            log(f"🎉 ¡OBJETIVO DE GANANCIA ALCANZADO (+{unrealized_pct:.2f}%)! Vendiendo posición...")
            for order in open_orders:
                cancel_order(order['id'])
                
            qty_sell = round(qty_held, 5)
            try:
                res = submit_order(SYMBOL, qty_sell, "sell", "market")
                log(f"Venta con GANANCIA completada. ID Orden: {res['id']}")
                if os.path.exists(ACTIVE_TRADE_PATH):
                    os.remove(ACTIVE_TRADE_PATH)
            except Exception as e:
                log(f"Error cerrando posición con ganancia: {e}")
        else:
            log("Precio actual por debajo del objetivo. Manteniendo Satoshis en cuenta (NO SE VENDE EN PÉRDIDA).")
        return
    # Si no hay posición activa pero quedó un archivo viejo, limpiarlo
    if os.path.exists(ACTIVE_TRADE_PATH):
        os.remove(ACTIVE_TRADE_PATH)
    # B) MONITOREO DE ENTRADA 24/7 (Ruptura Alcista + FVG en velas de 5m)
    try:
        df = get_current_data(period="2d", interval="5m")
    except Exception as e:
        log(f"Error descargando datos: {e}")
        return
    # Calcular rango de las últimas 24 velas (últimas 2 horas)
    recent_candles = df.iloc[-24:].copy()
    if len(recent_candles) < 10:
        log("Esperando más datos para escaneo.")
        return
    # Nivel de resistencia reciente (máximo de las últimas 2 horas sin contar la vela actual)
    resistance_high = float(recent_candles['High'].iloc[:-1].max())
    
    # Velas de confirmación de 3 períodos
    c_t = recent_candles.iloc[-1]
    c_t1 = recent_candles.iloc[-2]
    c_t2 = recent_candles.iloc[-3]
    
    close_t = float(c_t['Close'])
    close_t1 = float(c_t1['Close'])
    low_t = float(c_t['Low'])
    high_t2 = float(c_t2['High'])
    
    log(f"Resistencia 2h: ${resistance_high:.2f} | Cierre Actual: ${close_t:.2f}")
    
    # Ruptura Alcista + FVG
    is_breakout = (close_t > resistance_high or close_t1 > resistance_high)
    is_fvg = (low_t > high_t2)
    
    if is_breakout and is_fvg:
        log(f"¡SEÑAL ALCISTA 24/7 DETECTADA EN BITCOIN (Ruptura + FVG)!")
        
        # Consultar saldo disponible
        try:
            acc = get_alpaca_account()
            cash_balance = float(acc['cash'])
            log(f"Saldo en Efectivo en Alpaca: ${cash_balance:.2f} USD")
        except Exception as e:
            log(f"Error consultando cuenta: {e}")
            return
            
        capital_base = min(300.0, cash_balance)
        if capital_base < 10.0:
            log("Capital insuficiente para operar (mínimo $10 USD).")
            return
            
        usd_to_invest = min(capital_base, cash_balance * 0.95)
        qty_to_buy = round(usd_to_invest / close_t, 5)
        
        if qty_to_buy < 0.00001:
            log("Cantidad de BTC calculada muy pequeña.")
            return
            
        target_pct = config.get("profit_target_pct", 3.0)
        tp_price = round(close_t * (1 + target_pct / 100.0), 2)
        
        log(f"Comprando {qty_to_buy:.5f} BTC (~${usd_to_invest:.2f} USD). Objetivo Take Profit: ${tp_price:.2f} (+{target_pct}%)...")
        try:
            order = submit_order(SYMBOL, qty_to_buy, "buy", "market")
            log(f"Compra ejecutada con éxito. ID: {order['id']}")
            
            # Guardar estado de la operación
            with open(ACTIVE_TRADE_PATH, 'w') as f:
                json.dump({
                    "entry_price": close_t,
                    "tp_price": tp_price,
                    "qty": qty_to_buy,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            log(f"Error ejecutando compra: {e}")
if __name__ == "__main__":
    main()
