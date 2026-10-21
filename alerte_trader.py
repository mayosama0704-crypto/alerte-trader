"""
Alerte copy-trading pour GitHub Actions (SANS achat automatique).
Surveille un ou plusieurs wallets (séparés par des virgules dans le secret WALLET).

Version corrigée :
- les erreurs RPC ne sont plus silencieuses (affichées dans le log)
- une transaction dont la lecture échoue n'est PAS marquée comme "vue" : elle sera retentée
- le script affiche ce qu'il fait à chaque passage
- la réponse de Telegram est vérifiée
"""
import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import requests

WALLETS = [w for w in re.split(r"[,\s;]+", os.environ["WALLET"]) if w]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RPC = os.environ.get("RPC_URL") or "https://api.mainnet-beta.solana.com"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
IGNORER = {"So11111111111111111111111111111111111111112"}
FICHIER_VUES = "seen.json"
FICHIER_JOURNAL = "journal.csv"
NB_SIGNATURES = 30


class RpcErreur(Exception):
    pass


def court(wallet):
    return wallet[:4] + "..." + wallet[-4:]


def rpc(methode, params):
    """Appelle le RPC Solana. Lève RpcErreur si ça échoue après 3 essais."""
    derniere = "inconnue"
    for essai in range(3):
        try:
            r = requests.post(
                RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params},
                timeout=25,
            )
        except Exception as e:
            derniere = f"réseau : {e}"
            time.sleep(2)
            continue
        if r.status_code == 429:
            derniere = "trop de requêtes (429)"
            time.sleep(2 * (essai + 1))
            continue
        try:
            data = r.json()
        except Exception:
            derniere = f"réponse illisible (HTTP {r.status_code})"
            time.sleep(1)
            continue
        if "error" in data:
            derniere = f"erreur RPC : {data['error']}"
            time.sleep(1)
            continue
        return data.get("result")
    raise RpcErreur(f"{methode} : {derniere}")


def dernieres_signatures(wallet):
    res = rpc("getSignaturesForAddress", [wallet, {"limit": NB_SIGNATURES}])
    return [s["signature"] for s in (res or []) if not s.get("err")]


def tokens_achetes(signature, wallet):
    tx = rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if tx is None:
        raise RpcErreur("transaction introuvable pour l'instant")
    meta = tx.get("meta")
    if not meta:
        return []

    def soldes(liste):
        out = {}
        for b in liste or []:
            if b.get("owner") == wallet:
                out[b["mint"]] = out.get(b["mint"], 0) + float(
                    b["uiTokenAmount"].get("uiAmount") or 0
                )
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
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": texte},
            timeout=25,
        )
        if r.status_code == 200:
            print("  Telegram : message envoyé.")
        else:
            print(f"  Telegram a refusé : HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print("  Telegram a échoué :", e)


def journal(wallet, mint, verdict, raisons):
    with open(FICHIER_JOURNAL, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    print(f"{len(WALLETS)} wallet(s) surveillé(s) : {', '.join(court(w) for w in WALLETS)}")
    vues = charger_vues()
    for wallet in WALLETS:
        try:
            signatures = dernieres_signatures(wallet)
        except RpcErreur as e:
            print(f"{court(wallet)} : lecture impossible ({e})")
            continue

        if wallet not in vues:
            if signatures:
                vues[wallet] = signatures
                print(f"Nouveau wallet {court(wallet)} : historique ignoré.")
            else:
                print(f"{court(wallet)} : aucune transaction trouvée.")
            continue

        deja = set(vues[wallet])
        nouvelles = [s for s in signatures if s not in deja]
        print(f"{court(wallet)} : {len(signatures)} transactions lues, {len(nouvelles)} nouvelles.")

        echecs = set()
        achats = {}
        for sig in reversed(nouvelles):
            time.sleep(0.4)
            try:
                mints = tokens_achetes(sig, wallet)
            except RpcErreur as e:
                print(f"  {sig[:8]}... : lecture échouée ({e}), sera retentée.")
                echecs.add(sig)
                continue
            print(f"  {sig[:8]}... : {len(mints)} token(s) acheté(s).")
            for mint in mints:
                achats[mint] = achats.get(mint, 0) + 1

        for mint, nb in achats.items():
            verdict, raisons = verifier_rugcheck(mint)
            print(f"  {court(wallet)} a acheté {mint} ({nb} achat(s)) -> {verdict} {raisons}")
            journal(wallet, mint, verdict, raisons)
            suite = f" ({nb} achats)" if nb > 1 else ""
            envoyer_telegram(
                f"Le trader {court(wallet)} a acheté un coin{suite}\n"
                f"RugCheck : {verdict} {', '.join(raisons)}\n"
                f"https://rugcheck.xyz/tokens/{mint}\n"
                f"https://dexscreener.com/solana/{mint}"
            )

        traitees = [s for s in signatures if s not in echecs]
        anciennes = [s for s in vues[wallet] if s not in signatures][:400]
        vues[wallet] = traitees + anciennes

    with open(FICHIER_VUES, "w", encoding="utf-8") as f:
        json.dump(vues, f)


if __name__ == "__main__":
    main()