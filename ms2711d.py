"""
Čitanje spektralnog traga sa Anritsu MS2711D (Spectrum Master) i rad sa
tragom: snimanje/učitavanje CSV, pronalaženje vrhova, nivo na frekvenciji.

Protokol (MS2711D Programming Manual 10580-00098): RS-232 9600 8N1 bez
handshaka, null-modem kabl. Koriste se samo komande koje ČITAJU:
  #69 (45h) Enter Remote       -> 13 bajtova (model, firmver)
  #70 (46h) Enter Remote odmah -> isto, ako #69 ne odgovori (dug sweep)
  #24 (18h) Query Trace Names  -> spisak sačuvanih tragova (gradi tabelu
            tragova u RAM-u, obavezno pre #33 za tragove 1-200)
  #33 (21h) Recall Sweep Trace -> trag (0 = poslednji sa ekrana, 1-200)
  #255 (FFh) Exit Remote       -> instrument nastavlja sa radom
Ništa se ne upisuje u instrument.

Serijski port se otvara preko pyserial, pa radi na Linuxu, Windows-u i macOS-u.
"""

import csv
import os
import statistics
import struct
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

MODEL_MS2711D = 0x16
MOD_SPEKTRALNI_ANALIZATOR = 0x30


class GreskaInstrumenta(Exception):
    pass


@dataclass
class Trag:
    """Jedan spektralni trag: frekvencije u MHz i nivoi u dBm."""

    frekvencije: list[float]
    nivoi: list[float]
    opis: str = ""          # npr. "MS2711D 09/27/2026 09:32:27, RBW 100 kHz"
    rbw_khz: float | None = None

    @property
    def korak(self) -> float:
        """Razmak između tačaka u MHz."""
        if len(self.frekvencije) < 2:
            return 0.0
        return (self.frekvencije[-1] - self.frekvencije[0]) / (len(self.frekvencije) - 1)

    @property
    def sum(self) -> float:
        """Procena nivoa šuma: medijana traga (vrhovi je malo pomeraju)."""
        return statistics.median(self.nivoi)

    def nivo_na(self, f_mhz: float) -> float | None:
        """Najviši nivo traga u okolini frekvencije (±max(1 tačka, RBW/2)),
        jer vrh na 401 tački retko pada tačno na traženu frekvenciju.
        None ako je frekvencija van opsega traga."""
        f0, f1 = self.frekvencije[0], self.frekvencije[-1]
        k = self.korak
        if not k or f_mhz < f0 - k or f_mhz > f1 + k:
            return None
        sirina = max(k, (self.rbw_khz or 0.0) / 2000.0)
        i0 = max(0, int((f_mhz - sirina - f0) / k))
        i1 = min(len(self.nivoi) - 1, int(round((f_mhz + sirina - f0) / k)))
        return max(self.nivoi[i0:i1 + 1])

    def vrhovi(self, iznad_suma_db: float) -> list[tuple[float, float]]:
        """Lokalni maksimumi bar `iznad_suma_db` iznad šuma, kao (MHz, dBm),
        od najjačeg. Vrhovi bliži od max(2 tačke, RBW) se spajaju (ostaje jači)."""
        prag = self.sum + iznad_suma_db
        n = self.nivoi
        kandidati = [
            (self.frekvencije[i], n[i])
            for i in range(len(n))
            if n[i] >= prag
            and (i == 0 or n[i] >= n[i - 1])
            and (i == len(n) - 1 or n[i] > n[i + 1])
        ]
        razmak = max(2 * self.korak, (self.rbw_khz or 0.0) / 1000.0)
        izabrani: list[tuple[float, float]] = []
        for f, p in sorted(kandidati, key=lambda t: -t[1]):
            if all(abs(f - g) > razmak for g, _ in izabrani):
                izabrani.append((f, p))
        return izabrani


# ---------- CSV ----------

def sacuvaj_csv(trag: Trag, putanja: str):
    with open(putanja, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# " + trag.opis])
        if trag.rbw_khz:
            w.writerow([f"# RBW_kHz={trag.rbw_khz:g}"])
        w.writerow(["frequency_MHz", "level_dBm"])
        for fr, p in zip(trag.frekvencije, trag.nivoi):
            w.writerow([f"{fr:.6f}", f"{p:.2f}"])


def ucitaj_csv(putanja: str) -> Trag:
    """Učitava trag iz teksta/CSV-a: prve dve brojčane kolone su frekvencija
    i nivo (dBm). Razdvojnik zarez, tačka-zarez ili tab; linije koje ne
    počinju brojem (zaglavlja, komentari) se preskaču. Jedinica frekvencije
    se pogađa: najveća vrednost > 100 000 znači Hz, inače MHz."""
    frek, nivoi = [], []
    rbw = None
    opis = os.path.basename(putanja)
    with open(putanja, encoding="utf-8", errors="replace") as f:
        for linija in f:
            s = linija.strip()
            if s.lstrip("# ").startswith("RBW_kHz="):
                try:
                    rbw = float(s.lstrip("# ").split("=", 1)[1].strip(' ",'))
                except ValueError:
                    pass
                continue
            delovi = [d.strip() for d in s.replace("\t", ";").replace(",", ";").split(";") if d.strip()]
            if len(delovi) < 2:
                continue
            try:
                fr, p = float(delovi[0]), float(delovi[1])
            except ValueError:
                continue
            frek.append(fr)
            nivoi.append(p)
    if len(frek) < 2:
        raise ValueError("no frequency/level data found")
    if max(frek) > 1e5:
        frek = [x / 1e6 for x in frek]
    par = sorted(zip(frek, nivoi))
    return Trag([a for a, _ in par], [b for _, b in par], opis, rbw)


# ---------- Serijska veza ----------

def portovi() -> list[str]:
    """Kandidati za serijski port (npr. /dev/ttyUSB0, COM3), USB adapteri prvo."""
    # na Linuxu comports() vraća i desetine ttyS bez hardvera (hwid "n/a")
    svi = [p for p in list_ports.comports() if p.hwid and p.hwid != "n/a"]
    usb = [p.device for p in svi if p.vid is not None]
    ostali = [p.device for p in svi if p.vid is None]
    return sorted(usb) + sorted(ostali)


class _Port:
    def __init__(self, putanja: str):
        try:
            # 9600 8N1, bez hardverskog i softverskog handshaka
            self.s = serial.Serial(putanja, 9600, bytesize=8, parity="N", stopbits=1,
                                   timeout=0.2, xonxoff=False, rtscts=False, dsrdtr=False)
        except serial.SerialException as e:
            raise GreskaInstrumenta(str(e)) from e
        self.s.reset_input_buffer()
        self.s.reset_output_buffer()

    def posalji(self, *bajtovi: int):
        self.s.write(bytes(bajtovi))
        self.s.flush()

    def citaj(self, n: int, timeout: float) -> bytes:
        buf = b""
        kraj = time.time() + timeout
        while len(buf) < n and time.time() < kraj:
            buf += self.s.read(n - len(buf))
        return buf

    def zatvori(self):
        self.s.close()


def procitaj(port: str, broj_traga: int = 0) -> tuple[Trag, list[tuple[int, str, str]]]:
    """Preuzima trag sa instrumenta (0 = poslednji sa ekrana, 1-200 =
    sačuvani) i spisak sačuvanih tragova kao (broj, datum/vreme, naziv).
    Instrument se na kraju uvek vraća iz remote režima."""
    p = _Port(port)
    u_remote = False
    try:
        # #69 čeka kraj sweep-a (kod sporog sweep-a može potrajati), pa #70
        p.posalji(0x45)
        r = p.citaj(13, 15)
        if len(r) < 13:
            p.posalji(0x46)
            r = p.citaj(13, 8)
        if len(r) < 13:
            raise GreskaInstrumenta(
                "no response from the instrument (check cable, port and that it is switched on)")
        u_remote = True
        model = struct.unpack(">H", r[0:2])[0]
        naziv_modela = r[2:9].decode("ascii", "replace").strip()
        firmver = r[9:13].decode("ascii", "replace")
        if model != MODEL_MS2711D:
            raise GreskaInstrumenta(f"unsupported instrument: {naziv_modela} (model {model:#x})")

        # #24: spisak sačuvanih tragova (i tabela u RAM-u za #33 1-200)
        p.posalji(0x18)
        h = p.citaj(2, 10)
        if len(h) < 2:
            raise GreskaInstrumenta("no response to trace list query")
        broj = struct.unpack(">H", h)[0]
        telo = p.citaj(41 * broj + 1, 10 + 0.05 * broj)
        spisak = []
        for i in range(broj):
            z = telo[i * 41:(i + 1) * 41]
            if len(z) < 41:
                break
            spisak.append((
                struct.unpack(">H", z[0:2])[0],
                z[3:21].decode("ascii", "replace"),
                z[25:41].decode("ascii", "replace").strip("\x00 "),
            ))

        # #33: trag
        p.posalji(0x21, broj_traga)
        h = p.citaj(2, 10)
        if len(h) == 1 and h[0] == 0xE0:
            raise GreskaInstrumenta(f"invalid trace location {broj_traga}")
        if len(h) < 2:
            raise GreskaInstrumenta("no response to trace recall")
        duzina = struct.unpack(">H", h)[0]
        telo = p.citaj(duzina, 20)
        if len(telo) < duzina:
            raise GreskaInstrumenta(f"incomplete trace ({len(telo)}/{duzina} bytes)")
        trag = _dekodiraj(h + telo, broj_traga, naziv_modela, firmver)
        return trag, spisak
    finally:
        if u_remote:
            p.posalji(0xFF)
            p.citaj(1, 3)
        p.zatvori()


def _dekodiraj(d: bytes, broj_traga: int, model: str, firmver: str) -> Trag:
    """Odgovor na #33 za režim spektralnog analizatora. Pozicije su
    1-bazirane kao u priručniku (tabela 'Recall Sweep Trace')."""
    if len(d) <= 11:
        raise GreskaInstrumenta(f"trace location {broj_traga} is empty")

    def u16(i):
        return struct.unpack(">H", d[i - 1:i + 1])[0]

    def u32(i):
        return struct.unpack(">I", d[i - 1:i + 3])[0]

    def dbm(v):
        return (v - 270000) / 1000.0

    mod = d[15]
    if mod != MOD_SPEKTRALNI_ANALIZATOR:
        raise GreskaInstrumenta(f"trace is not a spectrum analyzer trace (mode {mod:#x})")
    tacaka = u16(55)
    faktor = u16(335) or 1          # frekvencije su u jedinicama od `faktor` Hz
    start = u32(57) * faktor / 1e6
    stop = u32(61) * faktor / 1e6
    rbw_hz = u32(261)
    kraj = 432 + 4 * tacaka
    if len(d) < kraj - 1:
        raise GreskaInstrumenta("trace data too short")
    nivoi = [dbm(u32(432 + 4 * i)) for i in range(tacaka)]
    frek = [start + (stop - start) * i / (tacaka - 1) for i in range(tacaka)]
    datum = d[20:30].decode("ascii", "replace")
    vreme = d[30:38].decode("ascii", "replace")
    naziv = d[38:54].decode("ascii", "replace").strip("\x00 ")
    izvor = f"#{broj_traga}"
    opis = f"{model} {firmver} {izvor} {datum} {vreme} {naziv}".strip()
    return Trag(frek, nivoi, opis, rbw_hz / 1000.0 if rbw_hz else None)
