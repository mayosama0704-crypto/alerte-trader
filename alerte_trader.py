"""
Alerte copy-trading pour GitHub Actions (SANS achat automatique).
Surveille un ou plusieurs wallets (séparés par des virgules dans le secret WALLET).

Version filtrée :
- n'alerte que sur de VRAIS achats : le wallet a dépensé du SOL (ou de l'USDC/USDT)
  et a reçu un coin en échange (les transferts, airdrops et ventes sont ignorés)
- affiche l'âge du coin (d'après DexScreener)
- ignore les coins plus vieux que AGE_MAX_JOURS
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
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/{}"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = {USDC, USDT}
IGNORER = {WSOL} | STABLES

FICHIER_VUES = "seen.json"
FICHIER_JOURNAL = "journal.csv"
NB_SIGNATURES = 30

MIN_SOL = 0.01          # dépense minimale en SOL pour compter comme un achat
MIN_STABLE = 1.0        # dépense minimale en USDC/USDT pour compter comme un achat
AGE_MAX_JOURS = 7       # les coins plus vieux que ça sont ignorés


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
            raise RpcErreur(f"{methode} : erreur RPC : {data['error']}")
        return data.get("result")
    raise RpcErreur(f"{methode} : {derniere}")


def dernieres_signatures(wallet):
    res = rpc("getSignaturesForAddress", [wallet, {"limit": NB_SIGNATURES}])
    return [s["signature"] for s in (res or []) if not s.get("err")]


def vrais_achats(signature, wallet):
    """
    Renvoie une liste de (mint, depense_sol) pour les coins réellement achetés
    dans cette transaction. depense_sol vaut None si plusieurs coins sont achetés
    en même temps ou si le paiement était en stablecoin.
    """
    tx = rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
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

    recus = [
        m for m, montant in apres.items()
        if m not in IGNORER and montant > avant.get(m, 0)
    ]
    if not recus:
        return []

    # Dépense en SOL natif (hors frais de transaction)
    cles = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
    depense_sol = 0.0
    for i, k in enumerate(cles):
        pubkey = k.get("pubkey") if isinstance(k, dict) else k
        if pubkey == wallet:
            diff = meta["preBalances"][i] - meta["postBalances"][i]
            if i == 0:
                diff -= meta.get("fee", 0)
            depense_sol = diff / 1e9
            break

    # Dépense en stablecoins
    depense_stable = sum(
        max(avant.get(s, 0) - apres.get(s, 0), 0) for s in STABLES
    )

    if depense_sol < MIN_SOL and depense_stable < MIN_STABLE:
        return []  # pas de paiement : transfert, airdrop ou autre

    montant = round(depense_sol, 3) if (len(recus) == 1 and depense_sol >= MIN_SOL) else None
    return [(m, montant) for m in recus]


def age_du_coin_heures(mint):
    """Âge du coin en heures d'après DexScreener, ou None si inconnu."""
    try:
        d = requests.get(DEXSCREENER.format(mint), timeout=25).json()
    except Exception as e:
        print(f"  DexScreener indisponible : {e}")
        return None
    creations = [
        p.get("pairCreatedAt") for p in (d.get("pairs") or []) if p.get("pairCreatedAt")
    ]
    if not creations:
        return None
    return (time.time() * 1000 - min(creations)) / 3_600_000


def format_age(heures):
    if heures is None:
        return "inconnu"
    if heures < 1:
        return f"{int(heures * 60)} min"
    if heures < 48:
        return f"{heures:.0f} h"
    return f"{heures / 24:.0f} jours"


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


def journal(wallet, mint, verdict, raisons, age):
    with open(FICHIER_JOURNAL, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            [datetime.now(timezone.utc).isoformat(timespec="seconds"),
             court(wallet), mint, verdict, "; ".join(raisons), age]
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
        achats = {}  # mint -> {"nb": int, "sol": float ou None}
        for sig in reversed(nouvelles):
            time.sleep(0.2)
            try:
                trouves = vrais_achats(sig, wallet)
            except RpcErreur as e:
                print(f"  {sig[:8]}... : lecture échouée ({e}), sera retentée.")
                echecs.add(sig)
                continue
            print(f"  {sig[:8]}... : {len(trouves)} vrai(s) achat(s).")
            for mint, depense in trouves:
                fiche = achats.setdefault(mint, {"nb": 0, "sol": 0.0})
                fiche["nb"] += 1
                if depense is None or fiche["sol"] is None:
                    fiche["sol"] = None
                else:
                    fiche["sol"] += depense

        for mint, fiche in achats.items():
            heures = age_du_coin_heures(mint)
            age = format_age(heures)
            if heures is not None and heures > AGE_MAX_JOURS * 24:
                print(f"  {mint} ignoré : coin trop vieux ({age}).")
                journal(wallet, mint, "VIEUX", [], age)
                continue

            verdict, raisons = verifier_rugcheck(mint)
            print(f"  {court(wallet)} a acheté {mint} (âge {age}) -> {verdict} {raisons}")
            journal(wallet, mint, verdict, raisons, age)

            lignes = [f"Le trader {court(wallet)} a acheté un coin"
                      + (f" ({fiche['nb']} achats)" if fiche["nb"] > 1 else "")]
            if fiche["sol"]:
                lignes.append(f"Dépensé : environ {fiche['sol']:.2f} SOL")
            lignes.append(f"Âge du coin : {age}")
            lignes.append(f"RugCheck : {verdict} {', '.join(raisons)}")
            lignes.append(f"https://rugcheck.xyz/tokens/{mint}")
            lignes.append(f"https://dexscreener.com/solana/{mint}")
            envoyer_telegram("\n".join(lignes))

        traitees = [s for s in signatures if s not in echecs]
        anciennes = [s for s in vues[wallet] if s not in signatures][:400]
        vues[wallet] = traitees + anciennes

    with open(FICHIER_VUES, "w", encoding="utf-8") as f:
        json.dump(vues, f)


if __name__ == "__main__":
    main()
