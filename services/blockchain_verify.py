import logging
import aiohttp

ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


async def verify_crypto_payment(
    network_key: str,
    tx_hash: str,
    expected_wallet: str,
    expected_usd: float,
) -> tuple[bool, str]:
    """
    Verify a crypto payment on public blockchain explorers/RPCs (Trust Wallet / Binance deposit addresses).
    Returns (is_verified, message).
    """
    tx = tx_hash.strip()
    wallet = expected_wallet.strip()
    if not tx or not wallet:
        return False, "Не вказано TX Hash або адресу гаманця"

    min_amount = expected_usd * 0.98  # 2% tolerance for network/rounding differences

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            # 1. TRON / USDT TRC20
            if network_key == "USDT_TRC20":
                url = f"https://apilist.tronscanapi.com/api/transaction-info?hash={tx}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False, "Не вдалося перевірити транзакцію в Tronscan"
                    data = await resp.json()
                    if not data.get("confirmed"):
                        return False, "Транзакція ще не підтверджена в мережі TRON"
                    if data.get("contractRet") != "SUCCESS":
                        return False, "Транзакція не успішна в мережі TRON"

                    for tr in data.get("trc20TransferInfo") or []:
                        to_addr = str(tr.get("to_address") or "").strip()
                        decimals = int(tr.get("decimals") or 6)
                        raw_amt = float(tr.get("amount_str") or 0) / (10 ** decimals)
                        if to_addr == wallet and raw_amt >= min_amount:
                            return True, f"Підтверджено в TRON ({raw_amt:.2f} USDT)"
                    return False, "Отримувач або сума не співпадають у TRC20"

            # 2. EVM BEP20 (BSC) - USDT / USDC
            if network_key == "USDC_BEP20":
                rpc_url = "https://bsc-dataseed.binance.org"
                payload = {
                    "jsonrpc": "2.0",
                    "method": "eth_getTransactionReceipt",
                    "params": [tx],
                    "id": 1,
                }
                async with session.post(rpc_url, json=payload) as resp:
                    if resp.status != 200:
                        return False, "Не вдалося перевірити транзакцію в BSC RPC"
                    data = await resp.json()
                    receipt = data.get("result")
                    if not receipt:
                        return False, "Транзакцію ще не знайдено в мережі BSC"
                    if receipt.get("status") != "0x1":
                        return False, "Транзакція відхилена в мережі BSC"

                    target_hex = wallet.lower().replace("0x", "").rjust(64, "0")
                    for log in receipt.get("logs") or []:
                        topics = log.get("topics") or []
                        if len(topics) >= 3 and topics[0].lower() == ERC20_TRANSFER_TOPIC:
                            to_topic = topics[2].lower().replace("0x", "").rjust(64, "0")
                            if to_topic == target_hex:
                                raw_int = int(log.get("data") or "0x0", 16)
                                # BEP20 USDT/USDC uses 18 decimals on BSC (or 6 on some bridges)
                                amt_18 = raw_int / (10 ** 18)
                                amt_6 = raw_int / (10 ** 6)
                                if amt_18 >= min_amount or (amt_6 >= min_amount and amt_6 < expected_usd * 50):
                                    return True, "Підтверджено в мережі BSC (BEP20)"
                    return False, "Отримувач або сума не співпадають у BEP20"

            # 3. TON Network
            if network_key == "USDT_TON":
                url = f"https://tonapi.io/v2/blockchain/transactions/{tx}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return False, "Транзакцію ще не знайдено в TonAPI"
                    data = await resp.json()
                    if not data.get("success"):
                        return False, "Транзакція не успішна в мережі TON"
                    out_msgs = data.get("out_msgs") or []
                    in_msg = data.get("in_msg") or {}
                    dest = str((in_msg.get("destination") or {}).get("address") or "")
                    if wallet in dest or any(wallet in str((m.get("destination") or {}).get("address") or "") for m in out_msgs):
                        return True, "Підтверджено в мережі TON"
                    return False, "Не знайдено переказу на вказаний TON-гаманець"

    except Exception as e:
        logging.warning(f"Blockchain verify warning ({network_key}): {e}")
        return False, "Автоперевірка тимчасово недоступна — заявка передана адміністратору"

    return False, "Мережа потребує ручного підтвердження адміністратором"
