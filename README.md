# notrufmon – Verfügbarkeitsüberwachung Notruf 112

Messstellen (Raspberry Pi + Yeastar LTE-Gateway) rufen in festen Abständen aus dem
VoIP-Festnetz und den drei Mobilfunknetzen die 112 an. Die Leitstelle leitet die
bekannten Testrufnummern auf das Prüfsystem um, das jeden Anruf annimmt und bewertet:
zugestellt? Rufnummer korrekt? Ton in beide Richtungen? Fällt die Verfügbarkeit unter
einen Schwellwert, wird alarmiert.

Basis ist der SIP/RTP-Kern von voipmon (reine Python-Standardbibliothek).

## Stand

| Teil | Status |
|---|---|
| SIP-UA, RTP/RTCP (aus voipmon) | übernommen |
| Tonprüfung, die Mobilfunk-Umkodierung übersteht (`notrufmon/tones.py`) | fertig, getestet |
| Terminierung als SIP-Trunk, Zuordnung, Ergebnisklassen (`notrufmon/server/`) | fertig, lokal Ende zu Ende getestet |
| Probe-Ablauf (SIP-Leitungen, Ankündigung, Zwischenspeicher) | fertig, Ende zu Ende getestet |
| Anbindung Yeastar TG200/TG400 | wenn Gerät verfügbar |
| Server: Datenbank, Probe-API, Weboberfläche, Docker-Compose | fertig, Ende zu Ende getestet |
| Alarmierung, Berichte, Fernwartung (WireGuard) | geplant |

Installation: [docs/INSTALLATION.md](docs/INSTALLATION.md)

## Tests

```bash
python3 -m pytest -q
```

Die Systemtests brauchen PostgreSQL (`NOTRUFMON_TEST_DSN`), sonst werden sie übersprungen.
