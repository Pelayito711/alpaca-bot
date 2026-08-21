import os
import json
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta

# Configuración de directorios
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

def download_data(ticker="BTC-USD", period="30d", interval="5m"):
    """
    Descarga datos históricos de velas de 5m de Yahoo Finance.
    """
    session = requests.Session()
    session.verify = False
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    print(f"Descargando datos históricos para la optimización de {ticker}...")
    df = yf.download(ticker, period=period, interval=interval, session=session)
    if df.empty:
        raise ValueError("No se pudieron obtener datos desde Yahoo Finance.")
        
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
        
    if df.index.tz is None:
        df = df.tz_localize('UTC').tz_convert('America/New_York')
    else:
        df = df.tz_convert('America/New_York')
        
    return df.sort_index()

def parse_time(t_str):
    return datetime.strptime(t_str, "%H:%M").time()

def run_simulation(df, start_time_str, range_end_str, end_time_str, r_factor, risk_per_trade):
    """
    Simulación simplificada de Classic ORB en la sesión de Londres para optimizar parámetros.
    """
    t_start = parse_time(start_time_str)
    t_range_end = parse_time(range_end_str)
    t_end = parse_time(end_time_str)
    
    trading_days = []
    for timestamp in df.index:
        current_time = timestamp.time()
        # Londres cruza de 03:00 AM a 10:00 AM, no cruza la medianoche (es el mismo día en EST)
        if t_start <= current_time <= t_end:
            trading_days.append(timestamp.date())
        else:
            trading_days.append(None)
            
    df = df.copy()
    df['TradingDay'] = trading_days
    df = df.dropna(subset=['TradingDay'])
    
    grouped = df.groupby('TradingDay')
    trades = []
    
    for day, group in grouped:
        group = group.sort_index()
        opening_range = group[
            (group.index.time >= t_start) & (group.index.time <= t_range_end)
        ]
        
        if len(opening_range) < 3:
            continue
            
        range_high = float(opening_range['High'].max())
        range_low = float(opening_range['Low'].min())
        midpoint = (range_high + range_low) / 2.0
        
        post_range = group[group.index.time > t_range_end]
        in_trade = False
        trade_status = {}
        
        for idx in range(len(post_range)):
            candle = post_range.iloc[idx]
            timestamp = post_range.index[idx]
            current_time = timestamp.time()
            
            if in_trade:
                high_price = float(candle['High'])
                low_price = float(candle['Low'])
                close_price = float(candle['Close'])
                
                if trade_status['Type'] == 'Long':
                    if low_price <= trade_status['SL']:
                        trades.append((trade_status['SL'] - trade_status['EntryPrice']) / trade_status['EntryPrice'])
                        in_trade = False
                        break
                    elif high_price >= trade_status['TP']:
                        trades.append((trade_status['TP'] - trade_status['EntryPrice']) / trade_status['EntryPrice'])
                        in_trade = False
                        break
                else: # Short
                    if high_price >= trade_status['SL']:
                        trades.append((trade_status['EntryPrice'] - trade_status['SL']) / trade_status['EntryPrice'])
                        in_trade = False
                        break
                    elif low_price <= trade_status['TP']:
                        trades.append((trade_status['EntryPrice'] - trade_status['TP']) / trade_status['EntryPrice'])
                        in_trade = False
                        break
                
                if current_time >= t_end:
                    pnl = (close_price - trade_status['EntryPrice']) / trade_status['EntryPrice'] if trade_status['Type'] == 'Long' else (trade_status['EntryPrice'] - close_price) / trade_status['EntryPrice']
                    trades.append(pnl)
                    in_trade = False
                    break
            else:
                close_price = float(candle['Close'])
                if close_price > range_high:
                    in_trade = True
                    trade_status = {
                        'Type': 'Long',
                        'EntryPrice': close_price,
                        'SL': midpoint,
                        'TP': close_price + r_factor * (close_price - midpoint)
                    }
                elif close_price < range_low:
                    in_trade = True
                    trade_status = {
                        'Type': 'Short',
                        'EntryPrice': close_price,
                        'SL': midpoint,
                        'TP': close_price - r_factor * (midpoint - close_price)
                    }
                    
    if not trades:
        return 0.0, 0.0
        
    trades = np.array(trades)
    # Aplicar apalancamiento o cálculo de riesgo simple basado en la cuenta
    # El rendimiento acumulado multiplicando los retornos individuales
    total_return = np.prod(1 + trades * (risk_per_trade / 100.0)) - 1
    
    wins = trades[trades > 0]
    losses = trades[trades <= 0]
    profit_factor = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    
    return total_return * 100.0, profit_factor

def main():
    # 1. Cargar configuración actual para mantener los tiempos de sesión
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    else:
        config = {
            "r_factor": 2.0,
            "risk_per_trade_pct": 2.0,
            "session_start_est": "03:00",
            "session_range_end_est": "03:15",
            "session_end_est": "10:00",
            "active": True
        }
        
    try:
        df = download_data()
    except Exception as e:
        print(f"Error descargando datos históricos: {e}. Se mantendrán los parámetros actuales.")
        return
        
    # Grid de búsqueda
    r_factors = [1.5, 1.75, 2.0, 2.25, 2.5]
    risks = [1.0, 1.5, 2.0, 2.5, 3.0]
    
    best_return = -999.0
    best_r = config["r_factor"]
    best_risk = config["risk_per_trade_pct"]
    best_pf = 0.0
    
    print("\nIniciando optimización de parámetros...")
    for r in r_factors:
        for rk in risks:
            ret, pf = run_simulation(
                df, 
                config["session_start_est"], 
                config["session_range_end_est"], 
                config["session_end_est"], 
                r, 
                rk
            )
            print(f"Probando R={r:.2f}, Riesgo={rk:.1f}% -> Retorno: {ret:+.2f}%, Profit Factor: {pf:.2f}")
            if ret > best_return:
                best_return = ret
                best_r = r
                best_risk = rk
                best_pf = pf
                
    print(f"\nOptimización finalizada:")
    print(f"-> Mejor R-Factor: {best_r:.2f}")
    print(f"-> Mejor Riesgo por Trade: {best_risk:.1f}%")
    print(f"-> Retorno Esperado (30d): {best_return:+.2f}%")
    print(f"-> Profit Factor: {best_pf:.2f}")
    
    # 2. Actualizar archivo de configuración
    config["r_factor"] = best_r
    config["risk_per_trade_pct"] = best_risk
    
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"Configuración auto-corregida guardada en: {CONFIG_PATH}")

if __name__ == "__main__":
    main()
