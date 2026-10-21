"""
Alerte copy-trading pour GitHub Actions (SANS achat automatique).
Chaque exécution : vérifie les nouvelles transactions du trader,
passe les nouveaux tokens dans RugCheck et envoie une alerte Telegram.
Les secrets viennent des variables d'environnement (jamais dans le code).
"""
import csv
import json
import os
from datetime import datetime

import requests

WALLET = os.environ["WALLET"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RPC = "https://api.mainnet-beta.solana.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
IGNORER = {"So11111111111111111111111111111111111111112"}
FICHIER_VUES = "seen.json"
FICHIER_JOURNAL = "journal.csv"


def rpc(methode, params):
    r = requests.post(
        RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params},
        timeout=25,
    )
    return r.json().get("result")


def dernieres_signatures():
    res = rpc("getSignaturesForAddress", [WALLET, {"limit": 20}])
    return [s["signature"] for s in (res or []) if not s.get("err")]


def tokens_achetes(signature):
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
            if b.get("owner") == WALLET:
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


def journal(mint, verdict, raisons):
    with open(FICHIER_JOURNAL, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [datetime.utcnow().isoformat(timespec="seconds"), mint, verdict, "; ".join(raisons)]
        )


def main():
    signatures = dernieres_signatures()
    premiere_fois = not os.path.exists(FICHIER_VUES)

    if premiere_fois:
        vues = set(signatures)  # on ignore l'historique, pas d'alerte
        print("Premier lancement : historique ignoré.")
    else:
        with open(FICHIER_VUES, encoding="utf-8") as f:
            vues = set(json.load(f))
        for sig in reversed(signatures):
            if sig in vues:
                continue
            vues.add(sig)
            for mint in tokens_achetes(sig):
                verdict, raisons = verifier_rugcheck(mint)
                print(f"Achat détecté : {mint} -> {verdict} {raisons}")
                journal(mint, verdict, raisons)
                envoyer_telegram(
                    f"Le trader a acheté un coin\n"
                    f"RugCheck : {verdict} {', '.join(raisons)}\n"
                    f"https://rugcheck.xyz/tokens/{mint}\n"
                    f"https://dexscreener.com/solana/{mint}"
                )

    # on garde les signatures déjà vues, plus celles de cette exécution
    with open(FICHIER_VUES, "w", encoding="utf-8") as f:
        anciennes = [s for s in vues if s not in signatures][:400]
        json.dump(list(signatures) + anciennes, f)


if __name__ == "__main__":
    main()
