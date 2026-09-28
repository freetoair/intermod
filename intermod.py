#!/usr/bin/env python3
"""
Kalkulator intermodulacionih smetnji.

Za datu lokaciju unose se frekvencije koje se emituju (predajnici) i
frekvencija na kojoj se u prijemniku javlja smetnja. Program generise
sve linearne kombinacije oblika |k1*F1 + k2*F2 + ...| gde je zbir
apsolutnih vrednosti koeficijenata red kombinacije (međumodulacija,
harmonici, mix prodotti), i prikazuje one koji padaju u zadatu
toleranciju oko frekvencije smetnje.

Drugi cilj je medjurekvencija (MF) prijemnika: kombinacija moze da
prodre pravo na ulaz medjufrekventeg stepena (feedthrough, spurious
odziv meseca) bez obzira na prijemnu frekvenciju, pa se i ona proveri
ako je MF uneta. Uz MF se proverava i lik (image) frekvencija
f_RX ± 2·MF, zavisno od strane lokalnog oscilatora: signal na liku se
u mešaču pretvara na istu MF kao i koristan signal. Rezultati se
razlikuju po koloni "Prodor":
RX   = kombinacija pada na primljenu frekvenciju,
MF   = kombinacija pada direktno na medjurekvenciju,
LIK+ = kombinacija pada na lik iznad RX (LO iznad, f_RX + 2·MF),
LIK− = kombinacija pada na lik ispod RX (LO ispod, f_RX − 2·MF).

Sve frekvencije se unose u MHz, odstupanje se prikazuje u kHz.
"""

import json
import math
import os
import re
import sys
from dataclasses import dataclass

import ms2711d

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolTip,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Rezultat:
    """Jedan intermodulacioni proizvod koji se pojavio na izlaznoj frekvenciji."""

    red: int            # zbir |ki| — red međumodulacije
    formula: str        # tekstualni zapis kombinacije, npr. "2×F1 − F3"
    frekvencija: float  # dobijena frekvencija u MHz
    odstupanje: float   # (proizvod − ciljna frekvencija) u kHz, sa znakom:
                        # "+" iznad centra propusnog opsega, "−" ispod
    cilj: str           # "RX" (primljena), "MF" (prodor na međufrekvenciju),
                        # "LIK+"/"LIK−" (lik frekvencija) ili "—"
    koef: tuple[int, ...] = ()  # koeficijenti k1..kn (za procenu nivoa)


def procena_nivoa(koef: tuple[int, ...], slabljenje_po_redu: float) -> float:
    """Gruba procena relativnog nivoa proizvoda u dB kad stvarni nivoi
    signala nisu poznati — meri koliko je kombinacija "realna", ne snagu.

    Za nelinearnost reda n, proizvod sa koeficijentima k ima amplitudu
    srazmernu multinomijalnom koeficijentu n!/∏|ki|! (zato je npr.
    F1+F2−F3 teorijski 6 dB jači od 2×F1−F2), a svaki viši red je slabiji
    za približno (IP − P_ulaz), što ovde zamenjuje `slabljenje_po_redu`.
    Osnovna frekvencija (red 1) je 0 dB."""
    red = sum(abs(k) for k in koef)
    multinom = math.factorial(red)
    for k in koef:
        multinom //= math.factorial(abs(k))
    return -slabljenje_po_redu * (red - 1) + 20.0 * math.log10(multinom)


# ---------- Jezik korisničkog interfejsa ----------
# Svi tekstovi koje korisnik vidi, kao (srpski, engleski). Kod i komentari
# ostaju na srpskom; jezik se bira u prozoru i pamti preko QSettings.
JEZIK = "en"

TEKSTOVI: dict[str, tuple[str, str]] = {
    "naslov": ("YT1BN Kalkulator frekvencijskih kombinacija (intermodulacija)",
               "YT1BN Frequency Combination Calculator (intermodulation)"),
    "ulaz": ("Ulazni parametri", "Input parameters"),
    "emit_lbl": ("Emitovane frekvencije (MHz):", "Transmitted frequencies (MHz):"),
    "emit_ph": ("Jedna frekvencija po redu, u MHz (može i kHz/GHz, decimalni zarez ili tačka).\n"
                "Primer:\n453.200\n458,750\n463.4 MHz",
                "One frequency per line, in MHz (kHz/GHz and decimal comma or point accepted).\n"
                "Example:\n453.200\n458.750\n463.4 MHz"),
    "rx_lbl": ("Primljena frekvencija smetnje (MHz):", "Interfered receive frequency (MHz):"),
    "rx_ph": ("npr. 455.975", "e.g. 455.975"),
    "mf_chk": ("Računaj i prodor na međufrekvenciju", "Also check IF breakthrough"),
    "mf_lbl": ("Međufrekvencija prijemnika (MHz):", "Receiver IF (MHz):"),
    "mf_ph": ("npr. 10.7 ili 455 kHz", "e.g. 10.7 or 455 kHz"),
    "filter_lbl": ("Širina MF filtera:", "IF filter bandwidth:"),
    "filter_tip": ("Propusni opseg MF filtra: kombinacija prodire na međufrekvenciju\n"
                   "ako padne unutar ±polovine izabrane širine oko MF.",
                   "IF filter bandwidth: a combination breaks through to the IF\n"
                   "if it falls within ±half of the selected width around the IF."),
    "lo_lbl": ("Lik frekvencija:", "Image frequency:"),
    "lo_iznad": ("LO iznad RX (lik = RX + 2·MF)", "LO above RX (image = RX + 2·IF)"),
    "lo_ispod": ("LO ispod RX (lik = RX − 2·MF)", "LO below RX (image = RX − 2·IF)"),
    "lo_obe": ("Obe strane (nepoznat LO)", "Both sides (LO unknown)"),
    "lo_tip": ("Lik (image) frekvencija se proverava sa tolerancijom MF filtra,\n"
               "jer i ona posle mešanja prolazi kroz MF filter.",
               "The image frequency is checked with the IF filter tolerance,\n"
               "since after mixing it also passes through the IF filter."),
    "red_lbl": ("Maksimalni red kombinacije:", "Maximum combination order:"),
    "red_tip": ("Najviši zbir |koeficijenata| koji se razmatra (red međumodulacije)",
                "Highest sum of |coefficients| considered (intermodulation order)"),
    "tol_lbl": ("Ulazni opseg RX:", "RX input bandwidth:"),
    "tol_tip": ("Puna širina prijemnog opsega: rezultat se prihvata u ±polovini\n"
                "oko primljene frekvencije (isto pravilo kao za MF filter).",
                "Full receiver input bandwidth: a result is accepted within ±half\n"
                "around the receive frequency (same rule as for the IF filter)."),
    "sve_chk": ("Prikaži sve proizvode (ne samo one u toleranciji)",
                "Show all products (not only those within tolerance)"),
    "izracunaj": ("Izračunaj", "Calculate"),
    "snimi": ("Sačuvaj...", "Save..."),
    "snimi_tip": ("Upiši unete frekvencije i parametre u JSON fajl",
                  "Write the entered frequencies and parameters to a JSON file"),
    "ucitaj": ("Učitaj...", "Load..."),
    "ucitaj_tip": ("Popuni polja iz ranije sačuvanog JSON fajla",
                   "Fill in the fields from a previously saved JSON file"),
    "jezik": ("Jezik:", "Language:"),
    "kolone": ("Prodor|Red|Kombinacija|Frekvencija (MHz)|Odstupanje (kHz)|Nivo (dBm)",
               "Target|Order|Combination|Frequency (MHz)|Offset (kHz)|Level (dBm)"),
    "slab_lbl": ("Slabljenje po redu kombinacije:", "Attenuation per order:"),
    "slab_tip": ("Stvarni nivoi nisu poznati, pa je visina linije procena:\n"
                 "svaki viši red je slabiji za ovoliko dB (≈ IP prijemnika − nivo signala),\n"
                 "a kombinacije više različitih predajnika su teorijski jače\n"
                 "(npr. F1+F2−F3 je 6 dB iznad 2×F1−F2).",
                 "Actual levels are unknown, so the line height is an estimate:\n"
                 "each higher order is weaker by this many dB (≈ receiver IP − signal level),\n"
                 "and combinations of more distinct transmitters are theoretically stronger\n"
                 "(e.g. F1+F2−F3 is 6 dB above 2×F1−F2)."),
    "napomena": ("visina = procena realnosti kombinacije, ne izmereni nivo",
                 "height = how likely the combination is, not a measured level"),
    "napomena_mereno": ("visina = nivo procenjen iz izmerenog traga i IIP3 (kartica Spektar)",
                        "height = level estimated from the measured trace and IIP3 (Spectrum tab)"),
    "neispravan": ("Neispravan unos", "Invalid input"),
    "err_emit": ("Emitovane frekvencije: {e}", "Transmitted frequencies: {e}"),
    "err_nema_emit": ("Unesite bar jednu emitovanu frekvenciju.", "Enter at least one transmitted frequency."),
    "err_rx": ("Primljena frekvencija nije validna.", "The receive frequency is not valid."),
    "err_mf": ("Međufrekvencija nije validna.", "The IF is not valid."),
    "err_nepoznat": ("nepoznat tekst '{t}'", "unknown text '{t}'"),
    "err_nejasan": ("nejasan unos kod '{t}' (za listu koristite zarez sa razmakom ili novi red)",
                    "ambiguous input at '{t}' (separate list items with comma and space, or a new line)"),
    "oprez": ("Oprez", "Caution"),
    "mnogo_auto": ("Veliki broj kombinacija ({n}) — pritisnite „Izračunaj“.",
                   "Large number of combinations ({n}) — press “Calculate”."),
    "mnogo_pitanje": ("Veliki broj kombinacija ({n}), računanje može potrajati.\nNastaviti?",
                      "Large number of combinations ({n}), the calculation may take a while.\nContinue?"),
    "status": ("{n} predajnika, red ≤ {red} → {uk} kombinacija (na RX: {rx} u ±{tol} kHz{mf}){lik}",
               "{n} transmitters, order ≤ {red} → {uk} combinations (on RX: {rx} within ±{tol} kHz{mf}){lik}"),
    "status_mf": (", prodor na MF: {mf}, na lik: {lik} u ±{tol} kHz",
                  ", IF breakthrough: {mf}, image: {lik} within ±{tol} kHz"),
    "snimi_naslov": ("Sačuvaj set frekvencija", "Save frequency set"),
    "ucitaj_naslov": ("Učitaj set frekvencija", "Load frequency set"),
    "err_snimanje": ("Greška pri snimanju", "Save error"),
    "err_fajl": ("Neispravan fajl", "Invalid file"),
    "sacuvano": ("Sačuvano: {p}", "Saved: {p}"),
    "ucitano": ("Učitano: {p}", "Loaded: {p}"),
    "leg_u": ("u propusnom opsegu (prodor)", "inside passband (breakthrough)"),
    "leg_van": ("u blizini opsega", "near passband"),
    "nema_rez": ("Nema rezultata za prikaz", "No results to display"),
    "nema_mf": ("Uključite „Računaj i prodor na međufrekvenciju“ za MF i lik",
                "Enable “Also check IF breakthrough” to see IF and image"),
    "osa_y": ("procena nivoa (dB)", "estimated level (dB)"),
    "osa_x": ("odstupanje (kHz)", "offset (kHz)"),
    "svi": ("Svi proizvodi", "All products"),
    "tip_red": ("red", "order"),
    "tip_odst": ("odstupanje", "offset"),
    "tip_nivo": ("procena nivoa", "estimated level"),
    "tip_jos": ("… i još {n}", "… and {n} more"),
    "osa_y_dbm": ("procenjeni nivo (dBm)", "estimated level (dBm)"),
    "osa_nivo": ("nivo (dBm)", "level (dBm)"),
    "prag": ("prag", "threshold"),
    "tab_proizvodi": ("Proizvodi", "Products"),
    "tab_spektar": ("Spektar (instrument)", "Spectrum (instrument)"),
    "port": ("Port:", "Port:"),
    "trag": ("Trag:", "Trace:"),
    "trag_ekran": ("0 — ekran (poslednji sweep)", "0 — screen (last sweep)"),
    "preuzmi": ("Preuzmi sa instrumenta", "Read from instrument"),
    "preuzmi_tip": ("Čita trag sa Anritsu MS2711D preko serijskog porta (9600 8N1, null-modem kabl).\n"
                    "Instrument se samo čita, ništa se u njemu ne menja.",
                    "Reads a trace from the Anritsu MS2711D over the serial port (9600 8N1, null-modem cable).\n"
                    "The instrument is only read, nothing in it is changed."),
    "ucitaj_trag": ("Učitaj trag...", "Load trace..."),
    "ucitaj_trag_tip": ("CSV ili tekst: kolone frekvencija (MHz ili Hz) i nivo (dBm)",
                        "CSV or text: columns frequency (MHz or Hz) and level (dBm)"),
    "snimi_trag": ("Sačuvaj trag...", "Save trace..."),
    "vrh_lbl": ("Vrhovi iznad šuma:", "Peaks above noise:"),
    "vrhovi_btn": ("Vrhovi → emitovane", "Peaks → transmitters"),
    "vrhovi_tip": ("Upisuje frekvencije pronađenih vrhova u emitovane frekvencije.\n"
                   "Tačnost je ± pola koraka traga (span/400) — za tačan proračun upišite\n"
                   "tačne frekvencije predajnika; nivoi se svejedno uzimaju iz traga.",
                   "Writes the detected peak frequencies into the transmitted frequencies.\n"
                   "Accuracy is ± half a trace step (span/400) — for an exact calculation enter\n"
                   "the exact transmitter frequencies; levels are still taken from the trace."),
    "mereno_chk": ("Koristi izmerene nivoe", "Use measured levels"),
    "mereno_tip": ("Nivo svakog proizvoda se računa iz izmerenih nivoa predajnika:\n"
                   "P = Σ|ki|·Pi − (red−1)·IIP3  (npr. 2F1−F2: 2·P1 + P2 − 2·IIP3).\n"
                   "Za redove različite od 3 to je gruba procena (koristi se isti IIP).",
                   "Each product level is computed from the measured transmitter levels:\n"
                   "P = Σ|ki|·Pi − (order−1)·IIP3  (e.g. 2F1−F2: 2·P1 + P2 − 2·IIP3).\n"
                   "For orders other than 3 this is a rough estimate (the same IIP is used)."),
    "iip_lbl": ("IIP3 prijemnika:", "Receiver IIP3:"),
    "prag_lbl": ("Jak prodor iznad:", "Strong hit above:"),
    "prag_tip": ("Proizvod čiji je procenjeni nivo iznad ovoga smatra se jakim prodorom\n"
                 "(npr. oko osetljivosti prijemnika).",
                 "A product whose estimated level is above this is a strong hit\n"
                 "(e.g. around the receiver sensitivity)."),
    "nema_traga": ("Nema traga — preuzmite ga sa instrumenta ili učitajte fajl",
                   "No trace — read it from the instrument or load a file"),
    "citam": ("Čitam sa instrumenta ({p})…", "Reading from instrument ({p})…"),
    "err_instr": ("Greška instrumenta", "Instrument error"),
    "err_trag_fajl": ("Neispravan fajl traga", "Invalid trace file"),
    "trag_ucitan": ("Trag učitan: {o}", "Trace loaded: {o}"),
    "trag_info": ("{o} · {f0}–{f1} MHz · {n} tačaka · RBW {rbw} kHz · šum {sum} dBm",
                  "{o} · {f0}–{f1} MHz · {n} points · RBW {rbw} kHz · noise {sum} dBm"),
    "nema_vrhova": ("Nema vrhova iznad zadatog praga.", "No peaks above the given threshold."),
    "zameni_tx": ("Zameniti postojeće emitovane frekvencije sa {n} pronađenih vrhova?",
                  "Replace the existing transmitted frequencies with the {n} detected peaks?"),
    "status_jaki": (", jakih prodora: {n} (≥ {prag} dBm)", ", strong hits: {n} (≥ {prag} dBm)"),
    "van_traga": (", izmereni nivoi nisu upotrebljeni: F{i} je van opsega traga",
                  ", measured levels not used: F{i} is outside the trace range"),
}

# Oznake ciljeva za prikaz; interno se uvek koriste srpski ključevi
NAZIVI_CILJEVA = {"MF": ("MF", "IF"), "LIK+": ("LIK+", "IMG+"), "LIK−": ("LIK−", "IMG−")}


def tr(kljuc: str, **kw) -> str:
    """Tekst za trenutni jezik; {imena} se popunjavaju iz kw."""
    tekst = TEKSTOVI[kljuc][1 if JEZIK == "en" else 0]
    return tekst.format(**kw) if kw else tekst


def naziv_cilja(cilj: str) -> str:
    return NAZIVI_CILJEVA.get(cilj, (cilj, cilj))[1 if JEZIK == "en" else 0]


def dec(tekst: str) -> str:
    """Decimalni zarez za srpski, tačka za engleski."""
    return tekst.replace(".", ",") if JEZIK == "sr" else tekst


def hiljade(n: int) -> str:
    """Razdvajanje hiljada: 1.234.567 (sr) / 1,234,567 (en)."""
    return f"{n:,}".replace(",", ".") if JEZIK == "sr" else f"{n:,}"


# razmak pre jedinice je unutar opcione grupe, da broj bez jedinice ne
# "pojede" razdvojnik (novi red) iza sebe
_BROJ_RE = re.compile(r"(\d+(?:[.,]\d+)?|[.,]\d+)(?:[ \t]*([gkm]hz))?", re.IGNORECASE)
_MNOZIOCI = {None: 1.0, "mhz": 1.0, "ghz": 1000.0, "khz": 0.001}


def parse_frekvencije(tekst: str) -> list[float]:
    """Parsira listu frekvencija iz teksta i vraca je u MHz.

    Svaka frekvencija je broj sa opcionom jedinicom (kHz, MHz, GHz; bez
    jedinice MHz), npr. '455 kHz', '2,4 GHz', '10.7'. Decimalni separator
    je tacka ili zarez ('453,200' = 453,2 MHz). Razdvojnici izmedju
    frekvencija: novi red, razmak, tacka-zarez, ili zarez PRACEN razmakom
    ('453.2, 458.75'). Zarez izmedju cifara je uvek decimalni, pa se
    '453,200,300' odbija kao nejasan unos umesto da se tiho pogresno procita."""
    rezultat = []
    kraj_prethodnog = 0
    for m in _BROJ_RE.finditer(tekst):
        razmak = tekst[kraj_prethodnog:m.start()]
        if rezultat or razmak.strip():
            # izmedju dva broja mora biti bar jedan razdvojnik, i to samo
            # razmak/;/zarez — zarez zalepljen za sledeci broj je nejasan
            if set(razmak) - set(" \t\r\n;,"):
                raise ValueError(tr("err_nepoznat", t=razmak.strip()))
            if rezultat and (not razmak or razmak.endswith(",")):
                raise ValueError(tr("err_nejasan", t=tekst[max(0, m.start() - 8):m.end()].strip()))
        broj, jedinica = m.group(1), m.group(2)
        rezultat.append(float(broj.replace(",", ".")) * _MNOZIOCI[jedinica and jedinica.lower()])
        kraj_prethodnog = m.end()
    ostatak = tekst[kraj_prethodnog:]
    if set(ostatak) - set(" \t\r\n;,"):
        raise ValueError(tr("err_nepoznat", t=ostatak.strip()))
    return rezultat


def _generisi_vektore(n: int, max_red: int):
    """Rekurzivno generise cele vektore k duzine n takve da je
    1 <= sum(|ki|) <= max_red, i to samo one ciji je prvi necioni
    koeficijent pozitivan: k i −k daju isti proizvod po |.|, pa se
    druga polovina ni ne generise. Odsecanje (pruning) po preostalom
    budzetu drzi kompleksnost podnošljivom i za veći broj predajnika."""

    def rekurzija(idx: int, budzet: int, tekuci: list[int], ima_necioni: bool):
        if idx == n:
            if ima_necioni:
                yield tuple(tekuci)
            return
        # Dok nije bilo necionog koeficijenta, negativni nisu dozvoljeni
        donja = -budzet if ima_necioni else 0
        for koef in range(donja, budzet + 1):
            tekuci.append(koef)
            yield from rekurzija(idx + 1, budzet - abs(koef), tekuci, ima_necioni or koef != 0)
            tekuci.pop()

    yield from rekurzija(0, max_red, [], False)


def broj_kombinacija(n: int, max_red: int) -> int:
    """Koliko vektora generise `_generisi_vektore` (bez njegovog pokretanja).
    Broj celih vektora sa sum(|ki|) <= r u n dimenzija je
    sum_j 2^j·C(n,j)·C(r,j); oduzima se nula-vektor i deli sa 2 (±k)."""
    ukupno = sum(2**j * math.comb(n, j) * math.comb(max_red, j) for j in range(min(n, max_red) + 1))
    return (ukupno - 1) // 2


def formataj_formulu(k: tuple[int, ...]) -> str:
    """Pravi citljiv zapis kombinacije tipa '2×F1 − F3 + F5'."""
    delovi = []
    for i, ki in enumerate(k, start=1):
        if ki == 0:
            continue
        znak = "+" if ki > 0 else "−"
        aps = abs(ki)
        clan = (f"{aps}×" if aps > 1 else "") + f"F{i}"
        if not delovi:
            delovi.append(("−" if ki < 0 else "") + clan)
        else:
            delovi.append(f" {znak} {clan}")
    return "".join(delovi)


def izracunaj(
    frekvencije: list[float],
    ciljevi: dict[str, float],
    max_red: int,
    tolerancija_khz: float,
    sve: bool = False,
    tolerancija_mf_khz: float | None = None,
) -> list[Rezultat]:
    """Generise sve kombinacije i vraca one koje padaju u toleranciju
    oko bilo kog cilja iz `ciljevi` (npr. {"RX": ..., "MF": ..., "LIK+": ...}).
    Za MF i lik frekvenciju se, ako je zadato, koristi polovina propusnog
    opsega MF filtra umesto opšte tolerancije (oba signala posle mešanja
    prolaze kroz MF filter). Osnovne frekvencije (red 1) se proveravaju
    samo za MF i lik — predajnik tačno na MF ili na liku je klasičan
    prodor, dok je predajnik na samoj RX frekvenciji trivijalan slučaj.
    Ako je `sve=True`, vraca se svaki proizvod reda >= 2 jednom (bez filtera)."""
    n = len(frekvencije)
    rezultati: dict[tuple, Rezultat] = {}
    for k in _generisi_vektore(n, max_red):
        proizvod = abs(sum(ki * f for ki, f in zip(k, frekvencije)))
        if proizvod == 0:
            # 0 Hz (DC) nas ne zanima
            continue
        red = sum(abs(ki) for ki in k)
        formula = formataj_formulu(k)
        if sve:
            if red < 2:
                continue
            # Bez filtera: prikazujemo proizvod sa odstupanjem od prvog cilja
            primljena = next(iter(ciljevi.values()))
            kljuc = (red, formula, round(proizvod, 9), "—")
            rezultati.setdefault(
                kljuc,
                Rezultat(red, formula, proizvod, (proizvod - primljena) * 1000.0, "—", k),
            )
            continue
        # Provera protiv svakog cilja; isti proizvod moze pogoditi i RX i MF.
        # Odstupanje se cuva sa znakom (+ iznad / − ispod centra propusnog
        # opsega), a prozor je simetrican pa se poredi apsolutna vrednost
        for cilj, f_cilj in ciljevi.items():
            if cilj == "RX" and red < 2:
                continue
            odstupanje_khz = (proizvod - f_cilj) * 1000.0
            granica = tolerancija_khz
            if cilj != "RX" and tolerancija_mf_khz is not None:
                granica = tolerancija_mf_khz
            if abs(odstupanje_khz) > granica:
                continue
            kljuc = (red, formula, round(proizvod, 9), cilj)
            if kljuc not in rezultati:
                rezultati[kljuc] = Rezultat(red, formula, proizvod, odstupanje_khz, cilj, k)
    # Sortiranje: po odstupanju SA znakom (negativna pre pozitivnih, kao
    # pri kliku na kolonu Odstupanje), pa po redu kombinacije
    return sorted(rezultati.values(), key=lambda r: (r.odstupanje, r.red))


def procena_nivoa_dbm(koef: tuple[int, ...], nivoi_tx: list[float], iip3_dbm: float) -> float:
    """Procena nivoa proizvoda u dBm iz izmerenih nivoa predajnika na ulazu
    prijemnika: P = Σ|ki|·Pi − (n−1)·IIP, gde je n red proizvoda. Za treći
    red je to standardna formula (2F1−F2: 2·P1 + P2 − 2·IIP3); za ostale
    redove je gruba procena jer se koristi isti IIP. Red 1 = nivo predajnika."""
    red = sum(abs(k) for k in koef)
    ulaz = sum(abs(k) * p for k, p in zip(koef, nivoi_tx))
    return ulaz - (red - 1) * iip3_dbm


def _lepi_podeoci(a: float, b: float, n: int = 5) -> list[float]:
    """Okrugle vrednosti podeoka ose (1/2/5 × 10^k) izmedju a i b."""
    if b <= a:
        return [a]
    korak = 10 ** math.floor(math.log10((b - a) / n))
    for m in (1, 2, 5, 10):
        if (b - a) / (korak * m) <= n:
            korak *= m
            break
    v = math.ceil(a / korak) * korak
    podeoci = []
    while v <= b + korak * 1e-9:
        podeoci.append(0.0 if abs(v) < korak * 1e-9 else v)
        v += korak
    return podeoci


def _broj(v: float) -> str:
    return dec(f"{v:g}").replace("-", "−")


@dataclass
class Panel:
    """Jedan panel grafika: osa X (kHz od cilja ili apsolutni MHz), oznaceni
    propusni opsezi i tacke (x, procenjeni nivo, rezultat, u_opsegu)."""

    naslov: str
    x_min: float
    x_max: float
    x_jedinica: str
    opsezi: list[tuple[float, float, str]]
    tacke: list[tuple[float, float, Rezultat, bool]]
    tezina: float = 1.0  # relativna širina panela u odnosu na ostale


class SpektarGrafik(QWidget):
    """Spektralni prikaz proizvoda: jedna vertikalna linija po proizvodu,
    visina = procena nivoa (procena_nivoa), osenčen propusni opseg cilja.
    Proizvodi u opsegu su istaknuti bojom, okolni (van opsega) sivi."""

    izabran = Signal(object)  # Rezultat na koji je kliknuto

    # Narandžasta iz referentne palete za "prodor"; siva za okolinu
    BOJA_U_OPSEGU = QColor("#eb6834")
    BOJA_VAN = QColor("#9a9892")
    BOJA_OPSEG = QColor(235, 104, 52, 38)
    BOJA_PRAG = QColor("#e34948")

    def __init__(self, legenda: bool = True, parent=None):
        super().__init__(parent)
        self.legenda = legenda
        self.paneli: list[Panel] = []
        self.poruka = ""  # tekst kad nema panela
        # Y opseg se zadaje spolja, da gornji i donji grafik imaju istu skalu
        self.y_opseg: tuple[float, float] = (-60.0, 0.0)
        # mereno=True: visine su procenjeni nivoi u dBm (iz izmerenog traga),
        # a `prag` je nivo iznad kog je prodor "jak" (crta se linijom)
        self.mereno = False
        self.prag: float | None = None
        self._pozicije: list[tuple[QPointF, Rezultat, float]] = []
        self.setMouseTracking(True)
        self.setMinimumHeight(150)

    def postavi(self, paneli: list[Panel], y_opseg: tuple[float, float], poruka: str = "",
                mereno: bool = False, prag: float | None = None):
        self.paneli = paneli
        self.y_opseg = y_opseg
        self.poruka = poruka
        self.mereno = mereno
        self.prag = prag
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        tekst = pal.color(QPalette.ColorRole.WindowText)
        slabo = QColor(tekst)
        slabo.setAlpha(110)
        mreza = QColor(tekst)
        mreza.setAlpha(28)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        self._pozicije = []
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        p.setFont(font)
        fm = p.fontMetrics()

        # Legenda (dve kategorije) u jednom redu gore — samo na gornjem grafiku
        if self.legenda:
            x_leg = 10
            for boja, opis in ((self.BOJA_U_OPSEGU, tr("leg_u")), (self.BOJA_VAN, tr("leg_van"))):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(boja)
                p.drawEllipse(QPointF(x_leg + 4, 12), 4, 4)
                p.setPen(tekst)
                p.drawText(x_leg + 12, 12 + fm.ascent() // 2 - 1, opis)
                x_leg += 24 + fm.horizontalAdvance(opis)

        if not self.paneli:
            p.setPen(slabo)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.poruka or tr("nema_rez"))
            return

        y_min, y_max = self.y_opseg
        levo, desno, dole = 44, 10, 34
        gore = 44 if self.legenda else 26
        # Širina panela srazmerna njegovoj težini
        jedinica = (self.width() - levo - desno) / sum(pn.tezina for pn in self.paneli)
        visina = self.height() - gore - dole
        razmak_panela = 14

        def y_pix(v):
            return gore + (y_max - v) / (y_max - y_min) * visina

        # Y osa (samo levo) i naziv
        for v in _lepi_podeoci(y_min, y_max, 5):
            y = y_pix(v)
            p.setPen(slabo)
            p.drawText(QRectF(0, y - 8, levo - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _broj(v))
        p.save()
        p.translate(10, gore + visina / 2)
        p.rotate(-90)
        p.setPen(slabo)
        # kad je grafik nizak i pun naziv ne staje, samo jedinica
        pun_naziv = tr("osa_y_dbm") if self.mereno else tr("osa_y")
        naziv_y = pun_naziv if fm.horizontalAdvance(pun_naziv) < visina else ("dBm" if self.mereno else "dB")
        p.drawText(QRectF(-visina / 2, -8, visina, 16), Qt.AlignmentFlag.AlignCenter, naziv_y)
        p.restore()

        pocetak = levo
        for i, pn in enumerate(self.paneli):
            sirina = pn.tezina * jedinica
            x0 = pocetak + (razmak_panela if i else 0)
            w = sirina - (razmak_panela if i else 0)
            pocetak += sirina

            def x_pix(v, x0=x0, w=w, pn=pn):
                return x0 + (v - pn.x_min) / (pn.x_max - pn.x_min) * w

            # horizontalna mreža
            p.setPen(QPen(mreza, 1))
            for v in _lepi_podeoci(y_min, y_max, 5):
                p.drawLine(QPointF(x0, y_pix(v)), QPointF(x0 + w, y_pix(v)))

            # prag jakog prodora (samo za izmerene nivoe)
            if self.prag is not None and y_min <= self.prag <= y_max:
                yp = y_pix(self.prag)
                p.setPen(QPen(self.BOJA_PRAG, 1.5, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(x0, yp), QPointF(x0 + w, yp))
                p.setPen(self.BOJA_PRAG)
                oznaka = dec(f"{tr('prag')} {self.prag:g} dBm")
                p.drawText(QPointF(x0 + w - fm.horizontalAdvance(oznaka) - 2, yp - 3), oznaka)

            # propusni opsezi: osenčeno, isprekidane ivice
            for a, b, _ in pn.opsezi:
                xa, xb = x_pix(a), x_pix(b)
                if xb - xa < 3:  # uzak opseg na širokoj osi: bar 3 px
                    c = (xa + xb) / 2
                    xa, xb = c - 1.5, c + 1.5
                p.fillRect(QRectF(xa, gore, xb - xa, visina), self.BOJA_OPSEG)
                p.setPen(QPen(self.BOJA_U_OPSEGU, 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(xa, gore), QPointF(xa, gore + visina))
                p.drawLine(QPointF(xb, gore), QPointF(xb, gore + visina))

            # X osa, podeoci i naslov panela
            p.setPen(QPen(slabo, 1))
            p.drawLine(QPointF(x0, gore + visina), QPointF(x0 + w, gore + visina))
            for v in _lepi_podeoci(pn.x_min, pn.x_max, max(2, int(w / 70))):
                x = x_pix(v)
                p.drawLine(QPointF(x, gore + visina), QPointF(x, gore + visina + 4))
                p.drawText(QRectF(x - 40, gore + visina + 4, 80, 14), Qt.AlignmentFlag.AlignCenter, _broj(v))
            p.drawText(QRectF(x0, gore + visina + 17, w, 14), Qt.AlignmentFlag.AlignCenter, pn.x_jedinica)
            p.setPen(tekst)
            # u uskom panelu naslov bez "(±…)" dela — opseg se vidi i na grafiku
            naslov = pn.naslov
            if fm.horizontalAdvance(naslov) > w:
                naslov = naslov.split("  (")[0]
            p.drawText(QRectF(x0, gore - 20, w, 16), Qt.AlignmentFlag.AlignCenter, naslov)

            # linije proizvoda: prvo okolina, pa prodori preko njih; kad je
            # gusto (režim "sve"), manji markeri bez oboda da ne prave mrlju
            gusto = len(pn.tacke) > w / 6
            r_markera = 2.5 if gusto else 4.5
            for u_opsegu in (False, True):
                boja = self.BOJA_U_OPSEGU if u_opsegu else self.BOJA_VAN
                for x_v, nivo, r, u in pn.tacke:
                    if u != u_opsegu:
                        continue
                    x, y = x_pix(x_v), y_pix(nivo)
                    p.setPen(QPen(boja, 2))
                    p.drawLine(QPointF(x, gore + visina), QPointF(x, y))
                    if gusto:
                        p.setPen(Qt.PenStyle.NoPen)
                    else:
                        p.setPen(QPen(pal.color(QPalette.ColorRole.Base), 2))
                    p.setBrush(boja)
                    p.drawEllipse(QPointF(x, y), r_markera, r_markera)
                    self._pozicije.append((QPointF(x, y), r, nivo))

            # Direktne oznake za najviše 3 najjača prodora u panelu; oznaka
            # koja bi se preklopila sa već ispisanom se preskače (ima tooltip)
            najjaci = sorted((t for t in pn.tacke if t[3]), key=lambda t: -t[1])
            zauzeto: list[QRectF] = []
            p.setPen(tekst)
            for x_v, nivo, r, _ in najjaci:
                if len(zauzeto) == 3:
                    break
                x, y = x_pix(x_v), y_pix(nivo)
                tw = fm.horizontalAdvance(r.formula)
                tx = min(max(x - tw / 2, x0), x0 + w - tw)
                ty = max(y - 8, gore + fm.ascent())
                okvir = QRectF(tx - 3, ty - fm.ascent(), tw + 6, fm.height())
                if any(okvir.intersects(z) for z in zauzeto):
                    continue
                zauzeto.append(okvir)
                pozadina = QColor(pal.color(QPalette.ColorRole.Base))
                pozadina.setAlpha(200)
                p.fillRect(okvir, pozadina)
                p.drawText(QPointF(tx, ty), r.formula)

    def _pogodak(self, pos) -> list[tuple[Rezultat, float]]:
        """Proizvodi čiji je marker blizu kursora (veća meta od markera)."""
        return [(r, nivo) for tacka, r, nivo in self._pozicije
                if abs(tacka.x() - pos.x()) <= 6 and abs(tacka.y() - pos.y()) <= 10]

    def mouseMoveEvent(self, e):
        pogodci = self._pogodak(e.position())
        if not pogodci:
            QToolTip.hideText()
            return
        redovi = []
        for r, nivo in pogodci[:8]:
            redovi.append(dec(
                f"<b>{r.formula}</b> &nbsp;({tr('tip_red')} {r.red}, {naziv_cilja(r.cilj)})<br>"
                f"{r.frekvencija:.4f} MHz, {tr('tip_odst')} {r.odstupanje:+.2f} kHz<br>"
                f"{tr('tip_nivo')} {nivo:.0f} {'dBm' if self.mereno else 'dB'}"
            ))
        if len(pogodci) > 8:
            redovi.append(tr("tip_jos", n=len(pogodci) - 8))
        QToolTip.showText(e.globalPosition().toPoint(), "<hr>".join(redovi), self)

    def mousePressEvent(self, e):
        pogodci = self._pogodak(e.position())
        if pogodci:
            self.izabran.emit(pogodci[0][0])


class TragGrafik(QWidget):
    """Prikaz izmerenog spektralnog traga: kriva nivoa, linija šuma i praga
    vrhova, pronađeni vrhovi i oznake predajnika/ciljeva (vertikalne linije)."""

    BOJA_TRAG = QColor("#2a78d6")
    BOJA_VRH = QColor("#eb6834")
    BOJA_TX = QColor("#1baf7a")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.trag: ms2711d.Trag | None = None
        self.vrhovi: list[tuple[float, float]] = []
        self.prag_vrhova: float | None = None
        self.oznake: list[tuple[float, str, bool]] = []  # (MHz, tekst, je_cilj)
        self._geom = None
        self.setMouseTracking(True)
        self.setMinimumHeight(150)

    def postavi(self, trag, vrhovi, prag_vrhova, oznake):
        self.trag, self.vrhovi, self.prag_vrhova, self.oznake = trag, vrhovi, prag_vrhova, oznake
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        tekst = pal.color(QPalette.ColorRole.WindowText)
        slabo = QColor(tekst)
        slabo.setAlpha(110)
        mreza = QColor(tekst)
        mreza.setAlpha(28)
        p.fillRect(self.rect(), pal.color(QPalette.ColorRole.Base))
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        p.setFont(font)
        fm = p.fontMetrics()
        self._geom = None
        t = self.trag
        if t is None:
            p.setPen(slabo)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("nema_traga"))
            return

        levo, desno, gore, dole = 48, 12, 22, 34
        w = self.width() - levo - desno
        h = self.height() - gore - dole
        x_min, x_max = t.frekvencije[0], t.frekvencije[-1]
        y_max = 10 * math.ceil((max(t.nivoi) + 5) / 10)
        y_min = min(10 * math.floor((min(t.nivoi) - 5) / 10), y_max - 30)
        self._geom = (levo, w, x_min, x_max)

        def xp(v):
            return levo + (v - x_min) / (x_max - x_min) * w

        def yp(v):
            return gore + (y_max - v) / (y_max - y_min) * h

        # mreža i ose
        for v in _lepi_podeoci(y_min, y_max, 6):
            p.setPen(QPen(mreza, 1))
            p.drawLine(QPointF(levo, yp(v)), QPointF(levo + w, yp(v)))
            p.setPen(slabo)
            p.drawText(QRectF(0, yp(v) - 8, levo - 6, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, _broj(v))
        p.setPen(QPen(slabo, 1))
        p.drawLine(QPointF(levo, gore + h), QPointF(levo + w, gore + h))
        for v in _lepi_podeoci(x_min, x_max, max(2, int(w / 80))):
            p.drawLine(QPointF(xp(v), gore + h), QPointF(xp(v), gore + h + 4))
            p.drawText(QRectF(xp(v) - 40, gore + h + 4, 80, 14), Qt.AlignmentFlag.AlignCenter, _broj(v))
        p.drawText(QRectF(levo, gore + h + 17, w, 14), Qt.AlignmentFlag.AlignCenter, "MHz")
        p.save()
        p.translate(10, gore + h / 2)
        p.rotate(-90)
        p.drawText(QRectF(-h / 2, -8, h, 16), Qt.AlignmentFlag.AlignCenter, tr("osa_nivo"))
        p.restore()

        # oznake predajnika (zeleno) i ciljeva RX/MF/lik (narandžasto, isprekidano)
        for f, oznaka, je_cilj in self.oznake:
            if not x_min <= f <= x_max:
                continue
            boja = self.BOJA_VRH if je_cilj else self.BOJA_TX
            p.setPen(QPen(boja, 1, Qt.PenStyle.DashLine if je_cilj else Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(xp(f), gore), QPointF(xp(f), gore + h))
            # natpis dole, iznad ose, da se ne sudara sa oznakama vrhova
            p.setPen(boja)
            p.drawText(QPointF(xp(f) + 3, gore + h - 4), oznaka)

        # šum i prag vrhova
        p.setPen(QPen(slabo, 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(levo, yp(t.sum)), QPointF(levo + w, yp(t.sum)))
        if self.prag_vrhova is not None and y_min <= self.prag_vrhova <= y_max:
            p.setPen(QPen(self.BOJA_VRH, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(levo, yp(self.prag_vrhova)), QPointF(levo + w, yp(self.prag_vrhova)))

        # kriva traga
        p.setPen(QPen(self.BOJA_TRAG, 1.5))
        tacke = [QPointF(xp(f), yp(v)) for f, v in zip(t.frekvencije, t.nivoi)]
        for a, b in zip(tacke, tacke[1:]):
            p.drawLine(a, b)

        # vrhovi: marker + oznaka frekvencije za najjače koji se ne preklapaju
        zauzeto: list[QRectF] = []
        for f, v in self.vrhovi:
            p.setPen(QPen(pal.color(QPalette.ColorRole.Base), 1.5))
            p.setBrush(self.BOJA_VRH)
            p.drawEllipse(QPointF(xp(f), yp(v)), 4, 4)
            oznaka = dec(f"{f:.3f}")
            tw = fm.horizontalAdvance(oznaka)
            tx = min(max(xp(f) - tw / 2, levo), levo + w - tw)
            ty = max(yp(v) - 7, gore + fm.ascent())
            okvir = QRectF(tx - 2, ty - fm.ascent(), tw + 4, fm.height())
            if len(zauzeto) < 10 and not any(okvir.intersects(z) for z in zauzeto):
                zauzeto.append(okvir)
                p.setPen(tekst)
                p.drawText(QPointF(tx, ty), oznaka)

    def mouseMoveEvent(self, e):
        if self._geom is None or self.trag is None:
            return
        levo, w, x_min, x_max = self._geom
        f = x_min + (e.position().x() - levo) / w * (x_max - x_min)
        if not x_min <= f <= x_max:
            QToolTip.hideText()
            return
        i = round((f - x_min) / self.trag.korak)
        QToolTip.showText(e.globalPosition().toPoint(),
                          dec(f"{self.trag.frekvencije[i]:.4f} MHz, {self.trag.nivoi[i]:.1f} dBm"), self)


class CitanjeInstrumenta(QThread):
    """Čita trag sa instrumenta u pozadini (traje nekoliko sekundi na 9600
    bauda), da prozor ne zamrzne."""

    gotovo = Signal(object, object, str)  # trag, spisak sačuvanih, greška

    def __init__(self, port: str, broj_traga: int, parent=None):
        super().__init__(parent)
        self.port, self.broj_traga = port, broj_traga

    def run(self):
        try:
            trag, spisak = ms2711d.procitaj(self.port, self.broj_traga)
            self.gotovo.emit(trag, spisak, "")
        except (OSError, ms2711d.GreskaInstrumenta) as e:
            self.gotovo.emit(None, None, str(e))


class Prozor(QMainWindow):
    """Glavni prozor aplikacije."""

    # Grafik prikazuje i proizvode van propusnog opsega, do ovoliko
    # širina (polovina opsega × OKOLINA), da se vidi šta je blizu ivice
    OKOLINA = 3.0

    # Iznad ovoliko kombinacija auto-proračun se ne pokreće (≈1 s računanja)
    MAX_AUTO_KOMBINACIJA = 300_000

    def __init__(self):
        super().__init__()
        self.resize(1000, 940)
        self.rezultati: list[Rezultat] = []
        # Za grafik: svi proizvodi do OKOLINA × granica oko ciljeva
        self._okolina: list[Rezultat] = []
        self._ciljevi: dict[str, float] = {}
        self._granice: dict[str, float] = {}
        self._emitovane: list[float] = []
        # Izmereni trag (instrument ili fajl) i iz njega procenjeni nivoi
        # proizvoda po id(Rezultat); None = procena po redu (bez merenja)
        self._trag: ms2711d.Trag | None = None
        self._spisak_tragova: list[tuple[int, str, str]] = []
        self._nivoi_mereni: dict[int, float] | None = None
        self._napomena_nivoa = ""
        self._citanje: CitanjeInstrumenta | None = None
        self._racunato = False
        # Status se čuva kao funkcija, da se pri promeni jezika ponovo ispiše
        self._status_fn = lambda: ""
        self._podesavanja = QSettings("YT1BN", "intermod")
        global JEZIK
        JEZIK = "sr" if self._podesavanja.value("jezik", "en") == "sr" else "en"
        self._napravi_ui()
        self._prevedi()

    def _napravi_ui(self):
        central = QWidget()
        glavni = QVBoxLayout(central)

        # ---------- Ulazni parametri ----------
        # Tekstovi (labele, tooltipovi...) se postavljaju u _prevedi()
        self.grp_ulaz = QGroupBox()
        forma = QFormLayout(self.grp_ulaz)

        self.txt_emitovane = QTextEdit()
        self.txt_emitovane.setFixedHeight(110)
        self.lbl_emit = QLabel()
        forma.addRow(self.lbl_emit, self.txt_emitovane)

        self.txt_primljena = QLineEdit()
        self.lbl_rx = QLabel()
        forma.addRow(self.lbl_rx, self.txt_primljena)

        # Medjurekvencija je opciona: kad se unese, kombinacije se proveri
        # i protiv nje (direktan prodor na ulaz MF pojacavala)
        self.chk_mf = QCheckBox()
        forma.addRow("", self.chk_mf)
        self.txt_mf = QLineEdit()
        self.txt_mf.setEnabled(False)
        self.chk_mf.toggled.connect(self.txt_mf.setEnabled)
        self.lbl_mf = QLabel()
        forma.addRow(self.lbl_mf, self.txt_mf)

        # Standardni propusni opsezi MF filtra; prodor na MF se računa
        # unutar ±polovine izabrane širine oko MF, ne zajedničkom tolerancijom
        self.cmb_mf_filter = QComboBox()
        for khz in (7.0, 12.5, 25.0):
            self.cmb_mf_filter.addItem("", khz)
        self.cmb_mf_filter.setCurrentIndex(2)
        self.cmb_mf_filter.setEnabled(False)
        self.chk_mf.toggled.connect(self.cmb_mf_filter.setEnabled)
        self.lbl_filter = QLabel()
        forma.addRow(self.lbl_filter, self.cmb_mf_filter)

        # Lik frekvencija: signal na f_RX ± 2·MF daje istu MF posle mešanja;
        # strana zavisi od toga da li je LO iznad ili ispod primljene
        self.cmb_lo = QComboBox()
        for strana in ("iznad", "ispod", "obe"):
            self.cmb_lo.addItem("", strana)
        self.cmb_lo.setCurrentIndex(2)
        self.cmb_lo.setEnabled(False)
        self.chk_mf.toggled.connect(self.cmb_lo.setEnabled)
        self.lbl_lo = QLabel()
        forma.addRow(self.lbl_lo, self.cmb_lo)

        self.spn_red = QSpinBox()
        self.spn_red.setRange(2, 10)
        self.spn_red.setValue(5)
        self.lbl_red = QLabel()
        forma.addRow(self.lbl_red, self.spn_red)

        self.spn_tol = QDoubleSpinBox()
        # Do 100 MHz: postoje prijemnici sa ulaznim band-pass filtrom do 50 MHz
        self.spn_tol.setRange(0.1, 100000.0)
        self.spn_tol.setDecimals(1)
        self.spn_tol.setValue(10.0)
        self.spn_tol.setSuffix(" kHz")
        self.lbl_tol = QLabel()
        forma.addRow(self.lbl_tol, self.spn_tol)

        self.chk_sve = QCheckBox()
        forma.addRow("", self.chk_sve)

        glavni.addWidget(self.grp_ulaz)

        # ---------- Dugmad, status i jezik ----------
        red_dugmici = QHBoxLayout()
        self.btn_izracunaj = QPushButton()
        self.btn_izracunaj.clicked.connect(lambda: self._proracun(False))
        red_dugmici.addWidget(self.btn_izracunaj)
        # Komplet ulaza (frekvencije i parametri) se može sačuvati u JSON i
        # kasnije učitati, da se setovi predajnika ne krcaju svaki put
        self.btn_snimi = QPushButton()
        self.btn_snimi.clicked.connect(self._snimi)
        red_dugmici.addWidget(self.btn_snimi)
        self.btn_ucitaj = QPushButton()
        self.btn_ucitaj.clicked.connect(self._ucitaj)
        red_dugmici.addWidget(self.btn_ucitaj)
        self.lbl_status = QLabel("")
        red_dugmici.addWidget(self.lbl_status, stretch=1)
        self.lbl_jezik = QLabel()
        red_dugmici.addWidget(self.lbl_jezik)
        self.cmb_jezik = QComboBox()
        self.cmb_jezik.addItem("English", "en")
        self.cmb_jezik.addItem("Srpski", "sr")
        self.cmb_jezik.setCurrentIndex(self.cmb_jezik.findData(JEZIK))
        self.cmb_jezik.currentIndexChanged.connect(self._promeni_jezik)
        red_dugmici.addWidget(self.cmb_jezik)
        glavni.addLayout(red_dugmici)

        # ---------- Tabela rezultata ----------
        self.tabela = QTableWidget(0, 6)
        # Sve kolone se mogu ručno menjati prevlačenjem (ranije je kolona
        # 'Kombinacija' bila fiksno razvučena, ostale zaključane)
        self.tabela.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tabela.setColumnWidth(0, 70)
        self.tabela.setColumnWidth(1, 50)
        self.tabela.setColumnWidth(2, 320)
        self.tabela.setColumnWidth(3, 150)
        self.tabela.setColumnWidth(4, 130)
        self.tabela.setColumnWidth(5, 110)
        self.tabela.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabela.verticalHeader().setVisible(False)
        self.tabela.setFont(QFont("monospace"))
        # Sortiranje klikom na zaglavlje za poslednje dve kolone
        # (Frekvencija, Odstupanje); numerički, ne po tekstu
        self._kolona_sorta = None
        self._red_sorta = Qt.SortOrder.AscendingOrder
        self.tabela.horizontalHeader().setSortIndicatorShown(True)
        self.tabela.horizontalHeader().sectionClicked.connect(self._klik_zaglavlja)

        # ---------- Grafici ----------
        # Gore glavni (RX, ili ceo spektar u režimu "sve"), dole MF i lik;
        # tabela i oba grafika dele prostor preko splitera
        grafik_okvir = QWidget()
        grafik_raspored = QVBoxLayout(grafik_okvir)
        grafik_raspored.setContentsMargins(0, 0, 0, 0)
        red_grafik = QHBoxLayout()
        self.lbl_slab = QLabel()
        red_grafik.addWidget(self.lbl_slab)
        self.spn_slabljenje = QDoubleSpinBox()
        self.spn_slabljenje.setRange(0.0, 60.0)
        self.spn_slabljenje.setDecimals(0)
        self.spn_slabljenje.setValue(20.0)
        self.spn_slabljenje.setSuffix(" dB")
        self.spn_slabljenje.valueChanged.connect(self._osvezi_grafik)
        red_grafik.addWidget(self.spn_slabljenje)
        self.lbl_napomena = QLabel()
        self.lbl_napomena.setEnabled(False)
        red_grafik.addWidget(self.lbl_napomena, stretch=1)
        grafik_raspored.addLayout(red_grafik)
        self.grafik_rx = SpektarGrafik(legenda=True)
        self.grafik_rx.izabran.connect(self._izaberi_u_tabeli)
        grafik_raspored.addWidget(self.grafik_rx, stretch=1)
        self.grafik_mf = SpektarGrafik(legenda=False)
        self.grafik_mf.izabran.connect(self._izaberi_u_tabeli)

        spliter = QSplitter(Qt.Orientation.Vertical)
        spliter.addWidget(self.tabela)
        # Kartice: proizvodi (grafik RX) i izmereni spektar sa instrumenta
        self.tabovi = QTabWidget()
        self.tabovi.addTab(grafik_okvir, "")
        self.tabovi.addTab(self._napravi_spektar_tab(), "")
        spliter.addWidget(self.tabovi)
        spliter.addWidget(self.grafik_mf)
        # Početni odnos: glavni (RX) grafik najviši; Qt skalira na stvarnu visinu
        spliter.setSizes([160, 320, 200])
        glavni.addWidget(spliter, stretch=1)

        # Promena bilo kog parametra pokreće novi proračun (automatski), ali
        # tek kad je bar jednom ručno pokrenut — da ne iskaču greške dok se
        # polja tek popunjavaju. Tajmer skuplja brze izmene u jedno računanje.
        self._tajmer = QTimer(self)
        self._tajmer.setSingleShot(True)
        self._tajmer.setInterval(400)
        self._tajmer.timeout.connect(lambda: self._proracun(True))
        for izvor in (
            self.txt_emitovane.textChanged,
            self.txt_primljena.textChanged,
            self.txt_mf.textChanged,
            self.spn_tol.valueChanged,
            self.spn_red.valueChanged,
            self.chk_mf.toggled,
            self.chk_sve.toggled,
            self.cmb_mf_filter.currentIndexChanged,
            self.cmb_lo.currentIndexChanged,
        ):
            izvor.connect(self._tajmer.start)

        self.setCentralWidget(central)

    def _napravi_spektar_tab(self) -> QWidget:
        """Kartica za izmereni spektar: preuzimanje sa MS2711D, fajl,
        vrhovi i podešavanja procene nivoa iz merenja."""
        okvir = QWidget()
        raspored = QVBoxLayout(okvir)
        raspored.setContentsMargins(0, 4, 0, 0)

        red1 = QHBoxLayout()
        self.lbl_port = QLabel()
        red1.addWidget(self.lbl_port)
        self.cmb_port = QComboBox()
        self.cmb_port.setEditable(True)
        self.cmb_port.addItems(ms2711d.portovi())
        sacuvan = self._podesavanja.value("port", "")
        if sacuvan:
            self.cmb_port.setCurrentText(sacuvan)
        self.cmb_port.setMinimumWidth(130)
        red1.addWidget(self.cmb_port)
        self.lbl_broj_traga = QLabel()
        red1.addWidget(self.lbl_broj_traga)
        self.cmb_trag = QComboBox()
        self.cmb_trag.setMinimumWidth(200)
        red1.addWidget(self.cmb_trag)
        self.btn_preuzmi = QPushButton()
        self.btn_preuzmi.clicked.connect(self._preuzmi_trag)
        red1.addWidget(self.btn_preuzmi)
        self.btn_ucitaj_trag = QPushButton()
        self.btn_ucitaj_trag.clicked.connect(self._ucitaj_trag)
        red1.addWidget(self.btn_ucitaj_trag)
        self.btn_snimi_trag = QPushButton()
        self.btn_snimi_trag.clicked.connect(self._snimi_trag)
        red1.addWidget(self.btn_snimi_trag)
        red1.addStretch(1)
        raspored.addLayout(red1)

        red2 = QHBoxLayout()
        self.lbl_vrh = QLabel()
        red2.addWidget(self.lbl_vrh)
        self.spn_vrh = QDoubleSpinBox()
        self.spn_vrh.setRange(1.0, 60.0)
        self.spn_vrh.setDecimals(0)
        self.spn_vrh.setValue(10.0)
        self.spn_vrh.setSuffix(" dB")
        self.spn_vrh.valueChanged.connect(self._osvezi_trag)
        red2.addWidget(self.spn_vrh)
        self.btn_vrhovi = QPushButton()
        self.btn_vrhovi.clicked.connect(self._vrhovi_u_emitovane)
        red2.addWidget(self.btn_vrhovi)
        red2.addSpacing(16)
        self.chk_mereno = QCheckBox()
        self.chk_mereno.toggled.connect(self._osvezi_nivoe)
        red2.addWidget(self.chk_mereno)
        self.lbl_iip = QLabel()
        red2.addWidget(self.lbl_iip)
        self.spn_iip = QDoubleSpinBox()
        self.spn_iip.setRange(-50.0, 50.0)
        self.spn_iip.setDecimals(0)
        self.spn_iip.setValue(0.0)
        self.spn_iip.setSuffix(" dBm")
        self.spn_iip.valueChanged.connect(self._osvezi_nivoe)
        red2.addWidget(self.spn_iip)
        self.lbl_prag = QLabel()
        red2.addWidget(self.lbl_prag)
        self.spn_prag = QDoubleSpinBox()
        self.spn_prag.setRange(-160.0, 30.0)
        self.spn_prag.setDecimals(0)
        self.spn_prag.setValue(-110.0)
        self.spn_prag.setSuffix(" dBm")
        self.spn_prag.valueChanged.connect(self._osvezi_nivoe)
        red2.addWidget(self.spn_prag)
        red2.addStretch(1)
        raspored.addLayout(red2)

        self.lbl_trag_info = QLabel()
        self.lbl_trag_info.setEnabled(False)
        raspored.addWidget(self.lbl_trag_info)
        self.grafik_trag = TragGrafik()
        raspored.addWidget(self.grafik_trag, stretch=1)
        self._omoguci_trag_dugmad()
        return okvir

    def _omoguci_trag_dugmad(self):
        ima = self._trag is not None
        for wdg in (self.btn_snimi_trag, self.btn_vrhovi, self.chk_mereno):
            wdg.setEnabled(ima)

    def _popuni_cmb_trag(self):
        """Stavka 0 (ekran) + sačuvani tragovi sa poslednjeg čitanja."""
        izabran = self.cmb_trag.currentData()
        self.cmb_trag.blockSignals(True)
        self.cmb_trag.clear()
        self.cmb_trag.addItem(tr("trag_ekran"), 0)
        for broj, vreme, naziv in self._spisak_tragova:
            self.cmb_trag.addItem(f"{broj} — {vreme} {naziv}".strip(), broj)
        i = self.cmb_trag.findData(izabran)
        self.cmb_trag.setCurrentIndex(i if i >= 0 else 0)
        self.cmb_trag.blockSignals(False)

    def _preuzmi_trag(self):
        port = self.cmb_port.currentText().strip()
        if not port or self._citanje is not None:
            return
        self.btn_preuzmi.setEnabled(False)
        self._postavi_status(lambda: tr("citam", p=port))
        self._citanje = CitanjeInstrumenta(port, int(self.cmb_trag.currentData() or 0), self)
        self._citanje.gotovo.connect(self._trag_preuzet)
        self._citanje.start()

    def _trag_preuzet(self, trag, spisak, greska: str):
        self._citanje.wait()
        self._citanje = None
        self.btn_preuzmi.setEnabled(True)
        if greska:
            self._postavi_status(lambda: f"{tr('err_instr')}: {greska}")
            QMessageBox.critical(self, tr("err_instr"), greska)
            return
        self._podesavanja.setValue("port", self.cmb_port.currentText().strip())
        self._spisak_tragova = spisak
        self._popuni_cmb_trag()
        self._postavi_trag(trag)

    def _ucitaj_trag(self):
        putanja, _ = QFileDialog.getOpenFileName(
            self, tr("ucitaj_trag"), os.path.expanduser("~"), "CSV / text (*.csv *.txt *.dat);;* (*)")
        if not putanja:
            return
        try:
            trag = ms2711d.ucitaj_csv(putanja)
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, tr("err_trag_fajl"), f"{putanja}: {e}")
            return
        self._postavi_trag(trag)

    def _snimi_trag(self):
        if self._trag is None:
            return
        putanja, _ = QFileDialog.getSaveFileName(
            self, tr("snimi_trag"), os.path.join(os.path.expanduser("~"), "trag.csv"), "CSV (*.csv)")
        if not putanja:
            return
        try:
            ms2711d.sacuvaj_csv(self._trag, putanja)
        except OSError as e:
            QMessageBox.critical(self, tr("err_snimanje"), str(e))
            return
        self._postavi_status(lambda: tr("sacuvano", p=putanja))

    def _postavi_trag(self, trag: ms2711d.Trag):
        self._trag = trag
        self._omoguci_trag_dugmad()
        self._opisi_trag()
        self.tabovi.setCurrentIndex(1)
        self._postavi_status(lambda: tr("trag_ucitan", o=trag.opis))
        self._osvezi_trag()
        self._osvezi_nivoe()

    def _opisi_trag(self):
        t = self._trag
        if t is None:
            self.lbl_trag_info.setText("")
            return
        # dec() samo na brojeve, ne na opis (u njemu je npr. verzija firmvera)
        self.lbl_trag_info.setText(tr(
            "trag_info", o=t.opis, f0=dec(f"{t.frekvencije[0]:.3f}"), f1=dec(f"{t.frekvencije[-1]:.3f}"),
            n=len(t.nivoi), rbw=dec(f"{t.rbw_khz:g}") if t.rbw_khz else "?", sum=dec(f"{t.sum:.1f}")))

    def _osvezi_trag(self):
        """Crta trag sa vrhovima i oznakama predajnika i ciljeva."""
        t = self._trag
        if t is None:
            self.grafik_trag.postavi(None, [], None, [])
            return
        oznake = [(f, f"F{i}", False) for i, f in enumerate(self._emitovane, start=1)]
        oznake += [(f, naziv_cilja(c), True) for c, f in self._ciljevi.items()]
        self.grafik_trag.postavi(t, t.vrhovi(self.spn_vrh.value()), t.sum + self.spn_vrh.value(), oznake)

    def _vrhovi_u_emitovane(self):
        """Upisuje frekvencije vrhova iz traga u emitovane frekvencije."""
        if self._trag is None:
            return
        vrhovi = self._trag.vrhovi(self.spn_vrh.value())
        if not vrhovi:
            QMessageBox.information(self, tr("tab_spektar"), tr("nema_vrhova"))
            return
        if self.txt_emitovane.toPlainText().strip() and QMessageBox.question(
            self, tr("tab_spektar"), tr("zameni_tx", n=len(vrhovi))
        ) != QMessageBox.StandardButton.Yes:
            return
        self.txt_emitovane.setPlainText("\n".join(f"{f:.4f}" for f, _ in sorted(vrhovi)))
        self.chk_mereno.setChecked(True)

    def _osvezi_nivoe(self):
        """Procenjuje nivoe proizvoda iz izmerenog traga (ako je uključeno i
        svi predajnici su u opsegu traga), pa osvežava tabelu, grafik i status."""
        self._nivoi_mereni = None
        self._napomena_nivoa = ""
        if self.chk_mereno.isChecked() and self._trag is not None and self._emitovane:
            nivoi_tx = [self._trag.nivo_na(f) for f in self._emitovane]
            if None in nivoi_tx:
                self._napomena_nivoa = tr("van_traga", i=nivoi_tx.index(None) + 1)
            else:
                iip = self.spn_iip.value()
                self._nivoi_mereni = {id(r): procena_nivoa_dbm(r.koef, nivoi_tx, iip) for r in self._okolina}
        self._popuni_tabelu()
        self._osvezi_grafik()
        self.lbl_status.setText(self._status_fn())

    def _jak(self, r: Rezultat) -> bool:
        return self._nivoi_mereni is not None and self._nivoi_mereni[id(r)] >= self.spn_prag.value()

    def _prevedi(self):
        """Postavlja sve tekstove interfejsa na trenutni jezik (JEZIK)."""
        self.setWindowTitle(tr("naslov"))
        self.grp_ulaz.setTitle(tr("ulaz"))
        self.lbl_emit.setText(tr("emit_lbl"))
        self.txt_emitovane.setPlaceholderText(tr("emit_ph"))
        self.lbl_rx.setText(tr("rx_lbl"))
        self.txt_primljena.setPlaceholderText(tr("rx_ph"))
        self.chk_mf.setText(tr("mf_chk"))
        self.lbl_mf.setText(tr("mf_lbl"))
        self.txt_mf.setPlaceholderText(tr("mf_ph"))
        self.lbl_filter.setText(tr("filter_lbl"))
        self.cmb_mf_filter.setToolTip(tr("filter_tip"))
        for i in range(self.cmb_mf_filter.count()):
            self.cmb_mf_filter.setItemText(i, dec(f"{self.cmb_mf_filter.itemData(i):g} kHz"))
        self.lbl_lo.setText(tr("lo_lbl"))
        self.cmb_lo.setToolTip(tr("lo_tip"))
        for i, kljuc in enumerate(("lo_iznad", "lo_ispod", "lo_obe")):
            self.cmb_lo.setItemText(i, tr(kljuc))
        self.lbl_red.setText(tr("red_lbl"))
        self.spn_red.setToolTip(tr("red_tip"))
        self.lbl_tol.setText(tr("tol_lbl"))
        self.spn_tol.setToolTip(tr("tol_tip"))
        self.chk_sve.setText(tr("sve_chk"))
        self.btn_izracunaj.setText(tr("izracunaj"))
        self.btn_snimi.setText(tr("snimi"))
        self.btn_snimi.setToolTip(tr("snimi_tip"))
        self.btn_ucitaj.setText(tr("ucitaj"))
        self.btn_ucitaj.setToolTip(tr("ucitaj_tip"))
        self.lbl_jezik.setText(tr("jezik"))
        self.tabela.setHorizontalHeaderLabels(tr("kolone").split("|"))
        self.lbl_slab.setText(tr("slab_lbl"))
        self.spn_slabljenje.setToolTip(tr("slab_tip"))
        self.tabovi.setTabText(0, tr("tab_proizvodi"))
        self.tabovi.setTabText(1, tr("tab_spektar"))
        self.lbl_port.setText(tr("port"))
        self.lbl_broj_traga.setText(tr("trag"))
        self._popuni_cmb_trag()
        self.btn_preuzmi.setText(tr("preuzmi"))
        self.btn_preuzmi.setToolTip(tr("preuzmi_tip"))
        self.btn_ucitaj_trag.setText(tr("ucitaj_trag"))
        self.btn_ucitaj_trag.setToolTip(tr("ucitaj_trag_tip"))
        self.btn_snimi_trag.setText(tr("snimi_trag"))
        self.lbl_vrh.setText(tr("vrh_lbl"))
        self.btn_vrhovi.setText(tr("vrhovi_btn"))
        self.btn_vrhovi.setToolTip(tr("vrhovi_tip"))
        self.chk_mereno.setText(tr("mereno_chk"))
        self.chk_mereno.setToolTip(tr("mereno_tip"))
        self.lbl_iip.setText(tr("iip_lbl"))
        self.lbl_prag.setText(tr("prag_lbl"))
        self.spn_prag.setToolTip(tr("prag_tip"))
        self._opisi_trag()
        self.grafik_trag.update()
        self.lbl_status.setText(self._status_fn())
        self._popuni_tabelu()
        self._osvezi_grafik()

    def _promeni_jezik(self):
        global JEZIK
        JEZIK = self.cmb_jezik.currentData()
        self._podesavanja.setValue("jezik", JEZIK)
        self._prevedi()

    def _postavi_status(self, fn):
        """Status se zadaje funkcijom, da bi se preveo pri promeni jezika."""
        self._status_fn = fn
        self.lbl_status.setText(fn())

    def _klik_zaglavlja(self, kol: int):
        """Ponovo poredja rezultate po kliknutoj koloni; drugi klik na istu
        kolonu okreće smer (uzlazno/silazno)."""
        if kol not in (3, 4, 5) or (kol == 5 and self._nivoi_mereni is None):
            return
        if self._kolona_sorta == kol:
            self._red_sorta = (
                Qt.SortOrder.DescendingOrder
                if self._red_sorta == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._kolona_sorta = kol
            self._red_sorta = Qt.SortOrder.AscendingOrder
        kljuc = {
            3: lambda r: r.frekvencija,
            4: lambda r: r.odstupanje,
            5: lambda r: self._nivoi_mereni[id(r)],
        }[kol]
        self.rezultati.sort(key=kljuc, reverse=self._red_sorta == Qt.SortOrder.DescendingOrder)
        self.tabela.horizontalHeader().setSortIndicator(kol, self._red_sorta)
        self._popuni_tabelu()

    def _proracun(self, quiet: bool):
        """Računa i popunjava tabelu. quiet=True je auto-režim (promena
        parametra): ne prikazuje prozore, a kod neispravnog unosa samo
        prekida — stara tabela ostaje dok unos ne postane valjan."""
        if quiet and not self._racunato:
            return
        try:
            emitovane = parse_frekvencije(self.txt_emitovane.toPlainText())
        except ValueError as e:
            if not quiet:
                QMessageBox.critical(self, tr("neispravan"), tr("err_emit", e=e))
            return
        if not emitovane:
            if not quiet:
                QMessageBox.warning(self, tr("neispravan"), tr("err_nema_emit"))
            return
        # Procena obima pre računanja: auto-režim (tokom kucanja) ne sme da
        # zamrzne prozor, pa se kod velikog broja kombinacija samo prekida
        # i traži ručno pokretanje, gde korisnik potvrđuje nastavak
        broj = broj_kombinacija(len(emitovane), self.spn_red.value())
        if broj > self.MAX_AUTO_KOMBINACIJA:
            if quiet:
                self._postavi_status(lambda: tr("mnogo_auto", n=hiljade(broj)))
                return
            if QMessageBox.question(
                self, tr("oprez"), tr("mnogo_pitanje", n=hiljade(broj))
            ) != QMessageBox.StandardButton.Yes:
                return
        try:
            primljena = parse_frekvencije(self.txt_primljena.text())[0]
        except (ValueError, IndexError):
            if not quiet:
                QMessageBox.critical(self, tr("neispravan"), tr("err_rx"))
            return

        ciljevi: dict[str, float] = {"RX": primljena}
        tolerancija_mf: float | None = None
        if self.chk_mf.isChecked():
            try:
                ciljevi["MF"] = parse_frekvencije(self.txt_mf.text())[0]
            except (ValueError, IndexError):
                if not quiet:
                    QMessageBox.critical(self, tr("neispravan"), tr("err_mf"))
                return
            # prodor na MF: samo unutar ±polovine propusnog opsega filtra
            tolerancija_mf = float(self.cmb_mf_filter.currentData()) / 2.0
            # lik frekvencija; ispod nule nema smisla (MF veća od pola RX)
            strana = self.cmb_lo.currentData()
            if strana in ("iznad", "obe"):
                ciljevi["LIK+"] = primljena + 2.0 * ciljevi["MF"]
            if strana in ("ispod", "obe") and primljena - 2.0 * ciljevi["MF"] > 0:
                ciljevi["LIK−"] = primljena - 2.0 * ciljevi["MF"]

        # "Ulazni opseg RX" je puna širina — rezultat se prihvata u
        # ±polovini oko primljene, isto kao kod MF filtra
        granica_rx = self.spn_tol.value() / 2.0
        granice = {c: (granica_rx if c == "RX" or tolerancija_mf is None else tolerancija_mf)
                   for c in ciljevi}
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # Računa se sa širim prozorom (OKOLINA) zbog grafika; tabela
            # prikazuje samo proizvode unutar stvarnog opsega
            self._okolina = izracunaj(
                emitovane,
                ciljevi,
                self.spn_red.value(),
                granica_rx * self.OKOLINA,
                sve=self.chk_sve.isChecked(),
                tolerancija_mf_khz=None if tolerancija_mf is None else tolerancija_mf * self.OKOLINA,
            )
        finally:
            QApplication.restoreOverrideCursor()
        if self.chk_sve.isChecked():
            self.rezultati = list(self._okolina)
        else:
            self.rezultati = [r for r in self._okolina if abs(r.odstupanje) <= granice[r.cilj]]
        self._ciljevi = ciljevi
        self._granice = granice
        self._emitovane = emitovane
        self._racunato = True
        self._kolona_sorta = None
        self.tabela.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._osvezi_nivoe()  # nivoi iz traga + tabela + grafik
        self._osvezi_trag()

        rezultati = self.rezultati
        n_emit, red = len(emitovane), self.spn_red.value()

        def status():
            rx = sum(1 for r in rezultati if r.cilj == "RX")
            mf = sum(1 for r in rezultati if r.cilj == "MF")
            lik = sum(1 for r in rezultati if r.cilj.startswith("LIK"))
            deo_mf = tr("status_mf", mf=mf, lik=lik, tol=f"{tolerancija_mf:g}") if "MF" in ciljevi else ""
            deo_lik = "".join(
                f", {naziv_cilja(c)} = {ciljevi[c]:.4f} MHz" for c in ("LIK+", "LIK−") if c in ciljevi
            )
            # jaki prodori se broje u trenutku ispisa (IIP/prag se menjaju bez proračuna)
            if self._nivoi_mereni is not None and rezultati is self.rezultati:
                jaki = sum(1 for r in rezultati if self._jak(r))
                deo_lik += tr("status_jaki", n=jaki, prag=f"{self.spn_prag.value():g}")
            else:
                deo_lik += self._napomena_nivoa
            return dec(tr("status", n=n_emit, red=red, uk=len(rezultati), rx=rx,
                          tol=f"{granica_rx:g}", mf=deo_mf, lik=deo_lik))

        self._postavi_status(status)

    def _snimi(self):
        """Upisuje komplet ulaznih parametara u JSON fajl."""
        putanja, _ = QFileDialog.getSaveFileName(
            self,
            tr("snimi_naslov"),
            os.path.join(os.path.expanduser("~"), "intermod.json"),
            "Intermod set (*.json)",
        )
        if not putanja:
            return
        podaci = {
            "version": 1,
            "tx": self.txt_emitovane.toPlainText(),
            "rx": self.txt_primljena.text(),
            "mf": self.chk_mf.isChecked(),
            "mf_freq": self.txt_mf.text(),
            "mf_filter_khz": float(self.cmb_mf_filter.currentData()),
            "lo_strana": self.cmb_lo.currentData(),
            "max_order": self.spn_red.value(),
            "tolerancija_khz": self.spn_tol.value(),
            "show_all": self.chk_sve.isChecked(),
            "slabljenje_db": self.spn_slabljenje.value(),
            "mereni_nivoi": self.chk_mereno.isChecked(),
            "iip3_dbm": self.spn_iip.value(),
            "prag_dbm": self.spn_prag.value(),
            "vrhovi_db": self.spn_vrh.value(),
        }
        try:
            with open(putanja, "w", encoding="utf-8") as f:
                json.dump(podaci, f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.critical(self, tr("err_snimanje"), str(e))
            return
        self._postavi_status(lambda: tr("sacuvano", p=putanja))

    def _ucitaj(self):
        """Popunjava sva polja iz fajla snimljenog preko 'Sačuvaj...'."""
        putanja, _ = QFileDialog.getOpenFileName(
            self,
            tr("ucitaj_naslov"),
            os.path.expanduser("~"),
            "Intermod set (*.json)",
        )
        if not putanja:
            return
        try:
            with open(putanja, encoding="utf-8") as f:
                podaci = json.load(f)
            self.txt_emitovane.setPlainText(podaci["tx"])
            self.txt_primljena.setText(podaci["rx"])
            self.chk_mf.setChecked(bool(podaci.get("mf", False)))
            self.txt_mf.setText(podaci.get("mf_freq") or "")
            self._izaberi_mf_filter(float(podaci.get("mf_filter_khz", 25.0)))
            i_lo = self.cmb_lo.findData(podaci.get("lo_strana", "obe"))
            self.cmb_lo.setCurrentIndex(i_lo if i_lo >= 0 else 2)
            self.spn_red.setValue(int(podaci.get("max_order", 5)))
            self.spn_tol.setValue(float(podaci.get("tolerancija_khz", 10.0)))
            self.chk_sve.setChecked(bool(podaci.get("show_all", False)))
            self.spn_slabljenje.setValue(float(podaci.get("slabljenje_db", 20.0)))
            self.spn_iip.setValue(float(podaci.get("iip3_dbm", 0.0)))
            self.spn_prag.setValue(float(podaci.get("prag_dbm", -110.0)))
            self.spn_vrh.setValue(float(podaci.get("vrhovi_db", 10.0)))
            self.chk_mereno.setChecked(bool(podaci.get("mereni_nivoi", False)))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, tr("err_fajl"), f"{putanja}: {e}")
            return
        self._postavi_status(lambda: tr("ucitano", p=putanja))

    def _izaberi_mf_filter(self, khz: float):
        """Bira stavku combo po vrednosti iz fajla; nestandardnu vrednost
        (npr. iz starog fajla) svaljuje na 25 kHz."""
        for i in range(self.cmb_mf_filter.count()):
            if abs(float(self.cmb_mf_filter.itemData(i)) - khz) < 1e-6:
                self.cmb_mf_filter.setCurrentIndex(i)
                return
        self.cmb_mf_filter.setCurrentIndex(2)

    def _osvezi_grafik(self):
        """Pravi panele za oba grafika iz poslednjeg proračuna. Gore: RX
        (osa X = odstupanje u kHz), ili ceo spektar u režimu "sve". Dole:
        po jedan panel za MF i lik frekvencije. Oba dele istu Y skalu."""
        if not self._racunato:
            return
        slabljenje = self.spn_slabljenje.value()
        mereno = self._nivoi_mereni is not None
        # slabljenje po redu važi samo za procenu bez merenja
        self.spn_slabljenje.setEnabled(not mereno)
        self.lbl_napomena.setText(tr("napomena_mereno") if mereno else tr("napomena"))

        def nivo(r: Rezultat) -> float:
            # izmereni (dBm) ako postoje, inače procena po redu (dB rel.)
            return self._nivoi_mereni[id(r)] if mereno else procena_nivoa(r.koef, slabljenje)

        gore: list[Panel] = []
        dole: list[Panel] = []
        if self.chk_sve.isChecked():
            frek = [r.frekvencija for r in self._okolina]
            if frek:
                a, b = min(frek + list(self._ciljevi.values())), max(frek + list(self._ciljevi.values()))
                marg = max((b - a) * 0.03, 0.01)
                gore.append(Panel(
                    tr("svi"),
                    a - marg, b + marg, "MHz",
                    [(f - self._granice[c] / 1000.0, f + self._granice[c] / 1000.0, c)
                     for c, f in self._ciljevi.items()],
                    [(r.frekvencija, nivo(r), r,
                      any(abs(r.frekvencija - f) * 1000.0 <= self._granice[c] for c, f in self._ciljevi.items()))
                     for r in self._okolina],
                ))
        else:
            for cilj, f_cilj in self._ciljevi.items():
                g = self._granice[cilj]
                panel = Panel(
                    dec(f"{naziv_cilja(cilj)}  {f_cilj:.4f} MHz  (±{g:g} kHz)"),
                    -g * self.OKOLINA, g * self.OKOLINA, tr("osa_x"),
                    [(-g, g, cilj)],
                    [(r.odstupanje, nivo(r), r, abs(r.odstupanje) <= g)
                     for r in self._okolina if r.cilj == cilj],
                )
                (gore if cilj == "RX" else dole).append(panel)

        # Zajednička Y skala za oba grafika, da se nivoi mogu porediti
        # (sa izmerenim nivoima i prag, da se linija praga uvek vidi)
        nivoi = [t[1] for pn in gore + dole for t in pn.tacke]
        prag = self.spn_prag.value() if mereno else None
        if prag is not None:
            nivoi.append(prag)
        y_max = 10 * math.ceil((max(nivoi, default=0) + 3) / 10)
        y_min = min(10 * math.floor((min(nivoi, default=-40) - 5) / 10), y_max - 30)
        self.grafik_rx.postavi(gore, (y_min, y_max), mereno=mereno, prag=prag)
        # Donji grafik ima smisla samo kad se računa MF (i nije režim "sve")
        self.grafik_mf.setVisible(bool(dole))
        self.grafik_mf.postavi(dole, (y_min, y_max), mereno=mereno, prag=prag)

    def _izaberi_u_tabeli(self, r: Rezultat):
        """Klik na liniju u grafiku bira odgovarajući red tabele (ako je
        proizvod u opsegu, pa postoji u tabeli)."""
        for i, t in enumerate(self.rezultati):
            if t is r:
                self.tabela.selectRow(i)
                self.tabela.scrollToItem(self.tabela.item(i, 0))
                return

    def _popuni_tabelu(self):
        self.tabela.setRowCount(len(self.rezultati))
        for vr, r in enumerate(self.rezultati):
            items = [
                QTableWidgetItem(naziv_cilja(r.cilj)),
                QTableWidgetItem(str(r.red)),
                QTableWidgetItem(r.formula),
                QTableWidgetItem(f"{r.frekvencija:.4f}"),
                QTableWidgetItem(f"{r.odstupanje:+.2f}"),
                QTableWidgetItem(f"{self._nivoi_mereni[id(r)]:.1f}" if self._nivoi_mereni is not None else ""),
            ]
            # Jak prodor (procenjeni nivo iznad praga): ceo red crveno i podebljano
            jak = self._jak(r)
            for kol, it in enumerate(items):
                if jak:
                    it.setForeground(QBrush(QColor("#c0392b")))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if kol != 2:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                # Redove sa prodorom na MF i lik obrubujemo bojom da se razlikuju
                if r.cilj == "MF":
                    it.setBackground(QBrush(QColor(255, 243, 205)))
                elif r.cilj.startswith("LIK"):
                    it.setBackground(QBrush(QColor(214, 234, 248)))
                self.tabela.setItem(vr, kol, it)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    prozor = Prozor()
    prozor.show()
    sys.exit(app.exec())
