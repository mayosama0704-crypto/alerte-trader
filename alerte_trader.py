"""
Alerte copy-trading pour GitHub Actions (SANS achat automatique).
Surveille un ou plusieurs wallets (séparés par des virgules dans le secret WALLET).
"""
import csv
import json
import os
from datetime import datetime

import requests

WALLETS = [w.strip() for w in os.environ["WALLET"].split(",") if w.strip()]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RPC = "https://api.mainnet-beta.solana.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
IGNORER = {"So11111111111111111111111111111111111111112"}
FICHIER_VUES = "seen.json"
FICHIER_JOURNAL = "journal.csv"


def court(wallet):
    return wallet[:4] + "..." + wallet[-4:]


def rpc(methode, params):
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params},
        timeout=25,
    )
    return r.json().get("result")


def dernieres_signatures(wallet):
    res = rpc("getSignaturesForAddress", [wallet, {"limit": 20}])
    return [s["signature"] for s in (res or []) if not s.get("err")]


def tokens_achetes(signature, wallet):
    tx = rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx or not tx.get("meta"):
        return []
    meta = tx["meta"]

    def soldes(liste):
        out = {}
        for b in liste or []:
            if b.get("owner") == wallet:
                out[b["mint"]] = float(b["uiTokenAmount"].get("uiAmount") or 0)
        return out

    avant = soldes(meta.get("preTokenBalances"))
    apres = soldes(meta.get("postTokenBalances"))
    return [
        m for m, montant in apres.items()
        if m not in IGNORER and montant > avant.get(m, 0)
    ]


def verifier_rugcheck(mint):
    try:
        d = requests.get(RUGCHECK.format(mint), timeout=25).json()
    except Exception as e:
        return "ERREUR", [str(e)]
    risques = d.get("risks") or []
    graves = [x.get("name", "?") for x in risques if x.get("level") == "danger"]
    moyens = [x.get("name", "?") for x in risques if x.get("level") == "warn"]
    if graves:
        return "MAUVAIS", graves
    if moyens:
        return "PRUDENCE", moyens
    return "OK", []


def envoyer_telegram(texte):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texte},
            timeout=25,
        )
    except Exception as e:
        print("Telegram a échoué :", e)


def journal(wallet, mint, verdict, raisons):
    with open(FICHIER_JOURNAL, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [datetime.utcnow().isoformat(timespec="seconds"),
             court(wallet), mint, verdict, "; ".join(raisons)]
        )


def charger_vues():
    if not os.path.exists(FICHIER_VUES):
        return {}
    try:
        with open(FICHIER_VUES, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def main():
    vues = charger_vues()
    for wallet in WALLETS:
        signatures = dernieres_signatures(wallet)
        if wallet not in vues:
            if signatures:
                vues[wallet] = signatures
                print(f"Nouveau wallet {court(wallet)} : historique ignoré.")
            continue
        deja = set(vues[wallet])
        for sig in reversed(signatures):
            if sig in deja:
                continue
            for mint in tokens_achetes(sig, wallet):
                verdict, raisons = verifier_rugcheck(mint)
                print(f"{court(wallet)} a acheté {mint} -> {verdict} {raisons}")
                journal(wallet, mint, verdict, raisons)
                envoyer_telegram(
                    f"Le trader {court(wallet)} a acheté un coin\n"
                    f"RugCheck : {verdict} {', '.join(raisons)}\n"
                    f"https://rugcheck.xyz/tokens/{mint}\n"
                    f"https://dexscreener.com/solana/{mint}"
                )
        anciennes = [s for s in vues[wallet] if s not in signatures][:400]
        vues[wallet] = list(signatures) + anciennes

    with open(FICHIER_VUES, "w", encoding="utf-8") as f:
        json.dump(vues, f)


if __name__ == "__main__":
    main()