import os
import json
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import yfinance as yf
import pandas as pd
from datetime import datetime, time, timedelta
import pytz

# Directorio del proyecto
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ACTIVE_TRADE_PATH = os.path.join(BASE_DIR, "active_trade.json")
LAST_TRADE_PATH = os.path.join(BASE_DIR, "last_trade.json")

# Leer credenciales desde variables de entorno
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}

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
        return r.json()
    else:
        raise ValueError(f"Error consultando cuenta en Alpaca: {r.text}")

def get_positions():
    url = f"{BASE_URL}/v2/positions"
    r = requests.get(url, headers=HEADERS, verify=False)
    if r.status_code == 200:
        return r.json()
    return []

def get_position(symbol="BTC/USD"):
    positions = get_positions()
    for pos in positions:
        if pos['symbol'] == symbol:
            return pos
    return None

def submit_order(symbol, qty, side, order_type="market", stop_price=None, time_in_force="gtc"):
    url = f"{BASE_URL}/v2/orders"
    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force
    }
    if stop_price:
        payload["stop_price"] = f"{stop_price:.2f}"
        
    r = requests.post(url, headers=HEADERS, json=payload, verify=False)
    if r.status_code in [200, 201]:
        return r.json()
    else:
        raise ValueError(f"Error enviando orden a Alpaca: {r.text}")

def cancel_order(order_id):
    url = f"{BASE_URL}/v2/orders/{order_id}"
    r = requests.delete(url, headers=HEADERS, verify=False)
    if r.status_code == 204:
        log(f"Orden {order_id} cancelada exitosamente.")
        return True
    else:
        log(f"Error cancelando orden {order_id}: {r.text}")
        return False

def get_current_data(ticker="BTC-USD", period="1d", interval="5m"):
    session = requests.Session()
    session.verify = False
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    
    df = yf.download(ticker, period=period, interval=interval, session=session)
    if df.empty:
        raise ValueError("No se pudieron descargar datos recientes para BTC-USD.")
        
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
        
    # 1. Cargar Configuración
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    else:
        log("Archivo config.json no encontrado. Usando valores por defecto.")
        config = {
            "r_factor": 2.0,
            "risk_per_trade_pct": 2.0,
            "session_start_est": "03:00",
            "session_range_end_est": "03:15",
            "session_end_est": "10:00",
            "active": true
        }

    if not config.get("active", True):
        log("El bot está desactivado en config.json. Saliendo.")
        return

    # Definición de horarios en hora de Nueva York (EST)
    tz_ny = pytz.timezone('America/New_York')
    now = datetime.now(tz_ny)
    current_date = now.date()
    current_time = now.time()
    
    t_start = datetime.strptime(config["session_start_est"], "%H:%M").time()
    t_range_end = datetime.strptime(config["session_range_end_est"], "%H:%M").time()
    t_end = datetime.strptime(config["session_end_est"], "%H:%M").time()
    
    # 2. Comprobar si hay una posición activa en Alpaca
    position = get_position("BTC/USD")
    
    # Cargar estado local de trade activo
    active_trade = {}
    if os.path.exists(ACTIVE_TRADE_PATH):
        with open(ACTIVE_TRADE_PATH, 'r') as f:
            active_trade = json.load(f)

    # A) GESTIÓN DE POSICIÓN ACTIVA
    if position is not None:
        log(f"Posición activa de BTC/USD detectada: {position['qty']} BTC.")
        
        # Si no tenemos registro del trade local, intentar reconstruir o salir
        if not active_trade or not active_trade.get("in_trade", False):
            log("Alerta: Posición en Alpaca sin registro local. Creando registro de emergencia.")
            active_trade = {
                "in_trade": True,
                "type": "Long" if float(position['qty']) > 0 else "Short",
                "entry_price": float(position['avg_entry_price']),
                "sl": float(position['avg_entry_price']) * 0.98, # Stop de emergencia al 2%
                "tp": float(position['avg_entry_price']) * 1.04, # TP de emergencia al 4%
                "stop_order_id": None,
                "qty": abs(float(position['qty']))
            }
            with open(ACTIVE_TRADE_PATH, 'w') as f:
                json.dump(active_trade, f, indent=2)

        # Descargar último precio para monitorear Take Profit
        df = get_current_data()
        current_price = float(df['Close'].iloc[-1])
        log(f"Monitoreando posición. Precio actual BTC: ${current_price:.2f} | TP: ${active_trade['tp']:.2f} | SL: ${active_trade['sl']:.2f}")

        # Comprobar si se alcanzó la hora límite de la sesión (10:00 AM EST) o el Take Profit
        is_tp_hit = False
        if active_trade['type'] == 'Long' and current_price >= active_trade['tp']:
            log("¡Take Profit alcanzado en Long!")
            is_tp_hit = True
        elif active_trade['type'] == 'Short' and current_price <= active_trade['tp']:
            log("¡Take Profit alcanzado en Short!")
            is_tp_hit = True

        is_time_limit_hit = (current_time >= t_end)
        
        if is_tp_hit or is_time_limit_hit:
            reason = "TakeProfit" if is_tp_hit else "Fin de Sesión (10:00 EST)"
            log(f"Cerrando posición. Razón: {reason}")
            
            # 1. Cancelar orden Stop Loss en Alpaca
            if active_trade.get("stop_order_id"):
                cancel_order(active_trade["stop_order_id"])
                
            # 2. Enviar orden de cierre a mercado
            side_close = "sell" if active_trade['type'] == 'Long' else "buy"
            try:
                res_close = submit_order("BTC/USD", active_trade["qty"], side_close, "market")
                log(f"Posición cerrada. ID Orden: {res_close['id']}")
            except Exception as e:
                log(f"ERROR cerrando posición: {e}")
                
            # Limpiar archivo de estado local
            if os.path.exists(ACTIVE_TRADE_PATH):
                os.remove(ACTIVE_TRADE_PATH)
        return

    # Si no hay posición activa pero el archivo local dice que sí, limpiar estado local
    if active_trade:
        log("Limpia de estado local: No hay posición en Alpaca pero existía registro local.")
        if os.path.exists(ACTIVE_TRADE_PATH):
            os.remove(ACTIVE_TRADE_PATH)

    # B) GESTIÓN FUERA DE HORARIO
    # Si estamos fuera del rango de la sesión, no buscar nuevas entradas
    if current_time < t_start or current_time >= t_end:
        log("Fuera de horario de la sesión de Londres (03:00 - 10:00 EST). Esperando...")
        return

    # C) MONITOREO Y BÚSQUEDA DE ENTRADAS (03:15 - 10:00 EST)
    if current_time >= t_range_end:
        # Cargar registro de último trade
        last_trade_date = ""
        if os.path.exists(LAST_TRADE_PATH):
            with open(LAST_TRADE_PATH, 'r') as f:
                last_trade_date = json.load(f).get("last_trade_date", "")

        if last_trade_date == str(current_date):
            log("Hoy ya se ejecutó un trade. Límite de 1 operación por día alcanzado.")
            return

        # Descargar velas de 5m
        try:
            df = get_current_data()
        except Exception as e:
            log(f"Error descargando datos de mercado: {e}")
            return

        # Filtrar velas del día actual de la sesión
        session_candles = df[df.index.date == current_date]
        
        # Extraer rango de apertura (03:00 a 03:15 EST)
        opening_range = session_candles[
            (session_candles.index.time >= t_start) & (session_candles.index.time <= t_range_end)
        ]
        
        if len(opening_range) < 3:
            log("Esperando a que se completen las velas del rango de apertura (03:00 - 03:15 EST)...")
            return
            
        range_high = float(opening_range['High'].max())
        range_low = float(opening_range['Low'].min())
        midpoint = (range_high + range_low) / 2.0
        
        log(f"Rango de Apertura Detectado: Alto = ${range_high:.2f} | Bajo = ${range_low:.2f} | Medio = ${midpoint:.2f}")

        # Comprobar últimas velas después del rango de apertura
        post_range_candles = session_candles[session_candles.index.time > t_range_end]
        if post_range_candles.empty:
            log("Esperando señales después de las 03:15 EST...")
            return

        last_close = float(post_range_candles['Close'].iloc[-1])
        log(f"Último precio de cierre: ${last_close:.2f}")

        # Buscar señales de ruptura
        signal = None
        if last_close > range_high:
            signal = "Long"
        elif last_close < range_low:
            signal = "Short"

        if signal:
            log(f"¡Señal de ruptura {signal} detectada a las {post_range_candles.index[-1].time()}!")
            
            # Consultar saldo disponible en Alpaca
            try:
                acc = get_alpaca_account()
                buying_power = float(acc['cash'])
                log(f"Balance de efectivo disponible en Alpaca: ${buying_power:.2f} USD")
            except Exception as e:
                log(f"Error consultando cuenta de Alpaca: {e}")
                return

            # Calcular tamaño de la posición
            # Usaremos una cuenta base de $300 (o el cash actual si es menor)
            capital_base = min(300.0, buying_power)
            if capital_base < 10.0:
                log("ERROR: Capital disponible insuficiente para abrir posición (mínimo $10 USD).")
                return

            risk_usd = capital_base * (config["risk_per_trade_pct"] / 100.0)
            price_risk = abs(last_close - midpoint)
            
            if price_risk <= 0:
                log("Error: Riesgo en precio es cero. Abortando entrada.")
                return

            # qty = riesgo_usd / riesgo_en_precio
            qty = risk_usd / price_risk
            
            # Asegurar que el costo total de la posición no supere el efectivo disponible
            costo_total = qty * last_close
            if costo_total > buying_power:
                qty = buying_power / last_close * 0.98 # 98% del poder de compra para evitar márgenes
                log(f"Ajustando tamaño por límite de capital. Nueva cantidad: {qty:.6f} BTC")

            # Redondear cantidad a 5 decimales (precisión estándar de BTC)
            qty = round(qty, 5)
            if qty <= 0.00001:
                log("ERROR: Cantidad calculada muy pequeña para operar en Alpaca.")
                return

            # Definir niveles de SL y TP
            sl_price = midpoint
            tp_price = last_close + config["r_factor"] * (last_close - midpoint) if signal == "Long" else last_close - config["r_factor"] * (midpoint - last_close)

            # 1. Enviar orden de ENTRADA a mercado
            side_entry = "buy" if signal == "Long" else "sell"
            log(f"Enviando orden de Entrada ({signal}) a Alpaca: {qty} BTC @ mercado...")
            try:
                entry_order = submit_order("BTC/USD", qty, side_entry, "market")
                log(f"Entrada ejecutada con éxito. ID: {entry_order['id']}")
            except Exception as e:
                log(f"ERROR al enviar orden de entrada: {e}")
                return

            # Esperar a que se actualice la posición y obtener el precio de ejecución real
            # Para simplificar y proteger de inmediato, asumimos el precio del trigger
            log(f"Estableciendo Stop Loss en Alpaca en ${sl_price:.2f}...")
            
            # 2. Enviar orden de STOP LOSS (Venta Stop) a Alpaca
            side_stop = "sell" if signal == "Long" else "buy"
            try:
                stop_order = submit_order("BTC/USD", qty, side_stop, "stop", stop_price=sl_price)
                stop_order_id = stop_order['id']
                log(f"Stop Loss de protección programado en servidor de Alpaca. ID: {stop_order_id}")
            except Exception as e:
                log(f"CRÍTICO: No se pudo colocar el Stop Loss en el servidor: {e}. Se intentará en la siguiente ejecución del bot.")
                stop_order_id = None

            # 3. Guardar estado local de la transacción activa
            active_trade = {
                "in_trade": True,
                "type": signal,
                "entry_price": last_close,
                "sl": sl_price,
                "tp": tp_price,
                "stop_order_id": stop_order_id,
                "qty": qty
            }
            with open(ACTIVE_TRADE_PATH, 'w') as f:
                json.dump(active_trade, f, indent=2)

            # 4. Registrar fecha del trade del día
            with open(LAST_TRADE_PATH, 'w') as f:
                json.dump({"last_trade_date": str(current_date)}, f, indent=2)

            log(f"Operación registrada: {signal} {qty} BTC | SL: ${sl_price:.2f} | TP: ${tp_price:.2f}")

if __name__ == "__main__":
    main()
