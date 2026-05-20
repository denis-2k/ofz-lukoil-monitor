import asyncio
import json
import os
import pathlib
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

import certifi

os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CA_BUNDLE = ROOT / "tbank_ca_bundle.pem"
OUT = ROOT / "market_state.json"
TARGET = "invest-public-api.tbank.ru"
HOST = "invest-public-api.tbank.ru"

INSTRUMENTS = {
    "lukoil_zo31": {"isin": "RU000A1059R0", "label": "ЛУКОЙЛ ЗО-31", "preferred_class_codes": ["TQCB"]},
    "ofz_26247": {"isin": "RU000A108EF8", "label": "ОФЗ 26247", "preferred_class_codes": ["TQOB"]},
}

SCREENING_CANDIDATES = [
    {"key": "lukoil_zo26", "isin": "RU000A1059N9", "label": "ЛУКОЙЛ ЗО-26", "issuer_risk": 1},
    {"key": "lukoil_zo27", "isin": "RU000A1059P4", "label": "ЛУКОЙЛ ЗО-27", "issuer_risk": 1},
    {"key": "lukoil_zo30", "isin": "RU000A1059Q2", "label": "ЛУКОЙЛ ЗО-30", "issuer_risk": 1},
    {"key": "lukoil_zo31", "isin": "RU000A1059R0", "label": "ЛУКОЙЛ ЗО-31", "issuer_risk": 1},
    {"key": "nornickel_zo26", "isin": "RU000A107C67", "label": "Норникель ЗО26-Д", "issuer_risk": 1},
    {"key": "rzd_zo26_2", "isin": "RU000A1084Q0", "label": "РЖД ЗО26-2-Р", "issuer_risk": 1},
    {"key": "rzd_zo28_1", "isin": "RU000A1089X5", "label": "РЖД ЗО28-1-Р", "issuer_risk": 1},
    {"key": "rzd_zo28_3", "isin": "RU000A1089U1", "label": "РЖД ЗО28-3-Р", "issuer_risk": 1},
    {"key": "polus_zo28", "isin": "RU000A108P79", "label": "Полюс ЗО28-Д", "issuer_risk": 1},
    {"key": "sovcomflot_zo28", "isin": "RU000A105A87", "label": "Совкомфлот ЗО-2028", "issuer_risk": 2},
    {"key": "gtlk_zo27", "isin": "RU000A107B43", "label": "ГТЛК ЗО27-Д", "issuer_risk": 3},
    {"key": "gtlk_zo28", "isin": "RU000A107CX7", "label": "ГТЛК ЗО28-Д", "issuer_risk": 3},
    {"key": "gtlk_zo29", "isin": "RU000A107D58", "label": "ГТЛК ЗО29-Д", "issuer_risk": 3},
]


def quotation_to_decimal(value) -> Decimal:
    return Decimal(value.units) + Decimal(value.nano) / Decimal("1000000000")


def money_to_decimal(value) -> Decimal:
    return Decimal(value.units) + Decimal(value.nano) / Decimal("1000000000")


def quote_level(level):
    return {
        "price": float(quotation_to_decimal(level.price)),
        "quantity": int(level.quantity),
    }


def choose_tradable_instrument(instruments, preferred_class_codes):
    for instrument in instruments:
        if (
            getattr(instrument, "api_trade_available_flag", False)
            and getattr(instrument, "class_code", "") in preferred_class_codes
        ):
            return instrument
    for instrument in instruments:
        if getattr(instrument, "api_trade_available_flag", False):
            return instrument
    return instruments[0]


def instrument_snapshot(instrument):
    return {
        "name": instrument.name,
        "ticker": instrument.ticker,
        "figi": instrument.figi,
        "uid": instrument.uid,
        "class_code": getattr(instrument, "class_code", None),
        "api_trade_available_flag": getattr(instrument, "api_trade_available_flag", None),
    }


def bond_snapshot(bond):
    nominal = money_to_decimal(bond.nominal)
    aci = money_to_decimal(bond.aci_value)
    return {
        "currency": bond.currency,
        "nominal_currency": bond.nominal.currency,
        "nominal": float(nominal),
        "aci_currency": bond.aci_value.currency,
        "aci": float(aci),
        "maturity_date": bond.maturity_date.date().isoformat() if bond.maturity_date else None,
        "coupon_quantity_per_year": bond.coupon_quantity_per_year,
        "floating_coupon_flag": bond.floating_coupon_flag,
        "perpetual_flag": bond.perpetual_flag,
        "amortization_flag": bond.amortization_flag,
        "buy_available_flag": bond.buy_available_flag,
        "sell_available_flag": bond.sell_available_flag,
    }


def fetch_usd_rub():
    url = (
        "https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/"
        "securities/USD000UTSTOM.json?iss.meta=off&iss.only=securities,marketdata"
    )
    try:
        with urllib.request.urlopen(url, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        columns = payload["marketdata"]["columns"]
        row = payload["marketdata"]["data"][0]
        md = dict(zip(columns, row))
        sec_columns = payload["securities"]["columns"]
        sec_row = payload["securities"]["data"][0]
        sec = dict(zip(sec_columns, sec_row))
        return float(md.get("LAST") or md.get("WAPRICE") or sec.get("PREVPRICE") or sec.get("PREVWAPRICE"))
    except Exception:
        return None


async def fetch_usd_rub_tinvest(client):
    try:
        found = await client.instruments.find_instrument(
            query="USD000UTSTOM",
            api_trade_available_flag=True,
        )
        instrument = choose_tradable_instrument(found.instruments, ["CETS"])
        book = await client.market_data.get_order_book(instrument_id=instrument.uid, depth=1)
        bids = [quote_level(level) for level in book.bids]
        asks = [quote_level(level) for level in book.asks]
        if bids and asks:
            return round((bids[0]["price"] + asks[0]["price"]) / 2, 4)
        last_price = quotation_to_decimal(book.last_price)
        return float(last_price) if last_price else None
    except Exception:
        return None


def ensure_ca_bundle() -> None:
    if CA_BUNDLE.exists():
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(CA_BUNDLE))
        return
    proc = subprocess.run(
        ["openssl", "s_client", "-showcerts", "-connect", f"{HOST}:443", "-servername", HOST],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    certs = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        proc.stdout,
        flags=re.S,
    )
    if len(certs) >= 2:
        CA_BUNDLE.write_text(
            pathlib.Path(certifi.where()).read_text(encoding="utf-8").rstrip()
            + "\n\n"
            + "\n\n".join(certs[1:])
            + "\n",
            encoding="utf-8",
        )
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(CA_BUNDLE))
    else:
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", certifi.where())


async def load_instrument_state(client, meta):
    found = await client.instruments.find_instrument(query=meta["isin"])
    if not found.instruments:
        return {**meta, "status": "not_found"}

    instrument = choose_tradable_instrument(
        found.instruments,
        meta.get("preferred_class_codes", ["TQCB"]),
    )
    state = {
        **meta,
        "status": "ok",
        **instrument_snapshot(instrument),
        "candidates": [instrument_snapshot(item) for item in found.instruments],
    }

    try:
        from t_tech.invest import InstrumentIdType

        bond = await client.instruments.bond_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID,
            id=instrument.uid,
        )
        state.update(bond_snapshot(bond.instrument))
    except Exception as exc:
        state["bond_error"] = str(exc)

    try:
        order_book = await client.market_data.get_order_book(
            instrument_id=instrument.uid,
            depth=10,
        )
        bids = [quote_level(level) for level in order_book.bids]
        asks = [quote_level(level) for level in order_book.asks]
        state["bids"] = bids
        state["asks"] = asks
        state["best_bid"] = bids[0] if bids else None
        state["best_ask"] = asks[0] if asks else None
        state["orderbook_ts"] = order_book.orderbook_ts.isoformat() if order_book.orderbook_ts else None
        if bids and asks and bids[0]["price"]:
            state["spread"] = round(asks[0]["price"] - bids[0]["price"], 6)
            state["spread_pct"] = round((asks[0]["price"] - bids[0]["price"]) / bids[0]["price"] * 100, 4)
        else:
            state["spread"] = None
            state["spread_pct"] = None
    except Exception as exc:
        state["status"] = "orderbook_error"
        state["error"] = str(exc)

    return state


def rub_dirty_price(state, price_key, usd_rub):
    price = state.get(price_key)
    if not price:
        return None
    nominal = state.get("nominal")
    aci = state.get("aci", 0)
    nominal_currency = state.get("nominal_currency")
    if not nominal:
        return None
    dirty = price["price"] / 100 * nominal + aci
    if nominal_currency == "usd":
        if not usd_rub:
            return None
        dirty *= usd_rub
    return dirty


def years_to_maturity(state):
    maturity = state.get("maturity_date")
    if not maturity:
        return None
    try:
        dt = datetime.fromisoformat(maturity).replace(tzinfo=timezone.utc)
        return max(0, (dt - datetime.now(timezone.utc)).days / 365.25)
    except Exception:
        return None


def build_screening(instruments, usd_rub):
    ofz = instruments.get("ofz_26247", {})
    ofz_dirty_bid = rub_dirty_price(ofz, "best_bid", usd_rub)
    rows = []
    for meta in SCREENING_CANDIDATES:
        state = instruments.get(meta["key"])
        if not state or state.get("status") not in {"ok", "orderbook_error"}:
            continue
        ask_dirty = rub_dirty_price(state, "best_ask", usd_rub)
        units_per_100_ofz = None
        if ofz_dirty_bid and ask_dirty:
            units_per_100_ofz = 100 * ofz_dirty_bid / ask_dirty
        spread_pct = state.get("spread_pct")
        term = years_to_maturity(state)
        risk = meta["issuer_risk"]
        liquidity_qty = (state.get("best_ask") or {}).get("quantity") or 0
        score = None
        verdict = "нет данных"
        if units_per_100_ofz is not None and spread_pct is not None:
            liquidity_bonus = min(5, liquidity_qty / 5)
            term_penalty = abs((term or 4) - 4) * 0.45
            risk_penalty = {1: 0, 2: 5, 3: 12}.get(risk, 8)
            score = units_per_100_ofz * 50 - spread_pct * 3 + liquidity_bonus - term_penalty - risk_penalty
            if spread_pct > 1.5:
                verdict = "ждать: широкий спред"
            elif risk >= 3:
                verdict = "только осознанно: высокий риск"
            elif liquidity_qty < 2:
                verdict = "проверить глубину"
            else:
                verdict = "рассмотреть лимитку"
        rows.append({
            "key": meta["key"],
            "label": meta["label"],
            "isin": meta["isin"],
            "issuer_risk": risk,
            "risk_label": {1: "низкий/умеренный", 2: "умеренный", 3: "повышенный"}.get(risk, "не задан"),
            "status": state.get("status"),
            "ask": (state.get("best_ask") or {}).get("price"),
            "ask_qty": (state.get("best_ask") or {}).get("quantity"),
            "bid": (state.get("best_bid") or {}).get("price"),
            "bid_qty": (state.get("best_bid") or {}).get("quantity"),
            "spread_pct": spread_pct,
            "dirty_ask_rub": round(ask_dirty, 2) if ask_dirty else None,
            "units_per_100_ofz": round(units_per_100_ofz, 4) if units_per_100_ofz else None,
            "maturity_date": state.get("maturity_date"),
            "years_to_maturity": round(term, 2) if term is not None else None,
            "score": round(score, 2) if score is not None else None,
            "verdict": verdict,
        })
    rows.sort(key=lambda item: (item["score"] is not None, item["score"] or -9999), reverse=True)
    return {
        "method": "Ранжирование показывает, какие замещающие облигации стоит открыть в брокере и проверить лимиткой. Это не команда на покупку.",
        "usd_rub": usd_rub,
        "ofz_dirty_bid_rub": round(ofz_dirty_bid, 2) if ofz_dirty_bid else None,
        "candidates": rows,
        "top": rows[:5],
    }


async def main() -> None:
    token = os.environ.get("T_INVEST_TOKEN") or os.environ.get("INVEST_TOKEN")
    if not token:
        raise SystemExit("T_INVEST_TOKEN secret is not set")

    ensure_ca_bundle()

    from t_tech.invest import AsyncClient

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "t-invest-api",
        "status": "ok",
        "usd_rub": fetch_usd_rub(),
        "instruments": {},
    }

    async with AsyncClient(token, target=TARGET) as client:
        if not result["usd_rub"]:
            result["usd_rub"] = await fetch_usd_rub_tinvest(client)
        metas = {
            **INSTRUMENTS,
            **{item["key"]: {**item, "preferred_class_codes": ["TQCB"]} for item in SCREENING_CANDIDATES},
        }
        for key, meta in metas.items():
            result["instruments"][key] = await load_instrument_state(client, meta)

    result["screening"] = build_screening(result["instruments"], result["usd_rub"])

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
